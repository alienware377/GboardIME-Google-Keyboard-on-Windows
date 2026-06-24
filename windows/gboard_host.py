"""
GboardIME Host — Windows side
Receives keystrokes from the GboardRelay Android app (via ADB reverse TCP)
and injects them into whatever Windows app currently has focus.

Hotkey: Ctrl+Alt+K  — show/hide the emulator window
System tray: right-click for options
"""

import ctypes
import ctypes.wintypes
import socket
import subprocess
import sys
import threading
import time
import os

import base64

import pystray
from PIL import Image, ImageDraw, ImageFont

# ── DPI awareness ────────────────────────────────────────────────────────────
# MUST run before any window/geometry call. Without this the process is DPI
# *virtualized*: GetWindowRect returns logical (scaled) pixels while SetWindowRgn
# takes PHYSICAL pixels — a mismatch equal to the display scale (2x at 200%),
# which made the keyboard-only crop land as a middle strip instead of the bottom
# band. Per-monitor-aware makes every coordinate physical and self-consistent.
def _enable_dpi_awareness():
    try:
        # PER_MONITOR_AWARE_V2 = -4
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
_enable_dpi_awareness()

def _window_scale(hwnd):
    """Display scale factor (1.0 at 96 DPI, 2.0 at 200%) for the window's monitor.
    Reliable now that the process is per-monitor DPI aware."""
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0

# ── Logging (host runs windowless under pythonw, so print() is invisible) ─────
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gboard_host.log")
def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass

# ── Windows API constants ────────────────────────────────────────────────────
INPUT_KEYBOARD      = 1
KEYEVENTF_UNICODE   = 0x0004
KEYEVENTF_KEYUP     = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_BACK    = 0x08
VK_RETURN  = 0x0D
VK_TAB     = 0x09
VK_LEFT    = 0x25
VK_RIGHT   = 0x27
VK_UP      = 0x26
VK_DOWN    = 0x28
VK_DELETE  = 0x2E
VK_HOME    = 0x24
VK_END     = 0x23
VK_PRIOR   = 0x21   # Page Up
VK_NEXT    = 0x22   # Page Down
VK_INSERT  = 0x2D
# Modifiers
VK_SHIFT   = 0x10
VK_CONTROL = 0x11
VK_MENU    = 0x12   # Alt
# Letters used in Ctrl-combos (copy/paste/cut/select-all/undo)
VK_A = 0x41
VK_C = 0x43
VK_V = 0x56
VK_X = 0x58
VK_Z = 0x5A

SW_HIDE    = 0
SW_SHOWNOACTIVATE = 4   # restore (un-minimize) to last size without activating
SW_SHOW    = 5
SW_SHOWNA  = 8   # show without activating (no focus steal)
SW_RESTORE = 9

# Extended window styles — the "touch keyboard" recipe
GWL_EXSTYLE        = -20
GWL_STYLE          = -16
WS_EX_TOPMOST      = 0x00000008
WS_EX_TOOLWINDOW   = 0x00000080
WS_EX_APPWINDOW    = 0x00040000   # force a taskbar button (so a minimized window can be restored)
WS_EX_NOACTIVATE   = 0x08000000   # window never takes focus, even on click
WS_SYSMENU         = 0x00080000   # system menu (required for the min/close caption buttons)
WS_MINIMIZEBOX     = 0x00020000   # minimize button in the caption
WS_MAXIMIZEBOX     = 0x00010000
WS_CAPTION         = 0x00C00000   # title bar (WS_BORDER | WS_DLGFRAME)
WS_THICKFRAME      = 0x00040000   # sizing border
WS_POPUP           = 0x80000000   # borderless popup

# System-menu manipulation (used to disable the close button so an accidental
# click can't terminate the emulator). A tool window shows ONLY a close (X)
# button in its slim caption; we grey it out and rely on Ctrl+Alt+K / auto-hide.
SC_CLOSE     = 0xF060
MF_BYCOMMAND = 0x00000000
MF_GRAYED    = 0x00000001
MF_DISABLED  = 0x00000002

# SetWindowPos
HWND_TOPMOST    = -1
HWND_NOTOPMOST  = -2
SWP_NOSIZE      = 0x0001
SWP_NOMOVE      = 0x0002
SWP_NOACTIVATE  = 0x0010
SWP_SHOWWINDOW  = 0x0040
SWP_NOZORDER    = 0x0004
SWP_FRAMECHANGED = 0x0020

# 64-bit-safe Get/SetWindowLongPtr bindings
try:
    _GetWindowLongPtr = ctypes.windll.user32.GetWindowLongPtrW
    _SetWindowLongPtr = ctypes.windll.user32.SetWindowLongPtrW
except AttributeError:                       # 32-bit Python fallback
    _GetWindowLongPtr = ctypes.windll.user32.GetWindowLongW
    _SetWindowLongPtr = ctypes.windll.user32.SetWindowLongW
_GetWindowLongPtr.restype  = ctypes.c_longlong
_GetWindowLongPtr.argtypes = [ctypes.c_void_p, ctypes.c_int]
_SetWindowLongPtr.restype  = ctypes.c_longlong
_SetWindowLongPtr.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]

_SetWindowPos = ctypes.windll.user32.SetWindowPos
_SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_uint]

# Window-region clipping (to crop the emulator down to just the keyboard band)
_CreateRectRgn = ctypes.windll.gdi32.CreateRectRgn
_CreateRectRgn.restype  = ctypes.c_void_p
_CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
_SetWindowRgn = ctypes.windll.user32.SetWindowRgn
_SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
_SetWindowRgn.restype  = ctypes.c_int

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
_GetWindowRect = ctypes.windll.user32.GetWindowRect
_GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
_GetClientRect = ctypes.windll.user32.GetClientRect
_GetClientRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
_ClientToScreen = ctypes.windll.user32.ClientToScreen
_ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(_POINT)]

def _window_geometry(hwnd):
    """Return (win_w, win_h, client_top_offset, client_h) in the window's own
    coordinate space — read live so aspect-locked resizes are respected."""
    wr = _RECT(); _GetWindowRect(hwnd, ctypes.byref(wr))
    cr = _RECT(); _GetClientRect(hwnd, ctypes.byref(cr))
    pt = _POINT(0, 0); _ClientToScreen(hwnd, ctypes.byref(pt))
    win_w = wr.right - wr.left
    win_h = wr.bottom - wr.top
    client_top = pt.y - wr.top          # window-y where the device screen begins
    client_h   = cr.bottom - cr.top
    return win_w, win_h, client_top, client_h

