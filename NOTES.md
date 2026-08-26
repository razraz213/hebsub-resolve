# NOTES.md

Out-of-scope bugs and observations parked here by Claude Code, plus manual
checklist items that no test can cover. Nothing here is a task until Raz says
it is.

## The panel duplicated the pipeline — fixed (D41, D42, D43)

Kept as a record because it cost more than it looked like it would.

`ui/app.py._run_pipeline` used to assemble the stages itself rather than
calling `host_resolve.run`. The copies drifted in both directions: the panel
proofread but wrote no intermediates (breaking CLAUDE.md rule 6), the CLI wrote
intermediates but never proofread. Neither was noticed until corpus 3 could not
be evaluated without re-transcribing its audio, and then again when the audio
cache landed in `host_resolve` where the panel could not reach it.

Fixed in D43: `host_resolve.run` is now the whole pipeline and the panel calls
it. `tests/test_ui.py::TestNoDuplicatePipeline` fails if `ui/app.py` ever
imports a pipeline stage again.

**The lesson worth keeping:** the duplication was invisible in both code
review and the test suite. What exposed it was needing an artifact that one
path produced and the other did not. Divergence between two copies of a
pipeline does not announce itself; it waits until you try to measure something.

## ITN — deferred to v2 (D2, D17)

Inverse text normalization — "עשרים אחוז" → "20%", spoken dates, currency —
is out of v1 in every form. The pass is not built, and as of D17 the
`itn_skipped` warning code is deleted too, so **v1 produces no record of the
number expressions it walked past.** That is deliberate. A log nobody was
funded to write is not a requirements document; it is a comment pretending to
be data.

What this costs, stated plainly so nobody rediscovers it in six months:
spoken numbers pass through as Hebrew words, and every one of them scores as
a WER error against a reference that writes digits. Some of v1's measured WER
is this, not model quality. Don't chase it.

When ITN is picked up for v2:

- Requirements come from the eval set **as it exists at that time** — count
  the number expressions in the references directly. Do not go looking for a
  v1 log; there isn't one.
- It needs a contract change first. Nearly all of ITN's value is in edits that
  change word count ("עשרים אחוז" is two words, "20%" is one), which the
  frozen contract forbids for every stage after transcribe. That is the real
  blocker, not the grammar work.
- The `itn` edit reason and an `itn_skipped`-style code both come back as part
  of that contract change, not before it.
- `NeMo-text-processing` was refused for v1 on dependency weight. Revisit it
  then, against a v2 contract that can actually use what it produces.

## Pending for the next code commit

- Pin `pysrt` in `requirements.txt`, exact version, no `>=` (D22). It rides
  along with the `contract.py` v2 update, not as its own change. `jiwer` and
  the rest of the allowed set need pinning there too whenever the module that
  imports each one is first built.

## Resolve placement — the real blocker (D28)

`host_resolve` cannot place subtitles onto a timeline that already has an
edit. Not a bug in this repo: Resolve's scripting API exposes no way to
position a subtitle clip, and no way to choose which video track a Fusion
title lands on. Every route was tested and is tabulated in D28.

What ships instead: the pipeline runs, writes `final.srt`, and imports it into
the media pool. One drag onto a subtitle track and it lands frame-exact,
because the SRT carries its own timings.

The one route not yet tried, if full automation is ever worth it: rebuild the
edit into a **new** timeline — append the subtitles to an empty timeline
first (verified: lands at frame 0, 0/39 cards drifting), then re-place every
source clip with the `{clipInfo}` `recordFrame` form, which does work for
video clips. Non-destructive, since the user's timeline is untouched. Fine for
a one-clip reel; a minefield for a real edit with transitions, speed changes,
compound clips and Fusion comps. Do not start this without deciding what
subset of edits it is allowed to refuse.

## Manual checklist — not coverable by a test

- [ ] Open `work/<job>/final.srt` in Notepad, VLC, and Resolve; confirm Hebrew
      renders and card 1 is not eaten (export.md's BOM warning).
- [ ] Drag the imported `final` clip from the media pool onto a subtitle
      track and confirm the cards line up with the audio by eye.
- [ ] Confirm `--placement textplus` still refuses on a populated V1. That
      guard is the only thing standing between this tool and a destroyed edit.

## Open, deliberately unresolved

- **`proofread` is not built.** D31 settles the approach (local, encoder-based,
  word-count preserving) but names no model, because nothing has been
  benchmarked. Per eval-protocol.md a pass that does not move its own metric
  gets deleted — so this needs `bench` before it needs code.
- **`bench` is not built.** Until it exists, every quality threshold in
  `segment` (including the D32 CPS change) rests on nine clips and judgement,
  not measurement.
- **`main` is not built.** The stages compose fine by hand and through
  `host_resolve`; the standalone orchestrator in docs/modules/main.md has no
  code yet.
- `Config.max_chars_per_card` is silently clamped to
  `max_lines * max_line_length`. At the D30 defaults (15 vs 80) it never
  binds; worth revisiting if a wide profile is ever added.

## Eval set — no longer blocked (D34)

Raz supplied hand-corrected subtitles for all nine reels as one SRT. Split
per clip into `tests/fixtures/references/` (`.srt` + `.txt`). D14's block on
`transcribe`, `proofread` and `bench` is lifted.

Baseline measured: **4.4% WER, 95.6% word accuracy**, error profile 63
substitutions / 1 deletion / 5 insertions.

The scoring, tuning and glossary scripts live in the session scratchpad, not
in the repo, because they belong in `bench` and `bench` has a spec they do not
follow yet. Port them when building it:

- `score_against_reference.py` — WER + the error catalogue
- `tune_boundaries.py` — boundary precision/recall/F1 vs Raz's cuts (D33)
- `build_glossary.py` — glossary with the safety filter (D34)
- `split_reference.py` — how the combined SRT was split; rerun if Raz
  supplies a corrected version

**Boundary F1 belongs in `bench` as the segment metric.** `eval-protocol.md`
currently names "Hebrew rule violations, CPS/line-length warning counts" for
the segment pass; those measure symptoms. Boundary agreement measures the
thing segment actually decides. Current: 59.3% F1 — so ~40% of cuts still
differ from Raz's, which is the module's real headroom.

**`--vocab` is measured harmful** (D34) and must stay off by default. If
anyone "fixes" it back on, they are undoing a measurement.

## Manual checklist — the panel

Not coverable by pytest, because all of it needs Resolve running.

- [ ] `python -m hebsub.ui.install`, restart Resolve, confirm the entry appears
      under **Workflow > Scripts**.
- [ ] With a timeline open, press TRANSCRIBE TIMELINE. Confirm `final` appears
      in the media pool and drags onto a subtitle track in sync.
- [ ] **Render queue safety.** Queue a render job in Resolve, run the panel,
      confirm the job is still there afterwards. `render_timeline_audio` used
      to call `DeleteAllRenderJobs()`, which would have destroyed a user's
      queued renders to extract an audio file. It now records the queue first
      and deletes only the job it created, and restores the previous render
      format. Verified once end-to-end (decoy job survived, queue 1 -> 1), but
      it is worth re-checking after any change to that function.
- [ ] Confirm the panel still connects after Resolve restarts.
