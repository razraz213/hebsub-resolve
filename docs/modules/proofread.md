# Module: proofread

Transcript in → corrected Transcript out. Same segments, same timings,
same word count. Only the words themselves may change.

This is the module that makes the product worth using. It is also the one
most likely to destroy a transcript if given freedom. Read the constraints
before writing a line of code.

## Owned files

```
src/hebsub/proofread.py
src/hebsub/llm/*
tests/test_proofread.py
```

## Learning your own vocabulary — `--learn`

```
python -m hebsub.proofread --learn corrected.srt
```

Harvests every Hebrew word from corrected file(s) into `lexicon.txt`, which
`hebrew_lexicon()` unions in. Format-agnostic on purpose: markup is stripped,
and SRT indices and timecodes fall out because they are not Hebrew.

**Why this is safe rather than merely additive.** D64 showed that adding words
to a lexicon usually *suppresses* corrections — every entry makes one more
non-word look real. A user's corrections are different in kind: they never
contain the ASR's mistakes, so learning can only make the **correct** side of a
disagreement recognisable. `תתעקם` becomes a word; `תתעכם` does not.

**What it is worth, measured honestly.** Leave-one-corpus-out, harvested words
transfer across domains **not at all** — Judaica vocabulary does not help a
workshop reel, matching D46. Against a corpus's own vocabulary the rule goes
from **6 fixes to 9**. Raz makes many reels per client, so the within-domain
case is the one that matters and the one three unrelated corpora cannot
measure.

`lexicon.txt` ships seeded with 1,790 words from the three corrected corpora.

## Taking the real word — `resolve_disagreements`

The only automatic correction in the second-opinion path, and Raz's rule:

| what the two models heard | action |
|---|---|
| both are real Hebrew words | **change nothing**, flag it |
| exactly one is a real word | **take the real one** |
| neither is a real word | **change nothing**, flag it |

Measured over all three corpora, on the 107 disagreements:

| | |
|---|---|
| edits made | **9** |
| words recovered | **6** |
| words damaged | **0** |
| pooled WER | 4.83% → **4.69%** |

**Both do-nothing branches are load-bearing.** On the 80 cases where both
candidates are real words, taking the partner would have cost **27 words** —
the shipping model is right 61% of the time when they disagree (D61). And on
the 13 where neither is real, the correct word was in the lexicon **once**, so
there is nothing to reach for.

The lexicon is DictaBERT's vocabulary. D46 measured it far too leaky to answer
"is this word suspicious" across a whole transcript — 94.3% coverage — but that
is a different job from being asked about two already-suspect candidates.

Word count, timings and `wid` are untouched; each change is recorded in `edits`
with reason `second_opinion`. A missing `transformers` yields an empty lexicon
and the pass degrades to flag-only rather than failing.

## The review list — `review_disagreements`

A second entry point, and the only thing in this module that **never changes a
word**. Given the shipping transcript and a second one from a different ASR
model, it returns the words the two disagree on.

Measured over all three corpora (D47, D48):

| | |
|---|---|
| a word the models **agree** on | wrong **1.5%** of the time |
| a word they **disagree** on | wrong **48.6%** of the time |
| flags per reel | ~5 |

That is a 49x lift on corpus 3, and by a wide margin the best error signal
found — against 0.5% for masked-LM rescoring (D44) and 31.7% for a
469,000-entry lexicon (D46).

**48.6% is under the >50% that D44 established as break-even for changing a
word automatically, so nothing is applied.** A review list has no such
threshold: it cannot damage a transcript because it never touches one.

Only **1:1 substitutions** are reported. Insertions and deletions are a
different phenomenon with different odds and were never measured; reporting
them would be quoting a number nobody has.

The output is a sidecar (`review.json`), not a `meta.warnings` entry — the
warning enum is frozen at eight codes, and `gap_not_applied` already sets the
precedent for a report that lives outside the Transcript. A review list is for
a human, not for a downstream stage.

The LLM pass uses a swappable adapter behind an interface, the same pattern as
the ASR engines. Adapters live in `src/hebsub/llm/`, one file each. Adding a
local DictaLM adapter later is a proofread task and touches nothing else.

## Hard constraints — non-negotiable

1. **Word count is immutable.** `len(words)` per segment is identical before
   and after. A correction is a 1:1 substitution.
2. **`wid` is immutable.** Never renumbered, never reordered.
3. **Timestamps are never touched.**
4. **Segment count and ids are never touched.**
5. **Every applied change appends to `edits`**, keyed on `wid`, with before,
   after, and reason.
6. **Anything considered and not applied is a warning, not an edit.** The
   `edits` list answers "what changed in my transcript" and must contain
   nothing else.
7. **A change that fails validation is discarded, not applied.** The original
   word survives. A missed correction is a minor annoyance; a hallucinated
   sentence is a broken product.

## What happened to ITN

Inverse text normalization ("עשרים אחוז" → "20%") is **deferred out of v1**
entirely (D2). Nearly all of its value lives in edits that change word count,
which the contract forbids, and the 1:1 subset was not worth a NeMo
dependency. `itn` is not a valid edit reason.

Nothing survives of it in v1 — not the pass, and not a warning either. The
draft kept an `itn_skipped` code so proofread could log number expressions it
declined to convert; that code is **deleted** (D17), because no pass was ever
funded to walk the words and emit it, and a warning nobody writes is dead
schema with a comment attached.

