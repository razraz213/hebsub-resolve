"""Tests for the contract module.

Every assertion here is derived from docs/contracts.md, which is frozen.
If a test and the spec disagree, the spec wins and this file is wrong.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hebsub.contract import (
    WARNING_CODES,
    ContractError,
    validate_subtitle_file,
    validate_transcript,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _word(
    wid: int, w: str, start: float, end: float, conf: float | None = 0.9
) -> dict:
    return {"wid": wid, "w": w, "start": start, "end": end, "conf": conf}


_WORDS = [
    _word(0, "אז", 1.240, 1.390, 0.98),
    _word(1, "בוא", 1.390, 1.610, 0.94),
    _word(2, "נדבר", 1.610, 1.900, 0.91),
    _word(3, "על", 1.900, 2.050, 0.99),
    _word(4, "מה", 2.050, 2.200, 0.97),
    _word(5, "שקרה", 2.200, 2.600, 0.88),
    _word(6, "בקמפיין", 2.600, 3.400, 0.76),
    _word(7, "האחרון.", 3.400, 4.880, 0.93),
]


def transcript() -> dict:
    """A minimal Transcript that must always validate."""
    words = copy.deepcopy(_WORDS)
    return {
        "meta": {
            "source_file": "interview_dana.mp4",
            "duration": 612.4,
            "language": "he",
            "engine": "ivrit_local",
            "engine_version": "whisper-large-v3-turbo-ct2-20250513",
            "created_at": "2026-08-21T14:03:00Z",
            "stages": ["transcribe"],
            "warnings": [],
        },
        "segments": [
            {
                "id": 0,
                "start": 1.240,
                "end": 4.880,
                "text": " ".join(w["w"] for w in words),
                "words": words,
                "speaker": None,
            }
        ],
        "edits": [],
    }


def subtitle_file() -> dict:
    """A minimal SubtitleFile that must always validate."""
    obj = transcript()
    seg = obj["segments"][0]
    lines = [
        " ".join(w["w"] for w in seg["words"][:6]),
        " ".join(w["w"] for w in seg["words"][6:]),
    ]
    seg["lines"] = lines
    # The separator counts (D12) -- " ".join, matching the text invariant.
    seg["cps"] = round(len(" ".join(lines)) / (seg["end"] - seg["start"]), 3)
    obj["meta"]["stages"] = ["transcribe", "proofread", "segment"]
    return obj


def warning(**overrides) -> dict:
    base = {
        "stage": "segment",
        "code": "cps_exceeded",
        "detail": "22.4 CPS over limit 17",
    }
    base.update(overrides)
    return base


def edit(**overrides) -> dict:
    base = {
        "stage": "proofread",
        "wid": 5,
        "before": "צליל",
        "after": "צריך",
        "reason": "glossary",
    }
    base.update(overrides)
    return base


def second_segment(obj: dict, offset: float = 10.0) -> dict:
    """A copy of segment 0 shifted forward by `offset` seconds.

    wids continue from the highest one already in the object: they are global
    and strictly increasing, so a second segment may not restart them at 0.
    """
    seg = copy.deepcopy(obj["segments"][0])
    seg["id"] = obj["segments"][-1]["id"] + 1
    next_wid = max(w["wid"] for s in obj["segments"] for w in s["words"]) + 1
    for i, w in enumerate(seg["words"]):
        w["wid"] = next_wid + i
        w["start"] = round(w["start"] + offset, 3)
        w["end"] = round(w["end"] + offset, 3)
    seg["start"] = seg["words"][0]["start"]
    seg["end"] = seg["words"][-1]["end"]
    return seg


# --------------------------------------------------------------------------
# happy paths
# --------------------------------------------------------------------------


class TestValid:
    def test_minimal_transcript_validates(self):
        validate_transcript(transcript())

    def test_returns_none(self):
        assert validate_transcript(transcript()) is None

    def test_null_conf_is_allowed(self):
        obj = transcript()
        for w in obj["segments"][0]["words"]:
            w["conf"] = None
        validate_transcript(obj)

    def test_conf_bounds_are_inclusive(self):
        obj = transcript()
        obj["segments"][0]["words"][0]["conf"] = 0.0
        obj["segments"][0]["words"][1]["conf"] = 1.0
        validate_transcript(obj)

    def test_speaker_string_is_allowed(self):
        obj = transcript()
        obj["segments"][0]["speaker"] = "SPEAKER_01"
        validate_transcript(obj)

    def test_multiple_segments_validate(self):
        obj = transcript()
        obj["segments"].append(second_segment(obj))
        validate_transcript(obj)

    def test_populated_edits_log_validates(self):
        obj = transcript()
        obj["edits"] = [
            edit(),
            edit(reason="punctuation"),
            edit(reason="llm"),
        ]
        validate_transcript(obj)

    def test_empty_segments_list_is_allowed(self):
        # transcribe.md forbids *writing* an empty transcript; that is
        # transcribe's rule to enforce, not the contract's.
        obj = transcript()
        obj["segments"] = []
        validate_transcript(obj)

    def test_integer_timestamps_are_allowed(self):
        obj = transcript()
        obj["meta"]["duration"] = 613
        validate_transcript(obj)

    def test_minimal_subtitle_file_validates(self):
        validate_subtitle_file(subtitle_file())

    def test_single_line_card_validates(self):
        obj = subtitle_file()
        seg = obj["segments"][0]
        seg["lines"] = [seg["text"]]
        seg["cps"] = round(len(seg["text"]) / (seg["end"] - seg["start"]), 3)
        validate_subtitle_file(obj)


# --------------------------------------------------------------------------
# top level shape
# --------------------------------------------------------------------------


class TestTopLevel:
    @pytest.mark.parametrize("bad", [None, [], "x", 3, 3.0, True])
    def test_non_dict_rejected(self, bad):
        with pytest.raises(ContractError):
            validate_transcript(bad)

    @pytest.mark.parametrize("key", ["meta", "segments", "edits"])
    def test_missing_top_level_key_rejected(self, key):
        obj = transcript()
        del obj[key]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    def test_unknown_top_level_key_rejected(self):
        obj = transcript()
        obj["extra"] = 1
        with pytest.raises(ContractError, match="extra"):
            validate_transcript(obj)

    def test_segments_must_be_a_list(self):
        obj = transcript()
        obj["segments"] = {}
        with pytest.raises(ContractError, match="segments"):
            validate_transcript(obj)

    def test_edits_must_be_a_list(self):
        obj = transcript()
        obj["edits"] = {}
        with pytest.raises(ContractError, match="edits"):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


class TestMeta:
    @pytest.mark.parametrize(
        "key",
        [
            "source_file",
            "duration",
            "language",
            "engine",
            "engine_version",
            "created_at",
            "stages",
            "warnings",
        ],
    )
    def test_missing_meta_key_rejected(self, key):
        obj = transcript()
        del obj["meta"][key]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    def test_unknown_meta_key_rejected(self):
        obj = transcript()
        obj["meta"]["mood"] = "cheerful"
        with pytest.raises(ContractError, match="mood"):
            validate_transcript(obj)

    def test_meta_must_be_a_dict(self):
        obj = transcript()
        obj["meta"] = []
        with pytest.raises(ContractError, match="meta"):
            validate_transcript(obj)

    @pytest.mark.parametrize("bad", [0, -1, "x", None])
    def test_duration_must_be_positive_number(self, bad):
        obj = transcript()
        obj["meta"]["duration"] = bad
        with pytest.raises(ContractError, match="duration"):
            validate_transcript(obj)

    @pytest.mark.parametrize(
        "key", ["source_file", "language", "engine", "engine_version"]
    )
    def test_empty_meta_string_rejected(self, key):
        obj = transcript()
        obj["meta"][key] = ""
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    @pytest.mark.parametrize(
        "bad", ["", "not-a-date", "2026-13-01T00:00:00Z", 20260821]
    )
    def test_created_at_must_be_iso8601(self, bad):
        obj = transcript()
        obj["meta"]["created_at"] = bad
        with pytest.raises(ContractError, match="created_at"):
            validate_transcript(obj)

    @pytest.mark.parametrize(
        "bad", ["transcribe", [], [1], ["transcribe", ""], None]
    )
    def test_stages_must_be_non_empty_list_of_names(self, bad):
        obj = transcript()
        obj["meta"]["stages"] = bad
        with pytest.raises(ContractError, match="stages"):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# segments
# --------------------------------------------------------------------------


class TestSegments:
    @pytest.mark.parametrize(
        "key", ["id", "start", "end", "text", "words", "speaker"]
    )
    def test_missing_segment_key_rejected(self, key):
        obj = transcript()
        del obj["segments"][0][key]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    def test_unknown_segment_key_rejected(self):
        obj = transcript()
        obj["segments"][0]["confidence"] = 0.5
        with pytest.raises(ContractError, match="confidence"):
            validate_transcript(obj)

    def test_subtitle_fields_rejected_by_transcript_validator(self):
        obj = subtitle_file()
        with pytest.raises(ContractError, match="lines"):
            validate_transcript(obj)

    @pytest.mark.parametrize("bad", ["0", 0.0, None, True])
    def test_id_must_be_an_int(self, bad):
        obj = transcript()
        obj["segments"][0]["id"] = bad
        with pytest.raises(ContractError, match="id"):
            validate_transcript(obj)

    def test_negative_id_rejected(self):
        obj = transcript()
        obj["segments"][0]["id"] = -1
        with pytest.raises(ContractError, match="id"):
            validate_transcript(obj)

    def test_duplicate_ids_rejected(self):
        obj = transcript()
        dup = second_segment(obj)
        dup["id"] = 0
        obj["segments"].append(dup)
        with pytest.raises(ContractError, match="id"):
            validate_transcript(obj)

    def test_end_must_exceed_start(self):
        obj = transcript()
        obj["segments"][0]["end"] = obj["segments"][0]["start"]
        with pytest.raises(ContractError, match="end"):
            validate_transcript(obj)

    def test_more_than_three_decimals_rejected(self):
        obj = transcript()
        obj["segments"][0]["words"][0]["start"] = 1.2405
        with pytest.raises(ContractError, match="3 decimal"):
            validate_transcript(obj)

    def test_negative_timestamp_rejected(self):
        obj = transcript()
        obj["segments"][0]["start"] = -1.0
        obj["segments"][0]["words"][0]["start"] = -1.0
        with pytest.raises(ContractError, match="start"):
            validate_transcript(obj)

    @pytest.mark.parametrize("bad", [1, 1.5, [], {}])
    def test_speaker_must_be_null_or_string(self, bad):
        obj = transcript()
        obj["segments"][0]["speaker"] = bad
        with pytest.raises(ContractError, match="speaker"):
            validate_transcript(obj)

    def test_words_must_be_non_empty(self):
        obj = transcript()
        obj["segments"][0]["words"] = []
        with pytest.raises(ContractError, match="words"):
            validate_transcript(obj)

    def test_segment_start_must_contain_its_words(self):
        obj = transcript()
        obj["segments"][0]["start"] = 2.000
        with pytest.raises(ContractError, match="start"):
            validate_transcript(obj)

    def test_segment_end_must_contain_its_words(self):
        obj = transcript()
        obj["segments"][0]["end"] = 4.000
        with pytest.raises(ContractError, match="end"):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------


class TestWords:
    @pytest.mark.parametrize("key", ["w", "start", "end", "conf"])
    def test_missing_word_key_rejected(self, key):
        obj = transcript()
        del obj["segments"][0]["words"][0][key]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    def test_unknown_word_key_rejected(self):
        obj = transcript()
        obj["segments"][0]["words"][0]["prob"] = 0.5
        with pytest.raises(ContractError, match="prob"):
            validate_transcript(obj)

    def test_empty_word_rejected(self):
        obj = transcript()
        obj["segments"][0]["words"][0]["w"] = ""
        obj["segments"][0]["text"] = " ".join(
            w["w"] for w in obj["segments"][0]["words"]
        )
        with pytest.raises(ContractError, match="empty"):
            validate_transcript(obj)

    def test_word_containing_space_rejected(self):
        # A word with a space cannot survive the " ".join round trip.
        obj = transcript()
        words = obj["segments"][0]["words"]
        words[0]["w"] = "אז בוא"
        obj["segments"][0]["text"] = " ".join(w["w"] for w in words)
        with pytest.raises(ContractError, match="whitespace"):
            validate_transcript(obj)

    @pytest.mark.parametrize("bad", [-0.1, 1.1, "0.9", True])
    def test_conf_out_of_range_rejected(self, bad):
        obj = transcript()
        obj["segments"][0]["words"][0]["conf"] = bad
        with pytest.raises(ContractError, match="conf"):
            validate_transcript(obj)

    def test_word_end_must_exceed_start(self):
        obj = transcript()
        obj["segments"][0]["words"][0]["end"] = 1.240
        with pytest.raises(ContractError, match="end"):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# the text invariant
# --------------------------------------------------------------------------


class TestTextInvariant:
    def test_text_must_equal_joined_words(self):
        obj = transcript()
        obj["segments"][0]["text"] = "משהו אחר לגמרי"
        with pytest.raises(ContractError, match="text"):
            validate_transcript(obj)

    def test_dropped_word_detected(self):
        obj = transcript()
        obj["segments"][0]["words"].pop()
        with pytest.raises(ContractError, match="text"):
            validate_transcript(obj)

    def test_double_space_in_text_detected(self):
        obj = transcript()
        obj["segments"][0]["text"] = obj["segments"][0]["text"].replace(
            " ", "  ", 1
        )
        with pytest.raises(ContractError, match="text"):
            validate_transcript(obj)

    def test_text_must_be_a_string(self):
        obj = transcript()
        obj["segments"][0]["text"] = None
        with pytest.raises(ContractError, match="text"):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# monotonicity
# --------------------------------------------------------------------------


class TestMonotonic:
    def test_overlapping_words_rejected(self):
        obj = transcript()
        words = obj["segments"][0]["words"]
        words[1]["start"] = 1.300  # earlier than words[0]["end"] == 1.390
        with pytest.raises(ContractError, match="non-decreasing"):
            validate_transcript(obj)

    def test_words_out_of_order_rejected(self):
        obj = transcript()
        words = obj["segments"][0]["words"]
        words[0], words[1] = words[1], words[0]
        obj["segments"][0]["text"] = " ".join(w["w"] for w in words)
        with pytest.raises(ContractError, match="non-decreasing"):
            validate_transcript(obj)

    def test_overlapping_segments_rejected(self):
        obj = transcript()
        obj["segments"].append(second_segment(obj, offset=1.0))
        with pytest.raises(ContractError, match="non-decreasing"):
            validate_transcript(obj)

    def test_touching_segments_allowed(self):
        # end == next start is non-decreasing, therefore legal.
        obj = transcript()
        nxt = second_segment(obj, offset=3.640)
        obj["segments"].append(nxt)
        assert nxt["start"] == obj["segments"][0]["end"]
        validate_transcript(obj)


# --------------------------------------------------------------------------
# edits log
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# warnings (v2, D16/D17/D18/D25)
# --------------------------------------------------------------------------


class TestWarnings:
    def test_empty_warnings_list_validates(self):
        validate_transcript(transcript())

    def test_warnings_must_be_a_list(self):
        obj = transcript()
        obj["meta"]["warnings"] = {}
        with pytest.raises(ContractError, match="warnings"):
            validate_transcript(obj)

    def test_file_level_warning_without_wids_validates(self):
        obj = transcript()
        obj["meta"]["warnings"] = [warning()]
        validate_transcript(obj)

    def test_warning_with_wid_span_validates(self):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(wid_start=1, wid_end=4)]
        validate_transcript(obj)

    def test_single_word_span_may_repeat_the_wid(self):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(wid_start=3, wid_end=3)]
        validate_transcript(obj)

    @pytest.mark.parametrize("code", list(WARNING_CODES))
    def test_every_code_in_the_enum_is_accepted(self, code):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(code=code)]
        validate_transcript(obj)

    def test_the_enum_is_exactly_eight_codes(self):
        # D25 added hebrew_rule_violation; D17 deleted itn_skipped.
        assert len(WARNING_CODES) == 8
        assert "hebrew_rule_violation" in WARNING_CODES
        assert "itn_skipped" not in WARNING_CODES

    @pytest.mark.parametrize(
        "code", ["itn_skipped", "", None, "CPS_EXCEEDED", "made_up"]
    )
    def test_code_outside_the_enum_rejected(self, code):
        # An unrecognised code must not pass through: downstream there is no
        # column for it, so it would be silently dropped.
        obj = transcript()
        obj["meta"]["warnings"] = [warning(code=code)]
        with pytest.raises(ContractError, match="code"):
            validate_transcript(obj)

    @pytest.mark.parametrize("key", ["stage", "code", "detail"])
    def test_missing_required_warning_key_rejected(self, key):
        obj = transcript()
        w = warning()
        del w[key]
        obj["meta"]["warnings"] = [w]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    def test_unknown_warning_key_rejected(self):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(severity="high")]
        with pytest.raises(ContractError, match="severity"):
            validate_transcript(obj)

    @pytest.mark.parametrize("key", ["stage", "detail"])
    def test_empty_warning_string_rejected(self, key):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(**{key: "  "})]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    @pytest.mark.parametrize("key", ["wid_start", "wid_end"])
    def test_half_a_wid_span_rejected(self, key):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(**{key: 2})]
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    def test_reversed_wid_span_rejected(self):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(wid_start=5, wid_end=2)]
        with pytest.raises(ContractError, match="wid_start"):
            validate_transcript(obj)

    @pytest.mark.parametrize("bad", [-1, "3", 2.5, True, None])
    def test_non_integer_wid_span_rejected(self, bad):
        obj = transcript()
        obj["meta"]["warnings"] = [warning(wid_start=bad, wid_end=9)]
        with pytest.raises(ContractError, match="wid_start"):
            validate_transcript(obj)

    def test_warning_must_be_an_object(self):
        obj = transcript()
        obj["meta"]["warnings"] = ["cps_exceeded"]
        with pytest.raises(ContractError, match="warnings"):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# wid -- the durable identity (v2)
# --------------------------------------------------------------------------


class TestWid:
    def test_missing_wid_rejected(self):
        obj = transcript()
        del obj["segments"][0]["words"][0]["wid"]
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    @pytest.mark.parametrize("bad", [-1, "0", 1.5, True, None])
    def test_wid_must_be_a_non_negative_int(self, bad):
        obj = transcript()
        obj["segments"][0]["words"][0]["wid"] = bad
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    def test_duplicate_wid_within_a_segment_rejected(self):
        obj = transcript()
        obj["segments"][0]["words"][1]["wid"] = obj["segments"][0]["words"][0]["wid"]
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    def test_duplicate_wid_across_segments_rejected(self):
        obj = transcript()
        obj["segments"].append(second_segment(obj))
        obj["segments"][1]["words"][0]["wid"] = 0
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    def test_wid_going_backwards_rejected(self):
        # Words are never reordered after transcribe, so a wid that decreases
        # in document order means an upstream stage corrupted the audit trail.
        obj = transcript()
        words = obj["segments"][0]["words"]
        words[2]["wid"], words[3]["wid"] = words[3]["wid"], words[2]["wid"]
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    def test_wids_need_not_start_at_zero(self):
        # That is transcribe's contract with itself, not the validator's job;
        # asserting it here would reject a legitimately sliced artifact.
        obj = transcript()
        for w in obj["segments"][0]["words"]:
            w["wid"] += 500
        validate_transcript(obj)

    def test_gaps_in_wid_sequence_allowed(self):
        obj = transcript()
        for i, w in enumerate(obj["segments"][0]["words"]):
            w["wid"] = i * 10
        validate_transcript(obj)

    def test_wid_survives_regrouping_into_a_subtitle_file(self):
        obj = subtitle_file()
        flat = [w["wid"] for s in obj["segments"] for w in s["words"]]
        assert flat == sorted(set(flat))
        validate_subtitle_file(obj)


class TestEdits:
    @pytest.mark.parametrize(
        "key", ["stage", "wid", "before", "after", "reason"]
    )
    def test_missing_edit_key_rejected(self, key):
        obj = transcript()
        e = edit()
        del e[key]
        obj["edits"] = [e]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)

    def test_unknown_edit_key_rejected(self):
        obj = transcript()
        obj["edits"] = [edit(confidence=0.4)]
        with pytest.raises(ContractError, match="confidence"):
            validate_transcript(obj)

    @pytest.mark.parametrize("reason", ["glossary", "punctuation", "llm"])
    def test_all_three_reasons_accepted(self, reason):
        obj = transcript()
        obj["edits"] = [edit(reason=reason)]
        validate_transcript(obj)

    @pytest.mark.parametrize(
        "reason", ["itn_skipped", "llm_rejected", "", "LLM", None]
    )
    def test_reason_outside_the_frozen_enum_rejected(self, reason):
        # proofread.md asks for itn_skipped; contracts.md does not allow it.
        obj = transcript()
        obj["edits"] = [edit(reason=reason)]
        with pytest.raises(ContractError, match="reason"):
            validate_transcript(obj)

    def test_edit_must_be_a_dict(self):
        obj = transcript()
        obj["edits"] = ["glossary"]
        with pytest.raises(ContractError, match="edits"):
            validate_transcript(obj)

    def test_edit_wid_must_be_a_non_negative_int(self):
        obj = transcript()
        obj["edits"] = [edit(wid=-1)]
        with pytest.raises(ContractError, match="wid"):
            validate_transcript(obj)

    @pytest.mark.parametrize("key", ["stage", "before", "after"])
    def test_edit_strings_must_be_strings(self, key):
        obj = transcript()
        obj["edits"] = [edit(**{key: 7})]
        with pytest.raises(ContractError, match=key):
            validate_transcript(obj)


# --------------------------------------------------------------------------
# subtitle file
# --------------------------------------------------------------------------


class TestSubtitleFile:
    def test_transcript_without_lines_rejected(self):
        with pytest.raises(ContractError, match="lines"):
            validate_subtitle_file(transcript())

    def test_three_lines_rejected(self):
        obj = subtitle_file()
        obj["segments"][0]["lines"] = ["א", "ב", "ג"]
        with pytest.raises(ContractError, match="lines"):
            validate_subtitle_file(obj)

    def test_zero_lines_rejected(self):
        obj = subtitle_file()
        obj["segments"][0]["lines"] = []
        with pytest.raises(ContractError, match="lines"):
            validate_subtitle_file(obj)

    def test_empty_line_rejected(self):
        obj = subtitle_file()
        obj["segments"][0]["lines"] = ["שורה", ""]
        with pytest.raises(ContractError, match="lines"):
            validate_subtitle_file(obj)

    def test_lines_must_reproduce_the_text(self):
        obj = subtitle_file()
        obj["segments"][0]["lines"] = ["שורה ראשונה", "שורה שנייה"]
        with pytest.raises(ContractError, match="lines"):
            validate_subtitle_file(obj)

    def test_cps_must_match_the_formula(self):
        obj = subtitle_file()
        obj["segments"][0]["cps"] = 99.0
        with pytest.raises(ContractError, match="cps"):
            validate_subtitle_file(obj)

    def test_cps_rounded_to_one_decimal_accepted(self):
        obj = subtitle_file()
        seg = obj["segments"][0]
        seg["cps"] = round(
            len(" ".join(seg["lines"])) / (seg["end"] - seg["start"]), 1
        )
        validate_subtitle_file(obj)

    @pytest.mark.parametrize("bad", ["14.2", None, True])
    def test_cps_must_be_a_number(self, bad):
        obj = subtitle_file()
        obj["segments"][0]["cps"] = bad
        with pytest.raises(ContractError, match="cps"):
            validate_subtitle_file(obj)

    def test_subtitle_file_still_enforces_transcript_rules(self):
        obj = subtitle_file()
        obj["segments"][0]["words"][0]["conf"] = 5.0
        with pytest.raises(ContractError, match="conf"):
            validate_subtitle_file(obj)


# --------------------------------------------------------------------------
# error messages must locate the problem
# --------------------------------------------------------------------------


class TestErrorMessages:
    def test_message_names_the_module(self):
        obj = transcript()
        del obj["meta"]
        with pytest.raises(ContractError) as exc:
            validate_transcript(obj)
        assert "contract" in str(exc.value)

    def test_message_gives_the_path_to_the_bad_field(self):
        obj = transcript()
        obj["segments"][0]["words"][1]["conf"] = 3.0
        with pytest.raises(ContractError) as exc:
            validate_transcript(obj)
        assert "segments[0].words[1].conf" in str(exc.value)

    def test_message_gives_the_path_for_a_bad_segment(self):
        obj = transcript()
        nxt = second_segment(obj)
        nxt["text"] = "לא נכון"
        obj["segments"].append(nxt)
        with pytest.raises(ContractError) as exc:
            validate_transcript(obj)
        assert "segments[1].text" in str(exc.value)


# --------------------------------------------------------------------------
# fixtures on disk + CLI
# --------------------------------------------------------------------------


class TestFixtures:
    def test_transcript_fixture_validates(self):
        obj = json.loads(
            (FIXTURES / "minimal_transcript.json").read_text(encoding="utf-8")
        )
        validate_transcript(obj)

    def test_subtitle_fixture_validates(self):
        obj = json.loads(
            (FIXTURES / "minimal_subtitle.json").read_text(encoding="utf-8")
        )
        validate_subtitle_file(obj)

    def test_broken_fixture_is_rejected(self):
        obj = json.loads(
            (FIXTURES / "broken_transcript.json").read_text(encoding="utf-8")
        )
        with pytest.raises(ContractError):
            validate_transcript(obj)


def _run_cli(*args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "hebsub.contract", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


class TestCLI:
    def test_cli_accepts_a_valid_transcript(self):
        r = _run_cli("--in", str(FIXTURES / "minimal_transcript.json"))
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout

    def test_cli_accepts_a_valid_subtitle_file(self):
        r = _run_cli(
            "--in", str(FIXTURES / "minimal_subtitle.json"), "--kind", "subtitle"
        )
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout

    def test_cli_rejects_a_broken_transcript(self):
        r = _run_cli("--in", str(FIXTURES / "broken_transcript.json"))
        assert r.returncode != 0
        assert "contract" in (r.stdout + r.stderr)

    def test_cli_fails_loudly_on_a_missing_file(self):
        r = _run_cli("--in", str(FIXTURES / "does_not_exist.json"))
        assert r.returncode != 0

    def test_cli_fails_loudly_on_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        r = _run_cli("--in", str(bad))
        assert r.returncode != 0
