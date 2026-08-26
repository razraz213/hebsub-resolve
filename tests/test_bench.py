"""Tests for the bench module.

Assertions come from docs/modules/bench.md. The metric functions are tested
against hand-built pairs with known answers, never against the pipeline --
a metric validated by the thing it measures is not a metric.
"""

from __future__ import annotations

import csv
import json

import pytest

from hebsub.bench import (
    COLUMNS,
    WARNING_CODES,
    BenchError,
    append_rows,
    boundary_f1,
    count_warnings,
    entity_accuracy,
    hebrew_rule_breakdown,
    load_glossary_terms,
    normalise_words,
    punct_f1,
    stats_columns,
    wer,
    wer_tolerant,
)


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------


class TestNormalisation:
    def test_strips_punctuation(self):
        assert normalise_words("שלום, עולם.") == ["שלומ", "עולמ"]

    def test_unifies_final_letters(self):
        assert normalise_words("שלום") == normalise_words("שלומ")

    def test_strips_niqqud(self):
        assert normalise_words("שָׁלוֹם") == normalise_words("שלום")

    def test_collapses_whitespace(self):
        assert normalise_words("  שלום \n\t עולם  ") == ["שלומ", "עולמ"]


# --------------------------------------------------------------------------
# WER
# --------------------------------------------------------------------------


class TestWer:
    def test_identical_is_zero(self):
        assert wer("אחת שתיים שלוש", "אחת שתיים שלוש") == 0.0

    def test_one_substitution_in_four(self):
        assert wer("אחת שתיים שלוש ארבע", "אחת שתיים שבע ארבע") == pytest.approx(0.25)

    def test_one_deletion_in_four(self):
        assert wer("אחת שתיים שלוש ארבע", "אחת שתיים ארבע") == pytest.approx(0.25)

    def test_one_insertion_in_four(self):
        assert wer("אחת שתיים שלוש ארבע", "אחת שתיים שלוש חמש ארבע") == pytest.approx(0.25)

    def test_punctuation_alone_is_not_an_error(self):
        # punctuation is scored by punct_f1; folding it into WER would punish
        # a model for punctuating correctly
        assert wer("אחת שתיים שלוש", "אחת, שתיים; שלוש.") == 0.0

    def test_final_letter_form_alone_is_not_an_error(self):
        assert wer("שלום עולם", "שלומ עולמ") == 0.0

    def test_completely_wrong_is_one(self):
        assert wer("אחת שתיים", "שבע שמונה") == pytest.approx(1.0)

    def test_empty_reference_raises(self):
        with pytest.raises(BenchError):
            wer("", "משהו")


# --------------------------------------------------------------------------
# entity accuracy
# --------------------------------------------------------------------------


class TestEntityAccuracy:
    def test_all_terms_correct(self):
        got = entity_accuracy("הלכתי לבית כנסת", "הלכתי לבית כנסת", ["בית כנסת"])
        assert got == pytest.approx(1.0)

    def test_missed_term(self):
        got = entity_accuracy("הלכתי לבית כנסת", "הלכתי לבית קפה", ["בית כנסת"])
        assert got == pytest.approx(0.0)

    def test_half_the_occurrences(self):
        got = entity_accuracy(
            "קדיש וגם קדיש", "קדיש וגם קידוש", ["קדיש"]
        )
        assert got == pytest.approx(0.5)

    def test_returns_none_when_no_term_appears(self):
        # null, not 0.0 -- nothing was measured
        assert entity_accuracy("שלום עולם", "שלום עולם", ["נטפליקס"]) is None

    def test_returns_none_without_a_glossary(self):
        assert entity_accuracy("שלום", "שלום", []) is None

    def test_multiword_term_must_match_in_order(self):
        got = entity_accuracy("בית כנסת גדול", "כנסת בית גדול", ["בית כנסת"])
        assert got == pytest.approx(0.0)


# --------------------------------------------------------------------------
# punctuation F1
# --------------------------------------------------------------------------


