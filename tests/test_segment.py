"""Tests for the segment module.

Assertions come from docs/modules/segment.md and D30. The hard rules are
tested as invariants; the quality targets are tested as warnings.

Everything here runs on synthetic JSON, which docs/eval-protocol.md explicitly
allows for this module -- no audio, no fixtures needed.
"""

from __future__ import annotations

import pytest

from hebsub.contract import ContractError, StageAlreadyRun, validate_subtitle_file
from hebsub.segment import HEBREW_RULE_IDS, Config, SegmentError, segment


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def transcript_from(words: list[str], *, per_word: float = 0.3) -> dict:
    """A one-segment Transcript whose words tick along at a fixed rate."""
    built = []
    for i, w in enumerate(words):
        built.append({
            "wid": i,
            "w": w,
            "start": round(i * per_word, 3),
            "end": round((i + 1) * per_word, 3),
            "conf": 0.9,
        })
    return {
        "meta": {
            "source_file": "synthetic.wav",
            "duration": round(len(words) * per_word, 3),
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
            "end": round(len(words) * per_word, 3),
            "text": " ".join(words),
            "words": built,
            "speaker": None,
        }],
        "edits": [],
    }


def cards_of(obj: dict) -> list[str]:
    return [seg["text"] for seg in obj["segments"]]


def warnings_with(obj: dict, code: str) -> list[dict]:
    return [w for w in obj["meta"]["warnings"] if w["code"] == code]


SPEECH = [
    "הדבר", "הכי", "טוב", "כדי", "לזכור", "בן", "אדם", "זה", "לזכור",
    "אותו", "בשם", "הטוב", "שעושים", "בזכותו.",
]


# --------------------------------------------------------------------------
# hard rules -- invariants
# --------------------------------------------------------------------------


class TestHardRules:
    def test_output_validates_as_a_subtitle_file(self):
        out = segment(transcript_from(SPEECH))
        validate_subtitle_file(out)

    def test_never_more_than_two_lines(self):
        out = segment(transcript_from(SPEECH * 3))
        assert all(1 <= len(s["lines"]) <= 2 for s in out["segments"])

    def test_no_line_exceeds_max_line_length(self):
        cfg = Config(max_chars_per_card=60, max_line_length=20)
        out = segment(transcript_from(SPEECH * 2), cfg=cfg)
        for seg in out["segments"]:
            for line in seg["lines"]:
                # a single word longer than the limit is the documented
                # exception, and must be warned about
                if len(line) > cfg.max_line_length:
                    assert len(line.split()) == 1

    def test_no_card_exceeds_max_duration(self):
        cfg = Config(max_card_duration=1.0)
        out = segment(transcript_from(SPEECH, per_word=0.4), cfg=cfg)
        for seg in out["segments"]:
            assert seg["end"] - seg["start"] <= cfg.max_card_duration + 1e-9

    def test_no_timestamp_is_invented(self):
        obj = transcript_from(SPEECH)
        starts = {w["start"] for w in obj["segments"][0]["words"]}
        ends = {w["end"] for w in obj["segments"][0]["words"]}
        out = segment(obj)
        for seg in out["segments"]:
            assert seg["start"] in starts
            assert seg["end"] in ends

    def test_wid_sequence_is_reproduced_exactly(self):
        obj = transcript_from(SPEECH)
        before = [w["wid"] for s in obj["segments"] for w in s["words"]]
        out = segment(obj)
        after = [w["wid"] for s in out["segments"] for w in s["words"]]
        assert after == before  # strict equality, not a count

    def test_no_word_is_lost_or_reordered(self):
        obj = transcript_from(SPEECH)
        before = [w["w"] for s in obj["segments"] for w in s["words"]]
        out = segment(obj)
        after = [w["w"] for s in out["segments"] for w in s["words"]]
        assert after == before

    def test_cards_never_overlap(self):
        out = segment(transcript_from(SPEECH * 2))
        segs = out["segments"]
        for a, b in zip(segs, segs[1:]):
            assert b["start"] >= a["end"] - 1e-9

    def test_ids_are_renumbered_from_zero(self):
        out = segment(transcript_from(SPEECH * 2))
        assert [s["id"] for s in out["segments"]] == list(
            range(len(out["segments"]))
        )

    def test_lines_rejoin_to_the_card_text(self):
        out = segment(transcript_from(SPEECH * 2))
        for seg in out["segments"]:
            assert " ".join(seg["lines"]) == seg["text"]


