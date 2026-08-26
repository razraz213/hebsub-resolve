# Module: segment

Transcript in → SubtitleFile out. Regroups words into subtitle cards and
splits each card into 1 or 2 display lines.

This is where existing Hebrew subtitle tools fail hardest, and where your
tool wins or loses. ASR gives you utterances; subtitles need cards a human
can read in the time they're on screen.

## Owned files

```
src/hebsub/segment.py
tests/test_segment.py
```

## Constraints (defaults, all configurable)

| Rule | Default | Kind |
|---|---|---|
| Max lines per card | 2 | **hard** |
| Max characters per line | 40 | **hard** |
| Max characters per card | 14 | **hard** — see *Card width* below |
| Max card duration | 6.0 s | **hard** — always achievable by splitting |
| Min card duration | 1.0 s | quality target |
| Max reading speed | 17 CPS | quality target |
| Min gap between cards | — | **not handled here.** See below. |

**Hard rules are invariants.** Violating one is a bug and a failing test.

**Quality targets are best effort.** When one can't be met, append a warning
and carry on. Never crash over a quality target, and never — under any
circumstances — fabricate a timestamp to satisfy one.

### Min card duration: merge, then warn

A card shorter than the minimum is first repaired by **merging it with an
adjacent card**. Merging is legal because it uses only real word timings and
preserves word order: the merged card's `start` is the first word's `start`
and its `end` is the last word's `end`.

Prefer merging with whichever neighbour keeps the result legal and better
balanced. If no merge is possible without violating a hard rule — max
duration, max line length, 2-line limit — leave the card as it is and append
`card_too_short`.

### Max CPS

A card still over max CPS once all splitting and merging options are
exhausted gets `cps_exceeded`. It ships; it is not silently ignored and it is
not fixed by stealing time that isn't there.

### Minimum gap between cards — not this module's problem

Removed from `segment` entirely (D3). Two adjacent cards may share a boundary
timestamp, because that boundary is a real word boundary and moving it would
break immutability. Frame-gap insertion is a *display* concern and lives in
`export` as the opt-in `--gap MS` flag.

## CPS formula

`cps = len(" ".join(lines)) / (end - start)`

The separator counts (D12), matching the `text` invariant. A two-line card is
measured on the same string the contract validates.

## Splitting priority

Break at the highest-priority boundary that fits the constraints:

1. Sentence-final punctuation (. ? ! …)
2. Clause punctuation (, ; :)
3. Before a coordinating conjunction (ו־, אבל, או, אז, כי)
4. Before a preposition or relative pronoun (ש־, את, של, עם, על)
5. Longest silence gap between adjacent words (from word timings)
6. Last resort: nearest word boundary to the midpoint

**This ladder is no longer the whole story (D40).** The silence gap was
measured to be the *strongest* predictor of where Raz cuts — a pause of 50 ms
more than doubles the odds, while a boundary with no pause at all is one he
takes only 34.6% of the time. Ranking it fifth was backwards. It now
contributes to the break cost directly, on the same scale as the rank above,
so a real pause outweighs any syntactic preference.

Grouping is also no longer greedy. `segment` considers every legal partition
of a sentence and takes the cheapest, because a greedy pass cuts when the
character budget fills rather than when the speech invites a cut — which is
what over-segmented by 7%.

Note the upstream dependency: priorities 1 and 2 only exist if the `llm`
pass's punctuation mode ran. On an unpunctuated transcript this module falls
through to 3–6 by design, and that is not an error.

### Card width: a wall by default, a target on request (D41)

`max_chars_per_card` is a **hard ceiling**, and 14 is re-confirmed as the best
value across all three corpora. `width_headroom` lifts that ceiling to
`max_chars_per_card + width_headroom` and turns the original number into a
target the optimiser pays `over_target_cost` per character to exceed.

**It defaults to 0**, which reproduces the hard wall exactly. The experiment
that produced the knob measured flat: 62.4% mean F1 for the wall against 62.5%
for the best soft setting. It is not off because it is untested — it is off
because it was tested.

Turn it on when the content warrants it. Card width tracks the material: 20.5%
of Raz's hand-cut cards on technical content run past 14 characters, against
7.8% and 8.7% on talking-head content. For the former, `width_headroom=4` with
`over_target_cost=1.5` is worth +3.8 F1; for the latter it costs ~2.5.

## Hebrew-specific rules

These are the details that make Hebrew subtitles read badly, and none of them
are handled by generic subtitle tools:

Each rule has a **rule id**. The id is not decoration: it is the grouping key
in the warning this module emits when a rule has to be broken, and it is the
only handle `bench` gets on Hebrew linguistics (D25).

