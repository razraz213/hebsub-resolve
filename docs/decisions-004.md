# decisions-004.md — the Resolve host, and what the API probe proved

Follows `decisions-003.md`. Numbering continues.

Everything in D26–D29 was established empirically against DaVinci Resolve
Studio 21.0.3.7 on 2026-08-25, not read off a forum. The probe scripts and the
rendered frames are the evidence. Where a claim here contradicts something you
remember about Resolve, the probe wins — several widely repeated claims about
Hebrew in Resolve turned out to be false on 21.

---

**D26 — The product is a Resolve plugin; the engine stays host-agnostic.**

`hebsub` remains a pure CLI/library that knows nothing about any NLE. A new
module, `host_resolve`, is the only thing that imports `DaVinciResolveScript`.
It renders timeline audio out, calls the pipeline, and places the result back.
It contains no ASR, no Hebrew logic, and no segmentation.

Premiere and a standalone GUI are later adapters over the identical CLI, not
rewrites. They are explicitly **not** in v1. Building three hosts at once
produces three half-working hosts.

---

**D27 — Scripting works on both editions. Studio is confirmed.**

Blackmagic's README states the scripting API is "a common superset of functions
for both the Free and Studio versions." Only Studio-gated AI calls return
`False`. `GetProductName()` on this machine returns **"DaVinci Resolve
Studio"** — the Windows uninstall registry entry reads plain "DaVinci Resolve"
and is not a reliable edition signal. Do not use the registry to detect edition;
call `GetProductName()`.

`Timeline.CreateSubtitlesFromAudio()` is irrelevant regardless of edition:
Resolve's auto-caption language enum contains no Hebrew at all. The full list
is Danish, Dutch, English, French, German, Italian, Japanese, Korean, Mandarin,
Norwegian, Portuguese, Russian, Spanish, Swedish. That absence is the entire
reason this project exists.

---

**D28 — Neither placement mechanism can be positioned by script.**

> **This ruling was rewritten after the first version of it was proved wrong
> on a real timeline.** The original said both mechanisms were frame-exact.
> That conclusion came from probes run on an *empty* timeline, which is the
> one case where both happen to work. On a timeline with an actual edit, both
> fail — one silently, one destructively. The corrected findings follow. The
> lesson is recorded here rather than quietly fixed: a placement probe on an
> empty timeline proves nothing about placement.

*Mechanism A — SRT onto a subtitle track.*
`MediaPool.ImportMedia([path.srt])` does import an `.srt` as a MediaPoolItem
with `Type: "Subtitle"`, and a plain `MediaPool.AppendToTimeline([clip])` does
place it with the SRT's own timings, to the frame — **on an empty timeline**,
measured at 0/39 cards drifting.

On a non-empty timeline it is silently wrong. `AppendToTimeline` appends after
existing content at the **global timeline end**, regardless of which track the
clip belongs to. On a 1104-frame timeline all 39 cards landed at exactly
+1104 frames, past the end of the programme.

There is no way to position a subtitle clip:

| Route | Result |
|---|---|
| `AppendToTimeline([clip])` | lands at global timeline end |
| `AppendToTimeline([{clipInfo}])` with `recordFrame` | returns `[None]` |
| `AppendToTimeline([{clipInfo}])` with `startFrame`/`endFrame` too | **crashed Resolve** (process restarted, PID changed) |
| `Timeline.ImportIntoTimeline(srt)` | returns `False` |
| subtitles appended first, video second | subtitles land right, video is pushed to +1100 |

The `{clipInfo}` form must never be used on a subtitle clip under any
argument combination. That is a hard rule.

*Mechanism B — Text+ on a video track.*
`InsertFusionTitleIntoTimeline("Text+")` returns a TimelineItem whose Fusion
comp contains a tool with ID `TextPlus`. `SetInput("StyledText", ...)`
round-trips Hebrew byte-identically, and the overwrite-trim behaviour is real:
`TimelineItem` has no `SetStart`/`SetEnd`/`SetDuration` and `SetProperty` is
transform-only, but insertion at the playhead performs an overwrite edit that
trims the previous card to end where the new one begins. Inserts at
+0/+30/+55/+95 produced durations `[30, 25, 40, 125]`, so only the final card
keeps the 125-frame default.

**But the insert always targets video track 1 and it overwrites.** On a
timeline with footage on V1 it cuts the titles *into* the footage and destroys
the edit — observed directly. Neither available lever helps:

| Attempt | Result |
|---|---|
| `AddTrack("video")` then insert | still lands on V1; the new V2 stays empty |
| lock V1, then insert | insert fails outright, returns `None`; does not fall through to V2 |

There is no `SetCurrentTrack` in the API. Destination-track selection is a UI
concept the scripting layer does not expose.

*Consequence for `host_resolve`.* Placement is only attempted when it is
provably correct:

- `--placement srt` appends only when the timeline is empty. Otherwise it
  imports the `.srt` into the media pool and says so — one drag, frame-exact,
  zero risk.
- `--placement textplus` refuses outright when V1 holds anything.

Full automation on a populated timeline would mean rebuilding the edit into a
new timeline (subtitles appended first, then every clip re-placed via the
`{clipInfo}` `recordFrame` form, which *does* work for video clips). That is
viable for a single-clip reel and a minefield for a real edit — transitions,
speed changes, compound clips, Fusion comps. Not v1. See `NOTES.md`.

---

**D29 — Hebrew renders correctly in Resolve 21. The tofu is a font default.**

A Text+ left on its default font renders Hebrew as `▯▯▯ ▯▯▯▯ ▯▯▯▯▯`. This is
missing glyphs, not broken bidi, and it is the single most likely thing to make
someone wrongly conclude Resolve cannot do Hebrew.

With `SetInput("Font", "Arial")` the same Text+ renders `אני עורך וידאו`
correctly — right glyphs, right RTL order, `אני` on the right. Verified by
rendering actual frames and looking at the pixels, not by reading the string
back.

Bidi is sound in both mechanisms, including the cases that usually break:

| Case | Text+ (Arial) | Subtitle track |
|---|---|---|
| `אני עורך וידאו` | ✅ | ✅ |
| `אני עורך ב Premiere Pro` | ✅ | ✅ |
| `יש לי 20 אחוז הנחה` | ✅ | ✅ |

Therefore: **`host_resolve` MUST set an explicit Hebrew-capable font on every
Text+ it creates.** Never rely on the default. Fonts confirmed present on this
machine: Arial, David, FrankRuehl, Gisha, Miriam, Narkisim, Segoe UI, Tahoma.

No RTL control characters (RLM/RLE/PDF) are needed. Do not inject them — the
renderer applies bidi itself, and injected marks would corrupt the `text`
invariant for no gain. This narrows what `export`'s `--isolate` flag is for; it
is a concern for *other* players, not for placement inside Resolve.

---

**D30 — Cards are ~15 characters, phrase-broken, sentence-bounded.**

Raz's target is viral short-form: roughly 15 characters on screen at a time,
about three Hebrew words.

"Never start or end mid-sentence" is **not achievable** at that width and is
not the rule. Hebrew averages ~4.5 characters per word, so a normal spoken
sentence of 8–15 words is 40–75 characters — three to five cards. Cards must
break inside sentences.

The rule that *is* enforced, and that captures the intent:

- A card never contains the end of one sentence and the start of the next.
  Sentence boundaries are always card boundaries.
- Breaks land on phrase boundaries by the existing `segment` priority ladder.
- The Hebrew rules in `docs/modules/segment.md` still bind, and a card that
  cannot satisfy them emits `hebrew_rule_violation` (D25).

This is a configuration change to `segment`'s existing max-line-length
constraint plus one new sentence-boundary rule. It is **not** a rewrite, and it
is not a contract change.

---

**D31 — Proofreading is local, conservative, and word-count preserving.**

Raz chose fully local over a cloud LLM. Combined with the ruling that timing
beats grammar, this resolves more cleanly than expected:

A conservative proofread may only substitute one word for one word. That is
exactly what a masked-language-model does, and it is **not** what a generative
LLM does. So the engine is an encoder (DictaBERT-family), not a 7B generative
model: mask the suspect token, score candidates, substitute or leave alone.
Word count is preserved *by construction* rather than by a rule the model might
violate.

Consequences:

- `meta.warnings` and `edits` keep keying on `wid`, unchanged.
- Timestamps stay exactly as `transcribe` emitted them. Contract stays v2.
- Grammar errors that would require merging or splitting words are **not
  fixed** in v1. They are out of scope by this ruling, not overlooked.
- This adds `torch` and `transformers` to the dependency set, which
  `CLAUDE.md` currently refuses. That refusal is hereby amended **for the
  proofread module only**, on the grounds that the local-only choice makes them
  unavoidable. Exact pins required, as always. The refusal on vendor LLM SDKs
  stands.

The model identifier is **not** fixed by this decision. It must be verified to
exist and be benchmarked against the eval set before being written into a spec.
Do not hardcode a HuggingFace repo name that nobody has downloaded.

---

**Still open, deliberately.**

- Which DictaBERT variant, and whether it beats a plain glossary pass at all.
  Per `eval-protocol.md`, a pass that does not move its own metric gets
  deleted. This one has not been measured yet.
- Whether cards ship as Text+ or on a subtitle track by default. Both work
  (D28). Text+ is the styled deliverable Raz asked for; the subtitle track is
  the shorter path to a working end-to-end run and is what `export` already
  produces.
- The probe left a project named `ZZ_hebsub_probe_DELETEME` in the project
  manager. Safe to delete by hand; nothing references it.

---

Post-freeze findings batch into `decisions-005.md`, not this file.

---

**D32 — CPS limit is 25 for the short-form profile, not 17.**

Measured over the nine reels: at the D30 defaults, a 17 CPS limit fires on
**157 of 624 cards — 25% of everything produced.** Max observed was 31.8.

That is not a quality problem it is detecting, it is the wrong instrument.
CPS is a *reading comfort* measure built for broadcast subtitles, where the
viewer reads the card independently of the audio. At ~15 characters synced to
speech, CPS mostly restates the speaker's rate, and `segment` has no lever on
it: it cannot slow speech down, and the cards already break at the cleanest
available boundary. Every one of those 157 warnings is unactionable.

A warning that fires on a quarter of all output trains you to ignore the
warning list, which is exactly what `meta.warnings` exists to prevent.

The default becomes **25.0**, which flags only genuinely extreme cards.
`--max-cps 17` restores the broadcast norm for long-form work.

This does not change the CPS *formula* (D12, `" ".join`), only the threshold
at which the quality target warns. `export`'s `stats.cards_over_cps` uses the
same threshold and moves with it.

Open question, not settled by this ruling: whether CPS earns its place in the
short-form profile at all, or whether the honest metric is
`min_card_duration`. Decide it from `bench` once there is a `bench`, not from
taste.

---

**D33 — The card budget is 14 characters, measured against Raz's own cuts.**

Raz supplied a hand-corrected reference for all nine reels — 650 cards on one
timeline, split per clip into `tests/fixtures/references/`. That makes the
segmenter measurable for the first time, and D30's "roughly 15 characters"
becomes a number that can be checked rather than asserted.

The metric is **boundary F1 against the human cuts**: what fraction of Raz's
650 card boundaries the segmenter also chose, within 120 ms, balanced against
how many of its own boundaries were spurious. Recall alone is not enough —
cutting everywhere matches every human cut by accident, and a 12-character cap
does exactly that (786 cards against his 650).

| cap | recall | precision | F1 | cards | mean chars | mean dur |
|---|---|---|---|---|---|---|
| 12 | 58.2% | 48.2% | 52.7% | 786 | 8.8 | 0.64s |
| 13 | 61.2% | 54.4% | 57.6% | 732 | 9.5 | 0.69s |
| **14** | **60.2%** | **58.5%** | **59.3%** | **668** | **10.5** | **0.76s** |
| 15 | 56.2% | 58.5% | 57.3% | 624 | 11.4 | 0.81s |
| 17 | 50.2% | 58.6% | 54.1% | 556 | 12.9 | 0.91s |
| 20 | 40.8% | 55.6% | 47.0% | 477 | 15.2 | 1.06s |

Raz's own reference: 650 cards, mean 10.4 chars, mean 0.80s.

Two independent criteria land on 14. It maximises boundary F1, *and* it
reproduces the reference's card shape more closely than any other setting —
668 cards vs 650, 10.5 chars vs 10.4, 0.76s vs 0.80s. Nothing was tuned to
make those agree; they agree because 14 is the right number.

The instinct that a wider cap would help was **wrong**, and worth recording as
wrong. It came from one observed case — Raz cutting `כדי לזכור בן אדם` as a
single 16-character card that a 15-cap could not fit. Generalising from that
one card would have pushed the budget to 17 or 18 and cost 5–11 points of F1.
One example is not a distribution.

*Consequences.*

- `Config.max_chars_per_card` defaults to **14**.
- **Boundary F1 is the metric for the segment pass**, replacing the vaguer
  "CPS/line-length warning counts" in `eval-protocol.md`. It is the only
  segment metric that measures the thing the module actually decides. Wire it
  into `bench` when `bench` is built.
- ~40% of cuts still differ from Raz's. That is the segmenter's real headroom,
  and it is now a number that can be moved rather than an opinion.

**D32 is confirmed by the same reference.** Raz's own hand-cut cards run at
mean 13.7 CPS with 19% over 17 and only 2% over 25. A 17 CPS limit would flag
a fifth of his own finished work as defective; 25 flags 2%. The threshold
change was made before this reference existed and the reference independently
supports it.

---

**D34 — Baseline ASR accuracy is 95.6%, and two assumptions died measuring it.**

Scored against Raz's reference for all nine reels, on normalised text per
`eval-protocol.md` (strip niqqud, unify finals, strip punctuation):

| reel | WER |
|---|---|
| reel F | 0.9% |
| reel D | 2.0% |
| reel I | 2.6% |
| reel F2 | 2.9% |
| reel J | 3.1% |
| reel H | 3.9% |
| reel B | 5.2% |
| reel E | 6.9% |
| reel C | 9.2% |
| **overall** | **4.4%** (95.6% word accuracy, 1452 reference words) |

*The error profile is almost pure substitution.* Across 1452 words: 63
substitutions, **1 deletion, 5 insertions**. The engine is not dropping or
inventing speech; it is mishearing individual words.

That is the strongest possible support for D31. A word-count-preserving
proofread can, in principle, reach ~92% of the errors without touching a
single timestamp. The conservative choice is not a compromise here — it is
aimed squarely at where the errors actually are.

