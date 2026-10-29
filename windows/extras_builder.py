"""Drag & drop editor for the extra key rows and columns (see extras.py).

Left: every available key, grouped. Middle: a picture of the keyboard with the rows and
columns around it, drawn with the real renderer so it looks exactly like the result.
Right: the look (match Gboard, solid colour or gradient) and key size.

Drag a key from the left into a row or column, drag keys around to reorder them, or
drag one out to remove it. A plain tap on a key in the list adds it to the row or
column picked last, which is easier with a finger than dragging. Changes show on the
real keyboard straight away; Save keeps them, Cancel puts everything back.
"""
import copy
import ctypes
import threading
import tkinter as tk
from tkinter import colorchooser, messagebox, ttk

from PIL import ImageTk

import extras as X

EDGE_NAME = {"top": "Row above the keyboard", "bottom": "Row below the keyboard",
             "left": "Column on the left", "right": "Column on the right"}
SUGGESTED = {"edge": "top", "keys": ["esc", "tab", "ctrl", "alt", "shift", "win",
                                     "left", "up", "down", "right"]}


class Builder:
    def __init__(self, manager):
        self.m = manager
        self.s = s = max(1.0, manager.scale)
        self.orig = copy.deepcopy(X.CONFIG.get())
        self.cfg = copy.deepcopy(self.orig)
        self.dirty = False
        self.active = None           # ("rows"|"cols", index) a tap adds into
        self.drag = None
        self.ghost = None
        self.ghost_photo = None
        self.chip_cache = {}
        self.slot_photos = []
        self.slot_hits = []          # (kind, idx, x0, y0, x1, y1, boxes)
        self.font = ("Segoe UI", -int(13 * s))
        self.font_b = ("Segoe UI Semibold", -int(13 * s))
        self.font_h = ("Segoe UI Semibold", -int(16 * s))
        self.font_small = ("Segoe UI", -int(11 * s))
        self.tip_win = None          # hover label naming the key under the pointer
        self.tip_kid = None
        self.tip_job = None

        w = self.win = tk.Toplevel(manager.root)
        w.title("GboardIME - Extra keys")
        w.configure(bg="#202124")
        if X.ICON_PATH:
            try:
                w.iconbitmap(X.ICON_PATH)    # not Tk's feather / Python's icon
            except Exception:
                pass
        try:
            wl, wt_, wr, wb = X.API["work_area"]()
        except Exception:
            wl, wt_, wr, wb = 0, 0, 1920, 1040
        W = min(int(1180 * s), int((wr - wl) * 0.95))
        H = min(int(760 * s), int((wb - wt_) * 0.92))
        x, y = wl + (wr - wl - W) // 2, wt_ + (wb - wt_ - H) // 2
        # The keyboard is always on top, so the editor goes beside it when there is room
        # (shrinking a little if needed) rather than underneath it.
        busy = X.keyboard_rect()
        if busy:
            # Clamp into the work area first: the keyboard may sit on another monitor,
            # and then this one is free from edge to edge.
            bl, br = max(wl, min(busy[0], wr)), min(wr, max(busy[2], wl))
            left, right = bl - wl, wr - br
            room, side = max((left, "l"), (right, "r"))
            if room >= int(900 * s):
                W = min(W, room - int(8 * s))
                x = wl + (left - W) // 2 if side == "l" else br + (right - W) // 2
        w.geometry(f"{W}x{H}+{x}+{y}")
        w.protocol("WM_DELETE_WINDOW", self.on_close)
        w.columnconfigure(1, weight=1)
        w.rowconfigure(0, weight=1)
        self._build_palette()
        self._build_layout()
        self._build_theme()
        self.refresh_all()
        w.update_idletasks()
        X.touch_friendly(w)          # finger drags must arrive as drags, not scrolls
        w.lift()
        w.focus_force()

    # -- basics -------------------------------------------------------------------
    def alive(self):
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def focus(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def changed(self):
        """Apply the working copy to the real keyboard right away (not saved yet)."""
        self.dirty = True
        X.CONFIG.publish(self.cfg, save=False)
        self.cfg = copy.deepcopy(X.CONFIG.get())     # keep the validated copy
        self.refresh_all()

    def refresh_all(self):
        self.chip_cache.clear()
        self.draw_palette()
        self.draw_layout()
        self.draw_list()
        self.sync_theme_controls()

    def px(self, v):
        return int(v * self.s)

    def _label(self, parent, text, font=None, fg="#e8eaed", **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=fg, font=font or self.font,
                        anchor="w", justify="left", **kw)

    def _button(self, parent, text, cmd, primary=False):
        return tk.Button(parent, text=text, command=cmd, font=self.font_b if primary else self.font,
                         bg="#8ab4f8" if primary else "#3c4043",
                         fg="#202124" if primary else "#e8eaed",
                         activebackground="#aecbfa" if primary else "#5f6368",
                         activeforeground="#202124" if primary else "#ffffff",
                         relief="flat", padx=self.px(10), pady=self.px(4), cursor="hand2")

    # -- palette -------------------------------------------------------------------
    def _build_palette(self):
        f = tk.Frame(self.win, bg="#202124")
        f.grid(row=0, column=0, sticky="ns", padx=(self.px(12), self.px(6)), pady=self.px(12))
        self._label(f, "Keys", self.font_h).pack(fill="x")
        self._label(f, "Drag onto a row or column, or tap to add\nto the one you picked last.",
                    self.font_small, fg="#9aa0a6").pack(fill="x", pady=(0, self.px(6)))
        inner = tk.Frame(f, bg="#202124")
        inner.pack(fill="both", expand=True)
        self.pal_w = self.px(300)
        self.pal = tk.Canvas(inner, width=self.pal_w, bg="#202124", highlightthickness=0)
        sb = tk.Scrollbar(inner, orient="vertical", command=self.pal.yview)
        self.pal.configure(yscrollcommand=sb.set)
        self.pal.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.pal.bind("<ButtonPress-1>", self.pal_down)
        self.pal.bind("<B1-Motion>", self.any_motion)
        self.pal.bind("<ButtonRelease-1>", self.any_up)
        self.pal.bind("<MouseWheel>", lambda e: (self._tip_hide(),
                                                 self.pal.yview_scroll(int(-e.delta / 120), "units")))
        self.pal.bind("<Motion>", lambda e: self._hover(self.pal, e))
        self.pal.bind("<Leave>", lambda e: self._tip_hide())

    def chip(self, kid, w, h):
        k = (kid, w, h)
        if k not in self.chip_cache:
            self.chip_cache[k] = ImageTk.PhotoImage(X.render_chip(kid, w, h, self.s, self.cfg))
        return self.chip_cache[k]

    def draw_palette(self):
        c = self.pal
        c.delete("all")
        cw, ch, gap = self.px(66), self.px(40), self.px(6)
        per = max(1, (self.pal_w - gap) // (cw + gap))
        y = self.px(4)
        groups = {g: [] for g in X.GROUPS}
        for kid, _, _, _, _, grp in X.CATALOG:
            groups[grp].append(kid)
        groups["Custom"] = [cdef["id"] for cdef in self.cfg["custom"]]
        for grp in X.GROUPS:
            c.create_text(0, y, text=grp, anchor="nw", fill="#9aa0a6", font=self.font_b)
            y += self.px(22)
            items = groups[grp]
            for i, kid in enumerate(items):
                col, row = i % per, i // per
                x0 = col * (cw + gap)
                y0 = y + row * (ch + gap)
                c.create_image(x0, y0, image=self.chip(kid, cw, ch), anchor="nw",
                               tags=("chip", f"k:{kid}"))
                if grp == "Custom":
                    c.create_text(x0 + cw - self.px(3), y0 + self.px(1), text="✕", anchor="ne",
                                  fill="#f28b82", font=self.font_small, tags=("del", f"d:{kid}"))
            rows = (len(items) + per - 1) // per
            y += rows * (ch + gap) + self.px(6)
            if grp == "Custom":
                b = c.create_rectangle(0, y, cw * 2 + gap, y + ch, outline="#8ab4f8",
                                       dash=(4, 3), width=max(1, int(self.s)), tags=("newcustom",))
                c.create_text((cw * 2 + gap) // 2, y + ch // 2, text="+ New custom key",
                              fill="#8ab4f8", font=self.font_b, tags=("newcustom",))
                y += ch + gap
        c.configure(scrollregion=(0, 0, self.pal_w, y + self.px(10)))

    # -- layout picture ---------------------------------------------------------------
    def _build_layout(self):
        f = tk.Frame(self.win, bg="#202124")
        f.grid(row=0, column=1, sticky="nsew", padx=self.px(6), pady=self.px(12))
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)
        self._label(f, "Your layout", self.font_h).grid(row=0, column=0, sticky="w")
        self.lay = tk.Canvas(f, bg="#171717", highlightthickness=0)
        self.lay.grid(row=1, column=0, sticky="nsew", pady=(self.px(6), self.px(8)))
        self.lay.bind("<Configure>", lambda e: self.draw_layout())
        self.lay.bind("<ButtonPress-1>", self.lay_down)
        self.lay.bind("<B1-Motion>", self.any_motion)
        self.lay.bind("<ButtonRelease-1>", self.any_up)
        self.lay.bind("<Motion>", lambda e: self._hover(self.lay, e))
        self.lay.bind("<Leave>", lambda e: self._tip_hide())
        adds = tk.Frame(f, bg="#202124")
        adds.grid(row=2, column=0, sticky="ew")
        self.add_btns = {}
        for edge, text in (("top", "+ Row above"), ("bottom", "+ Row below"),
                           ("left", "+ Column left"), ("right", "+ Column right")):
            b = self._button(adds, text, lambda e=edge: self.add_bar(e))
            b.pack(side="left", padx=(0, self.px(6)))
            self.add_btns[edge] = b
        self.list_frame = tk.Frame(f, bg="#202124")
        self.list_frame.grid(row=3, column=0, sticky="ew", pady=(self.px(8), 0))

    def _slots(self):
        """Rectangles for every bar in the picture, plus the keyboard box."""
        c = self.lay
        CW, CH = max(200, c.winfo_width()), max(200, c.winfo_height())
        m, gap = self.px(14), self.px(6)
        row_h, col_w = self.px(48), self.px(62)
        n = X.count_edges(self.cfg)
        x0 = m + n["left"] * (col_w + gap)
        x1 = CW - m - n["right"] * (col_w + gap)
        ky0 = m + n["top"] * (row_h + gap)
        ky1 = CH - m - n["bottom"] * (row_h + gap)
        # Columns span the rows' height too, like on the real keyboard.
        cy0 = m if n["top"] else ky0
        cy1 = CH - m if n["bottom"] else ky1
        slots = []
        cnt = {"top": 0, "bottom": 0, "left": 0, "right": 0}
        for kind in ("rows", "cols"):
            for i, b in enumerate(self.cfg[kind]):
                e = b["edge"]
                j = cnt[e]
                cnt[e] += 1
                if e == "top":
                    r = (x0, m + j * (row_h + gap), x1, m + j * (row_h + gap) + row_h)
                elif e == "bottom":
                    r = (x0, ky1 + gap + j * (row_h + gap), x1, ky1 + gap + j * (row_h + gap) + row_h)
                elif e == "left":
                    r = (m + j * (col_w + gap), cy0, m + j * (col_w + gap) + col_w, cy1)
                else:
                    r = (x1 + gap + j * (col_w + gap), cy0, x1 + gap + j * (col_w + gap) + col_w, cy1)
                slots.append((kind, i, r))
        return slots, (x0, ky0, x1, ky1)

    def draw_layout(self):
        c = self.lay
        if not self.alive():
            return
        c.delete("all")
        self.slot_photos = []
        self.slot_hits = []
        slots, kb = self._slots()
        x0, y0, x1, y1 = kb
        c.create_rectangle(x0, y0, x1, y1, fill="#3c4043", outline="#5f6368")
        c.create_text((x0 + x1) // 2, (y0 + y1) // 2, text="Gboard keyboard", fill="#9aa0a6",
                      font=self.font_h)
        # The outermost column on each side gets rounded outer corners, as on the keyboard.
        side_x = {}
        for kind, i, r in slots:
            e = self.cfg[kind][i]["edge"]
            if e in ("left", "right"):
                pick = min if e == "left" else max
                side_x[e] = pick(side_x.get(e, r[0]), r[0])
        for kind, i, (a, b, cc, d) in slots:
            w, h = int(cc - a), int(d - b)
            bar = self.cfg[kind][i]
            edge = bar["edge"]
            if bar["keys"] and w > 8 and h > 8:
                img, boxes = X.render_bar(edge, w, h, bar["keys"], self.s, {}, -1, self.cfg)
                if edge in side_x and side_x[edge] == a:
                    gap = X.sizes(self.s, self.cfg)[2]           # same rule, preview width
                    img = X.round_outer(img, edge, max(3, int((w - 2 * gap) * 0.2)) + gap,
                                        "#171717")
                ph = ImageTk.PhotoImage(img)
                self.slot_photos.append(ph)
                c.create_image(a, b, image=ph, anchor="nw")
            else:
                boxes = []
                c.create_rectangle(a, b, cc, d, outline="#8ab4f8", dash=(5, 4),
                                   width=max(1, int(self.s)))
                c.create_text((a + cc) // 2, (b + d) // 2,
                              text="Drag keys here" if edge in ("top", "bottom") else "Drag\nkeys\nhere",
                              fill="#8ab4f8", font=self.font_small, justify="center")
            if self.active == (kind, i):
                c.create_rectangle(a - 3, b - 3, cc + 3, d + 3, outline="#fdd663",
                                   width=max(2, int(self.s * 1.5)))
            self.slot_hits.append((kind, i, a, b, cc, d, boxes))
        if not slots:
            c.create_text((x0 + x1) // 2, y0 - self.px(30) if y0 > self.px(60) else y1 + self.px(30),
                          text="Add a row or column below, or start with the suggested row.",
                          fill="#9aa0a6", font=self.font)

    def draw_list(self):
        for wdg in self.list_frame.winfo_children():
            wdg.destroy()
        n = X.count_edges(self.cfg)
        rows_full = len(self.cfg["rows"]) >= X.MAX_ROWS
        cols_full = len(self.cfg["cols"]) >= X.MAX_COLS
        for e, b in self.add_btns.items():
            full = rows_full if e in ("top", "bottom") else cols_full
            b.configure(state="disabled" if full else "normal")
        if not self.cfg["rows"] and not self.cfg["cols"]:
            self._button(self.list_frame, "Start with a suggested row", self.suggest,
                         primary=True).pack(anchor="w")
            return
        for kind in ("rows", "cols"):
            for i, b in enumerate(self.cfg[kind]):
                line = tk.Frame(self.list_frame, bg="#202124")
                line.pack(fill="x", pady=self.px(2))
                self._label(line, f"{EDGE_NAME[b['edge']]}  ({len(b['keys'])} keys)",
                            self.font).pack(side="left")
                self._button(line, "Remove", lambda k=kind, j=i: self.remove_bar(k, j)).pack(
                    side="right", padx=(self.px(4), 0))
                other = {"top": "Move below", "bottom": "Move above",
                         "left": "Move to right", "right": "Move to left"}[b["edge"]]
                self._button(line, other, lambda k=kind, j=i: self.flip_bar(k, j)).pack(
                    side="right", padx=(self.px(4), 0))
                same = [j for j, bb in enumerate(self.cfg[kind]) if bb["edge"] == b["edge"]]
                if len(same) > 1:
                    pos = same.index(i)
                    if pos > 0:
                        self._button(line, "▲", lambda k=kind, j=i, t=same[pos - 1]:
                                     self.swap(k, j, t)).pack(side="right", padx=(self.px(4), 0))
                    if pos < len(same) - 1:
                        self._button(line, "▼", lambda k=kind, j=i, t=same[pos + 1]:
                                     self.swap(k, j, t)).pack(side="right", padx=(self.px(4), 0))
                line.bind("<Button-1>", lambda e, k=kind, j=i: self.set_active(k, j))

    # -- bar operations -------------------------------------------------------------------
    def add_bar(self, edge):
        kind = "rows" if edge in ("top", "bottom") else "cols"
        cap = X.MAX_ROWS if kind == "rows" else X.MAX_COLS
        if len(self.cfg[kind]) >= cap:
            return
        self.cfg[kind].append({"edge": edge, "keys": []})
        self.active = (kind, len(self.cfg[kind]) - 1)
        self.changed()

    def suggest(self):
        self.cfg["rows"].append(copy.deepcopy(SUGGESTED))
        self.active = ("rows", len(self.cfg["rows"]) - 1)
        self.changed()

    def remove_bar(self, kind, i):
        del self.cfg[kind][i]
        self.active = None
        self.changed()

    def flip_bar(self, kind, i):
        b = self.cfg[kind][i]
        b["edge"] = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}[b["edge"]]
        self.changed()

    def swap(self, kind, i, j):
        L = self.cfg[kind]
        L[i], L[j] = L[j], L[i]
        if self.active in ((kind, i), (kind, j)):
            self.active = (kind, j if self.active == (kind, i) else i)
        self.changed()

    def set_active(self, kind, i):
        self.active = (kind, i)
        self.draw_layout()

    def add_to_active(self, kid):
        if not self.active or self.active[1] >= len(self.cfg[self.active[0]]):
            if self.cfg["rows"]:
                self.active = ("rows", 0)
            elif self.cfg["cols"]:
                self.active = ("cols", 0)
            elif len(self.cfg["rows"]) < X.MAX_ROWS:
                self.cfg["rows"].append({"edge": "top", "keys": []})
                self.active = ("rows", 0)
            else:
                return
        kind, i = self.active
        cap = X.MAX_KEYS_ROW if kind == "rows" else X.MAX_KEYS_COL
        if len(self.cfg[kind][i]["keys"]) >= cap:
            messagebox.showinfo("Extra keys", "That row or column is full.", parent=self.win)
            return
        self.cfg[kind][i]["keys"].append(kid)
        self.changed()

    # -- hover names --------------------------------------------------------------------
    def _kid_at(self, widget, ev):
        if widget is self.pal:
            x, y = self.pal.canvasx(ev.x), self.pal.canvasy(ev.y)
            for it in reversed(self.pal.find_overlapping(x, y, x, y)):
                for t in self.pal.gettags(it):
                    if t.startswith("k:"):
                        return t[2:]
            return None
        for kind, i, a, b, c, d, boxes in self.slot_hits:
            if a <= ev.x <= c and b <= ev.y <= d:
                lx, ly = ev.x - a, ev.y - b
                keys = self.cfg[kind][i]["keys"]
                for k, (x0, y0, x1, y1) in enumerate(boxes):
                    if x0 <= lx <= x1 and y0 <= ly <= y1 and k < len(keys):
                        return keys[k]
        return None

    @staticmethod
    def _from_touch():
        """Was the mouse message being handled made up from a finger touch? Windows
        tags those (MI_WP_SIGNATURE + touch bit). A finger never hovers, so a label
        popping up after every tap would only get in the way."""
        try:
            info = ctypes.WinDLL("user32").GetMessageExtraInfo() & 0xFFFFFFFF
            return (info & 0xFFFFFF80) == 0xFF515780
        except Exception:
            return False

    def _hover(self, widget, ev):
        if self.drag or self._from_touch():
            self._tip_hide()
            return
        kid = self._kid_at(widget, ev)
        if kid == self.tip_kid:
            return
        self._tip_hide()
        self.tip_kid = kid
        if kid:
            self.tip_job = self.win.after(350, lambda: self._tip_show(kid))

    def _tip_show(self, kid):
        self.tip_job = None
        if kid != self.tip_kid or self.drag or not self.alive():
            return
        if not self.tip_win:
            t = self.tip_win = tk.Toplevel(self.win)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            self.tip_label = tk.Label(t, bg="#e8eaed", fg="#202124", font=self.font,
                                      padx=self.px(8), pady=self.px(3), bd=0)
            self.tip_label.pack()
        self.tip_label.configure(text=X.key_name(kid, self.cfg))
        px, py = self.win.winfo_pointerxy()
        self.tip_win.update_idletasks()
        tw = self.tip_win.winfo_reqwidth()
        x = min(px + self.px(12), self.win.winfo_rootx() + self.win.winfo_width() - tw - self.px(4))
        self.tip_win.geometry(f"+{max(0, x)}+{py + self.px(20)}")
        self.tip_win.deiconify()
        self.tip_win.lift()
        self.tip_job = self.win.after(5000, self._tip_hide)    # never linger

    def _tip_hide(self):
        if self.tip_job:
            try:
                self.win.after_cancel(self.tip_job)
            except Exception:
                pass
            self.tip_job = None
        if self.tip_win:
            try:
                self.tip_win.withdraw()
            except Exception:
                pass
        self.tip_kid = None

    # -- drag & drop --------------------------------------------------------------------
    def pal_down(self, ev):
        self._tip_hide()
        x, y = self.pal.canvasx(ev.x), self.pal.canvasy(ev.y)
        items = self.pal.find_overlapping(x, y, x, y)
        for it in reversed(items):
            tags = self.pal.gettags(it)
            for t in tags:
                if t.startswith("d:"):
                    self.delete_custom(t[2:])
                    return
            if "newcustom" in tags:
                self.custom_dialog()
                return
            for t in tags:
                if t.startswith("k:"):
                    self.drag = {"src": "palette", "kid": t[2:], "x": ev.x_root, "y": ev.y_root,
                                 "moved": False}
                    return
        # Empty space: drag to scroll the list. Touch no longer pans it by itself,
        # because gestures are blocked so that dragging keys works with a finger.
        self.pal.scan_mark(ev.x, ev.y)
        self.drag = {"src": "scroll"}

    def lay_down(self, ev):
        self._tip_hide()
        for kind, i, a, b, c, d, boxes in self.slot_hits:
            if a <= ev.x <= c and b <= ev.y <= d:
                self.set_active(kind, i)
                lx, ly = ev.x - a, ev.y - b
                for k, (x0, y0, x1, y1) in enumerate(boxes):
                    if x0 <= lx <= x1 and y0 <= ly <= y1:
                        self.drag = {"src": "slot", "kind": kind, "i": i, "k": k,
                                     "kid": self.cfg[kind][i]["keys"][k],
                                     "x": ev.x_root, "y": ev.y_root, "moved": False}
                        return
                return

    def any_motion(self, ev):
        d = self.drag
        if not d:
            return
        if d["src"] == "scroll":
            self.pal.scan_dragto(ev.x, ev.y, gain=1)
            return
        if not d["moved"]:
            if abs(ev.x_root - d["x"]) + abs(ev.y_root - d["y"]) < self.px(6):
                return
            d["moved"] = True
            if d["src"] == "slot":                       # lift it out of its row
                del self.cfg[d["kind"]][d["i"]]["keys"][d["k"]]
                self.changed()
            self._make_ghost(d["kid"])
        self._move_ghost(ev.x_root, ev.y_root)
        self._show_drop(ev.x_root, ev.y_root)

    def any_up(self, ev):
        d, self.drag = self.drag, None
        self._kill_ghost()
        self.lay.delete("drop")
        if not d or d["src"] == "scroll":
            return
        if not d["moved"]:
            if d["src"] == "palette":
                self.add_to_active(d["kid"])
            return
        target = self._drop_target(ev.x_root, ev.y_root)
        if target:
            kind, i, idx = target
            cap = X.MAX_KEYS_ROW if kind == "rows" else X.MAX_KEYS_COL
            keys = self.cfg[kind][i]["keys"]
            if len(keys) < cap:
                keys.insert(idx, d["kid"])
                self.active = (kind, i)
            elif d["src"] == "palette":
                messagebox.showinfo("Extra keys", "That row or column is full.", parent=self.win)
            self.changed()
        elif d["src"] == "slot":
            self.changed()                                 # dropped outside: removed

    def _make_ghost(self, kid):
        g = self.ghost = tk.Toplevel(self.win)
        g.overrideredirect(True)
        g.attributes("-topmost", True)
        try:
            g.attributes("-alpha", 0.85)
        except Exception:
            pass
        self.ghost_photo = self.chip(kid, self.px(66), self.px(40))
        tk.Label(g, image=self.ghost_photo, bd=0).pack()

    def _move_ghost(self, x, y):
        if self.ghost:
            self.ghost.geometry(f"+{x - self.px(33)}+{y - self.px(20)}")

    def _kill_ghost(self):
        if self.ghost:
            self.ghost.destroy()
            self.ghost = None

    def _drop_target(self, xr, yr):
        lx = xr - self.lay.winfo_rootx()
        ly = yr - self.lay.winfo_rooty()
        pad = self.px(10)
        for kind, i, a, b, c, d, boxes in self.slot_hits:
            if a - pad <= lx <= c + pad and b - pad <= ly <= d + pad:
                horiz = self.cfg[kind][i]["edge"] in ("top", "bottom")
                pos = (lx - a) if horiz else (ly - b)
                idx = 0
                for (x0, y0, x1, y1) in boxes:
                    centre = (x0 + x1) / 2 if horiz else (y0 + y1) / 2
                    if pos > centre:
                        idx += 1
                return kind, i, idx
        return None

    def _show_drop(self, xr, yr):
        self.lay.delete("drop")
        t = self._drop_target(xr, yr)
        if not t:
            return
        kind, i, idx = t
        for k2, i2, a, b, c, d, boxes in self.slot_hits:
            if (k2, i2) != (kind, i):
                continue
            horiz = self.cfg[kind][i]["edge"] in ("top", "bottom")
            if boxes:
                if idx < len(boxes):
                    p = boxes[idx][0] if horiz else boxes[idx][1]
                else:
                    p = boxes[-1][2] if horiz else boxes[-1][3]
            else:
                p = (c - a) // 2 if horiz else (d - b) // 2
            lw = max(3, int(self.s * 2))
            if horiz:
                self.lay.create_line(a + p, b - 4, a + p, d + 4, fill="#fdd663", width=lw, tags="drop")
            else:
                self.lay.create_line(a - 4, b + p, c + 4, b + p, fill="#fdd663", width=lw, tags="drop")

    # -- custom keys ------------------------------------------------------------------
    def custom_dialog(self):
        d = tk.Toplevel(self.win)
        d.title("New custom key")
        d.configure(bg="#202124")
        d.transient(self.win)
        d.grab_set()
        pad = self.px(10)
        body = tk.Frame(d, bg="#202124")
        body.pack(fill="both", expand=True, padx=pad, pady=pad)
        self._label(body, "Button label").pack(fill="x")
        label = tk.Entry(body, font=self.font, width=24)
        label.pack(fill="x", pady=(0, pad))
        kind = tk.StringVar(value="combo")
        rb = tk.Frame(body, bg="#202124")
        rb.pack(fill="x")
        for val, txt in (("combo", "Key combo"), ("text", "Type some text")):
            tk.Radiobutton(rb, text=txt, variable=kind, value=val, bg="#202124", fg="#e8eaed",
                           selectcolor="#3c4043", activebackground="#202124", font=self.font,
                           command=lambda: swap()).pack(side="left", padx=(0, pad))
        combo = tk.Frame(body, bg="#202124")
        mods = {m: tk.BooleanVar() for m in ("Ctrl", "Alt", "Shift", "Win")}
        mf = tk.Frame(combo, bg="#202124")
        mf.pack(fill="x", pady=(pad, 0))
        for m, v in mods.items():
            tk.Checkbutton(mf, text=m, variable=v, bg="#202124", fg="#e8eaed",
                           selectcolor="#3c4043", activebackground="#202124",
                           font=self.font).pack(side="left", padx=(0, pad))
        key = ttk.Combobox(combo, values=X.COMBO_KEY_CHOICES, state="readonly", font=self.font)
        key.set("S")
        key.pack(fill="x", pady=(pad // 2, 0))
        text = tk.Frame(body, bg="#202124")
        self._label(text, "Text to type").pack(fill="x", pady=(pad, 0))
        txt = tk.Entry(text, font=self.font)
        txt.pack(fill="x")

        def swap():
            combo.pack_forget()
            text.pack_forget()
            (combo if kind.get() == "combo" else text).pack(fill="x")
        swap()

        def add():
            lab = label.get().strip()
            if kind.get() == "combo":
                parts = [m.upper() for m, v in mods.items() if v.get()] + [key.get()]
                value = "+".join(parts)
                if not X.parse_combo(value):
                    messagebox.showerror("Extra keys", f"Can't use {value}.", parent=d)
                    return
                lab = lab or value.title().replace("+", "+")
            else:
                value = txt.get()
                if not value:
                    messagebox.showerror("Extra keys", "Enter the text to type.", parent=d)
                    return
                lab = lab or value[:8]
            used = {c["id"] for c in self.cfg["custom"]}
            n = 1
            while f"c{n}" in used:
                n += 1
            self.cfg["custom"].append({"id": f"c{n}", "label": lab[:24], "type": kind.get(),
                                       "value": value})
            d.destroy()
            self.changed()

        btns = tk.Frame(body, bg="#202124")
        btns.pack(fill="x", pady=(pad, 0))
        self._button(btns, "Add key", add, primary=True).pack(side="right")
        self._button(btns, "Cancel", d.destroy).pack(side="right", padx=(0, pad))
        label.focus_set()

    def delete_custom(self, kid):
        c = next((x for x in self.cfg["custom"] if x["id"] == kid), None)
        if not c or not messagebox.askyesno("Extra keys", f"Delete the custom key '{c['label']}'?",
                                            parent=self.win):
            return
        self.cfg["custom"] = [x for x in self.cfg["custom"] if x["id"] != kid]
        for kind in ("rows", "cols"):
            for b in self.cfg[kind]:
                b["keys"] = [k for k in b["keys"] if k != kid]
        self.changed()

    # -- look ---------------------------------------------------------------------------
    def _build_theme(self):
        f = self.theme_frame = tk.Frame(self.win, bg="#202124")
        f.grid(row=0, column=2, sticky="ns", padx=(self.px(6), self.px(12)), pady=self.px(12))
        self._label(f, "Look", self.font_h).pack(fill="x")
        self.mode = tk.StringVar()
        for val, txt in (("auto", "Match my Gboard theme"), ("solid", "Solid colour"),
                         ("gradient", "Gradient")):
            tk.Radiobutton(f, text=txt, variable=self.mode, value=val, bg="#202124", fg="#e8eaed",
                           selectcolor="#3c4043", activebackground="#202124", font=self.font,
                           anchor="w", command=self.on_mode).pack(fill="x")
        self._label(f, "Matching reads your current Gboard theme,\nincluding picture themes.",
                    self.font_small, fg="#9aa0a6").pack(fill="x", pady=(self.px(2), self.px(6)))
        self.reread = self._button(f, "Re-read Gboard theme", self.reread_theme)
        self.reread.pack(anchor="w")
        cf = tk.Frame(f, bg="#202124")
        cf.pack(fill="x", pady=(self.px(10), 0))
        self.c1 = self._button(cf, "Colour 1", lambda: self.pick("c1"))
        self.c1.pack(side="left")
        self.c2 = self._button(cf, "Colour 2", lambda: self.pick("c2"))
        self.c2.pack(side="left", padx=(self.px(6), 0))
        self.direction = tk.StringVar()
        df = tk.Frame(f, bg="#202124")
        df.pack(fill="x", pady=(self.px(4), 0))
        self.dir_btns = []
        for val, txt in (("v", "Top to bottom"), ("h", "Left to right")):
            r = tk.Radiobutton(df, text=txt, variable=self.direction, value=val, bg="#202124",
                               fg="#e8eaed", selectcolor="#3c4043", activebackground="#202124",
                               font=self.font, command=self.on_dir)
            r.pack(side="left", padx=(0, self.px(8)))
            self.dir_btns.append(r)
        self._label(f, "Text colour", self.font_b).pack(fill="x", pady=(self.px(12), 0))
        tf = tk.Frame(f, bg="#202124")
        tf.pack(fill="x")
        self.textmode = tk.StringVar()
        for val, txt in (("auto", "Automatic"), ("custom", "Custom")):
            tk.Radiobutton(tf, text=txt, variable=self.textmode, value=val, bg="#202124",
                           fg="#e8eaed", selectcolor="#3c4043", activebackground="#202124",
                           font=self.font, command=self.on_textmode).pack(side="left", padx=(0, self.px(8)))
        self._label(f, "Key size", self.font_b).pack(fill="x", pady=(self.px(12), 0))
        self.size = tk.Scale(f, from_=70, to=150, orient="horizontal", bg="#202124", fg="#e8eaed",
                             troughcolor="#3c4043", highlightthickness=0, font=self.font_small,
                             length=self.px(220), command=self.on_size)
        self.size.pack(fill="x")
        self.editbtn = tk.BooleanVar()
        tk.Checkbutton(f, text="Editor button on the keyboard", variable=self.editbtn,
                       bg="#202124", fg="#e8eaed", selectcolor="#3c4043",
                       activebackground="#202124", font=self.font, anchor="w",
                       command=self.on_editbtn).pack(fill="x", pady=(self.px(12), 0))
        self._label(f, "A small button above the keyboard that\nopens this window.",
                    self.font_small, fg="#9aa0a6").pack(fill="x")
        tk.Frame(f, bg="#202124").pack(fill="both", expand=True)
        bf = tk.Frame(f, bg="#202124")
        bf.pack(fill="x")
        self._button(bf, "Save", self.save, primary=True).pack(side="right")
        self._button(bf, "Cancel", self.cancel).pack(side="right", padx=(0, self.px(6)))

    def sync_theme_controls(self):
        th = self.cfg["theme"]
        self.mode.set(th["mode"])
        self.direction.set(th["dir"])
        self.textmode.set("auto" if th["text"] == "auto" else "custom")
        self.size.set(int(round(self.cfg.get("scale", 1.0) * 100)))
        self.editbtn.set(bool(self.cfg.get("editbtn", True)))
        custom = th["mode"] != "auto"
        self.c1.configure(state="normal" if custom else "disabled",
                          bg=th["c1"] if custom else "#3c4043")
        grad = th["mode"] == "gradient"
        self.c2.configure(state="normal" if grad else "disabled", bg=th["c2"] if grad else "#3c4043")
        for r in self.dir_btns:
            r.configure(state="normal" if grad else "disabled")
        self.reread.configure(state="normal" if th["mode"] == "auto" else "disabled")

    def on_mode(self):
        self.cfg["theme"]["mode"] = self.mode.get()
        self.changed()

    def on_dir(self):
        self.cfg["theme"]["dir"] = self.direction.get()
        self.changed()

    def on_textmode(self):
        if self.textmode.get() == "auto":
            self.cfg["theme"]["text"] = "auto"
            self.changed()
        else:
            got = colorchooser.askcolor(parent=self.win, title="Text colour")
            if got and got[1]:
                self.cfg["theme"]["text"] = got[1]
                self.changed()
            else:
                self.sync_theme_controls()

    def on_size(self, val):
        v = int(float(val)) / 100.0
        if abs(v - self.cfg.get("scale", 1.0)) >= 0.01:
            self.cfg["scale"] = v
            self.changed()

    def on_editbtn(self):
        self.cfg["editbtn"] = bool(self.editbtn.get())
        self.changed()

    def pick(self, which):
        got = colorchooser.askcolor(color=self.cfg["theme"][which], parent=self.win,
                                    title="Choose a colour")
        if got and got[1]:
            self.cfg["theme"][which] = got[1]
            self.changed()

    def reread_theme(self):
        self.reread.configure(state="disabled", text="Reading...")

        def work():
            ok = X.THEME.capture(force=True) if X.THEME else False
            self.win.after(0, lambda: (self.reread.configure(text="Re-read Gboard theme"),
                                       self.refresh_all(),
                                       None if ok else messagebox.showinfo(
                                           "Extra keys", "Show the keyboard first, then try again.",
                                           parent=self.win)))
        threading.Thread(target=work, daemon=True).start()

    # -- save / cancel --------------------------------------------------------------------
    def save(self):
        X.CONFIG.publish(self.cfg, save=True)
        self.dirty = False
        self.win.destroy()

    def cancel(self):
        X.CONFIG.publish(self.orig, save=False)
        self.dirty = False
        self.win.destroy()

    def on_close(self):
        if not self.dirty:
            self.win.destroy()
            return
        ans = messagebox.askyesnocancel("Extra keys", "Save your changes?", parent=self.win)
        if ans is None:
            return
        self.save() if ans else self.cancel()
