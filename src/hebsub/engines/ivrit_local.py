"""The default engine: faster-whisper running an ivrit.ai Hebrew model.

Free, offline, no API key. See docs/modules/transcribe.md, "ivrit_local".

Two things here are not obvious and both are load-bearing:

1. The language token is pinned to "he". The ivrit.ai models have degraded
   language detection by design; auto-detect misfires on them.
2. On Windows, CTranslate2 resolves cublas64_12.dll and cudnn64_9.dll by bare
   name via LoadLibrary, which ignores os.add_dll_directory(). The pip
   nvidia-*-cu12 wheels put those DLLs somewhere Windows will not search, so
   they have to be mapped into the process by full path before ctranslate2
   loads -- otherwise it raises "Library cublas64_12.dll is not found" while
   the file is sitting right there.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from hebsub.engines import EngineError

__all__ = [
    "IvritLocalEngine",
    "DEFAULT_MODEL",
    "bootstrap_cuda_dlls",
    "disable_hf_symlinks",
]

DEFAULT_MODEL = "ivrit-ai/whisper-large-v3-turbo-ct2"

# Mapped by full path before ctranslate2 loads. Order matters: cublas depends
# on cublasLt, and cudnn64_9 is a dispatcher that pulls in its siblings.
_PRELOAD = (
    ("cublas", "cublasLt64_12.dll"),
    ("cublas", "cublas64_12.dll"),
    ("cudnn", "cudnn64_9.dll"),
)

_cuda_bootstrapped = False


def bootstrap_cuda_dlls() -> list[str]:
    """Make the pip-installed CUDA runtime DLLs loadable. Idempotent, no-op off Windows."""
    global _cuda_bootstrapped
    if _cuda_bootstrapped or os.name != "nt":
        return []
    import ctypes
    import sysconfig

    added: list[str] = []
    base = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    if not base.is_dir():
        _cuda_bootstrapped = True
        return added

    for sub in sorted(base.iterdir()):
        binn = sub / "bin"
        if binn.is_dir() and any(binn.glob("*.dll")):
            os.add_dll_directory(str(binn))
            os.environ["PATH"] = str(binn) + os.pathsep + os.environ.get("PATH", "")
            added.append(str(binn))

    for package, dll_name in _PRELOAD:
        dll = base / package / "bin" / dll_name
        if dll.exists():
            try:
                ctypes.WinDLL(str(dll))
            except OSError as exc:  # pragma: no cover - hardware dependent
                print(
                    f"transcribe: could not preload {dll_name}: {exc}",
                    file=sys.stderr,
                )

    _cuda_bootstrapped = True
    return added


def disable_hf_symlinks() -> None:
    """Make huggingface_hub copy files into its cache instead of symlinking.

    Creating a symlink on Windows needs either Developer Mode or an elevated
    process. Neither is a reasonable thing to require of a video editor, and
    without one the download dies partway through with

        [WinError 1314] A required privilege is not held by the client

    leaving a snapshot directory that is missing files -- so the *next* run
    fails too, on a model that looks downloaded. Copying costs some disk and
    works everywhere.

    Must run before huggingface_hub is imported: the flag is read into module
    constants at import time, so setting it afterwards has no effect. That is
    why this sits here rather than anywhere more obvious, and why
    `faster_whisper` is imported inside `_load` rather than at module scope.
    """
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


# Substrings CTranslate2 and the CUDA loader use when the runtime libraries
# are absent. Matched on text because neither raises a typed error -- both
# surface a bare RuntimeError.
_CUDA_LIB_MARKERS = (
    "cublas", "cudnn", "cudart", "cuda runtime", "nvrtc",
    "is not found or cannot be loaded", "no kernel image",
)


def _is_missing_cuda_library(exc: Exception) -> bool:
    """Is this failure 'the GPU is there but its libraries are not'?"""
    text = str(exc).lower()
    return any(marker in text for marker in _CUDA_LIB_MARKERS)


class IvritLocalEngine:
    """faster-whisper adapter. Returns raw engine output, unmapped."""

    name = "ivrit_local"

    def __init__(
        self,
        model: str | None = None,
        device: str = "auto",
        compute_type: str | None = None,
    ) -> None:
        self.model_name = model or DEFAULT_MODEL
        self.version = self.model_name
        self._requested_device = device
        self._requested_compute = compute_type
        self._model = None
        self.device = device
        self.compute_type = compute_type or ""

    def _load(self):
        if self._model is not None:
            return self._model

        bootstrap_cuda_dlls()
        disable_hf_symlinks()   # before faster_whisper pulls in huggingface_hub
        try:
            import ctranslate2
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise EngineError(
                f"transcribe: ivrit_local needs faster-whisper installed "
                f"({exc}); pip install faster-whisper"
            ) from exc

        device = self._requested_device
        if device == "auto":
            try:
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"

        compute = self._requested_compute or (
            "float16" if device == "cuda" else "int8"
        )

        try:
            self._model = WhisperModel(
                self.model_name, device=device, compute_type=compute
            )
        except Exception as exc:
            # A PRESENT GPU IS NOT A USABLE GPU.
            #
            # `get_cuda_device_count()` asks the DRIVER how many devices
            # exist. It says 1 on any machine with an NVIDIA card, whether or
            # not the CUDA runtime libraries are installed -- and CTranslate2
            # only discovers they are missing when it tries to load one:
            #
            #   Library cublas64_12.dll is not found or cannot be loaded
            #
            # That is exactly the frozen Windows build, which deliberately
            # does not ship the 2 GB CUDA runtime. It picked cuda, then died.
            # Falling back is right for every caller: a slower transcript beats
            # no transcript, and the CPU path is 4.9x realtime against 11.7x.
            if device == "cuda" and _is_missing_cuda_library(exc):
                fallback = self._requested_compute or "int8"
                print(
                    f"transcribe: CUDA is present but its runtime libraries are "
                    f"not ({exc}); falling back to CPU. Roughly 2.4x slower, "
                    f"same transcript.",
                    flush=True,
                )
                try:
                    self._model = WhisperModel(
                        self.model_name, device="cpu", compute_type=fallback
                    )
                    self.device, self.compute_type = "cpu", fallback
                    return self._model
                except Exception as cpu_exc:
                    raise EngineError(
                        f"transcribe: could not load model "
                        f"{self.model_name!r} on cuda ({exc}) and the CPU "
                        f"fallback also failed: {cpu_exc}"
                    ) from cpu_exc

            hint = ""
            if "1314" in str(exc) or "privilege" in str(exc).lower():
                # The cache is now half-written; a retry alone will not fix it.
                hint = (
                    " -- Windows refused to create a symlink in the model "
                    "cache. Delete the model's folder under "
                    "~/.cache/huggingface/hub and run again; hebsub now asks "
                    "huggingface to copy instead of symlink, so the retry "
                    "will succeed."
                )
            raise EngineError(
                f"transcribe: could not load model {self.model_name!r} on "
                f"{device} ({compute}): {exc}{hint}"
            ) from exc

        self.device, self.compute_type = device, compute
        return self._model

    def transcribe(self, wav_path: str, vocab: list[str] | None = None) -> dict:
        model = self._load()

        # Only the correct spellings are ever fed to the engine as hints --
        # transcribe strips mapping left-hand sides before we see them.
        initial_prompt = " ".join(vocab) if vocab else None

        def run(active):
            segments, info = active.transcribe(
                wav_path,
                language="he",          # never auto-detect: see module docstring
                word_timestamps=True,
                vad_filter=True,
                beam_size=5,
                temperature=0.0,        # single value: no sampling fallback,
                                        # so the same input gives the same output
                condition_on_previous_text=False,
                initial_prompt=initial_prompt,
            )
            return list(segments), info  # generator: force the work inside

        try:
            segments, info = run(model)
        except Exception as exc:
            # THE CUDA FAILURE LANDS HERE, NOT AT MODEL CONSTRUCTION.
            #
            # `WhisperModel(device="cuda")` succeeds on any machine with an
            # NVIDIA driver, because CTranslate2 loads cuBLAS lazily. The
            # missing library only surfaces when the encoder first runs --
            # deep inside this generator, at `model.encode(features)`:
            #
            #   Library cublas64_12.dll is not found or cannot be loaded
            #
            # Wrapping the constructor therefore catches nothing, which is
            # exactly the mistake that shipped a broken installer. The retry
            # has to live where the work happens.
            if self.device == "cuda" and _is_missing_cuda_library(exc):
                print(
                    f"transcribe: CUDA is present but its runtime libraries "
                    f"are not ({exc}); retrying on CPU. Roughly 2.4x slower, "
                    f"same transcript.",
                    flush=True,
                )
                self._model = None
                self._requested_device = "cpu"
                self._requested_compute = self._requested_compute or "int8"
                try:
                    segments, info = run(self._load())
                except Exception as cpu_exc:
                    raise EngineError(
                        f"transcribe: ivrit_local failed on {wav_path} on "
                        f"cuda ({exc}) and the CPU retry also failed: "
                        f"{cpu_exc}"
                    ) from cpu_exc
            else:
                raise EngineError(
                    f"transcribe: ivrit_local failed on {wav_path}: {exc}"
                ) from exc

        return {
            "engine": self.name,
            "version": self.version,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": info.language,
            "language_probability": info.language_probability,
            "duration": info.duration,
            "segments": [
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "words": [
                        {
                            "w": word.word,
                            "start": word.start,
                            "end": word.end,
                            "conf": word.probability,
                        }
                        for word in (seg.words or [])
                    ],
                }
                for seg in segments
            ],
        }
