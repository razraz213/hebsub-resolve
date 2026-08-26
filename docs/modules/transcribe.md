# Module: transcribe

Audio/video file in → Transcript JSON out.

## Owned files

```
src/hebsub/transcribe.py
src/hebsub/engines/*
tests/test_transcribe.py
```

Adding a new ASR engine is a transcribe task: the adapter is a new file under
`src/hebsub/engines/` and requires no change anywhere else in the repo.

## Responsibility

1. Extract mono 16 kHz WAV from any input file using ffmpeg.
2. Call a swappable ASR **engine adapter**.
3. Repair tokens the engine split inside a word (see below).
4. Normalize the engine's response into the Transcript contract, assigning
   every word its stable `wid`.

Nothing else. No correction, no punctuation fixing, no segmentation.
Whatever the engine returns is what this module reports, warts included.

### Token repair — deciding what counts as one word

This is the one place a word may be split or joined, and it is here for a
structural reason: **transcribe is the stage that decides what a word is.**
Every later stage is forbidden from changing the word count, because `wid` is
assigned at the end of this step and is identity from then on. A join that
happened later would renumber everything downstream of it.

Repairs are 1:1 merges of adjacent tokens, never rewrites:

| repair | engine emits | should be | why it matters |
|---|---|---|---|
| thousands separator (D39) | `75` + `,000` | `75,000` | every price, date and statistic |
| geresh (D41) | `ג` + `'יפ` | `ג'יפ` | every loanword: ג'וב, צ'ק, ז'אנר |

Both leave a card boundary free to fall *inside* a word if left alone. The
geresh repair was measured at a 17% relative WER reduction on its own — 5.45%
to 4.51% on the same token stream.

The geresh is not punctuation. It marks a sound Hebrew has no letter for: `ג'`
is *j*, `צ'`/`ץ'` is *ch*, `ז'` is *zh*. Joining requires the preceding token
to end in one of those four letters, which is what keeps a genuine quotation
(`אמר 'שלום`) from being glued on. See D41 for the guard's known limit.

## Windows: the model cache cannot use symlinks

`huggingface_hub` populates its cache by symlinking `snapshots/` entries at
`blobs/`. **Creating a symlink on Windows requires Developer Mode or an
elevated process**, neither of which is a reasonable thing to ask of a video
editor, and without one the download dies partway through with

```
[WinError 1314] A required privilege is not held by the client
```

The failure is worse than it looks: it leaves a snapshot directory holding
`model.bin` but missing small files like `preprocessor_config.json`, so the
model *looks* downloaded and every later run fails the same way.

`disable_hf_symlinks()` sets `HF_HUB_DISABLE_SYMLINKS` so the cache is
populated by copying. **It must run before `huggingface_hub` is imported** —
the flag is read into module constants at import time — which is why it sits
beside `bootstrap_cuda_dlls()` in the engine and why `faster_whisper` is
imported inside `_load` rather than at module scope.

An already half-written cache is not repaired by the flag alone; the engine's
error message says which folder to delete.

## `wid` assignment

`transcribe` is the only stage that creates `wid`s. They start at 0 and
increase by 1 across the whole file, in word order, across segment boundaries.
No gaps, no reuse. Every later stage treats them as read-only identity.

## CLI

```
python -m hebsub.transcribe --in audio.mp4 --out work/job/01_raw.json \
       --engine ivrit_local [--model <name>] [--vocab glossary.txt] [--force]
```

`--vocab` takes the glossary file defined in `docs/modules/proofread.md`.
Transcribe passes **only the right-hand sides** to the engine: for a mapping
line `נטפליקט => נטפליקס` it sends `נטפליקס`, and for a bare term it sends the
term. Mis-transcription spellings are never fed to the engine as hints.

Re-running raises `StageAlreadyRun` if `transcribe` is already in
`meta.stages`, unless `--force` is passed.

## Engine adapter interface

```python
class Engine(Protocol):
    name: str
    version: str
    def transcribe(self, wav_path: str, vocab: list[str] | None) -> dict: ...
```

Returns raw engine output; the module maps it to the contract.
Adapters live in `src/hebsub/engines/`, one file each.

### `ivrit_local`

- `faster-whisper` with `ivrit-ai/whisper-large-v3-turbo-ct2`.
- Language token must be set explicitly to `he`. The ivrit.ai models have
  degraded language detection by design — auto-detect will misfire.
- `word_timestamps=True`. `vad_filter=True`.
- `initial_prompt` carries the glossary terms when `--vocab` is passed.
- Free, offline, no key. This is the default engine.

### `elevenlabs`

- Scribe v2 batch endpoint. Key from `.env` as `ELEVENLABS_API_KEY`.
- Word-level timestamps come back natively — map them directly.
- Supports keyterm prompting; pass `--vocab` terms through.
- Returns no per-word confidence: emit `conf: null`, never 0. Downstream this
  means every word is *eligible* for the llm pass, which is the intended
  behaviour — see D9.
- Costs money per hour of audio. Never the default.

Adding a third engine must not require touching any file outside
`src/hebsub/engines/`.

## Timing violations — clamp, don't raise

Whisper occasionally emits word timings that overlap or run backwards. This is
an engine quirk, not a broken file, and raising on it would make the default
engine unusable.

- Clamp the offending value to the minimum that satisfies monotonicity.
- Append one warning per clamp: `{"stage": "transcribe", "code":
  "timing_clamped", "wid_start": <wid>, "wid_end": <wid>, "detail": "..."}`
  where `detail` names the original and clamped values.
- Never move a timestamp for any other reason. Immutability begins the moment
  transcribe returns.

## Failure behaviour

- ffmpeg missing → raise with the exact command that failed.
- Engine returns empty → raise. Do not write an empty Transcript.
- Engine returns segments without word timings → raise. Word timings are
  mandatory; `segment` cannot do its job without them.
- Clamping a timing is **not** a failure. See above.

## Acceptance criteria

- Given a real fixture clip, produces a Transcript passing
  `validate_transcript()`.
- `text` equals the joined `words` for every segment.
- `wid`s are `0..n-1`, contiguous, in word order, with no duplicates.
- Timestamps are monotonic and within `[0, meta.duration]` **after clamping**,
  and every clamp has a matching `timing_clamped` warning.
- A fixture with deliberately overlapping engine timings produces a valid
  Transcript plus the expected warnings, and does not raise.
- Switching `--engine` changes only `meta.engine` and the content — the
  output shape is structurally identical.
- Same input twice with `ivrit_local` produces identical output (set a seed /
  use greedy decoding so this holds).
- Running twice without `--force` raises `StageAlreadyRun`.

## Blocked on fixtures

Per D14, this module cannot be implemented or tested until the three real
30-second clips and their hand-corrected references exist in
`tests/fixtures/`. Do not synthesize audio to unblock it. The engine-adapter
mapping logic can be unit-tested against recorded engine responses (JSON
captured from a real run) without audio, and that is the only part of this
module that may proceed early.

## Explicitly out of scope

Punctuation, number formatting, glossary enforcement, line breaking, speaker
labels. All of these belong to later stages.
