# decisions-003.md — rulings on the bench judgment calls

Follows `decisions-002.md`. Numbering continues.

**D23 — Stay at v2; add a changelog.** D16–D18 change the warning enum and the
warning object shape, not the Transcript or SubtitleFile schemas. Nothing
consuming v2 breaks and every cross-reference stays honest. Add a dated
changelog block at the top of `contracts.md` recording what changed within v2
and under which decision number.

**D24 — Bench emits a row for every clip, always.** `cards_3plus_lines` is
correctly dropped as structurally always zero. But a clip that fails validation
must not vanish from the CSV — a benchmark that silently omits its hardest
inputs is worse than no benchmark. Every bench row carries:

| Column | Notes |
|---|---|
| `status` | `ok` or `failed` |
| `failure_reason` | validation error message, or empty |

On `status=failed`, all metric columns are null, not zero. Null means "not
measured"; zero means "measured, and it was zero". Never conflate them.

**D25 — `segment` owns the Hebrew rules; `bench` only counts.**
Overrules the reading that bench derives `hebrew_rule_violations` from the
SubtitleFile. That would put the rule logic in two places — segment deciding
where to split, bench judging whether the split was legal — and two
implementations of the same rules drift.

`segment` violates its own rules deliberately, when no legal split exists.
It therefore knows, and it reports:

```json
{ "stage": "segment", "code": "hebrew_rule_violation",
  "wid_start": 61, "wid_end": 62,
  "detail": "line ends on function word 'של'" }
```

Bench counts warnings with that code and groups by the rule named in `detail`.
Bench contains no Hebrew linguistic logic at all.

Warning codes in v1 become: `timing_clamped`, `cps_exceeded`,
`card_too_short`, `line_too_long`, `hebrew_rule_violation`, `llm_rejected`,
`edit_budget_hit`, `gap_not_applied`.

---

Post-freeze findings batch into `decisions-004.md`, not this file.