class TestPunctF1:
    def test_perfect_punctuation(self):
        assert punct_f1("שלום, עולם.", "שלום, עולם.") == pytest.approx(1.0)

    def test_no_punctuation_produced(self):
        assert punct_f1("שלום, עולם.", "שלום עולם") == pytest.approx(0.0)

    def test_returns_none_when_reference_has_none(self):
        # nothing to score; 0.0 would read as total failure
        assert punct_f1("שלום עולם", "שלום, עולם.") is None

    def test_half_right(self):
        got = punct_f1("אחת, שתיים. שלוש", "אחת, שתיים שלוש")
        assert got == pytest.approx(2 / 3)

    def test_wrong_mark_counts_against_both_sides(self):
        got = punct_f1("שלום.", "שלום?")
        assert got == pytest.approx(0.0)

    def test_scores_only_words_that_align(self):
        # the differing word is WER's problem, not punctuation's
        assert punct_f1("אחת. שתיים", "אחת. שבע") == pytest.approx(1.0)


# --------------------------------------------------------------------------
# boundary F1 (D33)
# --------------------------------------------------------------------------


class TestBoundaryF1:
    def test_identical_cuts(self):
        assert boundary_f1([0.0, 1.0, 2.0], [0.0, 1.0, 2.0]) == pytest.approx(1.0)

    def test_within_tolerance_counts_as_a_match(self):
        assert boundary_f1([1.0], [1.05], tol=0.12) == pytest.approx(1.0)

    def test_outside_tolerance_does_not(self):
        assert boundary_f1([1.0], [1.5], tol=0.12) == pytest.approx(0.0)

    def test_over_segmentation_is_punished_by_precision(self):
        # every reference cut is matched, but half the produced cuts are junk
        got = boundary_f1([0.0, 2.0], [0.0, 1.0, 2.0, 3.0])
        assert got == pytest.approx(2 * 1.0 * 0.5 / 1.5)

    def test_recall_alone_would_not_catch_that(self):
        # guards the reason the column is F1 and not recall
        cutting_everywhere = [i / 10 for i in range(100)]
        assert boundary_f1([0.0, 5.0], cutting_everywhere) < 0.1

    def test_returns_none_without_a_reference(self):
        assert boundary_f1([], [1.0, 2.0]) is None

    def test_no_produced_cuts_is_zero_not_none(self):
        assert boundary_f1([1.0], []) == 0.0


# --------------------------------------------------------------------------
# warnings
# --------------------------------------------------------------------------


def warning(code: str, detail: str = "x", **extra) -> dict:
    return {"stage": "segment", "code": code, "detail": detail, **extra}


class TestWarningCounting:
    def test_empty_list_gives_all_zeros(self):
        counts = count_warnings([])
        assert set(counts) == set(WARNING_CODES)
        assert all(v == 0 for v in counts.values())

    def test_counts_by_code(self):
        counts = count_warnings([
            warning("cps_exceeded"), warning("cps_exceeded"),
            warning("card_too_short"),
        ])
        assert counts["cps_exceeded"] == 2
        assert counts["card_too_short"] == 1
        assert counts["line_too_long"] == 0

    def test_unknown_code_raises_naming_bench_and_the_code(self):
        with pytest.raises(BenchError, match="made_up"):
            count_warnings([warning("made_up")])

    def test_the_enum_is_the_eight_v1_codes(self):
        assert len(WARNING_CODES) == 8
        assert "hebrew_rule_violation" in WARNING_CODES
        assert "itn_skipped" not in WARNING_CODES


class TestHebrewRuleBreakdown:
    def test_groups_by_rule_id(self):
        got = hebrew_rule_breakdown([
            warning("hebrew_rule_violation", "et_split: a"),
            warning("hebrew_rule_violation", "function_word_line_end: b"),
            warning("hebrew_rule_violation", "et_split: c"),
        ])
        assert got == "et_split=2;function_word_line_end=1"

    def test_sorted_alphabetically(self):
        got = hebrew_rule_breakdown([
            warning("hebrew_rule_violation", "zzz: a"),
            warning("hebrew_rule_violation", "aaa: b"),
        ])
        assert got == "aaa=1;zzz=1"

    def test_empty_when_there_are_none(self):
        assert hebrew_rule_breakdown([warning("cps_exceeded")]) == ""

    def test_sum_equals_the_warning_count(self):
        warnings = [
            warning("hebrew_rule_violation", "et_split: a"),
            warning("hebrew_rule_violation", "et_split: b"),
            warning("hebrew_rule_violation", "number_unit_split: c"),
        ]
        total = sum(
            int(part.split("=")[1])
            for part in hebrew_rule_breakdown(warnings).split(";")
        )
        assert total == count_warnings(warnings)["hebrew_rule_violation"] == 3

    def test_detail_without_a_rule_id_raises(self):
        with pytest.raises(BenchError):
            hebrew_rule_breakdown([
                warning("hebrew_rule_violation", "line ends on a function word")
            ])


