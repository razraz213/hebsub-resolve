# Module: bench

The eval runner. Runs the pipeline over the eval set and writes one CSV row
per (clip × engine). This is the module that turns "better" from a feeling
into a number.

The protocol it implements is `docs/eval-protocol.md`. Read that first.

> **Status: column list settled (D21 → D23–D25).** D21 held this spec until
> the column list was signed off. `decisions-003.md` does that: the row shape
> is fixed by D24, and `hebrew_rule_violations` moves to a warning count by
> D25. The hold on the column list is released. Bench is still blocked
> end-to-end on the real fixtures (D14) — see below — but the metric
> functions and the row-assembly logic can be built against this spec now.
>
> Bench is the project's measurement instrument; wrong columns produce
> confident wrong answers that stay undetected for months. Post-freeze
> findings go in `decisions-004.md`, not into a quiet edit here.

## Owned files

```
src/hebsub/bench.py
tests/test_bench.py
```

## CLI

```
python -m hebsub.bench --set eval/ --engines ivrit_local,elevenlabs \
       --out bench.csv [--passes glossary,llm] [--force]
```

## What it does

For each clip in `--set` and each engine in `--engines`:

1. Run the pipeline (via the module functions, not by shelling out to `main`),
   **through export**, into a per-run work directory.
2. Score the transcript against that clip's hand-corrected reference.
3. Read the subtitle-quality numbers out of export's report sidecar.
4. Append one row to the CSV.

It measures. It does not correct, tune, or retry.

Export runs even though the `.srt` itself is never scored — the run exists to
produce `final.srt.report.json`, which is where the delivered-file numbers
come from.

## Where each number comes from

This matters more than it looks. Three different artifacts, three different
jobs, and mixing them up is how a benchmark starts lying.

