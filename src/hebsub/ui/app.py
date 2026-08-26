"""The hebsub panel.

One button: transcribe the timeline that is open in Resolve, and leave the
finished .srt in the media pool ready to drag onto a subtitle track.

It calls hebsub.host_resolve.run() and nothing else. Every decision about
audio, ASR, correction, card widths and placement already lives in a module
that owns it; this file only collects settings, starts the work on a thread,
and shows what happened.

Placement note, so the panel does not promise something Resolve cannot do:
subtitles are imported into the media pool and dragged onto a track by hand.
Resolve exposes no way to position a subtitle clip by script, and appending
one to a timeline that already has an edit puts every card past the end of the
programme (D28). One drag is the honest cost.
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

from hebsub.ui.hebrew import T, rtl, translate_log
from hebsub.ui.theme import GRILLE, SCREW, Palette, fonts

__all__ = ["main", "Panel"]

PAD = 14


# The pipeline prefixes its progress lines with the module name, which is
# right for a terminal and noise in a panel. Strip it here: how the log reads
# is a presentation concern and belongs on this side of the boundary.
_LOG_PREFIXES = ("host_resolve: ", "  ")


class _Tee:
    """Funnels the pipeline's stdout into the log queue, line by line."""

    def __init__(self, sink: queue.Queue):
        self._sink = sink
        self._buf = ""

    @staticmethod
    def _tidy(line: str) -> str:
        for prefix in _LOG_PREFIXES:
            if line.startswith(prefix):
                line = line[len(prefix):]
                break
        return translate_log(line)

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink.put(("log", self._tidy(line)))
        return len(text)

    def flush(self) -> None:
        if self._buf:
            self._sink.put(("log", self._tidy(self._buf)))
            self._buf = ""


