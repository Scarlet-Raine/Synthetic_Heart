"""Tests for the first-run setup page and the redirect that offers it.

The page is the only thing a native install asks the user, so it has to render
without a browser, without a database, and without any external endpoint being
configured. The redirect must also be conservative: pushing an existing
deployment at a setup page would be worse than never showing it.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from core import webui as webui_module


def _renderer():
    """Call the renderer without building the whole WebUI interface.

    ``_render_setup`` only touches ``self.logo_url``, so a stub is enough and
    the test stays fast and database-free.
    """
    stub = types.SimpleNamespace(logo_url="/static/synth_logo_bg.png")
    return webui_module.SynthWebUIInterface._render_setup(stub)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_page_renders() -> None:
    html = _renderer()
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_no_placeholder_survives() -> None:
    html = _renderer()
    leftovers = [
        token for token in ("%%", "%%BRAND_NAME%%", "%%TZ_OPTIONS%%") if token in html
    ]
    assert leftovers == []


def test_page_asks_for_the_things_the_installer_deliberately_skips() -> None:
    """Persona, household, and engine are exactly what the page is for."""
    html = _renderer()
    for config_key in (
        "SYNTH_NAME",
        "TRAINER_NAME",
        "SYNTH_PROFILE",
        "TZ",
        "PROMPT_LOCATION",
        "SCENE_NOTE",
        "PROJECT_DEFAULT_LANGUAGE",
        "BASE_CORTEX",
        "SETUP_COMPLETED",
    ):
        assert config_key in html, f"{config_key} is never saved by the page"


def test_timezone_and_language_dropdowns_are_populated() -> None:
    html = _renderer()
    assert html.count("<option") > 100, (
        "the timezone list should be the full IANA catalogue"
    )
    assert 'value="en"' in html, "English must always be offered"


def test_uses_the_configured_accent_colour() -> None:
    html = _renderer()
    assert "--accent: #6bfefe" in html or "--accent: rgb" in html


def test_page_is_self_contained() -> None:
    """No CDN, no build step: it must work offline on a fresh install."""
    html = _renderer()
    for pattern in ("http://", "https://", 'src="//'):
        # The only absolute URL allowed is the provider API addresses the user
        # types in themselves, which appear in placeholders.
        assert pattern not in html.replace("https://api.openai.com", "")


# ---------------------------------------------------------------------------
# the redirect
# ---------------------------------------------------------------------------


def _stub():
    """A stand-in for the WebUI that borrows the real gate methods.

    ``_setup_completed`` is looked up on the class at call time, so the
    monkeypatches below take effect without building the whole interface.
    """
    stub = types.SimpleNamespace()
    stub._setup_completed = lambda: webui_module.SynthWebUIInterface._setup_completed(
        stub
    )
    return stub


def _pending() -> bool:
    return asyncio.run(webui_module.SynthWebUIInterface._first_run_pending(_stub()))


def test_no_redirect_once_the_page_was_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: True
    )
    assert _pending() is False


def test_no_redirect_when_an_endpoint_is_already_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing deployment must never be pushed at a setup page."""

    class _Registry:
        async def list_endpoints(self, enabled_only: bool = False):
            return [object()]

    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: False
    )
    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry",
        lambda: _Registry(),
    )
    assert _pending() is False


def test_no_redirect_when_the_registry_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doubt resolves to 'do not redirect'."""
    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: False
    )

    def boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry", boom
    )
    assert _pending() is False
