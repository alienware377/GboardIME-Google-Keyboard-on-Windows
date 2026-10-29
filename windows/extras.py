"""Extra key rows and columns around the Gboard keyboard.

Adds up to 3 rows (above or below the keyboard) and 2 columns (left or right) of
keys Gboard does not have: Ctrl, Alt, PC Shift, Win, Fn, Tab, Esc, arrows, Home/End,
PgUp/PgDn, F-keys, cut/copy/paste and user-defined combos or text snippets.

How it fits together:
  * Bars are small Tk windows glued to the emulator window. Top rows sit directly on
    the top edge of the Gboard keyboard (inside the emulator window, over the empty
    relay area), bottom rows directly under it, columns beside it. They never take
    focus, so keys land in the Windows app the user was typing into.
  * Ctrl / Alt / Shift / Win / Fn are STICKY: tap once and they apply to the next
    key, tap again to lock them, tap a third time to release. They also apply to the
    next key typed on Gboard itself, because the host routes Gboard's input through
    handle_text / handle_del / handle_key first. After a combo the relay app is told
    to CLEAR, which drops Gboard's half-composed word so it is never re-sent later.
  * The look is matched to the live Gboard theme from an adb screenshot: the strip of
    background along each edge of the keyboard is stretched behind the bar (so image
    themes continue across), and keys are drawn as translucent tiles like Gboard's.
    A custom solid colour or gradient can be chosen instead.
  * A drag & drop builder edits the layout; a one-time banner announces the feature.

The host hands its helpers to init(); this module never imports gboard_host (it runs
as __main__, so importing it would start a second copy of the host).
"""
import io
import json
import os
import queue
import re
import subprocess
import threading
import time
import ctypes
import ctypes.wintypes as wt

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:                     # the host already depends on pillow (tray icon)
    Image = None

API = {}                              # filled by init()
FEATURE_TAG = "extras-1"              # bump to show the what's-new banner again
MAX_ROWS = 3
MAX_COLS = 2
MAX_KEYS_ROW = 16
MAX_KEYS_COL = 12

# A PRIVATE user32 handle. ctypes.windll.user32 hands every caller the same function
# objects, and the host sets argtypes on them with its own RECT/POINT classes, so
# passing ours through the shared copy raised "expected LP__RECT instance" on every
# call. A separate WinDLL gets separate function objects with their own argtypes.
u32 = ctypes.WinDLL("user32", use_last_error=True)
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "GboardIME")
THEME_DIR = os.path.join(STATE_DIR, "theme")


def log(msg):
    try:
        API["log"](f"[extras] {msg}")
    except Exception:
        pass


# ── Key catalogue ────────────────────────────────────────────────────────────
WINLOGO = "WINLOGO"     # drawn as the four-pane Windows logo instead of a glyph

# (id, label, icon, kind, value, group). kind: mod | fn | key | combo
CATALOG = [
    ("ctrl",  "Ctrl",  None, "mod", "ctrl",  "Modifiers"),
    ("alt",   "Alt",   None, "mod", "alt",   "Modifiers"),
    ("shift", "Shift", None, "mod", "shift", "Modifiers"),
    ("win",   "Win",   WINLOGO, "mod", "win", "Modifiers"),
    ("fn",    "Fn",    None, "fn",  "fn",    "Modifiers"),

    ("tab",    "Tab",   "\uE7FD", "key",   "TAB",       "Editing"),
    ("esc",    "Esc",   None,     "key",   "ESC",       "Editing"),
    ("enter",  "Enter", None,     "key",   "ENTER",     "Editing"),
    ("bksp",   "Bksp",  "\uE750", "key",   "BACKSPACE", "Editing"),
    ("del",    "Del",   None,     "key",   "DELETE",    "Editing"),
    ("ins",    "Ins",   None,     "key",   "INSERT",    "Editing"),
    ("cut",    "Cut",   "\uE8C6", "combo", "CTRL+X",    "Editing"),
    ("copy",   "Copy",  "\uE8C8", "combo", "CTRL+C",    "Editing"),
    ("paste",  "Paste", "\uE77F", "combo", "CTRL+V",    "Editing"),
    ("undo",   "Undo",  "\uE7A7", "combo", "CTRL+Z",    "Editing"),
    ("redo",   "Redo",  "\uE7A6", "combo", "CTRL+Y",    "Editing"),
    ("selall", "All",   "\uE8B3", "combo", "CTRL+A",    "Editing"),

    ("left",  "Left",  "\uE72B", "key", "LEFT",     "Navigation"),
    ("up",    "Up",    "\uE74A", "key", "UP",       "Navigation"),
    ("down",  "Down",  "\uE74B", "key", "DOWN",     "Navigation"),
    ("right", "Right", "\uE72A", "key", "RIGHT",    "Navigation"),
    ("home",  "Home",  None,     "key", "HOME",     "Navigation"),
    ("end",   "End",   None,     "key", "END",      "Navigation"),
    ("pgup",  "PgUp",  None,     "key", "PAGEUP",   "Navigation"),
    ("pgdn",  "PgDn",  None,     "key", "PAGEDOWN", "Navigation"),

    ("caps",   "Caps",  None,     "key", "CAPSLOCK",   "System"),
    ("numlk",  "NumLk", None,     "key", "NUMLOCK",    "System"),
    ("scrlk",  "ScrLk", None,     "key", "SCROLLLOCK", "System"),
    ("menu",   "Menu",  "\uE8FD", "key", "APPS",       "System"),
    ("start",  "Start", None,     "key", "LWIN",       "System"),
    ("prtsc",  "PrtSc", None,     "key", "PRTSC",      "System"),
    ("pause",  "Pause", None,     "key", "PAUSE",      "System"),
] + [(f"f{n}", f"F{n}", None, "key", f"F{n}", "Function keys") for n in range(1, 13)]

# What each key is called when you hover it in the editor (icons alone can be cryptic).
KEY_NAMES = {
    "ctrl": "Ctrl (tap: next key only, double-tap: lock)",
    "alt": "Alt (tap: next key only, double-tap: lock)",
    "shift": "PC Shift for shortcuts (tap: next key only, double-tap: lock)",
    "win": "Windows key for shortcuts like Win+E (tap: next key only, double-tap: lock)",
    "fn": "Fn: number keys become F1 to F12, arrows become PgUp/PgDn/Home/End",
    "tab": "Tab", "esc": "Escape", "enter": "Enter", "bksp": "Backspace",
    "del": "Delete", "ins": "Insert",
    "cut": "Cut (Ctrl+X)", "copy": "Copy (Ctrl+C)", "paste": "Paste (Ctrl+V)",
    "undo": "Undo (Ctrl+Z)", "redo": "Redo (Ctrl+Y)", "selall": "Select all (Ctrl+A)",
    "left": "Left arrow", "up": "Up arrow", "down": "Down arrow", "right": "Right arrow",
    "home": "Home", "end": "End", "pgup": "Page Up", "pgdn": "Page Down",
    "caps": "Caps Lock", "numlk": "Num Lock", "scrlk": "Scroll Lock",
    "menu": "Menu key (opens the right-click menu)", "start": "Start menu",
    "prtsc": "Print Screen", "pause": "Pause / Break",
}

GROUPS = ["Modifiers", "Editing", "Navigation", "System", "Function keys", "Custom"]
REPEATABLE = {"BACKSPACE", "DELETE", "LEFT", "RIGHT", "UP", "DOWN", "PAGEUP", "PAGEDOWN"}
TOGGLE_VK = {"caps": 0x14, "numlk": 0x90, "scrlk": 0x91}
_CAT = {c[0]: c for c in CATALOG}

