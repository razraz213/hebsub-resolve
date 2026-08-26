"""The eval runner: pipeline over the eval set, one CSV row per (clip x engine).

This is the module that turns "better" from a feeling into a number, so it is
also the module where being wrong is most expensive: a benchmark that lies
stays undetected for months. Three rules follow from that and are enforced
throughout.

  1. **Never re-parse the emitted SRT** (D16). Export's report sidecar is
     authoritative for every delivered-file statistic. A reader that disagrees
     with the writer is a bug hunt nobody needs.
  2. **Every clip gets a row** (D24). A clip that fails must not vanish, or the
     average quietly improves and the hardest inputs disappear from the table.
  3. **Null is not zero.** An unmeasured column is empty; a measured zero is
     `0`. Conflating them turns a crashed clip into a perfect score.

It contains no Hebrew linguistic logic at all (D25). Hebrew rule violations are
counted from the warnings `segment` emitted and grouped by the rule id at the
front of each `detail`; the rule list lives in `segment` and only there.

See docs/modules/bench.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

__all__ = [
    "BenchError",
    "COLUMNS",
    "normalise_words",
    "wer",
    "wer_tolerant",
    "matres_skeleton",
    "entity_accuracy",
    "punct_f1",
    "boundary_f1",
    "count_warnings",
    "hebrew_rule_breakdown",
    "stats_columns",
    "load_reference_cards",
    "run_clip",
]

MODULE = "bench"

# Local engines cost nothing to run; a paid engine's adapter supplies its own
# per-hour rate. Nothing here guesses a price.
LOCAL_ENGINES = frozenset({"ivrit_local"})

FINAL_FORMS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})

# Attached prefix letters, stripped before GLOSSARY comparison only (D11).
# Never applied to WER: there it would hide real errors.
_ATTACHED_PREFIXES = frozenset("בלכמשהו")

# The eight v1 warning codes, in the order their columns appear.
WARNING_CODES = (
    "timing_clamped",
    "cps_exceeded",
    "card_too_short",
    "line_too_long",
    "hebrew_rule_violation",
    "llm_rejected",
    "edit_budget_hit",
    "gap_not_applied",
)

REQUIRED_STATS = (
    "cards",
    "cards_over_cps",
    "cards_over_line_len",
    "cards_under_min_duration",
    "max_cps",
    "mean_cps",
)

COLUMNS = (
    # operational
    "clip", "engine", "engine_version", "passes_enabled",
    "wall_clock_s", "cost_usd", "run_at", "status", "failure_reason",
    # metrics, one per pass
    "wer", "wer_tolerant", "entity_accuracy", "punct_f1", "boundary_f1",
    # subtitle quality, straight from export's report
    "cards", "cards_over_cps", "pct_over_cps", "cards_over_line_len",
    "cards_under_min_duration", "max_cps", "mean_cps",
    # warnings
    *(f"warn_{code}" for code in WARNING_CODES),
    "hebrew_rule_breakdown",
)

# Every column that is a *measurement*. On a failed row these are all null.
METRIC_COLUMNS = frozenset(COLUMNS) - {
    "clip", "engine", "engine_version", "passes_enabled",
    "wall_clock_s", "cost_usd", "run_at", "status", "failure_reason",
}

_SRT_CARD = re.compile(
    r"(?m)^\d+\s*\n(\d\d:\d\d:\d\d[,.]\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d[,.]\d\d\d)\s*\n"
)
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


class BenchError(Exception):
    """Raised when the benchmark cannot produce an honest number."""


def _fail(problem: str) -> None:
    raise BenchError(f"{MODULE}: {problem}")


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalise_words(text: str) -> list[str]:
    """eval-protocol.md: strip niqqud, unify finals, strip punctuation, collapse.

    Punctuation is removed here on purpose. It is scored separately by
    punct_f1 -- folding it into WER penalises a model for adding punctuation
    correctly, which is exactly the wrong incentive.
    """
    text = _strip_diacritics(text)
    text = "".join(
        " " if unicodedata.category(ch).startswith("P") else ch for ch in text
    )
    return text.translate(FINAL_FORMS).split()


# --------------------------------------------------------------------------
# metrics -- pure functions, unit-tested against hand-built pairs
# --------------------------------------------------------------------------


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate on normalised text: (S + D + I) / N."""
    ref = normalise_words(reference)
    hyp = normalise_words(hypothesis)
    if not ref:
        _fail("cannot compute WER against an empty reference")

    errors = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        a=ref, b=hyp, autojunk=False
    ).get_opcodes():
        if tag == "replace":
            errors += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            errors += i2 - i1
        elif tag == "insert":
            errors += j2 - j1
    return errors / len(ref)


