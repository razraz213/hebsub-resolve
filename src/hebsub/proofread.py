"""Transcript -> corrected Transcript.

Same segments, same timings, same word count. Only the words themselves may
change. This is the module most likely to destroy a transcript if given
freedom, so every pass here is a 1:1 substitution behind guards, and anything
that fails a guard is discarded rather than applied.

The asymmetry that drives every design choice: a missed correction is a minor
annoyance, a hallucinated sentence is a broken product. When in doubt, keep
the original word.

See docs/modules/proofread.md.
"""

from __future__ import annotations

import argparse
import difflib
import re
import json
import sys
import unicodedata
from pathlib import Path

from hebsub.contract import (
    StageAlreadyRun,
    guard_stage,
    record_stage,
    validate_transcript,
)

__all__ = [
    "proofread",
    "review_disagreements",
    "resolve_disagreements",
    "hebrew_lexicon",
    "learn_words",
    "load_user_lexicon",
    "USER_LEXICON_NAME",
    "is_real_word",
    "ProofreadError",
    "Config",
    "Glossary",
    "load_glossary",
    "normalise_for_match",
    "match_variants",
    "similarity",
    "edit_distance",
]

MODULE = "proofread"
STAGE = "proofread"

FINAL_FORMS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})

# Attached prefix letters, stripped before glossary comparison (D11). Hebrew
# glues these onto the front of a word, so the glossary says the term and the
# transcript says the term with a preposition fused to it.
ATTACHED_PREFIXES = frozenset("בלכמשהו")

# Only punctuation may be appended by the llm pass. Anything else would be a
# word, and a word would change the word count.
PUNCTUATION = frozenset(".,;:!?…\"'")


class ProofreadError(Exception):
    """Raised when proofreading cannot run at all."""


def _fail(problem: str) -> None:
    raise ProofreadError(f"{MODULE}: {problem}")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


class Config:
    """Proofread settings. Every pass can be turned off independently."""

    def __init__(
        self,
        passes: tuple[str, ...] = ("glossary",),
        fuzzy_threshold: float = 0.82,
        min_fuzzy_length: int = 3,
        # 0.99, not the spec's 0.75, and measured: Whisper's mean confidence
        # is 0.968 on 96%-accurate output, so 0.75 admits only 3.8% of words
        # and the pass never sees the errors. Confidence is not calibrated
        # here. What prevents damage is the confusion set, the margin, and the
        # glossary freeze -- never this gate. See D37.
        conf_threshold: float = 0.99,
        edit_budget: float = 0.15,
        max_edit_ratio: float = 0.60,
    ) -> None:
        self.passes = tuple(passes)
        self.fuzzy_threshold = fuzzy_threshold
        self.min_fuzzy_length = min_fuzzy_length
        self.conf_threshold = conf_threshold
        self.edit_budget = edit_budget
        self.max_edit_ratio = max_edit_ratio


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------


def _split_trailing_punct(word: str) -> tuple[str, str]:
    """Separate a word from the punctuation glued to its end.

    A replacement must not silently eat the punctuation that was attached to
    the word it replaces -- `segment` reads that punctuation to find sentence
    and clause boundaries, so losing it here degrades segmentation later.
    """
    marks = ""
    stem = word
    while stem and unicodedata.category(stem[-1]).startswith("P"):
        marks = stem[-1] + marks
        stem = stem[:-1]
    return stem, marks


def normalise_for_match(word: str) -> str:
    """Fold a word for glossary comparison only (D11).

    Strips niqqud, unifies final letters, drops punctuation. Deliberately NOT
    used for anything else: this normalisation is lossy on purpose, and
    comparing anything but glossary candidates through it would hide real
    differences.

    Note what this does *not* do: strip the attached prefix. See
    match_variants -- unconditional stripping mangles every word that
    legitimately begins with one of those letters, and most Hebrew words that
    matter here do.
    """
    decomposed = unicodedata.normalize("NFKD", word)
    stripped = "".join(
        ch for ch in decomposed
        if not unicodedata.combining(ch)
        and not unicodedata.category(ch).startswith("P")
    )
    return stripped.translate(FINAL_FORMS)


