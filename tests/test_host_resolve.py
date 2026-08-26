"""Tests for the Resolve host adapter.

Resolve cannot run in CI, so everything Resolve-shaped is faked. That is
enough for the part that actually goes wrong: reading the timeline's audio
structure correctly, and deciding whether an existing render can be reused.

The live findings this module depends on are recorded as D26-D29 and D42.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hebsub.host_resolve import (
    DEFAULT_CLIP_NAME,
    REVIEW_CLIP_NAME,
    REVIEW_MARKER_COLOR,
    REVIEW_MARKER_TAG,
    _report_warnings,
    audio_cut_list,
    clear_review_markers,
    harvest_corrections,
    subtitle_tracks,
    seek,
    timecode_at,
    next_clip_name,
    place_review_markers,
    review_cards,
    work_dir_name,
    timeline_fingerprint,
)


# --------------------------------------------------------------------------
# fakes -- just enough Resolve to exercise the getters we rely on
# --------------------------------------------------------------------------


class FakeMediaPoolItem:
    def __init__(self, path, fps=25.0):
        self._props = {"File Path": str(path), "FPS": fps}

    def GetClipProperty(self, key):
        return self._props.get(key)


class FakeItem:
    def __init__(self, start, duration, source_start=0, path=None,
                 source_fps=25.0, name="clip"):
        self._start = start
        self._duration = duration
        self._source_start = source_start
        self._name = name
        self._mp = FakeMediaPoolItem(path, source_fps) if path else None

    def GetStart(self):
        return self._start

    def GetEnd(self):
        return self._start + self._duration

    def GetDuration(self):
        return self._duration

    def GetSourceStartFrame(self):
        return self._source_start

    def GetMediaPoolItem(self):
        return self._mp

    def GetName(self):
        return self._name


class FakeTimeline:
    def __init__(self, tracks, origin=108000, end=None):
        self._tracks = tracks          # {index: (enabled, [FakeItem, ...])}
        self._origin = origin
        self._end = end if end is not None else origin + 3000

    def GetStartFrame(self):
        return self._origin

    def GetEndFrame(self):
        return self._end

    def GetName(self):
        return "Timeline 1"

    def GetTrackCount(self, kind):
        return max(self._tracks) if kind == "audio" and self._tracks else 0

    def GetIsTrackEnabled(self, kind, index):
        return self._tracks.get(index, (True, []))[0]

    def GetItemListInTrack(self, kind, index):
        if kind != "audio":
            return []
        return list(self._tracks.get(index, (True, []))[1])


class FakeProject:
    def __init__(self, fps=30.0):
        self._fps = fps

    def GetSetting(self, key):
        return self._fps if key == "timelineFrameRate" else ""


@pytest.fixture
def media(tmp_path):
    f = tmp_path / "take.mp4"
    f.write_bytes(b"not really media, but it has a size and an mtime")
    return f


def one_clip(media, **kw):
    opts = {"start": 108000, "duration": 90, "source_start": 0,
            "path": media, "source_fps": 25.0}
    opts.update(kw)
    return FakeTimeline({1: (True, [FakeItem(**opts)])})


# --------------------------------------------------------------------------
# reading the timeline
# --------------------------------------------------------------------------


class TestAudioCutList:
    def test_timeline_frames_use_the_timeline_rate(self, media):
        # 90 frames at 30 fps is 3 seconds, whatever the source rate is
        cut = audio_cut_list(FakeProject(30.0), one_clip(media))
        assert cut["items"][0]["duration_s"] == pytest.approx(3.0)

    def test_the_timeline_origin_is_subtracted(self, media):
        # Resolve timelines start at 01:00:00:00 -- frame 108000 at 30 fps.
        # A clip at the very start of the timeline is at 0.0s, not 3600s.
        cut = audio_cut_list(FakeProject(30.0), one_clip(media, start=108000))
        assert cut["items"][0]["timeline_start_s"] == pytest.approx(0.0)

        cut = audio_cut_list(FakeProject(30.0), one_clip(media, start=108150))
        assert cut["items"][0]["timeline_start_s"] == pytest.approx(5.0)

    def test_source_offset_uses_the_source_rate_not_the_timeline_rate(self, media):
        # The trap, and it is silent: GetSourceStartFrame counts SOURCE frames
        # while GetStart/GetDuration count TIMELINE frames. On a 25 fps clip in
        # a 30 fps timeline, dividing by the wrong one desyncs every cut.
        cut = audio_cut_list(
            FakeProject(30.0),
            one_clip(media, source_start=50, source_fps=25.0),
        )
        assert cut["items"][0]["source_in_s"] == pytest.approx(2.0)   # 50/25
        assert cut["items"][0]["source_in_s"] != pytest.approx(50 / 30)

    def test_a_transition_has_no_media_and_is_reported_not_raised(self):
        timeline = FakeTimeline(
            {1: (True, [FakeItem(108000, 12, path=None, name="Cross Fade 0 dB")])}
        )
        cut = audio_cut_list(FakeProject(30.0), timeline)
        assert cut["items"] == []
        assert len(cut["unresolved"]) == 1
        assert cut["unresolved"][0]["name"] == "Cross Fade 0 dB"

    def test_a_disabled_track_is_recorded_as_disabled(self, media):
        timeline = FakeTimeline({1: (False, [FakeItem(108000, 90, path=media)])})
        cut = audio_cut_list(FakeProject(30.0), timeline)
        assert cut["items"][0]["enabled"] is False

    def test_every_audio_track_is_read(self, media):
        timeline = FakeTimeline({
            1: (True, [FakeItem(108000, 90, path=media)]),
            2: (True, []),
            3: (True, [FakeItem(108030, 60, path=media)]),
        })
        cut = audio_cut_list(FakeProject(30.0), timeline)
        assert {i["track"] for i in cut["items"]} == {1, 3}

    def test_a_missing_source_file_does_not_raise(self, tmp_path):
        cut = audio_cut_list(FakeProject(30.0), one_clip(tmp_path / "gone.mp4"))
        assert cut["items"][0]["size"] is None


# --------------------------------------------------------------------------
# the fingerprint -- what decides whether a render can be skipped
# --------------------------------------------------------------------------


class TestFingerprint:
    def test_it_is_stable_across_repeated_reads(self, media):
        a = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        b = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        assert a == b

    def test_moving_a_clip_changes_it(self, media):
        a = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        b = timeline_fingerprint(
            audio_cut_list(FakeProject(), one_clip(media, start=108030))
        )
        assert a != b

    def test_trimming_a_clip_changes_it(self, media):
        a = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        b = timeline_fingerprint(
            audio_cut_list(FakeProject(), one_clip(media, duration=60))
        )
        assert a != b

    def test_sliding_the_source_in_point_changes_it(self, media):
        a = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        b = timeline_fingerprint(
            audio_cut_list(FakeProject(), one_clip(media, source_start=25))
        )
        assert a != b

    def test_swapping_the_source_file_changes_it(self, tmp_path, media):
        other = tmp_path / "other.mp4"
        other.write_bytes(b"different media entirely")
        a = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        b = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(other)))
        assert a != b

    def test_replacing_the_file_on_disk_changes_it(self, media):
        a = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        media.write_bytes(b"same path, completely different audio in it now")
        b = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        assert a != b, "a re-linked or re-exported source must invalidate the cache"

    def test_disabling_a_track_changes_it(self, media):
        on = FakeTimeline({1: (True, [FakeItem(108000, 90, path=media)])})
        off = FakeTimeline({1: (False, [FakeItem(108000, 90, path=media)])})
        assert (timeline_fingerprint(audio_cut_list(FakeProject(), on))
                != timeline_fingerprint(audio_cut_list(FakeProject(), off)))

    def test_changing_the_frame_rate_changes_it(self, media):
        a = timeline_fingerprint(audio_cut_list(FakeProject(30.0), one_clip(media)))
        b = timeline_fingerprint(audio_cut_list(FakeProject(25.0), one_clip(media)))
        assert a != b

    def test_renaming_a_clip_does_not_change_it(self, media):
        a = timeline_fingerprint(
            audio_cut_list(FakeProject(), one_clip(media, name="C0502.MP4"))
        )
        b = timeline_fingerprint(
            audio_cut_list(FakeProject(), one_clip(media, name="hero shot"))
        )
        assert a == b, "the fingerprint must track audio, not cosmetics"

    def test_a_transition_still_counts(self, media):
        # Transitions carry no media but they do change the mix, so a timeline
        # that gains one is not the same timeline.
        plain = FakeTimeline({1: (True, [FakeItem(108000, 90, path=media)])})
        faded = FakeTimeline({1: (True, [
            FakeItem(108000, 90, path=media),
            FakeItem(108090, 12, path=None, name="Cross Fade 0 dB"),
        ])})
        assert (timeline_fingerprint(audio_cut_list(FakeProject(), plain))
                != timeline_fingerprint(audio_cut_list(FakeProject(), faded)))

    def test_it_is_a_short_hex_string(self, media):
        fp = timeline_fingerprint(audio_cut_list(FakeProject(), one_clip(media)))
        assert isinstance(fp, str)
        assert len(fp) == 16
        int(fp, 16)


# --------------------------------------------------------------------------
# how warnings reach a human
# --------------------------------------------------------------------------


def warnings_of(*codes):
    return [{"code": c, "detail": f"detail for {c} #{i}"}
            for i, c in enumerate(codes)]


class TestWarningReport:
    def test_nothing_is_printed_when_there_are_no_warnings(self, capsys):
        _report_warnings([], Path("03_segmented.json"))
        assert capsys.readouterr().out == ""

    def test_the_summary_counts_every_code(self, capsys):
        _report_warnings(
            warnings_of("card_too_short", "card_too_short", "cps_exceeded"),
            Path("03_segmented.json"),
        )
        out = capsys.readouterr().out
        assert "card_too_short x2" in out
        assert "cps_exceeded x1" in out

    def test_the_full_list_is_never_dumped(self, capsys):
        # A 15-minute timeline produces ~75 warnings. On a six-line panel log,
        # printing them all is the same as printing nothing.
        many = warnings_of(*(["card_too_short"] * 60 + ["cps_exceeded"] * 15))
        _report_warnings(many, Path("03_segmented.json"))
        out = capsys.readouterr().out
        assert len(out.splitlines()) <= 6
        assert "and 73 more" in out
        assert "03_segmented.json" in out

    def test_examples_do_not_repeat_a_code(self, capsys):
        # One example each of three codes beats three of the loudest one.
        many = warnings_of(*(["card_too_short"] * 20 + ["cps_exceeded"]
                             + ["hebrew_rule_violation"]))
        _report_warnings(many, Path("03_segmented.json"))
        shown = [ln for ln in capsys.readouterr().out.splitlines()
                 if ln.strip().startswith("e.g.")]
        codes = [ln.split("[", 1)[1].split("]", 1)[0] for ln in shown]
        assert len(codes) == len(set(codes))
        assert set(codes) == {"card_too_short", "cps_exceeded",
                              "hebrew_rule_violation"}

    def test_a_short_list_says_nothing_about_more(self, capsys):
        _report_warnings(warnings_of("cps_exceeded"), Path("03_segmented.json"))
        assert "more" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# naming the clip so two runs are tellable apart
# --------------------------------------------------------------------------


class TestNextClipName:
    """Re-running before deleting the previous clip leaves two identically
    named subtitle clips in the pool, and Resolve shows no import time in the
    list -- there is no way to tell which is which. Versioning removes the
    guess: the highest V is the newest.
    """

    def test_an_empty_bin_gets_the_plain_name(self):
        assert next_clip_name([]) == DEFAULT_CLIP_NAME

    def test_a_bin_without_ours_gets_the_plain_name(self):
        assert next_clip_name(["C0502.MP4", "music.mp3"]) == DEFAULT_CLIP_NAME

    def test_the_base_name_counts_as_v1(self):
        assert next_clip_name([DEFAULT_CLIP_NAME]) == f"{DEFAULT_CLIP_NAME} V2"

    def test_it_climbs(self):
        names = [DEFAULT_CLIP_NAME]
        for expected in ("V2", "V3", "V4"):
            nxt = next_clip_name(names)
            assert nxt == f"{DEFAULT_CLIP_NAME} {expected}"
            names.append(nxt)

    def test_it_takes_the_highest_not_the_count(self):
        # Deleting the middle ones must not hand out a name already in use.
        assert next_clip_name([f"{DEFAULT_CLIP_NAME} V7"]) == (
            f"{DEFAULT_CLIP_NAME} V8"
        )

    def test_a_deleted_base_still_climbs(self):
        assert next_clip_name([f"{DEFAULT_CLIP_NAME} V2",
                               f"{DEFAULT_CLIP_NAME} V3"]) == (
            f"{DEFAULT_CLIP_NAME} V4"
        )

    def test_unrelated_names_are_ignored(self):
        assert next_clip_name(["HebSub Subtitles Old", "Subtitles V9",
                               "HebSub"]) == DEFAULT_CLIP_NAME

    def test_case_and_whitespace_do_not_fool_it(self):
        assert next_clip_name([f"  {DEFAULT_CLIP_NAME.lower()} v3  "]) == (
            f"{DEFAULT_CLIP_NAME} V4"
        )

    def test_none_and_blank_entries_do_not_raise(self):
        assert next_clip_name([None, "", "   "]) == DEFAULT_CLIP_NAME

    def test_the_name_is_a_legal_filename(self):
        name = next_clip_name([DEFAULT_CLIP_NAME])
        assert not set(name) & set(r'\\/:*?"<>|')


# --------------------------------------------------------------------------
# one folder per timeline, per project
# --------------------------------------------------------------------------


class TestWorkDirName:
    """Keyed on the timeline alone, two projects that each hold a
    "Timeline 1" share a folder and overwrite each other's artifacts. The
    rendered audio was never at risk -- the fingerprint covers clip paths and
    mtimes, so a different project misses the cache and re-renders (D42) --
    but `01_raw.json`, `final.srt` and `review.json` were.
    """

    def test_the_project_is_part_of_the_name(self):
        assert "GUY" in work_dir_name("GUY TEST SUBS", "Timeline 1")

    def test_two_projects_with_the_same_timeline_do_not_collide(self):
        a = work_dir_name("Client A", "Timeline 1")
        b = work_dir_name("Client B", "Timeline 1")
        assert a != b

    def test_two_timelines_in_one_project_do_not_collide(self):
        a = work_dir_name("Same Project", "Timeline 1")
        b = work_dir_name("Same Project", "Timeline 2")
        assert a != b

    def test_it_is_a_legal_folder_name(self):
        name = work_dir_name("Raz / clients: 2026", 'Timeline 1 — "cut"')
        assert not set(name) & set(r'\/:*?"<>|')

    def test_hebrew_names_do_not_vanish(self):
        # A Hebrew timeline name must not slug down to nothing.
        name = work_dir_name("פרויקט", "סרטון 3")
        assert name and name != "__"

    def test_empty_names_still_produce_something(self):
        assert work_dir_name("", "") == "unnamed__unnamed"

    def test_it_is_stable(self):
        assert work_dir_name("P", "T") == work_dir_name("P", "T")


# --------------------------------------------------------------------------
# review navigation -- markers and the review track
# --------------------------------------------------------------------------


class FakeMarkerTimeline:
    """Just the marker half of the Timeline API.

    Resolve keys markers by frame and silently refuses a second one on an
    occupied frame -- AddMarker returns False rather than raising -- which is
    the behaviour the nudge exists for.
    """

    def __init__(self, markers=None, origin=108000):
        self.markers = dict(markers or {})
        self._origin = origin

    def GetStartFrame(self):
        return self._origin

    def GetMarkers(self):
        return dict(self.markers)

    def AddMarker(self, frame, colour, name, note, duration, custom=""):
        if frame in self.markers:
            return False
        self.markers[frame] = {
            "color": colour, "name": name, "note": note,
            "duration": duration, "customData": custom,
        }
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(frame, None) is not None


REVIEW = [
    {"wid": 4, "start": 1.0, "heard": "שלום",
     "alternative": "שלו"},
    {"wid": 9, "start": 2.5, "heard": "כן",
     "alternative": "קן"},
]


def test_markers_land_relative_to_the_timeline_start():
    # frameId is measured from the timeline start, not from 01:00:00:00.
    # Probed live: an existing marker read back as frame 30289 on a timeline
    # whose GetStartFrame() is 108000. Getting this wrong puts every marker
    # an hour out.
    timeline = FakeMarkerTimeline()
    assert place_review_markers(FakeProject(fps=30.0), timeline, REVIEW) == 2
    assert sorted(timeline.markers) == [30, 75]
    assert timeline.markers[30]["color"] == REVIEW_MARKER_COLOR
    assert timeline.markers[30]["customData"] == REVIEW_MARKER_TAG


def test_marker_note_carries_what_the_second_model_heard():
    timeline = FakeMarkerTimeline()
    place_review_markers(FakeProject(fps=30.0), timeline, REVIEW)
    assert timeline.markers[30]["name"] == REVIEW[0]["heard"]
    assert REVIEW[0]["alternative"] in timeline.markers[30]["note"]


def test_a_clash_nudges_rather_than_dropping_the_flag():
    # Two flagged words inside the same frame, or one of the user's own
    # markers already sitting there. Losing the flag silently is the one
    # outcome that is not acceptable.
    timeline = FakeMarkerTimeline({30: {"color": "Cyan", "customData": ""}})
    flags = [dict(REVIEW[0]), dict(REVIEW[0], wid=5)]
    assert place_review_markers(FakeProject(fps=30.0), timeline, flags) == 2
    assert sorted(timeline.markers) == [30, 31, 32]


def test_clearing_spares_the_users_own_markers():
    # Raz's timeline already carries a Cyan marker of his own.
    # DeleteMarkersByColor would take it with ours; customData will not.
    timeline = FakeMarkerTimeline({
        12: {"color": "Cyan", "customData": ""},
        30: {"color": REVIEW_MARKER_COLOR, "customData": REVIEW_MARKER_TAG},
        75: {"color": REVIEW_MARKER_COLOR, "customData": REVIEW_MARKER_TAG},
    })
    assert clear_review_markers(timeline) == 2
    assert list(timeline.markers) == [12]


def test_rerunning_replaces_its_own_markers_instead_of_stacking_them():
    timeline = FakeMarkerTimeline({9: {"color": "Blue", "customData": ""}})
    project = FakeProject(fps=30.0)
    place_review_markers(project, timeline, REVIEW)
    place_review_markers(project, timeline, REVIEW)
    assert sorted(timeline.markers) == [9, 30, 75]


def _cards():
    return {"segments": [
        {"start": 0.5, "end": 2.0, "words": [
            {"wid": 3, "w": "a"}, {"wid": 4, "w": "b"}]},
        {"start": 2.2, "end": 3.4, "words": [
            {"wid": 9, "w": "c"}]},
    ]}


def test_review_cards_span_the_card_the_word_sits_in():
    strip = review_cards(_cards(), REVIEW)
    assert [(c[0], c[1]) for c in strip] == [(0.5, 2.0), (2.2, 3.4)]


def test_review_card_shows_the_word_above_the_alternative():
    strip = review_cards(_cards(), REVIEW)
    expected = REVIEW[0]["heard"] + "\n" + REVIEW[0]["alternative"]
    assert strip[0][2] == expected


def test_a_flag_with_no_surviving_card_is_skipped_not_faked():
    strip = review_cards(_cards(), [dict(REVIEW[0], wid=999)])
    assert strip == []


def test_the_review_track_versions_off_its_own_base():
    # "HebSub Subtitles V2" in the bin must not push the review clip to V2.
    existing = [DEFAULT_CLIP_NAME, f"{DEFAULT_CLIP_NAME} V2", REVIEW_CLIP_NAME]
    assert next_clip_name(existing, base=REVIEW_CLIP_NAME) == (
        f"{REVIEW_CLIP_NAME} V2"
    )
    assert next_clip_name(existing) == f"{DEFAULT_CLIP_NAME} V3"


class FakeSeekTimeline(FakeMarkerTimeline):
    def __init__(self, origin=108000, accept=True):
        super().__init__(origin=origin)
        self._accept = accept
        self.sent = None

    def SetCurrentTimecode(self, tc):
        self.sent = tc
        return self._accept


class TestSeek:
    """Moving the playhead to a flagged word.

    The trap: `SetCurrentTimecode` wants the ABSOLUTE timecode, while marker
    frames are relative to the timeline start. Both conventions live in this
    module and they are not the same.
    """

    def test_second_zero_is_the_timelines_own_start(self):
        # A Resolve timeline starts at 01:00:00:00, not 00:00:00:00.
        assert timecode_at(FakeProject(30.0), FakeSeekTimeline(), 0.0) ==             "01:00:00:00"

    def test_offsets_are_added_to_that_start(self):
        project, timeline = FakeProject(30.0), FakeSeekTimeline()
        # Probed live on Raz's timeline: each of these read back identically
        # from GetCurrentTimecode.
        assert timecode_at(project, timeline, 5.0) == "01:00:05:00"
        assert timecode_at(project, timeline, 60.0) == "01:01:00:00"
        assert timecode_at(project, timeline, 1009.63) == "01:16:49:19"

    def test_a_timeline_starting_at_zero_is_not_special_cased(self):
        assert timecode_at(FakeProject(30.0), FakeSeekTimeline(origin=0),
                           61.5) == "00:01:01:15"

    def test_seek_sends_it_and_returns_it(self):
        timeline = FakeSeekTimeline()
        assert seek(FakeProject(30.0), timeline, 5.0) == "01:00:05:00"
        assert timeline.sent == "01:00:05:00"

    def test_a_refused_seek_is_loud(self):
        from hebsub.host_resolve import ResolveError

        with pytest.raises(ResolveError):
            seek(FakeProject(30.0), FakeSeekTimeline(accept=False), 5.0)


# --------------------------------------------------------------------------
# learning from the corrected track
# --------------------------------------------------------------------------


class FakeCard:
    """A subtitle TimelineItem. Its GetName() IS its text (probed live)."""

    def __init__(self, text):
        self._text = text

    def GetName(self):
        return self._text


class FakeSubtitleTimeline:
    def __init__(self, tracks):
        self._tracks = tracks      # {index: [text, ...]}

    def GetTrackCount(self, kind):
        return max(self._tracks) if kind == "subtitle" and self._tracks else 0

    def GetItemListInTrack(self, kind, index):
        if kind != "subtitle":
            return []
        return [FakeCard(t) for t in self._tracks.get(index, [])]


ONE = "\u05e9\u05dc\u05d5\u05dd"           # shalom
TWO = "\u05e2\u05d5\u05dc\u05dd"           # olam
THREE = "\u05d7\u05d1\u05e8\u05d9\u05dd"  # chaverim
FIXED = "\u05d9\u05ea\u05dc\u05e9"         # a word only the human typed


def srt_of(words, tmp_path):
    """A minimal .srt carrying those words; only the text is read back."""
    body = "".join(
        f"{i}\n00:00:0{i},000 --> 00:00:0{i},900\n{w}\n\n"
        for i, w in enumerate(words, start=1)
    )
    path = tmp_path / "final.srt"
    path.write_text(body, encoding="utf-8")
    return path


class TestSubtitleTracks:
    def test_a_cards_name_is_its_text(self):
        tracks = subtitle_tracks(FakeSubtitleTimeline({1: [ONE, TWO]}))
        assert tracks[0]["cards"] == 2
        assert ONE in tracks[0]["text"] and TWO in tracks[0]["text"]

    def test_whitespace_is_normalised(self):
        # A two-line card comes back with its break already gone, and cards
        # are joined for a word-level comparison. Neither should leave ragged
        # whitespace that splits a word.
        tracks = subtitle_tracks(
            FakeSubtitleTimeline({1: [f"  {ONE}   {TWO}  "]})
        )
        assert tracks[0]["text"] == f"{ONE} {TWO}"

    def test_every_track_is_reported(self):
        tracks = subtitle_tracks(FakeSubtitleTimeline({1: [ONE], 2: [TWO]}))
        assert [t["index"] for t in tracks] == [1, 2]

    def test_no_subtitle_track_is_not_an_error(self):
        assert subtitle_tracks(FakeSubtitleTimeline({})) == []


class TestHarvest:
    def test_only_the_difference_is_learned(self, tmp_path):
        # The point of the whole feature. Learning the WHOLE track would feed
        # the ASR's own mistakes back in, and the lexicon exists to catch
        # exactly those.
        srt = srt_of([ONE, TWO, THREE], tmp_path)
        timeline = FakeSubtitleTimeline({1: [ONE, TWO, THREE, FIXED]})
        lex = tmp_path / "lexicon.txt"

        result = harvest_corrections(timeline, srt, lex)
        assert result["status"] == "learned"
        assert result["learned"] == [FIXED]
        assert lex.read_text(encoding="utf-8").split() == [FIXED]

    def test_an_unchanged_track_learns_nothing(self, tmp_path):
        srt = srt_of([ONE, TWO], tmp_path)
        timeline = FakeSubtitleTimeline({1: [ONE, TWO]})
        result = harvest_corrections(timeline, srt, tmp_path / "lexicon.txt")
        assert result["learned"] == []

    def test_a_word_already_known_is_not_added_twice(self, tmp_path):
        srt = srt_of([ONE], tmp_path)
        lex = tmp_path / "lexicon.txt"
        lex.write_text(FIXED + "\n", encoding="utf-8")
        timeline = FakeSubtitleTimeline({1: [ONE, FIXED]})

        assert harvest_corrections(timeline, srt, lex)["learned"] == []
        assert lex.read_text(encoding="utf-8").split() == [FIXED]

    def test_the_corrected_track_is_chosen_by_overlap_not_by_index(self, tmp_path):
        # Track 1 is the review track; track 2 is the real subtitles. Picking
        # by index would harvest the annotations.
        srt = srt_of([ONE, TWO, THREE], tmp_path)
        timeline = FakeSubtitleTimeline({
            1: [f"{ONE}\n{TWO}"],
            2: [ONE, TWO, THREE, FIXED],
        })
        result = harvest_corrections(timeline, srt, tmp_path / "lexicon.txt")
        assert result["track"] == 2
        assert result["learned"] == [FIXED]

    def test_a_different_programme_is_refused(self, tmp_path):
        # A track that shares almost nothing with our output is not a
        # correction of it. Saying so beats learning a stranger's words.
        srt = srt_of([ONE, TWO, THREE], tmp_path)
        timeline = FakeSubtitleTimeline({1: [FIXED]})
        lex = tmp_path / "lexicon.txt"

        result = harvest_corrections(timeline, srt, lex)
        assert result["status"] == "no_match"
        assert result["learned"] == []
        assert not lex.exists()

    def test_no_previous_run_is_reported_not_raised(self, tmp_path):
        result = harvest_corrections(
            FakeSubtitleTimeline({1: [ONE]}),
            tmp_path / "missing.srt",
            tmp_path / "lexicon.txt",
        )
        assert result["status"] == "no_srt"

    def test_the_lexicon_is_found_from_the_package_not_the_cwd(self):
        # Launched from Resolve's Workflow > Scripts menu the working
        # directory is Resolve's, and every learned word was invisible.
        from hebsub.host_resolve import lexicon_path

        assert lexicon_path().is_absolute()
        assert lexicon_path().name == "lexicon.txt"
        assert (lexicon_path().parent / "src" / "hebsub").is_dir()

