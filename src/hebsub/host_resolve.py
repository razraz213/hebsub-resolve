"""DaVinci Resolve host adapter -- the one button.

The only module that imports DaVinciResolveScript. It renders the timeline's
audio, hands it to the pipeline, and puts the result back on the timeline. It
contains no ASR, no Hebrew logic and no segmentation: if you want to change
how cards are cut, change `segment`, not this file (D26).

Everything here was verified against Resolve Studio 21.0.3.7; the findings are
recorded as D27-D29 in docs/decisions-004.md. Three of them are load-bearing:

  * Placing an .srt: ImportMedia() then a PLAIN AppendToTimeline([clip]).
    The {clipInfo} dict form returns None, leaves the Timeline handle dead,
    and crashed Resolve outright during the probe. Never use it here.
  * Placing Text+: a title inserted at the playhead performs an OVERWRITE
    edit, trimming the previous card to end where the new one starts. That is
    what gives exact durations despite TimelineItem having no SetDuration.
  * Text+ MUST be given an explicit Hebrew-capable font. On the default font
    Hebrew renders as empty boxes -- which looks like broken bidi and is not.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import json
import os
import sys
import time
from pathlib import Path

__all__ = [
    "connect", "ResolveError", "run", "place_srt", "place_textplus",
    "next_clip_name", "picture_spans", "speech_spans", "intersect_spans",
    "work_dir_name", "place_review_markers", "clear_review_markers",
    "review_cards", "REVIEW_MARKER_TAG", "timecode_at", "seek",
    "audio_cut_list", "timeline_fingerprint", "prepare_audio",
    "subtitle_tracks", "harvest_corrections", "lexicon_path",
    "work_root",
]

MODULE = "host_resolve"

WIN_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
WIN_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"

# The second opinion for the review list. NOT a better model -- it is worse
# on its own (5.50% against turbo's 4.73%). It is here because it is
# independently useful: a word the two disagree on is wrong 48.6% of the
# time against 1.5% for a word they agree on (D47, D48).
DEFAULT_REVIEW_MODEL = "ivrit-ai/whisper-large-v3-ct2"

DEFAULT_FONT = "Arial"
DEFAULT_TRACK_NAME = "hebsub"

# What the clip is called in the media pool. The file on disk stays
# `final.srt` -- CLAUDE.md rule 6 names the four artifacts and that is not a
# display concern -- but "final" is a terrible thing to hunt for in a media
# pool that already contains a hundred clips.
DEFAULT_CLIP_NAME = "HebSub Subtitles"
REVIEW_CLIP_NAME = "HebSub Review"

# Markers this tool owns. Colour is for the eye; the TAG is what identifies
# them, because Raz's timeline already carries a Cyan marker of his own and
# DeleteMarkersByColor would take it with ours.
REVIEW_MARKER_COLOR = "Fuchsia"
REVIEW_MARKER_TAG = "hebsub-review"

# Written beside the rendered WAV so a later run can tell whether the
# timeline still produces that same audio.
FINGERPRINT_SUFFIX = ".fingerprint"

# Files in the work dir that are never the rendered audio.
_NOT_AUDIO = (".json", ".srt", FINGERPRINT_SUFFIX)


class ResolveError(Exception):
    """Raised when Resolve is unreachable or refuses an operation."""


def _fail(problem: str) -> None:
    raise ResolveError(f"{MODULE}: {problem}")


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------


def connect():
    """Return the Resolve app object, or raise with something actionable."""
    if os.name == "nt":
        os.environ.setdefault("RESOLVE_SCRIPT_API", WIN_API)
        os.environ.setdefault("RESOLVE_SCRIPT_LIB", WIN_LIB)
        modules = str(Path(os.environ["RESOLVE_SCRIPT_API"]) / "Modules")
        if modules not in sys.path:
            sys.path.append(modules)

    try:
        import DaVinciResolveScript as dvr
    except ImportError as exc:
        _fail(
            f"cannot import DaVinciResolveScript ({exc}). Resolve's scripting "
            f"modules were not found -- expected them under "
            f"{os.environ.get('RESOLVE_SCRIPT_API', '<unset>')}"
        )

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        _fail(
            "Resolve is not reachable. Either it is not running, or external "
            "scripting is disabled in Preferences > System > General > "
            "'External scripting using'."
        )
    return resolve


def _current(resolve):
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        _fail("no project is open in Resolve")
    timeline = project.GetCurrentTimeline()
    if timeline is None:
        _fail("no timeline is open in Resolve")
    return project, timeline


def _fps(project) -> float:
    try:
        return float(project.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        _fail("could not read the timeline frame rate from the project")


def _timecode(frame: int, fps: float) -> str:
    """Absolute timeline frame -> HH:MM:SS:FF, using the timeline's own rate."""
    rate = int(round(fps))
    hours, rem = divmod(int(frame), 3600 * rate)
    minutes, rem = divmod(rem, 60 * rate)
    secs, frames = divmod(rem, rate)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"


# --------------------------------------------------------------------------
# audio out
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# reading the timeline's audio structure -- no render, no modification
# --------------------------------------------------------------------------


