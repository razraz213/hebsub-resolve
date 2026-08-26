# Module: host_resolve

The one button. Transcribes the open DaVinci Resolve timeline and puts Hebrew
subtitles back into the project.

## Owned files

```
src/hebsub/host_resolve.py
tests/test_host_resolve.py
```

`src/hebsub/ui/*` is a separate concern (the panel); it calls this module.

## Responsibility

1. Connect to Resolve and find the current project and timeline.
2. Obtain the timeline's audio as a file.
3. Call the pipeline (`transcribe` → `segment` → `export`). No domain logic
   lives here.
4. Place the result back — `.srt` onto a subtitle track, or Text+ cards.

Everything here was verified against **Resolve Studio 21.0.3.7**. The findings
are D26–D29 and D42 in `docs/decisions-004.md`. Where this file disagrees with
something you remember about Resolve, the probe wins.

## Where the artifacts go

`work/<project>__<timeline>/`, both slugged. Keyed on the timeline alone, two
projects that each contain a "Timeline 1" share a folder and overwrite each
other's `01_raw.json`, `final.srt` and `review.json`.

The rendered audio itself was never at risk: the fingerprint covers clip paths,
positions and mtimes, so a different project misses the cache and re-renders
(D42). Everything downstream of the audio was.

`work_dir_name` is pure and tested without Resolve.

## Getting the audio

Resolve renders the timeline's **mix**, and the mix is what the subtitles have
to match. That is the whole reason this module does not read the source clips
directly — see D42, which measured the alternative and rejected it:

| audio path | time | WER |
|---|---|---|
| **Resolve render (what ships)** | 21.5 s | **4.51%** |
| rebuilt from source files | 14.5 s | 5.18% |

The source path is faster and worse, and it fails catastrophically (25 words
instead of 1496) when a music track is summed at unity — which cannot be
avoided, because **the Resolve API exposes no audio levels at all**.
`TimelineItem.GetProperty()` returns an empty dict for audio items: no clip
gain, no fades, no automation. Rebuilding the mix is not possible, only
approximating it.

### The cache is where the time is actually saved

A render costs ~21 s and cannot be made cheaper — `AudioBitDepth: 16` shrinks
the file by a third but changes the time by ~1 s, and Resolve rejects
`AudioSampleRate: 16000` outright. So instead of making the render faster,
`prepare_audio` skips it when it can:

`audio_cut_list()` reads the timeline's audio structure through getters only
(no render, well under a second) and `timeline_fingerprint()` hashes it. If the
fingerprint matches the one stored beside the existing WAV, that WAV is reused.

**The fingerprint's blind spot, stated plainly:** it covers what the API
exposes — clip positions, trims, source files and their mtimes, track enable
state. It cannot cover the Fairlight mixer, because the API does not expose it.
Change only a volume automation curve and the fingerprint will not notice.
`prepare_audio` therefore always logs which path it took, and `--fresh-audio`
forces a render.

## The review list (`--review`)

Off by default. When on, `run` transcribes a second time with
`DEFAULT_REVIEW_MODEL` and calls `proofread.review_disagreements`, writing
`review.json` beside the other artifacts and printing a capped summary.

**The partner model is deliberately not the best model.**
`whisper-large-v3-ct2` scores 5.50% against turbo's 4.73% — it is worse. It is
there to be *independently* wrong: a sibling trained on the same data makes the
same mistakes and stays silent exactly where both are wrong (D48). A test in
`tests/test_ui.py` asserts the panel uses this specific pairing, so nobody
"upgrades" it to a better model and silently destroys the signal.

Cost is a second transcription pass, which roughly doubles the transcribe
stage. That is why it is opt-in: it buys a list to read, not a better `.srt`.

## Reaching the flagged words

The review list is only useful if you can act on it. Two aids, both built from
the same flags, both skipped entirely when nothing is flagged (D67).

**Timeline markers.** One Fuchsia marker per flagged word: name is the word
heard, note is what the second model heard instead. Up/down arrow steps between
them; the Markers panel lists them all.

Two facts about the marker API, both probed live and both silent when wrong:

- `frameId` is measured **from the timeline start**, not from 01:00:00:00. A
  marker at 1009.63 s on a 30 fps timeline reads back as frame 30289 while
  `GetStartFrame()` is 108000.
- Never `DeleteMarkersByColor`. Raz's timeline carries a Cyan marker of his
  own; colour is not identity. Ours carry `customData = REVIEW_MARKER_TAG` and
  `clear_review_markers` removes only those.

A frame can hold one marker. A clash — two flagged words in the same frame, or
one of the user's own markers — nudges up to 4 frames rather than dropping the
flag.

**Seeking (`seek`, `timecode_at`).** The panel's review list clicks through to
here. Note the trap, because both conventions live in this file: marker
`frameId` is **relative to the timeline start**, while `SetCurrentTimecode`
wants the **absolute** timecode. Second zero is `01:00:00:00` and frame 108000,
not `00:00:00:00` and frame 0.

**A review subtitle track — `--review-track`, off by default.** `review_cards`
turns each flag into a card spanning the real card the word sits in, reading
`heard` above `alternative`. Rendered with `export.render_plain_srt` and
imported as `HebSub Review`, versioned off its own base. Drop it on a *second*
subtitle track and each flagged word sits beside the real subtitle, over the
picture; delete the track when done.

