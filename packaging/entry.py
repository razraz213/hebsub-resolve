"""Entry point for the frozen Windows build.

Everything here exists because a frozen app is not a Python installation, and
three of hebsub's assumptions quietly depend on being one.

  1. `transcribe.extract_audio` calls `shutil.which("ffmpeg")`. A user who
     double-clicks an installer has not put ffmpeg on PATH and should not have
     to. The bundle carries its own, and this prepends the bundle directory to
     PATH so `which` finds it -- no change to the pipeline, which is right,
     because "where is ffmpeg" is a packaging question and not a transcribe
     question.

  2. `work_root()` is `Path.cwd() / "work"`. Launched from a Start Menu
     shortcut the working directory is wherever Windows felt like, and for an
     installed app it may not even be writable. It is pinned to a per-user
     documents folder instead.

  3. `lexicon_path()` resolves from the package, which inside a bundle is a
     read-only temp directory. The learned lexicon has to live beside the work
     folder, where it survives an upgrade.

None of this belongs in the modules themselves: the CLI and a source checkout
should keep behaving exactly as they do today. This is the installer's job.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "HebSub"


def _bundle_dir() -> Path:
    """Where the frozen app's files live, or the repo when running from source."""
    if getattr(sys, "frozen", False):
        # onedir: the exe's own folder. onefile: PyInstaller's extraction dir.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def _user_data() -> Path:
    """Somewhere writable that survives reinstalling the app."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare() -> Path:
    """Wire the bundle into the environment the pipeline expects."""
    bundle = _bundle_dir()

    # 1. bundled ffmpeg, discoverable by shutil.which
    for candidate in (bundle / "ffmpeg", bundle):
        if (candidate / "ffmpeg.exe").exists():
            os.environ["PATH"] = f"{candidate}{os.pathsep}{os.environ.get('PATH', '')}"
            break

    # 2 and 3. a writable home for artifacts and the learned lexicon
    data = _user_data()
    os.chdir(data)                       # work_root() is cwd / "work"
    (data / "work").mkdir(exist_ok=True)
    return data


FROZEN_LAUNCHER = '''"""hebsub -- opens the Hebrew subtitle panel.

Written by the HebSub installer. Edit the installer, not this.

Resolve runs menu scripts in its own embedded Python, which has none of the
pipeline's dependencies. This starts the bundled app as a separate process
instead, so nothing here can fail on an import.
"""

import subprocess

APP = r"{app}"


def main():
    try:
        subprocess.Popen([APP])
        print("hebsub: panel launched")
    except Exception as exc:
        print("hebsub: could not launch the panel:", exc)
        print("hebsub: expected it at", APP)


main()
'''


def _menu_target():
    """Where Resolve looks for menu scripts.

    Reuses `hebsub.ui.install.script_dirs` rather than repeating the paths --
    there is one right answer per platform and it should have one home.
    """
    from hebsub.ui.install import MENU_NAME, script_dirs

    for directory in script_dirs():
        if directory.is_dir():
            return directory / MENU_NAME
    directory = script_dirs()[0]
    directory.mkdir(parents=True, exist_ok=True)
    return directory / MENU_NAME


def install_menu() -> int:
    """Put a HebSub entry in Resolve's Workflow > Scripts menu."""
    app = Path(sys.executable)
    target = _menu_target()
    target.write_text(FROZEN_LAUNCHER.format(app=app), encoding="utf-8")
    print(f"installed: {target}")
    return 0


def remove_menu() -> int:
    target = _menu_target()
    if target.exists():
        target.unlink()
        print(f"removed: {target}")
    else:
        print("nothing to remove")
    return 0


def selftest(data: Path) -> int:
    """Check the bundle from inside itself, and write what it found.

    A windowed build has no console, so a failure at startup is invisible --
    the app simply does not appear. This makes the bundle able to answer "what
    is wrong" without a developer present, which is the only diagnostic a
    user who double-clicked an installer can actually run.
    """
    import shutil
    import traceback

    report, ok = [], True

    def check(name, fn):
        nonlocal ok
        try:
            report.append(f"OK    {name}: {fn()}")
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            ok = False
            report.append(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            report.append(traceback.format_exc())

    report.append(f"frozen      : {getattr(sys, 'frozen', False)}")
    report.append(f"bundle dir  : {_bundle_dir()}")
    report.append(f"data dir    : {data}")
    report.append(f"working dir : {Path.cwd()}")
    report.append("")

    check("ffmpeg on PATH", lambda: shutil.which("ffmpeg") or "NOT FOUND")
    check("tkinter", lambda: __import__("tkinter").TkVersion)
    check("ctranslate2", lambda: __import__("ctranslate2").__version__)
    check("faster_whisper", lambda: __import__("faster_whisper").__version__)
    check("onnxruntime", lambda: __import__("onnxruntime").__version__)
    check("av", lambda: __import__("av").__version__)
    check("hebsub.export", lambda: __import__(
        "hebsub.export", fromlist=["render_srt"]).__name__)
    check("hebsub.host_resolve", lambda: __import__(
        "hebsub.host_resolve", fromlist=["run"]).__name__)
    check("hebsub.ui.app", lambda: __import__(
        "hebsub.ui.app", fromlist=["Panel"]).__name__)

    def lexicon():
        from hebsub.proofread import hebrew_lexicon
        return f"{len(hebrew_lexicon())} entries"
    check("hebrew lexicon (transformers)", lexicon)

    def resolve():
        from hebsub import host_resolve
        r = host_resolve.connect()
        return f"{r.GetProductName()} {r.GetVersionString()}"
    # Not a failure: a user may well run this before opening Resolve. It is
    # reported either way, but only the bundle's own health sets the exit code.
    try:
        report.append(f"OK    DaVinci Resolve: {resolve()}")
    except Exception as exc:  # noqa: BLE001
        report.append(f"WARN  DaVinci Resolve: {exc}")
        report.append("      (open Resolve, and enable Preferences > System >")
        report.append("       General > 'External scripting using' = Local)")

    text = "\n".join(report)
    out = data / "selftest.txt"
    out.write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


def main() -> int:
    data = prepare()

    if "--selftest" in sys.argv:
        return selftest(data)
    if "--install-menu" in sys.argv:
        return install_menu()
    if "--remove-menu" in sys.argv:
        return remove_menu()

    # Imported only after prepare(), so the panel sees the finished
    # environment rather than racing it.
    from hebsub import host_resolve
    from hebsub.ui import app

    # lexicon_path() points inside the read-only bundle when frozen. Redirect
    # it to the writable data folder, creating the file so the first LEARN
    # press has somewhere to append.
    if getattr(sys, "frozen", False):
        lexicon = data / "lexicon.txt"
        lexicon.touch(exist_ok=True)
        host_resolve.lexicon_path = lambda: lexicon

    app.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
