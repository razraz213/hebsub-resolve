"""Tests for the panel.

A tkinter window cannot be built in CI, but almost nothing worth testing here
needs one. The two things that do go wrong are testable as plain functions:
how the pipeline's stdout is turned into log lines, and whether the panel's
settings reach the pipeline unmangled.

The panel deliberately owns no pipeline logic. `test_the_panel_owns_no_pipeline`
is the guard on that, and it exists because the panel *did* own a second copy
once and the two drifted (D41, D42).
"""

from __future__ import annotations

import queue

import pytest

from hebsub.ui.app import Panel, _Tee


# --------------------------------------------------------------------------
# the log
# --------------------------------------------------------------------------


def drain(sink: queue.Queue) -> list[str]:
    out = []
    while not sink.empty():
        kind, payload = sink.get_nowait()
        assert kind == "log"
        out.append(payload)
    return out


class TestTee:
    def test_complete_lines_are_emitted(self):
        sink = queue.Queue()
        _Tee(sink).write("one\ntwo\n")
        assert drain(sink) == ["one", "two"]

    def test_a_partial_line_waits_for_its_newline(self):
        sink = queue.Queue()
        tee = _Tee(sink)
        tee.write("half")
        assert drain(sink) == []
        tee.write(" a line\n")
        assert drain(sink) == ["half a line"]

    def test_flush_emits_a_trailing_partial_line(self):
        sink = queue.Queue()
        tee = _Tee(sink)
        tee.write("no newline at the end")
        tee.flush()
        assert drain(sink) == ["no newline at the end"]

    def test_flush_is_idempotent(self):
        sink = queue.Queue()
        tee = _Tee(sink)
        tee.write("once")
        tee.flush()
        tee.flush()
        assert drain(sink) == ["once"]

    def test_the_module_prefix_is_stripped_and_the_line_translated(self):
        # The pipeline writes English for a terminal; the panel is neither a
        # terminal nor English. Both are presentation, so both happen here.
        from hebsub.ui.hebrew import translate_log

        sink = queue.Queue()
        _Tee(sink).write("host_resolve: 1504 words\n")
        assert drain(sink) == [translate_log("1504 words")]

    def test_the_number_survives_translation(self):
        # A translated log that invented a different number would be worse
        # than an English one.
        sink = queue.Queue()
        _Tee(sink).write("host_resolve: 1504 words\n")
        assert "1504" in drain(sink)[0]

    def test_indented_continuation_lines_are_dedented(self):
        sink = queue.Queue()
        _Tee(sink).write("  drag it onto a subtitle track\n")
        assert drain(sink) == ["drag it onto a subtitle track"]

    def test_an_unprefixed_line_is_left_alone(self):
        sink = queue.Queue()
        _Tee(sink).write("segment: 682 cards\n")
        assert drain(sink) == ["segment: 682 cards"]

    def test_write_returns_the_character_count(self):
        # It stands in for stdout, so it has to behave like a stream.
        assert _Tee(queue.Queue()).write("abc\n") == 4


# --------------------------------------------------------------------------
# settings -> pipeline
# --------------------------------------------------------------------------


@pytest.fixture
def spy(monkeypatch):
    """Capture what the panel asks the pipeline for."""
    calls = {}

    def fake_run(**kwargs):
        calls.update(kwargs)
        return {"srt": "final.srt", "status": "placed", "cards": 1,
                "words": 1, "placed": 1, "audio": "rendered", "edits": 0,
                "warnings": [], "review": []}

    import hebsub.host_resolve as host
    monkeypatch.setattr(host, "run", fake_run)
    return calls


def run_panel(settings):
    # _run_pipeline never touches `self`, so no window is needed to call it.
    return Panel._run_pipeline(None, settings)


