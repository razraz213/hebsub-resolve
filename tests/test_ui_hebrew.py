"""Tests for the panel's Hebrew layer.

Rendering cannot be asserted in CI -- what a Tk label draws needs a screen and
an eye. What CAN be asserted is everything that would make the rendering wrong
before it ever reaches a widget: that the direction mark is the one the probe
proved works, that no translated line invents or loses a number, and that an
untranslated line still gets through.

The bidi findings behind this are recorded in ui/hebrew.py and as D69.
"""

from __future__ import annotations

import re

import pytest

from hebsub.ui.hebrew import PDF, RLE, T, rtl, translate_log


class TestRtl:
    def test_the_marks_are_the_ones_the_probe_proved(self):
        # RLM (U+200F) did nothing and RLI/PDI (U+2066/U+2069) rendered as
        # visible boxes. RLE/PDF is the pair that actually flips the base
        # direction in Tk 8.6 on Windows. Getting this constant wrong is
        # silent: the text still draws, just backwards.
        assert ord(RLE) == 0x202B
        assert ord(PDF) == 0x202C

    def test_it_wraps(self):
        assert rtl("שלום") == RLE + "שלום" + PDF

    def test_it_is_idempotent(self):
        once = rtl("שלום")
        assert rtl(once) == once

    def test_an_empty_line_stays_empty(self):
        # A blank log line must not become two invisible control characters.
        assert rtl("") == ""


class TestStrings:
    def test_no_ui_string_is_left_in_english(self):
        # A stray English caption is the whole point of this module failing.
        # Latin is allowed inside a string (product names), but a value that
        # is ONLY Latin letters means someone added a key and forgot it.
        # "none" is an em dash placeholder, not a word in any language.
        latin_only = {
            key: value for key, value in T.items()
            if key != "none"
            and re.fullmatch(r"[A-Za-z0-9 .,'\"()\u2014\u00b7:-]+", value)
        }
        assert latin_only == {}, f"untranslated: {sorted(latin_only)}"

    def test_panel_headings_carry_no_number(self):
        # The "01 ·" prefix is a separate widget, because a number beside
        # Hebrew is a weak-direction case RLE does not settle. A digit back
        # in these strings means it renders twice.
        for key in ("panel_connection", "panel_settings", "panel_log",
                    "panel_review"):
            assert not any(c.isdigit() for c in T[key]), key


class TestTranslateLog:
    @pytest.mark.parametrize("line,expected_number", [
        ("1504 words", "1504"),
        ("213 cards", "213"),
        ("4 correction(s)", "4"),
        ("corrected 6 word(s) automatically -- the other model heard a real "
         "Hebrew word where this one did not", "6"),
        ("12 word(s) worth a look (about half of these are genuinely wrong):",
         "12"),
        ("cleared 12 review marker(s)", "12"),
        ("placed 213 cards on a subtitle track", "213"),
        ("closed 41 sub-200ms gap(s) so those cards touch; real pauses left "
         "alone", "41"),
    ])
    def test_the_number_survives(self, line, expected_number):
        out = translate_log(line)
        assert out != line, f"not translated: {line!r}"
        assert expected_number in out

    @pytest.mark.parametrize("line,token", [
        ("wrote final.srt", "final.srt"),
        ("audio -> hebsub_audio.wav", "hebsub_audio.wav"),
        ("transcribing (ivrit_local)...", "ivrit_local"),
        ("second opinion (whisper-large-v3-ct2)...", "whisper-large-v3-ct2"),
        ("done -- artifacts in D:/work/subs", "D:/work/subs"),
    ])
    def test_the_filename_survives(self, line, token):
        out = translate_log(line)
        assert out != line, f"not translated: {line!r}"
        assert token in out

    def test_a_review_row_keeps_both_words_and_the_timecode(self):
        row = "1:23  תתעכם   (2nd model heard: תתעקם)"
        out = translate_log(row)
        assert "1:23" in out and "תתעכם" in out and "תתעקם" in out

    def test_an_unknown_line_is_passed_through_not_swallowed(self):
        # A traceback, or a message someone adds upstream tomorrow. English
        # in the log beats a line that silently disappears.
        odd = "Traceback (most recent call last):"
        assert translate_log(odd) == odd

    def test_a_blank_line_is_untouched(self):
        assert translate_log("") == ""
        assert translate_log("   ") == "   "

    def test_indentation_is_preserved(self):
        out = translate_log("  ...and 5 more, all of them in review.json")
        assert out.startswith("  ")
        assert "review.json" in out

    def test_no_pattern_is_shadowed_by_an_earlier_one(self):
        # "N correction(s)" would swallow the longer glossary variant if it
        # were listed first. Ordering in _LOG is load-bearing.
        with_glossary = translate_log(
            "4 correction(s) (no glossary.txt found next to the project)"
        )
        assert "glossary.txt" in with_glossary