def match_variants(word: str) -> tuple[str, ...]:
    """The folded word, plus the form with one attached prefix removed.

    D11 asks for attached prefixes to be stripped before glossary comparison,
    because Hebrew glues them on: the glossary says the term, the transcript
    says the term with a preposition fused to the front.

    Stripping *unconditionally* is wrong, and wrong in a way that is easy to
    miss. ב ל כ מ ש ה ו are ordinary letters as well as prefixes, so
    "שתיים" would fold to "תיימ" and "הצליחות" to "צליחות" -- neither of
    which is a word. Both forms are kept and either may match.
    """
    folded = normalise_for_match(word)
    if len(folded) >= 3 and folded[0] in ATTACHED_PREFIXES:
        return (folded, folded[1:])
    return (folded,)


def edit_distance(a: str, b: str) -> int:
    """Levenshtein distance. Small strings only; no dependency needed."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (ca != cb),  # substitution
            ))
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity in [0, 1]."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    if longest == 0:
        return 1.0
    return 1.0 - edit_distance(a, b) / longest


# --------------------------------------------------------------------------
# glossary
# --------------------------------------------------------------------------


class Glossary:
    """Mapped replacements and fuzzy-protected terms."""

    def __init__(self, mappings: dict[str, str], terms: list[str]) -> None:
        # normalised mis-spelling -> exact replacement, as written
        self.mappings = mappings
        # single-token terms, fuzzy-matched
        self.terms = terms
        self._normalised_terms = [(normalise_for_match(t), t) for t in terms]
        self._normalised_targets = {
            normalise_for_match(v) for v in mappings.values()
        }

    def __len__(self) -> int:
        return len(self.mappings) + len(self.terms)

    def is_protected(self, word: str) -> bool:
        """True when `word` is anything the glossary vouches for, prefix or not.

        That covers three things, and missing any of them leaves a hole:
          - a bare term
          - the left side of a mapping, i.e. a known mis-spelling
          - the RIGHT side of a mapping -- the spelling we corrected *to*.
            Leaving targets unprotected would let the llm pass quietly undo
            the glossary pass's work one stage later.

        Protected words are frozen against the llm pass: a term someone wrote
        down is evidence, and a model's probability is not.
        """
        variants = set(match_variants(word))
        if any(n in variants for n, _ in self._normalised_terms):
            return True
        if any(v in self.mappings for v in variants):
            return True
        return any(t in variants for t in self._normalised_targets)

    def lookup(self, word: str, *, threshold: float, min_length: int) -> str | None:
        """The replacement for `word`, or None to leave it alone."""
        variants = match_variants(word)
        if not variants or not variants[0]:
            return None

        # Mapped terms are exact: no threshold, no fuzziness (D11).
        for key in variants:
            mapped = self.mappings.get(key)
            if mapped is not None:
                return mapped if mapped != word else None

        full = variants[0]
        if len(full) < min_length:
            return None

        best, best_score = None, 0.0
        for normalised, original in self._normalised_terms:
            if len(normalised) < min_length:
                continue
            # "Already correct" is checked against every variant, so a term
            # carrying an attached prefix is recognised and left alone.
            if normalised in variants:
                return None

            # Fuzzy scoring, however, uses the FULL word only -- never the
            # prefix-stripped variant. Scoring the truncation invites exactly
            # the failure this cost an afternoon to find: "מתחילים" (they
            # begin) strips to "תחילים", which is one letter from "תהילים"
            # (Psalms) and scores 0.83, over threshold. Two unrelated words,
            # one destructive rewrite. Scored full-form it is 0.71 and safely
            # below.
            score = similarity(full, normalised)
            if score > best_score:
                best, best_score = original, score
        if best is not None and best_score >= threshold:
            return best
        return None


def load_glossary(path: Path | str | None) -> Glossary:
    """Parse the D11 glossary format. Fails loud on a malformed entry."""
    if path is None:
        return Glossary({}, [])
    path = Path(path)
    if not path.exists():
        _fail(f"glossary file does not exist: {path}")

    mappings: dict[str, str] = {}
    terms: list[str] = []

    for number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        if "=>" in line:
            left, right = (part.strip() for part in line.split("=>", 1))
            if not left or not right:
                _fail(f"{path}:{number}: mapping needs a term on both sides")
            # A mapping across a different number of words would change the
            # word count, which the contract forbids for this stage.
            if len(left.split()) != 1 or len(right.split()) != 1:
                _fail(
                    f"{path}:{number}: mapping {left!r} => {right!r} is not "
                    f"one word for one word. proofread may not change word "
                    f"count; multi-word corrections belong to a contract "
                    f"change, not a glossary line"
                )
            mappings[normalise_for_match(left)] = right
        elif "=" in line and "==" not in line:
            _fail(
                f"{path}:{number}: '=' is not the mapping operator, '=>' is "
                f"(D11). Ambiguous lines are a parse error, not a guess"
            )
        else:
            if len(line.split()) == 1:
                terms.append(line)
            # Multi-word bare terms are skipped: fuzzy matching is per-token,
            # so a multi-word term has nothing to match against. It is not an
            # error -- the term is simply inert.

    return Glossary(mappings, terms)


# --------------------------------------------------------------------------
# passes
# --------------------------------------------------------------------------


def _apply_glossary(
    obj: dict, glossary: Glossary, cfg: Config, edits: list[dict]
) -> int:
    """Deterministic 1:1 replacement. Highest confidence pass, runs first."""
    applied = 0
    for segment in obj["segments"]:
        for word in segment["words"]:
            stem, marks = _split_trailing_punct(word["w"])
            if not stem:
                continue
            replacement = glossary.lookup(
                stem,
                threshold=cfg.fuzzy_threshold,
                min_length=cfg.min_fuzzy_length,
            )
            if replacement is None or replacement == stem:
                continue

            before = word["w"]
            word["w"] = replacement + marks
            edits.append({
                "stage": STAGE,
                "wid": word["wid"],
                "before": before,
                "after": word["w"],
                "reason": "glossary",
            })
            applied += 1
        segment["text"] = " ".join(w["w"] for w in segment["words"])
    return applied


def _apply_llm(
    obj: dict,
    adapter,
    glossary: Glossary,
    cfg: Config,
    edits: list[dict],
    warnings: list[dict],
) -> int:
    """Contextual substitution and punctuation, behind every guard in the spec.

    The adapter is never trusted. Each proposal must survive eligibility, the
    per-segment edit budget, and an edit-distance ceiling, and a proposal that
    fails any of them is discarded with an `llm_rejected` warning rather than
    applied. Rejections never appear in `edits`.
    """
    applied = 0
    segments = obj["segments"]

    for index, segment in enumerate(segments):
        words = segment["words"]
        by_wid = {w["wid"]: w for w in words}

        request = {
            "segment": segment,
            "context_before": segments[max(0, index - 2):index],
            "context_after": segments[index + 1:index + 3],
            "glossary": list(glossary.mappings.values()) + glossary.terms,
        }

        try:
            proposals = adapter.propose(request)
        except Exception as exc:  # noqa: BLE001 - an adapter must never crash the run
            warnings.append({
                "stage": STAGE, "code": "llm_rejected",
                "wid_start": words[0]["wid"], "wid_end": words[-1]["wid"],
                "detail": f"adapter raised {type(exc).__name__}: {exc}",
            })
            continue

        if not isinstance(proposals, list):
            warnings.append({
                "stage": STAGE, "code": "llm_rejected",
                "wid_start": words[0]["wid"], "wid_end": words[-1]["wid"],
                "detail": "adapter returned prose, not a list of proposals",
            })
            continue

        budget = max(1, int(len(words) * cfg.edit_budget)) if words else 0
        substitutions = 0
        budget_hit = False

        for proposal in proposals:
            if not isinstance(proposal, dict) or "wid" not in proposal:
                _reject(warnings, None, None, "proposal is not a {wid: ...} object")
                continue

            wid = proposal["wid"]
            word = by_wid.get(wid)
            if word is None:
                _reject(warnings, wid, wid, f"wid {wid!r} is not in this segment")
                continue

            if "append" in proposal:
                mark = proposal["append"]
                if not isinstance(mark, str) or not mark:
                    _reject(warnings, wid, wid, "append must be a non-empty string")
                    continue
                if any(ch not in PUNCTUATION for ch in mark):
                    _reject(
                        warnings, wid, wid,
                        f"append {mark!r} is not punctuation; a word would "
                        f"change the word count"
                    )
                    continue
                before = word["w"]
                word["w"] = before + mark
                edits.append({
                    "stage": STAGE, "wid": wid, "before": before,
                    "after": word["w"], "reason": "punctuation",
                })
                applied += 1
                continue

            if "replacement" not in proposal:
                _reject(warnings, wid, wid, "proposal has neither replacement nor append")
                continue

            replacement = proposal["replacement"]
            if not isinstance(replacement, str) or not replacement.strip():
                _reject(warnings, wid, wid, "replacement must be a non-empty string")
                continue
            if len(replacement.split()) != 1:
                _reject(
                    warnings, wid, wid,
                    f"replacement {replacement!r} is more than one word"
                )
                continue

            # A word the glossary vouches for is frozen. The glossary is a
            # human's written-down observation; the adapter is a probability.
            # Evidence outranks a guess, and letting the guess win here is not
            # hypothetical -- the masked LM rewrote "אדס" (the Ades synagogue,
            # a protected term) into the non-word "עדס".
            stem_now, _ = _split_trailing_punct(word["w"])
            if glossary.is_protected(stem_now):
                _reject(
                    warnings, wid, wid,
                    f"{stem_now!r} is a glossary term; the glossary outranks "
                    f"the model"
                )
                continue

            # Eligibility: high-confidence words are frozen. conf null counts
            # as eligible (D9) -- unknown confidence is not evidence of
            # correctness, and a paid engine returning none must not silently
            # disable the pass.
            conf = word.get("conf")
            if conf is not None and float(conf) >= cfg.conf_threshold:
                _reject(
                    warnings, wid, wid,
                    f"word confidence {float(conf):.2f} is at or above the "
                    f"{cfg.conf_threshold} threshold"
                )
                continue

            if budget_hit or substitutions >= budget:
                if not budget_hit:
                    budget_hit = True
                    warnings.append({
                        "stage": STAGE, "code": "edit_budget_hit",
                        "wid_start": words[0]["wid"], "wid_end": words[-1]["wid"],
                        "detail": (
                            f"segment {segment['id']} hit the "
                            f"{cfg.edit_budget:.0%} edit budget "
                            f"({budget} of {len(words)} words); remaining "
                            f"candidates dropped"
                        ),
                    })
                continue

            stem, marks = _split_trailing_punct(word["w"])
            distance = edit_distance(stem, replacement)
            if stem and distance > cfg.max_edit_ratio * len(stem):
                _reject(
                    warnings, wid, wid,
                    f"edit distance {distance} exceeds "
                    f"{cfg.max_edit_ratio:.0%} of {stem!r}; that is a rewrite, "
                    f"not a correction"
                )
                continue

            before = word["w"]
            word["w"] = replacement + marks
            edits.append({
                "stage": STAGE, "wid": wid, "before": before,
                "after": word["w"], "reason": "llm",
            })
            substitutions += 1
            applied += 1

        segment["text"] = " ".join(w["w"] for w in segment["words"])

    return applied


def _reject(warnings: list[dict], wid_start, wid_end, detail: str) -> None:
    entry = {"stage": STAGE, "code": "llm_rejected", "detail": detail}
    # A malformed proposal can carry a wid of any type. The contract requires
    # wid_start/wid_end to be non-negative ints, so a junk wid is reported in
    # the detail text and omitted from the span rather than smuggled into a
    # field that would fail validation on the way out.
    if (
        isinstance(wid_start, int) and not isinstance(wid_start, bool)
        and isinstance(wid_end, int) and not isinstance(wid_end, bool)
        and wid_start >= 0 and wid_end >= 0
    ):
        entry["wid_start"] = wid_start
        entry["wid_end"] = wid_end
    warnings.append(entry)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# the review list -- where two ASR models disagree
# --------------------------------------------------------------------------


def _fold_for_diff(word: str) -> str:
    """Fold a word down to what two engines would have to agree on.

    Separate from `normalise_for_match`, which is documented as glossary-only
    and deliberately lossy in ways that would hide real differences here.
    This one drops exactly the three things that are spelling rather than
    word choice: niqqud, punctuation, and final-letter form. `שלום` and
    `שלומ` are one word written two ways; `סמים` and `שומעים` are not.
    """
    decomposed = unicodedata.normalize("NFKD", word)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    bare = "".join(
        c for c in bare if not unicodedata.category(c).startswith("P")
    )
    return bare.translate(FINAL_FORMS).strip()


def _flat_words(obj: dict) -> list[dict]:
    return [w for seg in obj.get("segments", []) for w in seg.get("words", [])]


def review_disagreements(primary: dict, alternative: dict) -> list[dict]:
    """Words two ASR models transcribed differently. Flags only; fixes nothing.

    Measured over all three corpora (D47, D48): a word the two models disagree
    on is wrong **48.6%** of the time against **1.5%** for a word they agree
    on -- a 49x lift on corpus 3, and by a wide margin the best error signal
    found. For comparison, masked-LM rescoring reached 0.5% precision (D44) and
    a 469k-entry lexicon reached 31.7% (D46).

    48.6% is still under the >50% that D44 established as break-even for
    changing a word automatically, so nothing here is applied. A review list
    has no such threshold: it cannot damage a transcript because it never
    touches one. It turns an invisible ~5% word error rate into a short list
    of specific words to look at.

    **Only 1:1 substitutions are reported.** Insertions and deletions are a
    different phenomenon with different odds and were never measured, and
    reporting them on this evidence would be quoting a number nobody has.

    Neither transcript is modified. `primary` supplies the wids and timings,
    because it is the transcript that ships.
    """
    mine = _flat_words(primary)
    theirs = _flat_words(alternative)
    if not mine or not theirs:
        return []

    left = [_fold_for_diff(w.get("w") or "") for w in mine]
    right = [_fold_for_diff(w.get("w") or "") for w in theirs]

    flags: list[dict] = []
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for offset in range(i2 - i1):
            word = mine[i1 + offset]
            flags.append({
                "wid": word["wid"],
                "start": word["start"],
                "heard": (word.get("w") or "").strip(),
                "alternative": (theirs[j1 + offset].get("w") or "").strip(),
            })
    flags.sort(key=lambda f: f["wid"])
    return flags


# Maximal runs of Hebrew letters, applied AFTER folding.
_HEBREW_RUN = re.compile(r"[א-ת]+")

USER_LEXICON_NAME = "lexicon.txt"


def learn_words(text: str) -> set[str]:
    """Every Hebrew word in a blob of text, folded for comparison.

    Deliberately dumb about format. Feed it a corrected `.srt` and the indices
    and timecodes fall out on their own, because they are not Hebrew -- so
    there is no parser to keep in step with anything.
    """
    found: set[str] = set()
    for token in text.split():
        # Fold FIRST. That is what removes niqqud, punctuation and the geresh,
        # so `\u05d2'\u05d9\u05e4` survives as one word instead of splitting into two.
        folded = _fold_for_diff(token)
        # Then take the Hebrew runs. Requiring the *whole* token to be Hebrew
        # was the bug: `<` and `>` are symbols, not punctuation, so the fold
        # left them attached and every word touching a <b> tag was discarded --
        # on a subtitle file, every word at a card boundary. 205 words
        # harvested from a 1,486-word file instead of 764.
        #
        # Markup is not a special case. `\u266a\u05e9\u05dc\u05d5\u05dd\u266a`, `~\u05e9\u05dc\u05d5\u05dd~`, `\u2192\u05e9\u05dc\u05d5\u05dd` and `8\u05e9\u05e7\u05dc`
        # all failed identically, and music notes are ordinary in subtitles.
        # Stripping tags would have fixed the symptom and left the class.
        for run in _HEBREW_RUN.findall(folded):
            if len(run) > 1:
                # A single letter left over from a mixed token -- the `\u05d4` of
                # `\u05d4Pocket` -- is a fragment, not a word.
                found.add(run)
    return found


def load_user_lexicon(path: Path | str | None) -> frozenset:
    """Words the user has confirmed by writing them in a corrected file.

    Measured leave-one-corpus-out, these transfer across *domains* not at all
    -- Judaica vocabulary does not help a workshop reel, which is what D46
    found for the detector too. What they do is help a domain against itself:
    with the held-out corpus's own words present, the rule goes from 6 fixes
    to 9. Raz makes many reels per client, so that is the case that matters
    and the one three unrelated corpora cannot measure.

    Safe by construction: a word he wrote can only make the *correct* side of
    a disagreement recognisable, because his corrections never contain the
    ASR's mistakes.
    """
    if path is None:
        return frozenset()
    path = Path(path)
    if not path.exists():
        return frozenset()
    return frozenset(learn_words(path.read_text(encoding="utf-8")))


_LEXICON: frozenset | None = None


def hebrew_lexicon(extra: frozenset | None = None) -> frozenset:
    """Whole Hebrew words, from DictaBERT's tokenizer vocabulary.

    105,288 entries. Not a dictionary and not pretending to be one -- D46
    measured it covering 94.3% of Raz's corrected words, which is far too
    leaky to answer "is this word suspicious" across a whole transcript.

    It is used for a much narrower question here: given two candidates the ASR
    models disagree on, is exactly one of them a real word? On that question
    it is right often enough to be worth acting on (D62), because being asked
    only about 107 already-suspect pairs is a different job from judging 4,331
    words.

    hspell's 469k forms are AGPL and, measured, buy nothing: identical fixes
    and identical WER (D64). The union of the two is *worse* than either alone,
    because this rule does not want to know which words exist -- it wants to
    know which strings are not words, and every entry added makes one more
    non-word look real. A stricter lexicon detects more.

    Returns an empty set if `transformers` is missing, which makes the caller
    degrade to flag-only rather than fail.
    """
    global _LEXICON
    if extra is None:
        default = Path.cwd() / USER_LEXICON_NAME
        extra = load_user_lexicon(default if default.exists() else None)
    if _LEXICON is None:
        try:
            from transformers import AutoTokenizer

            tok = AutoTokenizer.from_pretrained("dicta-il/dictabert")
            _LEXICON = frozenset(
                _fold_for_diff(t) for t in tok.get_vocab()
                if not t.startswith("##") and t
                and all("\u05d0" <= ch <= "\u05ea" for ch in t)
            )
        except Exception:  # noqa: BLE001 - an absent lexicon must not fail a run
            _LEXICON = frozenset()
    return frozenset(_LEXICON | extra) if extra else _LEXICON


def is_real_word(word: str, lexicon: frozenset) -> bool:
    """Is this a real Hebrew word, allowing for attached prefixes?

    Anything not written in Hebrew letters -- Latin script, digits -- returns
    True: it is not this function's business to judge, and treating it as a
    non-word would let the rule act on it.
    """
    folded = _fold_for_diff(word)
    if not folded:
        return False
    if not all("\u05d0" <= ch <= "\u05ea" for ch in folded):
        return True
    if folded in lexicon:
        return True
    # Hebrew fuses ב/כ/ל/מ/ש/ה/ו onto the front; no flat list holds every
    # combination, so peel up to two of them.
    if len(folded) > 2 and folded[0] in ATTACHED_PREFIXES and folded[1:] in lexicon:
        return True
    if (len(folded) > 3 and folded[0] in ATTACHED_PREFIXES
            and folded[1] in ATTACHED_PREFIXES and folded[2:] in lexicon):
        return True
    return False


def resolve_disagreements(
    primary: dict, alternative: dict, *, lexicon: frozenset | None = None
) -> dict:
    """Take the real word when the other model heard a non-word.

    Raz's rule, and it is a better rule than either tiebreaker measured in
    D61, because it only acts where there is evidence:

      * both candidates are real words -> change nothing. Measured on 80 such
        cases: taking the partner would have cost 27 words.
      * exactly one is a real word -> take it. Measured on 14 such cases:
        **6 words fixed, 0 broken**.
      * neither is real -> change nothing. The correct word was in the lexicon
        1 time in 13, so there is nothing to reach for.

    Word count, timings and `wid` are untouched -- this is a 1:1 substitution
    behind the same guarantees as every other pass here. Each change is
    recorded in `edits` with reason `second_opinion`, so nothing happens
    silently.
    """
    lex = hebrew_lexicon() if lexicon is None else lexicon
    out = json.loads(json.dumps(primary))          # never mutate the input
    if not lex:
        return out

    flags = review_disagreements(primary, alternative)
    by_wid = {w["wid"]: w for seg in out.get("segments", [])
              for w in seg.get("words", [])}
    edits = list(out.get("edits", []))
    touched = False

    for flag in flags:
        heard, other = flag["heard"], flag["alternative"]
        heard_real = is_real_word(heard, lex)
        other_real = is_real_word(other, lex)
        if heard_real == other_real:
            continue                                # rule 1 and rule 3
        if heard_real:
            continue                                # ours is the real one
        word = by_wid.get(flag["wid"])
        if word is None:
            continue
        edits.append({
            "stage": STAGE,
            "wid": flag["wid"],
            "before": word["w"],
            "after": other,
            "reason": "second_opinion",
        })
        word["w"] = other
        touched = True

    if touched:
        # The contract requires segment.text to equal the space-joined words,
        # so a substitution is not complete until the text is rebuilt.
        for segment in out.get("segments", []):
            segment["text"] = " ".join(w["w"] for w in segment["words"])

    out["edits"] = edits
    return out


def proofread(
    obj: dict,
    *,
    cfg: Config | None = None,
    glossary: Glossary | None = None,
    adapter=None,
    force: bool = False,
) -> dict:
    """Correct a Transcript in place-safe fashion and return the new one."""
    import copy

    cfg = cfg or Config()
    guard_stage(obj, STAGE, force=force)
    validate_transcript(obj)

    out = copy.deepcopy(obj)
    glossary = glossary if glossary is not None else Glossary({}, [])
    warnings = list(out["meta"].get("warnings", []))
    edits = list(out.get("edits", []))

    if "glossary" in cfg.passes:
        _apply_glossary(out, glossary, cfg, edits)

    if "llm" in cfg.passes:
        if adapter is None:
            from hebsub.llm import get_adapter

            adapter = get_adapter("null")
        _apply_llm(out, adapter, glossary, cfg, edits, warnings)

    out["edits"] = edits
    out["meta"]["warnings"] = warnings
    record_stage(out, STAGE)
    validate_transcript(out)

    # The contract cannot express "same words as the input", so assert it here
    # rather than trusting that every guard above held.
    _assert_shape_preserved(obj, out)
    return out


def _assert_shape_preserved(before: dict, after: dict) -> None:
    if len(before["segments"]) != len(after["segments"]):
        _fail("segment count changed; proofread may only substitute words")
    for old, new in zip(before["segments"], after["segments"]):
        if len(old["words"]) != len(new["words"]):
            _fail(
                f"segment {old['id']}: word count changed "
                f"{len(old['words'])} -> {len(new['words'])}"
            )
        for a, b in zip(old["words"], new["words"]):
            if a["wid"] != b["wid"]:
                _fail(f"wid changed {a['wid']} -> {b['wid']}")
            if a["start"] != b["start"] or a["end"] != b["end"]:
                _fail(f"wid {a['wid']}: timestamps were modified")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.proofread",
        description="Correct a Transcript without changing its shape.",
    )
    parser.add_argument("--in", dest="src", default=None)
    parser.add_argument("--out", dest="dst", default=None)
    parser.add_argument("--glossary", default=None)
    parser.add_argument(
        "--passes", default="glossary",
        help="comma-separated: glossary,llm. Empty string disables all.",
    )
    parser.add_argument("--llm-adapter", default="null")
    parser.add_argument("--conf-threshold", type=float, default=None)
    parser.add_argument("--edit-budget", type=float, default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--learn", nargs="+", metavar="FILE",
        help=(
            "harvest Hebrew words from corrected file(s) into the user "
            "lexicon, so the second-opinion rule recognises them next time. "
            "Accepts .srt directly -- timecodes are not Hebrew and fall out."
        ),
    )
    parser.add_argument(
        "--lexicon", default=None,
        help=f"user lexicon file (default: ./{USER_LEXICON_NAME})",
    )
    args = parser.parse_args(argv)

    if not args.learn and (not args.src or not args.dst):
        parser.error("--in and --out are required unless --learn is used")

    if args.learn:
        target = Path(args.lexicon or (Path.cwd() / USER_LEXICON_NAME))
        known = load_user_lexicon(target)
        found: set[str] = set()
        for source in args.learn:
            found |= learn_words(Path(source).read_text(encoding="utf-8"))
        fresh = found - known
        with target.open("a", encoding="utf-8") as handle:
            for word in sorted(fresh):
                handle.write(word + "\n")
        print(f"{MODULE}: learned {len(fresh)} new word(s) from "
              f"{len(args.learn)} file(s); {len(known) + len(fresh)} total "
              f"in {target.name}")
        return 0


    passes = tuple(p.strip() for p in args.passes.split(",") if p.strip())
    overrides = {
        k: v for k, v in (
            ("conf_threshold", args.conf_threshold),
            ("edit_budget", args.edit_budget),
        ) if v is not None
    }
    cfg = Config(passes=passes, **overrides)

    try:
        glossary = load_glossary(args.glossary)
    except ProofreadError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    adapter = None
    if "llm" in passes:
        from hebsub.llm import LLMError, get_adapter

        try:
            adapter = get_adapter(args.llm_adapter)
        except LLMError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    obj = json.loads(Path(args.src).read_text(encoding="utf-8"))
    before_warnings = len(obj["meta"].get("warnings", []))

    try:
        out = proofread(
            obj, cfg=cfg, glossary=glossary, adapter=adapter, force=args.force
        )
    except StageAlreadyRun as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except ProofreadError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    new_edits = len(out["edits"]) - len(obj.get("edits", []))
    new_warnings = len(out["meta"]["warnings"]) - before_warnings
    print(
        f"OK: {dst} written -- passes [{','.join(passes) or 'none'}], "
        f"{new_edits} edit(s), {new_warnings} new warning(s)"
    )
    by_reason: dict[str, int] = {}
    for edit in out["edits"]:
        by_reason[edit["reason"]] = by_reason.get(edit["reason"], 0) + 1
    for reason, count in sorted(by_reason.items()):
        print(f"  {reason}: {count}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
