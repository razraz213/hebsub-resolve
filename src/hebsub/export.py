"""SubtitleFile -> .srt / .vtt.

Formatting and display only. Zero decisions about content, timing, or line
breaks -- those were all made upstream. The one thing export is allowed to
change is the *displayed* end of a card, via --gap, because export produces
display output rather than a Transcript.

See docs/modules/export.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from hebsub.contract import validate_subtitle_file

__all__ = [
    "render_plain_srt","export", "render_srt", "render_vtt", "build_report", "ExportError"]

MODULE = "export"
STAGE = "export"

RLM = "‏"        # RIGHT-TO-LEFT MARK
LRI = "⁦"        # LEFT-TO-RIGHT ISOLATE
PDI = "⁩"        # POP DIRECTIONAL ISOLATE

_LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'\-&/]*[A-Za-z0-9]|[A-Za-z]")
_TRAILING_PUNCT = re.compile(r"([\.\?!،,:;…]+)$")


class ExportError(Exception):
    """Raised when a SubtitleFile cannot be rendered."""


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------


def _clock(seconds: float, separator: str) -> str:
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{ms:03d}"


# --------------------------------------------------------------------------
# gap
# --------------------------------------------------------------------------


# Measured across all three of Raz's hand-corrected corpora: 96.4% of the 1,882
# gaps between his cards are exactly zero, and there is a complete void from
# 1ms to 60ms -- not one gap in that range. Above 60ms he keeps 0.4% of gaps
# up to 200ms, then a real tail of speech pauses beyond it.
#
# The segmenter meanwhile leaves 32% of its gaps in the 1-200ms band, which is
# precisely the band he never produces. 200ms closes those and costs at most 9
# of his 1,882 gaps; 300ms would close only 33 more while tripling that cost.
DEFAULT_CLOSE_GAPS_MS = 200


def _displayed_ends(
    segments: list[dict], gap_ms: int, close_gaps_ms: int = 0,
    starts: list[float] | None = None,
) -> tuple[list[float], list[dict]]:
    """Adjust each card's displayed end. Never touches a start.

    Two opposite adjustments, and only one may be asked for at a time:

    * `gap_ms` **shaves** the end to open a gap before the next card -- the
      broadcast convention.
    * `close_gaps_ms` **extends** the end to meet the next card's start when
      the gap between them is small enough to be a segmentation artifact
      rather than a pause in the speech.

    The SubtitleFile on disk is not modified by either: this exists only in
    the emitted file, which is why it can live here at all. Timestamps in the
    Transcript are immutable after transcribe (CLAUDE.md rule 2); what a
    display file shows for how long is not a Transcript timestamp.
    """
    if gap_ms > 0 and close_gaps_ms > 0:
        raise ExportError(
            f"export: gap_ms={gap_ms} opens a gap between cards and "
            f"close_gaps_ms={close_gaps_ms} closes one. They are opposite "
            f"conventions; pick one."
        )

    ends = [float(seg["end"]) for seg in segments]
    warnings: list[dict] = []
    # Meet the start the next card will actually be DISPLAYED at. Using the
    # raw start would re-open every gap by however far `_displayed_starts`
    # moved it, which is the whole point of closing them.
    shown = starts if starts is not None else [float(s["start"]) for s in segments]

    if close_gaps_ms > 0:
        limit = close_gaps_ms / 1000.0
        for i in range(len(segments) - 1):   # a last card has nothing to meet
            end = float(segments[i]["end"])
            next_start = shown[i + 1]
            gap = next_start - end
            if 1e-9 < gap <= limit + 1e-9:
                ends[i] = round(next_start, 3)
        return ends, warnings

    if gap_ms <= 0:
        return ends, warnings

    gap = gap_ms / 1000.0
    for i, seg in enumerate(segments):
        if i + 1 >= len(segments):
            continue  # a card with no follower is never shaved
        start = float(seg["start"])
        end = float(seg["end"])
        next_start = shown[i + 1]
        if next_start > end + 1e-9:
            continue  # a real gap already exists here

        # Leave at least one millisecond of duration.
        slack = max(0.0, (end - start) - 0.001)
        applied = min(gap, slack)
        ends[i] = round(end - applied, 3)
        if applied + 1e-9 < gap:
            warnings.append({
                "stage": STAGE,
                "code": "gap_not_applied",
                "wid_start": seg["words"][0]["wid"],
                "wid_end": seg["words"][-1]["wid"],
                "detail": (
                    f"requested {gap_ms}ms, applied {int(round(applied * 1000))}ms"
                ),
            })
    return ends, warnings


# --------------------------------------------------------------------------
# bidi opt-ins
# --------------------------------------------------------------------------


def _apply_rlm(line: str) -> str:
    """Anchor the line as RTL and keep trailing punctuation on the right."""
    out = _TRAILING_PUNCT.sub(lambda m: RLM + m.group(1), line)
    return RLM + out


def _apply_isolate(line: str) -> str:
    """Wrap Latin runs so an English name cannot drag Hebrew punctuation."""
    return _LATIN_RUN.sub(lambda m: f"{LRI}{m.group(0)}{PDI}", line)


# Sentence punctuation, which short-form subtitles do not display. Deliberately
# NOT including " or ' -- those are gershayim/geresh and belong to the word:
# חב"ד, חז"ל, נתב"ג, קכא'. Stripping them would corrupt the text.
# Everything Raz drops. The question mark is deliberately NOT in here.
#
# Counted across all three corrected corpora, 4,331 words: `?` 29 times,
# geresh 13, gershayim 13, and then `,` 3, `.` 2, `!` 1. Question marks are
# the only sentence punctuation he actually uses -- a question that reads as a
# statement is a different sentence, and short-form Hebrew keeps the mark for
# exactly that reason. The comma and full stop are noise at 5 in 4,331.
#
# `!` is left out on the same evidence (1 occurrence). Adding it back is one
# character here if that ever turns out to be wrong.
_DISPLAY_PUNCT = ".,;:…"

# Kept in the emitted file, though `segment` still uses it upstream.
_KEPT_PUNCT = "?"


def _strip_display_punct(line: str) -> str:
    """Drop trailing sentence punctuation from each word, for display only.

    Short-form Hebrew subtitles are not punctuated -- except for the question
    mark, which carries meaning rather than grammar. `segment` needs all of it
    to find sentence and clause boundaries (split priorities 1 and 2), so it
    survives the whole pipeline and is dropped here, where display concerns
    live and the SubtitleFile on disk is not affected.
    """
    out = []
    for token in line.split():
        stripped = token.rstrip(_DISPLAY_PUNCT)
        out.append(stripped if stripped else token)
    return " ".join(out)


def _decorate(
    lines: list[str], *, rlm: bool, isolate: bool, strip_punct: bool = False
) -> list[str]:
    out = list(lines)
    if strip_punct:
        out = [_strip_display_punct(line) for line in out]
    if isolate:
        out = [_apply_isolate(line) for line in out]
    if rlm:
        out = [_apply_rlm(line) for line in out]
    return out


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------


# Deliberately ASCII. It sits in an RTL subtitle track, so Hebrew could be
# mistaken for content and mixed scripts invite bidi reordering.
TIMING_CLIP_TEXT = ">> TIMING CLIP - DELETE ME <<"


def _timing_clip_chunk(
    segments: list[dict], first_start: float | None = None
) -> str:
    """A card from absolute zero to the first real card, or nothing.

    Resolve exposes no way to position a subtitle clip by script (D28), so the
    .srt is dragged onto the track by hand. What you get to align is the
    clip's *content*, and when the first spoken word is minutes into the
    programme there is nothing at the front to align against -- on the
    workshop timeline the first word is 2m44s in. A card starting at
    00:00:00,000 makes the clip begin where the programme begins, so snapping
    it to the timeline start is exact rather than eyeballed. The card is
    labelled for deletion because that is what it is for.

    Returns "" when the first card already starts at zero: there is no room,
    and a zero-length card is not a card.
    """
    if first_start is None and not segments:
        return ""
    first = float(segments[0]["start"]) if first_start is None else first_start
    if first <= 0:
        return ""
    return (
        f"1\n"
        f"{_clock(0.0, ',')} --> {_clock(first, ',')}\n"
        f"{TIMING_CLIP_TEXT}\n\n"
    )


def _displayed_starts(
    segments: list[dict], onset_spans: list[tuple[float, float]] | None
) -> list[float]:
    """Move a card's displayed start forward to the next moment it may begin.

    `onset_spans` are the ranges where a card is allowed to start. The host
    supplies them; every other caller passes nothing and nothing moves.
    `host_resolve` intersects two things: where there is a picture, and where
    the VAD says there is actually speech.

    Whisper's word onsets run early -- measured against Raz's corrected files,
    a median of 34ms (about one frame at 30fps) across all three corpora. At a
    speech onset after silence it is far worse, because the model takes the
    breath before the word: measured at a median of 173ms and up to 460ms,
    which puts the first card of each video **before that video exists**.

    A card starting outside every span is pulled forward to the edge of the
    next one. A card that ends before that edge is left alone -- it belongs to
    nothing, and moving its start past its own end would invert it.

    Only the displayed start moves; the SubtitleFile is untouched. Same
    standing as `_displayed_ends`.
    """
    starts = [float(seg["start"]) for seg in segments]
    if not onset_spans:
        return starts

    spans = sorted(onset_spans)
    for i, seg in enumerate(segments):
        start = starts[i]
        if any(a - 1e-6 <= start <= b + 1e-6 for a, b in spans):
            continue                       # already over a picture
        later = [a for a, _ in spans if a > start]
        if not later:
            continue                       # past the last picture; leave it
        edge = min(later)
        if edge < float(seg["end"]) - 1e-6:
            starts[i] = round(edge, 3)
    return starts


def render_plain_srt(
    cards: list[tuple[float, float, str]], *, timing_clip: bool = True
) -> str:
    """Render (start, end, text) triples as an .srt.

    A deliberate side door. The review track is not a SubtitleFile -- it has no
    words, no wids and no cps, and inventing them to satisfy the contract would
    put a fake object into the pipeline for the sake of reusing one function.
    This writes the same timestamp format and nothing else.

    It gets the same leading timing card as `render_srt`, and for the same
    reason: Resolve drops the lead-in silence when it imports an .srt, so a
    review track whose first card is 40s in would land 40s adrift of the
    subtitles it is annotating. Both files begin at absolute zero, so snapping
    both to the timeline start puts them frame-for-frame on top of each other.
    """
    lead = ""
    if timing_clip and cards:
        lead = _timing_clip_chunk([], first_start=float(cards[0][0]))
    chunks = [lead] if lead else []
    for index, (start, end, text) in enumerate(cards, start=1 + bool(lead)):
        chunks.append(
            f"{index}\n"
            f"{_clock(float(start), ',')} --> {_clock(float(end), ',')}\n"
            f"{text}\n\n"
        )
    return "".join(chunks)


def render_srt(
    obj: dict, *, gap_ms: int = 0, rlm: bool = False, isolate: bool = False,
    strip_punct: bool = False, timing_clip: bool = False,
    close_gaps_ms: int = 0,
    onset_spans: list[tuple[float, float]] | None = None,
) -> tuple[str, list[dict]]:
    segments = obj["segments"]
    starts = _displayed_starts(segments, onset_spans)
    ends, warnings = _displayed_ends(segments, gap_ms, close_gaps_ms, starts)

    lead = _timing_clip_chunk(segments, starts[0] if starts else None) \
        if timing_clip else ""
    offset = 1 if lead else 0

    chunks: list[str] = [lead] if lead else []
    for index, seg in enumerate(segments):
        lines = _decorate(
            seg["lines"], rlm=rlm, isolate=isolate, strip_punct=strip_punct
        )
        chunks.append(
            f"{index + 1 + offset}\n"
            f"{_clock(starts[index], ',')} --> {_clock(ends[index], ',')}\n"
            + "\n".join(lines)
            + "\n\n"
        )
    return "".join(chunks), warnings


def render_vtt(
    obj: dict, *, gap_ms: int = 0, rlm: bool = False, isolate: bool = False,
    strip_punct: bool = False, close_gaps_ms: int = 0,
    onset_spans: list[tuple[float, float]] | None = None,
) -> tuple[str, list[dict]]:
    segments = obj["segments"]
    starts = _displayed_starts(segments, onset_spans)
    ends, warnings = _displayed_ends(segments, gap_ms, close_gaps_ms, starts)

    chunks = ["WEBVTT\n\n"]
    for index, seg in enumerate(segments):
        lines = _decorate(
            seg["lines"], rlm=rlm, isolate=isolate, strip_punct=strip_punct
        )
        chunks.append(
            f"{index + 1}\n"
            f"{_clock(float(seg['start']), '.')} --> {_clock(ends[index], '.')}\n"
            + "\n".join(lines)
            + "\n\n"
        )
    return "".join(chunks), warnings


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


# These must match segment's Config defaults. `stats.cards_over_cps` and
# segment's `warn_cps_exceeded` are meant to be read as a pair -- segment
# saying "I could not do better" against export saying "here is what shipped"
# -- and that comparison is meaningless if the two count against different
# thresholds. D32 moved the CPS limit to 25; this followed it.
DEFAULT_MAX_CPS = 25.0
DEFAULT_MAX_LINE_LENGTH = 40
DEFAULT_MIN_CARD_DURATION = 0.4


def build_report(
    obj: dict,
    source: str,
    warnings: list[dict],
    *,
    max_cps: float = DEFAULT_MAX_CPS,
    max_line_length: int = DEFAULT_MAX_LINE_LENGTH,
    min_card_duration: float = DEFAULT_MIN_CARD_DURATION,
) -> dict:
    """Counted off the SubtitleFile as export writes it. Authoritative for bench."""
    segments = obj["segments"]
    cps_values = [float(seg["cps"]) for seg in segments]

    return {
        "source": source,
        "warnings": warnings,
        "stats": {
            "cards": len(segments),
            "cards_over_cps": sum(1 for c in cps_values if c > max_cps),
            "cards_over_line_len": sum(
                1 for seg in segments
                if any(len(line) > max_line_length for line in seg["lines"])
            ),
            # Tolerance, because a card sitting exactly on the minimum should
            # not be counted as under it: 1.4 - 1.0 is 0.3999999999999999 in
            # binary float, and a benchmark that flips on that is noise.
            "cards_under_min_duration": sum(
                1 for seg in segments
                if float(seg["end"]) - float(seg["start"]) < min_card_duration - 1e-9
            ),
            "max_cps": round(max(cps_values), 3) if cps_values else 0.0,
            "mean_cps": (
                round(sum(cps_values) / len(cps_values), 3) if cps_values else 0.0
            ),
        },
    }


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def export(
    obj: dict,
    dst: Path | str,
    *,
    fmt: str = "srt",
    gap_ms: int = 0,
    rlm: bool = False,
    isolate: bool = False,
    strip_punct: bool = False,
    timing_clip: bool = False,
    close_gaps_ms: int = 0,
    onset_spans: list[tuple[float, float]] | None = None,
    bom: bool = False,
    source: str | None = None,
    max_cps: float = DEFAULT_MAX_CPS,
    max_line_length: int = DEFAULT_MAX_LINE_LENGTH,
    min_card_duration: float = DEFAULT_MIN_CARD_DURATION,
) -> dict:
    """Write the subtitle file and its report sidecar. Returns the report."""
    validate_subtitle_file(obj)
    if fmt not in ("srt", "vtt"):
        raise ExportError(f"{MODULE}: unknown format {fmt!r}; expected srt or vtt")

    renderer = render_srt if fmt == "srt" else render_vtt
    text, warnings = renderer(
        obj, gap_ms=gap_ms, rlm=rlm, isolate=isolate, strip_punct=strip_punct,
        close_gaps_ms=close_gaps_ms, onset_spans=onset_spans,
        **({"timing_clip": timing_clip} if fmt == "srt" else {}),
    )

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so "\n" reaches the file as LF, not the platform's CRLF.
    with open(dst, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
        fh.write(text)

    report = build_report(
        obj,
        source or "<memory>",
        warnings,
        max_cps=max_cps,
        max_line_length=max_line_length,
        min_card_duration=min_card_duration,
    )
    # Whether one was actually WRITTEN, not merely asked for: a first card at
    # zero leaves no room, and .vtt never gets one.
    segs = obj["segments"]
    report["stats"]["closed_gaps"] = sum(
        1 for i in range(len(segs) - 1)
        if 1e-9 < float(segs[i + 1]["start"]) - float(segs[i]["end"])
        <= close_gaps_ms / 1000.0 + 1e-9
    ) if close_gaps_ms > 0 else 0
    report["stats"]["timing_clip"] = bool(
        timing_clip and fmt == "srt" and _timing_clip_chunk(obj["segments"])
    )
    report_path = dst.with_name(dst.name + ".report.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.export",
        description="Render a SubtitleFile to .srt or .vtt.",
    )
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", dest="dst", required=True)
    parser.add_argument("--format", dest="fmt", choices=("srt", "vtt"), default="srt")
    parser.add_argument("--gap", type=int, default=0, metavar="MS")
    parser.add_argument(
        "--close-gaps", type=int, default=0, metavar="MS",
        help=(
            "extend a card's displayed end to meet the next card when the gap "
            "between them is at most MS. 200 matches Raz's hand-cut files, "
            "where 96.4%% of cards touch. Opposite of --gap; not both."
        ),
    )
    parser.add_argument(
        "--timing-clip", action="store_true",
        help=(
            "prepend a placeholder card from 00:00:00,000 to the first real "
            "card, so the imported clip begins where the programme begins. "
            "srt only; delete the card after positioning."
        ),
    )
    parser.add_argument("--rlm", action="store_true")
    parser.add_argument("--isolate", action="store_true")
    parser.add_argument(
        "--strip-punct", action="store_true",
        help="drop sentence punctuation from the displayed text (short-form)",
    )
    parser.add_argument("--bom", action="store_true")
    args = parser.parse_args(argv)

    obj = json.loads(Path(args.src).read_text(encoding="utf-8"))
    try:
        report = export(
            obj,
            args.dst,
            fmt=args.fmt,
            gap_ms=args.gap,
            timing_clip=args.timing_clip,
            close_gaps_ms=args.close_gaps,
            rlm=args.rlm,
            isolate=args.isolate,
            strip_punct=args.strip_punct,
            bom=args.bom,
            source=args.src,
        )
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    stats = report["stats"]
    print(
        f"OK: {args.dst} written -- {stats['cards']} cards, "
        f"max CPS {stats['max_cps']}, mean {stats['mean_cps']}"
    )
    if report["warnings"]:
        print(f"  {len(report['warnings'])} warning(s) in the report sidecar")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
