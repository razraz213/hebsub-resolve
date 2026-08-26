"""Build the Windows installer, from a clean checkout to one .exe.

    python packaging/build.py

Four stages, each skippable once done so a re-run is cheap:

    1. a build virtualenv with the CORE requirements only. Not the ambient
       interpreter: whatever happens to be installed there ends up inside the
       bundle, and torch alone is 535 MB for a pass that is off by default.
    2. an LGPL ffmpeg. The usual Windows "full" builds enable x264/x265 and are
       GPL, which would conflict with this project's MIT licence.
    3. PyInstaller, onedir.
    4. Inno Setup, wrapping the directory into one installer.

The ASR model is deliberately NOT bundled: ~1.5 GB, someone else's weights,
and fetched on first use into the HuggingFace cache where a reinstall or a
second machine account can reuse it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VERSION = "4.0.0"

FFMPEG_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-lgpl-shared.zip"
)

ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def run(cmd, **kw):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    result = subprocess.run([str(c) for c in cmd], **kw)
    if result.returncode != 0:
        raise SystemExit(f"failed ({result.returncode}): {cmd[0]}")
    return result


def stage_venv(out: Path) -> Path:
    """A build environment with the core requirements and nothing else."""
    venv = out / "buildenv"
    python = venv / "Scripts" / "python.exe"
    if python.exists():
        print("1. build venv           already present")
        return python
    print("1. build venv           creating")
    run([sys.executable, "-m", "venv", venv])
    run([python, "-m", "pip", "install", "-q",
         "-r", REPO / "requirements.txt", "pyinstaller==6.19.0"])
    return python


def stage_ffmpeg(out: Path) -> Path:
    """ffmpeg.exe plus its shared DLLs and licence, from an LGPL build."""
    binaries = out / "ffmpeg" / "bin"
    if (binaries / "ffmpeg.exe").exists():
        print("2. ffmpeg (LGPL)        already present")
        return binaries
    print("2. ffmpeg (LGPL)        downloading")
    binaries.mkdir(parents=True, exist_ok=True)
    archive = out / "ffmpeg" / "ffmpeg.zip"
    with urlopen(FFMPEG_URL) as response, open(archive, "wb") as handle:
        shutil.copyfileobj(response, handle)

    with zipfile.ZipFile(archive) as z:
        # ffmpeg only -- ffprobe and ffplay are never invoked. The shared build
        # will not start without the DLLs beside it.
        wanted = [n for n in z.namelist() if "/bin/" in n
                  and (n.endswith("ffmpeg.exe") or n.endswith(".dll"))]
        for name in wanted:
            with z.open(name) as src, open(binaries / Path(name).name, "wb") as dst:
                shutil.copyfileobj(src, dst)
        # LGPL requires the licence to travel with the binary.
        for name in z.namelist():
            if name.endswith("/") or "LICENSE" not in name.upper():
                continue
            with z.open(name) as src, \
                    open(binaries / f"FFMPEG_{Path(name).name}", "wb") as dst:
                shutil.copyfileobj(src, dst)
    return binaries


def stage_freeze(python: Path, out: Path) -> Path:
    print("3. PyInstaller          freezing")
    env = dict(os.environ)
    run([python, "-m", "PyInstaller", "--noconfirm",
         "--distpath", out / "dist", "--workpath", out / "build",
         HERE / "hebsub.spec"], cwd=HERE, env=env)
    app = out / "dist" / "HebSub"
    exe = app / "HebSub.exe"
    if not exe.exists():
        raise SystemExit(f"PyInstaller produced no exe at {exe}")
    return app


def stage_installer(app: Path, ffmpeg: Path, out: Path) -> Path:
    iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None)
    if iscc is None:
        raise SystemExit(
            "Inno Setup not found. Install it with:\n"
            "    winget install --id JRSoftware.InnoSetup"
        )
    print("4. Inno Setup           compiling")
    run([iscc, f"/DHebSubSrc={app}", f"/DHebSubFfmpeg={ffmpeg}",
         f"/DHebSubVersion={VERSION}", f"/O{out}", HERE / "hebsub.iss"])
    setup = out / f"HebSub-{VERSION}-Setup.exe"
    if not setup.exists():
        raise SystemExit(f"Inno Setup produced no installer at {setup}")
    return setup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO.parent / "hebsub-build"),
                        help="scratch directory for the build (default: ../hebsub-build)")
    parser.add_argument("--skip-installer", action="store_true",
                        help="stop after PyInstaller, do not compile the .exe")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"building HebSub {VERSION} in {out}\n")

    python = stage_venv(out)
    ffmpeg = stage_ffmpeg(out)
    app = stage_freeze(python, out)

    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"\n   bundle: {app}  ({size / 1e6:.0f} MB)")

    if args.skip_installer:
        return 0

    setup = stage_installer(app, ffmpeg, out)
    print(f"\ndone: {setup}  ({setup.stat().st_size / 1e6:.0f} MB)")
    print("\nTest it with:")
    print(f"   {setup}")
    print(f"   \"%LOCALAPPDATA%\\Programs\\HebSub\\HebSub.exe\" --selftest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