# Named keys -> (virtual key, extended flag)
NAMED_VK = {
    "ESC": (0x1B, False), "TAB": (0x09, False), "ENTER": (0x0D, False),
    "BACKSPACE": (0x08, False), "SPACE": (0x20, False),
    "DELETE": (0x2E, True), "INSERT": (0x2D, True), "HOME": (0x24, True), "END": (0x23, True),
    "PAGEUP": (0x21, True), "PAGEDOWN": (0x22, True),
    "LEFT": (0x25, True), "UP": (0x26, True), "RIGHT": (0x27, True), "DOWN": (0x28, True),
    "CAPSLOCK": (0x14, False), "NUMLOCK": (0x90, True), "SCROLLLOCK": (0x91, False),
    "PRTSC": (0x2C, True), "PAUSE": (0x13, False), "APPS": (0x5D, True), "LWIN": (0x5B, True),
}
for _n in range(1, 25):
    NAMED_VK[f"F{_n}"] = (0x70 + _n - 1, False)
MOD_VK = {"ctrl": (0x11, False), "alt": (0x12, False), "shift": (0x10, False), "win": (0x5B, True)}
MOD_ALIASES = {"CTRL": "ctrl", "CONTROL": "ctrl", "ALT": "alt", "SHIFT": "shift", "WIN": "win"}
MOD_ORDER = ["ctrl", "alt", "shift", "win"]
FN_MAP = {"1": "F1", "2": "F2", "3": "F3", "4": "F4", "5": "F5", "6": "F6", "7": "F7",
          "8": "F8", "9": "F9", "0": "F10", "-": "F11", "=": "F12"}
FN_NAV = {"UP": "PAGEUP", "DOWN": "PAGEDOWN", "LEFT": "HOME", "RIGHT": "END"}
COMBO_KEY_CHOICES = ([chr(c) for c in range(ord("A"), ord("Z") + 1)] +
                     [str(d) for d in range(10)] + [f"F{n}" for n in range(1, 25)] +
                     ["ESC", "TAB", "ENTER", "SPACE", "BACKSPACE", "DELETE", "INSERT", "HOME",
                      "END", "PAGEUP", "PAGEDOWN", "LEFT", "RIGHT", "UP", "DOWN", "APPS",
                      "PRTSC", "PAUSE", "CAPSLOCK", "NUMLOCK", "SCROLLLOCK",
                      "-", "=", "[", "]", ";", "'", ",", ".", "/", "\\", "`"])


def key_name(kid, cfg=None):
    """Friendly name for a key, for the editor's hover label."""
    if kid in KEY_NAMES:
        return KEY_NAMES[kid]
    kd = keydef(kid, cfg)
    if not kd:
        return kid
    if kid.startswith("c"):                        # custom key
        what = f'types "{kd[4]}"' if kd[3] == "text" else kd[4].title()
        return f"{kd[1]}: {what}"
    return kd[1]


def keydef(kid, cfg=None):
    """Return a catalogue-shaped tuple for a preset id or a custom key id."""
    if kid in _CAT:
        return _CAT[kid]
    for c in (cfg or CONFIG.get())["custom"]:
        if c.get("id") == kid:
            kind = "text" if c.get("type") == "text" else "combo"
            return (kid, c.get("label") or "?", None, kind, c.get("value", ""), "Custom")
    return None


# ── Configuration ────────────────────────────────────────────────────────────
DEFAULT_THEME = {"mode": "auto", "c1": "#3b2a7a", "c2": "#c2549f", "dir": "v", "text": "auto"}


def _default_cfg():
    return {"rows": [], "cols": [], "custom": [], "theme": dict(DEFAULT_THEME), "scale": 1.0,
            "editbtn": True}


