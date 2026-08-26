# Module: ui

The panel. A window with one button that runs the pipeline on the open Resolve
timeline and shows what happened.

## Owned files

```
src/hebsub/ui/*
tests/test_ui.py
tests/test_ui_hebrew.py
```

## The rule this module exists to obey

**The panel owns no pipeline logic.** It collects three settings, calls
`host_resolve.run`, and renders the result. That is the entire contract.

This is written down because the panel broke it once. `_run_pipeline` used to
assemble the stages itself — a second copy of `host_resolve.run` — and the two
copies drifted:

- the panel proofread but wrote **no intermediates**, breaking CLAUDE.md rule 6
- the CLI wrote intermediates but **never proofread**

Neither difference was noticed until corpus 3 could not be evaluated without
re-transcribing its audio from scratch (D41), and then again when the audio
cache landed in `host_resolve` and the panel could not see it (D42). Both cost
real measurement time to discover. `test_the_panel_owns_no_pipeline` fails if
`ui/app.py` imports a pipeline stage.

## Responsibility

1. Collect settings: card width, whether the `llm` proofread pass runs, whether
   punctuation is stripped.
2. Call `host_resolve.run` on a worker thread.
3. Stream its stdout into the log, and poll the queue from the tk main loop.
4. Show the outcome and offer to open the work folder.

Everything else — connecting to Resolve, rendering or reusing audio,
transcribing, proofreading, segmenting, exporting, placing — belongs to
`host_resolve` and the modules it calls.

## Settings the panel owns

Four, and no more: card width, the `llm` proofread pass, punctuation
stripping, and **Second opinion** — which switches on the review list
(`host_resolve --review`). Every one of them is a choice about what the user
wants; none of them is pipeline logic.

Second opinion is off by default. It doubles transcription time and what it
returns is a list to read rather than a better `.srt`, so it is a decision per
job rather than a default.

## Hebrew, and what the bidi probe found

The panel is Hebrew. Nothing below it is: `host_resolve` and the CLI still
speak English, and their log lines are translated on the way in by
`ui/hebrew.py`. Translating upstream would change the CLI, the module specs and
the tests that assert on those lines — presentation belongs here, which is the
rule this module already exists to obey.

**Four facts, measured on Tk 8.6.15 / Windows 11, not assumed.** Every one of
them is silent when you get it wrong: the text still draws, just backwards.

1. Tk renders each Hebrew run correctly. Given `ABC` + aleph-bet-gimel it draws
   `A B C` then `GIMEL BET ALEPH` — the run is reversed, which is right.
2. The base direction is nevertheless always **LTR**. Plain
   `aleph-bet-gimel ABC` puts the Hebrew on the left and `ABC` on the right,
   which is backwards for a Hebrew line.
3. **RLM (U+200F) does not fix it.** Nor does RLI/PDI (U+2066/U+2069) — Tk has
   no glyph for those and draws visible boxes.
4. **RLE … PDF (U+202B … U+202C) does.** So every displayed string goes through
   `hebrew.rtl()`.

Two things `rtl()` still cannot settle, both handled by splitting the string
across separate widgets rather than by another control character:

- **A number beside Hebrew.** `"01 · חיבור"` renders with the `01` on the left,
  reading as though the number came last. `Panel._panel` takes the number and
  the word as two arguments and packs them `side="right"`. Same for the card
  width readout — `"14 תווים"` had the same fault.
- **A Latin value beside a Hebrew label.** Project names are Latin. Label and
  value are separate widgets, so there are two single-direction strings and no
  bidi to fight.

`Text.bbox()` reports LOGICAL positions — index 0 of a Hebrew line reads back
as the leftmost x — which affects caret and selection, not drawing. The log is
read-only. Do not "fix" it.

**Layout.** Centred where it can be, mirrored where direction carries meaning:
review rows pack from the right (tick, time, word, `←`, alternative) with COPY
at the far left; footer buttons on the right; scrollbars on the left, because
in an RTL layout the right edge is where the text begins.

**Type.** The design system already names the swap: Rubik for Hebrew display,
IBM Plex Sans Hebrew for Hebrew body, no uppercasing and no mono letter-spacing
— Hebrew has neither. IBM Plex Mono carries no Hebrew at all, so Hebrew
"engraved" labels use the Hebrew face at a small size rather than rendering as
boxes. Hebrew roles are sized a point above their Latin equivalents: with no
ascenders or descenders to read shape from, the same nominal size reads smaller.

## The review list

Panel `04 · Worth checking`. Built once, hidden until a job produces flags — a
panel that is empty for every run without the second opinion on would be dead
furniture.

