# CLAUDE.md — global rules for this repo

Read this file at the start of every task. It overrides your defaults.

## What this project is

`hebsub` — a standalone Hebrew subtitle pipeline. Audio or video file goes in,
a clean `.srt` comes out, usable in any editor (Resolve, Premiere, YouTube).

It is **not** a transcription model. The ASR engine is a swappable commodity.
The product is everything that happens after the ASR: Hebrew-specific
correction, subtitle segmentation, and export that doesn't break RTL.

## The modules

Eight, each with a spec in `docs/modules/`:

| Module | Spec | Role |
|---|---|---|
| `transcribe` | `docs/modules/transcribe.md` | media → Transcript |
| `proofread` | `docs/modules/proofread.md` | Transcript → corrected Transcript |
| `segment` | `docs/modules/segment.md` | Transcript → SubtitleFile |
| `export` | `docs/modules/export.md` | SubtitleFile → `.srt` / `.vtt` |
| `main` | `docs/modules/main.md` | orchestrator, zero domain logic |
| `bench` | `docs/modules/bench.md` | eval runner, writes `bench.csv` |
| `host_resolve` | `docs/modules/host_resolve.md` | Resolve adapter; **the whole pipeline lives here** |
| `ui` | `docs/modules/ui.md` | the panel; owns no pipeline logic |

## Scope lock — the most important rule

Every task names exactly one module. Each module spec opens with an
**`Owned files`** block. You may create or edit exactly the files in that
block, and nothing else.

- Do **not** touch files owned by another module, even to "quickly fix"
  something you noticed.
- Do **not** change `docs/contracts.md` or `docs/decisions-001.md`. If a task
  seems to require a schema change, stop and say so. Schema changes are a
  separate, explicit task.
- If you find a bug outside your module, write it in `NOTES.md` and continue.
- Adding a file inside a glob the block already owns (a new ASR engine adapter
  under `src/hebsub/engines/`, say) is in scope. Adding one anywhere else is
  not.

## Architecture rules

1. **One data contract.** Every stage reads a Transcript JSON and writes a
   Transcript JSON. The schema is in `docs/contracts.md` (v2) and is frozen.
2. **Timestamps are immutable** after the transcribe stage. No stage after
   `transcribe` may invent, shift, or delete a timestamp. `segment` may only
   *group* existing word timings; it never edits them. `transcribe` itself may
   clamp overlapping engine output, logging a `timing_clamped` warning.
3. **`wid` is forever.** Every word carries a stable global index assigned at
   transcribe. No stage renumbers or reuses one. Edits and warnings key on
   `wid`, which is what makes the audit trail survive segmentation.
4. **Every module is a pure function plus a CLI.** The function takes a dict
   and returns a dict. The CLI reads a file and writes a file. No module reads
   config from anywhere except its own arguments.
5. **`main` is a dumb orchestrator.** It parses args, calls stages in order,
   writes intermediate files, handles errors, prints the warning summary. It
   contains zero domain logic. If you want to add logic to `main`, it belongs
   in a module instead. See `docs/modules/main.md`.
6. **Every intermediate artifact hits disk** under `work/<jobname>/`:
   `01_raw.json`, `02_proofread.json`, `03_segmented.json`, `final.srt`.
   Never pipe stages in memory only. Debuggability beats elegance here.
7. **Fail loud on failure; warn loudly on a missed target.** No silent
   `except: pass`. No returning partial results with a print statement. If a
   stage can't do its job, raise with a message naming the module.
   A *quality target* that can't be met is different: it appends a structured
   entry to `meta.warnings` and the run continues. Warnings are never dropped,
   never swallowed, and always surface in the CLI summary. The list of hard
   rules and quality targets is in `docs/contracts.md`.
8. **Re-running a stage raises `StageAlreadyRun`** unless `--force` is passed.
   A stage detects this from `meta.stages`.

## Workflow for every task

1. Read `CLAUDE.md`, `docs/contracts.md`, and `docs/modules/<module>.md`.
2. Confirm every file you intend to touch is in that spec's `Owned files`
   block. If it isn't, stop and say so.
3. Write or update the test first, from the spec's acceptance criteria.
4. Implement the smallest thing that passes.
5. Run `pytest tests/test_<module>.py` until green.
6. Run `pytest` (full suite) to prove you broke nothing.
7. Report: what you changed, what the tests say, what you deliberately left out.

Do not report success without showing the actual test output.

## Fixtures — and what is currently blocked

**In this public repository the eval corpora are absent.** They are a working
editor's real client reels and are not redistributable, so `bench.csv`, the
hand-corrected references and the harvested `lexicon.txt` are not here. One
corpus-dependent test skips; the other 630 run on synthetic fixtures.

Real fixtures are the maintainer's job. Do not invent audio and do not synthesize a
"golden" reference transcript: a fabricated eval set produces confident,
meaningless numbers.

`tests/fixtures/` needs three real clips cut from your own material — 30s clean,
30s with English mixing, 30s noisy — plus their hand-corrected references.
Until those exist:

- **Blocked:** `transcribe`, `proofread`, `bench`.
- **Unblocked:** `contract`, `segment`, `export`, `main` — all can be tested
  on synthetic JSON built in the test file.

## Environment

- Windows. Python: `C:\Users\PC\AppData\Local\Python\pythoncore-3.14-64\python.exe`
- `ffmpeg` must be on PATH; the pipeline shells out to it for audio extraction.
- All file I/O is UTF-8 explicit: `encoding="utf-8"`. Never rely on the
  Windows default codepage — it will corrupt Hebrew.
- Print Hebrew to console with `sys.stdout.reconfigure(encoding="utf-8")`.

## Dependencies

Do not add a dependency without asking first. Current allowed set:

```
faster-whisper, requests, jiwer, pytest, python-dotenv, pysrt
```

`pysrt` is **test-only** — it exists so `export` is validated by a parser it
did not write. Nothing in `src/` imports it.

Refused for v1, do not reach for them: `NeMo-text-processing` (ITN is deferred
out of v1), `transformers`/`torch` (punctuation is a mode of the LLM pass, not
a local model), any vendor-specific LLM SDK (the LLM pass uses a swappable
adapter over `requests`).

Pin exact versions in `requirements.txt`. No `>=`.

## Secrets

API keys come from `.env` (gitignored) via `python-dotenv`. Never hardcode a
key, never print a key, never write a key into a JSON artifact or a log line.

## Definition of done for any task

- Tests green, full suite green.
- The module's CLI runs standalone on a real file from `tests/fixtures/`.
- Committed to git with a message naming the module and what changed.

## Task prompt template (Raz uses this)

> Read CLAUDE.md, docs/contracts.md, and docs/modules/<MODULE>.md.
> Task: <one sentence>.
> Edit only the files in that spec's `Owned files` block.
> Write the test first. Run pytest until green, then run the full suite.
> Do not modify any other file. Report the test output.
