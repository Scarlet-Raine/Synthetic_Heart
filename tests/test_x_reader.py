"""Tests for the X Reader plugin (plugins/x_reader)."""

from typing import Any

import pytest

# ``plugins.x_reader/__init__`` rebinds the package to the ``x_reader`` module
# via a sys.modules shim (same as plugins.goals), so a plain
# ``import plugins.x_reader.x_reader`` fails at runtime. Trigger the package
# import first, then bind the concrete submodule in sys.modules.
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - type-checker resolution only
    pass
else:
    import importlib

    xr = importlib.import_module("plugins.x_reader.x_reader")


def _clear_cache() -> None:
    xr._cache.clear()


# ── Structural URL parsing ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/ImBreckWorsham/status/2095223236841673141",
        "https://twitter.com/user/status/1234567890123456789",
        "https://www.x.com/a/status/42?lang=en",
        "https://mobile.twitter.com/handle/status/9001/photo/1",
        "http://x.com/u/status/777",
    ],
)
def test_extract_tweet_id_valid(url: str) -> None:
    assert xr._extract_tweet_id(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "https://example.com/status/123",
        "https://x.com/user/post/123",
        "x.com/user/status/123",  # missing scheme
        "https://youtube.com/watch?v=123",
        "https://x.com/user",
        "https://x.com/user/status/notanumber",
    ],
)
def test_extract_tweet_id_rejects_garbage(url: Any) -> None:
    assert xr._extract_tweet_id(url) is None


# ── Tier resolution ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_oembed_tier_returns_text(monkeypatch) -> None:
    _clear_cache()

    def fake_http(url: str):
        if "publish.twitter.com/oembed" in url:
            return {
                "html": (
                    '<blockquote class="twitter-tweet"><p lang="en" dir="ltr">'
                    "The assassination of my husband was a great tragedy, but "
                    "what really bothers me is when people don't like Israel."
                    "</p>— Erika Kirk (@ErikaKirk) "
                    '<a href="https://x.com/u/status/123">June 15, 2026</a>'
                    "</blockquote>"
                ),
                "author_name": "Erika Kirk",
                "author_url": "https://twitter.com/ErikaKirk",
            }
        return None

    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    result = await xr.fetch_post("123")
    assert result["success"] is True
    assert result["source"] == "oembed"
    assert "Israel" in result["text"]
    assert result["author_name"] == "Erika Kirk"


@pytest.mark.asyncio
async def test_cascade_to_syndication_when_oembed_unavailable(monkeypatch) -> None:
    _clear_cache()

    calls: list[str] = []

    def fake_http(url: str):
        calls.append(url)
        if "publish.twitter.com/oembed" in url:
            return None
        if "syndication.twimg.com" in url:
            return {
                "text": "hello twitter world",
                "user": {"name": "Scar", "screen_name": "scar"},
                "created_at": "2026-01-01T00:00:00.000Z",
                "entities": {
                    "media": [
                        {"media_url_https": "https://pbs.twimg.com/media/1.jpg"},
                        {"media_url_https": "https://pbs.twimg.com/media/2.jpg"},
                    ]
                },
            }
        return None

    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    result = await xr.fetch_post("456")
    assert result["success"] is True
    assert result["source"] == "syndication"
    assert result["text"] == "hello twitter world"
    assert result["author_screen_name"] == "scar"
    assert result["media"] == [
        "https://pbs.twimg.com/media/1.jpg",
        "https://pbs.twimg.com/media/2.jpg",
    ]
    assert any("syndication" in u for u in calls)


@pytest.mark.asyncio
async def test_syndication_media_stripped_when_not_requested(monkeypatch) -> None:
    _clear_cache()

    def fake_http(url: str):
        if "publish.twitter.com/oembed" in url:
            return None
        if "syndication.twimg.com" in url:
            return {
                "text": "no pics please",
                "user": {"name": "A", "screen_name": "a"},
                "entities": {"media": [{"media_url_https": "https://x/1.jpg"}]},
            }
        return None

    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    result = await xr.fetch_post("789", include_media=False)
    assert result["success"] is True
    assert "media" not in result


@pytest.mark.asyncio
async def test_fail_closed_when_all_tiers_empty(monkeypatch) -> None:
    _clear_cache()

    def fake_http(url: str):
        return None

    async def fake_search(tweet_id: str) -> dict:
        return {"source": "search_fallback", "results": [], "searched": True}

    monkeypatch.setattr(xr, "_http_get_json", fake_http)
    monkeypatch.setattr(xr, "_search_fallback", fake_search)

    result = await xr.fetch_post("111")
    assert result["success"] is False
    assert result["source"] == "search_fallback"
    assert result["reason"] == "no_indexed_content"


