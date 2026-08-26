"""The contract boundary.

Every stage of the pipeline calls into here on its input *and* its output.
This is how a broken stage gets caught at the boundary instead of three
stages later.

The rules enforced here come from docs/contracts.md, which is frozen. This
module deliberately validates the schema *as written*: unknown fields are
rejected, and the `edits` reason enum is exactly the four documented values.
If a stage needs a field the schema does not have, that is a schema change
task, not a loosening of this file.

What this module does NOT do, on purpose:

- It does not check that `meta.stages` is ordered or free of duplicates.
  The spec says stages exist so a module can detect being run twice or out
  of order; that check belongs to the module making the decision.
- It does not check that `edits[].segment_id` points at a segment that
  still exists. `segment` renumbers ids, which strands earlier edits. The
  schema has no answer for that, so neither does this validator.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "ContractError",
    "StageAlreadyRun",
    "WARNING_CODES",
    "guard_stage",
    "record_stage",
    "validate_transcript",
    "validate_subtitle_file",
]

MODULE = "contract"

TOP_LEVEL_KEYS = frozenset({"meta", "segments", "edits"})
META_KEYS = frozenset(
    {
        "source_file",
        "duration",
        "language",
        "engine",
        "engine_version",
        "created_at",
        "stages",
        "warnings",
    }
)
META_STRING_KEYS = ("source_file", "language", "engine", "engine_version")
SEGMENT_KEYS = frozenset({"id", "start", "end", "text", "words", "speaker"})
SUBTITLE_KEYS = frozenset({"lines", "cps"})
WORD_KEYS = frozenset({"wid", "w", "start", "end", "conf"})
EDIT_KEYS = frozenset({"stage", "wid", "before", "after", "reason"})
# `second_opinion` added by D62: a second ASR model transcribed the word
# differently, and exactly one of the two candidates is a real Hebrew
# word. Enum amendment inside v2, same standing as `hebrew_rule_violation`
# (D23/D25) -- it breaks no consumer of the v2 schema.
EDIT_REASONS = ("glossary", "punctuation", "llm", "second_opinion")

WARNING_REQUIRED_KEYS = frozenset({"stage", "code", "detail"})
WARNING_OPTIONAL_KEYS = frozenset({"wid_start", "wid_end"})
# docs/contracts.md "Codes in v1" -- exactly these eight, per D16/D17/D25.
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

TIMESTAMP_DECIMALS = 3
MAX_LINES_PER_CARD = 2

# Timestamps carry 3 decimals, so exact float comparison is safe to within
# well under a millisecond. This guards the last bit of binary rounding.
EPSILON = 1e-9

# `cps` is documented by formula but shown in the spec example rounded to one
# decimal place, so accept anything within that rounding error.
CPS_TOLERANCE = 0.05 + EPSILON


class ContractError(Exception):
    """Raised when an object does not satisfy docs/contracts.md."""


class StageAlreadyRun(Exception):
    """Raised when a stage is asked to run twice without --force.

    Lives here rather than in any one stage because the rule is a property of
    `meta.stages`, which is contract territory, and every stage needs it.
    """


def guard_stage(obj: Any, stage: str, *, force: bool = False) -> None:
    """Raise StageAlreadyRun if `stage` already appears in meta.stages.

    With `force=True` this is a no-op: the caller has said it means to run
    again. See docs/contracts.md, "Re-running a stage".
    """
    if force:
        return
    stages = (obj or {}).get("meta", {}).get("stages", [])
    if stage in stages:
        raise StageAlreadyRun(
            f"{stage}: already present in meta.stages ({', '.join(stages)}); "
            f"pass --force to run it again"
        )


def record_stage(obj: Any, stage: str) -> None:
    """Append `stage` to meta.stages, without duplicating it.

    Re-running with --force must not append a second copy -- see
    docs/contracts.md, "Re-running a stage".
    """
    stages = obj["meta"].setdefault("stages", [])
    if stage not in stages:
        stages.append(stage)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _fail(path: str, problem: str) -> None:
    raise ContractError(f"{MODULE}: {path} {problem}")


def _is_number(value: Any) -> bool:
    # bool is a subclass of int; it is never a valid number here.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_keys(obj: Any, path: str, required: frozenset[str]) -> None:
    if not isinstance(obj, dict):
        _fail(path, f"must be an object, got {type(obj).__name__}")
    missing = sorted(required - obj.keys())
    if missing:
        _fail(path, f"is missing required key(s): {', '.join(missing)}")
    unexpected = sorted(obj.keys() - required)
    if unexpected:
        _fail(path, f"has unexpected key(s): {', '.join(unexpected)}")


def _check_non_empty_string(value: Any, path: str) -> None:
    if not isinstance(value, str):
        _fail(path, f"must be a string, got {type(value).__name__}")
    if not value.strip():
        _fail(path, "must be a non-empty string")


def _check_timestamp(value: Any, path: str) -> float:
    if not _is_number(value):
        _fail(path, f"must be a number of seconds, got {type(value).__name__}")
    seconds = float(value)
    if not math.isfinite(seconds):
        _fail(path, f"must be finite, got {value!r}")
    if seconds < 0:
        _fail(path, f"must be >= 0, got {value!r}")
    if round(seconds, TIMESTAMP_DECIMALS) != seconds:
        _fail(
            path,
            f"must have at most {TIMESTAMP_DECIMALS} decimal places, "
            f"got {value!r}",
        )
    return seconds


def _check_span(start: Any, end: Any, path: str) -> tuple[float, float]:
    lo = _check_timestamp(start, f"{path}.start")
    hi = _check_timestamp(end, f"{path}.end")
    if hi <= lo:
        _fail(f"{path}.end", f"must be greater than start ({lo!r}), got {hi!r}")
    return lo, hi


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


def _validate_meta(meta: Any) -> None:
    _check_keys(meta, "meta", META_KEYS)

    for key in META_STRING_KEYS:
        _check_non_empty_string(meta[key], f"meta.{key}")

    duration = meta["duration"]
    if not _is_number(duration):
        _fail("meta.duration", f"must be a number, got {type(duration).__name__}")
    if not math.isfinite(float(duration)) or float(duration) <= 0:
        _fail("meta.duration", f"must be a positive number, got {duration!r}")

    created_at = meta["created_at"]
    if not isinstance(created_at, str):
        _fail(
            "meta.created_at",
            f"must be an ISO 8601 string, got {type(created_at).__name__}",
        )
    try:
        datetime.fromisoformat(created_at)
    except ValueError:
        _fail("meta.created_at", f"must be ISO 8601, got {created_at!r}")

    stages = meta["stages"]
    if not isinstance(stages, list):
        _fail("meta.stages", f"must be a list, got {type(stages).__name__}")
    if not stages:
        _fail("meta.stages", "must name at least one stage")
    for i, stage in enumerate(stages):
        if not isinstance(stage, str) or not stage.strip():
            _fail("meta.stages", f"[{i}] must be a non-empty string, got {stage!r}")

    warnings = meta["warnings"]
    if not isinstance(warnings, list):
        _fail("meta.warnings", f"must be a list, got {type(warnings).__name__}")
    for i, entry in enumerate(warnings):
        _validate_warning(entry, f"meta.warnings[{i}]")


def _validate_warning(entry: Any, path: str) -> None:
    """Enforce the warning object shape from docs/contracts.md.

    An unknown `code` is a hard failure, not a pass-through. A warning nobody
    has a column for is a warning that gets silently dropped downstream, which
    is the exact failure the warning list exists to prevent.
    """
    if not isinstance(entry, dict):
        _fail(path, f"must be an object, got {type(entry).__name__}")

    missing = sorted(WARNING_REQUIRED_KEYS - entry.keys())
    if missing:
        _fail(path, f"is missing required key(s): {', '.join(missing)}")
    unexpected = sorted(
        entry.keys() - WARNING_REQUIRED_KEYS - WARNING_OPTIONAL_KEYS
    )
    if unexpected:
        _fail(path, f"has unexpected key(s): {', '.join(unexpected)}")

    _check_non_empty_string(entry["stage"], f"{path}.stage")
    _check_non_empty_string(entry["detail"], f"{path}.detail")

    code = entry["code"]
    if code not in WARNING_CODES:
        _fail(
            f"{path}.code",
            f"must be one of {', '.join(WARNING_CODES)}; got {code!r}",
        )

    has_start = "wid_start" in entry
    has_end = "wid_end" in entry
    if has_start != has_end:
        present, absent = (
            ("wid_start", "wid_end") if has_start else ("wid_end", "wid_start")
        )
        _fail(path, f"has {present} without {absent}; both must be present or neither")
    if has_start:
        for key in ("wid_start", "wid_end"):
            value = entry[key]
            if not _is_int(value):
                _fail(f"{path}.{key}", f"must be an integer, got {type(value).__name__}")
            if value < 0:
                _fail(f"{path}.{key}", f"must be >= 0, got {value!r}")
        if entry["wid_start"] > entry["wid_end"]:
            _fail(
                path,
                f"must have wid_start <= wid_end, got {entry['wid_start']!r} > "
                f"{entry['wid_end']!r}",
            )


# --------------------------------------------------------------------------
# words and segments
# --------------------------------------------------------------------------


def _validate_word(word: Any, path: str) -> tuple[float, float]:
    _check_keys(word, path, WORD_KEYS)

    wid = word["wid"]
    if not _is_int(wid):
        _fail(f"{path}.wid", f"must be an integer, got {type(wid).__name__}")
    if wid < 0:
        _fail(f"{path}.wid", f"must be >= 0, got {wid!r}")

    text = word["w"]
    if not isinstance(text, str):
        _fail(f"{path}.w", f"must be a string, got {type(text).__name__}")
    if not text:
        _fail(f"{path}.w", "must be a non-empty string")
    if any(ch.isspace() for ch in text):
        _fail(
            f"{path}.w",
            f"must not contain whitespace (it would break the text "
            f"invariant), got {text!r}",
        )

    conf = word["conf"]
    if conf is not None:
        if not _is_number(conf):
            _fail(
                f"{path}.conf",
                f"must be a number in [0.0, 1.0] or null, "
                f"got {type(conf).__name__}",
            )
        if not 0.0 <= float(conf) <= 1.0:
            _fail(f"{path}.conf", f"must be within [0.0, 1.0], got {conf!r}")

    return _check_span(word["start"], word["end"], path)


def _validate_segment(segment: Any, path: str, *, subtitle: bool) -> None:
    required = SEGMENT_KEYS | SUBTITLE_KEYS if subtitle else SEGMENT_KEYS
    _check_keys(segment, path, required)

    seg_id = segment["id"]
    if not _is_int(seg_id):
        _fail(f"{path}.id", f"must be an integer, got {type(seg_id).__name__}")
    if seg_id < 0:
        _fail(f"{path}.id", f"must be >= 0, got {seg_id!r}")

    speaker = segment["speaker"]
    if speaker is not None:
        _check_non_empty_string(speaker, f"{path}.speaker")

    seg_start, seg_end = _check_span(segment["start"], segment["end"], path)

    words = segment["words"]
    if not isinstance(words, list):
        _fail(f"{path}.words", f"must be a list, got {type(words).__name__}")
    if not words:
        _fail(
            f"{path}.words",
            "must contain at least one word; word timings are mandatory",
        )

    spans = [
        _validate_word(word, f"{path}.words[{i}]") for i, word in enumerate(words)
    ]

    text = segment["text"]
    if not isinstance(text, str):
        _fail(f"{path}.text", f"must be a string, got {type(text).__name__}")
    expected = " ".join(word["w"] for word in words)
    if text != expected:
        _fail(
            f"{path}.text",
            f"must equal the space-joined words; expected {expected!r}, "
            f"got {text!r}",
        )

    # A segment's bounds must contain its own words. Strict equality is only
    # required of `segment` output, and is that module's test to make.
    if seg_start > spans[0][0] + EPSILON:
        _fail(
            f"{path}.start",
            f"({seg_start!r}) must not begin after its first word "
            f"({spans[0][0]!r})",
        )
    if seg_end + EPSILON < spans[-1][1]:
        _fail(
            f"{path}.end",
            f"({seg_end!r}) must not end before its last word "
            f"({spans[-1][1]!r})",
        )

    for i in range(1, len(spans)):
        if spans[i][0] + EPSILON < spans[i - 1][1]:
            _fail(
                f"{path}.words[{i}].start",
                f"must be non-decreasing: {spans[i][0]!r} precedes the end of "
                f"{path}.words[{i - 1}] ({spans[i - 1][1]!r})",
            )

    if subtitle:
        _validate_subtitle_fields(segment, path, seg_start, seg_end, text)


def _validate_subtitle_fields(
    segment: dict, path: str, start: float, end: float, text: str
) -> None:
    lines = segment["lines"]
    if not isinstance(lines, list):
        _fail(f"{path}.lines", f"must be a list, got {type(lines).__name__}")
    if not 1 <= len(lines) <= MAX_LINES_PER_CARD:
        _fail(
            f"{path}.lines",
            f"must have 1 or {MAX_LINES_PER_CARD} entries, got {len(lines)}",
        )
    for i, line in enumerate(lines):
        if not isinstance(line, str):
            _fail(
                f"{path}.lines[{i}]",
                f"must be a string, got {type(line).__name__}",
            )
        if not line.strip():
            _fail(f"{path}.lines[{i}]", "must be a non-empty string")

    rejoined = " ".join(lines)
    if rejoined != text:
        _fail(
            f"{path}.lines",
            f"must reproduce the segment text when joined with a space; "
            f"expected {text!r}, got {rejoined!r}",
        )

    cps = segment["cps"]
    if not _is_number(cps):
        _fail(f"{path}.cps", f"must be a number, got {type(cps).__name__}")
    # The separator counts (D12), matching the `text` invariant. This is
    # " ".join, not "".join -- a two-line card is measured on the same string
    # the contract validates above.
    expected = len(" ".join(lines)) / (end - start)
    if abs(float(cps) - expected) > CPS_TOLERANCE:
        _fail(
            f"{path}.cps",
            f"must equal len(' '.join(lines)) / (end - start) = "
            f"{expected:.3f}, got {cps!r}",
        )


# --------------------------------------------------------------------------
# edits
# --------------------------------------------------------------------------


def _validate_edit(entry: Any, path: str) -> None:
    _check_keys(entry, path, EDIT_KEYS)

    for key in ("stage", "before", "after"):
        value = entry[key]
        if not isinstance(value, str):
            _fail(
                f"{path}.{key}",
                f"must be a string, got {type(value).__name__}",
            )
    if not entry["stage"].strip():
        _fail(f"{path}.stage", "must be a non-empty string")

    wid = entry["wid"]
    if not _is_int(wid):
        _fail(f"{path}.wid", f"must be an integer, got {type(wid).__name__}")
    if wid < 0:
        _fail(f"{path}.wid", f"must be >= 0, got {wid!r}")

    reason = entry["reason"]
    if reason not in EDIT_REASONS:
        _fail(
            f"{path}.reason",
            f"must be one of {', '.join(EDIT_REASONS)}; got {reason!r}",
        )


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def _validate(obj: Any, *, subtitle: bool) -> None:
    label = "subtitle file" if subtitle else "transcript"
    if not isinstance(obj, dict):
        _fail(label, f"must be an object, got {type(obj).__name__}")
    _check_keys(obj, label, TOP_LEVEL_KEYS)

    _validate_meta(obj["meta"])

    segments = obj["segments"]
    if not isinstance(segments, list):
        _fail("segments", f"must be a list, got {type(segments).__name__}")

    for i, segment in enumerate(segments):
        _validate_segment(segment, f"segments[{i}]", subtitle=subtitle)

    seen: dict[int, int] = {}
    for i, segment in enumerate(segments):
        seg_id = segment["id"]
        if seg_id in seen:
            _fail(
                f"segments[{i}].id",
                f"duplicates the id of segments[{seen[seg_id]}] ({seg_id})",
            )
        seen[seg_id] = i

    # `wid` is the only durable identity a word has: unique across the whole
    # file and strictly increasing in document order. No stage may renumber or
    # reuse one, and no stage after transcribe may reorder words -- so a wid
    # that repeats or goes backwards means an earlier stage corrupted the
    # audit trail, whatever else still looks correct.
    #
    # Not checked here: that wids start at 0 and have no gaps. That is
    # transcribe's own contract with itself, and asserting it here would
    # reject a legitimately sliced artifact.
    previous_wid: int | None = None
    previous_where = ""
    for i, segment in enumerate(segments):
        for j, word in enumerate(segment["words"]):
            where = f"segments[{i}].words[{j}]"
            wid = word["wid"]
            if previous_wid is not None and wid <= previous_wid:
                _fail(
                    f"{where}.wid",
                    f"({wid}) must be strictly greater than the wid of "
                    f"{previous_where} ({previous_wid}); wids are unique and "
                    f"never reordered",
                )
            previous_wid, previous_where = wid, where

    for i in range(1, len(segments)):
        previous, current = segments[i - 1], segments[i]
        if float(current["start"]) + EPSILON < float(previous["end"]):
            _fail(
                f"segments[{i}].start",
                f"must be non-decreasing: {current['start']!r} precedes the "
                f"end of segments[{i - 1}] ({previous['end']!r})",
            )

    edits = obj["edits"]
    if not isinstance(edits, list):
        _fail("edits", f"must be a list, got {type(edits).__name__}")
    for i, entry in enumerate(edits):
        _validate_edit(entry, f"edits[{i}]")


def validate_transcript(obj: Any) -> None:
    """Raise ContractError unless `obj` is a valid Transcript."""
    _validate(obj, subtitle=False)


def validate_subtitle_file(obj: Any) -> None:
    """Raise ContractError unless `obj` is a valid SubtitleFile."""
    _validate(obj, subtitle=True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.contract",
        description="Validate a Transcript or SubtitleFile JSON artifact.",
    )
    parser.add_argument(
        "--in",
        dest="path",
        required=True,
        help="path to the JSON artifact to validate",
    )
    parser.add_argument(
        "--kind",
        choices=("transcript", "subtitle"),
        default="transcript",
        help="which contract to check against (default: transcript)",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"{MODULE}: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"{MODULE}: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    validator = (
        validate_subtitle_file if args.kind == "subtitle" else validate_transcript
    )
    try:
        validator(obj)
    except ContractError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    count = len(obj["segments"])
    print(f"OK: {path} is a valid {args.kind} ({count} segments)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