WM_HOTKEY  = 0x0312
MOD_CONTROL = 0x0002
MOD_ALT     = 0x0001
VK_K        = 0x4B

HOST_PORT   = 9877   # Windows host listens here (9876 is taken by BlenderMCP)
DEVICE_PORT = 9876   # what the Android relay APK connects to inside the emulator
ADB_PATH   = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk",
                           "platform-tools", "adb.exe")

# ── SendInput structs ────────────────────────────────────────────────────────
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.wintypes.WORD),
        ("wScan",       ctypes.wintypes.WORD),
        ("dwFlags",     ctypes.wintypes.DWORD),
        ("time",        ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_byte * 28)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _INPUT_UNION)]

_SendInput = ctypes.windll.user32.SendInput
_SendInput.restype  = ctypes.c_uint
_SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]

def _make_ki(vk=0, scan=0, flags=0):
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                        dwExtraInfo=None)
    return i

def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    _SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

def inject_unicode_char(ch):
    s = ord(ch)
    _send(
        _make_ki(scan=s, flags=KEYEVENTF_UNICODE),
        _make_ki(scan=s, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    )

def inject_vk(vk, extended=False):
    flags = KEYEVENTF_EXTENDEDKEY if extended else 0
    _send(
        _make_ki(vk=vk, flags=flags),
        _make_ki(vk=vk, flags=flags | KEYEVENTF_KEYUP),
    )

def inject_combo(mod_vks, vk, extended=False):
    """Press modifier keys (Shift/Ctrl/Alt), tap vk, release modifiers in reverse.
    Powers selection (Shift+arrows), word jumps, and Ctrl+A/C/V/X/Z."""
    flags = KEYEVENTF_EXTENDEDKEY if extended else 0
    seq = [_make_ki(vk=m) for m in mod_vks]
    seq.append(_make_ki(vk=vk, flags=flags))
    seq.append(_make_ki(vk=vk, flags=flags | KEYEVENTF_KEYUP))
    seq.extend(_make_ki(vk=m, flags=KEYEVENTF_KEYUP) for m in reversed(mod_vks))
    _send(*seq)

# Named keys -> (vk, is_extended). Extended = the grey nav cluster (arrows, home,
# end, delete, page up/down, insert) so Windows treats them as the nav keys.
_KEY_VK = {
    "ENTER": (VK_RETURN, False), "TAB": (VK_TAB, False),
    "BACKSPACE": (VK_BACK, False), "BACK": (VK_BACK, False),
    "DELETE": (VK_DELETE, True), "INSERT": (VK_INSERT, True),
    "LEFT": (VK_LEFT, True), "RIGHT": (VK_RIGHT, True),
    "UP": (VK_UP, True), "DOWN": (VK_DOWN, True),
    "HOME": (VK_HOME, True), "END": (VK_END, True),
    "PAGEUP": (VK_PRIOR, True), "PAGEDOWN": (VK_NEXT, True),
    "A": (VK_A, False), "C": (VK_C, False), "V": (VK_V, False),
    "X": (VK_X, False), "Z": (VK_Z, False),
}
_MOD_VK = {"SHIFT": VK_SHIFT, "CTRL": VK_CONTROL, "CONTROL": VK_CONTROL, "ALT": VK_MENU}

def inject_key_spec(spec):
    """spec like 'LEFT', 'SHIFT+LEFT', 'CTRL+A', 'CTRL+SHIFT+HOME'."""
    parts = spec.split("+")
    name = parts[-1].strip().upper()
    mods = [_MOD_VK[p.strip().upper()] for p in parts[:-1] if p.strip().upper() in _MOD_VK]
    if name in _KEY_VK:
        vk, ext = _KEY_VK[name]
        inject_combo(mods, vk, ext)
        return True
    return False

def inject_text(text):
    """Inject a string of text character by character."""
    for ch in text:
        if ch == '\n':
            inject_vk(VK_RETURN)
        elif ch == '\t':
            inject_vk(VK_TAB)
        else:
            inject_unicode_char(ch)
        time.sleep(0.002)          # tiny gap prevents dropped chars

def inject_backspace(count=1):
    for _ in range(count):
        inject_vk(VK_BACK)
        time.sleep(0.002)

# ── Target window tracking ───────────────────────────────────────────────────
# We continuously track the last foreground window that is NOT the emulator.
# Before injecting keystrokes we restore focus there, bypassing Windows'
# SetForegroundWindow restriction via AttachThreadInput.

_target_hwnd: int = 0
_target_lock  = threading.Lock()
_last_inject_time: float = 0.0   # time of last SendInput; guards cursor-sync debounce

def _is_emulator_window(hwnd: int) -> bool:
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    t = buf.value.lower()
    return any(k in t for k in ("emulator", "pixel_6", "avd", "android emulator"))

def _target_tracker():
    """Background thread: update _target_hwnd to the last non-emulator fg window."""
    global _target_hwnd
    while True:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if hwnd and not _is_emulator_window(hwnd):
            with _target_lock:
                _target_hwnd = hwnd
        time.sleep(0.15)

def _keyboard_watchdog():
    """While the keyboard is visible, keep re-stamping the (lazily-recreated) Qt
    child render window with WS_EX_NOACTIVATE and re-assert topmost, so a tap on
    Gboard never starts stealing focus again after Qt rebuilds its surface."""
    while True:
        time.sleep(1.0)
        if not _emulator_visible and not _crop_active:
            continue
        hwnd = _find_emulator_hwnd()
        if not hwnd:
            continue
        _stamp_children_noactivate(hwnd)
        _hide_emulator_toolbar(hwnd)
        _SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                      SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        # Re-assert the crop region — Qt clears it whenever it rebuilds its surface.
        if _crop_active:
            _apply_crop_region(hwnd)

def _force_foreground(hwnd: int):
    """Set hwnd as foreground even from a background process (AttachThreadInput trick)."""
    if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
        return
    GetCurrentThreadId     = ctypes.windll.kernel32.GetCurrentThreadId
    GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
    AttachThreadInput      = ctypes.windll.user32.AttachThreadInput
    SetForegroundWindow    = ctypes.windll.user32.SetForegroundWindow
    BringWindowToTop       = ctypes.windll.user32.BringWindowToTop

    cur_tid = GetCurrentThreadId()
    tgt_tid = GetWindowThreadProcessId(hwnd, None)
    if cur_tid != tgt_tid:
        AttachThreadInput(cur_tid, tgt_tid, True)
    SetForegroundWindow(hwnd)
    BringWindowToTop(hwnd)
    if cur_tid != tgt_tid:
        AttachThreadInput(cur_tid, tgt_tid, False)

def _inject_into_target(fn):
    """Restore focus to target window, run fn() to inject, leave focus there."""
    global _last_inject_time
    with _target_lock:
        target = _target_hwnd
    if target:
        _force_foreground(target)
        time.sleep(0.04)   # let activation settle before SendInput
    _last_inject_time = time.time()
    fn()

# ── Command dispatcher ───────────────────────────────────────────────────────
def dispatch(cmd: str):
    cmd = cmd.rstrip("\r\n")   # only strip the line terminator — keep payload spaces
    if cmd.startswith("TEXT:"):
        text = cmd[5:]
        _inject_into_target(lambda: inject_text(text))
    elif cmd.startswith("DEL:"):
        try:
            n = int(cmd[4:])
            _inject_into_target(lambda: inject_backspace(n))
        except ValueError:
            pass
    elif cmd.startswith("KEY:"):
        # Generic key spec with optional modifiers, e.g. KEY:ENTER, KEY:SHIFT+LEFT,
        # KEY:CTRL+A, KEY:CTRL+SHIFT+HOME. Backward compatible with all old KEY:* names.
        spec = cmd[4:]
        parts = spec.split("+")
        if parts[-1].strip().upper() in _KEY_VK:
            _inject_into_target(lambda: inject_key_spec(spec))
        else:
            log(f"[key] unknown key spec: {spec!r}")
    elif cmd.startswith("CROP:"):
        arg = cmd[5:].strip()
        log(f"[crop] CROP command received: arg={arg!r}")
        if arg.upper() == "OFF":
            _set_crop(False)
        else:
            try:
                _set_crop(True, float(arg))
            except ValueError:
                log(f"[crop] bad CROP arg: {arg!r}")
    elif cmd == "PING":
        pass  # keepalive

# ── TCP server ───────────────────────────────────────────────────────────────
_client_conn = None
_client_lock = threading.Lock()

def server_thread():
    global _client_conn
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", HOST_PORT))
        except OSError as e:
            print(f"[GboardHost] Cannot bind port {HOST_PORT}: {e}")
            return
        srv.listen(1)
        print(f"[GboardHost] Listening on 127.0.0.1:{HOST_PORT}")
        while True:
            try:
                conn, addr = srv.accept()
            except Exception:
                break
            print(f"[GboardHost] Android connected from {addr}")
            with _client_lock:
                _client_conn = conn
            try:
                buf = ""
                with conn:
                    conn.settimeout(5.0)
                    while True:
                        try:
                            chunk = conn.recv(4096).decode("utf-8", errors="replace")
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            if line.strip():
                                dispatch(line)
            except Exception as e:
                print(f"[GboardHost] Connection lost: {e}")
            finally:
                with _client_lock:
                    _client_conn = None
            print("[GboardHost] Android disconnected, waiting for reconnect...")

# ── ADB helpers ──────────────────────────────────────────────────────────────
def _emulator_serial():
    """Return the emulator serial (e.g. emulator-5554); handles a physical phone
    being connected at the same time so adb never errors with 'more than one device'."""
    try:
        out = subprocess.run([ADB_PATH, "devices"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("emulator-") and "\tdevice" in line:
                return line.split("\t", 1)[0]
    except Exception:
        pass
    return None

def adb(*args, capture=False):
    serial = _emulator_serial()
    cmd = [ADB_PATH] + (["-s", serial] if serial else []) + list(args)
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout.strip()
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def setup_adb_reverse():
    """Map the emulator's :9876 (what the APK connects to) to the Windows host's
    :9877. (9876 on Windows is taken by BlenderMCP.)"""
    adb("reverse", f"tcp:{DEVICE_PORT}", f"tcp:{HOST_PORT}")
    print(f"[GboardHost] ADB reverse set: emulator:{DEVICE_PORT} → host:{HOST_PORT}")

# ── Emulator window management ───────────────────────────────────────────────
_emulator_visible = None   # None = unknown; will be synced on first toggle
# Auto show/hide ("act like the Windows touch keyboard"):
_auto_mode   = True        # master enable for focus-driven show/hide
_auto_shown  = False       # True only when the keyboard was raised BY auto-focus
                           # (not by the Ctrl+Alt+K hotkey). Physical typing only
                           # auto-hides an auto-shown keyboard, so a manual show stays put.

def _find_emulator_hwnd():
    """Find the main Android Emulator window, visible OR hidden."""
    best = ctypes.c_void_p(0)
    fallback = ctypes.c_void_p(0)

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    def _cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value
        tl = title.lower()
        # NEVER touch the emulator's auxiliary windows (Extended Controls, etc.) —
        # only the main device window should be docked / styled.
        if "extended controls" in tl or "extended-controls" in tl:
            return True
        # Prefer the specific titled main window (visible or not)
        if "android emulator" in tl:
            best.value = hwnd
            return False
        # Fallback: any window with emulator/pixel/avd keywords (still not aux)
        if any(k in tl for k in ("emulator", "pixel_6", "avd")):
            fallback.value = hwnd
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return best.value or fallback.value or None

def _get_window_pid(hwnd):
    pid = ctypes.wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value

def _hide_emulator_toolbar(main_hwnd=None):
    """Hide the emulator's separate white control-strip window. It's a distinct
    top-level window (class Qt653QWindowToolSaveBits, title 'Emulator', same PID
    as the main device window). Hiding it removes the sidebar without touching the
    Android screen. Re-asserted by the watchdog because Qt may recreate it."""
    main_hwnd = main_hwnd or _find_emulator_hwnd()
    if not main_hwnd:
        return
    main_pid = _get_window_pid(main_hwnd)
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    def _cb(hwnd, _):
        if hwnd == main_hwnd:
            return True
        if _get_window_pid(hwnd) != main_pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(ctypes.c_void_p(hwnd), cls, 256)
        if "ToolSaveBits" in cls.value:
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE)
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)

def _stamp_noactivate(hwnd):
    """OR WS_EX_NOACTIVATE onto a single window's exstyle."""
    ex = _GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    if not (ex & WS_EX_NOACTIVATE):
        _SetWindowLongPtr(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)

def _stamp_children_noactivate(parent):
    """Recursively apply WS_EX_NOACTIVATE to every descendant window.
    The emulator's Qt/QEMU render surface is a CHILD window — it's the one that
    grabs activation on a tap, so the top-level style alone isn't enough."""
    count = [0]
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    def _cb(child, _):
        _stamp_noactivate(child)
        count[0] += 1
        # Recurse into this child's own children
        ctypes.windll.user32.EnumChildWindows(child, WNDENUMPROC(_cb), 0)
        return True

    ctypes.windll.user32.EnumChildWindows(parent, WNDENUMPROC(_cb), 0)
    return count[0]

def _disable_close_button(hwnd):
    """Grey out and disable the window's close (X) button (and Alt+F4) so an
    accidental click can't terminate the emulator process. The user hides the
    keyboard via Ctrl+Alt+K, the tray, or auto-hide instead."""
    if not hwnd:
        return
    try:
        u = ctypes.windll.user32
        hmenu = u.GetSystemMenu(ctypes.c_void_p(hwnd), False)
        if hmenu:
            u.EnableMenuItem(ctypes.c_void_p(hmenu), SC_CLOSE,
                             MF_BYCOMMAND | MF_GRAYED | MF_DISABLED)
            # Force the caption to repaint so the X greys out immediately.
            u.DrawMenuBar(ctypes.c_void_p(hwnd))
    except Exception as e:
        print(f"[GboardHost] disable close button failed: {e}")


def _make_keyboard_window(hwnd):
    """Apply the touch-keyboard window styles: no-activate + always-on-top.
    After this, clicking the emulator/Gboard never steals focus from your app."""
    if not hwnd:
        return
    ex = _GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW  # no taskbar button
    ex &= ~WS_EX_APPWINDOW
    _SetWindowLongPtr(hwnd, GWL_EXSTYLE, ex)
    # Borderless: strip the native title bar / sizing border entirely. The custom
    # dark-grey title bar (with a minimize button) is drawn by _titlebar_thread() as
    # a separate host-owned overlay floating directly above this window. This avoids
    # the native dilemma where a slim (tool-window) caption can ONLY show a close
    # button and never a minimize button.
    st = _GetWindowLongPtr(hwnd, GWL_STYLE)
    st &= ~(WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
    st |= WS_POPUP
    _SetWindowLongPtr(hwnd, GWL_STYLE, st)
    # The clickable render surface is a child window — stamp the whole tree so a
    # tap on Gboard can't activate the emulator and steal Windows focus.
    n = _stamp_children_noactivate(hwnd)
    # Hide the emulator's separate white toolbar window (same PID).
    _hide_emulator_toolbar(hwnd)
    # Re-assert topmost z-order + apply the frame change without activating
    _SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                  SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)
    print(f"[GboardHost] Applied no-activate + topmost styles (top-level + {n} child windows)")

# "Keyboard only" is now achieved by giving the AVD a SHORT physical resolution
# (hw.lcd.height in config.ini), so the whole emulator window is naturally compact.
# The old SetWindowRgn clip is abandoned: on this Qt/GL window it never visually
# clipped (DWM composites the render surface past the region) — it only killed
# mouse hit-testing on the top, which made the window unmovable and the toggle
# button untappable. We now just keep the window region NULL (whole window shown).
_emulator_size = (380, 470)   # current (w, h) logical; compact aspect ~1080x1300
_crop_active   = False        # retained for compatibility; always False now

def _apply_crop_region(hwnd):
    """Always clear any window region (no clip). Kept so leftover regions from an
    older host build get cleared on the next dock."""
    try:
        _SetWindowRgn(hwnd, None, True)   # NULL region → whole window visible
    except Exception:
        pass

def _position_emulator():
    """Place + size the emulator at the bottom-right, applying the crop band so the
    VISIBLE part (input box + Gboard) ends at the bottom of the screen."""
    hwnd = _find_emulator_hwnd()
    if not hwnd:
        return
    _make_keyboard_window(hwnd)
    # _emulator_size is in LOGICAL units (chosen while the host was DPI-virtualized);
    # now that we're DPI-aware, scale to PHYSICAL pixels to keep the same visual size.
    scale = _window_scale(hwnd)
    w = int(_emulator_size[0] * scale)
    h = int(_emulator_size[1] * scale)
    sw = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN (physical now)
    sh = ctypes.windll.user32.GetSystemMetrics(1)   # SM_CYSCREEN (physical now)
    # First set the requested width; the emulator aspect-locks the height itself.
    _SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), sw - w - 10, sh - h - 50,
                  w, h, SWP_NOACTIVATE)
    # Re-read the ACTUAL size Qt settled on, then dock bottom-right by that size so
    # the (visible) bottom edge sits at sh-50.
    aw, ah, _, _ = _window_geometry(hwnd)
    _SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), sw - aw - 10, sh - ah - 50,
                  aw, ah, SWP_NOACTIVATE)
    _apply_crop_region(hwnd)

