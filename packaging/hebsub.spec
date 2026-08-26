# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build.

onedir, not onefile, and deliberately. The bundle is ~600 MB; a onefile exe
re-extracts all of it to a temp directory on EVERY launch, which is tens of
seconds before a window appears and looks exactly like a hang. The installer
turns the directory into a single download anyway, so onefile buys nothing and
costs the first impression.

`collect_all` is used for the three packages that carry native libraries or
resolve imports at runtime -- PyInstaller's static analysis finds neither.
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# ffmpeg. `transcribe.extract_audio` shells out to it -- Resolve's "Audio
# Only" render gives us whatever container it likes, and Whisper wants mono
# 16 kHz PCM. A user who double-clicked an installer has not put ffmpeg on
# PATH, so it ships inside and entry.py prepends this folder.
#
# An LGPL build, deliberately: the usual Windows "full" builds enable x264 and
# x265 and are GPL, which would conflict with this project's MIT licence. The
# licence text ships beside the binary, which LGPL requires.
# ffmpeg is deliberately NOT listed here. Both `binaries` and `datas` end up
# in PyInstaller's binary handling -- it reclassifies DLLs found in datas --
# and that walks ffmpeg.exe's import table and hoists every dependent DLL to
# the bundle root. The result was avcodec, avformat, avfilter and four more
# sitting BOTH in ffmpeg/ and at the top level, byte-identical: 134 MB of
# duplication in a 591 MB bundle, measured.
#
# ffmpeg.exe is a self-contained program that finds its DLLs beside itself,
# not a library this app links against. So the installer copies it in
# (see hebsub.iss), PyInstaller never sees it, and there is exactly one copy.

# ctranslate2 and onnxruntime ship compiled DLLs beside their Python modules;
# av carries a whole FFmpeg. Static analysis sees none of it.
for package in ("ctranslate2", "onnxruntime", "av", "faster_whisper"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# transformers is imported for ONE thing -- AutoTokenizer, to build the
# DictaBERT word list. Collecting the whole package pulls in every model
# architecture it knows about, so take only the tokenizer machinery.
hiddenimports += [
    "transformers",
    "transformers.models.auto",
    "transformers.models.auto.tokenization_auto",
    "transformers.models.bert",
    "transformers.models.bert.tokenization_bert",
]
hiddenimports += collect_submodules("tokenizers")

# The panel's own package.
hiddenimports += collect_submodules("hebsub")

a = Analysis(
    ["entry.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # torch is only needed by the masked_lm pass, which is off by default
        # and measured at 0.12pp. 535 MB is not worth shipping for that.
        "torch", "torchvision", "torchaudio",
        # Resolve provides this at runtime from its own install directory;
        # host_resolve.connect() puts it on sys.path. It cannot be bundled and
        # must not be looked for at build time.
        "DaVinciResolveScript",
        # Never used, and each drags in a large dependency tree.
        "matplotlib", "scipy", "pandas", "IPython", "notebook",
        "tensorflow", "jax", "flax",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HebSub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # a GUI app; a console window behind it is noise
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="HebSub",
)