def audio_cut_list(project, timeline) -> dict:
    """Describe the timeline's audio using getters only.

    Every call here is read-only, and the whole thing runs in well under a
    second on a 200-clip timeline. That is what makes it usable as a cache key
    for the 21-second render.

    Two rate traps live in here, and both are silent when you get them wrong:

      * `GetStart`/`GetDuration` count **timeline** frames; `GetSourceStartFrame`
        counts **source** frames. On 25 fps media in a 30 fps timeline those
        are different units.
      * A Resolve timeline starts at 01:00:00:00, so `GetStart` on the first
        clip is 108000 at 30 fps, not 0. `GetStartFrame()` is the origin.

    (`GetSourceStartTime` looks like the convenient one and is not: it returns
    the source *timecode* in seconds, so a camera file stamped 19:10:09:05
    reports 69009.2 rather than an offset into the file.)

    Items with no media behind them -- transitions, mostly -- are reported in
    `unresolved` rather than raising. They are usually harmless: the clips a
    cross-fade sits between are themselves in the list.
    """
    fps = _fps(project)
    origin = timeline.GetStartFrame()
    items: list[dict] = []
    unresolved: list[dict] = []

    for track in range(1, timeline.GetTrackCount("audio") + 1):
        try:
            enabled = bool(timeline.GetIsTrackEnabled("audio", track))
        except Exception:  # noqa: BLE001 - older builds may not expose it
            enabled = True

        for item in timeline.GetItemListInTrack("audio", track) or []:
            record = {
                "track": track,
                "enabled": enabled,
                "name": item.GetName(),
                "timeline_start_s": round((item.GetStart() - origin) / fps, 6),
                "duration_s": round(item.GetDuration() / fps, 6),
            }

            media = item.GetMediaPoolItem()
            path = media.GetClipProperty("File Path") if media is not None else None
            if not path:
                record["why"] = (
                    "no media pool item" if media is None else "empty File Path"
                )
                unresolved.append(record)
                continue

            try:
                source_fps = float(media.GetClipProperty("FPS"))
            except (TypeError, ValueError):
                source_fps = fps
            if not source_fps:
                source_fps = fps

            try:
                stat = Path(path).stat()
                size, mtime = stat.st_size, round(stat.st_mtime, 3)
            except OSError:
                # An offline or re-linked clip. Not an error here -- the render
                # is what has to cope with it -- but it does mean the cache
                # cannot vouch for the file, so both fields stay None.
                size = mtime = None

            record.update({
                "path": str(path),
                "source_in_s": round(item.GetSourceStartFrame() / source_fps, 6),
                "size": size,
                "mtime": mtime,
            })
            items.append(record)

    return {
        "fps": fps,
        "origin_frame": origin,
        "length_s": round((timeline.GetEndFrame() - origin) / fps, 6),
        "items": items,
        "unresolved": unresolved,
    }


def timeline_fingerprint(cut_list: dict) -> str:
    """Hash of everything about the timeline that changes its audio.

    Deliberately excludes clip names and the timeline origin: renaming a clip
    or moving the whole timeline's start timecode does not change a single
    sample. Deliberately includes each source file's size and mtime, so
    re-exporting or re-linking a source invalidates the cache.

    **What it cannot see is the Fairlight mixer.** `TimelineItem.GetProperty()`
    returns an empty dict for audio items -- no clip gain, no fades, no
    automation (D42). Change only a volume curve and this hash will not move.
    That is the one way a reused WAV can be stale, and it is why
    `prepare_audio` always says out loud which path it took.
    """
    significant = {
        "fps": cut_list["fps"],
        "items": [
            [i["track"], i["enabled"], i["path"], i["source_in_s"],
             i["timeline_start_s"], i["duration_s"], i["size"], i["mtime"]]
            for i in cut_list["items"]
        ],
        # transitions carry no media but they do change the mix
        "unresolved": [
            [u["track"], u["timeline_start_s"], u["duration_s"]]
            for u in cut_list["unresolved"]
        ],
    }
    canonical = json.dumps(significant, sort_keys=True, ensure_ascii=False)
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()


def existing_audio(out_dir: Path) -> list[Path]:
    """Rendered audio already sitting in the work dir."""
    return sorted(
        p for p in out_dir.glob("hebsub_audio*")
        if p.is_file() and p.suffix.lower() not in _NOT_AUDIO
    )


def prepare_audio(
    project, timeline, out_dir: Path, *, reuse: bool = True
) -> tuple[Path, str]:
    """Get the timeline's audio, rendering only when it has actually changed.

    Returns `(wav, "reused" | "rendered")`.

    The render costs ~21 s and cannot be made meaningfully cheaper:
    `AudioBitDepth: 16` shrinks the file by a third and moves the clock by
    about a second, and Resolve refuses `AudioSampleRate: 16000` outright
    (D42). So the saving has to come from not rendering at all, which is safe
    exactly when the fingerprint matches.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = out_dir / ("hebsub_audio" + FINGERPRINT_SUFFIX)

    fingerprint = None
    try:
        fingerprint = timeline_fingerprint(audio_cut_list(project, timeline))
    except Exception as exc:  # noqa: BLE001
        # Never let a cache optimisation break the actual job.
        print(f"{MODULE}: could not fingerprint the timeline ({exc}); rendering")

    if reuse and fingerprint and stamp.exists():
        already = existing_audio(out_dir)
        if already and stamp.read_text(encoding="utf-8").strip() == fingerprint:
            print(
                f"{MODULE}: timeline unchanged since the last run -- reusing "
                f"{already[0].parent.name}/{already[0].name}, skipping the "
                f"render"
            )
            return already[0], "reused"

    wav = render_timeline_audio(project, timeline, out_dir)
    if fingerprint:
        stamp.write_text(fingerprint + "\n", encoding="utf-8")
    return wav, "rendered"


def render_timeline_audio(project, timeline, out_dir: Path, timeout_s: int = 900) -> Path:
    """Render the timeline's audio mix to a WAV and return its path.

    The mix is what the viewer hears, which is what the subtitles have to
    match -- reading the source clips directly would miss level automation,
    muted takes and everything else the edit actually does.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("hebsub_audio*"):
        try:
            stale.unlink()
        except OSError:
            pass

    # Do NOT touch jobs we did not create. DeleteAllRenderJobs() would wipe a
    # user's queued renders -- on a real client project that is destroying
    # their work to extract an audio file. Remember what was already queued so
    # only our own job is removed afterwards.
    try:
        pre_existing = {
            job.get("JobId") for job in (project.GetRenderJobList() or [])
        }
    except Exception:
        pre_existing = set()

    # Their render format is part of their project setup; put it back.
    try:
        previous_format = project.GetCurrentRenderFormatAndCodec()
    except Exception:
        previous_format = None

    # Audio-only formats carry an implicit codec, so
    # SetCurrentRenderFormatAndCodec("wav", ...) returns False for every codec
    # string -- GetRenderCodecs("wav") is empty. The stock "Audio Only" preset
    # is the supported route. Whatever container it picks is fine: transcribe
    # runs ffmpeg over it anyway.
    if not project.LoadRenderPreset("Audio Only"):
        _fail(
            "Resolve refused the 'Audio Only' render preset. Without it there "
            "is no scripted way to render audio alone."
        )

    settings = {
        "SelectAllFrames": True,
        "TargetDir": str(out_dir),
        "CustomName": "hebsub_audio",
        "ExportVideo": False,
        "ExportAudio": True,
        # A third off the file for nothing: 306 MB -> 204 MB on a 17-minute
        # timeline. It does NOT make the render faster (22.5s -> 21.0s) --
        # Resolve is busy mixing, not writing. Asking for 16 kHz as well is
        # rejected outright, so this is as cheap as the render gets (D42).
        "AudioBitDepth": 16,
    }
    if not project.SetRenderSettings(settings):
        _fail(f"Resolve refused the render settings: {settings}")

    job = project.AddRenderJob()
    if not job:
        _fail("Resolve refused to queue the audio render job")

    project.StartRendering([job], isInteractiveMode=False)
    waited = 0
    while project.IsRenderingInProgress() and waited < timeout_s:
        time.sleep(1)
        waited += 1
    timed_out = project.IsRenderingInProgress()
    if timed_out:
        project.StopRendering()

    # Clean up after ourselves: our job, and only our job. Then hand the
    # render page back the way we found it.
    try:
        for entry in project.GetRenderJobList() or []:
            job_id = entry.get("JobId")
            if job_id and job_id not in pre_existing:
                project.DeleteRenderJob(job_id)
    except Exception:
        pass
    if previous_format:
        try:
            project.SetCurrentRenderFormatAndCodec(
                previous_format.get("format", ""), previous_format.get("codec", "")
            )
        except Exception:
            pass

    if timed_out:
        _fail(f"audio render did not finish within {timeout_s}s")

    # Do not assume an extension -- the preset chooses the container.
    produced = existing_audio(out_dir)
    if not produced:
        anything = sorted(p.name for p in out_dir.iterdir())
        _fail(
            f"audio render reported success but produced nothing in {out_dir} "
            f"(found: {anything or 'nothing'})"
        )
    return produced[0]