class TestSettingsReachThePipeline:
    def test_max_chars_is_forwarded(self, spy):
        run_panel({"max_chars": 18, "passes": "glossary", "strip_punct": True})
        assert spy["max_chars"] == 18

    def test_passes_are_split_into_a_tuple(self, spy):
        run_panel({"max_chars": 14, "passes": "glossary,llm",
                   "strip_punct": True})
        assert spy["passes"] == ("glossary", "llm")

    def test_an_empty_passes_string_disables_proofreading(self, spy):
        # "" must not become ("",), which the proofread module would reject.
        run_panel({"max_chars": 14, "passes": "", "strip_punct": True})
        assert spy["passes"] == ()

    def test_strip_punct_is_forwarded_both_ways(self, spy):
        run_panel({"max_chars": 14, "passes": "glossary", "strip_punct": False})
        assert spy["strip_punct"] is False
        run_panel({"max_chars": 14, "passes": "glossary", "strip_punct": True})
        assert spy["strip_punct"] is True

    def test_the_review_checkbox_selects_the_measured_pairing(self, spy):
        # The partner is not a better model -- it is worse alone. It is the
        # one whose disagreements were measured at 48.6% precision (D48), so
        # the panel must not quietly substitute a different one.
        from hebsub.host_resolve import DEFAULT_REVIEW_MODEL

        run_panel({"max_chars": 14, "passes": "glossary",
                   "strip_punct": True, "review": True})
        assert spy["review_model"] == DEFAULT_REVIEW_MODEL

    def test_review_off_costs_no_second_pass(self, spy):
        run_panel({"max_chars": 14, "passes": "glossary",
                   "strip_punct": True, "review": False})
        assert spy["review_model"] is None

    def test_review_defaults_off_when_the_setting_is_absent(self, spy):
        run_panel({"max_chars": 14, "passes": "glossary", "strip_punct": True})
        assert spy["review_model"] is None

    def test_the_timing_card_is_on_by_default(self, spy):
        # It is the Resolve drag-in path; the card is what makes the drop
        # exact. Absent setting must not silently disable it.
        run_panel({"max_chars": 14, "passes": "glossary", "strip_punct": True})
        assert spy["timing_clip"] is True

    def test_the_timing_card_can_be_switched_off(self, spy):
        run_panel({"max_chars": 14, "passes": "glossary",
                   "strip_punct": True, "timing_clip": False})
        assert spy["timing_clip"] is False

    def test_the_result_is_returned_untouched(self, spy):
        result = run_panel({"max_chars": 14, "passes": "glossary",
                            "strip_punct": True})
        assert result["status"] == "placed"
        assert result["audio"] == "rendered"


class TestNoDuplicatePipeline:
    def test_the_panel_owns_no_pipeline(self):
        # The regression this guards: the panel used to assemble the stages
        # itself, so it proofread but wrote no intermediates while the CLI
        # wrote intermediates but never proofread. Neither was noticed until
        # a corpus could not be evaluated without re-transcribing it.
        import inspect

        from hebsub.ui import app

        source = inspect.getsource(app)
        for stage in ("from hebsub.transcribe import",
                      "from hebsub.segment import",
                      "from hebsub.export import",
                      "from hebsub.proofread import"):
            assert stage not in source, (
                f"the panel imports a pipeline stage ({stage!r}). It must call "
                f"host_resolve.run instead of assembling the stages itself."
            )


# --------------------------------------------------------------------------
# CLEAR FLAGS
# --------------------------------------------------------------------------