def matres_skeleton(word: str) -> str:
    """A word with its optional vowel letters removed.

    Unvocalised Hebrew is written two ways and both are correct: כתיב מלא
    spells out ו and י, כתיב חסר leaves them implicit. `המשלוח` and `המשלח`
    are the same word; so are `ליבא`/`לייבא`, `הייבוא`/`היבוא`,
    `יכולתי`/`יכלתי`.

    Dropping ו and י collapses those pairs. It also collapses a few genuinely
    different words -- `שיר` and `שר` both reduce to `שר` -- which is exactly
    why this does NOT replace `wer`. It powers a second, spelling-tolerant
    column instead, so the strict number stays comparable with every earlier
    row in bench.csv and the tolerant one shows how much of the gap is real.
    """
    return "".join(ch for ch in word if ch not in "וי")


def wer_tolerant(reference: str, hypothesis: str) -> float:
    """WER that does not charge for כתיב מלא / כתיב חסר spelling differences.

    Measured on Raz's own test clip: of 9 counted errors, 5 were spelling
    conventions rather than mistakes -- 5.49% strict against roughly 1.8% real.
    Reporting only the strict figure overstates how wrong the output is, and
    would send someone chasing errors that are not there.
    """
    ref = [matres_skeleton(w) for w in normalise_words(reference)]
    hyp = [matres_skeleton(w) for w in normalise_words(hypothesis)]
    if not ref:
        _fail("cannot compute WER against an empty reference")

    errors = 0
    for tag, i1, i2, j1, j2 in SequenceMatcher(
        a=ref, b=hyp, autojunk=False
    ).get_opcodes():
        if tag == "replace":
            errors += max(i2 - i1, j2 - j1)
        elif tag == "delete":
            errors += i2 - i1
        elif tag == "insert":
            errors += j2 - j1
    return errors / len(ref)


def _token_variants(token: str) -> set[str]:
    """A token as written, plus the form with one attached prefix removed.

    D11 requires stripping attached prefix letters before glossary comparison,
    because Hebrew glues them on: the glossary says the term, the transcript
    says the term with a preposition fused to the front.

    Both forms are kept rather than only the stripped one. Always stripping
    would let a term match a genuinely different word that merely happens to
    start with a prefix letter. This is deliberately NOT applied to WER, where
    it would hide real errors.
    """
    variants = {token}
    if len(token) >= 3 and token[0] in _ATTACHED_PREFIXES:
        variants.add(token[1:])
    return variants


def entity_accuracy(
    reference: str, hypothesis: str, terms: list[str]
) -> float | None:
    """Fraction of glossary-term occurrences the hypothesis got right.

    Returns None -- not 0.0 -- when the reference contains none of the terms.
    Nothing was measured, and reporting 0 would read as total failure.
    """
    if not terms:
        return None
    ref = normalise_words(reference)
    hyp = normalise_words(hypothesis)

    expected = matched = 0
    for term in terms:
        needle = normalise_words(term)
        if not needle:
            continue
        in_ref = _count_sublist(ref, needle)
        if not in_ref:
            continue
        expected += in_ref
        matched += min(in_ref, _count_sublist(hyp, needle))

    if expected == 0:
        return None
    return matched / expected


