"""Tests for the export module.

Assertions come from docs/modules/export.md. The SRT is parsed back with
`pysrt` -- a test-only dependency (D5) that exists precisely so the writer is
not validated by its own reader.
"""

from __future__ import annotations

import copy
import json

import pysrt
import pytest

from hebsub.export import (
    DEFAULT_CLOSE_GAPS_MS,
    TIMING_CLIP_TEXT,
    ExportError,
    build_report,
    export,
    render_plain_srt,
    render_srt,
    render_vtt,
)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def card(wid0: int, lines: list[str], start: float, end: float) -> dict:
    words = []
    text = " ".join(lines)
    pieces = text.split()
    step = (end - start) / max(len(pieces), 1)
    for i, piece in enumerate(pieces):
        words.append({
            "wid": wid0 + i,
            "w": piece,
            "start": round(start + i * step, 3),
            "end": round(start + (i + 1) * step, 3),
            "conf": 0.9,
        })
    words[-1]["end"] = end
    return {
        "id": 0,
        "start": start,
        "end": end,
        "text": text,
        "words": words,
        "speaker": None,
        "lines": lines,
        "cps": round(len(" ".join(lines)) / (end - start), 3),
    }


def subtitle_file(cards: list[dict]) -> dict:
    for i, c in enumerate(cards):
        c["id"] = i
    return {
        "meta": {
            "source_file": "synthetic.wav",
            "duration": max(c["end"] for c in cards),
            "language": "he",
            "engine": "synthetic",
            "engine_version": "test",
            "created_at": "2026-08-25T10:00:00Z",
            "stages": ["transcribe", "segment"],
            "warnings": [],
        },
        "segments": cards,
        "edits": [],
    }


def simple() -> dict:
    return subtitle_file([
        card(0, ["שלום עולם"], 1.0, 3.0),
        card(10, ["בדיקה שנייה"], 3.0, 5.5),
        card(20, ["שורה ראשונה", "שורה שנייה"], 6.0, 9.0),
    ])


# --------------------------------------------------------------------------
# format
# --------------------------------------------------------------------------


class TestSrtFormat:
    def test_parses_with_pysrt(self, tmp_path):
        dst = tmp_path / "out.srt"
        export(simple(), dst)
        parsed = pysrt.open(str(dst), encoding="utf-8")
        assert len(parsed) == 3

    def test_timestamps_round_trip_to_the_millisecond(self, tmp_path):
        obj = simple()
        dst = tmp_path / "out.srt"
        export(obj, dst)
        parsed = pysrt.open(str(dst), encoding="utf-8")
        for item, seg in zip(parsed, obj["segments"]):
            assert item.start.ordinal == round(seg["start"] * 1000)
            assert item.end.ordinal == round(seg["end"] * 1000)

    def test_indices_are_one_based_and_sequential(self, tmp_path):
        dst = tmp_path / "out.srt"
        export(simple(), dst)
        parsed = pysrt.open(str(dst), encoding="utf-8")
        assert [i.index for i in parsed] == [1, 2, 3]

    def test_two_line_card_keeps_both_lines(self, tmp_path):
        dst = tmp_path / "out.srt"
        export(simple(), dst)
        parsed = pysrt.open(str(dst), encoding="utf-8")
        assert parsed[2].text.split("\n") == ["שורה ראשונה", "שורה שנייה"]

    def test_exact_bytes(self):
        obj = subtitle_file([card(0, ["שלום"], 1.0, 2.0)])
        text, _ = render_srt(obj)
        assert text == "1\n00:00:01,000 --> 00:00:02,000\nשלום\n\n"

    def test_blank_line_after_the_last_card(self):
        text, _ = render_srt(simple())
        assert text.endswith("\n\n")

    def test_no_bom_by_default(self, tmp_path):
        dst = tmp_path / "out.srt"
        export(simple(), dst)
        assert dst.read_bytes()[:3] != b"\xef\xbb\xbf"

    def test_bom_flag_adds_one(self, tmp_path):
        dst = tmp_path / "out.srt"
        export(simple(), dst, bom=True)
        assert dst.read_bytes()[:3] == b"\xef\xbb\xbf"

    def test_line_endings_are_lf_not_crlf(self, tmp_path):
        dst = tmp_path / "out.srt"
        export(simple(), dst)
        assert b"\r" not in dst.read_bytes()

    def test_unknown_format_raises(self, tmp_path):
        with pytest.raises(ExportError):
            export(simple(), tmp_path / "out.xyz", fmt="xyz")


