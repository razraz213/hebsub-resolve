# Module: export

SubtitleFile in → `.srt` file out. Formatting and display only. Zero
decisions about content, timing, or line breaks — those were made upstream.

## Never before the picture (`picture_spans`)

A displayed start is pulled forward when it lands outside the ranges the host
says a card may begin in. `host_resolve` intersects two things — where there is
a picture, and where the VAD says there is actually speech. Every other caller
passes nothing and nothing moves.

Gaps are closed **after** starts are computed, onto the displayed start. Doing
it the other way round re-opens every gap by however far the clamp moved the
next card.

**Why it is needed.** Whisper's word onsets run early. Measured against Raz's
corrected files across all three corpora, the median card starts **34 ms**
(about one frame at 30 fps) before his. At a speech onset after silence it is
far worse, because the model takes the breath before the word: **median 173 ms,
up to 460 ms**. On a timeline of separate videos that puts the first card of
each video in the black gap before it.

Rules:

- A card already over a picture is untouched.
- A card that **ends** before the next picture begins is left alone — it
  belongs to nothing, and moving its start past its own end would invert it.
- A card after the last picture is left alone.
- Ends never move; the SubtitleFile is untouched. Same standing as
  `_displayed_ends`.
- The timing clip ends at the **displayed** first start, so the placeholder and
  the first card cannot disagree.

Measured on Raz's workshop timeline: **11 cards started before their video; now
0.**

*Not fixed here:* the residual ~34 ms global bias on every other card. That is
Whisper's, it is uniform, and correcting it is a separate decision.

## Closing dead space (`--close-gaps`)

Extends a card's displayed end to meet the next card's start when the gap
between them is at most `MS`. The opposite of `--gap`, and asking for both is
refused rather than silently resolved.

**The threshold is measured, not chosen.** Across Raz's three hand-corrected
corpora — 1,882 gaps between cards:

| gap | share of his gaps |
|---|---|
| **touching (≤0ms)** | **96.4%** |
| 1–60ms | **0.0%** — not one |
| 61–200ms | 0.4% |
| >200ms | 3.2% |

The distribution has a hole in it. He never leaves a gap under 60ms, and
almost never under 200ms; above that is a real population of speech pauses.
The segmenter meanwhile put **32% of its gaps in the 1–200ms band** — exactly
the band he never produces — because those gaps are word-timing artifacts, not
silence anyone can hear.

`DEFAULT_CLOSE_GAPS_MS = 200` closes them and costs at most 9 of his 1,882
gaps. 300ms would close only 33 more while tripling that cost.

Result on the same three corpora: **63.8% → 95.9% of cards touching**, against
his 96.4%.

Applies to `.vtt` as well as `.srt`: dead space between cards is wrong in any
player, unlike the timing clip which is a Resolve-only concern. On by default
in `host_resolve`, off in this module's CLI.

## The timing clip (`--timing-clip`)

A placeholder card from `00:00:00,000` to the first real card, labelled
`>> TIMING CLIP - DELETE ME <<`.

It exists because of D28: Resolve exposes no scripted way to position a
subtitle clip, so the `.srt` is dragged onto a track by hand. What you align
is the clip's *content* — and when the first spoken word is minutes into the
programme there is nothing at the front to align against. On Raz's workshop
timeline the first word is at **2m44s**. A card starting at zero makes the
clip begin where the programme begins, so snapping it to the timeline start is
exact rather than eyeballed. The card is then deleted.

Rules:

- **`.srt` only.** A `.vtt` goes to a web player where nothing is dragged; a
  "delete me" card there would simply be a bug.
- **Skipped when the first card already starts at 0** — no room, and a
  zero-length card is not a card.
- **ASCII text, deliberately.** It sits in an RTL subtitle track: Hebrew could
  be mistaken for content, and mixed scripts invite bidi reordering.
- Real card timings and the report's card count are untouched; the report
  records whether a clip was actually written, not merely requested.

Off in this module's CLI, **on by default in `host_resolve`** — that is the
path where the file gets dragged, and the only path where the card helps.

## Owned files

```
src/hebsub/export.py
tests/test_export.py
```

## Format

Standard SubRip:

```
1
00:00:01,240 --> 00:00:04,880
אז בוא נדבר על מה שקרה
בקמפיין האחרון.

```

- Timestamps `HH:MM:SS,mmm`, comma decimal separator, zero-padded.
- Blank line after every card, including the last one.
- Line endings: `\n`. Not `\r\n`. Every modern NLE handles it, and CRLF
  causes duplicate-blank-line bugs in several parsers.

## Encoding — get this wrong and everything looks broken

- **UTF-8 without BOM.** A BOM makes some players render `1` as the first
  subtitle's text or fail to parse card 1 entirely.
- Write with explicit `encoding="utf-8"`, `newline="\n"`.
- Add a `--bom` flag for the rare legacy player that needs it. Default off.

## Frame gap — `--gap MS`

`segment` deliberately leaves adjacent cards touching, because a card boundary
is a real word boundary and moving one upstream would break timestamp
immutability (D3). Inserting a visual gap is legal **here**, because export
produces display output, not a Transcript.

```
--gap MS    default 0 (off)
```

When set, shave up to `MS` milliseconds off each card's displayed `end`, only
where the next card actually starts at or before that point.

- Never shave below what leaves a positive duration. Shave
  `min(MS, available_slack)` and no more.
- Never shift a `start`. Only the displayed `end` moves, and only earlier.
- The SubtitleFile on disk is not modified. This is a rendering-time
  adjustment that exists solely in the emitted file.