def _dock_emulator_bottom(w=None, h=None):
    """Move emulator window to the bottom-right of the primary display, topmost."""
    global _emulator_size
    if w is not None and h is not None:
        _emulator_size = (w, h)
    _position_emulator()

def _set_size(w, h):
    """Resize the emulator to (w, h) and re-dock to bottom-right."""
    global _emulator_size
    _emulator_size = (w, h)
    _position_emulator()

def _set_crop(active, frac=None):
    """No-op shim. Keyboard-only is handled by the compact AVD resolution now, so
    the old Windows-side clip does nothing except guarantee the region is cleared.
    Kept so the relay app's CROP messages and the tray menu don't error."""
    log(f"[crop] ignored (handled by compact AVD resolution) active={active}")
    hwnd = _find_emulator_hwnd()
    if hwnd:
        _apply_crop_region(hwnd)

def _nudge_crop(px):
    """No-op (the old manual crop nudge). Kept so the tray menu doesn't error."""
    log("[crop] nudge ignored (handled by compact AVD resolution)")

def _is_emu_shown(hwnd):
    """Real on-screen state — True only if the window is visible AND not minimized.
    The watcher trusts THIS, not the _emulator_visible flag, so a manual minimize or
    any other out-of-band change can't desync auto show/hide."""
    if not hwnd:
        return False
    try:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return False
        if ctypes.windll.user32.IsIconic(hwnd):   # minimized
            return False
    except Exception:
        return False
    return True

