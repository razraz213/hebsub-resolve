"""Hebrew for the panel: the string table, and the pipeline's log translated.

The panel is Hebrew because Raz is. Nothing below this file knows that --
`host_resolve` and the CLI still speak English, and their log lines are
translated on their way into the panel's log. That is deliberate:
`docs/modules/ui.md` says presentation belongs on this side of the boundary,
and translating upstream would change the CLI, the module specs and the tests
that assert on those lines. Cosmetic means cosmetic.

WHAT THE BIDI PROBE FOUND (Tk 8.6.15 on Windows 11, measured, not assumed)

  1. Tk renders each Hebrew run correctly. Given "ABC" + aleph-bet-gimel it
     draws A B C then GIMEL BET ALEPH -- the Hebrew run is reversed, which is
     right.
  2. But the base direction is always LTR. Plain "aleph-bet-gimel ABC" puts
     the Hebrew on the LEFT and ABC on the right, which is backwards for a
     Hebrew line.
  3. RLM (U+200F) does not fix it. Neither does RLI/PDI (U+2066/U+2069) --
     those render as visible boxes, because Tk has no glyph for them.
  4. RLE ... PDF (U+202B ... U+202C) DOES fix it: ABC moves to the left and
     the Hebrew to the right, which is correct RTL base direction.

So every Hebrew string that a widget displays goes through `rtl()`. A string
of pure Hebrew does not strictly need it; passing everything through one
function is what stops the one line that mixes in a file name from being the
line that renders backwards.

`Text.bbox()` still reports positions in LOGICAL order -- index 0 of a Hebrew
line reads back as the leftmost x. That affects caret and selection, not
drawing. The log is read-only, so it does not matter here. Do not "fix" it.
"""

from __future__ import annotations

import re

__all__ = ["rtl", "T", "translate_log", "RLE", "PDF"]

# Right-to-left embedding, and the pop that closes it.
RLE = "‫"
PDF = "‬"


def rtl(text: str) -> str:
    """Force RTL base direction on a line the panel is about to display.

    Idempotent, and a no-op on the empty string so blank log lines stay blank
    rather than becoming two invisible control characters.
    """
    if not text or text.startswith(RLE):
        return text
    return f"{RLE}{text}{PDF}"


# --------------------------------------------------------------------------
# the panel's own chrome
# --------------------------------------------------------------------------

T = {
    "title": "hebsub — כתוביות "
             "בעברית לדה "
             "וינצ'י ריזולב",
    "wordmark_sub": "כתוביות בעברית "
                    "· דה וינצ'י ריזולב",

    # Panel headings, the word only. The "01 ·" part is a separate widget --
    # see Panel._panel for why a number beside Hebrew cannot live in the same
    # string.
    "panel_connection": "חיבור",
    "panel_settings": "הגדרות",
    "panel_log": "יומן",
    "panel_review": "מילים לבדיקה",

    # connection panel
    "project": "פרויקט",
    "timeline": "טיימליין",
    "reconnect": "חבר מחדש",
    "not_connected": "לא מחובר",
    "none": "—",

    # settings
    "card_width": "רוחב כרטיס",
    "chars": "תווים",
    "width_hint": "נמדד כמיטבי "
                  "מול העריכות "
                  "שלך",
    "opt_punct": "להשמיט סימני "
                 "פיסוק (סגנון "
                 "רילס)",
    "opt_llm": "מעבר הגהה במודל "
               "שפה",
    "opt_review": "חוות דעת שנייה "
                  "— רשימת מילים "
                  "לבדיקה (איטי "
                  "יותר)",
    "opt_timing": "כרטיס תזמון "
                  "בהתחלה (לגרור "
                  "להתחלה, אז למחוק)",

    # the button
    "run": "תמלל את הטיימליין",
    "running": "עובד…",

    # status line
    "ready": "מוכן.",
    "preparing": "מכין את האודיו "
                 "של הטיימליין…",
    "open_timeline": "פתח טיימליין "
                     "בריזולב, ואז "
                     "לחץ על חבר מחדש.",
    "failed": "נכשל — ראה את "
              "היומן.",

    # footer
    "open_folder": "פתח תיקיית "
                   "פלט",
    "clear_flags": "נקה סימונים",
    "learn": "למד מהתיקונים",
    "learn_added": "מילים נלמדו "
                   "מהתיקונים שלך",
    "learn_none": "לא נמצאו מילים "
                  "חדשות ללמוד",
    "learn_total": "סך הכל במילון:",
    "learn_no_run": "אין ריצה קודמת "
                    "לטיימליין הזה — "
                    "הרץ תמלול קודם",
    "learn_no_match": "ערוץ הכתוביות "
                      "לא תואם לריצה "
                      "האחרונה — לא "
                      "נלמד כלום",
    "footer_hint": "גרור את הקליפ "
                   "שיובא לערוץ "
                   "כתוביות — התזמון "
                   "שמור בתוכו",

    # review rows
    "copy": "העתק",
    "copied": "הועתק",
    "review_hint": "לחץ על שורה "
                   "כדי לקפוץ "
                   "למילה בטיימליין",

    # what the panel itself writes into the log (the pipeline's own lines go
    # through translate_log instead)
    "done": "סיים",
    "cards_placed": "כרטיסים הונחו "
                    "על ערוץ כתוביות",
    "cards_on_timeline": "כרטיסים על "
                         "הטיימליין.",
    "imported_as": "יובא למאגר "
                   "הראשי בשם",
    "drag_hint": "גרור אותו לערוץ "
                 "כתוביות — התזמון "
                 "שמור בתוכו והוא "
                 "ינחת מדויק לפריים",
    "cards_drag": "כרטיסים. גרור "
                  "מהמאגר הראשי את",
    "markers_placed": "סימונים על "
                      "הטיימליין — "
                      "חיצי מעלה/מטה "
                      "מדלגים ביניהם",
    "review_clip_hint": "במאגר הראשי; "
                        "גרור לערוץ "
                        "כתוביות שני "
                        "כדי לראות את "
                        "החלופות בהקשר",
    "review_listed": "מילים ברשימה "
                     "למטה — לחץ על "
                     "אחת כדי לקפוץ "
                     "אליה",
    "audio_reused": "האודיו נוצל "
                    "מחדש — הטיימליין "
                    "לא השתנה מהריצה "
                    "הקודמת",
    "cleared": "נוקו",
    "markers_word": "סימונים",
    "no_timeline": "לא פתוח טיימליין "
                   "בריזולב",
    "cannot_open": "לא ניתן לפתוח",
}