class TestVtt:
    def test_header_and_dot_separator(self):
        text, _ = render_vtt(simple())
        assert text.startswith("WEBVTT\n\n")
        assert "00:00:01.000 --> 00:00:03.000" in text


# --------------------------------------------------------------------------
# gap
# --------------------------------------------------------------------------


class TestGap:
    def test_default_is_off(self):
        text, warnings = render_srt(simple())
        assert "00:00:03,000" in text
        assert warnings == []

    def test_gap_shortens_the_displayed_end_only(self):
        obj = simple()
        text, _ = render_srt(obj, gap_ms=83)
        # card 1 ends at 3.0 and card 2 starts at 3.0 -> shave to 2.917
        assert "00:00:02,917" in text
        # starts are never moved
        assert "00:00:03,000 --> " in text

    def test_gap_does_not_mutate_the_input(self):
        obj = simple()
        before = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        render_srt(obj, gap_ms=83)
        assert json.dumps(obj, ensure_ascii=False, sort_keys=True) == before

    def test_last_card_is_never_shaved(self):
        obj = simple()
        text, _ = render_srt(obj, gap_ms=83)
        assert "00:00:09,000" in text

    def test_card_with_an_existing_gap_is_untouched(self):
        obj = simple()
        text, _ = render_srt(obj, gap_ms=83)
        # card 2 ends 5.5, card 3 starts 6.0 -- already gapped
        assert "00:00:05,500" in text

    def test_unhonourable_gap_warns_once_with_a_wid_span(self):
        obj = subtitle_file([
            card(0, ["א"], 1.0, 1.02),
            card(5, ["ב"], 1.02, 3.0),
        ])
        _, warnings = render_srt(obj, gap_ms=500)
        gap_warnings = [w for w in warnings if w["code"] == "gap_not_applied"]
        assert len(gap_warnings) == 1
        assert gap_warnings[0]["wid_start"] <= gap_warnings[0]["wid_end"]

    def test_duration_never_goes_non_positive(self):
        obj = subtitle_file([
            card(0, ["א"], 1.0, 1.02),
            card(5, ["ב"], 1.02, 3.0),
        ])
        text, _ = render_srt(obj, gap_ms=5000)
        parsed = pysrt.from_string(text)
        for item in parsed:
            assert item.end.ordinal > item.start.ordinal


# --------------------------------------------------------------------------
# bidi opt-ins
# --------------------------------------------------------------------------


class TestBidiFlags:
    def test_no_control_characters_by_default(self):
        text, _ = render_srt(simple())
        for ch in ("‏", "⁦", "⁩"):
            assert ch not in text

    def test_rlm_exact_bytes(self):
        obj = subtitle_file([card(0, ["שלום."], 1.0, 2.0)])
        text, _ = render_srt(obj, rlm=True)
        assert "‏שלום‏." in text

    def test_isolate_wraps_latin_runs(self):
        obj = subtitle_file([card(0, ["אני עורך ב Premiere"], 1.0, 3.0)])
        text, _ = render_srt(obj, isolate=True)
        assert "⁦Premiere⁩" in text

    def test_isolate_leaves_pure_hebrew_alone(self):
        obj = subtitle_file([card(0, ["שלום עולם"], 1.0, 2.0)])
        text, _ = render_srt(obj, isolate=True)
        assert "⁦" not in text


# --------------------------------------------------------------------------
# report sidecar
# --------------------------------------------------------------------------