| Source | Supplies |
|---|---|
| the hand-corrected reference | `wer`, `entity_accuracy`, `punct_f1` |
| `final.srt.report.json` (export's report) | every `stats` column, and `warn_gap_not_applied` |
| `03_segmented.json` → `meta.warnings` | every `warn_*` column except `warn_gap_not_applied`, and `hebrew_rule_breakdown` |

**Never re-parse the emitted SRT to compute a statistic** (D16). The writer's
own report is authoritative. A reader that disagrees with the writer is a bug
hunt nobody needs, and it would be measuring the parser as much as the
pipeline.

Nor does bench re-derive a *judgement* from the SubtitleFile (D25). It used to
compute `hebrew_rule_violations` by inspecting where `segment` put its line
breaks. That is gone. `segment` decides where to split, so `segment` is the
only thing that knows a split was illegal, and it says so in a
`hebrew_rule_violation` warning. Bench counts those warnings and groups them
by the rule id at the front of each `detail` string. **Bench contains no
Hebrew linguistic logic at all** — not a function word list, not a construct
chain heuristic, nothing. If the rules change, only `segment` changes.

## Metrics — one per pass (D4, D20)

The old single-WER rule is gone. A pass is judged on its own metric and must
not regress the others.

| Column | Metric | Pass it judges |
|---|---|---|
| `wer` | word error rate | llm (substitution) |
| `wer_tolerant` | WER ignoring כתיב מלא/חסר spelling differences | llm (substitution) |
| `entity_accuracy` | % of glossary terms rendered correctly | glossary |
| `punct_f1` | punctuation F1 against the reference | llm (punctuation) |
| `boundary_f1` | card-boundary F1 against the reference cuts | segment |

Normalization before WER scoring: strip niqqud, unify final-letter forms,
strip punctuation, collapse whitespace. Punctuation is scored **separately**
by `punct_f1` — never folded into WER, or a model that adds correct
punctuation gets penalized for it.

`boundary_f1` was added by D33 and replaces the vaguer "CPS/line-length
warning counts" that `eval-protocol.md` names for the segment pass. Those
count symptoms; boundary agreement measures the thing `segment` actually
decides — where to cut. It is the fraction of the reference's card start
times that the run also chose (recall), balanced against how many of its own
cuts the reference did not have (precision), within `--boundary-tol` seconds.

Recall alone is not a usable metric here: cutting after every word matches
every human cut by accident. Precision is what stops that, so the column is
F1 and not either half. Measured baseline at the D33 defaults: **59.3%**.

`wer_tolerant` exists because unvocalised Hebrew has two correct spellings
for the same word — `המשלוח`/`המשלח`, `הייבוא`/`היבוא` — and the strict
figure charges for the difference. Measured over corpus 1, **18% of all
counted error was spelling convention** (3.65% strict against 2.99%
tolerant). It is a **second column, never a redefinition**: `wer` stays
comparable with every earlier row, and collapsing ו/י merges a few genuinely
different words (`שיר`/`שר`), so the tolerant figure is a floor rather than
the truth. Read them as a pair. See D39.

`boundary_f1` needs a reference `.srt`. A reference supplied as `.txt` only
scores the other three metrics and leaves this column **null, not zero** —
unmeasured is not the same as bad.

## Subtitle-quality columns

Straight from export's `stats` block, no recomputation:

| Column | Source | Meaning |
|---|---|---|
| `cards` | `stats.cards` | total cards in the delivered file |
| `cards_over_cps` | `stats.cards_over_cps` | count over max CPS |
| `pct_over_cps` | derived | `cards_over_cps / cards` |
| `cards_over_line_len` | `stats.cards_over_line_len` | hard rule — must be 0 |
| `cards_under_min_duration` | `stats.cards_under_min_duration` | quality target, not a failure |
| `max_cps` | `stats.max_cps` | the worst card in the file |
| `mean_cps` | `stats.mean_cps` | the shape of the whole file |

`pct_over_cps` is the only derived value, and it is derived from two reported
counts rather than recounted.

There is deliberately **no `cards_3plus_lines` column.** A 3-line card is a
hard-rule violation that `validate_subtitle_file()` rejects at the stage
boundary, so a run that produced one has already raised and has no row. A
column that is structurally always 0 is noise pretending to be a check.
D24 confirms the omission. It is the one column dropped there; every other
column survives, and a failing clip still gets a row (see below).

`hebrew_rule_violations` used to sit in this table, sourced from the
SubtitleFile. It is now `warn_hebrew_rule_violation`, a warning count like the
others (D25). One number, one source.

## Warning columns

One column per v1 warning code, each a count:

`warn_timing_clamped`, `warn_cps_exceeded`, `warn_card_too_short`,
`warn_line_too_long`, `warn_hebrew_rule_violation`, `warn_llm_rejected`,
`warn_edit_budget_hit`, `warn_gap_not_applied`

The first seven come from `meta.warnings` on the final Transcript; the last
comes from export's report, since export appends to no Transcript.

Counting is mechanical: group `meta.warnings` by `code` and count. An entry
whose `code` is not one of the eight raises, naming `bench` and the unknown
code. A warning the runner does not recognise must not be silently dropped
into no column at all — that is precisely the failure mode the warning list
exists to prevent.

### `hebrew_rule_breakdown`

One extra column, alongside `warn_hebrew_rule_violation`, carrying the
per-rule split:

```
et_split=2;function_word_line_end=5
```

Rule ids sorted alphabetically, `;`-joined, `=` between id and count, rules
with a zero count omitted, empty string when there are none. The ids come from
splitting each `detail` on its first `": "` and taking the left half — see
`docs/modules/segment.md` for the format and the rule list.

It is one packed column rather than one column per rule on purpose. A column
per rule would put `segment`'s rule list into `bench`'s header, so adding a
Hebrew rule would silently change the shape of every historical `bench.csv`
comparison. The packed column keeps the header stable and keeps the rule list
in exactly one file.

`warn_hebrew_rule_violation` must equal the sum of the breakdown counts.

Note the overlap with the stats columns, which is intentional and worth
reading as a pair. `warn_cps_exceeded` is `segment` saying *"I could not do
better here"*; `cards_over_cps` is export saying *"this is what the file
actually looks like"*. When those two numbers diverge, something between the
two stages is wrong, and you want to see it in the same row.

There is no `warn_itn_skipped` column. The code was deleted in v1 (D17); the
v2 intent lives in `NOTES.md`.

## Operational columns

`clip`, `engine`, `engine_version`, `passes_enabled`, `wall_clock_s`,
`cost_usd`, `run_at`, `status`, `failure_reason`.

`cost_usd` is 0 for local engines and computed from the engine adapter's
published per-hour rate otherwise.

### `status` and `failure_reason` (D24)

**Every clip gets a row, always.** A clip that fails validation must not
vanish from the CSV. A benchmark that silently omits its hardest inputs is
worse than no benchmark — the average quietly improves and nobody can see why.

| Column | Values |
|---|---|
| `status` | `ok` or `failed` |
| `failure_reason` | the message from the exception that stopped the run, or empty when `status=ok` |

`failure_reason` is typically a `ContractError` message from a stage boundary,
which already names the offending module, field, and `wid`. Any other
exception's message goes in verbatim. It is never truncated and never
summarised; if it contains a comma the csv writer quotes it like anything
else.

These two replace the old single `error` column. `status` is what you filter
and group on; `failure_reason` is what you read afterwards. One column doing
both jobs meant every consumer had to test a free-text field for emptiness to
learn whether a row was trustworthy.

### Null is not zero

On `status=failed`, **all** metric columns — the three metrics, every
subtitle-quality column, every `warn_*` column, `hebrew_rule_breakdown` — are
written as null. In the CSV that is an **empty field**.

A measured zero is written `0`. Never write `0` for something that was not
measured, and never write an empty field for a genuine zero. Null means "not
measured"; zero means "measured, and it was zero". Conflating them turns a
crashed clip into a perfect score, which is the single most dangerous thing a
benchmark can do.

The operational columns stay populated on a failed row: `clip`, `engine`,
`passes_enabled`, `run_at`, and `wall_clock_s` (time spent before the failure)
are all real measurements and are written. `engine_version` and `cost_usd` are
written when the run got far enough to know them, empty otherwise.

## Ablation

`--passes` runs the pipeline with a subset of proofread passes enabled, so a
pass's contribution is a diff between two rows rather than an argument.
Running with `--passes ''` is the baseline: raw ASR, no correction.

## Output rules

- Append, never overwrite. `bench.csv` is a history, and its git log is the
  story of whether the project is improving.
- UTF-8 explicit, `newline=""` for the csv writer.
- A clip that fails to process still gets its row: `status=failed`, the
  message in `failure_reason`, and every metric column empty (D24). One broken
  clip does not abort the run — this is the one place in the repo where
  continuing past a failure is right, because the point is the comparison
  table. The failure is loud in the row, not swallowed.

## Acceptance criteria

- Two engines × three clips produces six rows plus a header.
- Rerunning appends; the existing rows are untouched.
- A clip whose pipeline raises produces exactly one row with `status=failed`,
  a non-empty `failure_reason`, and every metric column an empty string — not
  `0` — and the run exits 0.
- A successful clip's row has `status=ok` and an empty `failure_reason`.
- A run of two engines × three clips where one clip raises on one engine still
  produces six rows: five `ok`, one `failed`.
- A genuine zero survives the round trip: a clip with no warnings writes `0`
  in each `warn_*` column, and that row is distinguishable from a failed row
  by reading the columns alone.
- Metric functions are unit-tested against hand-built pairs with known WER,
  known entity accuracy, and known punctuation F1 — not against the pipeline.
- The stats columns are read from a hand-written `report.json` fixture and
  land in the CSV unchanged. This test exists specifically to catch someone
  reintroducing SRT parsing.
- A report missing an expected `stats` key raises, naming `bench` and the key.
  A silently-empty column is worse than a crash here.
- `--passes ''` produces a row whose `entity_accuracy` equals the raw ASR
  baseline.
- Warning counting is tested from a hand-built `meta.warnings` list: three
  `hebrew_rule_violation` entries across two rule ids produce
  `warn_hebrew_rule_violation=3` and the matching `hebrew_rule_breakdown`
  string, sorted and `;`-joined.
- `warn_hebrew_rule_violation` equals the sum of the breakdown counts, on
  every row.
- A `meta.warnings` entry with an unrecognised `code` raises, naming `bench`
  and the code.
- `bench.py` contains no Hebrew string literals and no rule list (D25). Grep
  for the rule ids in `src/hebsub/bench.py` and assert zero hits — the ids are
  read from `detail`, never enumerated.

## Blocked on fixtures

Per D14, the end-to-end behaviour of this module needs the real clips and
their hand-corrected references. The **metric functions** can and should be
built and unit-tested now against small hand-written string pairs — that part
is not blocked, and it is the part most worth getting right early.

## Explicitly out of scope

Choosing an engine. Tuning a threshold. Plotting. Re-deriving anything export
already reported. The CSV is the deliverable; judgment stays with Raz.