def _count_sublist(haystack: list[str], needle: list[str]) -> int:
    """Occurrences of `needle` in `haystack`, allowing an attached prefix."""
    if not needle or len(needle) > len(haystack):
        return 0
    n = len(needle)
    hits = 0
    for i in range(len(haystack) - n + 1):
        window = haystack[i:i + n]
        if all(
            want in _token_variants(have) for have, want in zip(window, needle)
        ):
            hits += 1
    return hits


def _punctuation_tags(text: str) -> list[tuple[str, str]]:
    """(bare word, trailing punctuation) for every word, in order."""
    out = []
    for token in _strip_diacritics(text).split():
        marks = ""
        while token and unicodedata.category(token[-1]).startswith("P"):
            marks = token[-1] + marks
            token = token[:-1]
        bare = "".join(
            ch for ch in token if not unicodedata.category(ch).startswith("P")
        ).translate(FINAL_FORMS)
        if bare:
            out.append((bare, marks))
    return out


def punct_f1(reference: str, hypothesis: str) -> float | None:
    """F1 over punctuation marks, aligned by word position.

    Aligns the bare word sequences first, then compares the punctuation
    attached to each aligned pair. Marks on words that do not align at all are
    not counted either way -- that is a transcription error, and WER already
    charges for it.

    Returns None when the reference has no punctuation: there is nothing to
    score, and 0.0 would read as "got it all wrong".
    """
    ref_tags = _punctuation_tags(reference)
    hyp_tags = _punctuation_tags(hypothesis)
    if not any(marks for _, marks in ref_tags):
        return None

    tp = fp = fn = 0
    matcher = SequenceMatcher(
        a=[w for w, _ in ref_tags], b=[w for w, _ in hyp_tags], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(i2 - i1):
            ref_mark = ref_tags[i1 + offset][1]
            hyp_mark = hyp_tags[j1 + offset][1]
            if ref_mark and hyp_mark:
                if ref_mark == hyp_mark:
                    tp += 1
                else:
                    fp += 1
                    fn += 1
            elif hyp_mark:
                fp += 1
            elif ref_mark:
                fn += 1

    if tp == 0:
        return 0.0
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    return 2 * precision * recall / (precision + recall)


def boundary_f1(
    reference_starts: list[float], produced_starts: list[float], tol: float = 0.12
) -> float | None:
    """F1 over card-boundary times (D33) -- the metric for the segment pass.

    Recall alone is useless: cut after every word and every human cut is
    matched by accident. Precision punishes the spurious cuts, so the reported
    number is F1.
    """
    if not reference_starts:
        return None
    if not produced_starts:
        return 0.0

    recalled = sum(
        1 for t in reference_starts
        if any(abs(t - p) <= tol for p in produced_starts)
    )
    precise = sum(
        1 for p in produced_starts
        if any(abs(t - p) <= tol for t in reference_starts)
    )
    recall = recalled / len(reference_starts)
    precision = precise / len(produced_starts)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------
# warnings -- mechanical counting, no domain knowledge
# --------------------------------------------------------------------------


def count_warnings(warnings: list[dict]) -> dict[str, int]:
    """Group meta.warnings by code. An unknown code raises rather than vanishing.

    A warning with no column is a warning silently dropped, which is the exact
    failure the warning list exists to prevent.
    """
    counts = {code: 0 for code in WARNING_CODES}
    for entry in warnings or []:
        code = entry.get("code")
        if code not in counts:
            _fail(
                f"unrecognised warning code {code!r}; expected one of "
                f"{', '.join(WARNING_CODES)}"
            )
        counts[code] += 1
    return counts


def hebrew_rule_breakdown(warnings: list[dict]) -> str:
    """`et_split=2;function_word_line_end=5`, sorted, zero-count rules omitted.

    The rule ids are read off the front of each `detail`, never enumerated
    here -- that is what keeps the rule list in `segment` and only there (D25).
    One packed column rather than one column per rule keeps the CSV header
    stable when a Hebrew rule is added.
    """
    counts: dict[str, int] = {}
    for entry in warnings or []:
        if entry.get("code") != "hebrew_rule_violation":
            continue
        rule_id, sep, _ = (entry.get("detail") or "").partition(": ")
        if not sep or not rule_id.strip():
            _fail(
                f"hebrew_rule_violation detail must start with '<rule_id>: ', "
                f"got {entry.get('detail')!r}"
            )
        counts[rule_id.strip()] = counts.get(rule_id.strip(), 0) + 1
    return ";".join(f"{rule}={n}" for rule, n in sorted(counts.items()))


# --------------------------------------------------------------------------
# export's report -- authoritative, never recomputed
# --------------------------------------------------------------------------


def stats_columns(report: dict) -> dict:
    """Lift export's stats into CSV columns. Only pct_over_cps is derived."""
    stats = report.get("stats")
    if not isinstance(stats, dict):
        _fail("report has no 'stats' block")

    for key in REQUIRED_STATS:
        if key not in stats:
            _fail(f"report stats is missing the key {key!r}")

    cards = stats["cards"]
    over = stats["cards_over_cps"]
    return {
        "cards": cards,
        "cards_over_cps": over,
        "pct_over_cps": round(over / cards, 4) if cards else "",
        "cards_over_line_len": stats["cards_over_line_len"],
        "cards_under_min_duration": stats["cards_under_min_duration"],
        "max_cps": stats["max_cps"],
        "mean_cps": stats["mean_cps"],
    }


# --------------------------------------------------------------------------
# the eval set
# --------------------------------------------------------------------------


def _srt_starts(text: str) -> list[float]:
    out = []
    for match in _SRT_CARD.finditer(text):
        stamp = match.group(1).replace(".", ",")
        h, m, rest = stamp.split(":")
        s, ms = rest.split(",")
        out.append(int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000)
    return out


def load_reference_cards(path: Path) -> list[str]:
    """Card texts from a reference .srt, tags stripped."""
    raw = path.read_text(encoding="utf-8-sig")
    hits = list(_SRT_CARD.finditer(raw))
    cards = []
    for i, match in enumerate(hits):
        lo = match.end()
        hi = hits[i + 1].start() if i + 1 < len(hits) else len(raw)
        cards.append(" ".join(_TAG.sub("", raw[lo:hi]).split()))
    return cards


def _find_reference(refs_dir: Path, stem: str) -> tuple[str, list[float] | None]:
    """Reference text plus card starts. Prefers .txt for text, .srt for cuts."""
    srt = refs_dir / f"{stem}.srt"
    txt = refs_dir / f"{stem}.txt"

    starts = None
    text = None
    if srt.exists():
        raw = srt.read_text(encoding="utf-8-sig")
        starts = _srt_starts(raw)
        text = " ".join(load_reference_cards(srt))
    if txt.exists():
        text = txt.read_text(encoding="utf-8")
    if text is None:
        _fail(f"no reference for {stem!r} in {refs_dir} (expected .txt or .srt)")
    return text, starts


def load_glossary_terms(path: Path | None) -> list[str]:
    """Right-hand sides and bare terms -- the spellings that should appear."""
    if path is None or not path.exists():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        term = line.split("=>", 1)[1].strip() if "=>" in line else line
        if term and term not in terms:
            terms.append(term)
    return terms


# --------------------------------------------------------------------------
# one row
# --------------------------------------------------------------------------


def _blank_row() -> dict:
    return {column: "" for column in COLUMNS}


def run_clip(
    clip: Path,
    engine: str,
    *,
    refs_dir: Path,
    work_root: Path,
    terms: list[str],
    glossary=None,
    passes: str = "",
    boundary_tol: float = 0.12,
    max_chars: int | None = None,
    model: str | None = None,
) -> dict:
    """Run the pipeline over one clip and return one CSV row. Never raises."""
    from hebsub.export import export
    from hebsub.proofread import Config as ProofreadConfig
    from hebsub.proofread import Glossary, proofread
    from hebsub.segment import Config, segment
    from hebsub.transcribe import transcribe

    row = _blank_row()
    row.update({
        "clip": clip.stem,
        "engine": engine,
        "passes_enabled": passes,
        "run_at": datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "cost_usd": 0 if engine in LOCAL_ENGINES else "",
        "status": "ok",
    })

    started = time.time()
    job = work_root / f"{clip.stem}__{engine}"
    job.mkdir(parents=True, exist_ok=True)

    try:
        raw = transcribe(clip, engine=engine, model=model)
        (job / "01_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        row["engine_version"] = raw["meta"]["engine_version"]

        # Ablation lives here: `--passes ''` is the raw-ASR baseline, and a
        # pass's contribution is the diff between two rows rather than an
        # argument about it.
        enabled = tuple(p.strip() for p in passes.split(",") if p.strip())
        corrected = raw
        if enabled:
            corrected = proofread(
                raw,
                cfg=ProofreadConfig(passes=enabled),
                glossary=glossary if glossary is not None else Glossary({}, []),
            )
            (job / "02_proofread.json").write_text(
                json.dumps(corrected, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        cfg = Config(max_chars_per_card=max_chars) if max_chars else Config()
        cards = segment(corrected, cfg=cfg)
        segmented = job / "03_segmented.json"
        segmented.write_text(
            json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        srt = job / "final.srt"
        # Hand segment's own thresholds to export rather than letting each
        # module fall back to its own defaults. `cards_over_cps` and
        # `warn_cps_exceeded` are only comparable when both count against the
        # same limit; they silently did not before this was wired through.
        export(
            cards, srt, source=str(segmented),
            max_cps=cfg.max_cps,
            max_line_length=cfg.max_line_length,
            min_card_duration=cfg.min_card_duration,
        )

        # Read the report back off disk. Export is authoritative for every
        # delivered-file number and this is the only place they come from.
        report_path = srt.with_name(srt.name + ".report.json")
        if not report_path.exists():
            _fail(f"export wrote no report sidecar at {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        row.update(stats_columns(report))

        # Warnings: the pipeline's own, plus export's (it appends to no
        # Transcript, so gap_not_applied lives only in the report).
        all_warnings = list(cards["meta"].get("warnings", []))
        all_warnings.extend(report.get("warnings", []))
        counts = count_warnings(all_warnings)
        for code, n in counts.items():
            row[f"warn_{code}"] = n
        row["hebrew_rule_breakdown"] = hebrew_rule_breakdown(all_warnings)

        reference, ref_starts = _find_reference(refs_dir, clip.stem)
        hypothesis = " ".join(
            w["w"] for s in corrected["segments"] for w in s["words"]
        )
        row["wer"] = round(wer(reference, hypothesis), 4)
        row["wer_tolerant"] = round(wer_tolerant(reference, hypothesis), 4)

        accuracy = entity_accuracy(reference, hypothesis, terms)
        row["entity_accuracy"] = "" if accuracy is None else round(accuracy, 4)

        punct = punct_f1(reference, hypothesis)
        row["punct_f1"] = "" if punct is None else round(punct, 4)

        produced = [float(s["start"]) for s in cards["segments"]]
        boundary = (
            boundary_f1(ref_starts, produced, tol=boundary_tol)
            if ref_starts else None
        )
        row["boundary_f1"] = "" if boundary is None else round(boundary, 4)

    except Exception as exc:  # noqa: BLE001 -- one broken clip must not abort
        row["status"] = "failed"
        row["failure_reason"] = f"{type(exc).__name__}: {exc}"
        # Null, never zero: nothing here was measured.
        for column in METRIC_COLUMNS:
            row[column] = ""

    row["wall_clock_s"] = round(time.time() - started, 2)
    return row


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------


def append_rows(rows: list[dict], out: Path) -> None:
    """Append, never overwrite. bench.csv is a history."""
    fresh = not out.exists() or out.stat().st_size == 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        if fresh:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.bench",
        description="Run the pipeline over the eval set and score it.",
    )
    parser.add_argument("--set", dest="eval_set", required=True)
    parser.add_argument("--engines", default="ivrit_local")
    parser.add_argument("--out", default="bench.csv")
    parser.add_argument("--refs", default=None, help="default: <set>/references")
    parser.add_argument("--glossary", default=None)
    parser.add_argument("--passes", default="")
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--boundary-tol", type=float, default=0.12)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args(argv)

    eval_set = Path(args.eval_set)
    if not eval_set.is_dir():
        print(f"{MODULE}: --set {eval_set} is not a directory", file=sys.stderr)
        return 2

    refs_dir = Path(args.refs) if args.refs else eval_set / "references"
    if not refs_dir.is_dir():
        print(f"{MODULE}: no reference directory at {refs_dir}", file=sys.stderr)
        return 2

    clips = sorted(
        p for p in eval_set.iterdir()
        if p.is_file() and p.suffix.lower() in
        (".mp4", ".mov", ".mkv", ".wav", ".mp3", ".m4a", ".aac")
    )
    if not clips:
        print(f"{MODULE}: no media files in {eval_set}", file=sys.stderr)
        return 2

    # A clip with no reference is not part of THIS eval set, and recording it
    # as a failed row is a lie: nothing failed. D24's "every clip gets a row"
    # is about clips that break, not about a --set/--refs mismatch. Since
    # tests/fixtures holds both corpora, running one of them against the other's
    # references would otherwise fill bench.csv -- a history file -- with rows
    # that look like breakage.
    scored, skipped = [], []
    for clip in clips:
        has_ref = any(
            (refs_dir / f"{clip.stem}{ext}").exists() for ext in (".srt", ".txt")
        )
        (scored if has_ref else skipped).append(clip)
    if skipped:
        print(f"{MODULE}: {len(skipped)} clip(s) skipped -- no reference in "
              f"{refs_dir.name}: {', '.join(c.stem for c in skipped[:4])}"
              f"{' …' if len(skipped) > 4 else ''}")
    if not scored:
        print(f"{MODULE}: no clip in {eval_set} has a reference in {refs_dir}",
              file=sys.stderr)
        return 2
    clips = scored

    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    glossary_path = Path(args.glossary) if args.glossary else None
    terms = load_glossary_terms(glossary_path)
    from hebsub.proofread import load_glossary as _load_glossary
    glossary = _load_glossary(glossary_path)
    work_root = Path(args.work_dir) if args.work_dir else Path.cwd() / "work" / "bench"
    out = Path(args.out)

    print(f"{MODULE}: {len(clips)} clip(s) x {len(engines)} engine(s) -> {out}")
    if terms:
        print(f"{MODULE}: {len(terms)} glossary term(s) for entity_accuracy")

    rows = []
    for engine in engines:
        for clip in clips:
            row = run_clip(
                clip, engine,
                refs_dir=refs_dir,
                work_root=work_root,
                terms=terms,
                glossary=glossary,
                passes=args.passes,
                boundary_tol=args.boundary_tol,
                max_chars=args.max_chars,
                model=args.model,
            )
            rows.append(row)
            if row["status"] == "ok":
                print(
                    f"  {row['clip'][:26]:<28} WER {row['wer']:<8} "
                    f"bF1 {row['boundary_f1']:<8} cards {row['cards']:<5} "
                    f"{row['wall_clock_s']}s"
                )
            else:
                print(f"  {row['clip'][:26]:<28} FAILED  {row['failure_reason']}")

    append_rows(rows, out)

    ok = [r for r in rows if r["status"] == "ok"]
    print()
    print(f"{MODULE}: {len(ok)}/{len(rows)} ok, {len(rows)} row(s) appended to {out}")
    if ok:
        print(f"{MODULE}: mean WER "
              f"{sum(float(r['wer']) for r in ok) / len(ok):.4f}")
        scored = [r for r in ok if r["boundary_f1"] != ""]
        if scored:
            print(f"{MODULE}: mean boundary F1 "
                  f"{sum(float(r['boundary_f1']) for r in scored) / len(scored):.4f}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
