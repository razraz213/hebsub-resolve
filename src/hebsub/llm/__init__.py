"""LLM adapters for the proofread pass.

Same pattern as the ASR engines: one file per adapter, and the adapter's only
job is to propose changes. Deciding whether a proposal is safe to apply is
`proofread`'s job and happens behind guards the adapter cannot influence --
eligibility, edit budget, edit distance. An adapter is never trusted.

A proposal is one of two shapes, and never prose:

    {"wid": 47, "replacement": "צריך"}   -- 1:1 substitution, reason `llm`
    {"wid": 47, "append": "."}           -- punctuation, reason `punctuation`

`append` preserves word count by construction: the mark joins an existing
word, it never becomes a word of its own.

See docs/modules/proofread.md.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["LLMAdapter", "LLMError", "get_adapter", "available_adapters"]


class LLMError(Exception):
    """Raised when an adapter cannot be constructed or reached."""


@runtime_checkable
class LLMAdapter(Protocol):
    """What every adapter must expose."""

    name: str
    version: str

    def propose(self, request: dict) -> list[dict]:
        """Return proposals for one segment.

        `request` carries the segment, up to two segments of context on each
        side, the glossary terms, and each word's confidence. Returning an
        empty list is always valid and always safe.
        """
        ...


def available_adapters() -> tuple[str, ...]:
    return ("null", "masked_lm")


def get_adapter(name: str, **kwargs) -> LLMAdapter:
    """Construct an adapter by name, importing lazily.

    Lazy so that a missing optional dependency for one adapter never stops
    another from being used -- and so that `--passes ''` costs nothing.
    """
    if name in ("null", "", None):
        from hebsub.llm.null import NullAdapter

        return NullAdapter(**kwargs)
    if name == "masked_lm":
        from hebsub.llm.masked_lm import MaskedLMAdapter

        return MaskedLMAdapter(**kwargs)
    raise LLMError(
        f"proofread: unknown llm adapter {name!r}; "
        f"available: {', '.join(available_adapters())}"
    )