# --------------------------------------------------------------------------
# export's report is authoritative
# --------------------------------------------------------------------------


def report(**stats) -> dict:
    base = {
        "cards": 100, "cards_over_cps": 4, "cards_over_line_len": 0,
        "cards_under_min_duration": 7, "max_cps": 21.4, "mean_cps": 12.8,
    }
    base.update(stats)
    return {"source": "03_segmented.json", "warnings": [], "stats": base}


class TestStatsColumns:
    def test_hand_written_report_lands_unchanged(self, tmp_path):
        # exists specifically to catch someone reintroducing SRT parsing
        path = tmp_path / "final.srt.report.json"
        path.write_text(json.dumps(report(), ensure_ascii=False), encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        got = stats_columns(loaded)
        assert got["cards"] == 100
        assert got["cards_over_cps"] == 4
        assert got["max_cps"] == 21.4
        assert got["mean_cps"] == 12.8

    def test_pct_over_cps_is_derived_from_two_reported_counts(self):
        got = stats_columns(report(cards=200, cards_over_cps=5))
        assert got["pct_over_cps"] == pytest.approx(0.025)

    def test_pct_is_null_not_zero_when_there_are_no_cards(self):
        assert stats_columns(report(cards=0, cards_over_cps=0))["pct_over_cps"] == ""

    @pytest.mark.parametrize("key", [
        "cards", "cards_over_cps", "cards_over_line_len",
        "cards_under_min_duration", "max_cps", "mean_cps",
    ])
    def test_missing_stat_raises_naming_bench_and_the_key(self, key):
        broken = report()
        del broken["stats"][key]
        with pytest.raises(BenchError, match=key):
            stats_columns(broken)

    def test_report_without_stats_raises(self):
        with pytest.raises(BenchError, match="stats"):
            stats_columns({"source": "x", "warnings": []})


# --------------------------------------------------------------------------
# CSV behaviour
# --------------------------------------------------------------------------


def row(**overrides) -> dict:
    base = {column: "" for column in COLUMNS}
    base.update({"clip": "c", "engine": "ivrit_local", "status": "ok"})
    base.update(overrides)
    return base


class TestCsv:
    def test_header_written_once_then_rows_appended(self, tmp_path):
        out = tmp_path / "bench.csv"
        append_rows([row(clip="a")], out)
        append_rows([row(clip="b")], out)
        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert lines[0].startswith("clip,")
        assert len(lines) == 3  # header + 2 rows

    def test_rerunning_leaves_earlier_rows_untouched(self, tmp_path):
        out = tmp_path / "bench.csv"
        append_rows([row(clip="first")], out)
        before = out.read_text(encoding="utf-8")
        append_rows([row(clip="second")], out)
        assert out.read_text(encoding="utf-8").startswith(before)

    def test_failure_reason_with_a_comma_is_quoted(self, tmp_path):
        out = tmp_path / "bench.csv"
        append_rows(
            [row(status="failed", failure_reason="boom, and then, more boom")], out
        )
        parsed = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
        assert parsed[0]["failure_reason"] == "boom, and then, more boom"

    def test_hebrew_survives_the_round_trip(self, tmp_path):
        out = tmp_path / "bench.csv"
        append_rows([row(clip="סרטון לדוגמה")], out)
        parsed = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
        assert parsed[0]["clip"] == "סרטון לדוגמה"

    def test_a_measured_zero_is_written_as_zero(self, tmp_path):
        out = tmp_path / "bench.csv"
        append_rows([row(warn_cps_exceeded=0, cards=0)], out)
        parsed = list(csv.DictReader(out.read_text(encoding="utf-8").splitlines()))
        # distinguishable from the empty string a failed row would carry
        assert parsed[0]["warn_cps_exceeded"] == "0"
        assert parsed[0]["boundary_f1"] == ""


# --------------------------------------------------------------------------
# glossary
# --------------------------------------------------------------------------


class TestGlossary:
    def test_takes_the_right_hand_side_of_a_mapping(self, tmp_path):
        path = tmp_path / "g.txt"
        path.write_text("נטפליקט => נטפליקס\n", encoding="utf-8")
        assert load_glossary_terms(path) == ["נטפליקס"]

    def test_keeps_bare_terms(self, tmp_path):
        path = tmp_path / "g.txt"
        path.write_text("קדיש\nטלית\n", encoding="utf-8")
        assert load_glossary_terms(path) == ["קדיש", "טלית"]

    def test_ignores_comments_and_blanks(self, tmp_path):
        path = tmp_path / "g.txt"
        path.write_text("# a comment\n\nקדיש   # trailing\n", encoding="utf-8")
        assert load_glossary_terms(path) == ["קדיש"]

    def test_missing_file_is_not_an_error(self, tmp_path):
        assert load_glossary_terms(tmp_path / "nope.txt") == []


# --------------------------------------------------------------------------
# D25 -- bench holds no Hebrew logic
# --------------------------------------------------------------------------


def _bench_code_only() -> str:
    """bench.py with comments and string literals removed.

    Docstrings legitimately mention the rule ids while explaining that the
    module does not know them, so a plain substring search on the file gives a
    false positive. What matters is that no rule id appears in code.
    """
    import io
    import tokenize
    from pathlib import Path

    import hebsub.bench as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    kept = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(token.string)
    return " ".join(kept)


class TestNoHebrewLogic:
    @pytest.mark.parametrize("rule_id", [
        "et_split", "function_word_line_end", "construct_chain_split",
        "number_unit_split", "english_phrase_split",
    ])
    def test_no_hebrew_rule_id_appears_in_bench_code(self, rule_id):
        # D25: the ids are read off `detail`, never enumerated here. Adding a
        # Hebrew rule must not require touching bench.
        assert rule_id not in _bench_code_only(), rule_id

    def test_no_hebrew_word_lists_in_bench_code(self):
        # The final-form translation table is Hebrew but is orthography, not
        # linguistics; it lives in a string literal, which is stripped above.
        hebrew = {ch for ch in _bench_code_only() if "֐" <= ch <= "׿"}
        assert not hebrew, f"unexpected Hebrew in bench code: {hebrew}"


# --------------------------------------------------------------------------
# spelling-tolerant WER (D39)
# --------------------------------------------------------------------------


class TestMatresLectionis:
    @pytest.mark.parametrize("plene,defective", [
        ("המשלוח", "המשלח"),
        ("לייבא", "ליבא"),
        ("הייבוא", "היבוא"),
        ("יכולתי", "יכלתי"),
    ])
    def test_real_spelling_pairs_collapse(self, plene, defective):
        # every one of these was counted as an error on Raz's own test clip
        from hebsub.bench import matres_skeleton

        assert matres_skeleton(plene) == matres_skeleton(defective)

    def test_tolerant_wer_forgives_a_spelling_variant(self):
        assert wer("המשלוח הראשון", "המשלח הראשון") > 0
        assert wer_tolerant("המשלוח הראשון", "המשלח הראשון") == 0.0

    def test_tolerant_wer_still_charges_a_real_error(self):
        # ק/כ changes the word: "follow" vs "delay"
        assert wer_tolerant("פשוט תעקבו", "פשוט תעכבו") > 0

    def test_tolerant_never_exceeds_strict(self):
        pairs = [
            ("המשלוח הראשון שיבאתי", "המשלח הראשון שהבאתי"),
            ("אחת שתיים שלוש", "אחת שתיים שלוש"),
            ("יכולתי לשחרר", "יכלתי לשחרר"),
        ]
        for ref, hyp in pairs:
            assert wer_tolerant(ref, hyp) <= wer(ref, hyp)

    def test_strict_wer_is_unchanged(self):
        # bench.csv history stays comparable; the tolerant figure is a new
        # column, not a redefinition
        assert wer("אחת שתיים שלוש ארבע", "אחת שתיים שבע ארבע") == pytest.approx(0.25)

    def test_both_columns_exist(self):
        assert "wer" in COLUMNS
        assert "wer_tolerant" in COLUMNS