def show_emulator(manual=False):
    """Raise the keyboard without stealing focus. manual=True marks it as a hotkey
    show (so physical typing won't auto-hide it)."""
    global _emulator_visible, _auto_shown, _target_hwnd
    hwnd = _find_emulator_hwnd()
    if not hwnd:
        log("[auto] show: emulator window not found")
        return
    # Snapshot the current target BEFORE showing the emulator so we don't
    # accidentally overwrite it with the emulator hwnd.
    cur_fg = ctypes.windll.user32.GetForegroundWindow()
    if cur_fg and not _is_emulator_window(cur_fg):
        with _target_lock:
            _target_hwnd = cur_fg
    # Apply touch-keyboard styles so taps never steal focus, then show without
    # activating. If it was MINIMIZED, SW_SHOWNA leaves it minimized — use
    # SW_SHOWNOACTIVATE to un-minimize without stealing focus.
    _make_keyboard_window(hwnd)
    if ctypes.windll.user32.IsIconic(hwnd):
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    else:
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWNA)
    _SetWindowPos(hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                  SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
    _emulator_visible = True
    _auto_shown = (not manual)
    log(f"[auto] shown (manual={manual})")

def hide_emulator():
    global _emulator_visible, _auto_shown
    hwnd = _find_emulator_hwnd()
    if not hwnd:
        return
    ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
    _emulator_visible = False
    _auto_shown = False
    log("[auto] hidden")

def toggle_emulator():
    hwnd = _find_emulator_hwnd()
    if not hwnd:
        log("[auto] toggle: emulator window not found")
        return
    # Decide from the REAL window state (visible & not minimized), so the hotkey
    # works even if the flag desynced (e.g. after a manual minimize).
    if _is_emu_shown(hwnd):
        hide_emulator()
    else:
        show_emulator(manual=True)

# ── Custom dark-grey title bar (host-drawn overlay) ──────────────────────────
# A borderless emulator window has no caption to drag or minimize. We draw our own
# slim dark-grey bar as a separate always-on-top, no-activate overlay floating
# directly BELOW the emulator: a "⌨ Gboard" label + a "—" minimize button. Dragging
# the bar moves the emulator with it; the minimize button hides the keyboard (same
# as Ctrl+Alt+K). A poll loop keeps the bar glued to the emulator's bottom edge and
# shows/hides it in lockstep with the emulator.
_BAR_BG      = "#2b2b2b"
_BAR_BG_HOT  = "#3a3a3a"
_BAR_FG      = "#d0d0d0"
_BAR_BTN_HOT = "#c0392b"   # red hover on the minimize button area

def _titlebar_thread():
    try:
        import tkinter as tk
    except Exception as e:
        log(f"[titlebar] tkinter unavailable, skipping custom bar: {e}")
        return

    u = ctypes.windll.user32

    # Bar height scaled to the emulator's monitor DPI (physical px, since the
    # process is per-monitor DPI aware). Default 2.0 for this 200% display.
    hwnd0 = _find_emulator_hwnd()
    scale = _window_scale(hwnd0) if hwnd0 else 2.0
    BAR_H  = max(28, int(round(24 * scale)))     # thicker bar
    FONT_PX = max(10, int(round(10 * scale)))
    RADIUS = max(8,  int(round(11 * scale)))     # rounded top-corner radius

    gdi = ctypes.windll.gdi32
    gdi.CreateRoundRectRgn.restype  = ctypes.c_void_p
    gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6

    root = tk.Tk()
    root.overrideredirect(True)            # no native Tk frame — just our bar
    root.attributes("-topmost", True)
    root.configure(bg=_BAR_BG)
    root.withdraw()                        # hidden until the emulator is shown

    # Stamp the Tk top-level so it never steals focus and never shows in taskbar.
    def _stamp_bar():
        h = root.winfo_id()
        p = u.GetParent(h)
        target = p if p else h
        ex = _GetWindowLongPtr(target, GWL_EXSTYLE)
        ex |= WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
        ex &= ~WS_EX_APPWINDOW
        _SetWindowLongPtr(target, GWL_EXSTYLE, ex)
        return target

    bar_hwnd = [0]

    container = tk.Frame(root, bg=_BAR_BG, height=BAR_H)
    container.pack(fill="both", expand=True)

    title = tk.Label(container, text="⌨ Gboard", bg=_BAR_BG, fg=_BAR_FG,
                     font=("Segoe UI", FONT_PX), anchor="w", padx=int(8 * scale))
    title.pack(side="left", fill="both", expand=True)

    btn = tk.Label(container, text="—", bg=_BAR_BG, fg=_BAR_FG,
                   font=("Segoe UI", FONT_PX), width=4)
    btn.pack(side="right", fill="y")

    # ── Minimize button ──
    def _on_min_enter(_):  btn.configure(bg=_BAR_BTN_HOT, fg="#ffffff")
    def _on_min_leave(_):  btn.configure(bg=_BAR_BG, fg=_BAR_FG)
    def _on_min_click(_):
        try:
            hide_emulator()
        except Exception as e:
            log(f"[titlebar] minimize failed: {e}")
    btn.bind("<Enter>", _on_min_enter)
    btn.bind("<Leave>", _on_min_leave)
    btn.bind("<Button-1>", _on_min_click)

    # ── Drag the bar → move the emulator with it ──
    drag = {"cx": 0, "cy": 0, "ex": 0, "ey": 0, "hwnd": 0}

    def _cursor():
        pt = _POINT(); u.GetCursorPos(ctypes.byref(pt)); return pt.x, pt.y

    def _on_drag_start(_):
        h = _find_emulator_hwnd()
        if not h:
            return
        wr = _RECT(); _GetWindowRect(ctypes.c_void_p(h), ctypes.byref(wr))
        drag["cx"], drag["cy"] = _cursor()
        drag["ex"], drag["ey"] = wr.left, wr.top
        drag["hwnd"] = h

    def _on_drag_move(_):
        h = drag["hwnd"]
        if not h:
            return
        cx, cy = _cursor()
        nx = drag["ex"] + (cx - drag["cx"])
        ny = drag["ey"] + (cy - drag["cy"])
        # Move the emulator; the sync loop reglues the bar above it. Move the bar
        # immediately too so it tracks the cursor without a 1-tick lag.
        _SetWindowPos(ctypes.c_void_p(h), ctypes.c_void_p(HWND_TOPMOST),
                      nx, ny, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        wr = _RECT(); _GetWindowRect(ctypes.c_void_p(h), ctypes.byref(wr))
        w = wr.right - wr.left
        root.geometry(f"{w}x{BAR_H}+{wr.left}+{wr.bottom}")

    for w in (title, container):
        w.bind("<Button-1>", _on_drag_start)
        w.bind("<B1-Motion>", _on_drag_move)

    # Round only the BOTTOM corners: the region starts RADIUS above the bar so the
    # top rounded corners fall above the window and are clipped, leaving the top
    # edge square so it meets the emulator's bottom flush.
    def _round_bottom(hwnd, w):
        rgn = gdi.CreateRoundRectRgn(0, -RADIUS, w + 1, BAR_H + 1,
                                     RADIUS * 2, RADIUS * 2)
        _SetWindowRgn(ctypes.c_void_p(hwnd), ctypes.c_void_p(rgn), True)

    # ── Sync loop: keep the bar glued above the emulator, mirror show/hide ──
    state = {"shown": None, "rw": 0}
    def _sync():
        try:
            h = _find_emulator_hwnd()
            if h and _is_emu_shown(h):
                wr = _RECT(); _GetWindowRect(ctypes.c_void_p(h), ctypes.byref(wr))
                w = wr.right - wr.left
                root.geometry(f"{w}x{BAR_H}+{wr.left}+{wr.bottom}")
                if state["shown"] is not True:
                    root.deiconify()
                    if not bar_hwnd[0]:
                        bar_hwnd[0] = _stamp_bar()
                    state["shown"] = True
                    state["rw"] = 0          # force region re-apply after re-show
                if bar_hwnd[0] and w != state["rw"]:
                    _round_bottom(bar_hwnd[0], w)
                    state["rw"] = w
                # Re-assert topmost above the emulator without activating.
                if bar_hwnd[0]:
                    _SetWindowPos(ctypes.c_void_p(bar_hwnd[0]),
                                  ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                                  SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            else:
                if state["shown"] is not False:
                    root.withdraw()
                    state["shown"] = False
        except Exception as e:
            log(f"[titlebar] sync error: {e}")
        root.after(120, _sync)

    root.after(200, _sync)
    log("[titlebar] custom dark-grey title bar started")
    try:
        root.mainloop()
    except Exception as e:
        log(f"[titlebar] mainloop ended: {e}")

# ── Android bi-directional sync (host → relay app) ───────────────────────────

def send_to_android(cmd: str) -> bool:
    """Write a newline-terminated command to the connected Android relay app.
    Returns True only if the bytes were actually handed to a live socket."""
    with _client_lock:
        conn = _client_conn
    if conn is None:
        return False
    try:
        conn.sendall((cmd + "\n").encode("utf-8"))
        return True
    except Exception as e:
        log(f"[sync] send_to_android error: {e}")
        return False


def _get_cursor_only(ctrl):
    """Return (sel_start, sel_end) char offsets from a UIA control, or None.
    Tries EM_GETSEL first (fast, cross-process safe for Win32 Edit controls).
    Returns None for controls that don't expose their cursor this way."""
    if ctrl is None:
        return None
    try:
        hwnd = ctrl.NativeWindowHandle
        if hwnd:
            EM_GETSEL = 0x00B0
            ret = ctypes.windll.user32.SendMessageW(
                ctypes.c_void_p(hwnd), EM_GETSEL, 0, 0)
            ret_u = ret & 0xFFFFFFFF          # treat as unsigned 32-bit
            if ret_u != 0xFFFFFFFF:           # 0xFFFF_FFFF = selection too large
                s = ret_u & 0xFFFF
                e = (ret_u >> 16) & 0xFFFF
                return (s, e)
    except Exception:
        pass
    return None


def _sync_field_to_android_thread():
    """Background thread: read the focused Windows field (text + cursor) and send
    SYNC to the Android relay app so Gboard's buffer matches the new field."""
    ctypes.windll.ole32.CoInitialize(None)
    try:
        import uiautomation as auto
        auto.SetGlobalSearchTimeout(0.5)
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg and _is_emulator_window(fg):
            return
        ctrl = auto.GetFocusedControl()
        if ctrl is None or not _is_editable_focus(ctrl):
            return

        # 1. Read text — ValuePattern first (simple Edit), then TextPattern (rich text)
        text = None
        try:
            vp = ctrl.GetValuePattern()
            if vp is not None:
                text = vp.Value
        except Exception:
            pass
        if text is None:
            try:
                tp = ctrl.GetTextPattern()
                if tp is not None:
                    text = tp.DocumentRange.GetText(8000)
            except Exception:
                pass
        if text is None:
            text = ""
        # Cap: Android relay trims buffer at 800 chars; send last 4000 for context
        if len(text) > 4000:
            text = text[-4000:]

        # 2. Read cursor (fast path: EM_GETSEL; fallback: cursor at end)
        cur = _get_cursor_only(ctrl)
        if cur is not None:
            sel_start = max(0, min(cur[0], len(text)))
            sel_end   = max(0, min(cur[1], len(text)))
        else:
            sel_start = sel_end = len(text)   # default: cursor at end

        # 3. Send. Retry briefly so a focus change that lands during the startup
        #    connection handshake isn't silently dropped (SYNC is one-shot per field).
        text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        msg = f"SYNC:{text_b64}:{sel_start}:{sel_end}"
        sent = False
        for _ in range(10):
            if send_to_android(msg):
                sent = True
                break
            time.sleep(0.3)
        if sent:
            log(f"[sync] SYNC sent: len={len(text)} sel={sel_start}:{sel_end}")
        else:
            log(f"[sync] SYNC dropped (no client connected): len={len(text)}")
    except Exception as e:
        log(f"[sync] _sync_field_to_android_thread error: {e}")
    finally:
        ctypes.windll.ole32.CoUninitialize()


# ── Auto show/hide: focus watcher (UI Automation) ────────────────────────────
# Editable control types we treat as "an input box" (by ControlTypeName).
_EDITABLE_CTRLS = {"EditControl", "DocumentControl", "ComboBoxControl"}

def _is_editable_focus(ctrl):
    """True if the UIA-focused control is a writable text field."""
    if ctrl is None:
        return False
    try:
        if ctrl.ControlTypeName not in _EDITABLE_CTRLS:
            return False
    except Exception:
        return False
    # Reject read-only fields (labels rendered as Edit, disabled boxes, etc.)
    try:
        if not ctrl.IsEnabled:
            return False
    except Exception:
        pass
    try:
        vp = ctrl.GetValuePattern()
        if vp is not None and vp.IsReadOnly:
            return False
    except Exception:
        pass
    return True

def _sync_key(fg, ctrl):
    """Stable per-field identity used to decide when to push a fresh SYNC to Gboard.

    Deliberately EXCLUDES geometry: a multiline input box grows as you type, so its
    BoundingRectangle changes on every keystroke. If the SYNC trigger keyed on that,
    every character would look like a 'new field', re-fire SYNC, and setText() would
    wipe the half-typed buffer (the intermittent-loss bug). We key on the control's
    durable identity instead (native handle / UIA RuntimeId / type+name)."""
    if ctrl is None:
        return None
    nwh = 0
    try:
        nwh = ctrl.NativeWindowHandle or 0
    except Exception:
        pass
    rid = None
    try:
        rid = tuple(ctrl.GetRuntimeId())
    except Exception:
        pass
    ct = name = None
    try:
        ct = ctrl.ControlTypeName
    except Exception:
        pass
    try:
        name = ctrl.Name          # accessibility label/placeholder — stable per field
    except Exception:
        pass
    return (fg, ct, nwh, rid, name)

def _focus_sig(fg, ctrl):
    """A stable-per-field, distinct-across-fields signature of the focused control,
    used to suppress re-showing the keyboard on the same field right after the user
    types on the hardware keyboard."""
    if ctrl is None:
        return ("none", fg)
    rect = name = ct = None
    try:
        r = ctrl.BoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    try:
        name = ctrl.Name
    except Exception:
        pass
    try:
        ct = ctrl.ControlTypeName
    except Exception:
        pass
    return (fg, ct, name, rect)

def _focus_watcher():
    """Poll the keyboard-focused UIA element. When an input box gains focus, raise
    the keyboard; when focus leaves to a non-editable element, auto-hide it.
    Skips the emulator's own windows so a Gboard tap never triggers a hide."""
    try:
        import uiautomation as auto
    except Exception as e:
        log(f"[auto] uiautomation unavailable: {e!r}")
        return
    auto.SetGlobalSearchTimeout(0.5)
    log("[auto] focus watcher started")
    last_beat     = time.time()
    suppress_sig  = None   # focus signature where physical typing hid the keyboard;
                           # don't re-raise for the SAME field until focus moves away.
    last_sync_key  = None  # stable identity of the last field we sent SYNC for
    last_cursor    = None  # (sel_start, sel_end) of last CURSOR command sent
    while True:
        # The ENTIRE body is guarded: a single uncaught exception here used to kill
        # the thread, silently disabling auto show/hide "after a while". Now it logs
        # and keeps polling no matter what.
        try:
            # Wake immediately on a physical keypress (hook sets the event), else poll.
            typed = _hide_request.wait(0.25)
            if typed:
                _hide_request.clear()
            # Heartbeat every ~60s so the log can prove the watcher is still alive.
            now = time.time()
            if now - last_beat >= 60:
                last_beat = now
                rs = _is_emu_shown(_find_emulator_hwnd())
                log(f"[auto] watcher alive (mode={_auto_mode} shown={rs} auto={_auto_shown})")
            if not _auto_mode:
                continue
            try:
                ctrl = auto.GetFocusedControl()
            except Exception:
                ctrl = None
            # If focus is on the emulator itself, leave state untouched.
            try:
                fg = ctypes.windll.user32.GetForegroundWindow()
            except Exception:
                fg = 0
            if fg and _is_emulator_window(fg):
                continue
            sig = _focus_sig(fg, ctrl)
            editable = _is_editable_focus(ctrl)
            # Trust the REAL window state, not the flag — a manual minimize/close
            # otherwise desyncs the flag and auto-show stops working entirely.
            shown = _is_emu_shown(_find_emulator_hwnd())

            if typed:
                # Physical typing: hide an auto-shown keyboard and remember THIS field
                # so we don't pop right back up while it still has focus.
                if shown and _auto_shown:
                    hide_emulator()
                    suppress_sig = sig
                continue

            # Focus moved off the suppressed field → clear the suppression.
            if suppress_sig is not None and sig != suppress_sig:
                suppress_sig = None

            if editable:
                if not shown and sig != suppress_sig:
                    show_emulator(manual=False)
                # ── Android buffer sync ─────────────────────────────────────
                skey = _sync_key(fg, ctrl)
                since_inject = time.time() - _last_inject_time
                if skey is not None and skey != last_sync_key:
                    # Genuinely a DIFFERENT field. Only push a fresh SYNC once the
                    # relay has been quiet for >1s — otherwise a field switch right
                    # after typing (or a stray re-focus mid-glide) would setText()
                    # over the buffer the user is still building. If the guard is
                    # active we leave last_sync_key unchanged so it retries next tick.
                    if since_inject > 1.0:
                        last_sync_key = skey
                        last_cursor   = None
                        threading.Thread(
                            target=_sync_field_to_android_thread, daemon=True).start()
                elif skey is not None and skey == last_sync_key \
                        and not typed and since_inject > 0.4:
                    # Same field: poll cursor for mouse-click repositioning.
                    # EM_GETSEL is fast enough to call inline every 250 ms.
                    try:
                        cur = _get_cursor_only(ctrl)
                        if cur is not None and cur != last_cursor:
                            last_cursor = cur
                            send_to_android(f"CURSOR:{cur[0]}:{cur[1]}")
                    except Exception:
                        pass
            else:
                # Only auto-hide a keyboard that auto raised — never a manual one.
                if shown and _auto_shown:
                    hide_emulator()
                # NOTE: do NOT clear last_sync_key on a non-editable blip. Focus
                # briefly bounces off the field while the keyboard shows/hides; if we
                # reset here, returning to the SAME field would re-SYNC and wipe the
                # buffer. The key only changes when focus lands on a truly new field.
        except Exception as e:
            log(f"[auto] watcher loop error (continuing): {e!r}")
            time.sleep(0.5)

# ── Auto-hide on physical typing (low-level keyboard hook) ────────────────────
LLKHF_INJECTED = 0x10
WH_KEYBOARD_LL = 13
WM_KEYDOWN_   = 0x0100
WM_SYSKEYDOWN_ = 0x0104
_MOD_VKS = {0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C,
            0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}  # shift/ctrl/alt/caps/win (+L/R)

class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", ctypes.wintypes.DWORD),
                ("scanCode", ctypes.wintypes.DWORD),
                ("flags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int,
                               ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
_hook_ref = None   # keep the CFUNCTYPE alive

# 64-bit-safe bindings — without argtypes the function pointer / handle get
# truncated to 32 bits and SetWindowsHookExW fails.
ctypes.windll.user32.SetWindowsHookExW.restype  = ctypes.c_void_p
ctypes.windll.user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, _HOOKPROC, ctypes.c_void_p, ctypes.wintypes.DWORD]
ctypes.windll.user32.CallNextHookEx.restype  = ctypes.c_longlong
ctypes.windll.user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
ctypes.windll.kernel32.GetModuleHandleW.restype  = ctypes.c_void_p
ctypes.windll.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
_UnhookWindowsHookEx = ctypes.windll.user32.UnhookWindowsHookEx
_UnhookWindowsHookEx.restype  = ctypes.c_bool
_UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
_SetTimer = ctypes.windll.user32.SetTimer
_SetTimer.restype  = ctypes.c_void_p
_SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]

# The hook callback MUST be fast: Windows can drop a low-level hook whose proc is
# slow. So the proc only sets this Event; the focus watcher (which already owns all
# show/hide decisions, single-threaded) performs the actual hide on its next tick.
_hide_request = threading.Event()

def _kbd_hook_thread():
    """Hide an auto-shown keyboard the moment the user types on the PHYSICAL
    keyboard (mirrors how the Windows touch keyboard yields to hardware input).
    Injected keystrokes (our own SendInput) carry LLKHF_INJECTED and are ignored,
    and Ctrl+Alt+K is ignored so the hotkey still works."""
    global _hook_ref

    def _proc(nCode, wParam, lParam):
        try:
            if nCode == 0 and wParam in (WM_KEYDOWN_, WM_SYSKEYDOWN_):
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                injected = bool(kb.flags & LLKHF_INJECTED)
                vk = kb.vkCode
                if (not injected) and vk not in _MOD_VKS:
                    # Ignore while Ctrl+Alt are held (that's the show/hide hotkey chord).
                    ga = ctypes.windll.user32.GetAsyncKeyState
                    ctrl_alt = (ga(0x11) & 0x8000) and (ga(0x12) & 0x8000)
                    if not ctrl_alt and _emulator_visible and _auto_shown:
                        _hide_request.set()   # offload — keep this proc instant
        except Exception:
            pass
        return ctypes.windll.user32.CallNextHookEx(None, nCode, wParam, lParam)

    _hook_ref = _HOOKPROC(_proc)
    hmod = ctypes.windll.kernel32.GetModuleHandleW(None)
    handle = ctypes.windll.user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_ref, hmod, 0)
    if not handle:
        log("[auto] failed to install keyboard hook")
        return
    log("[auto] keyboard hook installed")
    # Windows can silently drop a low-level hook (e.g. after a slow proc) and posts
    # NO message when it does, so GetMessage can't detect it. A periodic timer lets
    # us reinstall a fresh hook unconditionally — cheap insurance against it dying.
    WM_TIMER = 0x0113
    _SetTimer(None, 0, 5000, None)
    msg = ctypes.wintypes.MSG()
    while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_TIMER:
            _UnhookWindowsHookEx(handle)
            handle = ctypes.windll.user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_ref, hmod, 0)
            if not handle:
                log("[auto] hook reinstall failed; retrying next tick")
        ctypes.windll.user32.TranslateMessage(ctypes.byref(msg))
        ctypes.windll.user32.DispatchMessageW(ctypes.byref(msg))

