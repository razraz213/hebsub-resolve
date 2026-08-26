"""Analog Instrument, as far as Tk will carry it.

Raz's design system is a vintage electronics faceplate: cream ground, teal as
the "on" colour, rust only as a signal, engraved mono labels, corners no
rounder than 2px. Tk gives us flat fills, borders and fonts, so the palette
and the label treatment come across; the screw corners and grille are drawn on
a Canvas where one is available.

Fonts degrade rather than fail. IBM Plex is the system's choice but is not on
every machine, so each role resolves to the first family actually installed.
"""

from __future__ import annotations

__all__ = ["Palette", "fonts", "SCREW", "GRILLE"]


class Palette:
    cream = "#FFEDB7"        # page background
    paper = "#FBF3DE"        # panel surface
    teal = "#4ABA94"         # primary, the "on" state
    teal_deep = "#357C63"    # pressed
    rust = "#D0542D"         # signal, sparingly
    rust_deep = "#A63E1F"
    taupe = "#685B53"        # secondary text, engraved labels
    charcoal = "#2B3A39"     # body text, borders
    hairline = "#D5CDBE"     # charcoal at ~18% over paper, flattened for Tk
    disabled = "#B3A99C"


SCREW = "#CFC5B0"
GRILLE = "#E6DCC4"

# The design system names the Hebrew swap directly: Unbounded -> Rubik for
# display, IBM Plex Sans -> IBM Plex Sans Hebrew for body, and no uppercasing
# or mono letter-spacing, neither of which Hebrew has. IBM Plex Mono carries
# no Hebrew at all, so Hebrew "engraved" labels fall back to the Hebrew body
# face at a small size rather than silently rendering as boxes.
_STACKS = {
    "display": ["Unbounded", "Rubik", "Segoe UI Semibold", "Arial Black", "Arial"],
    "body": ["IBM Plex Sans", "Segoe UI", "Arial"],
    "mono": ["IBM Plex Mono", "Consolas", "Courier New"],
    "hebrew": ["IBM Plex Sans Hebrew", "Rubik", "Segoe UI", "Arial"],
    "hebrew_display": ["Rubik", "IBM Plex Sans Hebrew", "Segoe UI", "Arial"],
    # The log mixes Hebrew with file names and numbers, so it cannot be a
    # Latin-only mono. A Hebrew face that also has clear digits wins over
    # column alignment we were never getting in Hebrew anyway.
    "hebrew_log": ["IBM Plex Sans Hebrew", "Segoe UI", "Arial"],
}


def fonts(root) -> dict[str, tuple]:
    """Resolve each role to a family that is actually installed."""
    from tkinter import font as tkfont

    available = {name.lower() for name in tkfont.families(root)}

    def pick(role: str) -> str:
        for family in _STACKS[role]:
            if family.lower() in available:
                return family
        return _STACKS[role][-1]

    display, body, mono, hebrew = (
        pick("display"), pick("body"), pick("mono"), pick("hebrew")
    )
    he_display, he_log = pick("hebrew_display"), pick("hebrew_log")
    return {
        "wordmark": (display, 22, "bold"),
        "heading": (display, 13, "bold"),
        "body": (body, 10),
        "body_bold": (body, 10, "bold"),
        "button": (body, 11, "bold"),
        # engraved panel labels: small mono caps
        "label": (mono, 8),
        "value": (mono, 9),
        "log": (mono, 9),
        "hebrew": (hebrew, 11),
        # Hebrew roles. Sized a point up from their Latin equivalents:
        # Hebrew has no ascenders or descenders to read shape from, so the
        # same nominal size reads smaller.
        "he_label": (he_display, 9),
        "he_body": (hebrew, 11),
        "he_value": (hebrew, 11, "bold"),
        "he_button": (he_display, 13, "bold"),
        "he_word": (hebrew, 13),
        "he_log": (he_log, 10),
        "he_hint": (hebrew, 9),
    }