# --------------------------------------------------------------------------
# placement A -- srt onto a subtitle track
# --------------------------------------------------------------------------


# "HebSub Subtitles", then "HebSub Subtitles V2", V3, ... The base name counts
# as V1, so the highest number visible is always the newest. The review track
# versions independently off its own base.


def _slug(text: str) -> str:
    """Filesystem-safe, and readable enough to recognise in a folder listing."""
    out = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in text)
    return out.strip("_") or "unnamed"


def work_dir_name(project_name: str, timeline_name: str) -> str:
    """Folder for one timeline's artifacts, scoped by project.

    Keyed on the timeline alone, two projects that each contain a "Timeline 1"
    share a folder and overwrite each other's `01_raw.json`, `final.srt` and
    `review.json`. The rendered audio itself was never at risk -- the
    fingerprint covers clip paths, positions and mtimes, so a different
    project misses the cache and re-renders (D42) -- but everything downstream
    of it was.

    Pure, so the rule is testable without Resolve.
    """
    return f"{_slug(project_name)}__{_slug(timeline_name)}"


def _versioned(base: str) -> "re.Pattern[str]":
    return re.compile(rf"^{re.escape(base)}(?:\s+V(\d+))?$", re.IGNORECASE)


def next_clip_name(existing, base: str = DEFAULT_CLIP_NAME) -> str:
    """The next unused subtitle-clip name, given what the bin already holds.

    Re-running before deleting the previous clip leaves two identically named
    subtitle clips in the pool and no way to tell which is which -- Resolve
    shows no import time in the list. Versioning removes the guess: the
    highest V wins.

    Pure, so it is testable without Resolve.
    """
    pattern = _versioned(base)
    highest = 0
    for name in existing:
        match = pattern.match((name or "").strip())
        if not match:
            continue
        highest = max(highest, int(match.group(1)) if match.group(1) else 1)
    if highest == 0:
        return base
    return f"{base} V{highest + 1}"


def speech_spans(wav_path: Path) -> list[tuple[float, float]]:
    """Where the VAD says there is actually speech, in seconds.

    Whisper's word onsets run early -- measured at a median 34ms against Raz's
    corrected files. A large part of that is the VAD's own `speech_pad_ms`,
    which defaults to **400ms** and hands the model a lead-in of room tone
    before every utterance; the model then puts the word onset somewhere in
    it. Running the VAD again with no padding gives the tight boundary.

    Returns [] if the VAD is unavailable, so a missing dependency degrades to
    the previous behaviour rather than failing the run.
    """
    try:
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except ImportError:
        return []
    try:
        audio = decode_audio(str(wav_path), sampling_rate=16000)
        stamps = get_speech_timestamps(
            audio,
            VadOptions(
                # 400ms of padding is the default and a large part of why
                # onsets run early: it hands the model room tone before every
                # utterance and the word lands somewhere inside it.
                speech_pad_ms=0,
                # 2000ms only splits at video boundaries. 200 also splits at
                # ordinary pauses, which is where Raz sees the drift, and
                # matches the gap threshold export already uses.
                min_silence_duration_ms=200,
            ),
            sampling_rate=16000,
        )
    except Exception:  # noqa: BLE001 - never fail a run over a timing refinement
        return []
    return [(s["start"] / 16000.0, s["end"] / 16000.0) for s in stamps]