| Rule id | Rule |
|---|---|
| `et_split` | **Never split `את` from the noun it marks.** It's a function word; alone at the end of a line it reads as an error. |
| `function_word_line_end` | **Never end a line with a one- or two-letter function word** (ב, ל, של, עם, על, כי, אם). Push it to the next line. |
| `construct_chain_split` | **Never split a construct chain (סמיכות)** across lines when detectable — `בית ספר`, `מנהל שיווק`. Keep the pair together. |
| `number_unit_split` | **Never split a number from its unit** — `20 אחוז`, `3 מיליון`. |
| `english_phrase_split` | **Keep an inline English phrase intact.** Latin-script runs are not split mid-phrase; a broken English phrase inside RTL text is where bidi rendering goes ugliest. |

One more, which is a **preference, not a rule**, and emits no warning when it
loses: **prefer a top line longer than the bottom line** when both are legal.
Reads better and keeps the shape stable.

### When no legal split exists

These rules are quality targets, not hard rules. A card can be constructed
where every candidate split point breaks one of them and the hard rules —
2 lines, max line length, max card duration — leave no way out. When that
happens `segment` picks the least-bad split, ships the card, and **says so**.
It does not crash, and it does not quietly pretend the split was clean.

`segment` owns these rules end to end (D25). It is the stage that decides
where to split, so it is the only stage that knows a split was illegal. No
downstream consumer re-judges the output — `bench` counts what `segment`
reports and contains no Hebrew logic at all. Two implementations of the same
rules drift, and the drift is silent.

The warning:

```json
{ "stage": "segment", "code": "hebrew_rule_violation",
  "wid_start": 61, "wid_end": 62,
  "detail": "function_word_line_end: line ends on function word 'של'" }
```

`detail` is `"<rule_id>: <prose>"` — the rule id, a colon, a space, then a
human-readable description naming the offending token where there is one. The
prefix is what makes "group violations by rule" a `split(": ", 1)` for a
consumer rather than a second copy of the rule logic. Prose after the colon is
free-form and may change; the rule id may not.

`wid_start`/`wid_end` span the words either side of the offending split, so
the violation resolves to a card in the final SRT by lookup.

One violation, one warning. A card that breaks two rules emits two warnings.

## Balance

When a card splits into two lines, minimize the difference in length between
them *subject to* the rules above. The syntactic rules always beat the balance
heuristic — a well-balanced card that splits `את` from its noun is worse than
a lopsided card that doesn't.

## Warnings this module emits

`cps_exceeded`, `card_too_short`, `line_too_long`, `hebrew_rule_violation`.

Each carries the `wid_start`/`wid_end` of the card it describes, so it can be
traced to a card in the final SRT by lookup. `line_too_long` should be
unreachable — max line length is a hard rule — and exists only for the
pathological case of a single word longer than the limit, which cannot be
split at a word boundary.

`hebrew_rule_violation` is described above. Unlike the other three it is not
about a card's measurements but about where its boundary fell, and its
`detail` carries a machine-readable rule id as its first token.

## Acceptance criteria

### Hard rules — tested as invariants

- Zero cards with more than 2 lines.
- Zero cards exceeding max line length (single over-long words excepted, and
  each one warned).
- Zero cards above max card duration.
- No timestamp is invented: every card's `start` equals some word's `start`
  and `end` equals some word's `end`.
- No word is lost, duplicated, or reordered: flattening all cards reproduces
  the input `wid` sequence exactly. Test this as strict equality of the wid
  list, not a count.
- Every word keeps the `wid` it arrived with.
- Cards never overlap.
- `segment` is the only stage that renumbers `id`; ids come out `0..n-1`.
- Output passes `validate_subtitle_file()`.

### Quality targets — tested as warnings

- A fixture with a 0.4 s utterance between two long pauses, where merging is
  legal, produces a merged card and no warning.
- The same fixture where merging would breach max duration produces an
  unmodified short card **and** a `card_too_short` warning. It does not raise
  and it does not stretch the timestamp.
- A dense-speech fixture over 17 CPS produces the card plus `cps_exceeded`.

### Hebrew rules

- One test per rule, each with a fixture that triggers it and where a legal
  alternative split exists: the alternative is taken, and **no**
  `hebrew_rule_violation` is emitted.
- One test per rule with a fixture boxed in by the hard rules so no legal
  split exists: the card ships, and exactly one `hebrew_rule_violation` is
  emitted whose `detail` starts with that rule's id and whose
  `wid_start`/`wid_end` bracket the split. It does not raise.
- A card breaking two rules at once emits two warnings, not one.
- `detail` splits on the first `": "` into a known rule id and a non-empty
  remainder, for every warning this module emits. Assert on the id, never on
  the prose.
- On the eval set, every Hebrew-rule violation is accounted for by a warning:
  the counts match. The target for the count itself is zero, and a non-zero
  count is a quality signal to chase in `bench`, not a failing test — a
  fixture deliberately constructed to have no legal split would otherwise be
  unfixable.

## Explicitly out of scope

File format, encoding, RTL control characters, frame gaps — all of that is
`export`. This module thinks in strings and lists, never in `.srt` syntax.
