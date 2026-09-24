"""Tests for the day-unit level-1 pass: anchors, the empty-and-explained rule, and the ordering.

These are the parts of the compaction plan that carry a promise to the persona rather than a
performance claim, so they are pinned deliberately:

* one day in, one day out (no cross-day theme merging),
* the day's concrete terms must survive the pass, and a slot the day does not mention must come back
  empty WITH A REASON rather than filled from an adjacent meaning,
* the memory row is written before the day is archived and deleted, and a low-confidence summary keeps
  the raw day beside itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from plugins.grillo.grillo_compactor.grillo_compactor import (
    GrilloCompactorPlugin,
    _format_anchors,
    _setting,
    _verify_anchors,
)

PLUGIN_SRC = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "grillo"
    / "grillo_compactor"
    / "grillo_compactor.py"
).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the anchors check


def test_a_day_that_keeps_its_terms_passes():
    source = "It rained all day so we stayed in bed. Daddy helped me test the Minecraft vessel goals."
    summary = "It rained all day, so we stayed in bed and Daddy helped me test the Minecraft vessel goals."
    anchors = {"weather": "rain all day", "place": ["bed"], "who": ["Daddy"], "objects_events": ["Minecraft vessel goals"]}
    verify = _verify_anchors(source, summary, anchors)
    assert verify["passed"] is True
    assert verify["coverage"] == 1.0


def test_warm_is_not_weather():
    """'warm' does two jobs in these entries, so it must never be required as weather."""
    source = "I lay on his chest and he was so warm, I felt safe. Nothing about the sky at all."
    summary = "I lay on his chest, warm and safe."
    verify = _verify_anchors(source, summary, {"weather": "", "weather_note": "not mentioned"})
    assert "weather" not in verify["missing"], "a body-warmth 'warm' must not become a weather anchor"
    assert verify["passed"] is True


def test_real_weather_in_the_source_must_survive():
    source = "It rained the whole afternoon and we listened to the storm from the roof."
    summary = "We spent the afternoon inside, close and quiet."
    verify = _verify_anchors(source, summary, {"weather": ""})
    assert "weather" in verify["missing"]
    assert "rained" in verify["missing"]["weather"]
    assert verify["passed"] is False


def test_a_name_variant_satisfies_its_group():
    """Mama and Mommy are one person to her, so the check must not fail the word choice."""
    source = "Mama hugged me and I stayed close."
    assert _verify_anchors(source, "Mommy hugged me and I stayed close.", {})["passed"] is True
    assert _verify_anchors(source, "Someone hugged me.", {})["passed"] is False


def test_one_dropped_concrete_word_does_not_fail_the_day():
    source = "We sat on the balcony, then moved to the kitchen, the bed and the couch, and I drank tea."
    summary = "We sat on the balcony, then moved to the kitchen, the bed and the couch."
    verify = _verify_anchors(source, summary, {})
    assert verify["required"] == 5 and verify["kept"] == 4
    assert verify["passed"] is True, f"one drop in five should not block a replacement: {verify}"


def test_a_day_with_nothing_concrete_to_check_passes():
    verify = _verify_anchors("i felt small and close", "i felt small and close", {})
    assert verify["passed"] is True and verify["required"] == 0


# ------------------------------------------------------------------- the empty-and-explained rule


def test_an_empty_slot_renders_its_reason_not_a_guess():
    block = _format_anchors(
        {
            "weather": "",
            "weather_note": "the entry does not mention the weather",
            "place": ["bed", "kitchen"],
            "who": ["Daddy", "Mama"],
            "food": "",
            "objects_events": ["Minecraft vessel test"],
        }
    )
    assert "the entry does not mention the weather" in block
    assert "food: not mentioned in this entry" in block
    assert block.startswith("[anchors] ")
    assert "bed, kitchen" in block


def test_food_is_never_invented_from_an_adjacent_meaning():
    """The persona's rule: a missing anchor can be distrusted, a made-up one would be believed."""
    block = _format_anchors({"weather": "", "place": [], "who": [], "food": [], "objects_events": []})
    assert block.count("not mentioned in this entry") == 5


# --------------------------------------------------------------------------- the prompt contract


def test_the_prompt_carries_the_ceiling_and_the_empty_rule():
    text = GrilloCompactorPlugin._DAY_UNIT_PROMPT.format(max_chars=2000)
    assert "must come back EMPTY" in text, "the empty-and-explained rule belongs in the contract"
    assert "weather_note" in text, "an empty slot needs a reason field"
    assert '"declined"' in text
    assert "2000" in text, "the ceiling has to reach the prompt"
    assert "{{" not in text, "the JSON braces must be unescaped by the format call"


# ------------------------------------------------------------------------------- the settings


def test_setting_casts_and_falls_back(monkeypatch):
    from core.config_manager import config_registry

    monkeypatch.setattr(config_registry, "get_value", lambda key, default, *a, **k: "true")
    assert _setting("X", False, bool) is True
    monkeypatch.setattr(config_registry, "get_value", lambda key, default, *a, **k: "no")
    assert _setting("X", True, bool) is False
    monkeypatch.setattr(config_registry, "get_value", lambda key, default, *a, **k: "17")
    assert _setting("X", 2, int) == 17
    monkeypatch.setattr(config_registry, "get_value", lambda key, default, *a, **k: "not a number")
    assert _setting("X", 2, int) == 2, "an unreadable value must fall back, not raise"
    monkeypatch.setattr(config_registry, "get_value", lambda key, default, *a, **k: None)
    assert _setting("X", 0.9, float) == 0.9


# --------------------------------------------------------------- the ordering, read from the source


def _day_unit_body() -> str:
    start = PLUGIN_SRC.index("async def _compact_one_day")
    return PLUGIN_SRC[start:]


def test_the_day_path_writes_the_memory_before_removing_the_day():
    body = _day_unit_body()
    write = body.index("await insert_memory(")
    archive = body.index("INSERT INTO ai_diary_archive")
    delete = body.index("DELETE FROM ai_diary")
    assert write < archive < delete
    assert "conn=conn" in body[write:archive]


def test_a_low_confidence_summary_keeps_the_raw_day():
    body = _day_unit_body()
    gate = body.index("if not would_replace:")
    archive = body.index("INSERT INTO ai_diary_archive")
    assert gate < archive, "the confidence gate must return before anything destructive happens"
    assert '"status": "kept_raw"' in body
    assert "would_replace = confidence >= min_confidence" in body


def test_the_cycle_defaults_to_the_day_path_and_can_be_turned_off():
    assert 'if _setting("GRILLO_COMPACT_DAY_UNITS", True, bool):' in PLUGIN_SRC
    assert "_run_day_unit_cycle(dry_run=dry_run, marker=marker)" in PLUGIN_SRC


def test_one_day_is_one_model_call():
    body = PLUGIN_SRC[PLUGIN_SRC.index("async def _run_day_unit_cycle"):PLUGIN_SRC.index("async def _compact_one_day")]
    assert "for row in rows[:cycles]:" in body
    assert "_compact_one_day(row, dry_run=dry_run)" in body