# ── Global hotkey (Ctrl+Alt+K) ───────────────────────────────────────────────
def hotkey_thread():
    ctypes.windll.user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT, VK_K)
    msg = ctypes.wintypes.MSG()
    while ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY and msg.wParam == 1:
            toggle_emulator()

# ── System tray icon ─────────────────────────────────────────────────────────
def _make_icon_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=12, fill="#1565C0")
    d.text((10, 18), "KB", fill="white")
    return img

def run_tray():
    def on_toggle(icon, item):  toggle_emulator()
    def on_dock(icon, item):    _dock_emulator_bottom()
    def on_adb(icon, item):     setup_adb_reverse()
    def on_quit(icon, item):    icon.stop(); os._exit(0)

    def on_auto(icon, item):
        global _auto_mode
        _auto_mode = not _auto_mode
        print(f"[GboardHost] Auto show/hide = {_auto_mode}")
    def auto_checked(item):     return _auto_mode

    def size_action(w, h):
        return lambda icon, item: _set_size(w, h)

    icon = pystray.Icon(
        "GboardIME",
        _make_icon_image(),
        "GboardIME Host (Ctrl+Alt+K = toggle keyboard)",
        menu=pystray.Menu(
            pystray.MenuItem("Toggle keyboard  [Ctrl+Alt+K]", on_toggle, default=True),
            pystray.MenuItem("Auto show/hide", on_auto, checked=auto_checked),
            pystray.MenuItem("Dock to bottom-right",          on_dock),
            pystray.MenuItem("Keyboard size", pystray.Menu(
                pystray.MenuItem("Small",  size_action(300, 375)),
                pystray.MenuItem("Medium", size_action(380, 475)),
                pystray.MenuItem("Large",  size_action(460, 575)),
                pystray.MenuItem("XLarge", size_action(560, 700)),
            )),
            pystray.MenuItem("Re-apply ADB reverse",          on_adb),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",                           on_quit),
        ),
    )
    icon.run()

# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. Start TCP server
    t_srv = threading.Thread(target=server_thread, daemon=True)
    t_srv.start()

    # 2. Set up ADB reverse (retry in background until emulator is ready)
    def adb_retry():
        for _ in range(30):
            time.sleep(3)
            try:
                setup_adb_reverse()
                return
            except Exception:
                pass
    threading.Thread(target=adb_retry, daemon=True).start()

    # 3. Target window tracker
    threading.Thread(target=_target_tracker, daemon=True).start()

    # 3b. Keep re-stamping the emulator's child render window (Qt recreates it)
    threading.Thread(target=_keyboard_watchdog, daemon=True).start()

    # 3c. Auto show/hide: UIA focus watcher + physical-typing hook
    threading.Thread(target=_focus_watcher, daemon=True).start()
    threading.Thread(target=_kbd_hook_thread, daemon=True).start()

    # 3d. Custom dark-grey title bar overlay (drag + minimize) above the emulator
    threading.Thread(target=_titlebar_thread, daemon=True).start()

    # 4. Global hotkey listener
    t_hk = threading.Thread(target=hotkey_thread, daemon=True)
    t_hk.start()

    # 4. Dock emulator after a short delay, then start hidden so the focus watcher
    #    can raise it on demand (mirrors the Windows touch keyboard).
    def deferred_dock():
        global _emulator_visible
        time.sleep(8)
        _dock_emulator_bottom()
        if _auto_mode:
            hwnd = _find_emulator_hwnd()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
                _emulator_visible = False
    threading.Thread(target=deferred_dock, daemon=True).start()

    print("[GboardHost] Started. Ctrl+Alt+K toggles emulator. Right-click tray to quit.")
    run_tray()