Spoken numbers therefore pass through as words, silently and correctly. When
ITN is built for v2 its requirements come from the eval set as it stands then,
not from a v1 log nobody wrote. The v2 intent is parked in `NOTES.md`.
Do not build the conversion, and do not emit a warning about not building it.

## Passes, in order

Each pass is a separate function. They run in this order and each one can be
disabled by a flag, so their individual contribution is measurable.

### 1. `glossary` — deterministic, highest confidence

A user-supplied file of terms: client names, brands, products, jargon.

```
# comment
נטפליקס                 ← protect this term; fuzzy-match to it
נטפליקט => נטפליקס      ← known mis-transcription, always replace
```

Format rules (D11):

- One entry per line, UTF-8, `#` starts a comment.
- `=>` is the mapping operator. `=` is not; it was ambiguous and is a
  parse error.
- **Mapped terms** (`wrong => right`) are exact replacements. No threshold,
  no fuzziness — if the normalized left-hand side matches, replace.
- **Bare terms** are fuzzy-matched: normalized Levenshtein similarity
  **≥ 0.82**, applied to tokens of **length ≥ 3 only**.
- Normalization before comparison: strip niqqud, normalize final letters
  (ך ם ן ף ץ), strip attached prefix letters (ב ל כ מ ש ה ו).

This pass alone fixes most of what editors actually complain about.

### 2. `llm` — contextual correction and punctuation

For homographs and misheard words the glossary can't catch (Hebrew has no
vowels; "דבר" is three different words), **and** for sentence punctuation,
which Hebrew ASR output almost entirely lacks.

Both jobs happen in one call. A separate local punctuation model was refused
for v1 (D5): 2–3 GB of dependency for something this call can already do.

- Adapter-based, swappable. First implementation is whichever API is already
  paid for. No vendor SDK — plain `requests`.
- Input: one segment plus two segments of context on each side, **plus** the
  glossary, **plus** the confidence score of each word.
- Output is a JSON list of two shapes, never prose:
  - `{"wid": 47, "replacement": "צריך"}` — substitution, reason `llm`
  - `{"wid": 47, "append": "."}` — punctuation mark appended to that word's
    existing text, reason `punctuation`
- `append` preserves word count by construction — the mark joins the word, it
  never becomes a word. Only punctuation characters are accepted in `append`;
  anything else is rejected.
- If the model returns prose, discard the whole response and warn
  `llm_rejected`.

Guards, all of which discard rather than crash:

- **Eligibility.** Only words with `conf < threshold` (default 0.75) may be
  substituted. High-confidence words are frozen. **`conf: null` counts as
  eligible** (D9) — unknown confidence is not evidence of correctness, and a
  paid engine that returns no confidences must not silently disable the pass.
  Punctuation `append` is not gated on confidence.
- **Edit budget.** At most **15%** of the words in a segment (configurable)
  may be substituted by this pass. On hitting the cap, apply nothing further
  in that segment, warn `edit_budget_hit`, and drop the remaining candidates.
  This is the backstop for the null-confidence case above.
- **Edit distance.** Reject any replacement whose edit distance from the
  original exceeds 60% of the original word's length — that's a rewrite, not
  a correction.
- Every rejection appends `llm_rejected` with the `wid` and the reason it was
  rejected. Rejections never appear in `edits`.

## Metrics — one per pass

The old "every pass must lower WER or be deleted" rule is replaced (D4).
A punctuation pass cannot move a punctuation-stripped WER, and the rule would
have deleted it for succeeding.

**Each pass must improve its own named metric without regressing any other.**

| Pass | Metric |
|---|---|
| glossary | entity accuracy — % of glossary terms rendered correctly |
| llm (substitution) | WER, punctuation-stripped and normalized per `eval-protocol.md` |
| llm (punctuation) | punctuation F1 against the reference |

Measured by `bench`. See `docs/modules/bench.md`.

## Acceptance criteria

- Word count, segment count, `wid` sequence, and every timestamp are
  bit-identical to input. This is a test, not a hope.
- On the golden fixtures, each pass improves its own metric and regresses
  neither of the others.
- Running proofread twice with `--force` produces output identical in
  `segments` and `edits`; only `meta.warnings` may differ. Byte-identity is
  not the test.
- Running proofread twice **without** `--force` raises `StageAlreadyRun`.
- With all passes disabled, `segments` and `edits` are unchanged;
  `meta.stages` still gains `proofread`.
- A deliberately adversarial fixture (an LLM adapter stubbed to return
  garbage/prose) results in zero applied edits, one `llm_rejected` warning per
  rejection, and no crash.
- A fixture where every word has `conf: null` and the stubbed adapter proposes
  a replacement for all of them applies at most 15% of them and warns
  `edit_budget_hit`.
- Every entry in `edits` has a `wid` that exists in `segments`.

## Blocked on fixtures

Per D14, the metric-based acceptance criteria need the real clips and their
hand-corrected references in `tests/fixtures/`. The invariant tests (word
count, wid stability, budget cap, adversarial adapter) run on synthetic JSON
and may proceed now.

## Explicitly out of scope

ITN. Line breaking, reading speed, anything to do with how the text is
displayed.