@pytest.mark.asyncio
async def test_search_fallback_tier_used_when_network_tiers_empty(monkeypatch) -> None:
    _clear_cache()

    def fake_http(url: str):
        return None

    async def fake_search(tweet_id: str) -> dict:
        return {
            "source": "search_fallback",
            "results": [
                {
                    "title": "t",
                    "snippet": "s",
                    "url": f"https://x.com/_/status/{tweet_id}",
                }
            ],
            "searched": True,
        }

    monkeypatch.setattr(xr, "_http_get_json", fake_http)
    monkeypatch.setattr(xr, "_search_fallback", fake_search)

    result = await xr.fetch_post("222")
    assert result["success"] is True
    assert result["source"] == "search_fallback"
    assert len(result["results"]) == 1


@pytest.mark.asyncio
async def test_method_override_pins_single_tier(monkeypatch) -> None:
    _clear_cache()

    def fake_http(url: str):
        return {"html": "<p>only via oembed</p>", "author_name": "A"}

    monkeypatch.setattr(xr, "X_FETCH_METHOD", "syndication")
    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    result = await xr.fetch_post("333")
    assert result["success"] is False
    assert result["reason"] == "unavailable"


@pytest.mark.asyncio
async def test_cache_serves_second_fetch(monkeypatch) -> None:
    _clear_cache()

    calls: list[str] = []

    def fake_http(url: str):
        calls.append(url)
        if "publish.twitter.com/oembed" in url:
            return {"html": "<p>cached text</p>", "author_name": "A"}
        return None

    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    first = await xr.fetch_post("555")
    second = await xr.fetch_post("555")
    assert first["success"] is True and second["success"] is True
    assert len(calls) == 1  # second call served from cache


# ── execute_action ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execute_action_rejects_non_status_url(monkeypatch) -> None:
    _clear_cache()

    def fake_http(url: str):  # pragma: no cover - must not be reached
        raise AssertionError("fetch must not run for a malformed URL")

    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    plugin = xr.XReaderPlugin()
    out = await plugin.execute_action(
        {"type": "x_fetch_post", "payload": {"url": "https://example.com/status/1"}},
        {},
        bot=None,
        original_message=None,
    )
    assert out["success"] is False
    assert out["reason"] == "not_an_x_status_url"


@pytest.mark.asyncio
async def test_execute_action_is_pure_tool_when_agent_tool(monkeypatch) -> None:
    """As an agent tool (context carries ``agent_tool: True``) the action must
    return the fetched result to the bounded loop WITHOUT enqueuing a separate
    LLM delivery turn — the loop-starvation/delivery-spam bug class."""
    _clear_cache()
    import core.auto_response as auto_response

    def fake_http(url: str):
        if "publish.twitter.com/oembed" in url:
            return {
                "html": "<p>agent sees this</p>",
                "author_name": "Agent",
                "author_url": "",
            }
        return None

    monkeypatch.setattr(xr, "_http_get_json", fake_http)

    async def _fail_if_delivery(*args, **kwargs):
        raise AssertionError(
            "request_llm_delivery must not be called when x_fetch_post runs "
            "as an agent tool"
        )

    monkeypatch.setattr(auto_response, "request_llm_delivery", _fail_if_delivery)

    plugin = xr.XReaderPlugin()
    out = await plugin.execute_action(
        {"type": "x_fetch_post", "payload": {"url": "https://x.com/u/status/999"}},
        {"agent_tool": True, "interface_path": "telegram_bot/1"},
        bot=None,
        original_message=None,
    )
    assert out["success"] is True
    assert out["source"] == "oembed"
    assert "agent sees this" in out["text"]


@pytest.mark.asyncio
async def test_execute_action_returns_schema_for_other_actions(monkeypatch) -> None:
    plugin = xr.XReaderPlugin()
    out = await plugin.execute_action(
        {"type": "something_else", "payload": {}},
        {},
        bot=None,
        original_message=None,
    )
    assert out["success"] is False
    assert out["reason"] == "unsupported_action"


def test_prompt_instructions_present() -> None:
    plugin = xr.XReaderPlugin()
    instructions = plugin.get_prompt_instructions("x_fetch_post")
    assert "THIRD-PARTY DATA" in str(instructions.get("usage", ""))


def test_schema_shape() -> None:
    plugin = xr.XReaderPlugin()
    schema = plugin.get_supported_actions()["x_fetch_post"]
    assert schema["required_fields"] == ["url"]
    assert "include_media" in schema["optional_fields"]
    assert schema["security_level"] == "low"