**Assumption 1, dead: a glossary can fix these.** Of 33 clean one-to-one
errors, only **7 (21%)** are safe for a deterministic rule. The rest are
contextual — the ASR heard a real Hebrew word, just the wrong one:

```
כל    => קול      אם  => ואם     אחד => אחת
כמה   => קמעה     ה'  => השם     שמים => עוצמים
```

Shipping `כל => קול` as a find-and-replace would rewrite one of the commonest
words in Hebrew on every run and destroy far more than it repairs. `glossary.txt`
therefore carries the 7 safe mappings plus protected vocabulary, and lists the
26 rejected ones commented out with the reason. **Do not uncomment them.**

Corollary: the glossary pass has a hard ceiling of about 21% of errors. The
remaining 79% are exactly the homograph problem `proofread.md` describes, and
exactly what a masked LM is for.

**Assumption 2, dead: `--vocab` is free.** Feeding the glossary to
faster-whisper as `initial_prompt` **made accuracy worse** — 4.4% → 5.1% WER.
Five reels degraded, three unchanged, one improved:

| | baseline | with --vocab |
|---|---|---|
| word accuracy | **95.6%** | 94.9% |

An initial prompt biases the decoder toward the supplied terms, so it starts
hearing them where they were not said. `--vocab` stays **off by default** and
is documented as measured-harmful on this eval set. It may still help a corpus
full of genuinely unusual proper nouns; that is a claim to measure, not
assume.

Both assumptions were mine, both were reasonable, and both were wrong. That is
the entire argument for having built the eval set before the passes.

---

**D35 — Short-form subtitles ship unpunctuated. `bench` found it.**

Raz's hand-corrected reference carries **12 punctuation marks across 1445
words** — 0.8%, and most of those are gershayim inside abbreviations
(`חב"ד`, `חז"ל`, `נתב"ג`) rather than sentence punctuation. He does not
punctuate viral subtitles. The pipeline was punctuating every card.

This surfaced because `punct_f1` came back empty on every row of the first
real `bench` run. The metric was working exactly as designed — returning null
rather than 0.0 for "nothing to measure" — and the null is what exposed it.

Punctuation is **not** removed upstream. `segment` needs it: sentence-final
and clause punctuation are split priorities 1 and 2, and D30's rule that a
card never spans two sentences depends entirely on being able to see where
sentences end. Strip it at `transcribe` or `proofread` and segmentation gets
materially worse.

So it is kept through the whole pipeline and dropped at **display** time,
which is what `export` is for:

- `export --strip-punct` removes trailing `.,;:!?…` from each displayed word.
- The SubtitleFile on disk is unchanged, and `stats` are unaffected — CPS was
  computed upstream on the real text.
- Gershayim and geresh are **never** stripped. They belong to the word;
  removing them would corrupt `חב"ד` into `חבד`.
- Off by default in `export`, which is a general-purpose module. **On** in
  `host_resolve`, which is the short-form button.

*Consequence for the eval set.* `punct_f1` is unmeasurable against this
reference and will stay null. That is honest rather than broken: the
punctuation pass described in `proofread.md` has no target to hit here, and a
number would have to be invented to produce one. If punctuation ever matters
for a long-form job, that needs its own reference.

---

**D36 — The glossary pass ships. Fuzzy matching nearly sank it.**

Measured through `bench`, baseline against `--passes glossary`, nine reels:

| metric | baseline | glossary | |
|---|---|---|---|
| WER | 4.07% | **3.65%** | −0.41 |
| **entity_accuracy** (its own metric) | 0.8875 | **0.9803** | **+9.29 pts** |
| boundary_f1 (must not regress) | 0.5916 | 0.5916 | unchanged |

Five clips better, four unchanged, **none worse**. reel F reached 0.00% WER.
`eval-protocol.md` asks that a pass improve its own named metric without
regressing any other; this one does, so it ships.

That is the headline. The useful part is what went wrong first.

**Two destructive rewrites, both from fuzzy matching, both on real output:**

| was | became | reality |
|---|---|---|
| `מתחילים` (they begin) | `תהילים` (Psalms) | unrelated words |
| `תפילון` (a prayer booklet) | `תפילין` (phylacteries) | different objects |

Two distinct causes, and both are worth keeping written down.

*Cause 1: scoring the prefix-stripped variant.* D11 asks for attached
prefixes to be stripped before glossary comparison. Applying that to the
**fuzzy** score compares a truncation of a real word against a term:
`מתחילים` strips to `תחילים`, one letter from `תהילים`, scoring 0.83 against
a 0.82 threshold. Fixed by scoring the **full** form only — 0.71, safely
below. Variants are still used for exact mapping lookup, where the left-hand
side is a known mis-transcription, and for the "already correct" check, so a
prefixed term is recognised and left alone.

*Cause 2: speculative protected terms.* `תפילין` was added to the glossary on
the assumption it might get mis-heard. It never appears in the corpus, so it
could never fix anything — but it could still rewrite a real word one edit
away, and it did. **A protected term with nothing to correct is pure
downside.** `תפילין`, `פייטן` and `חב"ד` were cut for this reason; every
remaining term occurs in the reference.

Removing `תפילין` moved תפילת הדרך from "same" to "better". The false positive
had been silently cancelling out a real correction in the same reel — the kind
of thing an aggregate WER hides completely and only a per-clip table exposes.

*Standing rules from this.*

- **Do not add a glossary term that does not occur in the corpus.** A test
  enforces the consequence: any fuzzy edit must be within edit distance 1.
  Mapped terms are exempt, because a human observed and wrote them down —
  `מכבלים => מכוונים` is distance 3 and entirely correct.
- The two mechanisms have different standards of proof and must not be
  conflated. A mapping is evidence; a similarity score is a guess.

---

**D37 — The llm pass is confusion-set rescoring, opt-in, worth 0.12pp.**

D31 chose a local encoder for the contextual errors a glossary cannot touch.
The obvious implementation — mask the word, take the model's top prediction —
was probed first and is **wrong**. Recording why, because it is the thing
anyone would try next.

*Free generation is dangerous.* A masked LM predicts what is likely, not what
was said; it has no access to the audio. Probed on `dicta-il/dictabert`:

| context | correct word | model's preference |
|---|---|---|
| `הדבר הכי ___` | `טוב` (p=0.06) | `חשוב` (p=0.84) |

A 14:1 preference for overwriting a perfectly correct word. Across four
control cases where the ASR was already right, top-1 generation would have
damaged one with dictabert and two with BEREL. At 96% baseline accuracy that
trade loses badly. Domain vocabulary is also simply missing — `תפילון` and
`ברכון` are OOV, so the model cannot represent what this corpus is about.

*What the probe did show* is that the measured errors are overwhelmingly
**phonetic**, not semantic:

```
קשרה -> כשרה (ק/כ)   כופף -> חופף (כ/ח)   האור -> העור (א/ע)
מאור -> מעור (א/ע)   הצליחות -> הסליחות (צ/ס)   אשכנדים -> אשכנזים (ד/ז)
```

So the adapter never asks the model what word belongs. It generates candidates
by swapping confusable Hebrew letters, keeps only those in the model's vocab,
and asks the model to choose between the heard word and *those* — proposing
nothing unless a candidate wins by a wide margin and clears an absolute floor.

*Measured, nine reels:*

| config | WER | llm edits |
|---|---|---|
| no passes | 4.07% | 0 |
| glossary only | 3.66% | 0 |
| **glossary + llm, conf < 0.99** | **3.54%** | **2, both correct** |

*The eligibility gate is nearly inert, and that is a finding.* Whisper's mean
confidence is **0.968** on 96%-accurate output, so `conf < 0.75` admits only
3.8% of words — and of those, most are garbled non-words the model cannot
score anyway. Confidence is not calibrated here and carries almost no signal
about correctness. The default therefore moves to **0.99**. What actually
prevents damage is the confusion set, the margin, and the glossary freeze —
never the confidence gate. Do not mistake the one for the other.

*The glossary outranks the model.* Before this rule the pass rewrote `אדס`
(the Ades synagogue, a protected term) into the non-word `עדס` — turning
2 correct / 1 wrong into a wash. Words the glossary vouches for are now frozen
against the llm pass: bare terms, mapping keys, **and mapping targets**, since
leaving targets open would let the llm pass silently undo the glossary pass
one stage later. Same principle as D36: a written-down observation is
evidence, a probability is a guess, and evidence wins.

*Status: opt-in.* Default `passes` is `("glossary",)`. Enabling the llm pass
costs `transformers` + `torch` (~2 GB) and a model download for 0.12pp. That
is a real improvement with zero measured damage, and it is nowhere near worth
making mandatory. `--passes glossary,llm --llm-adapter masked_lm` turns it on.

*Not deleted, despite the eval-protocol rule.* The rule says a pass that does
not move its metric gets deleted. This one does move it, just barely. The
adapter stays, off by default, with this decision attached — the negative
result about free generation is worth more than the code is.

---

**D38 — Two segment rules were wrong. A second corpus proved the fix real.**

Raz supplied a second hand-corrected set: 10 import/business reels, a
different speaker and a completely different subject from the Judaica corpus.
It is **held out** — every threshold in `segment` was chosen against corpus 1
and never against this one, so it is the only honest test of whether a change
generalises or memorises.

*Boundary F1, before and after:*

| corpus | before | after |
|---|---|---|
| corpus 1 (Judaica, tuned on) | 59.3% | **62.8%** |
| corpus 2 (business, held out) | 60.2% | **64.4%** |

Both moved, by similar amounts. Overfitting would have left the held-out set
flat; it did not. The baseline itself is worth noting — corpus 2 scored 60.2%
*before any change*, slightly **above** the corpus it was tuned on, which says
the D33 card budget was a real finding rather than a fit to nine reels.

*Change 1: the construct-chain rule never fired.* The list holds
`("בית","כנסת")`, but Hebrew glues prefixes on, so the text says `לבית` and
the raw tuple comparison missed every time. The segmenter was splitting
`נכנסים לבית / כנסת` while claiming to protect `בית כנסת`. Matching is now
prefix-aware. Raz splits **0** construct chains in 649 cuts, so the rule was
worth making work rather than dropping.

*Change 2: rule violations were a veto, not a cost.* `_break_priority`
sorted `(violations, rank, gap)` lexicographically, so **any** violation lost
to none regardless of how bad the alternative was. Auditing Raz's own cuts
shows that model is simply wrong — he breaks the cheap rules routinely:

| rule | his breaks that violate it |
|---|---|
| never end on `את` | 16 (2.5%) — `אומרים את`, `לוקחים את` |
| never end on a function word | 23 (3.5%) — `על` ×11, `של` ×5 |
| never split a construct chain | **0** |

Violations are now weighted and added to the ladder rank: `את` and
function-word cost 1 (about one rung), construct chain / number-unit /
English-phrase cost 4 and stay effectively prohibitive. A strong ladder
position can outweigh a cheap rule; nothing outweighs an expensive one.

*A measurement bug worth more than either change.* Re-basing each reference
reel to its own first card assumes speech starts at t=0 in the clip. Two
clips have a lead-in — `reel N` at 0.84 s, `reel G` at 2.26 s — so
every boundary in those reels was shifted by that much. `reel N` was scoring
**31.5%**; anchored on the ASR's first spoken word it scores **62.9%**. It was
never a segmentation problem. References are now anchored on speech onset.

*What the corpus says is still wrong.* On 8 independent reels — excluding the
duplicate framing and the suspect `תגובה` reel — corpus 2 reaches **66.0%**.
The remaining gap is real and concentrated: `reel A` produces 94 cards
against Raz's 77, over-segmenting dense interview speech. That is the next
thing to chase, and it is a genuine quality gap rather than a data artifact.

*A systematic bias, deliberately not acted on.* Almost every reel improves by
1–8 points under a constant shift of about **−0.06 s** — our card starts sit
consistently ~60 ms later than Raz's. That is either Whisper's word onsets
lagging the audio or Raz cutting slightly early on purpose. Applying it would
be a **contract change**: `docs/contracts.md` makes timestamps immutable after
transcribe, and `export`'s `--gap` is explicitly forbidden from moving a
start. It is worth roughly 3–5 points and it is not mine to take unilaterally.
Raised, not done.

---

**D39 — Three findings from Raz running it himself on unseen content.**

He ran the panel on a clip, hand-corrected the output, and kept both. Nine
counted errors at 5.49% WER. Classifying them individually is where the value
was; the aggregate said nothing actionable.

*Five of nine were not errors.* Unvocalised Hebrew is written two ways and
both are correct — כתיב מלא spells out ו and י, כתיב חסר leaves them implicit:

| he wrote | it wrote |
|---|---|
| `המשלוח` | `המשלח` |
| `ליבא` | `לייבא` |
| `הייבוא` ×2 | `היבוא` |
| `יכולתי` | `יכלתי` |

The real error rate on that clip was about **1.8%**, not 5.49%. Across corpus
1 the same correction moves the mean from **3.65% to 2.99%** — **18% of all
counted error was spelling convention**. `wer_tolerant` is added as a
**new column** rather than redefining `wer`: the strict figure stays
comparable with every earlier row in `bench.csv`, and collapsing ו/י also
merges a few genuinely different words (`שיר` and `שר`), so it is not safe as
the only number.

*One was a real bug.* faster-whisper returns `75` and `,000` as separate word
tokens, so "75,000 שקל" reached the screen as "75 ,000 שקל" — with a card
boundary free to fall between them. Every price, date and statistic was
exposed to it, which for business content is most of the numbers on screen.
`transcribe` now rejoins a separator followed by digits when the previous
token ends in a digit; a plain comma after a number is left alone. It belongs
in transcribe because that is the stage that decides what a word is — doing it
later would change word count, which the contract forbids.

*Three were genuine:* `שיבאתי`→`שהבאתי`, `להתאחסן`→`להתאכסן` (ח/כ),
`תעקבו`→`תעכבו` (ק/כ — his call to action turned into "delay").

**The llm pass checkbox is removed from the panel.** The last two errors are
exactly what confusion-set rescoring is for, and it caught neither. Measuring
why produced the number that settles it:

| | |
|---|---|
| words in DictaBERT's vocabulary | 96.0% |
| words it can actually **evaluate** | **50.7%** |

