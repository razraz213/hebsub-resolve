"""Hebrew masked-LM adapter: confusion-set rescoring, never free generation.

D34 measured that ~79% of the ASR's one-to-one errors are contextual -- a real
Hebrew word, just the wrong one -- and D31 chose a local encoder to fix them.
The obvious implementation, "mask the word and take the model's top
prediction", was probed first and is **wrong**, so it is deliberately not what
this does.

Why it is wrong, measured on the real cases:

  * A masked LM predicts what is *likely*, not what was *said*. It has no
    access to the audio. Asked to fill "הדבר הכי ___", dictabert wants
    "חשוב" at p=0.84 against "טוב" at p=0.06 -- a 14:1 preference for
    overwriting a perfectly correct word.
  * On four control cases where the ASR was already right, top-1 generation
    would have damaged one (dictabert) or two (BEREL).
  * Domain vocabulary is simply absent: `תפילון` and `ברכון` are OOV, so the
    model cannot even represent the words this corpus is about.

At a 96.4% baseline, a pass that damages a quarter of what it touches loses
badly. So the model is never asked what word belongs. It is only ever asked
to choose between the word the ASR produced and a small set of *phonetically
plausible* alternatives -- and if none of them wins by a wide margin, nothing
is proposed.

That narrowing is not arbitrary. The measured error catalogue is dominated by
letter confusions, not semantic slips:

    קשרה -> כשרה (ק/כ)    כופף -> חופף (כ/ח)    האור -> העור (א/ע)
    מאור -> מעור (א/ע)    הצליחות -> הסליחות (צ/ס)    אשכנדים -> אשכנזים (ד/ז)

Even so, every proposal still passes back through proofread's guards --
eligibility, edit budget, edit distance. This adapter is not trusted and is
not supposed to be.
"""

from __future__ import annotations

import unicodedata

from hebsub.llm import LLMError

__all__ = ["MaskedLMAdapter", "DEFAULT_MODEL", "CONFUSION_SETS", "confusion_candidates"]

DEFAULT_MODEL = "dicta-il/dictabert"

# Letters Hebrew ASR actually confuses, grouped by how they sound. Each group
# is mutually substitutable. Derived from the measured error catalogue plus
# the standard homophone sets in modern Israeli Hebrew.
CONFUSION_SETS = (
    frozenset("אע"),      # both silent/glottal
    frozenset("כחק"),     # kaf / het / kuf
    frozenset("סשצ"),     # samekh / shin / tsadi
    frozenset("טת"),      # tet / tav
    frozenset("בו"),      # bet / vav
    frozenset("דז"),      # dalet / zayin, heard in fast speech
    frozenset("הא"),      # he / alef
)

FINAL_FORMS = str.maketrans({"ך": "כ", "ם": "מ", "ן": "נ", "ף": "פ", "ץ": "צ"})


def _strip_marks(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(
        ch for ch in decomposed
        if not unicodedata.combining(ch)
        and not unicodedata.category(ch).startswith("P")
    )


def confusion_candidates(word: str, *, max_swaps: int = 1) -> list[str]:
    """Words one confusable-letter swap away from `word`.

    Single swap only by default. Two swaps explodes the candidate set and
    starts reaching genuinely different words, which is the failure mode this
    whole design exists to avoid.
    """
    bare = _strip_marks(word)
    if len(bare) < 2:
        return []

    out: list[str] = []
    for position, letter in enumerate(bare):
        for group in CONFUSION_SETS:
            if letter not in group:
                continue
            for other in group:
                if other == letter:
                    continue
                candidate = bare[:position] + other + bare[position + 1:]
                if candidate != bare and candidate not in out:
                    out.append(candidate)
    return out


class MaskedLMAdapter:
    """Proposes only among phonetically plausible alternatives."""

    name = "masked_lm"

    def __init__(
        self,
        model: str | None = None,
        *,
        margin: float = 4.0,
        min_ratio: float = 0.002,
        device: str = "cpu",
    ) -> None:
        self.model_name = model or DEFAULT_MODEL
        self.version = self.model_name
        # A candidate must beat the heard word by this factor before it is
        # even proposed. High on purpose: the cost of a wrong substitution is
        # far greater than the cost of a missed one.
        self.margin = margin
        # and it must clear a floor in absolute terms, so two equally
        # implausible words do not produce a proposal on their ratio alone
        self.min_ratio = min_ratio
        self.device = device
        self._tok = None
        self._model = None
        self._torch = None

    def _load(self):
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModelForMaskedLM, AutoTokenizer
        except ImportError as exc:
            raise LLMError(
                f"proofread: the masked_lm adapter needs transformers and "
                f"torch installed ({exc})"
            ) from exc

        try:
            self._tok = AutoTokenizer.from_pretrained(self.model_name)
            self._model = AutoModelForMaskedLM.from_pretrained(self.model_name)
            self._model.eval()
            self._model.to(self.device)
        except Exception as exc:
            raise LLMError(
                f"proofread: could not load masked LM {self.model_name!r}: {exc}"
            ) from exc
        self._torch = torch

    def _single_token_id(self, word: str) -> int | None:
        """The vocab id for `word`, or None if it is not a single token.

        Multi-token words cannot be scored against a single [MASK] slot, and
        splitting the mask across sub-words makes the probabilities
        incomparable between candidates of different lengths. Skipping them is
        the honest option.
        """
        ids = self._tok.convert_tokens_to_ids([word])
        if not ids or ids[0] is None or ids[0] == self._tok.unk_token_id:
            return None
        return ids[0]

    def propose(self, request: dict) -> list[dict]:
        segment = request.get("segment") or {}
        words = segment.get("words") or []
        if not words:
            return []

        self._load()
        torch = self._torch
        proposals: list[dict] = []

        for index, word in enumerate(words):
            heard = _strip_marks(word["w"])
            if len(heard) < 3:
                continue  # too short to disambiguate; noise only

            heard_id = self._single_token_id(heard)
            if heard_id is None:
                continue  # OOV: the model has no opinion worth having

            candidates = [
                c for c in confusion_candidates(heard)
                if self._single_token_id(c) is not None
            ]
            if not candidates:
                continue

            surface = [w["w"] for w in words]
            surface[index] = self._tok.mask_token
            text = " ".join(surface)

            enc = self._tok(text, return_tensors="pt", truncation=True)
            enc = {k: v.to(self.device) for k, v in enc.items()}
            positions = (
                enc["input_ids"][0] == self._tok.mask_token_id
            ).nonzero()
            if len(positions) == 0:
                continue

            with torch.no_grad():
                logits = self._model(**enc).logits[0, positions[0, 0].item()]
            probs = torch.softmax(logits, dim=-1)

            heard_p = float(probs[heard_id])
            best, best_p = None, 0.0
            for candidate in candidates:
                cid = self._single_token_id(candidate)
                p = float(probs[cid])
                if p > best_p:
                    best, best_p = candidate, p

            if best is None:
                continue
            # Both gates must hold: a wide relative margin AND an absolute
            # floor. Ratio alone fires on two equally implausible words.
            if best_p >= self.min_ratio and best_p >= self.margin * max(
                heard_p, 1e-12
            ):
                proposals.append({"wid": word["wid"], "replacement": best})

        return proposals