# --------------------------------------------------------------------------
# D30 -- the short-form profile
# --------------------------------------------------------------------------


class TestD30:
    def test_cards_never_breach_the_hard_ceiling(self):
        cfg = Config(max_chars_per_card=15)
        out = segment(transcript_from(SPEECH * 3), cfg=cfg)
        assert all(len(s["text"]) <= cfg.max_chars_per_card for s in out["segments"])

    def test_the_default_width_is_a_hard_wall(self):
        # D41: the soft-width experiment measured out flat across the three
        # corpora, so headroom ships off. The default must therefore behave
        # exactly as it did before the knob existed.
        cfg = Config(max_chars_per_card=15)
        assert cfg.target_chars_per_card == 15
        assert cfg.max_chars_per_card == 15
        out = segment(transcript_from(SPEECH * 3), cfg=cfg)
        assert all(len(s["text"]) <= 15 for s in out["segments"])

    def test_headroom_lifts_the_ceiling_when_asked_for(self):
        cfg = Config(max_chars_per_card=15, width_headroom=4)
        assert cfg.target_chars_per_card == 15
        assert cfg.max_chars_per_card == 19

    def test_a_card_may_exceed_the_target_only_with_headroom(self):
        words = ["מילה", "ארוכה", "מאוד", "כאן", "ועוד", "אחת"]
        wall = segment(transcript_from(words), cfg=Config(max_chars_per_card=10))
        assert all(len(s["text"]) <= 10 for s in wall["segments"])
        loose = segment(
            transcript_from(words),
            cfg=Config(max_chars_per_card=10, width_headroom=8,
                       over_target_cost=0.0),
        )
        assert max(len(s["text"]) for s in loose["segments"]) > 10

    def test_the_ceiling_still_bows_to_the_layout_rules(self):
        # A budget wider than max_lines * max_line_length cannot be laid out,
        # so headroom must not push it there.
        cfg = Config(max_chars_per_card=78, width_headroom=10, max_lines=2,
                     max_line_length=40)
        assert cfg.max_chars_per_card == 80

    def test_a_card_never_spans_two_sentences(self):
        words = ["אחת", "שתיים", "שלוש.", "ארבע", "חמש", "שש."]
        out = segment(transcript_from(words))
        for seg in out["segments"]:
            texts = [w["w"] for w in seg["words"]]
            # a sentence-final word may only ever be the LAST word of a card
            for i, w in enumerate(texts[:-1]):
                assert not w.endswith("."), (
                    f"card {seg['text']!r} continues past a sentence end"
                )

    def test_sentence_boundaries_are_always_card_boundaries(self):
        words = ["אחת", "שתיים.", "שלוש", "ארבע."]
        out = segment(transcript_from(words), cfg=Config(max_chars_per_card=100))
        assert cards_of(out) == ["אחת שתיים.", "שלוש ארבע."]

    def test_a_wider_budget_produces_fewer_cards(self):
        narrow = segment(transcript_from(SPEECH * 2), cfg=Config(max_chars_per_card=15))
        wide = segment(transcript_from(SPEECH * 2), cfg=Config(max_chars_per_card=40))
        assert len(wide["segments"]) < len(narrow["segments"])


# --------------------------------------------------------------------------
# Hebrew rules
# --------------------------------------------------------------------------


