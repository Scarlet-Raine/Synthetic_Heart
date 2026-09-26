# Grillo — Memory Compaction Beat

Part of the [G.R.I.L.L.O.](../guide.md) background subsystem.

## Purpose

Nightly memory housekeeping. Groups older memories by tag, asks the active LLM
to synthesize each cluster into a single compacted memory, archives the source
rows into `archived_memories`, and inserts the new compacted memory back into
`memories` with LLM-suggested tags and feeling. Keeps long-term memory dense and
useful instead of unbounded.

## Beat

- **Beat type:** `memory_consolidation`
- **Selection:** runs nightly at `GRILLO_COMPACT_TIME`.
- **Output:** compacted memory rows; source memories moved to the archive.

## Run Now

The plugin is **runnable** from the WebUI Plugins tab: the **Run compaction**
button triggers a single compaction cycle on demand (dispatched to
`run_action("compact_now", ...)`) without waiting for the nightly schedule.

## How it works

The plugin clusters candidate memories older than `GRILLO_COMPACT_AGE_DAYS`,
skips clusters below the minimum size, summarizes each with the Grillo cortex
(bounded by the max-chars/ratio limits), then archives and replaces them.
Discovery is automatic via the plugin registry.

The **day-unit pass** (`GRILLO_COMPACT_DAY_UNITS`, default on) is what a normal
run uses: one diary day in, one memory out. Clustering stays available by turning
that key off.

## Reliability rules worth knowing

- **The `emotion` label is bounded.** The compactor writes the model's
  `feeling` into `memories.emotion`. Long-lived stores may still declare that
  column `varchar(50)` while the schema in `scripts/sql/app_main_postgres.sql`
  says `TEXT`, so the label is trimmed at the narrowest declared width by
  `_bound_emotion` and the **full** feeling is kept in
  `archived_memories.notes["feeling"]`. A cosmetic label must never be able to
  fail a whole day's write. The startup migration
  `core/migrations.py::_widen_memories_text_columns` aligns such a store with
  the declared schema on the next boot (idempotent, fail-open, and it only
  widens columns it measured as bounded).
- **A day that already failed is done for the night.** Only `persisted` clears a
  day; `write_failed`, `anchors_failed`, `no_compression`, `kept_raw` and
  `unparseable` all leave the day exactly as it was, so the remaining cycles do
  not pay for the same answer again. The set is cleared at the start of the
  nightly run and of a manual `compact_now`.
- **A failed write never loses the day.** The memory row is written first and
  the day is only archived and removed afterwards, so a failure leaves the raw
  day in `ai_diary` (the day is then retried on the following night).

## Configuration

| Key | Purpose |
|-----|---------|
| `GRILLO_COMPACT_ENABLED` | Enable/disable this beat. |
| `GRILLO_COMPACT_TIME` | Time of day (HH:MM) to run compaction. |
| `GRILLO_COMPACT_CYCLES` | How many compaction cycles per run. |
| `GRILLO_COMPACT_BATCH_SIZE` | Memories processed per batch. |
| `GRILLO_COMPACT_AGE_DAYS` | Minimum age before a memory is a candidate. |
| `GRILLO_COMPACT_WINDOW_DAYS` | Time window grouped together. |
| `GRILLO_COMPACT_MIN_CLUSTER_SIZE` | Smallest tag cluster worth compacting. |
| `GRILLO_COMPACT_MAX_SUMMARY_CHARS` | Hard cap on a compacted summary length. |
| `GRILLO_COMPACT_MAX_SUMMARY_RATIO` | Summary length relative to source. |

Plus the shared Grillo settings. See the [G.R.I.L.L.O. guide](../guide.md).