- A card with no following card is never shaved.

`--gap 83` is the classic 2-frames-at-24fps value.

## RTL handling

Default: **write logical order, insert no bidi control characters.**
Conforming renderers apply the Unicode bidi algorithm correctly and this is
the most portable output. Resist the urge to "help."

Two opt-in flags for players that misbehave:

- `--rlm` — insert U+200F RIGHT-TO-LEFT MARK at the start of each line and
  before trailing punctuation. Fixes the classic bug where a line ending in
  `.` or `?` renders the punctuation on the wrong side.
- `--isolate` — wrap Latin-script runs in U+2066 / U+2069 (LRI / PDI) so an
  embedded English brand name can't drag surrounding Hebrew punctuation
  around with it.

Both default off. Both must be covered by a test asserting the exact byte
sequence produced.

## Display punctuation -- `--strip-punct`

Drops trailing `.,;:!?…` from each displayed word. Off by default.

Short-form Hebrew subtitles are not punctuated -- Raz's reference carries 12
marks across 1445 words (D35) -- but `segment` needs the punctuation to find
sentence and clause boundaries, so it survives the pipeline and is dropped
here. The SubtitleFile on disk is untouched and `stats` are unaffected.

Gershayim and geresh are never stripped: they belong to the word, and
`חב"ד` must not become `חבד`.

## `render_plain_srt` — the review track's side door

`(start, end, text)` triples in, `.srt` out. The review track is not a
SubtitleFile: it has no words, no `wid`s and no cps, and inventing them to
satisfy the contract would push a fake object through the pipeline for the sake
of reusing one function. This writes the same timestamp format and nothing
else.

It does get the leading timing card, on by default here. Resolve drops lead-in
silence on import, so a review track whose first card is 40 s in would land 40 s
away from the subtitles it annotates (D67).

## Other outputs

`--format vtt` produces WebVTT (dot decimal separator, `WEBVTT` header).
Same content, same rules, `--gap` applies identically. Adding a format must
not require touching any other module.

## Re-running

This module emits no Transcript, so it does not append `export` to
`meta.stages` and `StageAlreadyRun` does not apply (D16). Re-running export
overwrites its own output file and its own report. That is safe and intended.

## The report sidecar

Export **always** writes `<output>.report.json` next to the SRT — every run,
including a clean one with zero warnings. `final.srt` gets
`final.srt.report.json`.

```json
{
  "source": "03_segmented.json",
  "warnings": [ { "stage": "export", "code": "gap_not_applied",
                  "wid_start": 88, "wid_end": 94,
                  "detail": "requested 83ms, applied 0ms" } ],
  "stats": { "cards": 214, "cards_over_cps": 3, "cards_over_line_len": 0,
             "cards_under_min_duration": 5, "max_cps": 21.4, "mean_cps": 12.8 }
}
```

- `source` — the input path export was given.
- `warnings` — same object shape as `meta.warnings` in `docs/contracts.md`,
  same required keys, same `wid_start`/`wid_end` pairing rule. This is where
  a partially-honoured `--gap` reports, as `gap_not_applied`.
- `stats` — counted off the SubtitleFile as export writes it.

**This report is the authoritative source of subtitle-quality numbers.**
`bench` reads it (D16). Nothing in the repo re-parses an emitted SRT to
recompute these — the writer knows what it wrote, and a reader that
disagrees with the writer is a bug hunt nobody needs.

Note the division of labour: the counts in `stats` overlap with warnings that
`segment` already appended to `meta.warnings`, and that is deliberate.
`segment` records *why* it could not do better; export records *what the
delivered file actually looks like*. Bench wants the second.

Warnings in the report never change the exit code. A file with 3 cards over
CPS exported successfully.

## Acceptance criteria

- Output parses cleanly with `pysrt` in the test. `pysrt` is an approved
  **test-only** dependency (D5) and exists precisely so the writer is not
  validated by its own reader. Nothing in `src/` imports it.
- Byte-level fixture comparison against `tests/fixtures/expected.srt`.
- Round-trip: parse the exported SRT back and confirm every timestamp matches
  the SubtitleFile to the millisecond — with `--gap 0`, which is the default.
- With `--gap 83`, every emitted gap is ≥ 83 ms **or** equal to the full
  available slack, and no card's duration goes non-positive.
- `--gap` never alters a `start`, and never alters the input object.
- A `--gap` request that cannot be honoured for a card produces exactly one
  `gap_not_applied` warning in the report, carrying that card's `wid` range.
- `--bom`, `--rlm`, `--isolate` each covered by an exact-bytes test.
- `<output>.report.json` is written on every run, including one with zero
  warnings, where `warnings` is `[]` and `stats` is still fully populated.
- Every warning in the report satisfies the contract's warning object shape:
  `stage`, `code`, `detail` present and non-empty, `wid_start`/`wid_end`
  either both present with `wid_start <= wid_end` or both absent.
- `stats` is verified against a hand-counted synthetic SubtitleFile —
  `cards`, `cards_over_cps`, `cards_over_line_len`,
  `cards_under_min_duration`, `max_cps`, `mean_cps`.
- The report is written with `encoding="utf-8"` and is valid JSON.
- File opens correctly in Notepad, VLC, and DaVinci Resolve. This one is a
  manual checklist item in `NOTES.md`, not an automated test.

Per D14 this module is **not** blocked on real fixtures: its inputs are
synthetic SubtitleFile JSON built in the test.

## Explicitly out of scope

Burning in subtitles, styling, positioning, ASS/SSA. Not v1.