class TestHebrewRules:
    def test_a_card_does_not_end_on_a_function_word_when_avoidable(self):
        words = ["רציתי", "לדבר", "על", "הקמפיין", "החדש", "שלנו", "היום"]
        out = segment(transcript_from(words), cfg=Config(max_chars_per_card=18))
        for seg in out["segments"][:-1]:
            assert seg["words"][-1]["w"] not in ("על", "של", "עם", "את")

    def test_et_is_not_stranded_when_avoidable(self):
        words = ["הוא", "לקח", "את", "המצלמה", "החדשה", "שלו", "לעבודה"]
        out = segment(transcript_from(words), cfg=Config(max_chars_per_card=16))
        for seg in out["segments"][:-1]:
            assert seg["words"][-1]["w"] != "את"

    def test_construct_chain_is_kept_together(self):
        words = ["הלכנו", "לבית", "ספר", "גדול", "מאוד", "בעיר"]
        out = segment(transcript_from(words), cfg=Config(max_chars_per_card=14))
        for seg in out["segments"][:-1]:
            assert not (
                seg["words"][-1]["w"] == "בית"
            ), "split בית from ספר"

    def test_violation_detail_starts_with_a_known_rule_id(self):
        # Force violations by making the budget so tight that no legal split
        # exists, then assert the reporting contract rather than the count.
        out = segment(
            transcript_from(["נתתי", "את", "הספר", "של", "דנה", "לחבר"]),
            cfg=Config(max_chars_per_card=8),
        )
        for warn in warnings_with(out, "hebrew_rule_violation"):
            rule_id, _, prose = warn["detail"].partition(": ")
            assert rule_id in HEBREW_RULE_IDS
            assert prose.strip()

    def test_violation_carries_the_wid_span_of_the_break(self):
        out = segment(
            transcript_from(["נתתי", "את", "הספר", "של", "דנה", "לחבר"]),
            cfg=Config(max_chars_per_card=8),
        )
        for warn in warnings_with(out, "hebrew_rule_violation"):
            assert warn["wid_start"] <= warn["wid_end"]
            assert warn["stage"] == "segment"


# --------------------------------------------------------------------------
# quality targets -- warnings, never crashes
# --------------------------------------------------------------------------


class TestQualityTargets:
    def test_dense_speech_warns_about_cps(self):
        # 15 chars inside 0.3s is far over any sane reading speed
        out = segment(
            transcript_from(["אבגדהוזחט", "יכלמנסעפצ"], per_word=0.15),
            cfg=Config(max_chars_per_card=40, max_cps=17.0),
        )
        assert warnings_with(out, "cps_exceeded")

    def test_a_short_card_that_cannot_merge_warns_and_does_not_raise(self):
        out = segment(
            transcript_from(["אחת.", "שתיים."], per_word=0.2),
            cfg=Config(min_card_duration=5.0, max_chars_per_card=6,
                       width_headroom=0),
        )
        assert warnings_with(out, "card_too_short")

    def test_a_short_card_is_never_stretched(self):
        obj = transcript_from(["אחת.", "שתיים."], per_word=0.2)
        ends = {w["end"] for w in obj["segments"][0]["words"]}
        out = segment(obj, cfg=Config(min_card_duration=5.0))
        for seg in out["segments"]:
            assert seg["end"] in ends

    def test_upstream_warnings_are_preserved(self):
        obj = transcript_from(SPEECH)
        obj["meta"]["warnings"] = [{
            "stage": "transcribe",
            "code": "timing_clamped",
            "wid_start": 0,
            "wid_end": 0,
            "detail": "clamped",
        }]
        out = segment(obj)
        assert warnings_with(out, "timing_clamped")


# --------------------------------------------------------------------------
# stage bookkeeping
# --------------------------------------------------------------------------


class TestStages:
    def test_segment_is_recorded_in_stages(self):
        out = segment(transcript_from(SPEECH))
        assert "segment" in out["meta"]["stages"]

    def test_rerunning_raises_without_force(self):
        out = segment(transcript_from(SPEECH))
        with pytest.raises(StageAlreadyRun):
            segment(out)

    def test_force_does_not_duplicate_the_stage_name(self):
        out = segment(transcript_from(SPEECH))
        again = segment(out, force=True)
        assert again["meta"]["stages"].count("segment") == 1

    def test_empty_transcript_raises(self):
        obj = transcript_from(SPEECH)
        obj["segments"] = []
        with pytest.raises(SegmentError):
            segment(obj)

    def test_invalid_input_is_rejected_at_the_boundary(self):
        obj = transcript_from(SPEECH)
        obj["segments"][0]["text"] = "משהו אחר לגמרי"
        with pytest.raises(ContractError):
            segment(obj)
