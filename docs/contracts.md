# contracts.md — the frozen data contract (v2)

Every stage reads a **Transcript** and writes a **Transcript**. This file is
the single source of truth and does not change as part of a feature task.

## Changelog

The schema version is **v2** and stays there. This block records changes made
*inside* v2 — enum and warning-object amendments that break no consumer of the
v2 Transcript or SubtitleFile schemas (D23). A change that altered either
schema would be a version bump and a separate, explicit task; none of the
entries below is one.

| Date | Decisions | Change |
|---|---|---|
| 2026-08-26 | D62 | `second_opinion` added to the edit-reason enum, emitted by `proofread` when two ASR models disagree and exactly one candidate is a real Hebrew word. |
| 2026-08-21 | D23, D25 | This changelog block added. `hebrew_rule_violation` added to the code enum, emitted by `segment` when it splits against a Hebrew rule because no legal split exists. |
| — | D16, D17, D18 | Warning object shape specified rather than shown by example (D18); `itn_skipped` deleted from the code enum and `gap_not_applied` added (D17, D16). |
| — | decisions-001 | v2 itself: added `meta.warnings`, added stable `wid` to words, edits re-keyed onto `wid`, removed `itn` from the reason enum (deferred to v2 of the product), defined re-run behaviour, fixed the CPS formula. |

Entries dated `—` predate this changelog and their dates were not recorded.
Do not backfill them with guesses.

## Transcript

```json
{
  "meta": {
    "source_file": "interview_dana.mp4",
    "duration": 612.4,
    "language": "he",
    "engine": "ivrit_local",
    "engine_version": "whisper-large-v3-turbo-ct2-20250513",
    "created_at": "2026-08-21T14:03:00Z",
    "stages": ["transcribe", "proofread"],
    "warnings": []
  },
  "segments": [
    {
      "id": 0,
      "start": 1.240,
      "end": 4.880,
      "text": "אז בוא נדבר על מה שקרה בקמפיין האחרון.",
      "words": [
        { "wid": 0, "w": "אז",  "start": 1.240, "end": 1.390, "conf": 0.98 },
        { "wid": 1, "w": "בוא", "start": 1.390, "end": 1.610, "conf": 0.94 }
      ],
      "speaker": null
    }
  ],
  "edits": []
}
```

`META_KEYS` is exactly: `source_file, duration, language, engine,
engine_version, created_at, stages, warnings`.

## Field rules

- `wid` — **stable global word index.** Assigned by `transcribe`, starting at 0,
  increasing across the whole file. Never renumbered, never reused, by any
  stage. This is the only durable identity a word has.
- `id` — segment index. `segment` renumbers these; nothing else may.
- `start` / `end` — seconds, float, 3 decimals, non-decreasing across the file,
  `end > start`.
- `text` — must equal `" ".join(w["w"] for w in words)` at every stage.
- `conf` — 0.0–1.0, or `null` when the engine provides none. Never 0 as a stand-in.
- `speaker` — `null` in v1.

## Timestamp immutability

After `transcribe`, no stage may modify any `start` or `end`.

`transcribe` **may** clamp engine output to satisfy monotonicity — Whisper
occasionally emits overlapping word timings. Each clamp appends a warning with
code `timing_clamped`. Transcribe never raises for this; immutability begins
once transcribe returns.

`proofread` may change `w` and `text`. It may not add, remove, or reorder
words, and may not touch timings.

`segment` may **regroup** words into new segments. It may merge adjacent
groups and split a group. A new segment's `start` is its first word's `start`
and `end` is its last word's `end` — never computed any other way. Words keep
their `wid`.

## Warnings

Every stage may append. Nothing is ever silently dropped.

```json
{ "stage": "segment", "code": "cps_exceeded", "wid_start": 44, "wid_end": 51,
  "detail": "22.4 CPS over limit 17" }
```

### Object shape

| Key | Required | Notes |
|---|---|---|
| `stage` | yes | emitting module name |
| `code` | yes | from the enum below |
| `detail` | yes | human-readable, non-empty |
| `wid_start` | no | present when the warning concerns specific words |
| `wid_end` | no | as above |

If either `wid_*` is present, both must be, and `wid_start <= wid_end`.
A single-word warning sets both to the same value; it is not required to.
File-level warnings omit both. No other keys are permitted.

### Codes in v1

Exactly these eight:

`timing_clamped`, `cps_exceeded`, `card_too_short`, `line_too_long`,
`hebrew_rule_violation`, `llm_rejected`, `edit_budget_hit`, `gap_not_applied`.

`itn_skipped` was in the v2 draft and is **deleted** — no v1 pass was funded
to emit it, and a code nobody writes is dead schema. See `NOTES.md` for the
v2 intent.

`gap_not_applied` is emitted by `export` into its report sidecar, not into
`meta.warnings`; export produces no Transcript. See `docs/modules/export.md`.

`hebrew_rule_violation` is emitted by `segment`, and only by `segment` (D25).
It is the sole record that a Hebrew splitting rule was broken — no other stage
re-derives that judgement from the output, because two implementations of the
same rules drift. Its `detail` opens with the rule id, so a consumer can group
violations by rule without containing any Hebrew logic. The rule ids and the
`detail` format are in `docs/modules/segment.md`.

Warnings are **not** failures. They are the record of where the tool could not
fully meet its own quality targets, and they surface in the CLI summary.

## Edits log

`edits` records **changes actually applied**. Anything considered and not
applied is a warning, not an edit. This keeps "what changed in my transcript"
answerable from one list.

```json
{ "stage": "proofread", "wid": 47, "before": "צליל", "after": "צריך",
  "reason": "glossary" }
```

`reason` ∈ `{ glossary, punctuation, llm }`. `itn` is not in v1.

Because edits key on `wid`, they survive segmentation. Mapping an edit to the
final subtitle card is a lookup, not a reconstruction.

## Re-running a stage

If a stage's name is already in `meta.stages`, the stage raises
`StageAlreadyRun` unless `--force` is passed. With `--force` it runs again and
does not append a duplicate name.

Idempotency is therefore stated as: **running proofread twice with `--force`
produces output identical except for `meta.warnings`.** Byte-identity is not
the test; content-identity of `segments` and `edits` is.

Likewise "all passes disabled → output equals input" means all of `segments`
and `edits` are unchanged. `meta.stages` still gains the stage name.

## SubtitleFile (output of `segment`, input to `export`)

Each segment additionally carries:

```json
{ "lines": ["שורה ראשונה", "שורה שנייה"], "cps": 14.2 }
```

`lines` has 1 or 2 entries, never 3.
`cps = len(" ".join(lines)) / (end - start)` — the separator counts, matching
the `text` invariant.

## Hard rules vs. quality targets

Hard rules are invariants and are tested as such:

- ≤ 2 lines per card
- ≤ max line length
- ≤ max card duration (always achievable by splitting)
- every card boundary is a real word boundary
- flattening all cards reproduces the input word sequence exactly
- cards never overlap

Quality targets are best-effort; failure produces a warning, never a crash and
never a fabricated timestamp:

- min card duration (fix by merging where legal, else warn)
- max CPS
- Hebrew splitting rules (fix by choosing another split point where one is
  legal, else warn with `hebrew_rule_violation`)
- minimum gap between cards — **not handled here at all.** It is a display
  concern, implemented as an opt-in flag in `export`.

## Validation

`src/hebsub/contract.py` exposes `validate_transcript(obj) -> None`, raising
`ContractError` with a message naming the offending field and `wid`. Every
module validates its input and its output.