class Panel:
    def __init__(self, root):
        import tkinter as tk

        self.tk = tk
        self.root = root
        self.font = fonts(root)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.last_srt: Path | None = None

        root.title(T["title"])
        root.configure(bg=Palette.cream)
        root.minsize(760, 760)
        root.geometry("780x820")
        self._base_height = 820

        self._build()
        self.root.after(120, self._drain)
        self.refresh()

    # ----------------------------------------------------------------- build

    def _panel(self, parent, number: str, label: str):
        """A faceplate panel: paper surface, hairline border, engraved label.

        The number and the Hebrew word are separate widgets, packed from the
        right. The RLE embedding fixes the base direction of a Hebrew run
        beside a Latin one, but a bare NUMBER beside Hebrew is a weak-direction
        case it does not settle -- "01 · חיבור" came out with the 01 on the
        left, which reads as the number coming last. Two widgets and an
        explicit side="right" is not a workaround; it is the only version that
        cannot drift.

        Not uppercased, either: Hebrew has no case, so .upper() would do
        nothing except mangle a Latin word that wandered in.
        """
        tk = self.tk
        wrap = tk.Frame(parent, bg=Palette.cream)
        head = tk.Frame(wrap, bg=Palette.cream)
        head.pack(pady=(0, 4))
        for text, font in (
            (number, self.font["label"]),
            ("·", self.font["label"]),
            (rtl(label), self.font["he_label"]),
        ):
            tk.Label(
                head, text=text, font=font,
                bg=Palette.cream, fg=Palette.taupe,
            ).pack(side="right", padx=2)
        body = tk.Frame(
            wrap, bg=Palette.paper, highlightbackground=Palette.hairline,
            highlightthickness=1, bd=0,
        )
        body.pack(fill="both", expand=True)
        return wrap, body

    def _build(self):
        tk = self.tk
        root = self.root

        # --- masthead, with the screw corners and grille from the system ----
        head = tk.Canvas(
            root, height=74, bg=Palette.paper, highlightthickness=0, bd=0
        )
        head.pack(fill="x")
        self._masthead = head
        head.bind("<Configure>", self._draw_masthead)

        outer = tk.Frame(root, bg=Palette.cream)
        outer.pack(fill="both", expand=True, padx=PAD, pady=PAD)

        # --- connection ----------------------------------------------------
        wrap, body = self._panel(outer, "01", T["panel_connection"])
        wrap.pack(fill="x")
        body.columnconfigure(1, weight=1)

        # The label and the value are separate widgets, which is what keeps
        # this readable: project names are Latin ("Demo Project"), the label is
        # Hebrew, and Tk would lay a single mixed string out with the Hebrew
        # on the LEFT. Two widgets, two directions, no bidi to fight.
        self.var_project = tk.StringVar(value=T["none"])
        self.var_timeline = tk.StringVar(value=T["none"])
        # Columns run left to right as Tk always will, but the CONTENT is laid
        # out for a Hebrew eye: the label sits at the right edge where reading
        # starts, its value immediately to the left of it, and the button at
        # the far left where a trailing action belongs.
        body.columnconfigure(1, weight=1)
        for r, (lab, var) in enumerate(
            ((T["project"], self.var_project), (T["timeline"], self.var_timeline))
        ):
            tk.Label(
                body, textvariable=var, font=self.font["value"],
                bg=Palette.paper, fg=Palette.charcoal, anchor="e",
            ).grid(row=r, column=1, sticky="ew", padx=(12, 8),
                   pady=(8 if r == 0 else 2, 2))
            tk.Label(
                body, text=rtl(lab), font=self.font["he_body"],
                bg=Palette.paper, fg=Palette.taupe, anchor="e",
            ).grid(row=r, column=2, sticky="e", padx=(0, 12),
                   pady=(8 if r == 0 else 2, 2))

        self.btn_refresh = tk.Button(
            body, text=rtl(T["reconnect"]), font=self.font["he_label"],
            command=self.refresh,
            bg=Palette.paper, fg=Palette.taupe, activebackground=Palette.cream,
            bd=1, relief="solid", highlightbackground=Palette.hairline,
            padx=10, pady=3, cursor="hand2",
        )
        self.btn_refresh.grid(row=0, column=0, rowspan=2, padx=10, pady=8)

        # --- settings ------------------------------------------------------
        wrap, body = self._panel(outer, "02", T["panel_settings"])
        wrap.pack(fill="x", pady=(PAD, 0))

        grid = tk.Frame(body, bg=Palette.paper)
        grid.pack(padx=10, pady=10)

        tk.Label(
            grid, text=rtl(T["card_width"]), font=self.font["he_body"],
            bg=Palette.paper, fg=Palette.taupe,
        ).grid(row=0, column=0, sticky="e", padx=(0, 8))

        self.var_chars = tk.IntVar(value=14)
        self.scale = tk.Scale(
            grid, from_=8, to=28, orient="horizontal", variable=self.var_chars,
            bg=Palette.paper, fg=Palette.charcoal, troughcolor=Palette.cream,
            activebackground=Palette.teal, highlightthickness=0, bd=0,
            font=self.font["label"], length=190, showvalue=False,
            command=lambda _v: self._sync_chars(),
        )
        self.scale.grid(row=0, column=1, padx=(0, 8))

        # Same weak-direction problem as the panel numbers: "14 תווים" as one
        # string renders with the 14 on the left, which reads as "characters
        # 14". Number and unit are separate widgets, packed right to left.
        readout = tk.Frame(grid, bg=Palette.paper)
        readout.grid(row=0, column=2, sticky="w")
        self.var_chars_text = tk.StringVar(value="14")
        tk.Label(
            readout, textvariable=self.var_chars_text, font=self.font["value"],
            bg=Palette.paper, fg=Palette.charcoal, width=3, anchor="e",
        ).pack(side="right")
        tk.Label(
            readout, text=rtl(T["chars"]), font=self.font["he_body"],
            bg=Palette.paper, fg=Palette.taupe,
        ).pack(side="right", padx=(0, 4))

        tk.Label(
            grid, text=rtl(T["width_hint"]), font=self.font["he_hint"],
            bg=Palette.paper, fg=Palette.taupe,
        ).grid(row=1, column=0, columnspan=3, pady=(2, 8))

        # No contextual-correction toggle. The masked LM can only evaluate
        # 50.7% of words -- Hebrew's inflected forms are not single tokens in
        # its vocabulary -- and across nine reels it fired twice for +0.12pp.
        # On Raz's own test clip it produced byte-identical output. Offering a
        # checkbox that reliably does nothing is worse than not offering it.
        # The adapter is still in the repo behind
        # `--passes glossary,llm --llm-adapter masked_lm`. See D37, D39.
        self.var_llm = tk.BooleanVar(value=False)

        opts = tk.Frame(body, bg=Palette.paper)
        opts.pack(padx=10, pady=(0, 10))
        # anchor="e" on every row inside a frame that is itself centred: the
        # checkboxes line up on one edge instead of wandering with the length
        # of each caption.

        self.var_punct = tk.BooleanVar(value=True)
        self._option(opts, T["opt_punct"], self.var_punct)

        # Off by default: it doubles the transcription time, and what it buys
        # is a list to read rather than a better .srt. Worth it when the reel
        # matters, wasted when it does not -- so it is a choice, not a default.
        # On by default: it costs one card the user deletes, and it removes
        # the guesswork from positioning the clip.
        self.var_review = tk.BooleanVar(value=False)
        self._option(opts, T["opt_review"], self.var_review)

        self.var_timing = tk.BooleanVar(value=True)
        self._option(opts, T["opt_timing"], self.var_timing)

        # --- the button ----------------------------------------------------
        self.btn_run = tk.Button(
            outer, text=rtl(T["run"]), font=self.font["he_button"],
            command=self.start, bg=Palette.teal, fg="#12211C",
            activebackground=Palette.teal_deep, activeforeground="#FFFFFF",
            bd=0, relief="flat", pady=13, cursor="hand2",
        )
        self.btn_run.pack(fill="x", pady=(PAD, 0))

        self.var_status = tk.StringVar(value=rtl(T["ready"]))
        self.lbl_status = tk.Label(
            outer, textvariable=self.var_status, font=self.font["he_body"],
            bg=Palette.cream, fg=Palette.taupe, anchor="center",
        )
        self.lbl_status.pack(fill="x", pady=(6, 0))

        # --- log -----------------------------------------------------------
        wrap, body = self._panel(outer, "03", T["panel_log"])
        self.log_wrap = wrap
        # Packed last, on purpose. Tk's packer hands space out in pack order,
        # so an expanding widget packed early squeezes everything after it
        # off the window -- which is exactly what the footer did the moment
        # the review list appeared.

        self.log = tk.Text(
            body, height=9, font=self.font["he_log"], bg=Palette.paper,
            fg=Palette.charcoal, bd=0, highlightthickness=0, wrap="word",
            padx=10, pady=8, state="disabled",
        )
        # Right-aligned, because the log is Hebrew. Tk still lays each line
        # out LTR-base internally -- rtl() in `say` handles that -- but the
        # ragged edge has to be on the left for a Hebrew reader.
        self.log.tag_configure("rtl", justify="right")
        # Scrollbars go on the LEFT. In an RTL layout the right edge is where
        # the text begins, and a bar sitting there crowds the first word of
        # every line.
        bar = tk.Scrollbar(body, command=self.log.yview, bg=Palette.paper)
        self.log.configure(yscrollcommand=bar.set)
        bar.pack(side="left", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("ok", foreground=Palette.teal_deep)
        self.log.tag_configure("bad", foreground=Palette.rust_deep)
        self.log.tag_configure("dim", foreground=Palette.taupe)

        # --- review list ---------------------------------------------------
        # Built once and kept hidden. A panel that is empty for every job
        # without the second opinion on would be dead furniture, so the wrap
        # is only packed when there is something in it.
        self.review_wrap, review_body = self._panel(outer, "04", T["panel_review"])
        self.review_canvas = tk.Canvas(
            review_body, height=150, bg=Palette.paper,
            highlightthickness=0, bd=0,
        )
        review_bar = tk.Scrollbar(
            review_body, command=self.review_canvas.yview, bg=Palette.paper
        )
        self.review_canvas.configure(yscrollcommand=review_bar.set)
        review_bar.pack(side="left", fill="y")
        self.review_canvas.pack(fill="both", expand=True)
        self.review_rows = tk.Frame(self.review_canvas, bg=Palette.paper)
        self._review_window = self.review_canvas.create_window(
            (0, 0), window=self.review_rows, anchor="nw"
        )
        self.review_rows.bind(
            "<Configure>",
            lambda e: self.review_canvas.configure(
                scrollregion=self.review_canvas.bbox("all")
            ),
        )
        self.review_canvas.bind(
            "<Configure>",
            lambda e: self.review_canvas.itemconfigure(
                self._review_window, width=e.width
            ),
        )
        self.review: list[dict] = []
        self._review_done: set[int] = set()

        # --- footer --------------------------------------------------------
        foot = self._foot = tk.Frame(outer, bg=Palette.cream)
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        self.btn_folder = tk.Button(
            foot, text=rtl(T["open_folder"]), font=self.font["he_label"],
            command=self.open_folder, state="disabled",
            bg=Palette.cream, fg=Palette.taupe, activebackground=Palette.paper,
            bd=1, relief="solid", padx=12, pady=4, cursor="hand2",
        )
        self.btn_folder.pack(side="right")
        # Nothing tracks whether Raz has actually fixed a flagged word --
        # pretending to would mean guessing. This is the honest version: the
        # markers stay until he says he is done with them.
        self.btn_clear = tk.Button(
            foot, text=rtl(T["clear_flags"]), font=self.font["he_label"],
            command=self.clear_flags,
            bg=Palette.cream, fg=Palette.taupe, activebackground=Palette.paper,
            bd=1, relief="solid", padx=12, pady=4, cursor="hand2",
        )
        self.btn_clear.pack(side="right", padx=(0, 8))
        self.btn_learn = tk.Button(
            foot, text=rtl(T["learn"]), font=self.font["he_label"],
            command=self.learn_corrections,
            bg=Palette.cream, fg=Palette.taupe, activebackground=Palette.paper,
            bd=1, relief="solid", padx=12, pady=4, cursor="hand2",
        )
        self.btn_learn.pack(side="right", padx=(0, 8))
        tk.Label(
            foot, text=rtl(T["footer_hint"]), font=self.font["he_hint"],
            bg=Palette.cream, fg=Palette.taupe,
        ).pack(side="left", padx=(4, 0))

        self._pack_log()

    def _pack_log(self) -> None:
        """(Re)claim whatever vertical space is left for the log.

        Called again whenever the review list appears, so the list is packed
        against the bottom *before* the log expands into the remainder.
        """
        self.log_wrap.pack_forget()
        self.log_wrap.pack(fill="both", expand=True, pady=(PAD, 0))

    # -------------------------------------------------------- review list

    @staticmethod
    def clock(seconds: float) -> str:
        """m:ss -- the same shape host_resolve prints, so they read alike."""
        minutes, secs = divmod(int(float(seconds)), 60)
        return f"{minutes}:{secs:02d}"

    def show_review(self, review: list[dict]) -> None:
        """Rebuild the flagged-word list, or hide the panel if there is none.

        The list already existed in `review.json` and in the log, where the
        only way to act on a word was to read an m:ss off a log line and type
        it into Resolve by hand. Clicking a row moves the playhead instead.
        """
        for child in self.review_rows.winfo_children():
            child.destroy()
        self.review = list(review or [])
        self._review_done = set()

        if not self.review:
            self.review_wrap.pack_forget()
            self._pack_log()
            return

        for index, flag in enumerate(self.review):
            self._review_row(index, flag)
        self.review_wrap.pack(side="bottom", fill="x", pady=(PAD, 0))
        self._pack_log()
        self._fit_window()

    def _fit_window(self) -> None:
        """Make room for the list rather than squeezing the log to two lines.

        Grows only, and never past the screen. Shrinking back would fight a
        window the user had deliberately resized.
        """
        self.root.update_idletasks()
        want = self._base_height + self.review_wrap.winfo_reqheight() + PAD
        cap = self.root.winfo_screenheight() - 90
        if want <= self.root.winfo_height():
            return
        height = min(want, cap)
        # The position has to go in the geometry string too. Passing size
        # alone lets Windows re-place the window, so growing it made the
        # panel jump across the screen -- and a window whose bottom lands
        # off-screen hides the log and the list that caused the growth.
        x, y = self.root.winfo_x(), self.root.winfo_y()
        y = max(0, min(y, self.root.winfo_screenheight() - height - 60))
        self.root.geometry(f"{self.root.winfo_width()}x{height}+{x}+{y}")

    def _review_row(self, index: int, flag: dict) -> None:
        tk = self.tk
        row = tk.Frame(self.review_rows, bg=Palette.paper)
        row.pack(fill="x", padx=8, pady=1)

        # (widget, resting colour), so crossing a row off can put it back.
        cells: list[tuple] = []

        def tick():
            self._toggle_done(index, cells, mark)

        # The row is mirrored: everything packs from the RIGHT, so a Hebrew
        # reader meets the tick first, then the time, then the word and its
        # alternative. COPY goes to the far left, where the row ends.
        mark = tk.Button(
            row, text="✓", font=self.font["he_body"], width=2,
            command=tick, bg=Palette.paper, fg=Palette.disabled,
            activebackground=Palette.cream, bd=1, relief="solid",
            cursor="hand2",
        )
        mark.pack(side="right", padx=(8, 0))

        stamp = tk.Label(
            row, text=self.clock(flag["start"]), font=self.font["value"],
            bg=Palette.paper, fg=Palette.taupe, width=6, anchor="e",
            cursor="hand2",
        )
        stamp.pack(side="right")
        cells.append((stamp, Palette.taupe))

        # Each word gets its own label rather than one formatted string:
        # putting Hebrew, an arrow and a timecode in a single Tk label invites
        # bidi reordering, and which word is which is the whole point here.
        heard = tk.Label(
            row, text=flag["heard"], font=self.font["he_word"],
            bg=Palette.paper, fg=Palette.charcoal, cursor="hand2",
        )
        heard.pack(side="right", padx=(6, 10))
        cells.append((heard, Palette.charcoal))

        # Pointing left, because that is the direction of reading here.
        arrow = tk.Label(
            row, text="←", font=self.font["value"],
            bg=Palette.paper, fg=Palette.disabled, cursor="hand2",
        )
        arrow.pack(side="right")
        cells.append((arrow, Palette.disabled))

        other = tk.Label(
            row, text=flag["alternative"], font=self.font["he_word"],
            bg=Palette.paper, fg=Palette.rust_deep, cursor="hand2",
        )
        other.pack(side="right", padx=(0, 6))
        cells.append((other, Palette.rust_deep))

        tk.Button(
            row, text=rtl(T["copy"]), font=self.font["he_label"],
            command=lambda: self.copy_word(flag["alternative"]),
            bg=Palette.paper, fg=Palette.taupe,
            activebackground=Palette.cream, bd=1, relief="solid",
            padx=6, cursor="hand2",
        ).pack(side="left")

        for widget in (row, *(cell for cell, _ in cells)):
            widget.bind("<Button-1>", lambda e, f=flag: self.goto(f))

    def _toggle_done(self, index: int, cells: list, mark) -> None:
        """Cross a word off. Local to this window, and deliberately so.

        Nothing in Resolve knows whether the word was fixed (D67) -- inferring
        it would mean re-transcribing to check. This is a reading aid for the
        pass you are making right now, and it does not survive the window.
        """
        if index in self._review_done:
            self._review_done.discard(index)
        else:
            self._review_done.add(index)
        done = index in self._review_done

        mark.configure(fg=Palette.teal_deep if done else Palette.disabled)
        for cell, colour in cells:
            cell.configure(fg=Palette.disabled if done else colour)

    def copy_word(self, word: str) -> None:
        """Put a word on the clipboard, so Hebrew is never retyped by hand."""
        self.root.clipboard_clear()
        self.root.clipboard_append(word)
        self.say(f"{T['copied']}  {word}", "dim")

    def goto(self, flag: dict) -> None:
        """Move Resolve's playhead to a flagged word."""
        from hebsub import host_resolve

        try:
            resolve = host_resolve.connect()
            project, timeline = host_resolve._current(resolve)
            target = host_resolve.seek(project, timeline, flag["start"])
        except Exception as exc:  # noqa: BLE001
            self.say(translate_log(str(exc)), "bad")
            return
        self.var_status.set(rtl(f"{target}  —  {flag['heard']}"))

    def learn_corrections(self):
        """Read the corrected subtitle track and add its new words.

        Press it when the reel is finished, not before: it compares the track
        against the .srt of the LAST run, so the words it finds are the ones
        that changed. Correct first, then learn.
        """
        from hebsub import host_resolve

        try:
            resolve = host_resolve.connect()
            project, timeline = host_resolve._current(resolve)
            work = host_resolve.work_root() / host_resolve.work_dir_name(
                project.GetName(), timeline.GetName()
            )
            result = host_resolve.harvest_corrections(
                timeline, work / "final.srt", host_resolve.lexicon_path()
            )
        except Exception as exc:  # noqa: BLE001
            self.say(translate_log(str(exc)), "bad")
            return

        if result["status"] == "no_srt":
            self.say(T["learn_no_run"], "bad")
            self.var_status.set(rtl(T["learn_no_run"]))
            return
        if result["status"] == "no_match":
            self.say(T["learn_no_match"], "bad")
            self.var_status.set(rtl(T["learn_no_match"]))
            return

        words = result["learned"]
        if not words:
            self.say(f"\u2713 {T['learn_none']}", "ok")
            self.var_status.set(rtl(T["learn_none"]))
            return

        self.say(f"\u2713 {len(words)} {T['learn_added']}", "ok")
        # Printed in full, on purpose. These go into a file that accretes, so
        # a wrong one is permanent until somebody deletes the line -- seeing
        # them is the only chance to notice.
        for word in words:
            self.say(f"   {word}", "dim")
        self.say(f"   {T['learn_total']} {result['total']}", "dim")
        self.var_status.set(rtl(f"{len(words)} {T['learn_added']}"))

    def clear_flags(self):
        """Remove this tool's review markers from the current timeline."""
        from hebsub import host_resolve

        try:
            resolve = host_resolve.connect()
            project = resolve.GetProjectManager().GetCurrentProject()
            timeline = project.GetCurrentTimeline() if project else None
            if timeline is None:
                self.say(T["no_timeline"], "bad")
                return
            gone = host_resolve.clear_review_markers(timeline)
        except Exception as exc:  # noqa: BLE001
            self.say(translate_log(str(exc)), "bad")
            return
        self.say(f"✓ {T['cleared']} {gone} {T['markers_word']}", "ok")
        self.var_status.set(rtl(f"{T['cleared']} {gone} {T['markers_word']}."))

    def _draw_masthead(self, _event=None):
        c = self._masthead
        c.delete("all")
        w = c.winfo_width() or 720
        h = 74
        # grille: texture only, never behind text (design system)
        for x in range(int(w * 0.52), w, 7):
            c.create_line(x, 12, x, h - 12, fill=GRILLE, width=2)
        for x, y in ((10, 10), (w - 10, 10), (10, h - 10), (w - 10, h - 10)):
            c.create_oval(x - 4, y - 4, x + 4, y + 4, fill=SCREW, outline="")
        c.create_text(
            22, 30, text="HEBSUB", anchor="w",
            font=self.font["wordmark"], fill=Palette.charcoal,
        )
        c.create_text(
            24, 54, text=rtl(T["wordmark_sub"]), anchor="w",
            font=self.font["he_hint"], fill=Palette.taupe,
        )

    def _option(self, parent, label: str, var):
        """One centred Hebrew checkbox.

        The caption sits in its own Label rather than in the Checkbutton's
        `text`, because a Checkbutton draws its indicator on the left and Tk
        will not mirror that. A separate label lets the box sit on the right
        of the caption, which is where a Hebrew reader looks for it.
        """
        tk = self.tk
        row = tk.Frame(parent, bg=Palette.paper)
        row.pack(anchor="e", pady=3)
        box = tk.Checkbutton(
            row, variable=var, bg=Palette.paper, activebackground=Palette.paper,
            selectcolor=Palette.cream, highlightthickness=0, bd=0,
            cursor="hand2",
        )
        text = tk.Label(
            row, text=rtl(label), font=self.font["he_body"],
            bg=Palette.paper, fg=Palette.charcoal, cursor="hand2",
        )
        box.pack(side="right")
        text.pack(side="right", padx=(0, 2))
        text.bind("<Button-1>", lambda _e: box.invoke())
        return box

    def _sync_chars(self):
        self.var_chars_text.set(str(self.var_chars.get()))

    # ------------------------------------------------------------------ log

    def say(self, text: str, tag: str = "") -> None:
        # Every line gets the RTL embedding and the right-justify tag. A line
        # that is pure Latin -- a traceback, an untranslated upstream message
        # -- still reads correctly inside an RTL paragraph; the embedding only
        # decides which edge the line starts from.
        self.log.configure(state="normal")
        tags = ("rtl", tag) if tag else ("rtl",)
        self.log.insert("end", rtl(text) + "\n", tags)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------- resolve

    def refresh(self) -> None:
        try:
            from hebsub.host_resolve import ResolveError, _current, connect

            resolve = connect()
            project, timeline = _current(resolve)
            self.var_project.set(project.GetName())
            self.var_timeline.set(timeline.GetName())
            self.say(
                f"connected · {resolve.GetProductName()} "
                f"{resolve.GetVersionString()}", "ok"
            )
            self.var_status.set(rtl(T["ready"]))
            self.btn_run.configure(state="normal", bg=Palette.teal)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self.var_project.set(T["none"])
            self.var_timeline.set(T["not_connected"])
            self.say(translate_log(str(exc)), "bad")
            self.var_status.set(rtl(T["open_timeline"]))
            self.btn_run.configure(state="disabled", bg=Palette.disabled)

    # ---------------------------------------------------------------- work

    def start(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.btn_run.configure(
            state="disabled", text=rtl(T["running"]), bg=Palette.disabled
        )
        self.btn_folder.configure(state="disabled")
        self.var_status.set(rtl(T["preparing"]))
        self.say("", "")
        self.say("─" * 58, "dim")

        settings = {
            "max_chars": int(self.var_chars.get()),
            "passes": "glossary,llm" if self.var_llm.get() else "glossary",
            "strip_punct": bool(self.var_punct.get()),
            "review": bool(self.var_review.get()),
            "timing_clip": bool(self.var_timing.get()),
        }
        threading.Thread(target=self._work, args=(settings,), daemon=True).start()

    def _work(self, settings: dict) -> None:
        import contextlib

        tee = _Tee(self.events)
        try:
            with contextlib.redirect_stdout(tee):
                result = self._run_pipeline(settings)
            tee.flush()
            self.events.put(("done", result))
        except Exception as exc:  # noqa: BLE001
            tee.flush()
            self.events.put(("fail", (exc, traceback.format_exc())))

    def _run_pipeline(self, settings: dict) -> dict:
        """Hand the panel's settings to the one pipeline and get a result back.

        This used to be a second copy of `host_resolve.run`, and the copies
        drifted: the panel proofread but wrote no intermediates, the CLI wrote
        intermediates but never proofread, and neither difference was noticed
        until it cost real measurement time (D41, D42). There is one pipeline
        now. The panel decides what the user asked for and displays what came
        back; everything between those is somebody else's module.
        """
        from hebsub.host_resolve import run

        from hebsub.host_resolve import DEFAULT_REVIEW_MODEL

        return run(
            max_chars=settings["max_chars"],
            passes=tuple(p for p in settings["passes"].split(",") if p),
            strip_punct=settings["strip_punct"],
            review_model=DEFAULT_REVIEW_MODEL if settings.get("review") else None,
            timing_clip=settings.get("timing_clip", True),
        )

    # --------------------------------------------------------------- events

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.say(payload)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "fail":
                    self._fail(*payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _finish(self, result: dict) -> None:
        self.busy = False
        self.btn_run.configure(
            state="normal", text=rtl(T["run"]), bg=Palette.teal
        )
        self.last_srt = result["srt"]
        self.btn_folder.configure(state="normal")

        self.say("")
        if result["status"] == "placed":
            self.say(
                f"✓ {result['placed']} {T['cards_placed']}", "ok"
            )
            self.var_status.set(
                rtl(f"{T['done']} — {result['cards']} {T['cards_on_timeline']}")
            )
        else:
            name = (result["status"].split(":", 1)[1]
                    if str(result["status"]).startswith("pool:")
                    else "HebSub Subtitles")
            self.say(f"✓ {T['imported_as']} '{name}'", "ok")
            self.say(f"  {T['drag_hint']}", "dim")
            self.var_status.set(
                rtl(f"{T['done']} — {result['cards']} {T['cards_drag']} '{name}'")
            )

        if result.get("markers"):
            self.say(f"  {result['markers']} {T['markers_placed']}", "dim")
        if result.get("review_clip"):
            self.say(
                f"  '{result['review_clip']}' — {T['review_clip_hint']}", "dim"
            )

        self.show_review(result.get("review") or [])
        if result.get("review"):
            self.say(
                f"  {len(result['review'])} {T['review_listed']}", "dim"
            )

        if result.get("audio") == "reused":
            self.say(f"  {T['audio_reused']}", "dim")

        # The warning summary is not repeated here: the pipeline already put
        # it in the log above, and printing it twice was how the panel and the
        # CLI drifted apart in the first place.

    def _fail(self, exc: Exception, tb: str) -> None:
        self.busy = False
        self.btn_run.configure(
            state="normal", text=rtl(T["run"]), bg=Palette.teal
        )
        self.say("")
        self.say(f"✗ {type(exc).__name__}: {exc}", "bad")
        for line in tb.strip().splitlines()[-4:]:
            self.say(f"  {line}", "dim")
        self.var_status.set(rtl(T["failed"]))

    def open_folder(self) -> None:
        if not self.last_srt:
            return
        import subprocess

        target = self.last_srt.parent
        try:
            if sys.platform == "win32":
                subprocess.run(["explorer", str(target)], check=False)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(target)], check=False)
            else:
                subprocess.run(["xdg-open", str(target)], check=False)
        except OSError as exc:
            self.say(f"{T['cannot_open']} {target}: {exc}", "bad")


def main(argv: list[str] | None = None) -> int:
    import tkinter as tk

    root = tk.Tk()
    Panel(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