def intersect_spans(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Ranges covered by both. Either side empty means "no constraint"."""
    if not a:
        return list(b)
    if not b:
        return list(a)
    out: list[tuple[float, float]] = []
    i = j = 0
    a, b = sorted(a), sorted(b)
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def picture_spans(project, timeline) -> list[tuple[float, float]]:
    """Ranges of the timeline that actually have a picture, in seconds.

    Read-only. Used to stop a card being displayed before the video it
    belongs to: Whisper's onset for the first word after silence takes the
    breath with it, measured at up to 460ms early, which on a timeline of
    separate videos puts the card in the black gap before the next one.
    """
    fps = _fps(project)
    origin = timeline.GetStartFrame()
    spans: list[tuple[float, float]] = []
    for track in range(1, timeline.GetTrackCount("video") + 1):
        for item in timeline.GetItemListInTrack("video", track) or []:
            spans.append(((item.GetStart() - origin) / fps,
                          (item.GetEnd() - origin) / fps))
    spans.sort()
    merged: list[tuple[float, float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def timecode_at(project, timeline, seconds: float) -> str:
    """Seconds into the programme -> the timecode you type into Resolve.

    A Resolve timeline starts at 01:00:00:00, so second zero is frame 108000
    at 30fps, not frame 0. `SetCurrentTimecode` wants the ABSOLUTE timecode --
    unlike marker frames, which are relative to the timeline start (D67). The
    two conventions sit next to each other in this file and are not the same.
    """
    fps = _fps(project)
    frame = timeline.GetStartFrame() + int(round(float(seconds) * fps))
    return _timecode(frame, fps)


def seek(project, timeline, seconds: float) -> str:
    """Move Resolve's playhead to `seconds` into the programme.

    Returns the timecode it was sent to. Probed live: setting 0.0, 5.0, 60.0
    and 1009.63s on a 30fps timeline each read back byte-identical from
    GetCurrentTimecode, from the edit page.
    """
    target = timecode_at(project, timeline, seconds)
    if not timeline.SetCurrentTimecode(target):
        _fail(f"Resolve would not move the playhead to {target}")
    return target


def clear_review_markers(timeline) -> int:
    """Remove the markers this tool added, and only those.

    Identified by `customData`, never by colour: Raz's timeline already
    carries a Cyan marker of his own, and `DeleteMarkersByColor` would delete
    it alongside ours. Losing a user's markers to a cleanup step is exactly
    the class of damage the render-queue fix in D42 exists to prevent.
    """
    removed = 0
    for frame, info in list((timeline.GetMarkers() or {}).items()):
        tag = (info.get("customData") or "")
        if tag.startswith(REVIEW_MARKER_TAG):
            if timeline.DeleteMarkerAtFrame(frame):
                removed += 1
    return removed


def place_review_markers(project, timeline, review: list[dict]) -> int:
    """Drop a marker on each flagged word so Resolve can navigate to them.

    Up/down arrow steps between markers and the Markers panel lists them with
    their notes, so the words the second opinion could not resolve are
    reachable without leaving the edit page or opening review.json.

    Marker frames are relative to the timeline start -- confirmed against an
    existing marker at frame 30289 on a timeline whose start frame is 108000.
    Getting that wrong would place every marker an hour out.
    """
    fps = _fps(project)
    clear_review_markers(timeline)
    placed = 0
    for flag in review:
        frame = max(0, int(round(float(flag["start"]) * fps)))
        note = f"2nd model heard: {flag['alternative']}"
        # One marker per frame. A clash -- two flagged words inside the same
        # frame, or one of the user's own markers -- is nudged, not dropped.
        for nudge in range(4):
            if timeline.AddMarker(frame + nudge, REVIEW_MARKER_COLOR,
                                  flag["heard"], note, 1, REVIEW_MARKER_TAG):
                placed += 1
                break
    return placed


def review_cards(cards: dict, review: list[dict]) -> list[tuple[float, float, str]]:
    """Turn flags into subtitle cards for a second, disposable track.

    Each card spans the real card the word sits in, and shows the word above
    the alternative. Read on a second subtitle track it sits beside the actual
    subtitle, in context, over the picture -- which is how you would check it
    anyway.
    """
    by_wid: dict[int, dict] = {}
    for segment in cards.get("segments", []):
        for word in segment.get("words", []):
            by_wid[word["wid"]] = segment
    out = []
    for flag in review:
        segment = by_wid.get(flag["wid"])
        if segment is None:
            continue
        out.append((float(segment["start"]), float(segment["end"]),
                    f"{flag['heard']}\n{flag['alternative']}"))
    return out


# --------------------------------------------------------------------------
# learning from what Raz actually typed
# --------------------------------------------------------------------------


def work_root() -> Path:
    """Where `work/<project>__<timeline>/` lives.

    Deliberately still relative to the working directory, which is what every
    run so far has used -- pinning it to the package would silently relocate
    artifacts that already exist. One function so the panel and `run` cannot
    disagree about where the last run's .srt went.
    """
    return Path.cwd() / "work"


def lexicon_path() -> Path:
    """Where the learned words live, independent of the working directory.

    `proofread.hebrew_lexicon()` falls back to `Path.cwd() / lexicon.txt`,
    which is right for the CLI and wrong for the panel: launched from
    Resolve's Workflow > Scripts menu the working directory is Resolve's, not
    the repo's, so every word Raz had taught it was silently invisible. The
    file sits beside the package, so resolve it from the package.
    """
    return Path(__file__).resolve().parents[2] / "lexicon.txt"


def subtitle_tracks(timeline) -> list[dict]:
    """Every subtitle track, as {index, cards, text}.

    A subtitle TimelineItem's `GetName()` IS its text -- probed live on a
    611-card track, which is what makes this possible without an export step.
    Two things that probe also showed, and both matter here:

      * A two-line card comes back with its line break already gone, so the
        text is whitespace-normalised on this side rather than compared
        line-for-line.
      * Resolve strips `<` from the name (the timing card reads
        ">> TIMING CLIP - DELETE ME " with its closing marks eaten). Harmless,
        because `learn_words` takes maximal runs of Hebrew letters and ignores
        every symbol anyway -- but do not build anything on the name being
        byte-identical to what was written.
    """
    out = []
    for index in range(1, (timeline.GetTrackCount("subtitle") or 0) + 1):
        items = timeline.GetItemListInTrack("subtitle", index) or []
        words = " ".join(" ".join((i.GetName() or "").split()) for i in items)
        out.append({"index": index, "cards": len(items), "text": words})
    return out


def harvest_corrections(timeline, srt_path: Path, lexicon_path: Path) -> dict:
    """Learn the words Raz typed that we did not, straight off the timeline.

    The whole feature is one set difference: every Hebrew word on the corrected
    subtitle track that is not in the .srt we generated is, by construction,
    something he wrote. No card alignment, no timecode matching -- both would
    break the moment he merged two cards or nudged the clip, and neither is
    needed to answer "which words are new".

    **Only the difference is learned, never the whole track.** Harvesting
    everything would feed the ASR's own mistakes back into the lexicon --
    `תתעכם` would become a word the checker recognises, and the one thing the
    lexicon exists to catch is exactly that. A word that differs from our
    output is a word he changed on purpose; a word that matches is our guess,
    which he may simply not have reached yet.

    The track is chosen by overlap rather than by index, so the review track
    and any unrelated subtitles cannot be mistaken for the corrected one. If
    nothing overlaps enough to be the same programme, this reports that and
    writes nothing.
    """
    from hebsub.proofread import learn_words, load_user_lexicon

    if not srt_path.exists():
        return {"status": "no_srt", "learned": [], "track": 0}

    ours = learn_words(srt_path.read_text(encoding="utf-8"))
    if not ours:
        return {"status": "no_srt", "learned": [], "track": 0}

    best, best_share = None, 0.0
    for track in subtitle_tracks(timeline):
        theirs = learn_words(track["text"])
        share = len(ours & theirs) / len(ours)
        if share > best_share:
            best, best_share = (track, theirs), share

    # Below half, this is not the programme we transcribed -- a different
    # reel, or a track that was deleted. Saying so beats learning its words.
    if best is None or best_share < 0.5:
        return {"status": "no_match", "learned": [], "track": 0,
                "share": round(best_share, 3)}

    track, theirs = best
    known = load_user_lexicon(lexicon_path)
    fresh = sorted(theirs - ours - known)
    if fresh:
        with lexicon_path.open("a", encoding="utf-8") as handle:
            for word in fresh:
                handle.write(word + "\n")
    return {
        "status": "learned",
        "learned": fresh,
        "track": track["index"],
        "cards": track["cards"],
        "share": round(best_share, 3),
        "total": len(known) + len(fresh),
        "lexicon": str(lexicon_path),
    }


def timeline_is_empty(timeline) -> bool:
    """True when no track holds any item."""
    for kind in ("video", "audio", "subtitle"):
        for idx in range(1, timeline.GetTrackCount(kind) + 1):
            if timeline.GetItemListInTrack(kind, idx):
                return False
    return True


def _import_named(media_pool, srt_path: Path, base: str):
    """Import a copy of `srt_path` into the MASTER BIN under a versioned name.

    The master bin, not whichever bin the user happens to have selected: a
    subtitle clip that lands three folders deep in someone else's project
    structure is a clip they will not find. Their selection is part of their
    workspace, so it is restored afterwards.

    Resolve refuses to rename a subtitle clip -- SetClipProperty("Clip Name")
    returns False for every key tried -- so the pool name follows the FILE
    name, and this imports a copy carrying the name we want. The artifact
    CLAUDE.md rule 6 names stays exactly where it is; this is a second,
    human-named copy beside it, for the media pool to display.

    Returns (pool_name, clip).
    """
    root = media_pool.GetRootFolder()
    existing = [c.GetName() for c in (root.GetClipList() or [])] if root else []
    name = next_clip_name(existing, base=base)
    named = srt_path.with_name(f"{name}{srt_path.suffix}")
    if named != srt_path:
        try:
            named.write_bytes(srt_path.read_bytes())
            srt_path = named
        except OSError:
            pass
    previous = media_pool.GetCurrentFolder()
    if root:
        media_pool.SetCurrentFolder(root)
    try:
        imported = media_pool.ImportMedia([str(srt_path)])
    finally:
        if previous:
            media_pool.SetCurrentFolder(previous)
    if not imported:
        _fail(f"Resolve would not import {srt_path}")
    return name, imported[0]


def place_srt(project, timeline, srt_path: Path) -> tuple[int, str]:
    """Import the .srt, and append it only when that is actually correct.

    AppendToTimeline appends after the timeline's existing content -- at the
    GLOBAL timeline end, regardless of which track the clip belongs to. On an
    empty timeline that puts the subtitles at frame 0, which is right. On a
    timeline that already has an edit it puts every card past the end of the
    programme, which is silently wrong: measured at exactly +1104 frames on a
    1104-frame timeline.

    There is no scripted way to place a subtitle clip at a chosen frame. The
    {clipInfo} form with recordFrame returns [None] for subtitle clips, and
    Timeline.ImportIntoTimeline() returns False for an .srt. So when the
    timeline is not empty this imports the clip into the media pool and stops,
    rather than appending it somewhere useless.

    Returns (cards_placed, status).
    """
    media_pool = project.GetMediaPool()
    clip_name, clip = _import_named(media_pool, srt_path, DEFAULT_CLIP_NAME)

    kind = clip.GetClipProperty("Type")
    if kind != "Subtitle":
        _fail(f"Resolve imported {clip_name} as {kind!r}, not a Subtitle clip")

    if not timeline_is_empty(timeline):
        return 0, f"pool:{clip_name}"

    if timeline.GetTrackCount("subtitle") < 1:
        if not timeline.AddTrack("subtitle"):
            _fail("could not add a subtitle track to the timeline")

    # PLAIN list form only. The {clipInfo} form crashed Resolve during the
    # probe and returns [None] for subtitle clips even when it does not.
    if not media_pool.AppendToTimeline([clip]):
        _fail("AppendToTimeline placed no subtitle cards")

    index = timeline.GetTrackCount("subtitle")
    timeline.SetTrackName("subtitle", index, DEFAULT_TRACK_NAME)
    return len(timeline.GetItemListInTrack("subtitle", index) or []), "placed"


# --------------------------------------------------------------------------
# placement B -- Text+ on a video track
# --------------------------------------------------------------------------


def place_textplus(
    project,
    timeline,
    subtitle_obj: dict,
    *,
    font: str = DEFAULT_FONT,
    allow_overwrite: bool = False,
) -> int:
    """Lay one Text+ per card, in chronological order.

    Chronological order is not a style choice: each insert overwrite-trims the
    previous card to end exactly where this one begins, which is the only way
    to get per-card durations out of an API with no SetDuration (D28).

    DANGER, and the reason for the guard below:
    InsertFusionTitleIntoTimeline always targets video track 1 and performs an
    OVERWRITE edit. On a timeline with footage on V1 it cuts the titles INTO
    that footage and destroys the edit -- observed directly. Neither lever
    helps: adding a video track does not redirect the insert (it still lands
    on V1), and locking V1 makes the insert fail outright rather than fall
    through to V2. There is no SetCurrentTrack in the API.

    So this refuses to run against a V1 that holds anything, unless the caller
    explicitly accepts the damage.
    """
    v1_items = timeline.GetItemListInTrack("video", 1) or []
    if v1_items and not allow_overwrite:
        _fail(
            f"refusing to place Text+: video track 1 holds {len(v1_items)} "
            f"item(s), and inserted titles OVERWRITE whatever is on V1 -- this "
            f"would cut into the edit. Resolve exposes no way to target another "
            f"track. Use --placement srt, or run this on a timeline whose V1 is "
            f"empty."
        )

    fps = _fps(project)
    start_frame = int(timeline.GetStartFrame())
    placed = 0

    for seg in subtitle_obj["segments"]:
        frame = start_frame + int(round(float(seg["start"]) * fps))
        timeline.SetCurrentTimecode(_timecode(frame, fps))

        item = timeline.InsertFusionTitleIntoTimeline("Text+")
        if not item:
            _fail(
                f"could not insert a Text+ for card {seg['id']}. The 'Text+' "
                f"title must be available in the Effects library."
            )

        comp = item.GetFusionCompByIndex(1)
        tool = comp.FindToolByID("TextPlus") if comp else None
        if tool is None:
            _fail(f"card {seg['id']}: inserted title has no TextPlus tool")

        tool.SetInput("StyledText", "\n".join(seg["lines"]))
        # Without an explicit Hebrew-capable font this renders as tofu (D29).
        tool.SetInput("Font", font)
        tool.SetInput("Style", "Regular")
        placed += 1

    return placed


# --------------------------------------------------------------------------
# the button
# --------------------------------------------------------------------------


def run(
    *,
    work_dir: Path | None = None,
    placement: str = "srt",
    font: str = DEFAULT_FONT,
    max_chars: int | None = None,
    engine: str = "ivrit_local",
    model: str | None = None,
    vocab: str | None = None,
    gap_ms: int = 0,
    reuse_audio: bool = True,
    passes: tuple[str, ...] = ("glossary",),
    review_model: str | None = None,
    timing_clip: bool = True,
    close_gaps_ms: int | None = None,
    review_track: bool = False,
    glossary_path: Path | str | None = None,
    strip_punct: bool = True,
) -> dict:
    """Transcribe the open timeline and put Hebrew subtitles back on it.

    This is the whole pipeline and the only copy of it. Anything that drives
    hebsub from Resolve -- the CLI, the panel -- calls this rather than
    re-assembling the stages, because the two did drift once and both findings
    cost real measurement time to notice (D41, D42).
    """
    from hebsub.export import (
        DEFAULT_CLOSE_GAPS_MS,
        TIMING_CLIP_TEXT,
        export,
        render_plain_srt,
    )
    from hebsub.proofread import Config as ProofreadConfig
    from hebsub.proofread import (
        load_glossary,
        proofread,
        resolve_disagreements,
        review_disagreements,
    )
    from hebsub.segment import Config, segment
    from hebsub.transcribe import transcribe

    resolve = connect()
    project, timeline = _current(resolve)

    name = timeline.GetName() or "timeline"
    work = Path(work_dir) if work_dir else (
        work_root() / work_dir_name(project.GetName() or "project", name)
    )
    work.mkdir(parents=True, exist_ok=True)

    print(f"{MODULE}: {resolve.GetProductName()} {resolve.GetVersionString()}")
    print(f"{MODULE}: project {project.GetName()!r}, timeline {name!r}")

    # 1. audio -- rendered only when the timeline has actually changed
    wav, how = prepare_audio(project, timeline, work, reuse=reuse_audio)
    if how == "rendered":
        print(f"{MODULE}: audio -> {wav.name}")

    # 2. transcribe
    print(f"{MODULE}: transcribing ({engine})...")
    raw = transcribe(wav, engine=engine, model=model, vocab=vocab)
    (work / "01_raw.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    words = sum(len(s["words"]) for s in raw["segments"])
    print(f"{MODULE}: {words} words")

    # 2b. second opinion -- a review list, never an automatic correction
    review: list[dict] = []
    resolved = 0
    if review_model:
        print(f"{MODULE}: second opinion ({review_model.split('/')[-1]})...")
        alternative = transcribe(wav, engine=engine, model=review_model)
        # Take the real word where the other model heard a non-word (D62).
        # Everything still in dispute afterwards is flagged, never changed.
        before_edits = len(raw.get("edits", []))
        raw = resolve_disagreements(
            raw, alternative, lexicon=_tiebreak_lexicon()
        )
        resolved = len(raw.get("edits", [])) - before_edits
        (work / "01_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        review = review_disagreements(raw, alternative)
        (work / "review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if resolved:
            print(
                f"{MODULE}: corrected {resolved} word(s) automatically -- the "
                f"other model heard a real Hebrew word where this one did not"
            )
            for edit in raw["edits"][before_edits:]:
                print(f"  {edit['before']} -> {edit['after']}")
        _report_review(review, work / "review.json")

    # 3. proofread
    corrected = raw
    passes = tuple(p for p in passes if p)
    if passes:
        print(f"{MODULE}: proofreading ({', '.join(passes)})...")
        adapter = None
        if "llm" in passes:
            from hebsub.llm import get_adapter

            adapter = get_adapter("masked_lm")
        glossary_file = Path(glossary_path) if glossary_path else None
        if glossary_file is None:
            default = Path.cwd() / "glossary.txt"
            glossary_file = default if default.exists() else None
        corrected = proofread(
            raw,
            cfg=ProofreadConfig(passes=passes),
            glossary=load_glossary(glossary_file),
            adapter=adapter,
        )
        (work / "02_proofread.json").write_text(
            json.dumps(corrected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        # Only what THIS pass added. resolve_disagreements may already have
        # put second_opinion edits on the transcript, and proofread carries
        # them through -- counting the total reported the glossary as having
        # made corrections it did not make.
        edits = len(corrected.get("edits", [])) - len(raw.get("edits", []))
        print(
            f"{MODULE}: {edits} correction(s)"
            + ("" if edits or glossary_file else
               " (no glossary.txt found next to the project)")
        )

    # 4. segment
    cfg = Config(max_chars_per_card=max_chars) if max_chars else Config()
    cards = segment(corrected, cfg=cfg)
    (work / "03_segmented.json").write_text(
        json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{MODULE}: {len(cards['segments'])} cards")

    # 5. export
    srt = work / "final.srt"
    # Short-form Hebrew subtitles are not punctuated -- Raz's own reference
    # carries 12 marks across 1445 words. segment still needs the punctuation
    # to find sentence and clause boundaries, so it survives the pipeline and
    # is dropped here, at display time (D35).
    # On by default here and only here: this is the path where the .srt is
    # dragged onto a track by hand, which is the problem the card solves.
    # `export`'s own CLI leaves it off, because a .srt going anywhere else
    # should not carry a "delete me" card.
    # A card must never appear before the video it belongs to, nor before the
    # speech it transcribes. Intersecting the two gives the earliest moment a
    # card is allowed to start.
    spans = intersect_spans(picture_spans(project, timeline), speech_spans(wav))
    report = export(
        cards, srt, gap_ms=gap_ms, strip_punct=strip_punct,
        onset_spans=spans,
        timing_clip=timing_clip,
        close_gaps_ms=(DEFAULT_CLOSE_GAPS_MS if close_gaps_ms is None
                       and not gap_ms else (close_gaps_ms or 0)),
        source=str(work / "03_segmented.json"),
    )
    print(f"{MODULE}: wrote {srt.name}")
    closed = report["stats"].get("closed_gaps", 0)
    if closed:
        print(f"{MODULE}: closed {closed} sub-{DEFAULT_CLOSE_GAPS_MS}ms gap(s) "
              f"so those cards touch; real pauses left alone")
    if report["stats"].get("timing_clip"):
        print(
            f"{MODULE}: the first card is a placeholder reading "
            f"'{TIMING_CLIP_TEXT}'.\n"
            f"{MODULE}: snap the clip to the start of the timeline, then "
            f"delete that card -- everything after it lands frame-exact."
        )

    # 6b. make the unresolved words reachable without leaving Resolve
    markers = 0
    review_clip = ""
    if review:
        try:
            markers = place_review_markers(project, timeline, review)
        except Exception as exc:  # noqa: BLE001 - navigation must not fail a run
            print(f"{MODULE}: could not place review markers ({exc})")
        if markers:
            print(
                f"{MODULE}: {markers} {REVIEW_MARKER_COLOR} marker(s) on the "
                f"timeline -- up/down arrow steps between them, and the "
                f"Markers panel lists each word with what the 2nd model heard"
            )
        strip = review_cards(cards, review) if review_track else []
        if strip:
            review_srt = work / f"{REVIEW_CLIP_NAME}.srt"
            review_srt.write_text(
                render_plain_srt(strip, timing_clip=timing_clip),
                encoding="utf-8",
            )
            try:
                review_clip, _ = _import_named(
                    project.GetMediaPool(), review_srt, REVIEW_CLIP_NAME
                )
                print(
                    f"{MODULE}: drop '{review_clip}' on a SECOND subtitle "
                    f"track to see each flagged word beside its alternative, "
                    f"in context; delete the track when done"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"{MODULE}: could not import the review track ({exc})")

    # 6. place
    status = "placed"
    if placement == "srt":
        placed, status = place_srt(project, timeline, srt)
        pool_name = status.split(":", 1)[1] if status.startswith("pool:") else ""
        if status == "placed":
            print(f"{MODULE}: placed {placed} cards on a subtitle track")
        else:
            print(
                f"{MODULE}: timeline is not empty, so the .srt was imported "
                f"into the media pool instead of appended.\n"
                f"{MODULE}: drag '{pool_name}' from the master bin "
                f"onto a "
                f"subtitle track -- it carries its own timings and will land "
                f"frame-exact.\n"
                f"{MODULE}: (appending here would place every card past the "
                f"end of the programme; Resolve exposes no way to position a "
                f"subtitle clip by script.)"
            )
    elif placement == "textplus":
        placed = place_textplus(project, timeline, cards, font=font)
        print(f"{MODULE}: placed {placed} Text+ cards (font: {font})")
    else:
        _fail(f"unknown placement {placement!r}; expected srt or textplus")

    _report_warnings(cards["meta"]["warnings"], work / "03_segmented.json")

    return {
        "work_dir": str(work),
        "srt": srt,
        "status": status,
        "cards": len(cards["segments"]),
        "words": words,
        "placed": placed,
        "audio": how,
        "edits": len(corrected.get("edits", [])),
        "review": review,
        "markers": markers,
        "review_clip": review_clip,
        "resolved": resolved,
        "warnings": cards["meta"]["warnings"],
        "stats": report["stats"],
    }


def _tiebreak_lexicon():
    """The word list the second-opinion rule judges against.

    Built here rather than left to `proofread`'s default so the learned words
    are found whatever the working directory is -- see `lexicon_path`. Returns
    None on any failure, which makes `resolve_disagreements` fall back to its
    own default rather than the run dying over a word list.
    """
    try:
        from hebsub.proofread import hebrew_lexicon, load_user_lexicon

        return hebrew_lexicon(extra=load_user_lexicon(lexicon_path()))
    except Exception as exc:  # noqa: BLE001 - a lexicon must not fail a run
        print(f"{MODULE}: could not load the lexicon ({exc}); "
              f"falling back to the default")
        return None


def _clock(seconds: float) -> str:
    """m:ss -- what you type into Resolve's timecode field to find the word."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _report_review(review: list[dict], detail_file: Path,
                   shown: int = 10) -> None:
    """Print the review list: specific words, with somewhere to look.

    Deliberately not a warning and deliberately not a correction. Two models
    disagreeing makes a word wrong 48.6% of the time -- far and away the best
    error signal measured (D48), and still under the >50% that would justify
    changing it automatically (D44). So it is printed for a human and applied
    to nothing.

    Capped, for the same reason the warning summary is capped: on a six-line
    panel log, printing everything is the same as printing nothing.
    """
    if not review:
        print(f"{MODULE}: second opinion agrees everywhere -- nothing to review")
        return
    print(
        f"{MODULE}: {len(review)} word(s) worth a look "
        f"(about half of these are genuinely wrong):"
    )
    for flag in review[:shown]:
        print(f"  {_clock(flag['start']):>6}  {flag['heard']}"
              f"   (2nd model heard: {flag['alternative']})")
    if len(review) > shown:
        print(f"  ...and {len(review) - shown} more, all of them in "
              f"{detail_file.name}")


