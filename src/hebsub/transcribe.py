"""media -> Transcript.

Extract audio, call a swappable engine, map the result onto the contract.
Nothing else: no correction, no punctuation, no segmentation. Whatever the
engine returns is what this module reports, warts included.

See docs/modules/transcribe.md.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hebsub.contract import (
    StageAlreadyRun,
    guard_stage,
    record_stage,
    validate_transcript,
)
from hebsub.engines import EngineError, get_engine

__all__ = ["transcribe", "extract_audio", "load_vocab", "TranscribeError"]

MODULE = "transcribe"
STAGE = "transcribe"

SAMPLE_RATE = 16_000
TIMESTAMP_DECIMALS = 3


class TranscribeError(Exception):
    """Raised when transcription cannot produce a usable Transcript."""


def _fail(problem: str) -> None:
    raise TranscribeError(f"{MODULE}: {problem}")


def _round(value: float) -> float:
    return round(float(value), TIMESTAMP_DECIMALS)


# --------------------------------------------------------------------------
# audio
# --------------------------------------------------------------------------


def extract_audio(src: Path, dst: Path) -> Path:
    """Mono 16 kHz PCM WAV, which is what Whisper wants with no resampling."""
    if shutil.which("ffmpeg") is None:
        _fail("ffmpeg is not on PATH; the pipeline shells out to it for audio")
    if not src.exists():
        _fail(f"input file does not exist: {src}")

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le", str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", errors="replace").strip()[-1200:]
        _fail(
            f"ffmpeg failed with exit code {result.returncode}.\n"
            f"  command: {' '.join(cmd)}\n"
            f"  stderr tail:\n{tail}"
        )
    if not dst.exists() or dst.stat().st_size == 0:
        _fail(f"ffmpeg reported success but wrote no audio to {dst}")
    return dst


def load_vocab(path: Path | None) -> list[str] | None:
    """Read the glossary, keeping only the spellings we want the engine to produce.

    For a mapping line `wrong => right` only `right` is sent. Feeding the
    mis-transcription back to the engine as a hint would be actively harmful:
    it biases the model toward the very spelling the glossary exists to fix.
    See docs/modules/transcribe.md, `--vocab`.
    """
    if path is None:
        return None
    if not path.exists():
        _fail(f"vocab file does not exist: {path}")

    terms: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        term = line.split("=>", 1)[1].strip() if "=>" in line else line
        if term and term not in terms:
            terms.append(term)
    return terms or None


# --------------------------------------------------------------------------
# mapping engine output onto the contract
# --------------------------------------------------------------------------


_NUMBER_TAIL = re.compile(r"^[,.]\d")


def _join_number_fragments(words: list[dict]) -> list[dict]:
    """Rejoin a number the engine split at its thousands separator.

    faster-whisper hands back `75` and `,000` as two separate word tokens, so
    "75,000 שקל" reaches the screen as "75 ,000 שקל" -- with a card boundary
    free to fall between them. Every price, date and statistic is exposed to
    this, which for business content is most of the numbers on screen.

    Only joins when the previous token ends in a digit and this one is a
    separator followed by digits, so a plain comma after a number ("יש 5,
    אבל") is left alone.

    This runs BEFORE wids are assigned. transcribe is the stage that decides
    what a word is; doing it later would change word count, which the contract
    forbids for every stage after this one.
    """
    out: list[dict] = []
    for word in words:
        text = (word.get("w") or "").strip()
        if (
            out
            and _NUMBER_TAIL.match(text)
            and (out[-1].get("w") or "").strip()[-1:].isdigit()
        ):
            previous = out[-1]
            previous["w"] = (previous["w"] or "").strip() + text
            previous["end"] = word["end"]
            # the joined number is only as trustworthy as its worst half
            confs = [c for c in (previous.get("conf"), word.get("conf"))
                     if c is not None]
            previous["conf"] = min(confs) if confs else None
            continue
        out.append(dict(word))
    return out


# Marks that can carry the geresh role: the ASCII apostrophe faster-whisper
# actually emits, plus the Hebrew geresh and the typographic quote, so the
# fix survives an engine swap.
_GERESH_MARKS = "'׳’"

# The letters a geresh actually marks in modern Hebrew: it stands for a sound
# the alphabet has no letter for -- ג' is *j*, צ'/ץ' is *ch*, ז' is *zh* --
# so the mark lives inside the word rather than between two. Four letters, not
# the wider transliteration set, and the narrowness IS the guard: it is what
# keeps a genuine quotation ("אמר 'שלום") from being glued onto the word
# before it, since ר takes no geresh.
#
# Known limit, stated rather than hidden: a quotation opening straight after a
# word that does end in one of these -- "אז 'שלום" -- would still be joined.
# Measured across 7,300 words of real engine output (all three corpora), every
# token beginning with a geresh was a broken word and not one was a quotation,
# so the trade is worth taking. Whisper punctuates Hebrew speech with
# gershayim, and essentially never emits an opening single quote.
_GERESH_LETTERS = frozenset("גזצץ")

_GERESH_HEAD = re.compile(f"^[{_GERESH_MARKS}][א-ת]")


def _is_geresh_stem(text: str) -> bool:
    """True when `text` could be the first half of a geresh-split word.

    Prefixes come along for free: Hebrew fuses prepositions onto the front, so
    a split leaves "לג" or "הבירצ" as often as a bare "ג", and all of them end
    in the letter that carries the mark.
    """
    return bool(text) and text[-1] in _GERESH_LETTERS


def _join_geresh_fragments(words: list[dict]) -> list[dict]:
    """Rejoin a word the engine split at its geresh.

    faster-whisper hands back `ג'יפ` as `ג` and `'יפ`, so "לג'יפ" reaches the
    screen as "לג 'יפ" -- two words where there is one, with a card boundary
    free to fall between them. Measured on Raz's tools timeline this was 8 of
    81 word errors, the single largest cause in the file, and it hits every
    loanword Hebrew spells with a geresh: ג'וב, צ'ק, ז'אנר, ג'ינס.

    Same reasoning as `_join_number_fragments`, and the same placement: this
    runs BEFORE wids are assigned, because transcribe is the stage that
    decides what a word is. Doing it later would change the word count, which
    the contract forbids for every stage after this one.
    """
    out: list[dict] = []
    for word in words:
        text = (word.get("w") or "").strip()
        if (
            out
            and _GERESH_HEAD.match(text)
            and _is_geresh_stem((out[-1].get("w") or "").strip())
        ):
            previous = out[-1]
            previous["w"] = (previous["w"] or "").strip() + text
            previous["end"] = word["end"]
            # the joined word is only as trustworthy as its worse half
            confs = [c for c in (previous.get("conf"), word.get("conf"))
                     if c is not None]
            previous["conf"] = min(confs) if confs else None
            continue
        out.append(dict(word))
    return out


def _clamp_words(
    raw_segments: list[dict], warnings: list[dict]
) -> list[list[dict]]:
    """Assign wids and force monotonic timings, warning once per clamp.

    Whisper emits overlapping or backwards word timings often enough that
    raising would make the default engine unusable. Immutability begins the
    moment transcribe returns -- so this is the only place in the pipeline
    where a timestamp may move, and every move is recorded.
    """
    out: list[list[dict]] = []
    wid = 0
    previous_end = 0.0

    for seg in raw_segments:
        words: list[dict] = []
        for raw in _join_geresh_fragments(
                _join_number_fragments(seg.get("words", []))
        ):
            text = (raw.get("w") or "").strip()
            if not text:
                continue  # engines emit stray whitespace tokens; they are not words

            start = _round(raw["start"])
            end = _round(raw["end"])

            if start < previous_end:
                warnings.append({
                    "stage": STAGE,
                    "code": "timing_clamped",
                    "wid_start": wid,
                    "wid_end": wid,
                    "detail": (
                        f"start {start:.3f} overlapped the previous word's end "
                        f"{previous_end:.3f}; clamped to {previous_end:.3f}"
                    ),
                })
                start = previous_end

            if end <= start:
                # A zero or negative span cannot exist in the contract. One
                # millisecond is the smallest representable positive width.
                clamped = _round(start + 0.001)
                warnings.append({
                    "stage": STAGE,
                    "code": "timing_clamped",
                    "wid_start": wid,
                    "wid_end": wid,
                    "detail": (
                        f"end {end:.3f} was not after start {start:.3f}; "
                        f"clamped to {clamped:.3f}"
                    ),
                })
                end = clamped

            words.append({
                "wid": wid,
                "w": text,
                "start": start,
                "end": end,
                "conf": None if raw.get("conf") is None else float(raw["conf"]),
            })
            previous_end = end
            wid += 1

        if words:
            out.append(words)

    return out


def _build_transcript(
    raw: dict, source_file: str, duration: float, warnings: list[dict]
) -> dict:
    grouped = _clamp_words(raw.get("segments", []), warnings)
    if not grouped:
        _fail(
            "engine returned no words with timings. Word timings are "
            "mandatory; segment cannot do its job without them"
        )

    segments = []
    for index, words in enumerate(grouped):
        segments.append({
            "id": index,
            "start": words[0]["start"],
            "end": words[-1]["end"],
            "text": " ".join(w["w"] for w in words),
            "words": words,
            "speaker": None,
        })

    last_end = segments[-1]["end"]
    return {
        "meta": {
            "source_file": source_file,
            "duration": _round(max(duration, last_end)),
            "language": raw.get("language") or "he",
            "engine": raw["engine"],
            "engine_version": raw["version"],
            "created_at": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            "stages": [STAGE],
            "warnings": warnings,
        },
        "segments": segments,
        "edits": [],
    }


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def transcribe(
    src: Path | str,
    *,
    engine: str = "ivrit_local",
    model: str | None = None,
    vocab: Path | str | None = None,
    force: bool = False,
    existing: dict | None = None,
    keep_wav: Path | None = None,
) -> dict:
    """Transcribe `src` and return a Transcript dict."""
    if existing is not None:
        guard_stage(existing, STAGE, force=force)

    src = Path(src)
    terms = load_vocab(Path(vocab) if vocab else None)

    try:
        adapter = get_engine(engine, model=model) if model else get_engine(engine)
    except EngineError as exc:
        raise TranscribeError(str(exc)) from exc

    tmpdir = None
    try:
        if keep_wav is not None:
            keep_wav.parent.mkdir(parents=True, exist_ok=True)
            wav = extract_audio(src, keep_wav)
        else:
            tmpdir = tempfile.TemporaryDirectory(prefix="hebsub_")
            wav = extract_audio(src, Path(tmpdir.name) / "audio.wav")

        try:
            raw = adapter.transcribe(str(wav), terms)
        except EngineError as exc:
            raise TranscribeError(str(exc)) from exc
    finally:
        if tmpdir is not None:
            tmpdir.cleanup()

    if not raw.get("segments"):
        _fail(f"engine {engine!r} returned no segments for {src.name}")

    warnings: list[dict] = []
    obj = _build_transcript(raw, src.name, float(raw.get("duration") or 0.0), warnings)
    validate_transcript(obj)
    return obj


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.transcribe",
        description="Transcribe audio or video into a Transcript JSON.",
    )
    parser.add_argument("--in", dest="src", required=True, help="input media file")
    parser.add_argument("--out", dest="dst", required=True, help="output JSON path")
    parser.add_argument("--engine", default="ivrit_local")
    parser.add_argument("--model", default=None, help="override the engine's model")
    parser.add_argument("--vocab", default=None, help="glossary file")
    parser.add_argument("--force", action="store_true", help="re-run this stage")
    parser.add_argument(
        "--keep-wav", default=None, help="write the extracted WAV here instead of a temp dir"
    )
    args = parser.parse_args(argv)

    dst = Path(args.dst)
    existing = None
    if dst.exists():
        try:
            existing = json.loads(dst.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = None

    try:
        obj = transcribe(
            args.src,
            engine=args.engine,
            model=args.model,
            vocab=args.vocab,
            force=args.force,
            existing=existing,
            keep_wav=Path(args.keep_wav) if args.keep_wav else None,
        )
    except StageAlreadyRun as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except TranscribeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    words = sum(len(s["words"]) for s in obj["segments"])
    print(
        f"OK: {dst} written -- {len(obj['segments'])} segments, {words} words, "
        f"{len(obj['meta']['warnings'])} warning(s)"
    )
    for warn in obj["meta"]["warnings"]:
        print(f"  [{warn['code']}] {warn['detail']}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
