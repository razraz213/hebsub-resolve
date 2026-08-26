"""Transcript -> SubtitleFile.

Regroups words into subtitle cards and splits each card into 1 or 2 display
lines. This is where Hebrew subtitle tools usually fail, and it is the only
stage that decides where text breaks -- so it is also the only stage that
knows when a break was linguistically illegal, and says so (D25).

Timestamps are never invented here. A card's start is some word's start and
its end is some word's end, always. See docs/modules/segment.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

from hebsub.contract import (
    StageAlreadyRun,
    guard_stage,
    record_stage,
    validate_subtitle_file,
    validate_transcript,
)

__all__ = ["segment", "SegmentError", "Config", "HEBREW_RULE_IDS"]

MODULE = "segment"
STAGE = "segment"


class SegmentError(Exception):
    """Raised when segmentation cannot satisfy a hard rule."""


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


class Config:
    """Segmentation constraints. Defaults are the D30 short-form profile."""

    def __init__(
        self,
        # 14, not 15, and not by taste: measured against Raz's 650 hand-cut
        # cards across the nine reels. Boundary F1 against his cuts peaks at
        # 14 (59.3%), and the same setting independently reproduces his card
        # shape -- 668 cards vs his 650, mean 10.5 chars vs 10.4, mean 0.76s
        # vs 0.80s. Wider caps agree steadily worse: 18 drops F1 to 48%.
        # See D33.
        max_chars_per_card: int = 14,
        max_line_length: int = 40,
        max_card_duration: float = 6.0,
        # 15 chars is roughly three Hebrew words, which is ~0.8s of speech.
        # The broadcast-subtitle default of 1.0s would therefore mark almost
        # every card as too short and merge the D30 profile back into
        # conventional subtitles. Short-form cards are meant to flash.
        min_card_duration: float = 0.4,
        # 17 CPS is the broadcast-subtitle reading-comfort norm, and it is the
        # wrong instrument here. At ~15 chars synced to speech, CPS restates
        # the speaker's rate rather than measuring anything segment controls:
        # it cannot slow speech down, so every such warning is unactionable.
        # Measured over the nine reels, a 17 limit fired on 157 of 624 cards
        # (25%) -- enough noise to bury the warnings that do matter. 25 flags
        # only genuinely extreme cards. Pass --max-cps 17 for a broadcast job.
        max_cps: float = 25.0,
        max_lines: int = 2,
        # What an extra card costs the optimiser. 0 makes it indifferent
        # between one good card and two adequate ones.
        #
        # 0.5, and chosen honestly: the two corpora peak in different places
        # (1.0 on corpus 1, 0.0 on the held-out corpus 2), which is the signal
        # that neither peak is real. The curve is nearly flat -- 1.8 points
        # across the whole sweep on corpus 1 -- so the penalty is not what
        # earned the gain; the optimiser and the silence-gap weighting are.
        # 0.5 sits at corpus 2's peak and within 0.1 of corpus 1's, so it is
        # the setting that does not favour one speaker. See D40.
        card_penalty: float = 0.5,
        # How far above the target a card may go when the phrase demands it.
        #
        # OFF BY DEFAULT, and the reason is worth keeping. The hypothesis was
        # good: 20.5% of Raz's hand-cut cards on the workshop timeline are
        # wider than 14 and the widest is 24, so a hard wall at 14 forbids one
        # card in five outright. Swept over all three corpora it does not pay.
        # Allowing wide cards recovers some boundaries and loses more:
        #
        #        headroom  cost   corpus 1  corpus 2  corpus 3   mean
        #               0     -      65.1%     66.9%     55.2%  62.4%
        #               4   1.5      62.7%     64.4%     59.0%  62.0%
        #               4   4.0      64.2%     66.1%     57.3%  62.5%
        #
        # No setting wins everywhere, because the corpora genuinely differ:
        # 20.5% of the workshop cards run past 14 against 7.8% and 8.7% in the
        # other two. Width is a property of the content, not a constant, and
        # guessing it globally costs more than it earns.
        #
        # Kept as a knob rather than deleted: on workshop-style content --
        # dense technical nouns, long loanwords -- `width_headroom=4` with
        # `over_target_cost=1.5` is worth +3.8 F1. See D41.
        width_headroom: int = 0,
        # Cost per character above the target. This is what keeps the headroom
        # from becoming the new width: going wide has to buy something -- a
        # pause held, a construct chain kept whole -- or it does not happen.
        # Inert while `width_headroom` is 0.
        over_target_cost: float = 1.5,
    ) -> None:
        # The width the optimiser aims for. Not a wall: see `width_headroom`.
        self.target_chars_per_card = max_chars_per_card
        self.width_headroom = width_headroom
        self.over_target_cost = over_target_cost

        # Hard rules win over the target. A card budget wider than
        # max_lines * max_line_length cannot be laid out without either a
        # third line or an over-long one, and both are invariant violations --
        # so the budget is bounded by what the hard rules can actually hold.
        # At the D30 defaults (24 vs 2*40) this never binds.
        self.max_chars_per_card = min(
            max_chars_per_card + width_headroom, max_lines * max_line_length
        )
        self.requested_max_chars_per_card = max_chars_per_card
        self.max_line_length = max_line_length
        self.max_card_duration = max_card_duration
        self.min_card_duration = min_card_duration
        self.max_cps = max_cps
        self.max_lines = max_lines
        self.card_penalty = card_penalty


# --------------------------------------------------------------------------
# Hebrew
# --------------------------------------------------------------------------

# Rule ids are the grouping key in the warning, and the only handle `bench`
# gets on Hebrew linguistics (D25). The prose after "<id>: " is free-form;
# the id is not.
HEBREW_RULE_IDS = (
    "et_split",
    "function_word_line_end",
    "construct_chain_split",
    "number_unit_split",
    "english_phrase_split",
)

# One- and two-letter function words that read as an error when stranded at
# the end of a card. Deliberately conservative: only words that are *always*
# function words, never a content word that happens to be short.
FUNCTION_WORDS = frozenset({
    "ב", "ל", "מ", "ה", "ו", "כ", "ש",
    "של", "עם", "על", "כי", "אם", "אל", "מן", "לא", "גם", "או", "אז",
})

ET = "את"

# Prefix letters Hebrew glues onto the front of a word. Needed so the
# construct-chain list matches `לבית כנסת` as well as `בית כנסת`.
ATTACHED_PREFIXES = frozenset("בלכמשהו")

# Units that must not be separated from the number in front of them.
UNITS = frozenset({
    "אחוז", "אחוזים", "מיליון", "מיליארד", "אלף", "אלפים", "מאות", "עשרות",
    "שקל", "שקלים", "דולר", "דולרים", "יום", "ימים", "שנה", "שנים",
    "שעה", "שעות", "דקה", "דקות", "חודש", "חודשים", "קילו", "מטר",
})

# Construct chains (סמיכות) are not reliably detectable without morphology,
# which this module deliberately does not carry. A short list of common pairs
# catches the ones that actually show up and keeps the rule honest about its
# own reach -- see docs/modules/segment.md.
CONSTRUCT_CHAINS = frozenset({
    ("בית", "ספר"), ("בית", "חולים"), ("בית", "כנסת"), ("בית", "משפט"),
    ("מנהל", "שיווק"), ("עורך", "דין"), ("ראש", "ממשלה"), ("כדור", "רגל"),
    ("ברכת", "המזון"), ("תפילת", "הדרך"), ("עילוי", "נשמת"),
    ("בן", "אדם"), ("בני", "אדם"), ("בית", "דין"), ("יום", "כיפור"),
    ("ראש", "השנה"), ("שם", "הטוב"), ("סדר", "היום"),
})

# How much each rule costs a candidate break, measured against Raz's own
# 649 hand cuts. The scale is "rank steps" -- the splitting-priority ladder
# runs 1..4, so a weight of 1 is worth about one rung.
#
# The two cheap ones are cheap because HE BREAKS THEM. Across his reference:
# he ends a card on `את` 16 times (2.5%) and on a function word 23 times
# (3.5%) -- `על` x11, `של` x5. They are preferences, not laws, and treating
# them as vetoes forced a worse cut somewhere else in the same sentence.
#
# The expensive ones he never breaks: 0 construct chains split in 649 cuts.
RULE_WEIGHTS = {
    "et_split": 1,
    "function_word_line_end": 1,
    "construct_chain_split": 4,
    "number_unit_split": 4,
    "english_phrase_split": 4,
}

# A break costing this much or less is "clean": ladder rank 1 or 2 (sentence
# or clause punctuation) with no rule violations. Worth stopping the search on.
CLEAN_BREAK = 2

SENTENCE_FINAL = tuple(".?!…")
CLAUSE_PUNCT = tuple(",;:")
CONJUNCTIONS = frozenset({"אבל", "או", "אז", "כי", "וגם", "אלא"})

_LATIN = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")


def _strip_punct(word: str) -> str:
    return "".join(
        ch for ch in word if not unicodedata.category(ch).startswith("P")
    )


def _is_latin(word: str) -> bool:
    return bool(_LATIN.search(word))


def _is_number(word: str) -> bool:
    return bool(_DIGIT.search(word))


def _ends_sentence(word: str) -> bool:
    return word.rstrip().endswith(SENTENCE_FINAL)


def _chain_variants(word: str) -> tuple[str, ...]:
    """A word, plus the form with one attached prefix removed.

    Hebrew glues prefixes on, so the text says `לבית` where the chain list
    says `בית`. Comparing raw forms means the rule silently never fires: the
    segmenter was splitting `נכנסים לבית / כנסת` while claiming to protect
    `בית כנסת`.
    """
    if len(word) >= 3 and word[0] in ATTACHED_PREFIXES:
        return (word, word[1:])
    return (word,)


def _is_construct_chain(left: str, right: str) -> bool:
    """True if these two words form a known construct chain, prefixes and all."""
    return any(
        (a, b) in CONSTRUCT_CHAINS
        for a in _chain_variants(left)
        for b in _chain_variants(right)
    )


def _violation_cost(violations: list[tuple[str, str]]) -> int:
    """What a set of rule violations costs a candidate break.

    Weighted rather than counted. Counting made every rule a veto: one broken
    rule always lost to none, whatever the alternative looked like. Raz breaks
    the cheap rules routinely, so a count is simply the wrong model.
    """
    return sum(RULE_WEIGHTS.get(rule_id, 1) for rule_id, _ in violations)


# --------------------------------------------------------------------------
# rule checks on a candidate break
# --------------------------------------------------------------------------


def _violations_at_break(left: dict, right: dict | None) -> list[tuple[str, str]]:
    """Which Hebrew rules a break between `left` and `right` would violate.

    Returns (rule_id, prose) pairs. An empty list means the break is clean.
    A break before the end of the file has no `right`, and never violates.
    """
    if right is None:
        return []

    found: list[tuple[str, str]] = []
    left_bare = _strip_punct(left["w"])
    right_bare = _strip_punct(right["w"])

    # A break right after sentence-final punctuation is always legal: the
    # words either side belong to different sentences, so no rule binds.
    if _ends_sentence(left["w"]):
        return []

    if left_bare == ET:
        found.append((
            "et_split",
            f"'{ET}' separated from the noun it marks ('{right_bare}')",
        ))
    elif left_bare in FUNCTION_WORDS:
        found.append((
            "function_word_line_end",
            f"line ends on function word '{left_bare}'",
        ))

    if _is_construct_chain(left_bare, right_bare):
        found.append((
            "construct_chain_split",
            f"construct chain split: '{left_bare} {right_bare}'",
        ))

    if _is_number(left_bare) and right_bare in UNITS:
        found.append((
            "number_unit_split",
            f"number split from its unit: '{left_bare} {right_bare}'",
        ))

    if _is_latin(left["w"]) and _is_latin(right["w"]):
        found.append((
            "english_phrase_split",
            f"English phrase split mid-run: '{left_bare} {right_bare}'",
        ))

    return found


def _gap_bonus(gap: float) -> float:
    """How much a pause argues for cutting here.

    Measured over 2838 boundaries across both corpora, against Raz's own cuts:

        pause          he cuts there
        0.00-0.01s        34.6%
        0.01-0.05s        60.0%
        0.05-0.10s        81.2%
        0.10-0.20s        82.5%

    A pause of 50ms or more more than doubles the odds. This was priority 5 of
    6 on the splitting ladder -- below "nearest word boundary to the midpoint"
    -- which is backwards: it is the single strongest predictor there is.

    Returned as a negative cost, on the same scale as the ladder rank (1-4),
    so a real pause can outweigh any syntactic preference.
    """
    if gap >= 0.10:
        return 3.0
    if gap >= 0.05:
        return 2.5
    if gap >= 0.02:
        return 1.5
    return 0.0


def _break_priority(left: dict, right: dict | None, gap: float) -> tuple:
    """Rank a candidate break. Lower sorts better.

    Mirrors the splitting-priority ladder in docs/modules/segment.md: clean
    breaks first, punctuation over syntax, syntax over silence. The rule
    violations dominate everything -- a well-placed break that strands `את`
    is worse than an awkward one that does not.
    """
    if right is None:
        return (0, 0.0)

    word = left["w"]
    if _ends_sentence(word):
        rank = 1
    elif word.rstrip().endswith(CLAUSE_PUNCT):
        rank = 2
    elif _strip_punct(right["w"]) in CONJUNCTIONS:
        rank = 3
    else:
        rank = 4

    # One combined cost instead of violations-then-rank. Lexicographic
    # ordering made any violation a veto, so a break that stranded `של` could
    # never win even when every alternative was worse -- and Raz strands `של`
    # himself. Adding them lets a strong ladder position outweigh a cheap
    # rule, while an expensive rule still dominates.
    #
    # The gap is subtracted rather than used as a tie-break: at 2838 measured
    # boundaries it separates a cut from a non-cut better than anything else.
    cost = (
        _violation_cost(_violations_at_break(left, right))
        + rank
        - _gap_bonus(gap)
    )
    return (cost, -gap)


# --------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------


def _card_text(words: list[dict]) -> str:
    return " ".join(w["w"] for w in words)


def _group_into_cards(words: list[dict], cfg: Config) -> list[list[dict]]:
    """Greedy grouping, bounded by chars, duration, and sentence boundaries.

    D30: a card never contains the end of one sentence and the start of the
    next. Sentence boundaries are always card boundaries. Within a sentence
    the card breaks wherever the priority ladder says is cleanest.
    """
    cards: list[list[dict]] = []

    for sentence in _split_sentences(words):
        cards.extend(_group_sentence(sentence, cfg))
    return cards


def _split_sentences(words: list[dict]) -> list[list[dict]]:
    out: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        current.append(word)
        if _ends_sentence(word["w"]):
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _card_is_legal(words: list[dict], cfg: Config) -> bool:
    """Does this run of words satisfy every hard rule?"""
    if not words:
        return False
    if len(_card_text(words)) > cfg.max_chars_per_card:
        return False
    if words[-1]["end"] - words[0]["start"] > cfg.max_card_duration:
        return False
    # Character count alone does not prove a card can be laid out: word
    # boundaries may leave no cut where both lines fit.
    return _layout_ok(words, cfg)


def _width_cost(words: list[dict], cfg: Config) -> float:
    """What this card pays for being wider than the target.

    Zero at or below the target, then linear. The point is that a wide card is
    allowed but not free: it has to be buying a better break somewhere, which
    is exactly the trade Raz makes on the 20.5% of his cards that run past 14
    characters.
    """
    overshoot = len(_card_text(words)) - cfg.target_chars_per_card
    return cfg.over_target_cost * overshoot if overshoot > 0 else 0.0


def _group_sentence(words: list[dict], cfg: Config) -> list[list[dict]]:
    """Partition one sentence into cards, minimising total cost.

    Formerly greedy: fill to the character budget, cut at the least-bad
    boundary in reach, repeat. That over-segments, and measurably so -- 698
    cards against Raz's 650 on corpus 1, and 94 against 77 on the worst reel.
    The reason is structural rather than a bad threshold: a greedy pass cuts
    because the budget filled up, not because the speech invited a cut. At a
    boundary with no pause at all Raz cuts only 34.6% of the time; the greedy
    loop cut there whenever the budget said so.

    This considers every legal partition of the sentence instead, and takes
    the cheapest. Cost has two parts:

      * `_break_priority` for each cut -- rule violations, ladder rank, and
        now the silence gap, which is the strongest predictor there is.
      * `card_penalty` for each card, so an extra cut has to justify itself.
        Without it the optimiser is indifferent between one good card and two
        adequate ones, and picks up spurious boundaries for free.
      * `_width_cost` for each card, so running past the target width also has
        to justify itself. Together these two make width a preference the
        optimiser trades against break quality, rather than a wall it hits.

    O(n^2) over a sentence, and sentences are short.
    """
    n = len(words)
    if n == 0:
        return []

    INF = float("inf")
    # best[i] = cheapest cost to cover words[0:i]; prev[i] = where that card began
    best = [INF] * (n + 1)
    prev = [0] * (n + 1)
    best[0] = 0.0

    for end in range(1, n + 1):
        for start in range(end - 1, -1, -1):
            if best[start] == INF:
                continue
            card = words[start:end]
            if not _card_is_legal(card, cfg):
                # cards only get longer as `start` decreases, so once one is
                # illegal every earlier start is too
                break
            cost = best[start] + cfg.card_penalty + _width_cost(card, cfg)
            if end < n:
                left, right = words[end - 1], words[end]
                gap = right["start"] - left["end"]
                cost += _break_priority(left, right, gap)[0]
            if cost < best[end]:
                best[end] = cost
                prev[end] = start

    if best[n] == INF:
        # No legal partition -- a single word longer than the limit, which the
        # hard rules cannot satisfy. Fall back to one word per card and let
        # `line_too_long` report it rather than raising.
        return [[w] for w in words]

    cards: list[list[dict]] = []
    end = n
    while end > 0:
        start = prev[end]
        cards.append(words[start:end])
        end = start
    cards.reverse()
    return cards


def _merge_short_cards(
    cards: list[list[dict]], cfg: Config, warnings: list[dict]
) -> list[list[dict]]:
    """Repair sub-minimum cards by merging, then warn about what is left.

    Merging is legal because it only ever reuses real word timings. A merge
    that would breach a hard rule is not performed -- the short card ships
    with a warning instead, and its timestamp is never stretched.
    """
    out: list[list[dict]] = []
    for card in cards:
        duration = card[-1]["end"] - card[0]["start"]
        if duration >= cfg.min_card_duration or not out:
            out.append(card)
            continue

        previous = out[-1]
        # Never merge across a sentence boundary: that is the one rule D30
        # makes absolute.
        if _ends_sentence(previous[-1]["w"]):
            out.append(card)
            continue

        merged = previous + card
        merged_text = _card_text(merged)
        merged_duration = merged[-1]["end"] - merged[0]["start"]
        # The ceiling is the card budget, not the line budget. Merging up to
        # max_line_length * max_lines would silently undo the whole D30
        # profile: a run of legitimately short cards would fuse into a
        # 60-character block that no longer looks like short-form at all.
        if (
            len(merged_text) <= cfg.max_chars_per_card
            and merged_duration <= cfg.max_card_duration
        ):
            out[-1] = merged
        else:
            out.append(card)

    return out


# --------------------------------------------------------------------------
# lines
# --------------------------------------------------------------------------


def _layout_ok(words: list[dict], cfg: Config) -> bool:
    """Can this card be laid out within the line hard rules?

    A single word longer than the limit is the documented exception: it has no
    word boundary to split on, so it is allowed through and warned about
    rather than blocking the card forever.
    """
    lines = _split_lines(words, cfg)
    if len(lines) > cfg.max_lines:
        return False
    return all(
        len(line) <= cfg.max_line_length or len(line.split()) == 1
        for line in lines
    )


def _split_lines(words: list[dict], cfg: Config) -> list[str]:
    """1 or 2 display lines. At the D30 card width this is almost always 1."""
    text = _card_text(words)
    if len(text) <= cfg.max_line_length or len(words) == 1:
        return [text]

    best_cut, best_key = 1, None
    for cut in range(1, len(words)):
        top = _card_text(words[:cut])
        bottom = _card_text(words[cut:])
        if len(top) > cfg.max_line_length or len(bottom) > cfg.max_line_length:
            continue
        violations = _violation_cost(_violations_at_break(words[cut - 1], words[cut]))
        # Prefer a longer top line when both are legal, so the card keeps a
        # stable shape; balance loses to the syntactic rules above it.
        key = (violations, abs(len(top) - len(bottom)), -len(top))
        if best_key is None or key < best_key:
            best_key, best_cut = key, cut

    if best_key is None:
        return [text]
    return [_card_text(words[:best_cut]), _card_text(words[best_cut:])]


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def segment(obj: dict, *, cfg: Config | None = None, force: bool = False) -> dict:
    """Regroup a Transcript into a SubtitleFile."""
    cfg = cfg or Config()

    # Guard BEFORE validating. A second run is a StageAlreadyRun, and saying
    # so is far more useful than the ContractError you get from feeding a
    # SubtitleFile to validate_transcript -- which is what a re-run actually
    # looks like from in here.
    guard_stage(obj, STAGE, force=force)

    # With --force the input is this module's own previous output, so it
    # carries `lines` and `cps` and only validates as a SubtitleFile. The
    # words are identical either way, and that is all we regroup from.
    if STAGE in obj.get("meta", {}).get("stages", []):
        validate_subtitle_file(obj)
    else:
        validate_transcript(obj)

    words = [w for seg in obj["segments"] for w in seg["words"]]
    if not words:
        raise SegmentError(f"{MODULE}: transcript has no words to segment")

    warnings = list(obj["meta"].get("warnings", []))

    cards = _group_into_cards(words, cfg)
    cards = _merge_short_cards(cards, cfg, warnings)

    segments = []
    for index, card in enumerate(cards):
        lines = _split_lines(card, cfg)
        start, end = card[0]["start"], card[-1]["end"]
        duration = end - start
        text = _card_text(card)
        # Round once, then use the SAME value for the stored field and the
        # threshold test. Warning on the raw value while storing the rounded
        # one makes segment's `cps_exceeded` disagree with export's
        # `cards_over_cps` for any card that lands just above the limit and
        # rounds back onto it -- which is exactly the divergence bench.md
        # tells you to treat as a bug between stages.
        cps = round(
            len(" ".join(lines)) / duration if duration > 0 else 0.0, 3
        )

        segments.append({
            "id": index,
            "start": start,
            "end": end,
            "text": text,
            "words": card,
            "speaker": None,
            "lines": lines,
            "cps": cps,
        })

        span = {"wid_start": card[0]["wid"], "wid_end": card[-1]["wid"]}

        if duration < cfg.min_card_duration:
            warnings.append({
                "stage": STAGE, "code": "card_too_short", **span,
                "detail": (
                    f"{duration:.3f}s under minimum {cfg.min_card_duration:.3f}s; "
                    f"no legal merge available"
                ),
            })

        if cps > cfg.max_cps:
            warnings.append({
                "stage": STAGE, "code": "cps_exceeded", **span,
                "detail": f"{cps:.1f} CPS over limit {cfg.max_cps:.0f}",
            })

        for line in lines:
            if len(line) > cfg.max_line_length:
                warnings.append({
                    "stage": STAGE, "code": "line_too_long", **span,
                    "detail": (
                        f"line of {len(line)} chars exceeds "
                        f"{cfg.max_line_length}; no word boundary to split on"
                    ),
                })

        # The break that produced this card: report any rule it had to break.
        if index + 1 < len(cards):
            following = cards[index + 1][0]
            for rule_id, prose in _violations_at_break(card[-1], following):
                warnings.append({
                    "stage": STAGE,
                    "code": "hebrew_rule_violation",
                    "wid_start": card[-1]["wid"],
                    "wid_end": following["wid"],
                    "detail": f"{rule_id}: {prose}",
                })

    out = {
        "meta": {**obj["meta"], "warnings": warnings},
        "segments": segments,
        "edits": list(obj.get("edits", [])),
    }
    record_stage(out, STAGE)
    validate_subtitle_file(out)
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.segment",
        description="Regroup a Transcript into subtitle cards.",
    )
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", dest="dst", required=True)
    # Defaults live in Config and nowhere else. Repeating them here is how the
    # CLI silently ran with min_card_duration=1.0 while Config said 0.4.
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--max-line-length", type=int, default=None)
    parser.add_argument("--max-duration", type=float, default=None)
    parser.add_argument("--min-duration", type=float, default=None)
    parser.add_argument("--max-cps", type=float, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    obj = json.loads(Path(args.src).read_text(encoding="utf-8"))
    overrides = {
        "max_chars_per_card": args.max_chars,
        "max_line_length": args.max_line_length,
        "max_card_duration": args.max_duration,
        "min_card_duration": args.min_duration,
        "max_cps": args.max_cps,
    }
    cfg = Config(**{k: v for k, v in overrides.items() if v is not None})

    try:
        out = segment(obj, cfg=cfg, force=args.force)
    except StageAlreadyRun as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except SegmentError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    new = len(out["meta"]["warnings"]) - len(obj["meta"].get("warnings", []))
    print(f"OK: {dst} written -- {len(out['segments'])} cards, {new} new warning(s)")
    counts: dict[str, int] = {}
    for warn in out["meta"]["warnings"]:
        counts[warn["code"]] = counts.get(warn["code"], 0) + 1
    for code, count in sorted(counts.items()):
        print(f"  {code}: {count}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
