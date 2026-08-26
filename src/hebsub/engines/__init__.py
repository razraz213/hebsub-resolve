"""ASR engine adapters.

One file per engine. An adapter's only job is to run its engine and hand back
raw, engine-shaped output; mapping that onto the Transcript contract is
`transcribe`'s job, not the adapter's. Keeping the split here is what lets a
new engine be added without touching anything outside this package.

See docs/modules/transcribe.md, "Engine adapter interface".
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["Engine", "EngineError", "get_engine", "available_engines"]


class EngineError(Exception):
    """Raised when an engine cannot produce a usable result."""


@runtime_checkable
class Engine(Protocol):
    """What every adapter must expose."""

    name: str
    version: str

    def transcribe(self, wav_path: str, vocab: list[str] | None) -> dict:
        """Return raw engine output for a 16 kHz mono WAV."""
        ...


def available_engines() -> tuple[str, ...]:
    return ("ivrit_local",)


def get_engine(name: str, **kwargs) -> Engine:
    """Construct an adapter by name.

    Imports lazily so that a missing optional dependency for one engine never
    stops another engine from being used.
    """
    if name == "ivrit_local":
        from hebsub.engines.ivrit_local import IvritLocalEngine

        return IvritLocalEngine(**kwargs)
    raise EngineError(
        f"transcribe: unknown engine {name!r}; "
        f"available: {', '.join(available_engines())}"
    )