It can only score a word when the word *and* a candidate are both single
tokens, and Hebrew's inflected forms are not: `תעקבו`, `מתעטפים`, `הציציות`,
`כשהראש`, `וציוונו` are all invisible to it. No threshold changes that. It
fired twice across nine reels for +0.12pp and produced byte-identical output
on Raz's clip. A checkbox that reliably does nothing is worse than no
checkbox. The adapter stays in the repo behind
`--passes glossary,llm --llm-adapter masked_lm`, with D37 and this entry
explaining why it is not offered.

*Settled (2026-08-25).* Raz's run produced `תעכבו` where a re-run here
produced `תעקבו`, on what looked like the same audio. It was not the same
audio — he confirms he ran a different file. Not a decoding-determinism
problem, and not a bug.

---

**D40 — Over-segmentation was structural. Greedy replaced with an optimiser.**

Every measurement showed the same thing: more cards than Raz cuts. 698 against
650 on corpus 1, 686 against 641 on corpus 2, and 94 against 77 on the worst
reel. `recall > precision` throughout, which is the signature — finding most of
his cuts *plus* a pile he never made.

*The cause was not a bad threshold.* A greedy pass fills to the character
budget and cuts at the least-bad boundary in reach, so it cuts **because the
budget filled up**, not because the speech invited a cut. Measured over 2838
boundaries against his own cuts:

| pause between words | he cuts there |
|---|---|
| 0.00–0.01s (no pause) | **34.6%** |
| 0.01–0.05s | 60.0% |
| 0.05–0.10s | **81.2%** |
| 0.10–0.20s | **82.5%** |

A pause of 50 ms more than doubles the odds. It was **priority 5 of 6** on the
splitting ladder, below "nearest word boundary to the midpoint" — the single
strongest predictor, ranked next to last. Sentence-final punctuation is 7.4×
more common at his cuts and clause punctuation 2.4×, but neither approaches the
gap.

*Two changes.* The silence gap is now subtracted from the break cost on the
same scale as the ladder rank, so a real pause outweighs any syntactic
preference. And `_group_sentence` no longer walks left to right: it considers
every legal partition of the sentence and takes the cheapest, where cost is the
sum of break costs plus a `card_penalty` per card. O(n²) over a sentence, and
sentences are short.

*Result:*

| corpus | before | after |
|---|---|---|
| corpus 1 (Judaica, tuned on) | 62.8% | **65.1%** |
| corpus 2 (business, held out) | 64.4% | **66.9%** |

Both moved, the held-out set by more. `75 אלף` reached 86.8%.

*The penalty is set to 0.5, and the reasoning matters more than the number.*
The two corpora peak in different places — 1.0 on corpus 1, 0.0 on corpus 2 —
which is the signal that neither peak is real. The curve is nearly flat: 1.8
points across the entire sweep. **The penalty is not what earned the gain**;
the optimiser and the gap weighting are. 0.5 sits at corpus 2's peak and within
0.1 of corpus 1's, so it favours neither speaker.

*Worth recording as a trap avoided.* At `card_penalty=4.0` the card count is
almost exactly Raz's — 651 against 641 — and boundary F1 is **worse**. Matching
how many cards he makes is not the same as matching where he puts them, and a
count is the tempting thing to optimise because it is the number you can see.

*Still the worst reel.* `reel A` remains at 45.3% with 95 cards against 77.
Fast interview speech with few pauses is exactly where a pause-driven segmenter
has least to work with.

---

**D41 — A third corpus, a free WER win, and a width hypothesis that died.**

Raz ran the panel on a workshop timeline — ג'יפים, drawer slides, woodworking
— corrected the output by hand, and handed back both halves. That pair is now
`tests/fixtures/references3/` (611 cards). It is the first corpus outside the
talking-head domains the tool was built against, and the first that measures
the product *as shipped* rather than the pipeline in a harness.

It immediately scored worse than anything before it: **5.45% WER and 56.3%
boundary F1**, against ~3.0% and ~66% on corpora 1 and 2. Two investigations
came out of that. One paid; one did not. Both are recorded, because the one
that did not is the more useful record.

---

*The geresh split — the largest single error in the file, and free to fix.*

The engine hands back `ג'יפ` as two tokens, `ג` and `'יפ`, splitting at the
geresh. But the geresh is not punctuation: it marks a sound Hebrew has no
letter for — `ג'` is *j*, `צ'` is *ch*, `ז'` is *zh* — so it lives inside the
word. Eight occurrences in one 15-minute timeline, each costing two word
errors, and a card boundary free to fall into the middle of the word.

Same bug class as the thousands-separator split fixed in D39, same fix, same
placement: before `wid` assignment, because transcribe is the stage that
decides what a word is.

Measured on the identical token stream, so the join is the only variable:

| | tokens | WER |
|---|---|---|
| before | 1504 | 5.45% |
| after | 1496 | **4.51%** |

**A 17% relative reduction from eight joins.** It generalises to every loanword
Hebrew spells with a geresh — ג'וב, צ'ק, ז'אנר, ג'ינס — which is most brand
names and most technical vocabulary.

*The guard, and its known limit.* Joining requires the preceding token to end
in ג, ז, צ or ץ — the four letters a geresh actually marks in modern Hebrew.
The narrowness is the guard: it is what stops a genuine quotation (`אמר 'שלום`)
being glued onto the word before it, since ר takes no geresh. The limit that
remains: a quotation opening right after a word that *does* end in one of them
— `אז 'שלום` — would still be joined. Across 7,300 words of real engine output
every geresh-initial token was a broken word and not one was a quotation, so
the trade is worth taking. `test_the_known_false_join_is_pinned` holds that
case so it stays visible.

An earlier, tighter version of the guard required the stem to be prefix-only
(`ג`, `לג`, `הג`). It missed `הבירצ'ה` three times, because the stem is a whole
word. Narrowing the *letter* set was the better guard than narrowing the *stem*
shape.

---

*The width hypothesis, and why it is off by default.*

Measured against Raz's hand-cut cards: **20.5% of his workshop cards are wider
than 14 characters, and the widest is 24.** `max_chars_per_card=14` was a hard
wall. One card in five was one the segmenter was structurally incapable of
producing, which looked like a hard ceiling on recall.

So the wall became a target: a soft cost per character above 14, with the hard
ceiling lifted to `14 + width_headroom`. Swept over all three corpora:

| headroom | cost | corpus 1 | corpus 2 | corpus 3 | mean |
|---|---|---|---|---|---|
| **0 (control)** | — | **65.1%** | **66.9%** | 55.2% | **62.4%** |
| 4 | 1.5 | 62.7% | 64.4% | **59.0%** | 62.0% |
| 4 | 4.0 | 64.2% | 66.1% | 57.3% | 62.5% |

**Flat.** Best mean 62.5% against a control of 62.4% — noise. And the corpora
disagree in a way that explains itself: allowing wide cards is worth +3.8 F1 on
corpus 3 and costs ~2.5 on each of the others.

The reason is in the references, not the algorithm:

| corpus | mean width | over 14 chars | max |
|---|---|---|---|
| 1 (Judaica) | 10.4 | 7.8% | 20 |
| 2 (business) | 10.3 | 8.7% | 20 |
| 3 (workshop) | 11.3 | **20.5%** | **24** |

**Card width is a property of the content, not a constant.** Technical content
dense with long loanwords wants wider cards; talking-head content does not.
Guessing one global width costs more than it earns.

*So the mechanism ships and the default does not change.* `width_headroom=0`
reproduces the previous behaviour exactly, and `test_the_default_width_is_a_hard_wall`
pins that. For workshop-style content, `width_headroom=4` with
`over_target_cost=1.5` is worth +3.8 F1 and is there to be turned on.

The honest summary: the observation was right, the diagnosis was wrong. One
card in five being unreachable *is* true, and it is *not* what was costing the
most.

---

*Re-tuning against three corpora confirmed the existing defaults.*

With corpus 3 in the mix and headroom off:

| target width | corpus 1 | corpus 2 | corpus 3 | mean |
|---|---|---|---|---|
| 13 | 61.5% | 61.0% | 53.8% | 58.8% |
| **14** | **65.1%** | **66.9%** | 55.2% | **62.4%** |
| 15 | 60.3% | 61.4% | **62.6%** | 61.4% |
| 16 | 58.3% | 56.0% | 57.5% | 57.3% |

`card_penalty` likewise peaks at 0.5 (62.4%), the value D40 chose. **Nothing
changed.** A tuning that was chosen on one corpus and re-confirmed on two
held-out ones, one of them from a different domain, is a tuning worth trusting.

Note the peak at 14 is *sharp* for corpora 1 and 2 — three points either side —
and corpus 3 peaks at 15 instead. Same finding as the width sweep, arriving by
a second route.

---

*Two things the corpus surfaced that are not fixed here.*

The panel does not write its intermediates. `host_resolve.run` writes
`01_raw.json` and `03_segmented.json`; `ui/app.py` has its own copy of the
pipeline and writes only `final.srt`. That breaks CLAUDE.md rule 6 and it is
why corpus 3 needed a fresh transcription pass to be evaluated at all. Recorded
in `NOTES.md`; it is a `ui` fix, not a `segment` one.

Raz's `Correct.srt` is missing the blank-line separator between some cards. A
strict reader silently swallows those cards and scores their timecodes as
words — it read 5.45% WER as 7.31% before it was caught. The eval harness now
detects a card by an index line followed by a timecode line rather than by
blank-line splitting. Worth remembering the shape of that failure: **a parser
bug in the measuring instrument reads as a quality problem in the product.**

---

**D42 — The render stays. Rebuilding the mix from source files is faster and
worse.**

The ask was to drop the audio render, because a different Resolve plugin read
the timeline directly and felt instant. The approach is real and the API
supports it. It was built, measured against the render on the same timeline,
and rejected.

*What the API gives you.* `GetItemListInTrack("audio", n)` returns the clips;
`GetMediaPoolItem().GetClipProperty("File Path")` locates the source. Two rate
traps, both silent:

  * `GetStart`/`GetDuration` count **timeline** frames, `GetSourceStartFrame`
    counts **source** frames. Mixed-rate media (25 fps clips in a 30 fps
    timeline) desyncs every cut if you divide by the wrong one.
  * `GetSourceStartTime` looks like the convenient accessor and is a trap: it
    returns the source *timecode* in seconds. A camera file stamped
    `19:10:09:05` reports **69009.2**, not an offset into the file.

That is enough to build a cut list and hand it to one ffmpeg filter graph —
one input per unique file, `atrim`/`adelay` per clip, `amix`. On the probe
timeline (17.7 min, 219 clips, 75 unique sources) it produced correct-length
audio in 14.7 s against the render's 21.5 s.

*And then it transcribed to 25 words instead of 1496.*

| build | words found |
|---|---|
| Resolve render | 1496 |
| all tracks summed, −14 dB | 25 |
| all tracks summed, −20 dB | 15 |
| **A1 only (dialogue track), −6 dB** | **1499** |
| A1 excluding music files | 1489 |

Summing the tracks at unity puts a music bed at full level over the dialogue.
Resolve's mix has that music ducked. **The API exposes no way to know that:**
`TimelineItem.GetProperty()` returns an *empty dict* for audio items — no clip
gain, no volume, no fades, no automation, at any level. The mixer is simply not
scriptable. Reconstructing the mix is not possible; only guessing at it is.

Even with the dialogue track picked by hand — which is already not "one
button" — it loses:

| audio path | time | WER vs Raz's reference |
|---|---|---|
| **Resolve render** | 21.5 s | **4.51%** |
| sources, A1 only | 14.5 s | 5.18% |
| sources, music files excluded | 24.7 s | 5.18% |

**6.5 seconds faster and 15% relatively worse**, with a failure mode that is
silent: pick the wrong track and the output is 25 words of nonsense rather than
an error. The two paths disagree on only 2.1% of words, and some of those
disagreements favour the sources — `הריפוד`, `הסרטוטימ`, `אלסטי` and `הקרג` all
come out right without the music masking them — but in aggregate the mix wins.

*Rejected. The render is not the bottleneck it was assumed to be.* Measured on
the same timeline: render 21.5 s, transcribe 28.1 s. The render is 40% of the
wait, not the whole of it.

---

*The render cannot be made cheaper, so the saving has to come from not doing
it.*

`SetRenderSettings` accepts `AudioBitDepth: 16` — 306 MB to 204 MB, a third off
for free. It does **not** make the render faster (22.5 s → 21.0 s): Resolve is
busy mixing, not writing. `AudioSampleRate: 16000` is rejected outright
(`accepted=False`), and `GetRenderCodecs("wav")` is empty, confirming that the
"Audio Only" preset is the only scripted route to audio.

So `prepare_audio` skips the render instead. `audio_cut_list()` reads the
timeline's audio structure through getters only — **0.14 s for 219 items** —
and `timeline_fingerprint()` hashes it. Matching fingerprint, reuse the WAV.

Verified live against Resolve Studio 21.0.3.7:

| | |
|---|---|
| cut list read | 0.14 s |
| first run | 20.7 s (rendered, 204 MB) |
| second run | **0.2 s (reused)** |
| `--fresh-audio` | 20.7 s (rendered) |
| render queue | 1 job before, 1 after |

**20.5 s saved on every re-run.** Which is most runs, in practice: changing a
setting and going again does not change the timeline.

*The blind spot, stated rather than buried.* The fingerprint covers what the
API exposes — clip positions, trims, source paths, source file size and mtime,
track enable state, frame rate. It cannot cover the Fairlight mixer, for the
same reason the source-rebuild failed. **Change only a volume automation curve
and the fingerprint will not move.** Mitigations: `prepare_audio` always prints
which path it took, and `--fresh-audio` forces a re-render. The exposure is
small — levels barely move a transcript, and word timings not at all — but it
is real, and a silent stale cache is exactly the kind of bug that gets blamed
on the ASR.

*Deliberately not included in the fingerprint:* clip names and the timeline
origin. Renaming a clip or moving the timeline's start timecode does not change
a single sample.

---

*Two things this turned up that are not fixed here.*

The panel still calls `render_timeline_audio` directly, so **it does not get
the cache** — `ui/app.py` duplicates `host_resolve.run` rather than calling it.
This is now the second finding that traces to that duplication (D41 was the
first: the panel writes no intermediates). Both are one `ui` task. Recorded in
`NOTES.md`.

`transcribe` is now the largest cost in the pipeline at 28.1 s against the
render's 21.5 s. `faster-whisper`'s batched pipeline is the obvious lead and
was not investigated.

---

**D43 — One pipeline. The panel calls it instead of re-implementing it.**

`ui/app.py._run_pipeline` was a second copy of `host_resolve.run`. Neither copy
was wrong on its own; they were just different, and the differences were
exactly the kind nobody reads a diff to find:

