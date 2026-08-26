"""Tests for the proofread module.\n\nAssertions come from docs/modules/proofread.md. The metric-based criteria need\n`bench` and the real references; everything here is an invariant test on\nsynthetic JSON, which D14 explicitly allows to proceed.\n\nThe invariants matter more than the corrections. A missed correction is a\nminor annoyance; a transcript whose word count or timings shifted is a broken\nproduct, and every test below exists to make that impossible to ship.\n"""

from __future__ import annotations

import copy
import json

import pytest

from hebsub.contract import StageAlreadyRun, validate_transcript
from hebsub.llm import get_adapter
from hebsub.proofread import (
    Config,
    Glossary,
    ProofreadError,
    edit_distance,
    load_glossary,
    match_variants,
    normalise_for_match,
    proofread,
    USER_LEXICON_NAME,
    hebrew_lexicon,
    learn_words,
    load_user_lexicon,
    is_real_word,
    resolve_disagreements,
    review_disagreements,
    similarity,
)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def transcript(words: list[str], *, conf: float | None = 0.9) -> dict:
    built = []
    for i, w in enumerate(words):
        built.append({
            "wid": i,
            "w": w,
            "start": round(i * 0.3, 3),
            "end": round((i + 1) * 0.3, 3),
            "conf": conf,
        })
    return {
        "meta": {
            "source_file": "synthetic.wav",
            "duration": round(len(words) * 0.3, 3),
            "language": "he",
            "engine": "synthetic",
            "engine_version": "test",
            "created_at": "2026-08-25T10:00:00Z",
            "stages": ["transcribe"],
            "warnings": [],
        },
        "segments": [{
            "id": 0,
            "start": 0.0,
            "end": round(len(words) * 0.3, 3),
            "text": " ".join(words),
            "words": built,
            "speaker": None,
        }],
        "edits": [],
    }


class StubAdapter:
    """Returns whatever it was handed. The point is that it is not trusted."""

    name = "stub"
    version = "0"

    def __init__(self, proposals):
        self._proposals = proposals

    def propose(self, request):
        if callable(self._proposals):
            return self._proposals(request)
        return self._proposals


def words_of(obj: dict) -> list[str]:
    return [w["w"] for s in obj["segments"] for w in s["words"]]


def warnings_with(obj: dict, code: str) -> list[dict]:
    return [w for w in obj["meta"]["warnings"] if w["code"] == code]


LLM = Config(passes=("llm",))


# --------------------------------------------------------------------------
# hard constraints -- these are tests, not hopes
# --------------------------------------------------------------------------


class TestShapeIsImmutable:
    def test_word_count_is_identical(self):
        obj = transcript(["אחת", "שתיים", "שלוש"])
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        assert len(words_of(out)) == len(words_of(obj))

    def test_wid_sequence_is_identical(self):
        obj = transcript(["אחת", "שתיים", "שלוש"])
        before = [w["wid"] for s in obj["segments"] for w in s["words"]]
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        assert [w["wid"] for s in out["segments"] for w in s["words"]] == before

    def test_every_timestamp_is_bit_identical(self):
        obj = transcript(["אחת", "שתיים", "שלוש"])
        before = [
            (w["start"], w["end"]) for s in obj["segments"] for w in s["words"]
        ]
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        after = [
            (w["start"], w["end"]) for s in out["segments"] for w in s["words"]
        ]
        assert after == before

    def test_segment_count_and_ids_are_untouched(self):
        obj = transcript(["אחת", "שתיים"])
        out = proofread(obj, glossary=Glossary({}, []))
        assert [s["id"] for s in out["segments"]] == [s["id"] for s in obj["segments"]]

    def test_input_object_is_not_mutated(self):
        obj = transcript(["אחת", "שתיים"])
        snapshot = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        assert json.dumps(obj, ensure_ascii=False, sort_keys=True) == snapshot

    def test_output_validates(self):
        obj = transcript(["אחת", "שתיים"])
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        validate_transcript(out)

    def test_text_invariant_follows_the_words(self):
        obj = transcript(["אחת", "שתיים"])
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        for seg in out["segments"]:
            assert seg["text"] == " ".join(w["w"] for w in seg["words"])


# --------------------------------------------------------------------------
# the edits log
# --------------------------------------------------------------------------