# --------------------------------------------------------------------------
# the pipeline's log
# --------------------------------------------------------------------------

# (pattern, replacement). Ordered: the first match wins, so anything specific
# has to sit above anything general. `\1`-style groups carry the numbers and
# file names through untouched -- a translated log that invented a different
# number would be worse than an English one.
_LOG = [
    (r"^connected · (.+)$",
     "מחובר · \\1"),
    (r"^project '(.+)', timeline '(.+)'$",
     "פרויקט: '\\1' · "
     "טיימליין: '\\2'"),

    # audio
    (r"^could not fingerprint the timeline \((.+)\); rendering$",
     "לא ניתן לזהות "
     "את הטיימליין "
     "(\\1); מרנדר"),
    (r"^timeline unchanged since the last run -- reusing (.+), skipping the render$",
     "הטיימליין לא "
     "השתנה מהריצה "
     "הקודמת — משתמש "
     "שוב ב-\\1, בלי לרנדר"),
    (r"^audio -> (.+)$",
     "אודיו ← \\1"),

    # stages
    (r"^transcribing \((.+)\)\.\.\.$",
     "מתמלל (\\1)…"),
    (r"^(\d+) words$",
     "\\1 מילים"),
    (r"^proofreading \((.+)\)\.\.\.$",
     "מגיה (\\1)…"),
    (r"^(\d+) correction\(s\) \(no glossary\.txt found next to the project\)$",
     "\\1 תיקונים "
     "(לא נמצא glossary.txt ליד "
     "הפרויקט)"),
    (r"^(\d+) correction\(s\)$",
     "\\1 תיקונים"),
    (r"^second opinion \((.+)\)\.\.\.$",
     "חוות דעת שנייה "
     "(\\1)…"),
    (r"^second opinion agrees everywhere -- nothing to review$",
     "שני המודלים "
     "מסכימים על הכל "
     "— אין מה לבדוק"),
    (r"^corrected (\d+) word\(s\) automatically -- the other model heard a real "
     r"Hebrew word where this one did not$",
     "תוקנו \\1 מילים "
     "אוטומטית — המודל "
     "השני שמע מילה "
     "עברית קיימת "
     "והראשון לא"),
    (r"^(\d+) word\(s\) worth a look \(about half of these are genuinely wrong\):$",
     "\\1 מילים שוות "
     "בדיקה (כמחצית "
     "מהן באמת שגויות):"),
    # a review row: "1:23  WORD   (2nd model heard: OTHER)"
    (r"^(\S+)\s+(\S+)\s+\(2nd model heard: (.+)\)$",
     "\\1  \\2  (המודל "
     "השני שמע: \\3)"),
    (r"^\(2nd model heard: (.+)\)$",
     "(המודל השני שמע: \\1)"),
    (r"^\.\.\.and (\d+) more, all of them in (.+)$",
     "ועוד \\1 — כולן "
     "ב-\\2"),

    # segmentation and export
    (r"^(\d+) cards$",
     "\\1 כרטיסים"),
    (r"^wrote (.+)$",
     "נכתב \\1"),
    (r"^closed (\d+) sub-(\d+)ms gap\(s\) so those cards touch; real pauses left alone$",
     "נסגרו \\1 מרווחים "
     "מתחת ל-\\2 מ\"ש כדי "
     "שהכרטיסים ייגעו "
     "— הפסקות אמיתיות "
     "נשמרו"),
    (r"^the first card is a placeholder reading '(.+)'\.$",
     "הכרטיס הראשון "
     "הוא סמן תזמון: '\\1'."),
    (r"^snap the clip to the start of the timeline, then delete that card -- "
     r"everything after it lands frame-exact\.$",
     "הצמד את הקליפ "
     "לתחילת הטיימליין, "
     "ואז מחק את הכרטיס "
     "הזה — כל מה שאחריו "
     "ינחת מדויק לפריים"),

    # placement
    (r"^placed (\d+) cards on a subtitle track$",
     "\\1 כרטיסים הונחו "
     "על ערוץ כתוביות"),
    (r"^placed (\d+) Text\+ cards \(font: (.+)\)$",
     "\\1 כרטיסי Text+ הונחו "
     "(גופן: \\2)"),
    (r"^timeline is not empty, so the \.srt was imported into the media pool "
     r"instead of appended\.$",
     "הטיימליין לא "
     "ריק, אז ה-srt יובא "
     "למאגר ולא הונח "
     "על הטיימליין."),
    (r"^drag '(.+)' from the master bin onto a subtitle track -- it carries its "
     r"own timings and will land frame-exact\.$",
     "גרור את '\\1' מהמאגר "
     "הראשי לערוץ "
     "כתוביות — התזמון "
     "שמור בתוכו והוא "
     "ינחת מדויק לפריים."),
    (r"^\(appending here would place every card past the end of the programme; "
     r"Resolve exposes no way to position a subtitle clip by script\.\)$",
     "(הוספה אוטומטית "
     "הייתה מניחה את "
     "הכל אחרי סוף "
     "התוכנית; ריזולב "
     "לא מאפשרת למקם "
     "קליפ כתוביות "
     "דרך סקריפט.)"),

    # markers and the review track
    # the colour name is Resolve's, and means nothing to a Hebrew reader --
    # what matters is that they are ours and how to step through them.
    (r"^(\d+) (\w+) marker\(s\) on the timeline -- up/down arrow steps between "
     r"them, and the Markers panel lists each word with what the 2nd model heard$",
     "\\1 סימונים ורודים "
     "על הטיימליין — "
     "חיצי מעלה/מטה "
     "מדלגים ביניהם, "
     "וחלונית הסימונים "
     "מציגה כל מילה "
     "ומה שהמודל השני "
     "שמע"),
    (r"^drop '(.+)' on a SECOND subtitle track to see each flagged word beside "
     r"its alternative, in context; delete the track when done$",
     "גרור את '\\1' לערוץ "
     "כתוביות שני כדי "
     "לראות כל מילה "
     "מסומנת ליד החלופה "
     "שלה"),
    (r"^cleared (\d+) review marker\(s\)$",
     "נוקו \\1 סימונים"),
    (r"^could not place review markers \((.+)\)$",
     "לא ניתן להניח "
     "סימונים (\\1)"),
    (r"^could not import the review track \((.+)\)$",
     "לא ניתן לייבא "
     "את ערוץ הבדיקה (\\1)"),

    # warnings and the tail
    (r"^warnings: (.+)$",
     "אזהרות: \\1"),
    (r"^e\.g\. (.+)$",
     "לדוגמה: \\1"),
    (r"^done -- artifacts in (.+)$",
     "סיים — הקבצים "
     "ב-\\1"),
]

_COMPILED = [(re.compile(pattern), repl) for pattern, repl in _LOG]


def translate_log(line: str) -> str:
    """Translate one pipeline log line, or hand it back unchanged.

    Unchanged is a perfectly good outcome. A line this table has never seen --
    a traceback, a new message someone added upstream -- must still reach the
    log in English rather than being swallowed or mangled. The panel's job is
    to be readable, not to guarantee every character is Hebrew.
    """
    stripped = line.strip()
    if not stripped:
        return line
    indent = line[:len(line) - len(line.lstrip())]
    for pattern, repl in _COMPILED:
        if pattern.match(stripped):
            return indent + pattern.sub(repl, stripped)
    return line