class TestClearFlags:
    """The panel's only non-run button.

    It exists because nothing tracks whether a flagged word has been fixed
    (D67), so the markers have to be dismissed deliberately. What matters here
    is that the panel delegates rather than reimplementing the tag filter --
    that is the mistake D41 is named after.
    """

    def test_the_panel_delegates_to_host_resolve(self):
        import inspect

        from hebsub.ui import app

        source = inspect.getsource(app.Panel.clear_flags)
        assert "clear_review_markers" in source
        assert "DeleteMarker" not in source, (
            "the panel is deleting markers itself. Only host_resolve knows "
            "which markers are ours, and it knows it by customData tag."
        )

    def test_it_reports_the_count_and_never_raises(self, monkeypatch):
        panel = Panel.__new__(Panel)
        said = []
        panel.say = lambda text, kind="": said.append((text, kind))

        class Status:
            def set(self, text):
                said.append((text, "status"))

        panel.var_status = Status()

        from hebsub import host_resolve

        timeline = object()
        project = type("P", (), {"GetCurrentTimeline": lambda self: timeline})()
        manager = type(
            "M", (), {"GetCurrentProject": lambda self: project}
        )()
        monkeypatch.setattr(
            host_resolve, "connect",
            lambda: type("R", (), {"GetProjectManager": lambda self: manager})(),
        )
        monkeypatch.setattr(host_resolve, "clear_review_markers", lambda t: 7)

        panel.clear_flags()
        assert any("7" in text for text, _ in said)

    def test_a_dead_resolve_is_reported_not_raised(self, monkeypatch):
        panel = Panel.__new__(Panel)
        said = []
        panel.say = lambda text, kind="": said.append((text, kind))
        panel.var_status = type("S", (), {"set": lambda self, t: None})()

        from hebsub import host_resolve

        def boom():
            raise host_resolve.ResolveError("host_resolve: Resolve is not reachable")

        monkeypatch.setattr(host_resolve, "connect", boom)
        panel.clear_flags()
        assert said and said[-1][1] == "bad"


# --------------------------------------------------------------------------
# the review list
# --------------------------------------------------------------------------


class TestReviewList:
    """Clicking a flagged word to move Resolve's playhead.

    The widget tree itself needs a real Tk window and is exercised by the
    smoke script rather than here (see docs/modules/ui.md). What is tested is
    the part that would be wrong silently: the timecode the row displays, and
    that the seek is delegated rather than reimplemented.
    """

    def test_the_row_stamp_matches_what_the_log_prints(self):
        # The panel and host_resolve show the same word at the same time. If
        # these two ever disagree the list stops being trustworthy.
        from hebsub.host_resolve import _clock

        for seconds in (0.0, 5.0, 83.4, 302.9, 1009.63):
            assert Panel.clock(seconds) == _clock(seconds)

    def test_goto_delegates_the_timecode_maths(self):
        import inspect

        from hebsub.ui import app

        source = inspect.getsource(app.Panel.goto)
        assert "host_resolve.seek" in source
        assert "SetCurrentTimecode" not in source, (
            "the panel is building a timecode itself. Only host_resolve knows "
            "that a Resolve timeline starts at 01:00:00:00."
        )

    def test_goto_moves_the_playhead_and_says_where(self, monkeypatch):
        panel = Panel.__new__(Panel)
        said = []
        panel.say = lambda text, kind="": said.append((text, kind))
        panel.var_status = type(
            "S", (), {"set": lambda self, t: said.append((t, "status"))}
        )()

        from hebsub import host_resolve

        moved = []
        monkeypatch.setattr(host_resolve, "connect", lambda: "resolve")
        monkeypatch.setattr(
            host_resolve, "_current", lambda r: ("project", "timeline")
        )
        monkeypatch.setattr(
            host_resolve, "seek",
            lambda p, t, s: moved.append(s) or "01:00:05:00",
        )

        panel.goto({"start": 5.0, "heard": "שלום"})
        assert moved == [5.0]
        assert any("01:00:05:00" in text for text, _ in said)

    def test_a_dead_resolve_is_reported_not_raised(self, monkeypatch):
        panel = Panel.__new__(Panel)
        said = []
        panel.say = lambda text, kind="": said.append((text, kind))
        panel.var_status = type("S", (), {"set": lambda self, t: None})()

        from hebsub import host_resolve

        def boom():
            raise host_resolve.ResolveError("host_resolve: not reachable")

        monkeypatch.setattr(host_resolve, "connect", boom)
        panel.goto({"start": 5.0, "heard": "x"})
        assert said and said[-1][1] == "bad"

