Local Time in Prompts
======================

Overview
--------
This project optionally adds structured local time context to prompts built by
``core.prompt_engine.build_prompt_request``.

Fields added (when enabled)
---------------------------
- ``local_time``: string in ``HH:MM`` 24-hour format (example: ``"04:30"``). No timezone name or UTC markers are included.
- ``local_hour``: integer hour (0-23)
- ``time_of_day``: categorical label, one of ``night``, ``early_morning``, ``morning``, ``afternoon``, ``evening``, ``late_evening`` (``early_morning`` corresponds to 04:00–05:59).
- ``local_date``: optional date string ``YYYY-MM-DD`` for local date context.

In the typed prompt path, the authoritative local date/time is folded into the
runtime context used by renderers, so the current turn can be prefixed with a
compact local timestamp without exposing timezone names or offsets.

Reality anchor in the current turn
----------------------------------
``_build_context_summary()`` renders the full ``[SYSTEM: REALITY ANCHOR]`` block
(date, time, season, location, ``Temporal Delta``) into
``PromptRequest.context_summary``, which every renderer merges into the **system**
message. On a long conversation that block can sit far from the text being
generated, so ``RuntimeContext.reality_anchor`` (built by
``_build_current_turn_anchor()``) carries a compact one-line duplicate of the same
facts: ``[SYSTEM: REALITY ANCHOR] Monday, April 20, 2026 · 9:27 PM (late evening) ·
Mid Spring · Sečovlje,Slovenia``.

``OpenAIRenderer``, ``AnthropicRenderer``, ``GeminiRenderer`` and ``TextRenderer``
each place that line on its own line immediately above the current user turn,
ahead of the ``[lang:… | time_of_day:…]`` metadata bracket. The stable
``Temporal Delta`` sentence is deliberately **not** duplicated — it stays in the
system block only. The line is omitted entirely when a turn carries no temporal
fields. Note that it does carry the exact clock time and location; the
``TIME AUTHORITY`` instruction still tells the model not to quote those back in
ordinary replies.

Configuration
-------------
- ``INCLUDE_LOCAL_TIME_IN_PROMPTS`` (component: ``prompt_engine``) — boolean, default ``True``. When ``False``, the fields above are not included.

Privacy & Implementation Notes
------------------------------
- No timezone names, offsets, or UTC timestamps are included in prompts by default to avoid leaking location information. If the session sets a timezone in session meta (``session_meta`` key ``timezone``), it is used to compute the local time, otherwise the server TZ configured via the project is used.
- The mapping of labels is deterministic and test-covered. Service operators can disable the feature via the config var for privacy-sensitive deployments.
- ``build_json_prompt()`` is now a deprecated alias kept for compatibility.

Testing
-------
Unit tests are provided under ``tests/test_time_zone_utils.py`` and ``tests/test_prompt_engine_time_fields.py``. The duplicated current-turn anchor is covered by ``tests/test_prompt_engine.py`` (anchor construction + prompt plumbing) and ``tests/test_prompt_renderers.py`` (per-renderer placement).