def _clean_cfg(c):
    """Validate a loaded config. A damaged file must never break the keyboard."""
    out = _default_cfg()
    if not isinstance(c, dict):
        return out
    for c_ in c.get("custom", []) if isinstance(c.get("custom"), list) else []:
        if isinstance(c_, dict) and isinstance(c_.get("id"), str) and c_["id"].startswith("c"):
            out["custom"].append({"id": c_["id"], "label": str(c_.get("label", "?"))[:24],
                                  "type": "text" if c_.get("type") == "text" else "combo",
                                  "value": str(c_.get("value", ""))[:200]})
    for key, edges, cap, kcap in (("rows", ("top", "bottom"), MAX_ROWS, MAX_KEYS_ROW),
                                  ("cols", ("left", "right"), MAX_COLS, MAX_KEYS_COL)):
        for b in (c.get(key) or [])[:cap]:
            if isinstance(b, dict) and b.get("edge") in edges:
                keys = [k for k in (b.get("keys") or []) if isinstance(k, str)
                        and keydef(k, out)][:kcap]
                out[key].append({"edge": b["edge"], "keys": keys})
    t = c.get("theme") if isinstance(c.get("theme"), dict) else {}
    th = dict(DEFAULT_THEME)
    if t.get("mode") in ("auto", "solid", "gradient"):
        th["mode"] = t["mode"]
    for k in ("c1", "c2"):
        if isinstance(t.get(k), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", t[k]):
            th[k] = t[k]
    if t.get("dir") in ("v", "h"):
        th["dir"] = t["dir"]
    if t.get("text") == "auto" or (isinstance(t.get("text"), str)
                                   and re.fullmatch(r"#[0-9a-fA-F]{6}", t["text"])):
        th["text"] = t["text"]
    out["theme"] = th
    try:
        out["scale"] = max(0.7, min(1.5, float(c.get("scale", 1.0))))
    except Exception:
        pass
    if isinstance(c.get("editbtn"), bool):
        out["editbtn"] = c["editbtn"]
    return out


class _Config:
    """Copy-on-write config shared between the Tk thread and the dispatch thread."""

    def __init__(self):
        self._cfg = _default_cfg()
        self._lock = threading.Lock()
        self.version = 0

    def load(self):
        try:
            raw = API["settings_load"]().get("extras")
            self._cfg = _clean_cfg(raw) if raw else _default_cfg()
        except Exception as e:
            log(f"config load failed: {e}")
            self._cfg = _default_cfg()

    def get(self):
        return self._cfg

    def publish(self, cfg, save=True):
        with self._lock:
            self._cfg = _clean_cfg(cfg)
            self.version += 1
        if save:
            try:
                API["settings_set"]("extras", self._cfg)
            except Exception as e:
                log(f"config save failed: {e}")
        try:
            API["reposition"]()
        except Exception:
            pass


CONFIG = _Config()


def count_edges(cfg):
    n = {"top": 0, "bottom": 0, "left": 0, "right": 0}
    for b in cfg["rows"] + cfg["cols"]:
        n[b["edge"]] += 1
    return n


def sizes(scale, cfg=None):
    cfg = cfg or CONFIG.get()
    k = cfg.get("scale", 1.0)
    return int(round(38 * scale * k)), int(round(50 * scale * k)), max(2, int(round(3 * scale)))


def corner_radius(scale, cfg=None):
    """Outer-corner radius for a side column: the key's own corner radius plus the gap
    around it, so the window's curve runs parallel to the outermost keys' corners."""
    _, col_w, gap = sizes(scale, cfg)
    return max(3, int((col_w - 2 * gap) * 0.2)) + gap


def get_extents(scale, bar_h):
    """Physical px the extras (and the bottom title bar) add on each side of the
    emulator window, so docking keeps everything inside the work area."""
    cfg = CONFIG.get()
    row_h, col_w, _ = sizes(scale, cfg)
    n = count_edges(cfg)
    return n["left"] * col_w, 0, n["right"] * col_w, n["bottom"] * row_h + bar_h


def bottom_offset(scale):
    """How far below the emulator the bottom title bar must sit (bottom rows)."""
    cfg = CONFIG.get()
    row_h, _, _ = sizes(scale, cfg)
    return count_edges(cfg)["bottom"] * row_h


# ── Sticky modifiers ─────────────────────────────────────────────────────────
class _Mods:
    """0 = off, 1 = next key only, 2 = locked. Shared by the bar (Tk thread) and the
    host's dispatch thread (keys typed on Gboard)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"ctrl": 0, "alt": 0, "shift": 0, "win": 0, "fn": 0}
        self.last_tap = {}
        self.version = 0

    def tap(self, name):
        """Tap: next key only. A quick double-tap (within Windows' double-click time)
        locks it. A slow second tap, or a tap on a locked key, turns it off, so two
        separate taps never lock by accident."""
        now = time.monotonic()
        with self.lock:
            cur = self.state[name]
            quick = now - self.last_tap.get(name, -9.0) <= _double_tap_s()
            if cur == 0:
                self.state[name] = 1
            elif cur == 1 and quick:
                self.state[name] = 2
            else:
                self.state[name] = 0
            self.last_tap[name] = now
            self.version += 1

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def consume(self):
        """Release every 'next key only' modifier after it has been used."""
        with self.lock:
            changed = False
            for k, v in self.state.items():
                if v == 1:
                    self.state[k] = 0
                    changed = True
            if changed:
                self.version += 1

    def clear(self):
        with self.lock:
            if any(self.state.values()):
                self.state = {k: 0 for k in self.state}
                self.version += 1


MODS = _Mods()


def _double_tap_s():
    try:
        return max(0.2, min(0.9, u32.GetDoubleClickTime() / 1000.0))
    except Exception:
        return 0.5


def _active(state):
    return [m for m in MOD_ORDER if state.get(m)]


# ── Key injection ────────────────────────────────────────────────────────────
_injq = queue.Queue()


def _inject_worker():
    while True:
        fn = _injq.get()
        try:
            API["inject_into_target"](fn)
        except Exception as e:
            log(f"inject failed: {e}")


def _press(vk, ext, up=False):
    flags = (API["KEYEVENTF_EXTENDEDKEY"] if ext else 0) | (API["KEYEVENTF_KEYUP"] if up else 0)
    return API["make_ki"](vk=vk, flags=flags)


def send_combo(mods, vk, ext, repeat=1):
    """Hold the modifiers, tap the key, release in reverse. ONE SendInput per tap so
    nothing can interleave between the modifier and the key."""
    seq = []
    for _ in range(max(1, repeat)):
        seq += [_press(*MOD_VK[m]) for m in mods]
        seq += [_press(vk, ext), _press(vk, ext, up=True)]
        seq += [_press(*MOD_VK[m], up=True) for m in reversed(mods)]
    API["send"](*seq)


def _vk_for_char(ch):
    """Virtual key for a character on the TARGET window's keyboard layout."""
    try:
        fg = u32.GetForegroundWindow()
        tid = u32.GetWindowThreadProcessId(fg, None)
        hkl = u32.GetKeyboardLayout(tid)
        r = u32.VkKeyScanExW(ctypes.c_wchar(ch), hkl)
    except Exception:
        r = u32.VkKeyScanW(ctypes.c_wchar(ch))
    r &= 0xFFFF
    if r == 0xFFFF:
        return None
    return r & 0xFF, (r >> 8) & 0xFF


def parse_combo(spec):
    """'CTRL+SHIFT+T' -> (['ctrl','shift'], vk, ext). Returns None if unknown."""
    parts = [p.strip() for p in spec.split("+") if p.strip()]
    if not parts:
        return None
    mods = []
    for p in parts[:-1]:
        m = MOD_ALIASES.get(p.upper())
        if m and m not in mods:
            mods.append(m)
    key = parts[-1]
    up = key.upper()
    if up in NAMED_VK:
        vk, ext = NAMED_VK[up]
        return mods, vk, ext
    if len(key) == 1:
        if key.isalnum():
            return mods, ord(key.upper()), False
        r = _vk_for_char(key)
        if r:
            vk, sh = r
            if sh & 1 and "shift" not in mods:
                mods.append("shift")
            return mods, vk, False
    return None


def _after_combo():
    """After a combo: drop Gboard's composing word so it is never re-sent against a
    field the combo just changed, and let 'next key only' modifiers go."""
    MODS.consume()
    try:
        API["send_to_android"]("CLEAR")
    except Exception:
        pass


def handle_text(text):
    """Called by the host for TEXT: from Gboard. Returns True if handled here (a
    modifier or Fn applied), False to let the host type the text normally."""
    st = MODS.snapshot()
    mods = _active(st)
    fn = bool(st["fn"])
    if not mods and not fn:
        return False
    if len(text) != 1:
        # A whole swipe-typed word or an autocorrect commit: type it normally.
        MODS.consume()
        return False
    ch = text
    if fn and ch in FN_MAP:
        vk, ext = NAMED_VK[FN_MAP[ch]]
        _injq.put(lambda: send_combo(mods, vk, ext))
        _after_combo()
        return True
    if not mods:                                   # Fn alone, not a digit row key
        MODS.consume()
        return False
    named = {"\n": "ENTER", "\t": "TAB", " ": "SPACE"}.get(ch)
    if named:
        vk, ext = NAMED_VK[named]
        use = list(mods)
    else:
        r = _vk_for_char(ch)
        if not r:                                  # not on this layout (emoji etc.)
            MODS.consume()
            return False
        vk, sh = r
        ext = False
        use = list(mods)
        # Letters: Gboard's own capitalisation must not turn Ctrl+C into Ctrl+Shift+C.
        # Only the PC Shift key adds Shift to a letter. Symbols keep the Shift their
        # layout needs (Ctrl and '+' is Ctrl+Shift+= on US, like a real keyboard).
        if not ch.isalpha() and (sh & 1) and "shift" not in use:
            use.append("shift")
    _injq.put(lambda: send_combo(use, vk, ext))
    _after_combo()
    return True


def handle_del(n):
    st = MODS.snapshot()
    mods = _active(st)
    if not mods:
        return False
    vk, ext = NAMED_VK["BACKSPACE"]
    _injq.put(lambda: send_combo(mods, vk, ext, repeat=n))
    _after_combo()
    return True


def handle_key(spec):
    """KEY:<spec> from Gboard (Enter, Tab, cursor arrows...)."""
    st = MODS.snapshot()
    mods = _active(st)
    if not mods and not st["fn"]:
        return False
    base, _, rep = spec.partition("*")
    try:
        repeat = max(1, min(1000, int(rep))) if rep else 1
    except ValueError:
        repeat = 1
    parsed = parse_combo(base)
    if not parsed:
        return False
    pm, vk, ext = parsed
    if st["fn"]:
        last = base.split("+")[-1].strip().upper()
        if last in FN_NAV:
            vk, ext = NAMED_VK[FN_NAV[last]]
    use = [m for m in MOD_ORDER if m in mods or m in pm]
    _injq.put(lambda: send_combo(use, vk, ext, repeat))
    _after_combo()
    return True


def activate_key(kid):
    """A key on one of our bars was pressed."""
    kd = keydef(kid)
    if not kd:
        return
    _, _, _, kind, value, _ = kd
    if kind in ("mod", "fn"):
        MODS.tap(value)
        return
    st = MODS.snapshot()
    mods = _active(st)
    if kind == "text":
        txt = value
        _injq.put(lambda: API["inject_text"](txt))
        _after_combo()
        return
    if kind == "key":
        name = value
        if st["fn"] and name in FN_NAV:
            name = FN_NAV[name]
        vk, ext = NAMED_VK[name]
        _injq.put(lambda: send_combo(mods, vk, ext))
        _after_combo()
        return
    parsed = parse_combo(value)
    if not parsed:
        log(f"cannot parse combo {value!r}")
        return
    pm, vk, ext = parsed
    use = [m for m in MOD_ORDER if m in mods or m in pm]
    _injq.put(lambda: send_combo(use, vk, ext))
    _after_combo()


# ── Keyboard geometry inside the emulator ────────────────────────────────────
def _adb(*args, timeout=6, text=True):
    """Run adb on the emulator hidden and bounded: (rc, out, err), rc None on timeout.
    Uses the host's runner when it handed one over, so every adb start goes through
    one place that always passes CREATE_NO_WINDOW."""
    run = API.get("run_adb")
    if run:
        return run(*args, timeout=timeout, text=text)
    kw = {"encoding": "utf-8", "errors": "replace"} if text else {}
    try:
        r = subprocess.run([API["adb_path"], "-s", "emulator-5554", *args],
                           capture_output=True, text=text, timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=NOWIN, **kw)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return None, "" if text else b"", "timeout"
    except Exception as e:
        return None, "" if text else b"", str(e)


class _KbGeom:
    """Where the Gboard keyboard sits on the Android screen (device px). Read from
    the IME window's touchable region; refreshed in the background while visible."""

    def __init__(self):
        self.top = None
        self.height = None
        self.at = 0.0

    def refresh(self):
        try:
            rc, out, _ = _adb("shell", "dumpsys window InputMethod")
            if rc != 0:
                return False                  # adb itself failed
            m = re.search(r"touchable region=SkRegion\(\((\d+),(\d+),(\d+),(\d+)\)", out)
            if m:
                top, bottom = int(m.group(2)), int(m.group(4))
                if 0 < top < bottom:
                    self.top, self.height = top, bottom
                    self.at = time.time()
                    return True
            return None                       # adb fine, Gboard just not on screen
        except Exception:
            pass
        return False


KB = _KbGeom()


def _geom_thread():
    fails = 0
    while True:
        try:
            visible = STATE.get("visible")
            if visible or KB.top is None:
                got = KB.refresh()
                if got is None:
                    fails = 0                          # adb answers; keyboard just hidden
                elif got:
                    if fails >= 3:
                        log("keyboard position readable again")
                    fails = 0
                else:
                    fails += 1
                    if fails == 3:
                        log("can't read the keyboard position from the emulator; backing off")
            if visible and THEME and THEME.wants_capture():
                THEME.capture()
        except Exception as e:
            log(f"geometry thread: {e}")
        base = 2.0 if STATE.get("visible") else 5.0
        time.sleep(base if fails < 3 else min(30.0, base * 2 ** min(fails - 2, 4)))


STATE = {"visible": False}


# ── Theme ────────────────────────────────────────────────────────────────────
def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _lum(rgb):
    r, g, b = [x / 255.0 for x in rgb[:3]]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _sat(rgb):
    mx, mn = max(rgb[:3]), min(rgb[:3])
    return 0 if mx == 0 else (mx - mn) / mx


class _Theme:
    """Gboard's live look, sampled from a screenshot: the background strip along each
    keyboard edge, whether it is dark, and the accent colour of its Enter key."""

    def __init__(self):
        self.strips = {}          # edge -> PIL image strip
        self.dark = True
        self.accent = (138, 180, 248)
        self.at = 0.0
        self.fail_at = 0.0
        self.version = 0
        self.lock = threading.Lock()
        self._load_cache()

    def _load_cache(self):
        if Image is None:
            return
        try:
            meta = json.load(open(os.path.join(THEME_DIR, "theme.json"), encoding="utf-8"))
            for e in ("left", "right", "top", "bottom"):
                p = os.path.join(THEME_DIR, f"{e}.png")
                if os.path.exists(p):
                    self.strips[e] = Image.open(p).convert("RGB")
            self.dark = bool(meta.get("dark", True))
            self.accent = tuple(meta.get("accent", self.accent))[:3]
            self.at = float(meta.get("at", 0))
            self.version += 1
        except Exception:
            pass

    @property
    def busy(self):
        return self.lock.locked()

    def wants_capture(self):
        if self.busy or CONFIG.get()["theme"]["mode"] != "auto":
            return False
        if time.time() - self.fail_at < 60:           # a try just failed; don't hammer adb
            return False
        if not (CONFIG.get()["rows"] or CONFIG.get()["cols"]):
            return False
        return time.time() - self.at > 600            # Gboard themes change rarely

    def capture(self, force=False):
        """force=True (the editor's Re-read button) waits for a capture that is
        already running and then reads again, rather than reporting a failure."""
        if Image is None:
            return False
        if not (self.lock.acquire(timeout=30) if force else self.lock.acquire(blocking=False)):
            return False
        ok = False
        try:
            ok = self._capture()
            return ok
        finally:
            if not ok:
                self.fail_at = time.time()
            self.lock.release()

    def _capture(self):
        try:
            if KB.top is None:
                KB.refresh()
            if KB.top is None:
                return False
            rc, ime, _ = _adb("shell", "dumpsys input_method")
            if rc != 0 or "mInputShown=true" not in ime:
                return False                           # keyboard not drawn right now
            rc, png, _ = _adb("exec-out", "screencap", "-p", timeout=10, text=False)
            if rc != 0 or not png:
                return False
            img = Image.open(io.BytesIO(png)).convert("RGB")
            W, H = img.size
            top = min(max(0, KB.top), H - 8)
            band = 6
            strips = {
                "left": img.crop((0, top, band, H)).resize((1, H - top), Image.BOX),
                "right": img.crop((W - band, top, W, H)).resize((1, H - top), Image.BOX),
                "top": img.crop((0, top, W, top + band)).resize((W, 1), Image.BOX),
                "bottom": img.crop((0, H - band, W, H)).resize((W, 1), Image.BOX),
            }
            avg = img.crop((0, top, W, H)).resize((1, 1), Image.BOX).getpixel((0, 0))
            dark = _lum(avg) < 0.5
            accent = None
            kh = H - top
            best = 0.0
            for fx, fy in ((0.92, 0.895), (0.93, 0.88), (0.91, 0.91), (0.92, 0.86)):
                x, y = int(W * fx), int(top + kh * fy)
                px = img.crop((x - 6, y - 6, x + 6, y + 6)).resize((1, 1), Image.BOX).getpixel((0, 0))
                s = _sat(px)
                if s > best and s > 0.3:
                    best, accent = s, px
            os.makedirs(THEME_DIR, exist_ok=True)
            for e, s in strips.items():
                s.save(os.path.join(THEME_DIR, f"{e}.png"))
            self.strips, self.dark = strips, dark
            if accent:
                self.accent = accent
            self.at = time.time()
            json.dump({"dark": dark, "accent": list(self.accent), "at": self.at},
                      open(os.path.join(THEME_DIR, "theme.json"), "w", encoding="utf-8"))
            self.version += 1
            log(f"theme captured: dark={dark} accent={self.accent}")
            return True
        except Exception as e:
            log(f"theme capture failed: {e}")
            return False


THEME = None   # created in init() (needs Image)


def _gradient(w, h, c1, c2, direction):
    a, b = _hex(c1), _hex(c2)
    if direction == "h":
        strip = Image.new("RGB", (max(2, w), 1))
        for x in range(strip.width):
            t = x / (strip.width - 1)
            strip.putpixel((x, 0), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    else:
        strip = Image.new("RGB", (1, max(2, h)))
        for y in range(strip.height):
            t = y / (strip.height - 1)
            strip.putpixel((0, y), tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))
    return strip.resize((w, h), Image.BILINEAR)


def background(edge, w, h, cfg=None):
    """Background image for a bar on the given edge, plus whether it reads as dark."""
    th = (cfg or CONFIG.get())["theme"]
    if th["mode"] == "solid":
        bg = Image.new("RGB", (w, h), _hex(th["c1"]))
    elif th["mode"] == "gradient":
        bg = _gradient(w, h, th["c1"], th["c2"], th["dir"])
    else:
        s = THEME.strips.get(edge) if THEME else None
        if s is not None:
            bg = s.resize((w, h), Image.BILINEAR)
        else:
            bg = Image.new("RGB", (w, h), (32, 33, 36))     # Gboard dark grey
    avg = bg.resize((1, 1), Image.BOX).getpixel((0, 0))
    return bg, _lum(avg) < 0.5


def text_color(dark, cfg=None):
    t = (cfg or CONFIG.get())["theme"]["text"]
    if t != "auto":
        return _hex(t)
    return (255, 255, 255) if dark else (32, 33, 36)


# ── Rendering ────────────────────────────────────────────────────────────────
_FONT_CACHE = {}


def _font(kind, px):
    key = (kind, px)
    if key not in _FONT_CACHE:
        fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        names = {"text": ["segoeuisb.ttf", "segoeui.ttf"],
                 "icon": ["SegoeIcons.ttf", "segmdl2.ttf"]}[kind]
        f = None
        for n in names:
            try:
                f = ImageFont.truetype(os.path.join(fonts, n), max(6, px))
                break
            except Exception:
                continue
        _FONT_CACHE[key] = f or ImageFont.load_default()
    return _FONT_CACHE[key]


def _draw_winlogo(d, cx, cy, size, color):
    g = max(1, size // 10)
    h = (size - g) // 2
    x0, y0 = cx - size // 2, cy - size // 2
    for dx in (0, h + g):
        for dy in (0, h + g):
            d.rectangle((x0 + dx, y0 + dy, x0 + dx + h, y0 + dy + h), fill=color)


def _label_fits(d, text, font, maxw):
    try:
        return d.textlength(text, font=font) <= maxw
    except Exception:
        return True


def draw_key(d, box, kd, dark, fg, accent, state, pressed, toggled, scale, ref=None):
    """Draw one Gboard-style key tile with its icon or label."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    # Size labels from the ROW height so a column's wider keys do not get bigger text.
    m = min(w, h, ref) if ref else min(w, h)
    r = max(3, int(min(w, h) * 0.2))
    on = state > 0 or toggled
    if on:
        fill = accent + (235,)
        tc = (255, 255, 255) if _lum(accent) < 0.62 else (32, 33, 36)
    else:
        base = 52 if dark else 225
        fill = (255, 255, 255, base + (45 if pressed else 0))
        tc = fg
        if not dark:
            d.rounded_rectangle((x0, y0 + max(1, int(scale)), x1, y1 + max(1, int(scale))),
                                radius=r, fill=(0, 0, 0, 38))
    if pressed and on:
        fill = tuple(min(255, c + 25) for c in accent) + (255,)
    d.rounded_rectangle(box, radius=r, fill=fill)
    _, label, icon, kind, value, _ = kd
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    if icon == WINLOGO:
        _draw_winlogo(d, cx, cy, int(m * 0.40), tc + (255,))
    elif icon:
        f = _font("icon", int(m * 0.46))
        d.text((cx, cy), icon, font=f, fill=tc + (255,), anchor="mm")
    else:
        px = int(min(m * 0.40, w * 0.5))
        f = _font("text", px)
        while px > 8 and not _label_fits(d, label, f, w * 0.84):
            px -= 1
            f = _font("text", px)
        d.text((cx, cy), label, font=f, fill=tc + (255,), anchor="mm")
    if state == 2:                                          # locked: little bar under it
        lw = max(4, int(w * 0.28))
        th = max(2, int(scale * 1.5))
        d.rounded_rectangle((cx - lw // 2, y1 - th * 3, cx + lw // 2, y1 - th * 2),
                            radius=th, fill=tc + (255,))


def round_outer(img, corner, radius, bg):
    """Fill the outer corners ('left' or 'right' side) of img with bg, anti-aliased."""
    w, h = img.size
    k = 4
    mask = Image.new("L", (w * k, h * k), 0)
    md = ImageDraw.Draw(mask)
    R = radius * k
    if corner == "right":
        md.rounded_rectangle((-2 * R, 0, w * k - 1, h * k - 1), radius=R, fill=255)
    else:
        md.rounded_rectangle((0, 0, w * k - 1 + 2 * R, h * k - 1), radius=R, fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)
    return Image.composite(img, Image.new("RGB", (w, h), bg), mask)


def render_bar(edge, w, h, keys, scale, mods_state, pressed_idx, cfg=None):
    """Render a whole bar. Returns (PIL image, list of key boxes)."""
    cfg = cfg or CONFIG.get()
    bg, dark = background(edge, w, h, cfg)
    fg = text_color(dark, cfg)
    accent = THEME.accent if THEME else (138, 180, 248)
    img = bg.convert("RGBA")
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    _, _, gap = sizes(scale, cfg)
    boxes = []
    n = len(keys)
    if n:
        horizontal = edge in ("top", "bottom")
        span = (w if horizontal else h) - gap * (n + 1)
        each = span / n
        for i, kid in enumerate(keys):
            a = gap + i * (each + gap)
            if horizontal:
                box = (int(a), gap, int(a + each), h - gap)
            else:
                box = (gap, int(a), w - gap, int(a + each))
            kd = keydef(kid, cfg)
            if not kd:
                continue
            st = mods_state.get(kd[4], 0) if kd[3] in ("mod", "fn") else 0
            tog = False
            if kid in TOGGLE_VK:
                try:
                    tog = bool(u32.GetKeyState(TOGGLE_VK[kid]) & 1)
                except Exception:
                    pass
            draw_key(d, box, kd, dark, fg, accent, st, i == pressed_idx, tog, scale,
                     ref=sizes(scale, cfg)[0] - 2 * gap)
            boxes.append(box)
    img.alpha_composite(over)
    return img.convert("RGB"), boxes


def render_chip(kid, w, h, scale, cfg=None):
    """A single key on its own (used by the builder palette and drag ghost)."""
    cfg = cfg or CONFIG.get()
    bg, dark = background("top", w, h, cfg)
    img = bg.convert("RGBA")
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    kd = keydef(kid, cfg)
    if kd:
        g = max(2, int(scale * 2))
        draw_key(d, (g, g, w - g, h - g), kd, dark, text_color(dark, cfg),
                 THEME.accent if THEME else (138, 180, 248), 0, False, False, scale)
    img.alpha_composite(over)
    return img.convert("RGB")


# ── Bars (Tk windows) ────────────────────────────────────────────────────────
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x08000000, 0x8, 0x80, 0x40000
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP = 0x0001 | 0x0002 | 0x0010            # NOSIZE | NOMOVE | NOACTIVATE
try:
    _GetWL = u32.GetWindowLongPtrW
    _SetWL = u32.SetWindowLongPtrW
except AttributeError:
    _GetWL = u32.GetWindowLongW
    _SetWL = u32.SetWindowLongW
_GetWL.restype = ctypes.c_ssize_t
_GetWL.argtypes = [ctypes.c_void_p, ctypes.c_int]
_SetWL.restype = ctypes.c_ssize_t
_SetWL.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
u32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
u32.GetParent.restype = ctypes.c_void_p
u32.GetParent.argtypes = [ctypes.c_void_p]
u32.GetAncestor.restype = ctypes.c_void_p
u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]

_bar_hwnds = []              # hwnds of every visible bar, for raise_now()


def _toplevel_hwnd(tkwin):
    """The real top-level window behind a Tk Toplevel.

    Tk only builds the wrapper window when the Toplevel is first mapped; until then
    its inner window sits in a temporary hidden parent. Asking right after
    deiconify() therefore returned that throwaway parent, so 'stay on top' and
    'never take focus' were applied to a window that was never shown. Flush Tk's
    pending work first, then walk up to the root ancestor."""
    try:
        tkwin.update_idletasks()
    except Exception:
        pass
    h = tkwin.winfo_id()
    return u32.GetAncestor(ctypes.c_void_p(h), 2) or u32.GetParent(h) or h   # GA_ROOT


def _stamp(tkwin):
    """Never take focus, stay topmost, no taskbar button."""
    target = _toplevel_hwnd(tkwin)
    ex = _GetWL(target, GWL_EXSTYLE)
    ex = (ex | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    _SetWL(target, GWL_EXSTYLE, ex)
    return target


class _GESTURECONFIG(ctypes.Structure):
    _fields_ = [("dwID", wt.DWORD), ("dwWant", wt.DWORD), ("dwBlock", wt.DWORD)]


u32.SetGestureConfig.argtypes = [ctypes.c_void_p, wt.DWORD, wt.UINT,
                                 ctypes.POINTER(_GESTURECONFIG), wt.UINT]
u32.SetPropW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p]
_ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, ctypes.c_void_p, ctypes.c_void_p)


def touch_friendly(tkwin):
    """Make finger drags arrive as ordinary mouse drags in this Tk window.

    A window that does not handle touch itself gets Windows' legacy touch support:
    a tap becomes a click, but a DRAG is treated as a pan (scroll) gesture and never
    reaches the app as mouse movement - so dragging worked with a mouse and silently
    did nothing with a finger. Blocking all gestures makes Windows promote touch to
    plain mouse messages straight away. Press-and-hold (the right-click ring) is
    switched off as well, so holding a key auto-repeats instead.
    Tk widgets are real child windows, so every one of them needs it."""
    try:
        top = _toplevel_hwnd(tkwin)
        hwnds = [top]
        cb = _ENUMPROC(lambda h, _: hwnds.append(h) or True)
        u32.EnumChildWindows(ctypes.c_void_p(top), cb, None)
        cfg = _GESTURECONFIG(0, 0, 1)          # GID all gestures: want none, block all
        for h in hwnds:
            u32.SetGestureConfig(ctypes.c_void_p(h), 0, 1, ctypes.byref(cfg),
                                 ctypes.sizeof(_GESTURECONFIG))
            u32.SetPropW(ctypes.c_void_p(h), "MicrosoftTabletPenServiceProperty",
                         ctypes.c_void_p(0x00010001))   # no press-and-hold, no flicks
    except Exception as e:
        log(f"touch_friendly: {e}")


def _move_now(win, x, y, w, h):
    """Move/resize a Tk toplevel immediately and make sure it got there.

    Tk does not move a window when 'wm geometry' is called; it does it later, when the
    event loop is idle. Any SetWindowPos on the window in between (raise_now runs right
    after every layout, and the host's watchdog thread calls it every second) makes Tk
    re-read the window's CURRENT spot and quietly drop the pending move. The bars only
    jumped to the right place when they were hidden and shown again, so dragging the
    keyboard left every extra key behind. Flushing Tk straight away fixes that, and
    comparing the real rectangle catches a move that still got lost."""
    win.geometry(f"{w}x{h}+{x}+{y}")
    try:
        win.update_idletasks()
    except Exception:
        pass


def _is_at(hwnd, x, y, w, h):
    if not hwnd:
        return True                  # not on screen yet; deiconify uses the new geometry
    r = wt.RECT()
    if not u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
        return True
    return (r.left, r.top, r.right - r.left, r.bottom - r.top) == (x, y, w, h)


_gdi = ctypes.WinDLL("gdi32")
_gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
_gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
_gdi.DeleteObject.argtypes = [ctypes.c_void_p]
u32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]


def _round_window(hwnd, w, h, corner, radius):
    """Round the two OUTER corners of a side column (corner = 'left'/'right'), or clear
    the clip (corner None). The round rect is pushed past the inner edge so the inner
    corners, which sit against the keyboard, stay square."""
    try:
        if not corner:
            u32.SetWindowRgn(ctypes.c_void_p(hwnd), None, True)
            return
        d = 2 * radius
        if corner == "right":
            rgn = _gdi.CreateRoundRectRgn(-d, 0, w + 1, h + 1, d, d)
        else:
            rgn = _gdi.CreateRoundRectRgn(0, 0, w + d + 1, h + 1, d, d)
        if rgn and not u32.SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(rgn), True):
            _gdi.DeleteObject(ctypes.c_void_p(rgn))      # Windows only owns it on success
    except Exception as ex:
        log(f"round corners: {ex}")


def raise_now():
    """Put the bars back above the emulator. The host re-asserts the emulator's
    topmost position every second, which would otherwise cover the top rows that sit
    over its relay area; it calls this right afterwards."""
    for h in list(_bar_hwnds):
        try:
            ok = u32.SetWindowPos(ctypes.c_void_p(h), HWND_TOPMOST, 0, 0, 0, 0, SWP)
            if not ok and time.time() - _RAISE_LOG[0] > 30:
                _RAISE_LOG[0] = time.time()
                log(f"raise failed hwnd={h:#x} err={ctypes.get_last_error()}")
        except Exception as e:
            if time.time() - _RAISE_LOG[0] > 30:
                _RAISE_LOG[0] = time.time()
                log(f"raise error: {e}")


_RAISE_LOG = [0.0]


class _Bar:
    def __init__(self, tk, ImageTk, root, edge, keys):
        self.tk, self.ImageTk = tk, ImageTk
        self.edge, self.keys = edge, list(keys)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.label = tk.Label(self.win, bd=0, highlightthickness=0)
        self.label.pack(fill="both", expand=True)
        self.hwnd = 0
        self.geom = None
        self.boxes = []
        self.pressed = -1
        self.photo = None
        self.render_key = None
        self.repeat_job = None
        self.shown = False
        self.rgn_key = None
        self.label.bind("<ButtonPress-1>", self._down)
        self.label.bind("<ButtonRelease-1>", self._up)
        self.label.bind("<B1-Motion>", self._move)

    def destroy(self):
        if self.hwnd in _bar_hwnds:
            _bar_hwnds.remove(self.hwnd)
        try:
            self.win.destroy()
        except Exception:
            pass

    def _hit(self, x, y):
        for i, (x0, y0, x1, y1) in enumerate(self.boxes):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        return -1

    def _kid(self, i):
        return self.keys[i] if 0 <= i < len(self.keys) else None

    def _down(self, ev):
        i = self._hit(ev.x, ev.y)
        self.pressed = i
        kid = self._kid(i)
        if kid is None:
            return
        kd = keydef(kid)
        if kd and kd[3] == "key" and kd[4] in REPEATABLE:
            activate_key(kid)                       # repeatable keys fire on press
            self.repeat_job = self.win.after(420, lambda: self._repeat(kid))
        self.render_key = None

    def _repeat(self, kid):
        if self.pressed >= 0 and self._kid(self.pressed) == kid:
            activate_key(kid)
            self.repeat_job = self.win.after(65, lambda: self._repeat(kid))

    def _move(self, ev):
        if self.pressed >= 0 and self._hit(ev.x, ev.y) != self.pressed:
            self._cancel_repeat()
            self.pressed = -1                       # slid off: cancel, like Gboard
            self.render_key = None

    def _cancel_repeat(self):
        if self.repeat_job:
            try:
                self.win.after_cancel(self.repeat_job)
            except Exception:
                pass
            self.repeat_job = None

    def _up(self, ev):
        self._cancel_repeat()
        i = self.pressed
        self.pressed = -1
        self.render_key = None
        if i < 0 or self._hit(ev.x, ev.y) != i:
            return
        kid = self._kid(i)
        kd = keydef(kid)
        if kd and not (kd[3] == "key" and kd[4] in REPEATABLE):
            activate_key(kid)

    def place(self, x, y, w, h, scale, corner=None):
        if w < 8 or h < 8:
            self.hide()
            return
        g = (x, y, w, h)
        if g != self.geom:
            _move_now(self.win, *g)
            self.geom = g
            self.render_key = None
        elif self.shown and not _is_at(self.hwnd, *g):
            _move_now(self.win, *g)     # a move got lost: put it right
        if not self.shown:
            self.win.deiconify()
            self.shown = True
            if not self.hwnd:
                self.hwnd = _stamp(self.win)
                touch_friendly(self.win)        # finger-hold must repeat, not right-click
            if self.hwnd not in _bar_hwnds:
                _bar_hwnds.append(self.hwnd)
        rk = (w, h, corner, corner_radius(scale) if corner else 0)
        if self.hwnd and rk != self.rgn_key:
            _round_window(self.hwnd, w, h, corner, rk[3])
            self.rgn_key = rk
        self.redraw(scale)

    def hide(self):
        if self.shown:
            self.win.withdraw()
            self.shown = False
            if self.hwnd in _bar_hwnds:
                _bar_hwnds.remove(self.hwnd)

    def redraw(self, scale):
        if not self.geom:
            return
        toggles = tuple(bool(u32.GetKeyState(v) & 1) for v in TOGGLE_VK.values())
        key = (self.geom, MODS.version, self.pressed, THEME.version if THEME else 0,
               CONFIG.version, toggles, scale)
        if key == self.render_key:
            return
        self.render_key = key
        _, _, w, h = self.geom
        img, self.boxes = render_bar(self.edge, w, h, self.keys, scale, MODS.snapshot(),
                                     self.pressed)
        self.photo = self.ImageTk.PhotoImage(img)
        self.label.configure(image=self.photo)


RELAY_BG = (0x26, 0x32, 0x38)      # the relay app's slate background (MainActivity SLATE)


def render_editbtn(size, pressed):
    """Keyboard-with-gear button, styled like the relay app's own hide button."""
    s = max(8, int(size))
    img = Image.new("RGB", (s, s), RELAY_BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=max(3, s // 6),
                        fill=(0x3a, 0x4a, 0x52) if pressed else (0x1e, 0x28, 0x2d))
    f = _font("icon", int(s * 0.5))
    if f:
        d.text((s / 2, s / 2), "", font=f, fill=(0xb0, 0xbe, 0xc5), anchor="mm")
    return img


class _EditButton:
    """Small button at the top right of the keyboard that opens the extra keys editor."""

    def __init__(self, tk, ImageTk, root, on_tap):
        self.ImageTk, self.on_tap = ImageTk, on_tap
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.label = tk.Label(self.win, bd=0, highlightthickness=0, bg="#263238")
        self.label.pack(fill="both", expand=True)
        self.hwnd = 0
        self.shown = False
        self.geom = None
        self.pressed = False
        self.drawn = None
        self.photo = None
        self.label.bind("<ButtonPress-1>", self._down)
        self.label.bind("<ButtonRelease-1>", self._up)

    def _inside(self, ev):
        return 0 <= ev.x < self.label.winfo_width() and 0 <= ev.y < self.label.winfo_height()

    def _down(self, ev):
        self.pressed = True
        self.redraw()

    def _up(self, ev):
        was = self.pressed
        self.pressed = False
        self.redraw()
        if was and self._inside(ev):
            self.on_tap()

    def place(self, x, y, size):
        g = (x, y, size)
        if g != self.geom or (self.shown and not _is_at(self.hwnd, x, y, size, size)):
            _move_now(self.win, x, y, size, size)
            self.geom = g
        if not self.shown:
            self.win.deiconify()
            self.shown = True
            if not self.hwnd:
                self.hwnd = _stamp(self.win)
                touch_friendly(self.win)
            if self.hwnd not in _bar_hwnds:
                _bar_hwnds.append(self.hwnd)
        self.redraw()

    def redraw(self):
        if not self.geom or self.drawn == (self.geom[2], self.pressed):
            return
        self.drawn = (self.geom[2], self.pressed)
        self.photo = self.ImageTk.PhotoImage(render_editbtn(self.geom[2], self.pressed))
        self.label.configure(image=self.photo)

    def hide(self):
        if self.shown:
            self.win.withdraw()
            self.shown = False
            self.pressed = False
            if self.hwnd in _bar_hwnds:
                _bar_hwnds.remove(self.hwnd)

    def destroy(self):
        self.hide()
        try:
            self.win.destroy()
        except Exception:
            pass


class _Banner:
    """One-time 'what's new' note above the keyboard, with a never-show-again X."""

    def __init__(self, tk, root, scale, on_setup, on_close):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        bg, fg = "#2d2440", "#f1ecff"
        self.win.configure(bg=bg)
        fpx = -max(11, int(12 * scale))
        pad = int(8 * scale)
        frame = tk.Frame(self.win, bg=bg)
        frame.pack(fill="both", expand=True, padx=pad, pady=pad)
        tk.Label(frame, text="New: extra keys!", bg=bg, fg="#ffffff", anchor="w",
                 font=("Segoe UI Semibold", fpx)).pack(fill="x")
        tk.Label(frame, text=("Add rows or columns of Ctrl, Alt, Shift, Win, Tab, Esc, arrows, "
                              "F-keys, cut/copy/paste and your own shortcuts around the keyboard. "
                              "Ctrl and friends also work with Gboard's keys."),
                 bg=bg, fg=fg, anchor="w", justify="left", wraplength=1,
                 font=("Segoe UI", fpx)).pack(fill="x", pady=(int(2 * scale), int(6 * scale)))
        self.msg = frame.winfo_children()[-1]
        btns = tk.Frame(frame, bg=bg)
        btns.pack(fill="x")
        tk.Button(btns, text="Set up extra keys", command=on_setup, bg="#8ab4f8", fg="#202124",
                  activebackground="#aecbfa", relief="flat", padx=pad, pady=int(3 * scale),
                  font=("Segoe UI Semibold", fpx)).pack(side="left")
        close = tk.Label(self.win, text="\u2715", bg=bg, fg=fg, font=("Segoe UI", fpx),
                         padx=pad, cursor="hand2")
        close.place(relx=1.0, x=-2, y=2, anchor="ne")
        close.bind("<Button-1>", lambda e: on_close())
        self.hwnd = 0
        self.shown = False
        self.geom = None

    def place(self, x, y, w):
        try:
            self.msg.configure(wraplength=max(50, w - 60))
            self.win.update_idletasks()
            h = self.win.winfo_reqheight()
            g = (x, y - h, w, h)
            if g != self.geom or (self.shown and not _is_at(self.hwnd, *g)):
                _move_now(self.win, *g)
                self.geom = g
            if not self.shown:
                self.win.deiconify()
                self.shown = True
                if not self.hwnd:
                    self.hwnd = _stamp(self.win)
                    _bar_hwnds.append(self.hwnd)
        except Exception as e:
            log(f"banner place: {e}")

    def hide(self):
        if self.shown:
            self.win.withdraw()
            self.shown = False

    def destroy(self):
        if self.hwnd in _bar_hwnds:
            _bar_hwnds.remove(self.hwnd)
        try:
            self.win.destroy()
        except Exception:
            pass


# ── Manager (runs in the host's Tk thread) ───────────────────────────────────
_cmdq = queue.Queue()


class _Manager:
    def __init__(self, root):
        import tkinter as tk
        from PIL import ImageTk
        self.tk, self.ImageTk, self.root = tk, ImageTk, root
        self.bars = []
        self.bar_cfg_version = -1
        self.banner = None
        self.editbtn = None
        self.builder = None
        self.hwnd = None
        self.hwnd_at = 0.0
        self.scale = 2.0
        root.after(300, self._tick)

    # -- emulator geometry ---------------------------------------------------
    def _emu(self):
        now = time.time()
        if not self.hwnd or now - self.hwnd_at > 2.0 or not u32.IsWindow(self.hwnd):
            self.hwnd = API["find_emulator_hwnd"]()
            self.hwnd_at = now
        return self.hwnd

    def _rebuild_bars(self):
        for b in self.bars:
            b.destroy()
        self.bars = []
        cfg = CONFIG.get()
        for b in cfg["rows"] + cfg["cols"]:
            self.bars.append(_Bar(self.tk, self.ImageTk, self.root, b["edge"], b["keys"]))
        self.bar_cfg_version = CONFIG.version

    def _layout(self, hwnd):
        """Screen rectangles for every bar, in config order."""
        wr = wt.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(wr))
        cr = wt.RECT()
        u32.GetClientRect(hwnd, ctypes.byref(cr))
        pt = wt.POINT(0, 0)
        u32.ClientToScreen(hwnd, ctypes.byref(pt))
        client_top, client_h = pt.y, cr.bottom - cr.top
        frac = (KB.top / KB.height) if (KB.top and KB.height) else 0.27
        kb_top = int(client_top + client_h * frac)
        kb_bottom = wr.bottom
        cfg = CONFIG.get()
        row_h, col_w, _ = sizes(self.scale, cfg)
        tops = [i for i, b in enumerate(cfg["rows"]) if b["edge"] == "top"]
        bots = [i for i, b in enumerate(cfg["rows"]) if b["edge"] == "bottom"]
        lefts = [i for i, b in enumerate(cfg["cols"]) if b["edge"] == "left"]
        rights = [i for i, b in enumerate(cfg["cols"]) if b["edge"] == "right"]
        # Top rows cover the empty relay area above the keyboard. Keep the first ~110
        # device px (the relay text box) clear; shrink rows rather than cover it.
        # Rows too thin to tap are hidden (furthest from the keyboard first) instead of
        # spilling over the relay text box and the app's hide button.
        reserve = int(client_h * (110.0 / (KB.height or 1320)))
        avail = max(0, kb_top - (client_top + reserve))
        th = row_h
        shown_tops = len(tops)
        if tops and avail < row_h * len(tops):
            floor = int(22 * self.scale)
            shown_tops = min(len(tops), avail // floor) if floor else 0
            th = min(row_h, avail // shown_tops) if shown_tops else 0
        rects = []
        for i, b in enumerate(cfg["rows"]):
            if b["edge"] == "top":
                k = len(tops) - tops.index(i)         # last top row touches the keyboard
                if k > shown_tops:
                    rects.append((0, 0, 0, 0))        # place() hides it
                else:
                    rects.append((wr.left, kb_top - k * th, wr.right - wr.left, th))
            else:
                k = bots.index(i)
                rects.append((wr.left, kb_bottom + k * row_h, wr.right - wr.left, row_h))
        # Columns run the full height of the rows too (top of the highest top row to the
        # bottom of the lowest bottom row), so rows give the side keys more room as well.
        col_top = kb_top - shown_tops * th
        col_bottom = kb_bottom + len(bots) * row_h
        for i, b in enumerate(cfg["cols"]):
            if b["edge"] == "left":
                k = len(lefts) - lefts.index(i)
                rects.append((wr.left - k * col_w, col_top, col_w, col_bottom - col_top))
            else:
                k = rights.index(i)
                rects.append((wr.right + k * col_w, col_top, col_w, col_bottom - col_top))
        self.corners = [None] * len(cfg["rows"]) + [
            ("left" if lefts.index(i) == 0 else None) if b["edge"] == "left"
            else ("right" if rights.index(i) == len(rights) - 1 else None)
            for i, b in enumerate(cfg["cols"])]
        self.lay = {"client_top": client_top, "dp": client_h / float(KB.height or 1320),
                    "rows_top": kb_top - shown_tops * th, "right": pt.x + cr.right}
        return rects, wr

    # -- main loop -----------------------------------------------------------
    def _tick(self):
        try:
            self._drain()
            if self.bar_cfg_version != CONFIG.version:
                self._rebuild_bars()
            h = self._emu()
            visible = bool(h) and API["is_emu_shown"](h)
            STATE["visible"] = visible
            if visible:
                self.scale = API["window_scale"](h) or self.scale
                rects, wr = self._layout(h)
                for bar, r, corner in zip(self.bars, rects, self.corners):
                    bar.place(*r, self.scale, corner)
                self._editbtn_tick()
                raise_now()
                self._banner_tick(wr)
            else:
                for bar in self.bars:
                    bar.hide()
                if self.editbtn:
                    self.editbtn.hide()
                if self.banner:
                    self.banner.hide()
        except Exception as e:
            log(f"tick: {e}")
        self.root.after(50, self._tick)

    def _drain(self):
        while True:
            try:
                cmd = _cmdq.get_nowait()
            except queue.Empty:
                return
            if cmd == "builder":
                self.open_builder()

    # -- editor button -----------------------------------------------------------
    def _editbtn_tick(self):
        if not CONFIG.get().get("editbtn", True):
            if self.editbtn:
                self.editbtn.destroy()
                self.editbtn = None
            return
        if not self.editbtn:
            self.editbtn = _EditButton(self.tk, self.ImageTk, self.root, self.open_builder)
        L = self.lay
        dp = L["dp"]
        size = max(16, int(64 * dp))
        x = int(L["right"] - 17 * dp - size)
        y = int(L["rows_top"] - 14 * dp - size)
        if y < L["client_top"] + 95 * dp:          # top rows fill the space: sit by the hide button
            x = int(L["right"] - 17 * dp - 80 * dp - 14 * dp - size)
            y = int(L["client_top"] + 13 * dp)
            size = min(size, int(L["rows_top"] - y - 2))
            if size < 16:                          # no room at all: rows win, tray still works
                self.editbtn.hide()
                return
        self.editbtn.place(x, y, size)
        h = self.editbtn.hwnd                      # raised last, so no row ever covers it
        if h in _bar_hwnds and _bar_hwnds[-1] != h:
            _bar_hwnds.remove(h)
            _bar_hwnds.append(h)

    # -- banner ----------------------------------------------------------------
    def _banner_seen(self):
        try:
            return FEATURE_TAG in (API["settings_load"]().get("whatsnew_seen") or [])
        except Exception:
            return True

    def _mark_seen(self):
        try:
            seen = list(API["settings_load"]().get("whatsnew_seen") or [])
            if FEATURE_TAG not in seen:
                seen.append(FEATURE_TAG)
                API["settings_set"]("whatsnew_seen", seen)
        except Exception as e:
            log(f"mark seen: {e}")
        if self.banner:
            self.banner.destroy()
            self.banner = None
        self._seen_cache = True

    def _banner_tick(self, wr):
        if getattr(self, "_seen_cache", None) is None:
            self._seen_cache = self._banner_seen()
        if self._seen_cache:
            return
        if not self.banner:
            self.banner = _Banner(self.tk, self.root, self.scale,
                                  on_setup=lambda: (self._mark_seen(), self.open_builder()),
                                  on_close=self._mark_seen)
        top_handle = max(22, int(round(18 * self.scale)))
        y = wr.top - top_handle - int(4 * self.scale)
        if y < int(80 * self.scale):                  # no room above: sit inside the top
            y = wr.top + int(120 * self.scale)
        self.banner.place(wr.left, y, wr.right - wr.left)

    # -- builder -----------------------------------------------------------------
    def open_builder(self):
        if not getattr(self, "_seen_cache", False):
            self._mark_seen()                      # found the feature: retire the banner
        try:
            if self.builder and self.builder.alive():
                self.builder.focus()
                return
            from extras_builder import Builder
            self.builder = Builder(self)
        except Exception as e:
            import traceback
            log(f"builder failed: {e}\n{traceback.format_exc()}")


MANAGER = None
ICON_PATH = next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                                           "assets", "GboardIME.ico"),)
                  if os.path.exists(p)), None)


def keyboard_rect():
    """Screen rectangle of the emulator plus every extra row and column, or None."""
    try:
        h = API["find_emulator_hwnd"]()
        if not h or not API["is_emu_shown"](h):
            return None
        r = wt.RECT()
        u32.GetWindowRect(ctypes.c_void_p(h), ctypes.byref(r))
        rects = [(r.left, r.top, r.right, r.bottom)]
        for hw in list(_bar_hwnds):
            b = wt.RECT()
            if u32.GetWindowRect(ctypes.c_void_p(hw), ctypes.byref(b)):
                rects.append((b.left, b.top, b.right, b.bottom))
        return (min(a[0] for a in rects), min(a[1] for a in rects),
                max(a[2] for a in rects), max(a[3] for a in rects))
    except Exception:
        return None


def open_builder():
    """Thread-safe: called from the tray menu thread."""
    _cmdq.put("builder")


def attach(root):
    """Called from the host's Tk thread once its root exists."""
    global MANAGER
    if Image is None:
        log("pillow missing - extras disabled")
        return
    MANAGER = _Manager(root)
    log("extra keys ready")


def init(api):
    """Called once at host start-up with the host's helpers."""
    global THEME
    API.update(api)
    CONFIG.load()
    if Image is not None:
        THEME = _Theme()
    threading.Thread(target=_inject_worker, daemon=True).start()
    threading.Thread(target=_geom_thread, daemon=True).start()