class TestReport:
    def test_written_on_every_run_even_when_clean(self, tmp_path):
        dst = tmp_path / "final.srt"
        export(simple(), dst)
        report_path = tmp_path / "final.srt.report.json"
        assert report_path.exists()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["warnings"] == []
        assert report["stats"]["cards"] == 3

    def test_stats_are_hand_countable(self):
        obj = subtitle_file([
            card(0, ["אבגדהוזחטי"], 1.0, 1.4),     # 10 chars / 0.4s = 25 cps
            card(20, ["שלום"], 2.0, 4.0),           # 4 / 2.0 = 2 cps
            # short (0.2s < 0.4s) but deliberately only 1 char, so it counts
            # as under-duration WITHOUT also tripping the CPS threshold
            card(30, ["א"], 5.0, 5.2),
        ])
        report = build_report(obj, "x.json", [], max_cps=17.0,
                              min_card_duration=0.4)
        stats = report["stats"]
        assert stats["cards"] == 3
        assert stats["cards_over_cps"] == 1
        assert stats["cards_over_line_len"] == 0
        assert stats["cards_under_min_duration"] == 1
        assert stats["max_cps"] == pytest.approx(25.0, abs=0.01)

    def test_report_is_valid_utf8_json(self, tmp_path):
        dst = tmp_path / "final.srt"
        export(simple(), dst, source="03_segmented.json")
        raw = (tmp_path / "final.srt.report.json").read_text(encoding="utf-8")
        assert json.loads(raw)["source"] == "03_segmented.json"

    def test_every_report_warning_satisfies_the_contract_shape(self, tmp_path):
        obj = subtitle_file([
            card(0, ["א"], 1.0, 1.02),
            card(5, ["ב"], 1.02, 3.0),
        ])
        report = export(obj, tmp_path / "final.srt", gap_ms=500)
        for warn in report["warnings"]:
            assert warn["stage"] and warn["code"] and warn["detail"]
            assert ("wid_start" in warn) == ("wid_end" in warn)
            if "wid_start" in warn:
                assert warn["wid_start"] <= warn["wid_end"]

    def test_export_appends_no_stage(self, tmp_path):
        obj = simple()
        export(obj, tmp_path / "final.srt")
        assert "export" not in obj["meta"]["stages"]


# --------------------------------------------------------------------------
# display punctuation (D35)
# --------------------------------------------------------------------------