One row per flagged word: `✓ | m:ss | heard → alternative | COPY`.

- **Click anywhere on a row** → `host_resolve.seek` moves Resolve's playhead
  there. The panel does not build the timecode itself;
  `test_goto_delegates_the_timecode_maths` fails if `SetCurrentTimecode`
  appears in this module. Only `host_resolve` knows a Resolve timeline starts
  at 01:00:00:00 (D68).
- **COPY** puts the alternative on the clipboard. It is Hebrew; retyping Hebrew
  to fix a Hebrew word is absurd.
- **✓** crosses the row off. **Local to the window and not persisted** — the
  same reason the markers do not clear themselves (D67): nothing knows whether
  the word was fixed.

Each cell is its own label rather than one formatted string. Hebrew, an arrow
and a timecode in a single Tk label invites bidi reordering, and which word is
which is the entire content.

Row order is mirrored: the tick sits at the right where a Hebrew reader starts,
COPY at the far left where the row ends, and the arrow points `←`.

`Panel.clock` must agree with `host_resolve._clock`; a test asserts it. Two
places showing the same word at different times is how a list stops being
trustworthy.

**Pack order is load-bearing.** Tk hands out vertical space in the order
`pack()` was called, so an expanding widget packed early squeezes everything
after it clean off the window — which is what the footer did the moment the
list appeared. The footer claims `side="bottom"` first, the list packs against
the bottom above it, and `_pack_log` re-packs the log *last* so it expands into
what is left. Then `_fit_window` grows the window by the list's height rather
than crushing the log to two lines. It only ever grows: shrinking back would
fight a window the user had deliberately resized.

## LEARN

`למד מהתיקונים`. Reads the corrected subtitle track off the timeline and adds
the words that differ from the last run's `.srt` to `lexicon.txt` — no export,
no CLI, no file paths. Delegates entirely to
`host_resolve.harvest_corrections`.

Press it **after** correcting and **before** re-running: the comparison is
against the last run's output, so a re-run makes the differences meaningless.

The words it adds are printed in full, on purpose. `lexicon.txt` accretes, so a
wrong entry is permanent until somebody deletes the line — seeing them is the
only chance to notice.

## CLEAR FLAGS

The one button in the panel that is not the run button. It calls
`host_resolve.clear_review_markers` on the current timeline and reports the
count. It exists because nothing tracks whether a flagged word has been fixed
(D67), so the markers have to be dismissed deliberately rather than inferred
away.

## Threading

tkinter is not thread-safe. The pipeline runs on a `threading.Thread`; it
communicates only by putting `("log" | "done" | "fail", payload)` on a
`queue.Queue`. The main loop drains that queue on an `after()` tick. Nothing
off the main thread touches a widget.

`_Tee` stands in for stdout during the run. It buffers until it has a whole
line, because the pipeline prints partial lines and a half-line in the log
looks like a crash.

## Presentation belongs here, not upstream

The pipeline prefixes its progress with the module name (`host_resolve: 1504
words`) because it is written for a terminal. `_Tee` strips that. Do **not**
"fix" this by making the pipeline quieter — the CLI needs those prefixes, and
moving presentation upstream is how the two copies diverged the first time.

The same applies to the warning summary: `host_resolve` prints it, the panel
displays it, and the panel does not print its own.

## Design

`docs/design.md` is not in this repo — the panel follows Raz's Analog
Instrument system (`C:\Users\PC\.claude\design\design.md`). The palette and
type stack live in `ui/theme.py`; nothing else should hardcode a colour.

## Acceptance criteria

- `ui/app.py` imports no pipeline stage.
- Settings reach `host_resolve.run` unmangled — in particular an empty passes
  string becomes `()`, not `("",)`, which `proofread` would reject.
- `_Tee` emits whole lines only, strips the module prefix, and flushes any
  trailing partial line exactly once.
- The panel never blocks the tk main loop on the pipeline.

## Not covered by tests

The window itself. `NOTES.md` holds the manual checklist — it needs Resolve
running and a human looking at it.

The review list's widget tree is in that category: it needs a real Tk window.
What was verified by hand, and is worth re-running after any change to
`_review_row`:

- three flags produce three rows, each reading `✓ · m:ss · heard · → ·
  alternative · COPY`, Hebrew intact and in the right cells
- ticking and unticking moves the row in and out of `_review_done`
- COPY lands the word on the system clipboard
- an empty list unpacks the panel rather than leaving an empty box
- a synthetic click on a row moves Resolve's real playhead (measured:
  row 2 at 127.0 s → `01:02:07:00`)
