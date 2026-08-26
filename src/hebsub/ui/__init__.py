"""User interface for hebsub.

Deliberately a thin shell. Everything here calls `hebsub.host_resolve.run()`
and does no pipeline work of its own -- no ASR, no segmentation, no placement
logic. If a behaviour needs changing, it changes in the module that owns it,
not here.

Two entry points:

  hebsub.ui.app          the panel, run it directly
  hebsub.ui.install      drops a launcher into Resolve's Scripts menu

The panel runs as its own process rather than inside Resolve's script host.
That is on purpose: Resolve's embedded Python is not guaranteed to have
faster-whisper, torch or the CUDA runtime on its path, while the interpreter
that installed them obviously does. Talking to Resolve over its scripting API
from outside is the same thing the CLI already does.
"""

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    from hebsub.ui.app import main as _main

    return _main(argv)