def _report_warnings(warnings: list[dict], detail_file: Path,
                     examples: int = 3) -> None:
    """Summarise, then show a few. Never dump the lot.

    A 15-minute timeline produces ~75 warnings, and printing every one buries
    the line that matters -- which on a panel is every line, because the log is
    six lines tall. The full list is already on disk in the segmented JSON,
    keyed by `wid`, which is where you go when you actually want to chase one.
    """
    if not warnings:
        return
    counts: dict[str, int] = {}
    for warn in warnings:
        counts[warn["code"]] = counts.get(warn["code"], 0) + 1
    summary = ", ".join(f"{code} x{n}" for code, n in sorted(counts.items()))
    print(f"{MODULE}: warnings: {summary}")

    # One example per code beats three of the same code.
    shown, seen = 0, set()
    for warn in warnings:
        if warn["code"] in seen:
            continue
        seen.add(warn["code"])
        print(f"  e.g. [{warn['code']}] {warn['detail']}")
        shown += 1
        if shown >= examples:
            break
    remaining = len(warnings) - shown
    if remaining > 0:
        print(f"  ...and {remaining} more, all of them in {detail_file.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hebsub.host_resolve",
        description="One button: Hebrew subtitles onto the open Resolve timeline.",
    )
    parser.add_argument(
        "--placement", choices=("srt", "textplus"), default="srt",
        help="subtitle track (exact, light) or Text+ clips (styled, heavy)",
    )
    parser.add_argument("--font", default=DEFAULT_FONT)
    parser.add_argument("--max-chars", type=int, default=None)
    parser.add_argument("--engine", default="ivrit_local")
    parser.add_argument("--model", default=None)
    parser.add_argument("--vocab", default=None)
    parser.add_argument("--gap", type=int, default=0, metavar="MS")
    parser.add_argument(
        "--passes", default="glossary",
        help="proofread passes, comma separated; empty string disables them",
    )
    parser.add_argument(
        "--glossary", default=None,
        help="glossary file (default: ./glossary.txt when it exists)",
    )
    parser.add_argument(
        "--close-gaps", type=int, default=None, metavar="MS",
        help=(
            "close gaps at most MS long so the cards touch (default 200, "
            "measured from Raz's own files). 0 disables."
        ),
    )
    parser.add_argument(
        "--no-timing-clip", action="store_true",
        help=(
            "do not prepend the placeholder card. The card exists because "
            "Resolve cannot position a subtitle clip by script (D28), so the "
            "clip is dragged in by hand and needs something at frame zero to "
            "align to."
        ),
    )
    parser.add_argument(
        "--review", nargs="?", const=DEFAULT_REVIEW_MODEL, default=None,
        metavar="MODEL",
        help=(
            "run a second ASR model and list the words the two disagree on. "
            "Flags only, never corrects. Costs a second transcription pass; "
            "about half the flagged words are genuinely wrong."
        ),
    )
    parser.add_argument(
        "--keep-punct", action="store_true",
        help="keep punctuation in the .srt (short-form Hebrew drops it, D35)",
    )
    parser.add_argument("--work-dir", default=None)
    parser.add_argument(
        "--fresh-audio", action="store_true",
        help=(
            "always re-render the timeline audio. The default reuses it when "
            "the timeline fingerprint is unchanged, which cannot detect "
            "Fairlight mixer edits -- use this after changing levels."
        ),
    )
    parser.add_argument(
        "--review-track", action="store_true",
        help=(
            "also emit a 'HebSub Review' .srt for a second subtitle track, "
            "showing each flagged word above the alternative. Off by default: "
            "the panel's review list and the timeline markers cover the same "
            "ground without adding a clip to the media pool."
        ),
    )
    parser.add_argument(
        "--clear-markers", action="store_true",
        help=(
            "remove this tool's review markers from the current timeline and "
            "exit. Nothing tracks whether a flagged word has been fixed, so "
            "clearing them is a decision, not something inferred."
        ),
    )
    args = parser.parse_args(argv)

    if args.clear_markers:
        try:
            resolve = connect()
            _, timeline = _current(resolve)
            gone = clear_review_markers(timeline)
        except ResolveError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(f"{MODULE}: cleared {gone} review marker(s)")
        return 0

    try:
        result = run(
            work_dir=args.work_dir,
            placement=args.placement,
            font=args.font,
            max_chars=args.max_chars,
            engine=args.engine,
            model=args.model,
            vocab=args.vocab,
            gap_ms=args.gap,
            reuse_audio=not args.fresh_audio,
            passes=tuple(p for p in args.passes.split(",") if p),
            glossary_path=args.glossary,
            strip_punct=not args.keep_punct,
            review_model=args.review,
            timing_clip=not args.no_timing_clip,
            close_gaps_ms=args.close_gaps,
            review_track=args.review_track,
        )
    except ResolveError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the CLI is the last line of defence
        print(f"{MODULE}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"{MODULE}: done -- artifacts in {result['work_dir']}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