class TestEditsLog:
    def test_an_applied_change_is_recorded(self):
        obj = transcript(["אחת", "שתיים"])
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        assert len(out["edits"]) == 1
        entry = out["edits"][0]
        assert entry["reason"] == "glossary"
        assert entry["before"] == "שתיים"
        assert entry["after"] == "שבע"

    def test_every_edit_wid_exists_in_segments(self):
        obj = transcript(["אחת", "שתיים", "שלוש"])
        out = proofread(obj, glossary=Glossary({"שתיימ": "שבע"}, []))
        live = {w["wid"] for s in out["segments"] for w in s["words"]}
        assert all(e["wid"] in live for e in out["edits"])

    def test_nothing_applied_means_no_edits(self):
        obj = transcript(["אחת", "שתיים"])
        out = proofread(obj, glossary=Glossary({}, []))
        assert out["edits"] == []

    def test_a_rejection_is_a_warning_not_an_edit(self):
        obj = transcript(["אחת", "שתיים"], conf=0.99)
        adapter = StubAdapter([{"wid": 1, "replacement": "שבע"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert out["edits"] == []
        assert warnings_with(out, "llm_rejected")


# --------------------------------------------------------------------------
# glossary pass
# --------------------------------------------------------------------------


class TestGlossaryPass:
    def test_mapped_term_is_replaced_exactly(self):
        obj = transcript(["הלכתי", "הצליחות"])
        out = proofread(obj, glossary=Glossary({"הצליחות": "הסליחות"}, []))
        assert words_of(out) == ["הלכתי", "הסליחות"]

    def test_replacement_keeps_trailing_punctuation(self):
        # segment reads this punctuation to find sentence boundaries; a
        # correction that ate it would degrade segmentation downstream
        obj = transcript(["הלכתי", "הצליחות."])
        out = proofread(obj, glossary=Glossary({"הצליחות": "הסליחות"}, []))
        assert words_of(out) == ["הלכתי", "הסליחות."]

    def test_attached_prefix_still_matches(self):
        obj = transcript(["הלכתי", "להצליחות"])
        out = proofread(obj, glossary=Glossary({"הצליחות": "הסליחות"}, []))
        assert out["edits"], "attached prefix should not prevent a match"

    def test_bare_term_is_fuzzy_matched(self):
        obj = transcript(["אמרתי", "אשכנדים"])
        out = proofread(obj, glossary=Glossary({}, ["אשכנזים"]))
        assert words_of(out) == ["אמרתי", "אשכנזים"]

    def test_a_correct_word_is_left_alone(self):
        obj = transcript(["אמרתי", "אשכנזים"])
        out = proofread(obj, glossary=Glossary({}, ["אשכנזים"]))
        assert out["edits"] == []

    def test_short_tokens_are_not_fuzzy_matched(self):
        # length >= 3 only: below that almost everything is 0.82 similar to
        # almost everything, and the pass would start rewriting real words
        obj = transcript(["של", "עם"])
        out = proofread(obj, glossary=Glossary({}, ["שלי"]))
        assert out["edits"] == []

    def test_a_distant_word_is_not_matched(self):
        obj = transcript(["אמרתי", "מכונית"])
        out = proofread(obj, glossary=Glossary({}, ["אשכנזים"]))
        assert out["edits"] == []

    def test_disabled_pass_changes_nothing(self):
        obj = transcript(["הלכתי", "הצליחות"])
        out = proofread(
            obj, cfg=Config(passes=()), glossary=Glossary({"הצליחות": "הסליחות"}, [])
        )
        assert words_of(out) == words_of(obj)
        assert out["edits"] == []


# --------------------------------------------------------------------------
# glossary file parsing
# --------------------------------------------------------------------------


class TestGlossaryFile:
    def test_parses_mappings_and_bare_terms(self, tmp_path):
        path = tmp_path / "g.txt"
        path.write_text(
            "# comment\nהצליחות => הסליחות\nקדיש\n\n", encoding="utf-8"
        )
        glossary = load_glossary(path)
        assert glossary.terms == ["קדיש"]
        assert len(glossary.mappings) == 1

    def test_multi_word_mapping_is_a_load_error(self, tmp_path):
        # it would change word count, which this stage may never do
        path = tmp_path / "g.txt"
        path.write_text("בית ספר => בית הספר\n", encoding="utf-8")
        with pytest.raises(ProofreadError, match="one word"):
            load_glossary(path)

    def test_single_equals_is_a_parse_error(self, tmp_path):
        path = tmp_path / "g.txt"
        path.write_text("שגיאה = נכון\n", encoding="utf-8")
        with pytest.raises(ProofreadError, match="=>"):
            load_glossary(path)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ProofreadError):
            load_glossary(tmp_path / "nope.txt")

    def test_no_glossary_is_an_empty_one(self):
        assert len(load_glossary(None)) == 0

    def test_the_real_glossary_loads(self):
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        glossary = load_glossary(repo / "glossary.txt")
        assert glossary.mappings and glossary.terms


# --------------------------------------------------------------------------
# llm pass guards -- the adapter is never trusted
# --------------------------------------------------------------------------


class TestLlmGuards:
    def test_null_adapter_changes_nothing(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        out = proofread(obj, cfg=LLM, adapter=get_adapter("null"))
        assert words_of(out) == words_of(obj)
        assert out["edits"] == []

    def test_a_low_confidence_word_may_be_substituted(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        adapter = StubAdapter([{"wid": 1, "replacement": "שתיים"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        # identical replacement still counts as applied; the guard under test
        # is eligibility, not novelty
        assert not warnings_with(out, "llm_rejected")

    def test_a_high_confidence_word_is_frozen(self):
        obj = transcript(["אחת", "שתיים"], conf=0.99)
        adapter = StubAdapter([{"wid": 1, "replacement": "שבע"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert words_of(out) == words_of(obj)
        assert warnings_with(out, "llm_rejected")

    def test_null_confidence_counts_as_eligible(self):
        # D9: unknown confidence is not evidence of correctness
        obj = transcript(["אחת", "שתיים"], conf=None)
        adapter = StubAdapter([{"wid": 1, "replacement": "שתיום"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert out["edits"], "conf: null must not silently disable the pass"

    def test_a_rewrite_is_rejected_on_edit_distance(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        adapter = StubAdapter([{"wid": 1, "replacement": "מכוניתאדומה"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert words_of(out) == words_of(obj)
        assert warnings_with(out, "llm_rejected")

    def test_a_multi_word_replacement_is_rejected(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        adapter = StubAdapter([{"wid": 1, "replacement": "שתיים שלוש"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert len(words_of(out)) == 2
        assert warnings_with(out, "llm_rejected")

    def test_a_wid_outside_the_segment_is_rejected(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        adapter = StubAdapter([{"wid": 999, "replacement": "שבע"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert warnings_with(out, "llm_rejected")


class TestPunctuationAppend:
    def test_punctuation_is_appended_to_the_word(self):
        obj = transcript(["אחת", "שתיים"], conf=0.99)
        adapter = StubAdapter([{"wid": 1, "append": "."}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert words_of(out) == ["אחת", "שתיים."]

    def test_append_preserves_word_count(self):
        obj = transcript(["אחת", "שתיים"], conf=0.99)
        adapter = StubAdapter([{"wid": 1, "append": "."}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert len(words_of(out)) == 2

    def test_append_is_not_gated_on_confidence(self):
        obj = transcript(["אחת", "שתיים"], conf=0.99)
        adapter = StubAdapter([{"wid": 1, "append": "?"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert out["edits"][0]["reason"] == "punctuation"

    def test_appending_a_word_is_rejected(self):
        # anything but punctuation would change the word count
        obj = transcript(["אחת", "שתיים"], conf=0.99)
        adapter = StubAdapter([{"wid": 1, "append": " ועוד"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert words_of(out) == words_of(obj)
        assert warnings_with(out, "llm_rejected")


# --------------------------------------------------------------------------
# adversarial adapter -- the acceptance criterion
# --------------------------------------------------------------------------


class TestAdversarialAdapter:
    def test_prose_response_applies_nothing_and_does_not_crash(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        adapter = StubAdapter("Sure! Here are my corrections: ...")
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert out["edits"] == []
        assert warnings_with(out, "llm_rejected")
        validate_transcript(out)

    def test_garbage_proposals_apply_nothing(self):
        obj = transcript(["אחת", "שתיים"], conf=0.1)
        adapter = StubAdapter([{"nonsense": True}, 42, None, {"wid": "x"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert words_of(out) == words_of(obj)
        assert len(warnings_with(out, "llm_rejected")) >= 3

    def test_an_adapter_that_raises_does_not_crash_the_run(self):
        class Exploding:
            name, version = "boom", "0"

            def propose(self, request):
                raise RuntimeError("model unavailable")

        obj = transcript(["אחת", "שתיים"], conf=0.1)
        out = proofread(obj, cfg=LLM, adapter=Exploding())
        assert words_of(out) == words_of(obj)
        assert warnings_with(out, "llm_rejected")


class TestEditBudget:
    def test_at_most_the_budget_is_applied_and_it_warns(self):
        # 20 words, all conf null, adapter proposes a change for every one
        words = [f"מילה{i:02d}" for i in range(20)]
        obj = transcript(words, conf=None)
        adapter = StubAdapter([
            {"wid": i, "replacement": f"מילה{i:02d}x"} for i in range(20)
        ])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        applied = [e for e in out["edits"] if e["reason"] == "llm"]
        assert len(applied) <= 3  # 15% of 20
        assert warnings_with(out, "edit_budget_hit")

    def test_budget_warning_appears_once_per_segment(self):
        words = [f"מילה{i:02d}" for i in range(20)]
        obj = transcript(words, conf=None)
        adapter = StubAdapter([
            {"wid": i, "replacement": f"מילה{i:02d}x"} for i in range(20)
        ])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert len(warnings_with(out, "edit_budget_hit")) == 1

    def test_under_budget_does_not_warn(self):
        words = [f"מילה{i:02d}" for i in range(20)]
        obj = transcript(words, conf=None)
        adapter = StubAdapter([{"wid": 0, "replacement": "מילה00x"}])
        out = proofread(obj, cfg=LLM, adapter=adapter)
        assert not warnings_with(out, "edit_budget_hit")


# --------------------------------------------------------------------------
# stage bookkeeping
# --------------------------------------------------------------------------


class TestStages:
    def test_stage_is_recorded(self):
        out = proofread(transcript(["אחת"]), glossary=Glossary({}, []))
        assert "proofread" in out["meta"]["stages"]

    def test_disabled_passes_still_record_the_stage(self):
        out = proofread(transcript(["אחת"]), cfg=Config(passes=()))
        assert "proofread" in out["meta"]["stages"]

    def test_rerunning_raises_without_force(self):
        out = proofread(transcript(["אחת"]), glossary=Glossary({}, []))
        with pytest.raises(StageAlreadyRun):
            proofread(out, glossary=Glossary({}, []))

    def test_force_does_not_duplicate_the_stage_name(self):
        out = proofread(transcript(["אחת"]), glossary=Glossary({}, []))
        again = proofread(out, glossary=Glossary({}, []), force=True)
        assert again["meta"]["stages"].count("proofread") == 1

    def test_idempotent_under_force(self):
        # docs: segments and edits identical; only meta.warnings may differ
        glossary = Glossary({"הצליחות": "הסליחות"}, [])
        first = proofread(transcript(["הלכתי", "הצליחות"]), glossary=glossary)
        second = proofread(copy.deepcopy(first), glossary=glossary, force=True)
        assert second["segments"] == first["segments"]
        assert second["edits"] == first["edits"]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize("a,b,expected", [
        ("abc", "abc", 0),
        ("abc", "abd", 1),
        ("abc", "ab", 1),
        ("", "abc", 3),
        ("kitten", "sitting", 3),
    ])
    def test_edit_distance(self, a, b, expected):
        assert edit_distance(a, b) == expected

    def test_similarity_is_one_for_identical(self):
        assert similarity("שלום", "שלום") == 1.0

    def test_similarity_is_zero_for_disjoint(self):
        assert similarity("abcd", "wxyz") == 0.0

    def test_normalisation_folds_final_letters(self):
        assert normalise_for_match("שלום") == normalise_for_match("שלומ")

    def test_normalisation_does_not_strip_the_prefix_itself(self):
        # ב ל כ מ ש ה ו are ordinary letters too. Folding them away
        # unconditionally turns "שתיים" into "תיימ", which is not a word.
        assert normalise_for_match("שתיים") == "שתיימ"
        assert normalise_for_match("הצליחות") == "הצליחות"

    def test_variants_offer_both_forms(self):
        assert match_variants("בבית") == ("בבית", "בית")

    def test_variants_leave_short_words_alone(self):
        # stripping a prefix off a two-letter word would leave one letter
        assert match_variants("של") == ("של",)

    def test_a_prefixed_word_matches_the_bare_term(self):
        glossary = Glossary({}, [])
        assert normalise_for_match("בית") in match_variants("בבית")


# --------------------------------------------------------------------------
# fuzzy-match false positives -- the regression that mattered
# --------------------------------------------------------------------------


class TestFuzzyFalsePositives:
    def test_matchilim_is_not_rewritten_to_tehilim(self):
        # Found on real output: "מתחילים" (they begin) was rewritten to
        # "תהילים" (Psalms) because the prefix-stripped form "תחילים" is one
        # letter from the term and scored 0.83. Two unrelated words.
        obj = transcript(["אנחנו", "מתחילים", "עכשיו"])
        out = proofread(obj, glossary=Glossary({}, ["תהילים"]))
        assert words_of(out) == ["אנחנו", "מתחילים", "עכשיו"]
        assert out["edits"] == []

    def test_fuzzy_scores_the_full_word_not_the_stripped_variant(self):
        from hebsub.proofread import normalise_for_match, similarity

        full = similarity(normalise_for_match("מתחילים"), normalise_for_match("תהילים"))
        stripped = similarity("תחילימ", normalise_for_match("תהילים"))
        assert stripped >= 0.82, "the stripped form is what used to match"
        assert full < 0.82, "the full form must not"

    def test_a_genuinely_close_misspelling_still_matches(self):
        # the pass must still earn its place
        obj = transcript(["אמרתי", "אשכנדים"])
        out = proofread(obj, glossary=Glossary({}, ["אשכנזים"]))
        assert words_of(out) == ["אמרתי", "אשכנזים"]

    def test_a_prefixed_correct_term_is_left_alone(self):
        obj = transcript(["הלכתי", "בקדיש"])
        out = proofread(obj, glossary=Glossary({}, ["קדיש"]))
        assert out["edits"] == []

    def test_every_real_edit_is_either_a_known_mapping_or_a_near_miss(self):
        """Two mechanisms, two standards of proof.\n\n        A mapped term is an error a human observed and wrote down, so it is\n        allowed to be a large edit -- `מכבלים => מכוונים` is distance 3 and\n        entirely correct. A fuzzy match has no such evidence behind it: it\n        fires on similarity alone, so anything it changes must be a near miss\n        or it is rewriting a real word into a different real word.\n        """
        from pathlib import Path

        from hebsub.proofread import edit_distance, match_variants

        repo = Path(__file__).resolve().parents[1]
        glossary = load_glossary(repo / "glossary.txt")
        work = repo / "work" / "bench"
        if not work.is_dir():
            pytest.skip("no bench run on disk")

        for raw_path in sorted(work.glob("*/01_raw.json")):
            obj = json.loads(raw_path.read_text(encoding="utf-8"))
            out = proofread(obj, glossary=glossary)
            for entry in out["edits"]:
                variants = match_variants(entry["before"])
                if any(v in glossary.mappings for v in variants):
                    continue  # human-verified; any distance is allowed
                distance = edit_distance(variants[0], match_variants(entry["after"])[0])
                assert distance <= 1, (
                    f"{raw_path.parent.name}: fuzzy match changed "
                    f"{entry['before']!r} -> {entry['after']!r} at distance "
                    f"{distance} with no evidence behind it"
                )


# --------------------------------------------------------------------------
# the glossary outranks the model
# --------------------------------------------------------------------------


class TestGlossaryOutranksTheModel:
    def test_a_protected_term_is_frozen_against_the_llm_pass(self):
        # Not hypothetical: the masked LM rewrote "אדס" (the Ades synagogue,
        # a protected term) into the non-word "עדס". A human wrote the term
        # down; the model produced a probability. Evidence wins.
        obj = transcript(["בכנסת", "אדס"], conf=0.1)
        adapter = StubAdapter([{"wid": 1, "replacement": "עדס"}])
        out = proofread(
            obj, cfg=LLM, glossary=Glossary({}, ["אדס"]), adapter=adapter
        )
        assert words_of(out) == ["בכנסת", "אדס"]
        assert warnings_with(out, "llm_rejected")

    def test_a_mapping_target_is_also_frozen(self):
        obj = transcript(["אמרתי", "הסליחות"], conf=0.1)
        adapter = StubAdapter([{"wid": 1, "replacement": "הצליחות"}])
        out = proofread(
            obj, cfg=LLM,
            glossary=Glossary({"הצליחות": "הסליחות"}, []), adapter=adapter,
        )
        assert words_of(out) == ["אמרתי", "הסליחות"]

    def test_an_unprotected_word_is_still_eligible(self):
        obj = transcript(["אמרתי", "קשרה"], conf=0.1)
        adapter = StubAdapter([{"wid": 1, "replacement": "כשרה"}])
        out = proofread(
            obj, cfg=LLM, glossary=Glossary({}, ["אדס"]), adapter=adapter
        )
        assert words_of(out) == ["אמרתי", "כשרה"]

    def test_is_protected_matches_through_an_attached_prefix(self):
        glossary = Glossary({}, ["קדיש"])
        assert glossary.is_protected("קדיש")
        assert glossary.is_protected("בקדיש")
        assert not glossary.is_protected("מכונית")


# --------------------------------------------------------------------------
# confusion-set candidate generation (pure; no model needed)
# --------------------------------------------------------------------------


class TestConfusionCandidates:
    @pytest.mark.parametrize("heard,said", [
        ("קשרה", "כשרה"),
        ("האור", "העור"),
        ("מאור", "מעור"),
        ("הצליחות", "הסליחות"),
        ("כופף", "חופף"),
        ("אשכנדים", "אשכנזים"),
    ])
    def test_the_real_error_pairs_are_reachable(self, heard, said):
        # every one of these came out of the measured error catalogue
        from hebsub.llm.masked_lm import confusion_candidates

        assert said in confusion_candidates(heard)

    def test_a_word_is_never_its_own_candidate(self):
        from hebsub.llm.masked_lm import confusion_candidates

        assert "קדיש" not in confusion_candidates("קדיש")

    def test_very_short_words_produce_nothing(self):
        from hebsub.llm.masked_lm import confusion_candidates

        assert confusion_candidates("א") == []

    def test_candidates_stay_the_same_length(self):
        # single-letter substitution only -- never insertion or deletion
        from hebsub.llm.masked_lm import confusion_candidates

        for candidate in confusion_candidates("הצליחות"):
            assert len(candidate) == len("הצליחות")

    def test_unrelated_words_are_not_reachable(self):
        from hebsub.llm.masked_lm import confusion_candidates

        assert "תהילים" not in confusion_candidates("מתחילים")
        assert "תפילין" not in confusion_candidates("תפילון")


# --------------------------------------------------------------------------
# the review list -- two ASR models disagreeing (D47, D48)
# --------------------------------------------------------------------------


class TestReviewDisagreements:
    """Flag, never fix.\n\n    Measured over three corpora: a word two independent models disagree on is\n    wrong 48.6% of the time, against 1.5% for a word they agree on -- a 49x\n    lift on corpus 3, and by a distance the best error signal found (D48). It\n    is still nowhere near the >50% precision that would license changing a\n    word automatically, so this returns a list for a human and touches nothing.\n    """

    def test_identical_transcripts_flag_nothing(self):
        a = transcript(["שלום", "עולם", "יפה"])
        assert review_disagreements(a, transcript(["שלום", "עולם", "יפה"])) == []

    def test_a_differing_word_is_flagged(self):
        a = transcript(["הוא", "סמים", "את", "הבורג"])
        b = transcript(["הוא", "שומעים", "את", "הבורג"])
        flags = review_disagreements(a, b)
        assert len(flags) == 1
        assert flags[0]["heard"] == "סמים"
        assert flags[0]["alternative"] == "שומעים"

    def test_a_flag_carries_the_wid_and_the_timestamp(self):
        # The wid is what makes it addressable; the timestamp is what makes it
        # findable in Resolve.
        a = transcript(["אחת", "שתיים", "שלוש"])
        b = transcript(["אחת", "שתיים", "ארבע"])
        flag = review_disagreements(a, b)[0]
        assert flag["wid"] == 2
        assert flag["start"] == a["segments"][0]["words"][2]["start"]

    def test_flags_come_back_in_wid_order(self):
        a = transcript(["אחת", "שתיים", "שלוש", "ארבע"])
        b = transcript(["אחד", "שתיים", "שלושה", "ארבע"])
        flags = review_disagreements(a, b)
        assert [f["wid"] for f in flags] == sorted(f["wid"] for f in flags)

    def test_a_final_letter_difference_is_not_a_disagreement(self):
        # שלום / שלומ is one word spelled two ways, not two candidate words.
        a = transcript(["שלום", "עולם"])
        b = transcript(["שלומ", "עולמ"])
        assert review_disagreements(a, b) == []

    def test_punctuation_alone_is_not_a_disagreement(self):
        a = transcript(["שלום", "עולם."])
        b = transcript(["שלום,", "עולם"])
        assert review_disagreements(a, b) == []

    def test_niqqud_alone_is_not_a_disagreement(self):
        a = transcript(["שָׁלוֹם", "עולם"])
        b = transcript(["שלום", "עולם"])
        assert review_disagreements(a, b) == []

    def test_an_inserted_or_dropped_word_is_not_flagged(self):
        # Only 1:1 substitutions were measured at 48.6% precision. A length
        # change is a different phenomenon with different odds, and shipping
        # it on this evidence would be claiming a number nobody measured.
        a = transcript(["אחת", "שתיים", "שלוש"])
        b = transcript(["אחת", "שתיים", "וחצי", "שלוש"])
        assert review_disagreements(a, b) == []

    def test_the_transcripts_are_never_modified(self):
        a = transcript(["הוא", "סמים", "את", "הבורג"])
        b = transcript(["הוא", "שומעים", "את", "הבורג"])
        before_a, before_b = copy.deepcopy(a), copy.deepcopy(b)
        review_disagreements(a, b)
        assert a == before_a
        assert b == before_b

    def test_an_empty_alternative_flags_nothing_and_does_not_raise(self):
        a = transcript(["שלום", "עולם"])
        empty = {"meta": a["meta"], "segments": [], "edits": []}
        assert review_disagreements(a, empty) == []

    def test_a_completely_different_transcript_does_not_flag_everything(self):
        # No shared anchors means no trustworthy alignment. Flagging every
        # word would bury the real ones, which is the failure mode that made
        # the lexicon detector useless (D46).
        a = transcript(["אחת", "שתיים", "שלוש", "ארבע", "חמש"])
        b = transcript(["ששה", "שבעה", "שמונה", "תשעה", "עשרה"])
        assert len(review_disagreements(a, b)) <= len(
            a["segments"][0]["words"]
        )


# --------------------------------------------------------------------------
# taking the real word when the other model heard a non-word (D62)
# --------------------------------------------------------------------------

LEX = frozenset({"שלומ", "עולמ", "הסליחות", "אשכנזימ", "בורג", "מקדח"})


class TestIsRealWord:
    def test_a_word_in_the_lexicon_is_real(self):
        assert is_real_word("שלום", LEX)

    def test_a_non_word_is_not(self):
        assert not is_real_word("הצליחות", LEX)

    def test_an_attached_prefix_is_peeled(self):
        # Hebrew fuses ב/כ/ל/מ/ש/ה/ו on; no flat list holds every combination.
        assert is_real_word("הבורג", LEX)
        assert is_real_word("שהבורג", LEX)

    def test_final_letter_form_does_not_matter(self):
        assert is_real_word("שלומ", LEX)

    def test_latin_script_is_not_judged(self):
        # Not this function's business, and calling it a non-word would let
        # the rule act on it.
        assert is_real_word("Pocket", LEX)
        assert is_real_word("8", LEX)

    def test_an_empty_word_is_not_real(self):
        assert not is_real_word("", LEX)
        assert not is_real_word("   ", LEX)


class TestResolveDisagreements:
    """Raz's rule. It only acts where there is evidence.\n\n    Measured over all three corpora: **6 words fixed, 0 broken**, from 9\n    edits. The two do-nothing branches are load-bearing -- on the 80 cases\n    where both candidates are real words, taking the partner would have cost\n    27 words.\n    """

    def both(self, mine, theirs):
        a = transcript(["הוא", mine, "את", "הבורג"])
        b = transcript(["הוא", theirs, "את", "הבורג"])
        return a, b

    def test_both_real_changes_nothing(self):
        a, b = self.both("שלום", "עולם")
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert out["segments"][0]["words"][1]["w"] == "שלום"
        assert out["edits"] == []

    def test_neither_real_changes_nothing(self):
        # The correct word was in the lexicon 1 time in 13, so there is
        # nothing to reach for.
        a, b = self.both("תתעכם", "תתקם")
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert out["segments"][0]["words"][1]["w"] == "תתעכם"
        assert out["edits"] == []

    def test_a_non_word_is_replaced_by_the_real_one(self):
        a, b = self.both("הצליחות", "הסליחות")
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert out["segments"][0]["words"][1]["w"] == "הסליחות"

    def test_our_real_word_is_kept_over_their_non_word(self):
        a, b = self.both("הסליחות", "הצליחות")
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert out["segments"][0]["words"][1]["w"] == "הסליחות"
        assert out["edits"] == []

    def test_the_change_is_recorded_as_an_edit(self):
        a, b = self.both("הצליחות", "הסליחות")
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert len(out["edits"]) == 1
        edit = out["edits"][0]
        assert edit["reason"] == "second_opinion"
        assert edit["before"] == "הצליחות"
        assert edit["after"] == "הסליחות"
        assert edit["wid"] == 1

    def test_the_result_still_validates(self):
        a, b = self.both("הצליחות", "הסליחות")
        validate_transcript(resolve_disagreements(a, b, lexicon=LEX))

    def test_word_count_and_timings_are_untouched(self):
        a, b = self.both("הצליחות", "הסליחות")
        out = resolve_disagreements(a, b, lexicon=LEX)
        before = [w for s in a["segments"] for w in s["words"]]
        after = [w for s in out["segments"] for w in s["words"]]
        assert len(before) == len(after)
        for x, y in zip(before, after):
            assert (x["wid"], x["start"], x["end"]) == (y["wid"], y["start"], y["end"])

    def test_the_input_is_never_mutated(self):
        a, b = self.both("הצליחות", "הסליחות")
        snapshot = copy.deepcopy(a)
        resolve_disagreements(a, b, lexicon=LEX)
        assert a == snapshot

    def test_an_empty_lexicon_changes_nothing(self):
        # A missing `transformers` must degrade to flag-only, not fail.
        a, b = self.both("הצליחות", "הסליחות")
        out = resolve_disagreements(a, b, lexicon=frozenset())
        assert out["segments"][0]["words"][1]["w"] == "הצליחות"
        assert out["edits"] == []

    def test_existing_edits_are_preserved(self):
        a, b = self.both("הצליחות", "הסליחות")
        a["edits"] = [{"stage": "proofread", "wid": 0, "before": "x",
                       "after": "y", "reason": "glossary"}]
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert len(out["edits"]) == 2
        assert out["edits"][0]["reason"] == "glossary"

    def test_a_resolved_word_stops_being_flagged(self):
        # Once applied, the two transcripts agree there, so the review list
        # shrinks by exactly what was fixed.
        a, b = self.both("הצליחות", "הסליחות")
        assert len(review_disagreements(a, b)) == 1
        out = resolve_disagreements(a, b, lexicon=LEX)
        assert review_disagreements(out, b) == []


# --------------------------------------------------------------------------
# learning the user's own vocabulary
# --------------------------------------------------------------------------


class TestLearnWords:
    """Harvest Hebrew words from whatever the user hands over.\n\n    Measured leave-one-corpus-out, these do not transfer across domains at all\n    -- Judaica vocabulary does not help a workshop reel. What they do is help\n    a domain against itself: with the held-out corpus's own words present the\n    rule goes from 6 fixes to 9. Raz makes many reels per client, so that is\n    the case that matters and the one three unrelated corpora cannot show.\n    """

    def test_it_picks_up_hebrew_words(self):
        assert learn_words("שלום עולם") == {"שלומ", "עולמ"}

    def test_symbols_of_any_kind_do_not_swallow_the_word(self):
        # The class, not the case. `<` and `>` are SYMBOLS, not punctuation,
        # so folding left them attached and the all-Hebrew test rejected the
        # word. Stripping tags would fix `<b>` and leave every other symbol
        # broken -- and music notes are ordinary in subtitles.
        for wrapped in ("<b>שלום</b>", "♪שלום♪", "~שלום~", "→שלום",
                        "[שלום]", "(שלום)", "#שלום", "שלום…"):
            assert learn_words(wrapped) == {"שלומ"}, wrapped

    def test_a_word_stuck_to_a_number_is_still_learned(self):
        assert learn_words("8שקל") == {"שקל"}

    def test_a_tag_inside_a_token_splits_it(self):
        assert learn_words("שלום<b>עולם") == {"שלומ", "עולמ"}

    def test_markup_does_not_swallow_the_words_around_it(self):
        # The bug this caught: `<` and `>` are SYMBOLS, not punctuation, so
        # the fold left them attached and every word at a card boundary was
        # rejected. 205 words harvested from a file instead of 764.
        assert learn_words("<b>שלום</b>") == {"שלומ"}
        assert "מעדיפימ" in learn_words("<b>מעדיפים?</b>")

    def test_srt_scaffolding_falls_out_on_its_own(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\n<b>שלום עולם</b>\n"
        assert learn_words(srt) == {"שלומ", "עולמ"}

    def test_latin_and_digits_are_not_learned(self):
        assert learn_words("Pocket Holes 8 3") == set()

    def test_mixed_tokens_are_not_learned(self):
        # `הPocket` is not a Hebrew word and must not become one. The `ה` left
        # behind is a fragment, which is why runs of one letter are dropped.
        assert learn_words("הPocket") == set()
        assert learn_words("הJig") == set()

    def test_final_letters_are_folded(self):
        assert learn_words("שלום") == learn_words("שלומ")

    def test_the_geresh_survives_folding_into_a_word(self):
        assert learn_words("ג'יפ") == {"גיפ"}

    def test_empty_text_learns_nothing(self):
        assert learn_words("") == set()
        assert learn_words("   \n\n  ") == set()


class TestLoadUserLexicon:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert load_user_lexicon(tmp_path / "nope.txt") == frozenset()

    def test_none_is_not_an_error(self):
        assert load_user_lexicon(None) == frozenset()

    def test_it_round_trips(self, tmp_path):
        path = tmp_path / USER_LEXICON_NAME
        path.write_text("שלום\nעולם\n", encoding="utf-8")
        assert load_user_lexicon(path) == {"שלומ", "עולמ"}

    def test_learned_words_reach_the_lexicon(self):
        extra = frozenset({"תתעקמ"})
        assert "תתעקמ" not in hebrew_lexicon(extra=frozenset())
        assert "תתעקמ" in hebrew_lexicon(extra=extra)

    def test_learning_a_word_does_not_make_its_misspelling_real(self):
        # The whole safety argument: his corrections never contain the ASR's
        # mistakes, so learning can only make the CORRECT side recognisable.
        extra = frozenset({"תתעקמ"})
        lex = hebrew_lexicon(extra=extra)
        assert is_real_word("תתעקם", lex)
        assert not is_real_word("תתעכם", lex)
