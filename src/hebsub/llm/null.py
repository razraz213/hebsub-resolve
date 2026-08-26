"""The adapter that proposes nothing.

The default, and deliberately so. `proofread` must be runnable -- and its
invariants testable -- without any model present, and a pass that is not
configured must be a no-op rather than an error.

It is also the control in every ablation: `--passes glossary` against
`--passes glossary,llm` is only a fair comparison if the llm pass with no
adapter changes precisely nothing.
"""

from __future__ import annotations

__all__ = ["NullAdapter"]


class NullAdapter:
    name = "null"
    version = "0"

    def propose(self, request: dict) -> list[dict]:  # noqa: ARG002
        return []
