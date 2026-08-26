# eval-protocol.md — the eval set and the benchmark

This is the foundation of the whole project. Build it before writing any
pipeline code. Without it, "better" is a feeling. With it, "better" is a
number you can hold Claude Code to.

## Whose job this is

**Raz's, entirely (D14).** Claude Code does not invent audio and does not
synthesize a golden reference transcript. A fabricated eval set produces
confident, meaningless numbers, which is worse than no numbers.

Until the fixtures exist, `transcribe`, `proofread`, and end-to-end `bench`
are blocked. `contract`, `segment`, `export`, `main`, and the `bench` metric
functions proceed on synthetic JSON and hand-written string pairs.

## The eval set

Target: **30–60 minutes of Hebrew audio**, of which **10–15 minutes is
hand-corrected to perfection**. That corrected portion is the reference.

Pick clips that look like your actual jobs, not like benchmark data:

| Clip | Why it's in the set |
|---|---|
| Clean talking head, studio-ish | The easy baseline. If a model fails here, drop it. |
| Two-person interview, some overlap | Real conversational Hebrew. |
| Speaker with heavy English mixing | Code-switching is where most engines collapse. |
| Audio with background music under speech | Every ad you cut has this. |
| Fast/mumbling speaker | The worst case you actually get sent. |
| Clip with numbers, dates, brand names | Where the glossary gets tested, and where v2's ITN will be judged. |

60–120 seconds each is plenty. More clips beats longer clips.

## The three test fixtures

Cut from the eval set, these live in `tests/fixtures/` and unblock the
`transcribe` and `proofread` tasks:

| Fixture | Length | Purpose |
|---|---|---|
| clean 30s | 30 s | the baseline; anything failing here is broken |
| english-mixing 30s | 30 s | bidi, Latin runs, `--isolate`, segment's English rule |
| noisy 30s | 30 s | music under speech; where `conf` actually varies |

Each ships with its hand-corrected reference. These three are the minimum bar
for starting; the full eval set is what `bench` runs against.

## Making the reference transcript

Transcribe by hand, or auto-transcribe then correct every word. Rules:

- Exactly what was said, including false starts and filler ("אה", "כאילו").
- Full punctuation. Numbers as digits.
- Correct spelling of every name and brand.
- One `.txt` per clip, UTF-8, plus a `.json` in the Transcript contract shape
  once you have the pipeline running.

Note on "numbers as digits": ITN is deferred out of v1 (D2), so the pipeline
will **not** produce digits from spoken numbers, and v1 does not warn about it
either — the `itn_skipped` code was deleted (D17). Spoken numbers pass through
as words and count as WER errors against a digits reference. That is expected,
and it is why WER is scored on normalized text. Keep the digits in the
reference: it is the correct transcript, it is the honest baseline for how far
v1 falls short, and it is what v2's ITN gets measured on.

## Metrics

Three numbers, not one. Word accuracy, entity accuracy, and punctuation are
different problems, and a tool can win on one while losing another.

### 1. Transcription accuracy

- **WER** via `jiwer`, against the reference.
- Normalize before scoring: strip niqqud, unify final-letter forms, strip
  punctuation, collapse whitespace.
- **Punctuation is scored separately** as F1 — otherwise a model that adds
  correct punctuation gets *penalized* against a punctuation-stripped
  comparison and you'll draw exactly the wrong conclusion.
- **Entity accuracy**: what fraction of names, brands, and numbers came out
  right. This is what clients notice. Averaged WER hides it.

### 2. Subtitle quality (post-pipeline only)

Automated counts on the final SubtitleFile:

- % of cards exceeding max CPS
- % of cards exceeding max line length (hard rule — must be 0)
- count of Hebrew splitting-rule violations (see `modules/segment.md`)
- count of cards with 3+ lines (hard rule — must be 0)
- count of each warning code

## One metric per pass (D4)

The rule is **not** "every pass must lower WER." A punctuation pass cannot
move a punctuation-stripped WER, and that rule would have deleted a pass for
succeeding at its job.

**Each pass must improve its own named metric without regressing any other.**

| Pass | Metric |
|---|---|
| glossary | entity accuracy |
| llm (substitution) | WER, punctuation-stripped and normalized |
| llm (punctuation) | punctuation F1 |
| segment heuristics | Hebrew rule violations, CPS/line-length warning counts |

A pass that moves nothing gets deleted, not tuned forever.

## The benchmark run

```
python -m hebsub.bench --set eval/ --engines ivrit_local,elevenlabs --out bench.csv
```

One CSV row per (clip × engine) with the metrics above, wall-clock time, and
cost. Ablate a pass with `--passes`. Full column list and behaviour in
`docs/modules/bench.md`.

Commit `bench.csv` every time you run it — the history of that file is the
story of whether the project is actually improving.

## Engines to test first

| Engine | Cost | Notes |
|---|---|---|
| `ivrit-ai/whisper-large-v3-turbo-ct2` | Free, local | Hebrew-tuned on conversational audio. Set language to `he` explicitly. |
| `ivrit-ai/whisper-large-v3-ct2` | Free, local | Slower, sometimes better on hard audio. |
| ElevenLabs Scribe v2 | ~$0.40/hr | Strongest commercial general model. Returns no per-word confidence — see D9. A 60-min eval set costs well under a dollar to run. |
| One wildcard | varies | Gemini or Speechmatics, for a third data point. |

## The rule that keeps you honest

**No feature ships without moving its own number on this eval set.**

If the glossary pass doesn't raise entity accuracy, delete it. If the
punctuation mode doesn't raise punctuation F1, delete it. If a segmentation
heuristic doesn't reduce rule violations, delete it. And if a pass improves
its own metric while regressing another, it doesn't ship either.

This rule is what stops the project turning into six months of
plausible-sounding code that nobody can prove helps.
