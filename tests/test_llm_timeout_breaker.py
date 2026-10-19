"""Consecutive LLM timeouts open a per-endpoint circuit instead of stacking waits."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.external_endpoints import timeout_breaker
from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol
from core.external_endpoints.timeout_breaker import TimeoutBreaker


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_circuit_opens_after_threshold_and_probes_after_cooldown() -> None:
    clock = _Clock()
    breaker = TimeoutBreaker(threshold=2, cooldown=100.0, clock=clock)
    assert breaker.acquire() == (True, False)
    breaker.record_timeout()
    assert breaker.acquire() == (True, False)  # still closed after one
    breaker.record_timeout()
    assert breaker.is_open
    assert breaker.acquire() == (False, False)  # fail fast during cooldown

    clock.now += 101
    assert breaker.acquire() == (True, True)  # single probe
    assert breaker.acquire() == (False, False)  # others still blocked

    breaker.record_timeout()  # probe failed -> re-open for another cooldown
    assert breaker.acquire() == (False, False)
    clock.now += 101
    assert breaker.acquire() == (True, True)
    breaker.record_success()
    assert not breaker.is_open
    assert breaker.acquire() == (True, False)


def test_released_probe_allows_another() -> None:
    clock = _Clock()
    breaker = TimeoutBreaker(threshold=1, cooldown=10.0, clock=clock)
    breaker.record_timeout()
    clock.now += 11
    assert breaker.acquire() == (True, True)
    breaker.release_probe()
    assert breaker.acquire() == (True, True)


class _HangingAdapter:
    def __init__(self) -> None:
        self._last_completion_metadata: dict[str, Any] = {}
        self.calls = 0
        self.timeouts: list[float] = []

    async def chat_completion(self, msgs: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.timeouts.append(float(kwargs["timeout"]))
        await asyncio.sleep(3600)


def _bridge(adapter: _HangingAdapter) -> ExternalCortexEngine:
    endpoint = SimpleNamespace(
        name="wedged",
        display_label="Wedged",
        default_model="m",
        available_models=[],
        extra_config={"timeout": 0.05, "max_retries": 5, "retry_on_timeout": True},
        protocol=EndpointProtocol.OPENAI,
    )
    return ExternalCortexEngine(
        endpoint=cast(Any, endpoint), adapter=cast(Any, adapter)
    )


@pytest.mark.asyncio
async def test_bridge_stops_retrying_and_then_fails_fast() -> None:
    adapter = _HangingAdapter()
    bridge = _bridge(adapter)
    msgs = [{"role": "user", "content": "hi"}]

    with pytest.raises(TimeoutError):
        await bridge.generate_response(msgs)
    # Retries stop as soon as the circuit opens (threshold = 2 attempts).
    assert adapter.calls == timeout_breaker.THRESHOLD

    with pytest.raises(TimeoutError, match="failing fast"):
        await bridge.generate_response(msgs)
    assert adapter.calls == timeout_breaker.THRESHOLD  # backend not touched