| | panel | CLI |
|---|---|---|
| proofread pass | yes | **no** |
| `01_raw` / `02_proofread` / `03_segmented` on disk | **no** | yes |
| audio cache (D42) | **no** | yes |

Both gaps were found by accident. The missing intermediates surfaced when
corpus 3 could not be evaluated without re-transcribing its audio from scratch
(D41). The missing cache surfaced immediately after D42 shipped, when the 20.5
seconds it saved turned out not to reach the one button Raz actually presses.

*The fix has two halves, and the first is the real one.* `host_resolve.run`
grew the proofread stage it never had, plus `passes`, `glossary_path` and
`strip_punct`, and now returns everything a caller could want — `srt`,
`status`, `words`, `edits`, `audio`. Only once it was genuinely the whole
pipeline could the panel call it. `_run_pipeline` is now nine lines: split the
settings, call `run`, return the result.

*The guard.* `test_the_panel_owns_no_pipeline` reads `ui/app.py` and fails if
it imports `transcribe`, `segment`, `export` or `proofread`. A comment saying
"do not duplicate this" would not have prevented the first duplication either.

*What the boundary now is.* The panel decides what the user asked for and
displays what came back. Between those two points it knows nothing. Two things
stay on the panel's side because they are presentation, not pipeline: stripping
the `host_resolve: ` prefix from log lines (the CLI needs it, a six-line panel
log does not), and the styling.

---

*A regression the live run caught, worth recording because the test suite could
not have.*

With the panel on the shared path, `run`'s habit of printing every warning
turned a three-line log into an eighty-line one. On a 15-minute timeline that
is ~75 warnings, and on a panel six lines tall it is the same as printing
nothing.

`_report_warnings` now prints one summary line, then **one example per code**
— not the first three, which on this material would have been three
`card_too_short`s — then a pointer to the segmented JSON, where all of them
live keyed by `wid`. The CLI reads better for it too.

*This is the second time the panel has been the thing that revealed a defect
in code that passed its tests.* Both times the mechanism was the same: the
tests check that a stage does its job, and say nothing about whether the output
is usable by a human with a small window.

---

*Verified live against Resolve Studio 21.0.3.7*, placement stubbed so nothing
was dropped into the open project:

```
timeline unchanged since the last run -- reusing hebsub_audio.wav, skipping the render
transcribing (ivrit_local)...          1496 words
proofreading (glossary)...             0 correction(s)
681 cards
wrote final.srt
warnings: card_too_short x53, cps_exceeded x9, hebrew_rule_violation x11
```

34.3 s end to end with the audio cache hit, against ~55 s cold. All four
artifacts on disk. The warning counts match Raz's own run of the panel before
any of this work, which is the continuity check that says the pipeline did not
quietly change while being rewired.

`ui` and `host_resolve` both have specs now (`docs/modules/ui.md`,
`docs/modules/host_resolve.md`) and tests — 37 between them, where there were
none. `CLAUDE.md`'s module table gains the two rows it was missing.

---

**D44 — A masked LM cannot proofread this transcript. Measured to 0.5%
precision, and not shipped.**

The plan was sound on paper and is worth writing down properly, because the
paper version will look attractive again in six months.

*The design.* Three stages, all local, no new dependencies: a lexicon built
from DictaBERT's own vocabulary (105,288 whole Hebrew words), a candidate
generator producing single-edit neighbours that are real words, and DictaBERT
scoring the survivors in context. Masking a position yields a distribution over
all 128k tokens in one forward pass, so forty candidates cost the same as five
— which is why the existing `llm` pass had been leaving so much on the table.

*Three measurements said go.*

Candidate reachability, on corpus 3's 44 one-for-one errors:

| generator | reaches the correct word |
|---|---|
| confusion sets (what the `llm` pass ships) | **9.1%** |
| any single edit | 59.1% |
| two edits | 81.8% |

That alone explained the `llm` pass's feeble +0.12pp: **its candidate generator
can only reach 9% of errors, so the model was never the binding constraint.**

Lexicon coverage of Raz's corrected words: **94.3%**, far better than the 50.7%
figure that had been quoted from D37 — that number described single-token
coverage under a different constraint and had been over-generalised.

And the reranker itself, handed the ASR's word and the truth: **it prefers the
truth on 21 of 23 scorable errors.**

*And then it lost at every single threshold.*

| generator | best WER against a 5.11% baseline |
|---|---|
| v1 — arbitrary single edits | 5.72% (+0.61) |
| v2 — morphology-constrained | 5.18% (+0.07) |

v1's damage had one signature: it stripped attached prefixes. `שהבורג` →
`הבורג`, `תעקבו` → `עקבו`, `שכשמו` → `כשמו`. Deleting a letter is a legal edit
and the bare stem is always the commoner word, so the model always agrees.

v2 rebuilt the generator on Hebrew morphology — phonetic confusion anywhere,
prefix letters *substituted* but never added or removed, ו/י added or dropped
for כתיב מלא/חסר, adjacent transposition, and no free insertion at all. Median
candidates per word fell from 36 to 4. It got much closer to break-even and
still never crossed it: **the optimum is to make no edits.**

*The number that ends it.* Of 820 proposals, **4 were right and 816 were
wrong — 0.5% precision, against the >50% needed merely to break even.**

```
בורג   -> אורג     was already right
מקדח   -> אקדח     was already right
פשוט   -> שפוט     was already right
רגיל   -> רגלי     was already right
```

*Why the 91% two-way result did not survive contact.* Preferring the truth over
the error is not the task. The task is preferring the truth over **every other
candidate**, on a word the model has no reason to think is wrong. With ~4
candidates and 1,467 words, something always outscores the correct word.

The base rate is what kills it, and it is worth stating as a rule rather than a
result: **at a 3% error rate, a correction pass must be right about which words
NOT to touch 97% of the time before its accuracy on the other 3% matters at
all.** DictaBERT is an encoder trained to predict plausible words. It is not
built to answer "is this word wrong", and no threshold converts one into the
other.

*Not shipped. Nothing in `src/` changed.* The existing default —
`passes=("glossary",)`, `llm` off — is now measured to be correct rather than
merely cautious.

---

*What this leaves.*

**Take the free win first.** All four correct proposals were orthographic, not
contextual: `הכול`→`הכל`, `שהכול`→`שהכל`, `זיוווד`→`זיווד`. Those are Raz's
consistent spelling convention, not ASR errors, and they need a mapping table
rather than a model. They belong in `glossary.txt` at zero risk.

**The 60% that need context still need a model that can be asked a question.**
DictaBERT cannot be; it is an encoder with no instruction following. That points
at DictaLM-2.0-instruct (7B) or a hosted model — a different class of tool, not
a better threshold on this one.

**A practical obstacle for the local route:** the installed `torch` is a
CPU-only build (`torch.cuda.is_available()` is False) even though the machine
has a 16 GB RTX 4060 Ti. The ASR does not care — faster-whisper reaches the GPU
through CTranslate2, not torch — but a 7B decoder on CPU is minutes per
timeline. Going local means installing a CUDA torch first.

---

**D45 — DictaLM-2.0-instruct probed and rejected. It rewrites instead of
correcting.**

D44 ended by naming the one local option left: a model that can be *asked a
question*, which an encoder cannot be. DictaLM-2.0-instruct is that model —
7B, Mistral-based, Hebrew-native, from the same lab as DictaBERT. Probed before
committing to it, on the two arms D44 proved were the right ones.

*Setup, which produced findings of its own.*

`torch` was a CPU-only build. `torch 2.12.1+cu130` (cp314) installs cleanly and
the driver (610.74) supports CUDA 13, so the GPU is now reachable from torch
for the first time. The ASR never needed it — faster-whisper reaches the card
through CTranslate2 — but anything transformers-based did.

**The 16 GB card is not 16 GB.** With Resolve and the desktop running, ~6 GB is
already gone. A 7B model at fp16 is ~14.5 GB and does not fit. The probe capped
the GPU at 8 GB and spilled the rest to system RAM.

*The result.*

| arm | result |
|---|---|
| **errors** — 20 sentences with a known ASR mistake | **fixed 0 of 20** |
| **control** — 20 sentences the ASR got entirely right | **left 7 of 20 alone** |
| speed | **~28 s per sentence** (574 s for 20) |

Zero fixes. Sixty-five percent of correct sentences rewritten. Any one of the
three numbers is disqualifying on its own.

*What it actually does.* The instruction said: correct only misrecognised
words, do not add, do not drop, do not reorder, return the sentence unchanged
if it is fine. It ignored all of it and behaved like a copy editor:

```
שלברגים של הקרג  ->  שלברגי הקרג            rewrote the construct
עדיף לפנות כדי שלא תשלמו  <-  reordered from "כדי שלא תשלמו... עדיף לפנות"
בטח שתעשו לבד  ->  בטח שתעשו זאת בעצמכם     substituted register
...מקרקש בתוך האוטו אז  ->  ...בתוך האוטו?  turned a clause into a question
```

Meanwhile it preserved the actual errors — `סמים` for `שומעים` survived
untouched inside a sentence it otherwise rewrote. On one it made things worse,
dropping a word: `בורג סקס` became `בורג רגיל`.

The model's Hebrew is not the problem. It punctuates correctly, renders
`בירץ'` with its geresh, and its rewrites are fluent. **It simply will not do a
constrained 1:1 substitution task when asked**, which is a known property of
small instruct-tuned models: they are trained to improve text, and "leave this
alone" is the one instruction that loses to that training.

No prompt is closing a gap of 0/20 and 65%. And at 28 s per sentence — a few
times faster with the whole model resident, but still tens of minutes per
timeline — it would be unusable even if it were accurate.

*Rejected. Nothing in `src/` changed.*

---

*Where the local route stands after D44 and D45.*

Two local models, from the same lab, at opposite ends of the design space:

| | DictaBERT (D44) | DictaLM-2.0 (D45) |
|---|---|---|
| what it is | masked encoder | instruct-tuned 7B |
| fixes errors | ranks truth over error 21/23 | 0/20 |
| leaves correct words alone | 0.5% precision | 35% |
| why it fails | cannot tell *wrong* from *less likely* | cannot be told to stop editing |

**They fail for opposite reasons and the failure is the same shape:** neither
can be made conservative. The task needs a model that will change three words
in fifteen hundred and leave the rest untouched, and that turns out to be a
much harder instruction to enforce than "correct this text".

What remains, in order of expected value:

1. **The deterministic wins.** `הכול`→`הכל`, `שהכול`→`שהכל` and friends are
   Raz's spelling convention, not ASR errors. A mapping table in
   `glossary.txt`, zero risk, no model.
2. **A hosted frontier model.** Untested, and the only remaining candidate.
   Instruction-following at that scale is qualitatively different — "return it
   unchanged if it is fine" is a request such models actually honour. It costs
   pennies per reel and sends transcript text off the machine, which is Raz's
   call to make.
3. **Accept 4.5% WER.** The pipeline is already 95.5% accurate on unseen
   domain content, and two serious attempts have now failed to beat it. That
   is a legitimate answer, not a surrender.

---

**D46 — A lexicon detector works, modestly. First positive result on
proofreading, and small enough to say so plainly.**

D44 and D45 both failed by trying to *decide*. This asks a smaller question:
not "what should this word be" but "is this a Hebrew word at all". Raz raised
it himself, from `תתעכם` and `יתעלש` — neither is a word, and nothing in the
pipeline noticed.

*Licensing, corrected.* An earlier turn treated the GPL Hebrew dictionary as
blocked. That was wrong and worth recording so the mistake is not repeated:
**copyleft obligations trigger on distribution, not on use.** Running hspell on
your own machine for your own client work creates no obligation whatsoever. It
matters only if hebsub is ever given to other people — and the dictionary is
in fact **AGPL v3**, which is stricter still, so that day it needs either a
permissive replacement or a fetch-on-first-run arrangement.

*The lexicons compared,* on all three corpora, as detectors:

| lexicon | entries | caught | false alarms | precision | recall |
|---|---|---|---|---|---|
| DictaBERT tokenizer vocab | 105,288 | 35/129 | 138 | 20.2% | 27.1% |
| DictaBERT + prefix strip | 105,288 | 23/129 | 73 | 24.0% | 17.8% |
| hspell, raw | 469,367 | 67/129 | **1,227** | 5.2% | 51.9% |
| **hspell + prefix strip** | 469,367 | 19/129 | **44** | **30.2%** | 14.7% |

Two things in that table are worth keeping.

**A tokenizer vocabulary is not a dictionary.** DictaBERT's 105k entries are
common surface forms, so ordinary inflected Hebrew — `תעקבו`, `מקדח`,
`למגירות`, `שהבורג` — reads as "not a word". hspell's 469k expanded forms
contain them.

**Prefix stripping is not optional, and it is not free.** hspell without it
produces 1,227 false alarms, because Hebrew fuses ב/כ/ל/מ/ש/ה/ו onto the front
and no flat list enumerates the combinations. Adding it cuts false alarms 28x —
and also halves recall, because the same rule rescues genuine errors
(`הקדחים` parses as ה+קדחים and stops looking wrong). Net precision 24% → 30%.

---

*Harvesting Raz's own corrections does almost nothing — measured held-out.*

The first run of this reported **100% precision** for "hspell + Raz's words".
That was leakage: the lexicon was built from the same references it was scored
against. Rebuilt leave-one-corpus-out — lexicon from the other two, tested on
the held-out one:

| held out | hspell | hspell + harvested |
|---|---|---|
| corpus 1 (Judaica) | 30.0% | 37.5% |
| corpus 2 (business) | 15.8% | 15.8% |
| corpus 3 (workshop) | 38.2% | 39.4% |
| **pooled** | **30.2%** | **31.7%** |

**Domain vocabulary does not transfer between domains.** Judaica words do not
help the workshop reel. The "it compounds every time you work" pitch is only
true *within* a domain, and this eval — three unrelated domains — is the
pessimistic case for it. Whether it compounds usefully over months in one
domain is not measurable with what exists today, and should not be claimed.

---

*What it is actually worth, as a product:*

| | |
|---|---|
| words flagged per reel | **3.0** |
| of those, genuinely wrong | **32%** |
| errors it can never flag | **110 of 129** |

Three words a reel, one of which is real. That is a modest, honest win, and it
is the first positive result on proofreading after two negatives.

The 110 it misses are the same wall as always: real Hebrew words in the wrong
place — `סמים` for `שומעים`, `הריקוד` for `הריפוד`. Nothing is misspelled, so
no dictionary can see them.