class TestStripPunct:
    def test_off_by_default(self):
        obj = subtitle_file([card(0, ["שעושים בזכותו."], 1.0, 3.0)])
        text, _ = render_srt(obj)
        assert "בזכותו." in text

    def test_drops_trailing_sentence_punctuation(self):
        obj = subtitle_file([card(0, ["שעושים בזכותו."], 1.0, 3.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "שעושים בזכותו" in text
        assert "בזכותו." not in text

    def test_drops_commas_and_question_marks(self):
        obj = subtitle_file([card(0, ["למשל, מה?"], 1.0, 3.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "למשל מה" in text

    @pytest.mark.parametrize("word", ['חב"ד', 'חז"ל', 'נתב"ג', "קכא'"])
    def test_keeps_gershayim_and_geresh(self, word):
        # these are part of the word, not sentence punctuation -- stripping
        # them would corrupt the text
        obj = subtitle_file([card(0, [f"אמרו {word}"], 1.0, 3.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert word in text

    def test_does_not_mutate_the_subtitle_file(self):
        obj = subtitle_file([card(0, ["שעושים בזכותו."], 1.0, 3.0)])
        before = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        render_srt(obj, strip_punct=True)
        assert json.dumps(obj, ensure_ascii=False, sort_keys=True) == before

    def test_a_word_that_is_only_punctuation_survives(self):
        obj = subtitle_file([card(0, ["שלום ..."], 1.0, 3.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "שלום" in text

    def test_report_stats_are_unaffected(self, tmp_path):
        # stripping is display-only; cps was computed upstream on real text
        obj = simple()
        plain = export(obj, tmp_path / "a.srt")
        stripped = export(obj, tmp_path / "b.srt", strip_punct=True)
        assert plain["stats"] == stripped["stats"]


# --------------------------------------------------------------------------
# the timing clip -- a placement aid, not subtitle content
# --------------------------------------------------------------------------


def one_card_at(start: float, end: float) -> dict:
    return subtitle_file([card(0, ["שלום"], start, end)])


class TestTimingClip:
    """A leading card from 00:00:00,000 to the first real card.

    Resolve exposes no scripted way to position a subtitle clip (D28), so the
    .srt is dragged onto the track by hand. What you get to align is the
    clip's *content* -- and on Raz's workshop timeline the first spoken word
    is 2m44s in, so there is nothing at the front to align against. A card
    that starts at zero makes the clip begin at the start of the programme, so
    snapping it to the timeline start is exact. He deletes the card after.
    """

    def test_it_is_off_by_default(self):
        text, _ = render_srt(one_card_at(3.0, 3.6))
        assert TIMING_CLIP_TEXT not in text

    def test_it_starts_at_absolute_zero(self):
        text, _ = render_srt(one_card_at(3.0, 3.6), timing_clip=True)
        assert text.startswith("1\n00:00:00,000 --> 00:00:03,000\n")

    def test_it_ends_exactly_where_the_first_card_begins(self):
        text, _ = render_srt(one_card_at(12.345, 13.0), timing_clip=True)
        assert "00:00:00,000 --> 00:00:12,345" in text

    def test_it_carries_an_unmistakable_ascii_label(self):
        text, _ = render_srt(one_card_at(3.0, 3.6), timing_clip=True)
        assert TIMING_CLIP_TEXT in text
        # It sits in an RTL subtitle track: Hebrew could be mistaken for
        # content, and mixed scripts invite bidi reordering.
        assert TIMING_CLIP_TEXT.isascii()

    def test_the_real_cards_keep_their_timings(self):
        obj = simple()
        plain, _ = render_srt(obj)
        with_clip, _ = render_srt(obj, timing_clip=True)
        for seg in obj["segments"]:
            stamp = f"{int(seg['start']) // 60:02d}"
            assert stamp in plain and stamp in with_clip
        assert plain.count("-->") + 1 == with_clip.count("-->")

    def test_indices_stay_sequential_and_one_based(self):
        text, _ = render_srt(simple(), timing_clip=True)
        indices = [int(line) for line in text.splitlines() if line.isdigit()]
        assert indices == list(range(1, len(indices) + 1))

    def test_a_card_already_at_zero_gets_no_timing_clip(self):
        # No room for one, and a zero-length card is not a card.
        text, _ = render_srt(one_card_at(0.0, 0.6), timing_clip=True)
        assert TIMING_CLIP_TEXT not in text
        assert text.startswith("1\n00:00:00,000 --> 00:00:00,600\n")

    def test_it_never_overlaps_the_first_card(self):
        text, _ = render_srt(one_card_at(5.0, 5.6), timing_clip=True)
        cards = pysrt.from_string(text)
        assert cards[0].end.ordinal <= cards[1].start.ordinal

    def test_the_file_still_parses_as_valid_srt(self):
        text, _ = render_srt(simple(), timing_clip=True)
        cards = pysrt.from_string(text)
        assert len(cards) == len(simple()["segments"]) + 1
        assert cards[0].text == TIMING_CLIP_TEXT

    def test_export_records_it_in_the_report(self, tmp_path):
        obj = one_card_at(3.0, 3.6)
        assert export(obj, tmp_path / "a.srt",
                      timing_clip=True)["stats"]["timing_clip"] is True
        assert export(obj, tmp_path / "b.srt")["stats"]["timing_clip"] is False

    def test_the_report_still_counts_only_real_cards(self, tmp_path):
        obj = simple()
        report = export(obj, tmp_path / "a.srt", timing_clip=True)
        assert report["stats"]["cards"] == len(obj["segments"])

    def test_vtt_never_gets_one(self, tmp_path):
        # A .vtt goes to a web player, where nothing is dragged anywhere. A
        # "delete me" card there would simply be a bug.
        export(one_card_at(3.0, 3.6), tmp_path / "a.vtt", fmt="vtt",
               timing_clip=True)
        assert TIMING_CLIP_TEXT not in (tmp_path / "a.vtt").read_text(
            encoding="utf-8"
        )


# --------------------------------------------------------------------------
# closing dead space between cards
# --------------------------------------------------------------------------


def two_cards(gap_s: float) -> dict:
    """Two cards with exactly `gap_s` of dead space between them."""
    return subtitle_file([
        card(0, ["שלום"], 1.0, 2.0),
        card(10, ["עולם"], round(2.0 + gap_s, 3), round(2.0 + gap_s + 1.0, 3)),
    ])


class TestCloseGaps:
    """Extend a card's displayed end to meet the next one, when the gap is
    small enough to be a segmentation artifact rather than a pause.

    Measured over Raz's three corrected corpora: **96.4% of his 1,882 gaps are
    exactly zero**, and there is a complete void from 1ms to 60ms -- not one
    gap in that range. The segmenter meanwhile puts 32% of its gaps in the
    1-200ms band, which is the band he never produces.
    """

    def test_it_is_off_by_default(self):
        text, _ = render_srt(two_cards(0.1))
        assert "00:00:01,000 --> 00:00:02,000" in text

    def test_a_small_gap_is_closed_exactly_onto_the_next_start(self):
        text, _ = render_srt(two_cards(0.1), close_gaps_ms=200)
        assert "00:00:01,000 --> 00:00:02,100" in text
        assert "00:00:02,100 --> 00:00:03,100" in text

    def test_a_real_pause_is_left_alone(self):
        text, _ = render_srt(two_cards(0.8), close_gaps_ms=200)
        assert "00:00:01,000 --> 00:00:02,000" in text

    def test_the_threshold_is_inclusive(self):
        text, _ = render_srt(two_cards(0.200), close_gaps_ms=200)
        assert "00:00:01,000 --> 00:00:02,200" in text

    def test_one_millisecond_over_the_threshold_is_kept(self):
        text, _ = render_srt(two_cards(0.201), close_gaps_ms=200)
        assert "00:00:01,000 --> 00:00:02,000" in text

    def test_starts_are_never_moved(self):
        obj = two_cards(0.1)
        starts = [round(float(seg["start"]) * 1000) for seg in obj["segments"]]
        cards = pysrt.from_string(render_srt(obj, close_gaps_ms=200)[0])
        assert [c.start.ordinal for c in cards] == starts

    def test_the_subtitle_file_on_disk_is_untouched(self):
        obj = two_cards(0.1)
        before = copy.deepcopy(obj)
        render_srt(obj, close_gaps_ms=200)
        assert obj == before

    def test_the_last_card_is_never_extended(self):
        obj = two_cards(0.1)
        cards = pysrt.from_string(render_srt(obj, close_gaps_ms=200)[0])
        assert cards[-1].end.ordinal == round(
            float(obj["segments"][-1]["end"]) * 1000
        )

    def test_cards_never_overlap_after_closing(self):
        obj = subtitle_file([
            card(0, ["א"], 1.0, 2.0),
            card(10, ["ב"], 2.05, 3.0),
            card(20, ["ג"], 3.1, 4.0),
        ])
        cards = pysrt.from_string(render_srt(obj, close_gaps_ms=200)[0])
        for a, b in zip(cards, cards[1:]):
            assert a.end.ordinal <= b.start.ordinal

    def test_an_already_touching_pair_is_unchanged(self):
        text, _ = render_srt(two_cards(0.0), close_gaps_ms=200)
        assert "00:00:01,000 --> 00:00:02,000" in text

    def test_opening_and_closing_gaps_at_once_is_refused(self):
        # --gap opens a gap, --close-gaps closes one. Asking for both is a
        # contradiction, and silently picking a winner would be worse.
        with pytest.raises(ExportError):
            render_srt(two_cards(0.1), gap_ms=40, close_gaps_ms=200)

    def test_the_report_counts_what_was_closed(self, tmp_path):
        obj = subtitle_file([
            card(0, ["א"], 1.0, 2.0),
            card(10, ["ב"], 2.05, 3.0),
            card(20, ["ג"], 3.9, 4.5),
        ])
        report = export(obj, tmp_path / "a.srt", close_gaps_ms=200)
        assert report["stats"]["closed_gaps"] == 1
        assert export(obj, tmp_path / "b.srt")["stats"]["closed_gaps"] == 0

    def test_the_default_matches_what_was_measured(self):
        assert DEFAULT_CLOSE_GAPS_MS == 200

    def test_vtt_closes_gaps_too(self):
        # Unlike the timing clip, dead space between cards is wrong in any
        # player, not just Resolve.
        text, _ = render_vtt(two_cards(0.1), close_gaps_ms=200)
        assert "00:00:02.100" in text


# --------------------------------------------------------------------------
# a card must not appear before its picture
# --------------------------------------------------------------------------


class TestOnsetSpans:
    """Whisper's onset for the first word after silence takes the breath with
    it -- measured against Raz's corrected files at a median 34ms early over
    all cards, but 173ms and up to 460ms for the first card of a video. On a
    timeline of separate videos that puts the card in the black gap before the
    next one, where there is nothing behind it.
    """

    def test_it_is_off_by_default(self):
        obj = subtitle_file([card(0, ["שלום"], 5.0, 6.0)])
        text, _ = render_srt(obj)
        assert "00:00:05,000 -->" in text

    def test_a_card_in_a_hole_is_pulled_to_the_next_picture(self):
        obj = subtitle_file([card(0, ["שלום"], 4.9, 6.0)])
        text, _ = render_srt(obj, onset_spans=[(0.0, 3.0), (5.0, 9.0)])
        assert "00:00:05,000 --> 00:00:06,000" in text

    def test_a_card_already_over_a_picture_is_untouched(self):
        obj = subtitle_file([card(0, ["שלום"], 5.5, 6.0)])
        text, _ = render_srt(obj, onset_spans=[(5.0, 9.0)])
        assert "00:00:05,500 --> 00:00:06,000" in text

    def test_a_card_entirely_inside_a_hole_is_left_alone(self):
        # It belongs to no picture at all; pulling its start past its own end
        # would invert the card.
        obj = subtitle_file([card(0, ["שלום"], 3.2, 3.6)])
        text, _ = render_srt(obj, onset_spans=[(0.0, 3.0), (5.0, 9.0)])
        assert "00:00:03,200 --> 00:00:03,600" in text

    def test_a_card_after_the_last_picture_is_left_alone(self):
        obj = subtitle_file([card(0, ["שלום"], 12.0, 13.0)])
        text, _ = render_srt(obj, onset_spans=[(0.0, 9.0)])
        assert "00:00:12,000 --> 00:00:13,000" in text

    def test_ends_are_never_moved(self):
        obj = subtitle_file([card(0, ["שלום"], 4.9, 6.0)])
        cards = pysrt.from_string(
            render_srt(obj, onset_spans=[(5.0, 9.0)])[0]
        )
        assert cards[0].end.ordinal == 6000

    def test_the_subtitle_file_on_disk_is_untouched(self):
        obj = subtitle_file([card(0, ["שלום"], 4.9, 6.0)])
        before = copy.deepcopy(obj)
        render_srt(obj, onset_spans=[(5.0, 9.0)])
        assert obj == before

    def test_the_timing_clip_ends_at_the_moved_start(self):
        # Otherwise the placeholder and the first card disagree by exactly the
        # amount the clamp moved, and the drop is off by that much.
        obj = subtitle_file([card(0, ["שלום"], 4.9, 6.0)])
        text, _ = render_srt(obj, onset_spans=[(5.0, 9.0)], timing_clip=True)
        assert text.startswith("1\n00:00:00,000 --> 00:00:05,000\n")

    def test_cards_never_overlap_after_clamping(self):
        obj = subtitle_file([
            card(0, ["א"], 4.9, 5.4),
            card(10, ["ב"], 5.4, 6.0),
        ])
        cards = pysrt.from_string(
            render_srt(obj, onset_spans=[(5.0, 9.0)])[0]
        )
        for a, b in zip(cards, cards[1:]):
            assert a.end.ordinal <= b.start.ordinal


class TestQuestionMarks:
    """Counted across all three corrected corpora, 4,331 words: `?` 29 times,
    against `,` 3, `.` 2 and `!` 1. The question mark is the only sentence
    punctuation Raz actually uses, and a question that reads as a statement is
    a different sentence.
    """

    def test_a_question_mark_survives_stripping(self):
        obj = subtitle_file([card(0, ["מה אתם מעדיפים?"], 1.0, 2.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "מה אתם מעדיפים?" in text

    def test_full_stops_and_commas_still_go(self):
        obj = subtitle_file([card(0, ["שלום, עולם."], 1.0, 2.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "שלום עולם" in text
        assert "," not in text.split("\n")[2]

    def test_a_question_mark_mid_card_survives(self):
        obj = subtitle_file([card(0, ["באמת? כן"], 1.0, 2.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "באמת? כן" in text

    def test_the_geresh_is_still_never_stripped(self):
        obj = subtitle_file([card(0, ["הג'יפ שלי"], 1.0, 2.0)])
        text, _ = render_srt(obj, strip_punct=True)
        assert "הג'יפ" in text

    def test_nothing_is_stripped_when_not_asked(self):
        obj = subtitle_file([card(0, ["שלום, עולם."], 1.0, 2.0)])
        text, _ = render_srt(obj)
        assert "שלום, עולם." in text

    def test_a_closed_gap_meets_the_MOVED_start(self):
        # The order matters: if gaps are closed against the raw start, every
        # gap re-opens by however far the onset clamp moved the next card.
        obj = subtitle_file([
            card(0, ["א"], 1.0, 2.0),
            card(10, ["ב"], 2.05, 3.0),
        ])
        text, _ = render_srt(obj, close_gaps_ms=200,
                             onset_spans=[(0.0, 1.5), (2.2, 9.0)])
        cards = pysrt.from_string(text)
        assert cards[1].start.ordinal == 2200, "second card should move to 2.2s"
        assert cards[0].end.ordinal == 2200, "first card should follow it"


# --------------------------------------------------------------------------
# render_plain_srt -- the review track's side door
# --------------------------------------------------------------------------


def test_plain_srt_is_parseable_by_pysrt():
    import pysrt

    cards = [(0.5, 2.0, "one"), (2.2, 3.4, "two\nalt")]
    parsed = pysrt.from_string(render_plain_srt(cards, timing_clip=False))
    assert len(parsed) == 2
    assert parsed[0].start.ordinal == 500
    assert parsed[1].end.ordinal == 3400
    assert parsed[1].text == "two\nalt"


def test_plain_srt_numbers_from_one():
    text = render_plain_srt([(0.0, 1.0, "a"), (1.0, 2.0, "b")])
    assert text.split("\n")[0] == "1"
    assert "\n2\n" in text


def test_plain_srt_of_nothing_is_empty_not_a_stub():
    assert render_plain_srt([]) == ""


def test_plain_srt_gets_the_same_timing_card_as_the_real_track():
    # Resolve drops the lead-in silence on import. Without a card at absolute
    # zero the review track lands adrift of the subtitles it annotates -- and
    # since it is READ against them, adrift is worse than useless.
    import pysrt

    parsed = pysrt.from_string(render_plain_srt([(40.0, 41.0, "x")]))
    assert len(parsed) == 2
    assert parsed[0].start.ordinal == 0
    assert parsed[0].end.ordinal == 40000
    assert parsed[0].text == TIMING_CLIP_TEXT
    assert parsed[1].index == 2


def test_plain_srt_skips_the_timing_card_when_there_is_no_room():
    text = render_plain_srt([(0.0, 1.0, "x")])
    assert TIMING_CLIP_TEXT not in text
    assert text.split("\n")[0] == "1"