It carries the same leading timing card as the real track, for the same reason
(D55) — otherwise it lands adrift of the thing it annotates.

It is off because the markers and the panel list already cover this ground, and
on it adds a clip to the media pool on every job (D68).

**Neither clears itself.** Nothing knows whether a word has been fixed, and
inferring it would mean guessing. `--clear-markers` (and the panel's CLEAR
FLAGS button) removes them when Raz decides he is done.

Both are wrapped in `try`: a navigation aid must never fail a job whose
subtitles are already correct.

## Learning from the corrected track

`GetName()` on a subtitle TimelineItem **is its text** — probed live on a
611-card track. That is what makes harvesting possible with no export step:
`harvest_corrections` reads the corrected track straight off the timeline.

**Only the difference is learned.** Every Hebrew word on the track that is not
in the `.srt` we generated is, by construction, something the user typed.
Harvesting the whole track would feed the ASR's own mistakes back in —
`תתעכם` would become a word the checker recognises, and catching exactly that
is what the lexicon is for.

It is one set difference: no card alignment, no timecode matching. Both would
break the moment two cards were merged or the clip nudged, and neither is
needed to answer "which words are new".

The track is chosen by **word overlap** with our output, not by index, so the
review track cannot be mistaken for the corrected one. Below 50% overlap this
is a different programme and it reports `no_match` and writes nothing.

Two things the probe also showed, neither of which anything may be built on:
a two-line card comes back with its line break already gone (so text is
whitespace-normalised here), and Resolve strips `<` from the name — the timing
card reads `">> TIMING CLIP - DELETE ME "`. Harmless, because `learn_words`
takes maximal runs of Hebrew letters and ignores every symbol.

**Caveat, and it is a real one:** the diff is against the `.srt` of the LAST
run. Harvest after correcting, before re-running. After a re-run the track
belongs to the previous output and the differences are run-to-run variation
rather than corrections.

## Where the lexicon and the artifacts live

`lexicon_path()` resolves from the package, not the working directory.
`proofread.hebrew_lexicon()` falls back to `Path.cwd() / lexicon.txt`, which is
right for the CLI and wrong for the panel: launched from Resolve's
**Workflow > Scripts** menu the working directory is Resolve's, so every
learned word was silently invisible. `run` now hands the tiebreaker its lexicon
explicitly.

`work_root()` is deliberately still `Path.cwd() / "work"` — pinning it would
relocate artifacts that already exist. It is a function so the panel and `run`
cannot disagree about where the last run's `.srt` went.

## Placement

Two modes, and the constraints on each are not obvious:

- **`srt`** — `ImportMedia()` then a plain `AppendToTimeline([clip])`. The
  `{clipInfo}` dict form returns None, leaves the Timeline handle dead, and
  crashed Resolve outright during the probe. Never use it here. Appending only
  lands correctly on an **empty** timeline (D28); on a timeline that holds
  anything, the clip is imported to the media pool for the user to drag, and
  the reason is printed.
- **`textplus`** — inserting a title at the playhead performs an *overwrite*
  edit, which is what gives exact durations despite `TimelineItem` having no
  `SetDuration`. Text+ **must** be given an explicit Hebrew-capable font; on
  the default font Hebrew renders as empty boxes, which looks like broken bidi
  and is not. Refuses outright when V1 holds anything, because the insert would
  overwrite the user's footage.

## Where the clip lands, and what it is called

`place_srt` imports into the **master bin**, not the currently selected one —
a subtitle clip that lands three folders deep in someone else's project
structure is a clip they will not find. The user's bin selection is part of
their workspace and is restored afterwards.

The pool name is **`HebSub Subtitles`**, then `HebSub Subtitles V2`, `V3`, …
The base name counts as V1, so **the highest number is always the newest**.
Without this, re-running before deleting the previous clip leaves two
identically named subtitle clips and no way to tell them apart — Resolve shows
no import time in the media pool list. `next_clip_name` is pure and tested
without Resolve; `place_srt` feeds it the master bin's current names.

The pool name is not `final`. Resolve refuses to rename
a subtitle clip — `SetClipProperty("Clip Name", ...)` returns `False` for every
key — so the name has to come from the filename, and `place_srt` imports a copy
carrying it. `final.srt` stays exactly where CLAUDE.md rule 6 requires;
`HebSub Subtitles.srt` sits beside it as the human-named deliverable.

## Safety rules

These are not style preferences. Each one corresponds to a way this module
could destroy a user's work:

1. **Never call `DeleteAllRenderJobs()`.** Record the queue before adding a
   job, delete only ids that were not there before. A user's render queue on a
   real client project is hours of work.
2. **Restore the render format** afterwards. It is part of their project setup.
3. **Never insert into a track that holds anything** without an explicit check.
4. **Never invent a timestamp.** This module does no timing work at all; it
   passes files between stages.

## Acceptance criteria

- `audio_cut_list` converts timeline frames at the *timeline* rate and source
  frames at the *source* rate. These differ, and mixing them up silently
  desyncs every clip from mixed-rate media.
- `timeline_fingerprint` is stable across repeated calls on unchanged input,
  changes when any clip moves, is trimmed, or swaps source file, and ignores
  cosmetic things like clip names.
- An item with no media pool item (a transition) is reported as unresolved
  rather than raising.
- The render helper leaves the render queue exactly as it found it.