*The shape that makes it safe.* **Flag, do not fix.** The detector's precision
is nowhere near the >50% that auto-correction needs (D44's arithmetic), but a
review list has no such threshold — it cannot damage a transcript because it
never changes one. It converts an invisible 4.5% WER into a three-item
checklist, and it catches the two words that prompted the whole investigation.

Auto-correction stays out. If precision ever reaches the fifties on a larger
eval set, it becomes a separate decision with numbers behind it.

---

**D47 — Two models disagreeing is the best error signal yet. The "full model is
better" claim was wrong.**

D44, D45 and D46 all failed the same way: they read the text and guessed. None
had access to the audio, and `סמים` vs `שומעים` is not decidable from text —
both are real Hebrew words and both fit the sentence. So this asks the audio
instead, by running a second ASR model and looking at where the two differ.

*The claim that did not survive validation, stated first.*

On corpus 3 the full 32-layer `ivrit-ai/whisper-large-v3-ct2` beat the
4-layer turbo we ship, 4.78% against 5.11%, and that was reported as "we have
been shipping the slow-lane model". **On all three corpora it is the reverse:**

| corpus | turbo (ships) | full |
|---|---|---|
| 1 (Judaica) | **4.57%** | 5.47% |
| 2 (business) | **4.79%** | 6.29% |
| 3 (workshop) | 5.11% | **4.78%** |
| **pooled** | **4.83%** | **5.50%** |

Turbo wins by 0.67 pp. ivrit.ai's turbo checkpoint is simply the better-tuned
of the two, and distillation is not costing what it was assumed to cost.
Corpus 3 was the exception and got reported as the rule — the third time this
session that a single-corpus number has been wrong, and the second time it was
announced before validation. **The default model does not change.**

*What did survive, and it is worth something.*

Used as a detector, "the two models disagree" is far better than anything
tried before:

| corpus | flags | precision | recall |
|---|---|---|---|
| 1 (Judaica) | 49 | 38.8% | 38.8% |
| 2 (business) | 26 | 38.5% | 25.6% |
| 3 (workshop) | 32 | **71.9%** | 52.3% |
| **pooled** | **107** | **48.6%** | **39.4%** |

Against the alternatives on the same corpora: DictaBERT rescoring 0.5% (D44),
lexicon detector 31.7% (D46), **model disagreement 48.6%**. On corpus 3, where
the two models are closest in quality, a disagreed word is **49x** more likely
to be wrong than an agreed one.

48.6% sits just under the >50% that D44 established as break-even for
auto-correction, so it does not license changing words on its own. It is
comfortably good enough for a review list, at roughly five flagged words per
reel against the lexicon's three, and half of them real instead of a third.

*And the finding that generalises cleanly — the reason to keep going.*

| corpus | turbo | perfect tiebreaker |
|---|---|---|
| 1 (Judaica) | 4.57% | **3.67%** |
| 2 (business) | 4.79% | **4.29%** |
| 3 (workshop) | 5.11% | **4.10%** |
| **pooled** | **4.83%** | **4.02%** |

**Every corpus improves, by 0.5–1.0 pp — 17% relative, pooled.** That is the
prize, and unlike the model swap it is consistent across three domains.

*But it has to be earned.* Blindly taking the full model's word on every
disagreement gives 5.26% — worse than doing nothing, because full is the weaker
model overall. The gain only exists if the right one is picked each time. On
corpus 3's 32 disagreements: full right 15, turbo right 9, **both wrong 8**.
Roughly a quarter are unwinnable by any tiebreaker.

*Why the remaining problem is tractable where the earlier ones were not.* D44
had to judge ~4 candidates across 1,400 words with no evidence. This is **107
binary choices across three corpora**, with word-level timings already on every
word — so each one can be settled by cutting that exact span of audio and
asking which spelling the sound supports. That is the acoustic rescoring the
text-only attempts never had, and it is a question an ASR model can actually
answer.

Next, in order: try `yi-whisper-large-v3-turbo-ct2` as the second voice (a
closer-matched pair should raise precision toward corpus 3's 71.9%), then build
the acoustic tiebreaker against the 4.02% ceiling.

---

**D48 — Eight Hebrew ASR models benchmarked. Nothing beats the default; the
ensemble ceiling is real; every cheap way to reach it fails.**

Raz asked whether a better model, or a deliberately mismatched one, would beat
the turbo/full pairing of D47. Every drop-in CTranslate2 Hebrew model that
exists was benchmarked on all three corpora, then every ordered pair.

*Single-model accuracy — the default survives.*

| model | corpus 1 | corpus 2 | corpus 3 | pooled |
|---|---|---|---|---|
| **turbo (default)** | 4.57% | 4.50% | 5.11% | **4.73%** |
| large-v3 | 5.47% | 6.29% | 4.78% | 5.50% |
| turbo-rc0 | 7.68% | 6.00% | 5.25% | 6.30% |
| v2-d4 (2024) | 9.55% | 12.86% | 7.81% | 10.02% |
| openai-v3 (base) | 10.66% | 13.64% | 7.27% | 10.46% |
| sivan22-v2 | 10.38% | 23.71% | 6.73% | 13.44% |
| yi-large | 66.78% | 80.00% | 75.91% | 74.19% |
| yi-turbo | 91.21% | 97.43% | 99.53% | 96.07% |

**`ivrit-ai/whisper-large-v3-turbo-ct2` stays.** No alternative comes close,
and the gap to the base OpenAI model (4.73% vs 10.46%) is the clearest evidence
yet that ivrit.ai's fine-tuning is doing most of the work in this pipeline.

*On the yi models, recorded so nobody repeats it:* **`yi` is the ISO 639-1 code
for Yiddish.** They are ivrit.ai's Yiddish models, shortlisted here because the
name was read as a version tag. The output is unmistakable — `אז מתי און מה די
פון`, `פארשט`, `וואסער`. Not broken, just a different language.

---

*The mismatch hypothesis is confirmed — for the ceiling, not for precision.*

Best oracle ceilings, turbo supplying the transcript and the partner only
voting:

| partner | flags | precision | oracle WER |
|---|---|---|---|
| **v2-d4 (2024)** | 179 | 29.1% | **3.83%** |
| sivan22-v2 | 181 | 32.6% | 3.83% |
| large-v3 (sibling) | 107 | **48.6%** | 3.93% |
| openai-v3 (base) | 220 | 27.7% | 4.04% |

**The 2024 model — 10.02% WER on its own, a full generation old — makes the
best partner.** Exactly Raz's intuition: a sibling model trained on the same
data makes the *same* mistakes and stays quiet where both are wrong. A
decorrelated model speaks up there.

The trade is precision. The sibling flags 107 words at 48.6%; the old model
flags 179 at 29.1%. For a review list the sibling is better. For a ceiling the
old model is.

*And more voices keep helping:*

| partners | flags | oracle |
|---|---|---|
| none | 0 | 4.73% |
| v2-d4 | 186 | 3.83% |
| v2-d4, sivan22 | 287 | 3.35% |
| + openai-v3 | 384 | 3.19% |
| all five | 455 | **3.05%** |

**4.73% → 3.05% is a 36% relative reduction**, and it is available from models
already on disk.

---

*Majority voting cannot collect any of it.*

The obvious way to cash a five-model ensemble is to let them outvote the base.
Measured:

| partners | threshold | changed | right | WER |
|---|---|---|---|---|
| 4 partners | 2 votes | 124 | 46 | 5.24% |
| 4 partners | **3 votes** | 41 | 19 | **4.71%** |
| 4 partners | 4 votes | 10 | 3 | 4.80% |
| 5 partners | 3 votes | 70 | 28 | 4.92% |

Best case 4.71% against 4.73% — noise. At the only setting that does not lose,
41 changes yield 19 correct: **46%, just under break-even, which is D44's
arithmetic arriving for the fourth time.**

The reason is structural. Every partner is a *weaker* model. When they agree
against turbo they are agreeing on a plausible-sounding error as often as on
the truth. Counting votes among models that are individually worse cannot
recover which one is right.

---

*What this leaves, stated precisely.*

There is **1.7 pp of measured, generalising headroom** sitting in the
disagreements — the largest quantified opportunity in the project. Four
mechanisms have now failed to reach it: masked-LM rescoring (D44), instruct-LLM
rewriting (D45), lexicon detection (D46), and majority voting (D48).

All four share one property: **they judge the text without hearing the audio.**
`סמים` versus `שומעים` is not decidable from text, and no amount of voting
among text hypotheses changes that.

The one source of evidence never used is the recording. Every word carries a
timestamp, so each flagged position can be cut from the audio and each
candidate spelling scored against the sound it is supposed to represent —
which CTranslate2 exposes directly via `score_batch`.

That is the remaining move, and it should be probed on twenty cases before
anything is built. Four ideas have died this session and the ones that died
loudest were the ones that looked best on paper.

*For a review list today,* if one ships before any of that: turbo + large-v3,
107 flags across 20 reels at 48.6% precision. Roughly five words a reel, half
of them genuinely wrong.

---

**D49 — A hosted frontier model proofreads this transcript. First method that
works. ONE CORPUS ONLY — not yet validated.**

Five local methods failed (D44–D48). All five failed at the same point: they
could not be made conservative. The hosted route was the one option never
tested, and the hypothesis was specific — **instruction-following at frontier
scale is qualitatively different, and "leave it alone if it is fine" is a
request such a model actually honours.**

*The control arm settles that.* Same 20 error sentences and 20 control
sentences as the DictaLM probe (D45), so the numbers are directly comparable:

| method | correct sentences left untouched |
|---|---|
| DictaBERT (D44) | 0.5% precision |
| DictaLM-2.0-instruct (D45) | 7 / 20 |
| lexicon detector (D46) | 32% precision |
| majority vote (D48) | 46% of changes right |
| **`claude-opus-5`** | **20 / 20** |

The exact failure that killed DictaLM — rewriting 13 of 20 perfectly good
sentences into fluent copy-edits — does not occur.

*And the measurement that decides it,* whole transcript in 180-word chunks,
scored against Raz's corrected file, corpus 3:

| | |
|---|---|
| WER before | 5.11% |
| **WER after** | **3.30%** |
| | **−1.82 pp, 35.5% relative** |
| edits | **24 right, 12 wrong — 67% precision** |
| cost | **$0.30 (~₪1.1)** per 15-minute reel |
| time | 125 s |

67% clears the >50% break-even that D44 established and that four subsequent
methods failed. It also beats the two-model *oracle* ceiling of 4.02% (D47),
which was a theoretical best case — this is real output.

It fixes the words that started the investigation: `תתעכם`→`תתעקם`,
`יתעלש`→`ייתלש`, and `סמים`→`שמים`, the last of which no text-only local
method could reach.

---

*Two problems, one of them mine.*

**Five of the twelve bad edits were the prompt over-firing.** The system prompt
said English terms go in Latin script; the model applied it through Hebrew
prefixes — `הפוקט`→`הPocket`, `הקרג`→`הKreg`, `הול`→`Hole`. The convention
needs to name specific standalone terms rather than state a general rule. The
genuinely wrong edits are fewer: `תעקבו`→`תעברו`, `דוך`→`דרך`,
`דיוקים`→`ליקויים`.

**Word count drifted, 1497 → 1490.** The contract forbids this absolutely:
every stage after `transcribe` must preserve word count because timestamps are
keyed to `wid`, and seven lost words would desync every card after them. The
guard is the one D45 designed and never got to use — **if a chunk returns a
different word count, discard it and keep the original chunk.** Chunking at 180
words means one bad chunk costs a fraction of a reel, not the reel.

---

*Status: NOT VALIDATED. Nothing recommended, nothing built.*

This is corpus 3 alone. Twice in the same session a single-corpus result has
been announced and then retracted — the full-model claim in D47 most recently.
Validation on corpora 1 and 2 was started and **blocked on an exhausted API
credit balance**, roughly $0.60 short. The runner caches per clip and resumes.

Until those numbers exist, the honest summary is: *the first method that has
produced a real WER reduction on any corpus, on a task where five others
produced none.*

*Operational notes worth keeping:*

The key lives in `.env` (gitignored, `.gitignore:27`). It was first written as
**UTF-16LE** — 254 bytes for a 127-character line — and a plain
`read_text(encoding="utf-8")` turned it into unmatchable garbage rather than
raising, so the loader reported "no key" while the key sat right there. Any
`.env` reader in this project must try UTF-16 before giving up. This is exactly
the class of Windows encoding bug CLAUDE.md's UTF-8 rule exists to prevent.

Raw HTTP via `requests`, not the Anthropic SDK: CLAUDE.md refuses
vendor-specific LLM SDKs for this module, and the llm pass is a swappable
adapter over `requests`. The probe deliberately exercises the path that would
ship.

Cost, measured rather than guessed: **$0.30 per 15-minute reel** whole-transcript.
Sentence-by-sentence cost $0.42 for 40 short sentences — roughly ₪3 a reel — so
chunk size is a cost lever as well as a context lever.

---

**D50 — D49 validated on all three corpora. And the obvious guard was the wrong
guard.**

D49 recorded a 5.11% → 3.30% result on corpus 3 and refused to recommend
anything, because twice in the same session a single-corpus number had been
announced and retracted. Corpora 1 and 2 now agree.

| corpus | before | after (ungated) |
|---|---|---|
| 1 (Judaica) | 4.57% | **3.32%** |
| 2 (business) | 4.79% | **4.14%** |
| 3 (workshop) | 5.11% | **3.30%** |
| **pooled 1+2** | **4.67%** | **3.73%** |

**Every corpus improves.** After five methods that did not (D44–D48), this is
the first that generalises. Pooled across all three: **4.83% → 3.58%, a 26%
relative reduction.**

Measured cost: **$0.56 for 2,845 words**, i.e. **~$0.20 per 1,000 words** —
about **$0.30 (₪1.1)** for a 15-minute timeline, or **3 cents** for a 40-second
reel.

---

*The guard finding, which matters more than it sounds.*

The ungated numbers above **violate the contract**. Word count moved (1497 →
1490 on corpus 3), and every stage after `transcribe` must preserve it because
timestamps key on `wid` — seven dropped words desync every card after them.

The guard D45 designed was: *if a chunk comes back with a different word count,
discard it and keep the original*. It is safe, obvious, and **throws away most
of the value**:

| guard | pooled WER | relative gain |
|---|---|---|
| none (contract-violating) | 3.73% | 20.3% |
| **discard the chunk** | 4.29% | **8.3%** |
| **align and substitute** | **4.04%** | **13.5%** |

Discarding a 180-word chunk because one word shifted destroys every correct fix
in that chunk alongside the one that broke it.

**The contract requires that the word count is preserved — not that the chunk
comes back verbatim.** So align the model's output against the input and accept
only the 1:1 substitutions, dropping its insertions and deletions. Word count is
preserved *by construction*, and the good edits inside a length-changing chunk
survive. That is 13.5% against 8.3% — the guard choice is worth more than half
the pass.

The residual gap to ungated (13.5% vs 20.3%) is real and structural: some
genuine fixes *require* changing the word count — joining a split word is the
obvious case, and `transcribe` already owns that repair (D39, D41). Those
belong upstream, not here.

---

*What this does not fix.* Chunk boundaries at 180 words are arbitrary; aligning
the whole clip at once is equivalent and simpler, which is what the measurement
above does. Corpus 2 gains least (4.79% → 4.36% aligned) and is worth a look
before tuning anything else.

*Prompt bug carried over from D49, not yet fixed:* the Latin-script convention
over-fires through Hebrew prefixes (`הפוקט` → `הPocket`, `הקרג` → `הKreg`).
Naming specific standalone terms instead of stating a general rule should
recover several points of precision, and has not been tried.

*Status: validated, still not built.* `src/` is unchanged. The pass belongs in
`proofread` as a new adapter under `src/hebsub/llm/`, opt-in like the existing
`llm` pass, with the alignment guard applied in `proofread` where the other
guards already live.

---

**D51 — The hosted proofread pass is declined. v2 stands.**

D50 validated it: 4.83% → 3.58% pooled across three corpora, 13.5% relative
with a contract-safe guard, ~₪1 for a 15-minute timeline. It works, and it is
the only proofreading method in D44–D50 that does.

Raz's call is not to build it, and the reasoning is worth recording because it
is not about the numbers:

**It would change what the tool is.** Today `hebsub` runs offline, forever,
with no account, no key, no bill and no network. The pass would trade that for
roughly one point of WER. At ₪1 a reel the money is irrelevant; the dependency
is not.

*So v2 is the shipping version.* `src/` is byte-identical to the `v2-working`
tag and has been through every investigation in D44–D51 — five methods measured
and rejected, one measured and declined. Nothing was built on a result that did
not survive validation, which is the point of the whole sequence.

*If this is ever revisited,* everything needed is in D49 and D50: the prompt,
the chunk size, the measured cost, the two guard designs and why alignment beats
discarding, and the one known prompt bug (the Latin-script rule over-firing
through Hebrew prefixes). It is a couple of hours of work from a standing start.

*The eval assets survive regardless* and are the durable output of the day:
three hand-corrected corpora, eight benchmarked ASR models, and a decision log
that says which roads are closed and why.

---

**D52 — The review list ships. Flag, never fix.**

D51 declined the hosted proofreader and left the best *offline* signal on the
table. This builds it.

`proofread.review_disagreements(primary, alternative)` returns the words two
ASR models transcribed differently. `host_resolve --review` runs the second
model and writes `review.json`; the panel exposes it as a **Second opinion**
checkbox, off by default.

*Verified against the benchmark rather than assumed.* Run over the same 20
reels D48 measured, the shipped function reproduces the number exactly:

| | |
|---|---|
| words flagged | 107 — **5.3 per reel** |
| genuinely wrong | 52 |
| **precision** | **48.6%** (D48: 48.6%) |

Live on Raz's 15-minute workshop timeline: **33 flags, 137 s end to end**,
catching `תתעכם`, `יתעלש`, `שמים`/`תמיד`, `תשרוט`/`ישרוט` — including the two
non-words that prompted the entire proofreading investigation.

*Three decisions worth keeping.*

**No contract change was needed, and none was made.** The warning enum is
frozen at eight codes and none fits. But `gap_not_applied` already lives in a
sidecar report rather than `meta.warnings`, which is the precedent: a review
list is information for a human, not for a downstream stage, so it is an
artifact rather than schema.

**Only 1:1 substitutions are reported.** Insertions and deletions are
disagreements too, but a different phenomenon with different odds, and they
were never measured. Shipping them would mean quoting a precision figure
nobody has. A test pins this.

**The partner model is deliberately the worse one.** `large-v3` scores 5.50%
against turbo's 4.73%. It is not there to be better, it is there to be
*independently* wrong — and `test_the_review_checkbox_selects_the_measured_pairing`
fails if anyone later swaps it for a stronger sibling, which would look like an
upgrade and would quietly destroy the signal.

*What it is honestly worth.* About five words a reel, half of them real. It
does not move WER at all — it moves nothing. What it changes is that an
invisible ~5% error rate becomes a short list with timecodes you can type
straight into Resolve. After six methods that tried to fix the transcript and
five that failed, the thing that shipped is the one that only points.

476 tests green.

---

**D53 — The timing clip: a card at frame zero so the drop is exact.**

Raz: *"I have a hard time putting the subtitles at the exact frame."*

The cause is D28. Resolve exposes no scripted way to position a subtitle clip,
so `place_srt` imports to the media pool and the user drags it onto a track.
What you have to align is the clip's **content**, and there is nothing in front
of the first card to align against. On his workshop timeline the first spoken
word is at **00:02:44,660** — nearly three minutes of programme before the
first subtitle, and no landmark anywhere in it.

His fix, and it is the right one: **a placeholder card from `00:00:00,000` to
the first real card.** The clip then begins where the programme begins, so
"snap to the start of the timeline" is exact. He deletes the card afterwards.

Implemented in `export` as `timing_clip`, on by default in `host_resolve` and
off in `export`'s own CLI — a `.srt` going anywhere other than a Resolve
timeline should not carry a "delete me" card. `.vtt` never gets one.

*Details that are decisions, not taste:*

**ASCII text** (`>> TIMING CLIP - DELETE ME <<`). It sits in an RTL subtitle
track. Hebrew could be mistaken for content, and mixed scripts invite bidi
reordering — the exact class of problem D29 already cost a day on.

**Skipped when the first card starts at zero.** No room, and a zero-length card
is not a card. The report records whether one was actually written rather than
whether it was asked for.

**Real timings and the report's card count are untouched.** The placeholder is
a render-time concern only; it never enters the SubtitleFile, so `segment`'s
invariant that it only ever groups existing words is not touched, and no card
without words ever exists in the contract.

*Verified against Resolve Studio 21.0.3.7 — and it is a bug fix, not a
usability fix.* The same `.srt` imported with and without the card:

| file | Start TC | Duration | End TC |
|---|---|---|---|
| plain | **00:02:44:20** | 00:14:58:07 | 00:17:42:27 |
| with timing card | **00:00:00:00** | 00:17:42:27 | 00:17:42:27 |

**Resolve drops the lead-in.** The imported clip's first frame is the first
subtitle, not the first frame of the programme, and the two durations differ by
exactly 00:02:44:20 — the missing silence. Dragging the plain clip to the start
of the timeline therefore lands every card **2m44s early**, which is precisely
the symptom Raz reported and could not pin down.

With the card, the clip's first frame is the programme's first frame and
`Start TC` is `00:00:00:00`, so snapping to the timeline start is exact rather
than approximate.

*Worth stating plainly:* this had been silently mis-timing every drag-in
placement since D28, and no test could have caught it — the `.srt` was always
correct. The defect lived entirely in what Resolve does with a correct file.

490 tests green.

---

**D54 — Cards touch unless there is a real pause. The threshold came from the
references, not from taste.**

Raz: *"I want the clips to not have dead spaces between them... however, when
there is real noticeable speech gap, I want it exactly with the gap."* And,
correctly: *"you can even measure the time of the small gaps to assess at what
length of silence should be left a gap."*

*The measurement settles it immediately.* Across all three corrected corpora,
1,882 gaps between consecutive cards:

| gap | his cards |
|---|---|
| **touching (≤0ms)** | **96.4%** |
| 1–60ms | **0.0% — not a single one** |
| 61–200ms | 0.4% |
| >200ms | 3.2% |

**The distribution has a void in it**, which is what makes this a threshold
rather than a judgement call. He never leaves a gap below 60ms; above 200ms
there is a genuine population of speech pauses.

Ours had **32% of gaps in the 1–200ms band** — precisely the band he never
produces. Those are word-timing artifacts, not silence a viewer could perceive.

`DEFAULT_CLOSE_GAPS_MS = 200`: closes them, costs at most 9 of his 1,882 gaps.
300ms would close 33 more while tripling that cost, so 200 is the knee.

| | gaps | touching |
|---|---|---|
| Raz | 1,882 | **96.4%** |
| ours before | 2,025 | 63.8% |
| **ours after** | 2,025 | **95.9%** |

*Why this can live in `export` at all.* CLAUDE.md rule 2 makes timestamps
immutable after transcribe, and extending a card's end looks like a violation.
It is not: `export` writes a display file, not a Transcript, and
`_displayed_ends` has always adjusted displayed ends — `gap_ms` shaves them for
the broadcast convention. This is the mirror of that, in the same function,
under the same justification. The SubtitleFile on disk is untouched, and a test
asserts it.

*Refused rather than resolved:* `gap_ms` and `close_gaps_ms` together raise.
One opens a gap, the other closes it; silently picking a winner would be worse
than saying no.

504 tests green.

---

**D55 — The "duplicated words" are not ours. Measured on the live timeline.**

Raz reported that every video after the first opens with a duplicated word:
`אז מה, אז מה ההבדל`. He exported the placed subtitles so it could be looked
at directly, which is what settled it.

*What the exported file shows.* 734 cards against our 689, and **47 adjacent
duplicate pairs**. Every duplicate is a card that in our file is preceded by a
gap wider than the close-gaps threshold — **46 of 46**, exactly.

*But our file is clean.* `final.srt` from the same run: 689 cards, **one**
adjacent repeat (`גם כאן`), which is a genuine ASR repetition, not a pipeline
fault. The raw transcript agrees — 1,496 words, two repeated runs, neither at
a segment boundary.

*And Resolve does not split gapped subtitles.* Probed: an .srt with three
cards and two 8-second gaps imports as **one** clip, same as a contiguous one.
The first hypothesis — one clip per contiguous run — is wrong.

*What the timeline actually contains:*

| track | items | adjacent repeats |
|---|---|---|
| Subtitle 1 | 611 | 1 |
| Subtitle 2 | 688 | 1 |
| Subtitle 3 | 688 | 1 |
| **Subtitle 4** | **734** | **47** |

**Four stacked subtitle tracks**, three of them clean placements of our output
and one — the track he exported — carrying 46 extra cards. Subtitle 1 is his
own 611-card corrected reference; 2 and 3 are our runs, each with the single
genuine repeat and nothing else.

**The same file placed twice on one track, a few frames apart, explains it
exactly.** A subtitle drop overwrites what it lands on, so the second drop
replaces the first everywhere the cards touch — and survives *next to* the
first only where there was a gap with nothing to overwrite. Survivors = gaps =
46. It also explains why the export is uniformly shifted 173 ms from ours, and
why he sees it "at the start of every video besides the first one": those are
the gaps.

*Nothing was changed in response.* The output is correct; the timeline had
accumulated placements. Worth recording because the symptom looked exactly
like a transcription bug, and two plausible pipeline explanations — Whisper
repeating on long silence, Resolve splitting at gaps — were both wrong and
both cheap to disprove.

---

**D56 — The clip is called `HebSub Subtitles` and lands in the master bin.**

`final` is a poor thing to hunt for in a media pool with a hundred clips, and
importing into whatever bin happens to be selected buries it.

`place_srt` now sets the master bin before importing and restores the user's
selection afterwards — their selection is part of their workspace.

*Resolve refuses to rename a subtitle clip.* `SetClipProperty("Clip Name")`,
`"File Name"` and every variant tried return `False`, and the clip stays
`final`. The pool name follows the **file** name, so `place_srt` imports a copy
called `HebSub Subtitles.srt`. `final.srt` stays exactly where CLAUDE.md rule 6
requires it; the named copy sits beside it for Resolve to display.

Verified live: clip lands in the master bin as `HebSub Subtitles`, with the
user's bin selection unchanged.

504 tests green.

---

**D57 — Cards no longer appear before their picture. Two biases, one fixed.**

Raz: *"the subtitles are starting a few frames before the actual speech...
before the video even starts."* He suspected the timing clip's length.

It is not the timing clip — that is a single card at the head of the file, and
a wrong length would shift everything equally. Measuring separated two effects:

| | median | frames @30 |
|---|---|---|
| **every card**, vs his corrected files | **−34 ms** | −1.02 |
| **first card of each video** | **−173 ms** | −5.2 (max −13.8) |

The −34 ms is remarkably stable: −34, −34, −33 across the three corpora, with
77.4% of our cards starting earlier than his. That is Whisper's word onsets
running about one frame early, everywhere.

The second is what he can see. At a speech onset after silence the model takes
the **breath before the word**, and on a timeline of separate videos that puts
the card in the black gap. Measured on his timeline: **11 cards started before
their video existed.**

*The fix uses information the host already has.* `host_resolve.picture_spans`
reads the video tracks — read-only, the same shape as `audio_cut_list` — and
`export` pulls a displayed start forward to the edge of the next picture when
it lands in a hole. Guards: a card already over a picture is untouched, a card
that *ends* before the next picture is left alone (it belongs to nothing, and
moving its start past its own end would invert it), and ends never move.

**The timing clip now ends at the displayed first start**, not the raw one.
Without that the placeholder and the first card would disagree by exactly the
amount the clamp moved, and the whole point of D53 is that they agree.

Result: **11 → 0**. The residual four hits sit exactly on a boundary,
sub-millisecond, within the same frame.

*Deliberately not fixed:* the −34 ms global bias. It is uniform, it is
Whisper's, and shifting every card by a frame is a separate decision with its
own risk — 23% of cards already start *later* than Raz's, so a blanket shift
would make those worse. D38 first raised it and it remains open.

513 tests green.

---

**D58 — The clip name carries a version, because Resolve will not tell you
which is which.**

Raz re-ran before deleting the previous clip and ended up with two clips both
called `HebSub Subtitles`. The media pool list shows no import time, so there
is no way to tell the new one from the old — and placing the wrong one is
silent, because both look right.

`place_srt` now reads the master bin and picks the next free name:
`HebSub Subtitles`, `HebSub Subtitles V2`, `V3`, and so on. **The base name
counts as V1**, so the highest number visible is always the newest.

*Details that matter:*

It takes the **highest** existing version, not the count — deleting the middle
ones must not hand out a name already in use.

Matching is case- and whitespace-insensitive, so a name Resolve has normalised
still counts.

`next_clip_name` is a pure function over a list of names, so the whole rule is
tested without Resolve; `place_srt` supplies the real ones. The file copy takes
the same name, since Resolve refuses to rename a subtitle clip (D56) and the
pool name follows the file.

The status string now carries the chosen name back (`pool:<name>`) so the CLI
and the panel both tell the user exactly what to drag, rather than a name that
might be a version behind.

523 tests green.

---

**D59 — The second model would not download. Windows symlinks, and a bug that
only I could not see.**

The Second opinion checkbox failed on Raz's machine with

```
[WinError 1314] A required privilege is not held by the client
```

`huggingface_hub` builds its cache by symlinking `snapshots/` entries at
`blobs/`, and **creating a symlink on Windows needs Developer Mode or an
elevated process**. Neither is reasonable to require of a video editor.

*Why it worked for me and not for him.* I hit this the first time the model was
downloaded and worked around it in my shell with
`HF_HUB_DISABLE_SYMLINKS=1` — then never put it in the shipped code. Every
subsequent run of mine inherited the env var from the shell. The panel does
not. **A workaround applied in the harness and not in the product is invisible
until a user runs it**, which is the second time this session the panel found
something the tests structurally could not (D43, D57).

*Why a retry would not have fixed it.* The failed download left a snapshot
holding `model.bin`, `config.json`, `tokenizer.json` and `vocabulary.json` but
**not** `preprocessor_config.json`. The model looks present and fails
identically every time.

*The fix.* `disable_hf_symlinks()` in the engine, beside `bootstrap_cuda_dlls()`
— both are Windows-specific things that must happen before a library is
imported. The flag is read into `huggingface_hub`'s module constants at import
time, so setting it afterwards does nothing; that is why `faster_whisper` is
imported inside `_load` rather than at module scope, and the docstring says so
before someone tidies it upward.

The error message now names the folder to delete when a half-written cache is
already there, since the flag alone will not repair one.

*Verified with the env var explicitly unset:* the model loads in 11s on CUDA,
and the full second-opinion run completes in 102s — 688 cards, 33 words
flagged, clip named `HebSub Subtitles V2`.

523 tests green.

---

**D60 — Question marks come back. Onsets clamp to speech, not just to picture.**

*Punctuation, decided by counting his files.* Across all three corrected
corpora, 4,331 words:

| mark | count |
|---|---|
| **`?`** | **29** |
| geresh | 13 |
| gershayim | 13 |
| `,` | 3 |
| `.` | 2 |
| `!` | 1 |

The question mark is the only sentence punctuation Raz uses. D35 stripped all
of it on the strength of "12 marks across 1445 words" — true in aggregate and
wrong in the particular, because it treated 29 question marks and 5 commas as
one phenomenon. A question that reads as a statement is a different sentence.
`?` is now kept; everything else still goes. `!` stays out on its single
occurrence, and is one character to restore.

*Onsets.* D57 clamped card starts to the picture. Raz then reported the
remaining case: cards starting while the video runs but nobody is talking yet.

**The VAD's `speech_pad_ms` defaults to 400ms** — it hands the model room tone
before every utterance and the word onset lands somewhere inside it. Re-running
the VAD with zero padding and `min_silence_duration_ms=200` (matching the gap
threshold export already uses) gives tight speech boundaries, and
`onset_spans` is now the intersection of picture and speech. ~30 cards move on
his timeline, the largest by 628ms.

*An ordering bug found while doing it.* `_displayed_ends` closed gaps against
the **raw** next start. Any card the clamp moved would have re-opened its gap
by exactly that much — silently undoing D54 for precisely the cards D57 and
this change were fixing. Starts are now computed first and gaps close onto the
displayed start, with a test pinning it.

*What was measured and deliberately not shipped.* A residual **−30 ms** (one
frame) bias remains on every card. A constant shift removes it from the median
and does not improve accuracy:

| shift | early | late | within one frame |
|---|---|---|---|
| 0 | 63.1% | 15.1% | 44.6% |
| +30 ms | 38.0% | 35.8% | 50.2% |

It converts early cards into late ones. Late is worse for reading, one frame is
below what a viewer can see, and the spread is unchanged — so the median
reaching zero would be cosmetic. D38 raised this first and it stays open, now
with numbers attached.

*Verified live:* 6 question marks in the output, commas and full stops gone,
**0 cards starting before their picture**, 63s end to end.

529 tests green.

---

**D61 — Neither tiebreaker beats leaving the word alone. The review list stays
a review list.**

Raz asked to automate the second opinion, with a fair argument: *"leaving it as
a mistake either way leaves me to fix it, so even if it'll be wrong I'll still
fix it, so we have nothing to lose."*

**There is something to lose, and it is measurable.** Of the 107 disagreements
across all three corpora:

| | |
|---|---|
| turbo was right | **55** |
| the partner was right | 35 |
| both wrong (unwinnable) | 17 |

**When the two models disagree, the shipping model is right 61% of the time.**
Auto-replacing makes the transcript worse more often than better, and the cost
is not symmetric: a word he had no reason to check becomes wrong, and the flag
that would have sent him to look at it is gone.

*Both proposed tiebreakers were built and measured on the 90 winnable cases:*

| method | right | wrong | no call | accuracy |
|---|---|---|---|---|
| **keep turbo (do nothing)** | 55 | 35 | 0 | **61.1%** |
| take the partner | 35 | 55 | 0 | 38.9% |
| DictaBERT in context | 31 | 22 | 37 | 58.5% |
| acoustic alignment | 12 | 28 | 50 | 30.0% |
| both agree, else abstain | 9 | 5 | 76 | 64.3% |

**Nothing beats doing nothing.** DictaBERT lands below the baseline (95% CI
45.2–71.8%, straddling it). The abstaining combination reaches 64.3% but on 14
of 90 cases, which is not a policy.

*The acoustic result is a trap worth naming.* At 30% it is well below chance,
and inverting it gives 70% — which looks like a win over 61.1%. It is not:
28/40 has a 95% CI of **55.8–84.2%**, which contains the baseline. Adopting an
inverted method on 40 samples, with no explanation for why its polarity is
backwards, is precisely the curve-fit that D47 and D48 already cost this
session. The likely cause is mundane — mean log-prob over a token span favours
whichever candidate tokenises shorter, so it measures tokenisation rather than
acoustic fit — and until that is understood the number means nothing.

*Not built. Nothing in `src/` changed.* The flag-only design of D52 stands, and
the measurement gives it a better caption: **when the panel flags a word, the
alternative is right about a third of the time, and roughly one flag in six is
a case where neither model got it.** That is genuinely useful to a human
reviewing, and useless to an automaton choosing.

*What would change the answer:* a tiebreaker with an independent, understood
signal. The acoustic idea is not dead — it is unimplemented. Scoring with the
*other* model's encoder, normalising for token count, and explaining the
polarity before trusting it would make it a real experiment rather than a
coin-flip with a story attached.

529 tests green, unchanged.

---

**D62 — Raz's rule: take the real word. 6 fixed, 0 broken.**

D61 measured two tiebreakers and neither beat leaving the word alone. Raz
proposed a third that neither of us had tried, and it is better than both
because it only acts where there is evidence:

> both models heard real words → flag it, do nothing. One heard a non-word →
> use the model with the real word. Neither is real → use context.

*Tested on the same 107 disagreements before building anything:*

| bucket | n | turbo right | partner right | neither |
|---|---|---|---|---|
| both real → do nothing | 80 | 50 | 23 | 7 |
| exactly one real → take it | 14 | 3 | 9 | 2 |
| neither real → context | 13 | 2 | 3 | 8 |

**Rule 2 fixes 6 words and breaks 0.** Pooled WER 4.83% → 4.69%.

*Both do-nothing branches earn their place.* On the 80 both-real cases, taking
the partner would cost 27 words — the shipping model is right 61% of the time
when they disagree. The lexicon gate is not a tiebreaker; it is a filter that
finds the few cases where one candidate is not a word at all.

*Rule 3 is dead, and the data is blunt.* Of the 13 cases where neither model
produced a real word, the correct answer is in the lexicon **once**. A
context-based replacement has a 1-in-13 ceiling before any model votes. Not
built. Those are exactly the words that started the proofreading investigation
— `תתעקם`, `ייתלש` — and they remain out of reach.

*The contract needed a fourth edit reason.* `second_opinion` joins `glossary`,
`punctuation` and `llm` — an enum amendment inside v2, the same standing as
`hebrew_rule_violation` (D23/D25), recorded in the contracts changelog. Raised
and approved before touching it, per CLAUDE.md.

*The contract also caught a real bug during implementation.* Substituting a
word without rebuilding `segment.text` fails validation immediately — the
schema requires text to equal the space-joined words. That invariant existed
precisely so a half-applied edit cannot ship, and it worked.

*What this is worth, honestly:* 9 edits and 6 words across 4,331. Small. But
after six methods that were worse than doing nothing, it is the first
correction pass in the project that improves the transcript with **zero
measured damage** — which is the property Raz asked for and the reason his
framing beat both of mine.

546 tests green.

---

**D63 — One work folder per project, not per timeline name.**

Raz, reading the reuse line in the log: *"it says hebsub_audio.wav which is ok
but is not really exclusive to the current project."*

The instinct was right and the location was not. The filename was never the
problem — the **folder** was. `work/` was keyed on the timeline name alone, so
two projects that each hold a "Timeline 1" shared
`work/Timeline_1/` and overwrote each other.

*What was and was not at risk.* The audio was safe: `timeline_fingerprint`
covers clip paths, positions, source mtimes, track enable state and frame rate
(D42), so a different project misses the cache and re-renders. **The artifacts
were not.** `01_raw.json`, `02_proofread.json`, `03_segmented.json`,
`final.srt` and `review.json` would each be clobbered by whichever project ran
last — and silently, since every filename is identical.

`work_dir_name(project, timeline)` now produces
`GUY_TEST_SUBS__Timeline_1___cut`. Pure, so the rule is tested without Resolve:
two projects with the same timeline name, two timelines in one project, Hebrew
names that must not slug away to nothing, and empty names.

The reuse line now prints `<folder>/<file>` rather than the bare filename, so
the scoping is visible in the log rather than something to take on trust.

*One-time cost:* existing `work/` folders do not match the new naming, so the
first run per timeline after this change re-renders once. The old folders are
harmless and regenerable.

553 tests green.

---

**D64 — hspell measured and declined again. The rule wants a *stricter*
lexicon, not a bigger one.**

D62 shipped Raz's rule on DictaBERT's 105k-word vocabulary and noted the
lexicon as the limiting factor. He asked to measure hspell before committing
to the AGPL. Downloaded to a scratch folder, measured, deleted.

| lexicon | entries | acts | fixes | breaks | WER after |
|---|---|---|---|---|---|
| **DictaBERT (shipping)** | 105,288 | 9 | 6 | 0 | **4.69%** |
| hspell | 469,367 | 9 | 6 | 0 | 4.69% |
| union | 536,210 | 8 | 5 | 0 | 4.71% |
| intersection | **38,445** | 10 | 7 | 0 | **4.66%** |

**hspell alone is worth nothing** — identical fixes, identical WER, 4.5x the
entries and a copyleft licence.

*The counterintuitive part, and the useful one.* The **union is worse than
either lexicon alone**, and the **intersection is better than both**. That
inverts the obvious intuition. This rule does not want to know which words
exist; it wants to know which strings are *not words*. Every entry added to the
lexicon makes one more non-word look real and suppresses a correction. A
stricter lexicon detects more.

*Different lexicons have different holes,* which is why the totals match while
the contents do not. DictaBERT holds `הסליחות` (trained on Hebrew text
including Judaica) and hspell does not; hspell reaches `הלבד` where DictaBERT's
prefix peeler reads `הלבט` as ה+לבט and calls it real. Each fixes what the
other misses, and they cancel.

*One anecdote, stated as an anecdote.* On the run Raz did today the second
model produced `תתעקם` -- the correct word, the one he has asked about three
times. hspell contains it; DictaBERT does not. So hspell **would** have fixed
that word on that run. It is one word, it depends on the second model happening
to get it right that time (an earlier run produced `תתקם`), and it is not
visible in the aggregate. Not a reason to take a licence.

*Declined. Nothing entered the repo* -- the dictionary was downloaded to the
scratchpad and deleted after measuring, exactly as in D46.

*The open lead:* the intersection result says a stricter lexicon is worth
about one more word per corpus. Building one without hspell -- frequency-
filtering DictaBERT's vocabulary, say -- costs nothing in licensing and has
not been tried.

553 tests green, unchanged.

---

**D65 — n-best has a real ceiling and no selector. Morphology cannot tell a
non-word from a word.**

Two ideas tested against the words that keep surviving every pass -- `תתעכם`,
`יתעלש`, `להתעלש`, `תתעלש`.

*First, the measurement that reframed both.* Of the 13 cases where both models
produced a non-word:

| | |
|---|---|
| the truth is **one edit** from a candidate | **11 / 13** |
| the truth is **in the lexicon** | **1 / 13** |

**Generation is not the bottleneck; recognition is.** Edit-1 produces `תתעקם`
trivially and then the lexicon throws it away.

*And most of these are homophones, so no acoustic method can help:*
`כ`/`ק` are both /k/, `ע`/`ה` are both silent, `כ`/`ח` are both /χ/. There is
nothing acoustic to separate. They are spelling decisions, not hearing
decisions -- which retires the whole "listen harder" family for these cases.
The exception is `תשרוט`/`ישרוט`, where /t/ vs /j/ is genuinely audible.

---

*Idea 2 -- is the truth already in Whisper's n-best?*

| beam | truth present | of which hard cases |
|---|---|---|
| n=1 | 8/52 | 1/11 |
| n=5 | 21/52 | 3/11 |
| **n=10** | **24/52 (46%)** | **4/11** |

The ceiling is real: at n=10 the correct word sits in the model's own beam for
nearly half the errors, with no second model and no lexicon. Two caveats that
stop it being a result. It is a **ceiling** -- converting it needs a selector,
and every selector tried has lost to "keep the greedy hypothesis" (D61,
D48). And the n=1 row is itself informative: simply re-decoding on a different
30-second window changes 8 words, which means beam rank is entangled with
window placement rather than being a stable property of the audio.

*Idea 4 -- morphology as a validity oracle. Dead, and instructively so.*

`dictabert-morph` analyses `תתעכם` as **VERB, Fem, Sing, 3rd, Future** --
confidently, correctly, and uselessly. `dictabert-lex` returns `[BLANK]` for
real and fake words alike.

Neither is broken. **Hebrew morphology is pattern-based, and `תתעכם` fits the
hitpael future pattern perfectly.** It is morphologically well-formed and
lexically non-existent: the pattern is fine, the root ע.כ.ם simply is not a
Hebrew root. An analyser validates the *shape*; it cannot know the root exists.

A real version needs a **root list** -- roughly 25k Hebrew roots -- plus a
generator expanding each into its paradigm. That is exactly what hspell is:
its 469k forms are generated from roots, which is why it holds `תתעקם`,
`ייתלש`, `תיתלש` and `ישרוט` while DictaBERT does not. DictaBERT's vocabulary
is whatever appeared often enough in training text; hspell's is systematic.

**So idea 4 collapses into hspell** -- and this is a sharper argument for it
than D64 produced. hspell's value is concentrated precisely in the cases Raz
keeps asking about and averaged away everywhere else, which is why the pooled
comparison showed nothing.

*Nothing built. Nothing in `src/` changed.*

*What is left, in order of cost:* harvesting Raz's own corrections into the
lexicon (free, untested within a single domain over time -- D46 only tested
cross-domain transfer, which is the pessimistic case); a frequency-filtered
stricter lexicon (free, D64's open lead); or accepting hspell's licence for a
gain now understood to be concentrated rather than absent.

553 tests green, unchanged.

---

**D66 — The user's own corrections become the lexicon.**

D65 established that the wall is recognition: of the 13 cases where both models
produce a non-word, the truth is one edit away in 11 and in the lexicon in 1.
hspell would fix that and is AGPL. Raz's own corrected files are free.

*Measured leave-one-corpus-out first, because D64 showed adding words can
suppress corrections:*

| held-out corpus | lexicon | fixes |
|---|---|---|
| pooled | DictaBERT only | **6** |
| pooled | + harvested from the other two | **6** |
| pooled | *(leaked — includes the held-out corpus)* | **9** |

**Cross-domain transfer is zero**, exactly as D46 found for the detector. The
leaked row is not a result but it is not noise either: it shows the mechanism
works, worth **+3 fixes (50%)**, whenever the word has been seen before.

That is the honest shape of it. This pays within a domain over time — many
reels for one client — and three unrelated corpora cannot measure that. It
ships because it is free, zero-risk in every configuration measured, and
compounds.

*Why it cannot backfire.* A general dictionary contains words that are real but
wrong in context, which is how hspell's 469k entries bought nothing. A user's
corrections contain only words he wrote, and never the ASR's mistakes — so
learning makes the **correct** side of a disagreement recognisable and leaves
the wrong side alone. Verified: `תתעקם` becomes a word, `תתעכם` does not.
A test pins that.

*A bug the work surfaced, and a second pass to fix it properly.*
`learn_words` harvested 205 words from a 1,486-word file. `<` and `>` are
**symbols**, not punctuation, so the fold left them attached and the
"is the whole token Hebrew?" test rejected every word touching a `<b>` tag —
on a subtitle file, every word at a card boundary.

The first fix stripped `<...>` tags and took it to 764. **That fixed the case
and left the class**, which Raz caught: `♪שלום♪`, `~שלום~`, `→שלום` and `8שקל`
all failed identically, and music notes are ordinary in subtitles.

The real fix drops the all-Hebrew test and takes **maximal runs of Hebrew
letters after folding** instead. Order matters: folding first removes the
geresh, so `ג'יפ` stays one word rather than splitting into `ג` and `יפ`. Runs
of a single letter are dropped, so the `ה` of `הPocket` is not learned as a
word. `8שקל` now correctly yields `שקל`.

The lesson generalises past this function: **"strip punctuation" is not "strip
everything that is not a letter"**, Unicode has five symbol and number
categories that are neither, and a fix aimed at the example rather than the
category leaves the rest of them live.

*Effect on the reel Raz keeps asking about*, with its own vocabulary learned:
5 corrections becomes 6, the new one being `תשרוט → ישרוט` — previously blocked
by nothing but the lexicon.

`lexicon.txt` ships seeded with 1,790 words from the three corrected corpora.
`--learn` accepts any file; feed it a corrected `.srt` after a job and the next
job knows those words.

566 tests green.

---

**D67 — Flagged words get a marker and a second subtitle track, not a fix.**

The review list has been correct and unreachable. 6 to 20 words per job, listed
in the panel log and in `review.json` as `m:ss`, and the only way to act on one
was to read a timecode off a log and type it into Resolve. Two builds, because
they answer different questions.

*Markers* answer "where are they". `AddMarker` on the timeline, Fuchsia, one
per flagged word, with the heard word as the marker name and the alternative as
the note. Up/down arrow steps between them and the Markers panel lists all of
them at once, both of which are Resolve's own navigation rather than ours.

Two things about markers had to be probed rather than assumed:

  * **`frameId` is relative to the timeline start, not to 01:00:00:00.** An
    existing marker read back as frame 30289 on a timeline whose
    `GetStartFrame()` is 108000 — 1009.63 s into a 1063 s programme. Treating
    that number as absolute would put every marker an hour out.
  * **`DeleteMarkersByColor` is the wrong tool.** Raz's timeline already
    carries a Cyan marker of his own, and colour is not identity. The markers
    are tagged with `customData = "hebsub-review"` and removed by tag, so
    clearing ours cannot take his. Verified live: 3 placed, 3 cleared, the
    Cyan one untouched.

One marker per frame; Resolve returns False rather than raising on a clash. Two
flagged words inside the same 33 ms, or one of the user's own markers already
there, nudge up to 4 frames. Losing a flag silently is the one outcome that is
not acceptable, and it happens in real material — the live probe placed 5.00 s
and 5.01 s at frames 150 and 151.

*A second subtitle track* answers "is it actually wrong". Each flagged word
becomes a card spanning the real card it sits in, showing the heard word above
the alternative. Dropped on a second subtitle track it sits beside the actual
subtitle, over the picture, which is how you would check it anyway. Named
`HebSub Review`, versioned off its own base so a `HebSub Subtitles V2` in the
bin does not push it to V2.

It gets the **same leading timing card** as the real track. Resolve drops
lead-in silence on import (D55); a review track whose first flagged word is 40 s
in would land 40 s adrift of the subtitles it annotates — and since it is read
*against* them, adrift is worse than useless. Both files start at absolute zero,
so snapping both to the timeline start puts them frame-for-frame on top.

*Neither disappears when you fix the word*, because nothing knows you fixed it.
Inferring it would mean re-transcribing to check, or guessing. The honest
version is a **CLEAR FLAGS** button in the panel and `--clear-markers` on the
CLI: the markers stay until Raz says he is done with them.

Both paths are wrapped: a marker or import failure prints and continues. A
navigation aid must never fail a job whose subtitles are already correct.

583 tests green, up from 569.

*Amended by D68*: the review track was built against the wrong half of the
suggestion. It survives behind `--review-track`, off by default.

---

**D68 — The review list becomes a list you can click.**

D67 built the markers and the review track. The track was the wrong half: the
suggestion Raz meant to pair with markers was the panel list, and the two
answer questions the track does not.

*The track is not deleted.* It works, it is tested, and it costs nothing when
it is off. What it does cost when it is on is a clip in the media pool on every
job, which is clutter for a thing now covered twice over. It moves behind
`--review-track`, default off. Deleting working code to tidy a decision is how
you end up rebuilding it.

**What the panel list does that a marker cannot.** A marker is a position. The
list is the whole set at once: every flagged word, its `m:ss`, what was heard
and what the second model heard instead, in one place, with the ones you have
already dealt with crossed off. Clicking a row calls `SetCurrentTimecode` and
the playhead goes there — no timecode read off a log line and typed back in.

One trap, and the two conventions sit ten lines apart in the same file:

  * marker `frameId` is **relative to the timeline start** (D67)
  * `SetCurrentTimecode` wants the **absolute** timecode

So second zero is `01:00:00:00`, not `00:00:00:00`, and frame 108000, not 0.
Probed live at 0.0, 5.0, 60.0 and 1009.63 s: every one read back byte-identical
from `GetCurrentTimecode`. Then end to end — a synthetic click on the second
row of a real panel moved Resolve's playhead to `01:02:07:00`, exactly the
127.0 s the flag carried.

Each cell of a row is its own label rather than one formatted string. Hebrew,
an arrow and a timecode in a single Tk label invites bidi reordering, and which
word is which is the entire content here.

**COPY** puts the alternative on the clipboard, because the alternative is
Hebrew and retyping Hebrew to fix a Hebrew word is absurd.

**The tick is local to the window and does not persist.** Same reason the
markers do not clear themselves: nothing knows whether the word was fixed, and
finding out would mean re-transcribing. It is a reading aid for the pass you
are making right now, and it says so.

The panel does not compute the timecode. `Panel.goto` calls
`host_resolve.seek`; a test fails if `SetCurrentTimecode` ever appears in
`ui/app.py`. Only `host_resolve` knows a Resolve timeline starts at an hour in,
and that fact having two homes is exactly D41.

595 tests green, up from 583.

---

**D69 — The panel speaks Hebrew, and Tk had to be measured before it could.**

Cosmetic, and scoped to `src/hebsub/ui/*`. `host_resolve` and the CLI still
print English; the panel translates their lines on the way into its log through
a table in `ui/hebrew.py`. Translating upstream would have changed the CLI, two
module specs and the tests that assert on those lines, which is not what
"cosmetic" means.

*The whole job turned on one question nobody could answer from memory: does Tk
do bidi?* Four probes, each rendered to a PNG and looked at, because reading
Hebrew out of a screenshot by eye is not evidence — I twice "read" a line as
correct that measurement showed was not.

  * Tk **does** reorder a Hebrew run. The test that settled it was `ABCאבג`
    with a Latin marker: after `C` comes GIMEL, then BET, then ALEPH. Words in
    a sentence are ambiguous to look at; three distinct letters after a fixed
    marker are not.
  * The base direction is nevertheless **LTR**. `אבג ABC` puts the Hebrew left
    and `ABC` right — backwards for a Hebrew line.
  * **RLM does nothing.** **RLI/PDI draw visible boxes** — Tk has no glyph.
  * **RLE … PDF works.** `ABC` moves left, Hebrew right.

Two cases RLE does not settle, and both showed up on screen before they showed
up in reasoning: a bare NUMBER beside Hebrew (`01 · חיבור` rendered with the 01
on the left; so did `14 תווים`) and a Latin VALUE beside a Hebrew label. Both
are fixed by splitting the string across two widgets and packing them
`side="right"`. That is not a workaround for a Tk bug — it is the only version
that cannot drift, because neither widget contains mixed text at all.

*Two real bugs fell out of doing this*, neither of them cosmetic:

  * `_fit_window` called `geometry()` with a size and no position, so Windows
    re-placed the window when the review list appeared. On a 1080-high screen
    that pushed the bottom of the panel off the display — hiding the log and
    the list that had caused the growth.
  * The screenshot harness believed `winfo_rootx`. This display is 3840x2160
    at 200% and both Tk and the capture process are DPI-unaware, so Tk's
    logical pixels addressed a physical screen and every crop grabbed the
    top-left quarter at 2x. It read exactly like "the right edge is cut off",
    which is why it survived four attempts to fix the wrong thing. The harness
    now measures the ratio instead of assuming it.

Type comes from the design system, which already specifies the Hebrew swap:
Rubik for display, IBM Plex Sans Hebrew for body, no uppercasing (Hebrew has no
case) and no mono letter-spacing. IBM Plex Mono has no Hebrew at all, so the
engraved labels use the Hebrew face rather than rendering as boxes.

Layout is centred where it can be and mirrored where direction carries meaning:
review rows pack from the right, COPY at the far left, scrollbars on the left.

620 tests green, up from 596. Rendering itself is still not testable in CI --
what is testable is that the direction marks are the ones the probe proved,
that no translated line invents or loses a number or a file name, and that an
unknown line reaches the log in English rather than disappearing.

---

**D70 — Harvesting the lexicon becomes one button, because GetName() is the text.**

`--learn` worked but cost a workflow: export the corrected `.srt`, find it,
run the CLI against it. Raz asked whether it could be automatic. It can, and
the reason is a single probe result: **a subtitle TimelineItem's `GetName()`
returns its text.** Verified on a live 611-card track. There is nothing to
export — the corrected words are readable straight off the timeline.

*What it learns is only the difference.* Every Hebrew word on the track that is
not in the `.srt` we generated is something he typed. Harvesting the whole
track would be easier and would be wrong: it would feed the ASR's own mistakes
back in, so `תתעכם` would become a word the checker recognises — and catching
exactly that is the only reason the lexicon exists.

It is one set difference. No card alignment, no timecode matching. Both would
break the first time two cards were merged or the clip nudged by a frame, and
neither is needed to answer "which words are new". The track is picked by word
**overlap** with our output rather than by index, so the review track cannot be
harvested by mistake; below 50% it is a different programme and nothing is
written.

*Two findings from the same probe, recorded so nothing is built on them:* a
two-line card comes back with its line break already gone, and Resolve strips
`<` from the name — the timing card reads `">> TIMING CLIP - DELETE ME "`.
Harmless here only because `learn_words` takes runs of Hebrew letters and
ignores every symbol (D66).

**A real bug fell out, and it would have made this feature pointless.**
`proofread.hebrew_lexicon()` resolves the user lexicon as
`Path.cwd() / lexicon.txt`. That is right for the CLI. Launched from Resolve's
**Workflow > Scripts** menu the working directory is Resolve's, so every word
Raz had ever taught it was silently not loaded — the file was being written and
never read. `lexicon_path()` now resolves from the package and `run` hands the
tiebreaker its lexicon explicitly. `work_root()` was centralised at the same
time but deliberately left relative to the working directory: pinning it would
relocate artifacts that already exist.

*The honest limit.* The diff is against the LAST run's `.srt`. Harvest after
correcting and before re-running; after a re-run the track belongs to the
previous output and the differences are run-to-run variation, not corrections.
That is why this is a button and not something that fires automatically —
the moment it is correct to press is a fact only Raz has.

Live: pressed on the workshop timeline, learned 3 words (`יתלש`, `תתלש`,
`שעצ`, all real Hebrew), lexicon 1790 -> 1793; pressed again, reported nothing
new.

631 tests green, up from 620.

