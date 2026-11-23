"""
Screen Balance - a small Windows tray app that helps you not overuse your PC.

Features
  * Break reminders after N minutes of continuous active use
  * Daily screen-time limit with gentle warnings (at 80%, 100%, then every 30 min)
  * Per-app usage tracking with a dashboard and 7-day history

Only *active* time is counted: if there's no mouse/keyboard input for a while
(idle threshold), the clock stops. An idle stretch as long as the break length
counts as a break and resets the continuous-use timer.

Data lives in %USERPROFILE%\\.screenbalance (config.json, usage.json).
"""

import ctypes
import ctypes.wintypes as wt
import datetime as dt
import json
import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
import traceback

import psutil
import pystray
from PIL import ImageTk

import design
from design import THEME, hexc, fmt_dur

APP_NAME = "ScreenBalance"
# Not under %APPDATA%: Microsoft Store Python silently redirects writes there to a hidden folder.
DATA_DIR = os.path.join(os.path.expanduser("~"), ".screenbalance")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
USAGE_PATH = os.path.join(DATA_DIR, "usage.json")

DEFAULT_CONFIG = {
    "break_interval_min": 50,   # remind after this much continuous use
    "break_length_min": 5,      # idle this long = you took a break
    "snooze_min": 10,
    "daily_limit_hours": 6.0,
    "idle_threshold_sec": 120,  # no input for this long = not counting time
    "over_limit_repeat_min": 30,
    "theme": "system",          # "system" (follow Windows), "light" or "dark"
    "startup_notice": True,     # "Screen Balance is running" notification when it starts
}
THEMES = ("system", "light", "dark")

TICK_MS = 1000  # 1 s so the "Break in" countdown ticks smoothly
SAVE_EVERY_SEC = 30
POPUP_TIMEOUT_SEC = 300  # an unanswered popup snoozes itself after 5 minutes
MIN_OPEN_BREAK_SEC = 10  # an open break shorter than this was probably a misclick
KEEP_DAYS = 60

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.GetTickCount.restype = wt.DWORD
user32.GetForegroundWindow.restype = wt.HWND
user32.GetParent.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]
user32.OpenInputDesktop.restype = wt.HANDLE
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]


# --------------------------------------------------------------------------- #
# Windows helpers
# --------------------------------------------------------------------------- #
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    millis = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return millis / 1000.0


kernel32.GetTickCount64.restype = ctypes.c_ulonglong


def uptime() -> float:
    """Seconds since boot. Unlike time.time() it ignores clock changes, and unlike
    time.monotonic() it is guaranteed to keep counting while the PC sleeps."""
    return kernel32.GetTickCount64() / 1000.0


def session_locked() -> bool:
    """True while Windows is locked (the lock screen owns the input desktop)."""
    desk = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    user32.CloseDesktop(desk)
    return False


def _window_pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_ENUM_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def _uwp_child_pid(hwnd, frame_pid) -> int:
    """Store (UWP) apps run inside ApplicationFrameHost; the real app is a child window."""
    found = []

    def cb(child, _):
        pid = _window_pid(child)
        if pid and pid != frame_pid:
            found.append(pid)
            return False
        return True
    user32.EnumChildWindows(hwnd, _ENUM_PROC(cb), 0)
    return found[0] if found else frame_pid


def foreground_app() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "Desktop"
    pid = _window_pid(hwnd)
    if pid == os.getpid():
        return "Screen Balance"
    try:
        name = psutil.Process(pid).name()
        if name.lower() == "applicationframehost.exe":
            name = psutil.Process(_uwp_child_pid(hwnd, pid)).name()
    except (psutil.Error, ValueError):
        return "Unknown"
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return {"explorer": "Desktop / Explorer", "LockApp": "Lock screen"}.get(name, name)


_mutex = None


def single_instance() -> bool:
    """One copy per Windows user session (Local\\ so other accounts aren't blocked)."""
    global _mutex
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_mutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def log_error(msg: str) -> None:
    """pythonw has no console, so errors go to a small log file instead."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "error.log")
        if os.path.exists(path) and os.path.getsize(path) > 256_000:
            os.replace(path, path + ".old")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def _personalize_flag(name: str, default: int = 1) -> int:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


def windows_uses_dark_mode() -> bool:
    """True when Windows' app mode is set to Dark (Settings > Personalization > Colors)."""
    return _personalize_flag("AppsUseLightTheme") == 0


def taskbar_is_dark() -> bool:
    """True when the taskbar uses the dark Windows mode (it can differ from the app mode)."""
    return _personalize_flag("SystemUsesLightTheme", 0) == 0


def style_titlebar(win):
    """Windows 11 title bar matching the current page color (light or dark)."""
    try:
        win.update_idletasks()
        hwnd = user32.GetParent(win.winfo_id())
        dark = ctypes.c_int(1 if THEME["dark"] else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), 4)
        r, g, b = THEME["bg"]
        color = ctypes.c_int(r | (g << 8) | (b << 16))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)
    except (AttributeError, OSError):
        pass


# --------------------------------------------------------------------------- #
# Autostart (HKCU Run key)
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_command() -> str:
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class UnreadableFile(Exception):
    """The file exists but can't be opened right now (locked by antivirus, backup, ...)."""


def load_json(path, default):
    """Read a JSON object. Missing file -> default. Damaged JSON -> kept aside as a backup,
    default returned. Locked file -> retried, then UnreadableFile (never treated as damaged,
    so a temporary lock can't wipe your history)."""
    last_err = None
    for attempt in range(5):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            raise ValueError("not a JSON object")
        except FileNotFoundError:
            return default
        except ValueError as e:
            backup = f"{path}.corrupt-{dt.datetime.now():%Y%m%d-%H%M%S}"
            try:
                os.replace(path, backup)
            except OSError:
                pass
            log_error(f"Could not read {path} ({e}); moved it to {backup}")
            return default
        except OSError as e:
            last_err = e
            time.sleep(0.3 * (attempt + 1))
    raise UnreadableFile(f"{path}: {last_err}")


# key: (lowest, highest, is_float)
CONFIG_LIMITS = {
    "break_interval_min": (5, 240, False),
    "break_length_min": (1, 60, False),
    "snooze_min": (1, 60, False),
    "daily_limit_hours": (0, 24, True),
    "idle_threshold_sec": (30, 1800, False),
    "over_limit_repeat_min": (5, 240, False),
}


def clean_config(raw: dict) -> dict:
    """Fill in defaults and clamp values so a hand-edited config can't crash the app."""
    cfg = dict(DEFAULT_CONFIG)
    for key, (lo, hi, is_float) in CONFIG_LIMITS.items():
        try:
            v = float(raw.get(key, cfg[key]))
        except (TypeError, ValueError):
            continue
        if v != v:  # NaN
            continue
        v = max(lo, min(hi, v))
        cfg[key] = v if is_float else int(v)
    if raw.get("theme") in THEMES:
        cfg["theme"] = raw["theme"]
    if isinstance(raw.get("startup_notice"), bool):
        cfg["startup_notice"] = raw["startup_notice"]
    return cfg


def clean_usage(raw: dict) -> dict:
    out = {}
    for day, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        try:
            dt.date.fromisoformat(day)
            apps = {str(k): float(v) for k, v in (rec.get("apps") or {}).items()}
            hours = {str(int(k)): float(v) for k, v in (rec.get("hours") or {}).items() if 0 <= int(k) < 24}
            entry = {
                "total": float(rec.get("total", 0)),
                "apps": apps,
                "hours": hours,
                "breaks": int(rec.get("breaks", 0)),
                "break_sec": float(rec.get("break_sec", 0)),
                "longest": float(rec.get("longest", 0)),
            }
            for k in ("first", "last"):
                if isinstance(rec.get(k), str):
                    entry[k] = rec[k][:5]
            out[day] = entry
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def merge_usage(base: dict, extra: dict) -> dict:
    """Add the days/times tracked in `extra` onto `base` (used after a locked-file startup)."""
    out = {k: dict(v, apps=dict(v["apps"]), hours=dict(v["hours"])) for k, v in base.items()}
    for day, rec in extra.items():
        cur = out.setdefault(day, {"total": 0.0, "apps": {}, "hours": {}, "breaks": 0,
                                   "break_sec": 0.0, "longest": 0.0})
        cur["total"] += rec.get("total", 0.0)
        for k in ("apps", "hours"):
            for name, secs in rec.get(k, {}).items():
                cur[k][name] = cur[k].get(name, 0.0) + secs
        cur["breaks"] += rec.get("breaks", 0)
        cur["break_sec"] += rec.get("break_sec", 0.0)
        cur["longest"] = max(cur.get("longest", 0.0), rec.get("longest", 0.0))
        if rec.get("first") and (not cur.get("first") or _hm(rec["first"]) < _hm(cur["first"])):
            cur["first"] = rec["first"]
        if rec.get("last") and (not cur.get("last") or _hm(rec["last"]) > _hm(cur["last"])):
            cur["last"] = rec["last"]
    return out


def _hm(stamp: str) -> int:
    try:
        h, m = stamp.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return 0


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def today_key() -> str:
    return dt.date.today().isoformat()


# --------------------------------------------------------------------------- #
# Small dark-themed Tk widgets
# --------------------------------------------------------------------------- #
def flat_button(parent, text, command, primary=False, width=None):
    bg = THEME["a1"] if primary else THEME["bg"]
    fg = (255, 255, 255) if primary else THEME["text"]
    hover = THEME["a1_hover"] if primary else THEME["card_hi"]
    b = tk.Label(parent, text=text, bg=hexc(bg), fg=hexc(fg), cursor="hand2",
                 font=("Segoe UI Semibold", 10), padx=16, pady=5, width=width,
                 highlightthickness=0 if primary else 1, highlightbackground=hexc(THEME["border_hi"]))
    b.bind("<Enter>", lambda e: b.config(bg=hexc(hover)))
    b.bind("<Leave>", lambda e: b.config(bg=hexc(bg)))
    b.bind("<Button-1>", lambda e: command())
    return b


# --------------------------------------------------------------------------- #
# Background rendering
# --------------------------------------------------------------------------- #
class RenderWorker:
    """Draws images with Pillow on a background thread so the UI thread never stalls.

    Each screen submits under its own key; a newer request replaces one that hasn't
    started yet, so the worker only ever draws the latest state. Finished images are
    handed back to the Tk thread (which alone may touch Tk) by polling at ~120 fps
    while work is in flight, and not at all when idle."""

    def __init__(self, root):
        self.root = root
        self._jobs = {}              # key -> (fn, args, callback)
        self._busy = 0
        self._cv = threading.Condition()
        self._results = queue.Queue()
        self._polling = False
        threading.Thread(target=self._run, daemon=True, name="render").start()

    def submit(self, key, fn, args, callback):
        with self._cv:
            self._jobs[key] = (fn, args, callback)
            self._cv.notify()
        if not self._polling:
            self._polling = True
            self.root.after(8, self._poll)

    def cancel(self, key):
        with self._cv:
            self._jobs.pop(key, None)

    def _run(self):
        while True:
            with self._cv:
                while not self._jobs:
                    self._cv.wait()
                key = next(iter(self._jobs))
                fn, args, callback = self._jobs.pop(key)
                self._busy += 1
            try:
                result = fn(*args)
            except Exception:
                log_error(f"render {key!r} failed:\n{traceback.format_exc()}")
                result = None
            with self._cv:
                self._busy -= 1
            if result is not None:
                self._results.put((key, callback, result))

    def _poll(self):
        delivered = {}
        try:
            while True:
                key, callback, result = self._results.get_nowait()
                delivered[key] = (callback, result)   # only the newest per screen matters
        except queue.Empty:
            pass
        for callback, result in delivered.values():
            try:
                callback(result)
            except Exception:
                log_error(f"applying a render failed:\n{traceback.format_exc()}")
        with self._cv:
            pending = bool(self._jobs) or self._busy > 0
        if pending or not self._results.empty():
            self.root.after(8, self._poll)
        else:
            self._polling = False


class FastTimer:
    """Windows' timer normally fires every ~15.6 ms, capping animations at ~64 fps.
    While an animation plays we ask for 1 ms resolution (~120 fps), then give it back
    straight away so it doesn't cost battery the rest of the time."""

    _users = 0

    @classmethod
    def acquire(cls):
        if cls._users == 0:
            try:
                ctypes.WinDLL("winmm").timeBeginPeriod(1)
            except OSError:
                pass
        cls._users += 1

    @classmethod
    def release(cls):
        if cls._users <= 0:
            return
        cls._users -= 1
        if cls._users == 0:
            try:
                ctypes.WinDLL("winmm").timeEndPeriod(1)
            except OSError:
                pass


class Animator:
    """Plays short animations in lockstep with the display.

    Each frame first waits for the next screen refresh (DwmFlush), then applies the new
    state, so a 240 Hz monitor gets 240 evenly spaced frames per second, a 60 Hz one 60.
    Progress is computed from real elapsed time, so a late frame catches up instead of
    making the motion drag. Nothing runs (and no power is used) when nothing animates."""

    def __init__(self, root):
        self.root = root
        self._anims = []
        self._running = False
        try:
            self._vsync = ctypes.WinDLL("dwmapi").DwmFlush
        except (OSError, AttributeError):
            self._vsync = None

    def play(self, duration, step):
        """step(t) gets progress 0..1 each frame; return False to stop early."""
        self._anims.append((time.perf_counter(), max(duration, 1e-3), step))
        if not self._running:
            self._running = True
            if self._vsync is None:
                FastTimer.acquire()
            self.root.after(0, self._frame)

    def _frame(self):
        if self._vsync is not None:
            try:
                self._vsync()          # wait for the next display refresh
            except OSError:
                self._vsync = None
                FastTimer.acquire()
        now = time.perf_counter()
        keep = []
        for began, duration, step in self._anims:
            t = min(1.0, (now - began) / duration)
            try:
                alive = step(t) is not False
            except Exception:
                log_error("animation frame failed:" + os.linesep + traceback.format_exc())
                alive = False
            if alive and t < 1.0:
                keep.append((began, duration, step))
        self._anims = keep
        if keep:
            self.root.after(0 if self._vsync is not None else 4, self._frame)
        else:
            self._running = False
            if self._vsync is None:
                FastTimer.release()


class HoverLayer:
    """Instant hover for a rendered page.

    The page itself is always drawn without hover. For each button, its hovered look is
    drawn once in the background, cropped to the button and cached; hovering then just
    shows that small image on top of the page, on the next display frame, instead of
    waiting ~25-50 ms for a full redraw."""

    def __init__(self, app, canvas, name):
        self.app, self.canvas, self.name = app, canvas, name
        self.item = canvas.create_image(0, 0, anchor="nw", state="hidden")
        self.cache = {}        # (context, bid) -> (PhotoImage, (x, y) in page pixels)
        self.pending = set()
        self.hits = []         # [(bid, box)] in page pixels
        self.origin = (0, 0)   # where the page's top-left sits on the canvas
        self.context = None
        self.hover = None

    def update(self, hits, origin, context, render_hovered):
        """After each page render. render_hovered(bid) -> (image, hits), run in the background."""
        self.hits, self.origin = hits, origin
        if context != self.context:
            self.context = context
            if len(self.cache) > 64:
                self.cache.clear()
        for bid, box in hits:
            ck = (context, bid)
            if ck in self.cache or ck in self.pending:
                continue
            self.pending.add(ck)
            self.app.renderer.submit(f"{self.name}:hover:{ck}", self._crop_job,
                                     (render_hovered, bid, box), lambda res, ck=ck: self._store(ck, res))
        self._show()

    @staticmethod
    def _crop_job(render_hovered, bid, box):
        img, _ = render_hovered(bid)
        x0, y0, x1, y1 = box
        x0, y0 = max(0, int(x0) - 2), max(0, int(y0) - 2)
        x1, y1 = min(img.width, math.ceil(x1) + 2), min(img.height, math.ceil(y1) + 2)
        return img.crop((x0, y0, x1, y1)), (x0, y0)

    def _store(self, ck, result):
        self.pending.discard(ck)
        try:
            if not self.canvas.winfo_exists():
                return
        except tk.TclError:
            return
        crop, pos = result
        self.cache[ck] = (ImageTk.PhotoImage(crop), pos)
        if ck == (self.context, self.hover):
            self._show()

    def hit(self, x, y):
        ox, oy = self.origin
        for bid, (x0, y0, x1, y1) in self.hits:
            if x0 + ox <= x <= x1 + ox and y0 + oy <= y <= y1 + oy:
                return bid
        return None

    def set_hover(self, bid):
        if bid != self.hover:
            self.hover = bid
            self._show()

    def _show(self):
        found = self.cache.get((self.context, self.hover)) if self.hover else None
        if not found:
            self.canvas.itemconfig(self.item, state="hidden")
            return
        photo, (x, y) = found
        ox, oy = self.origin
        self.canvas.coords(self.item, ox + x, oy + y)
        self.canvas.itemconfig(self.item, image=photo, state="normal")
        self.canvas.tag_raise(self.item)


def ms_to_next_second(origin=None):
    """Delay (ms) until the next whole second (of the wall clock, or since `origin` on
    the uptime clock), so 1-second clocks tick on the boundary instead of drifting."""
    frac = (time.time() if origin is None else uptime() - origin) % 1.0
    return max(1, int((1.0 - frac) * 1000) + 3)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class ScreenBalance:
    def __init__(self):
        try:
            self.config = clean_config(load_json(CONFIG_PATH, {}))
        except UnreadableFile as e:
            log_error(f"settings unreadable, using defaults for now: {e}")
            self.config = clean_config({})
        # if usage.json is locked at startup, keep tracking in memory and merge into the
        # file once it can be read again, instead of overwriting the history
        self._usage_pending_merge = False
        try:
            self.usage = clean_usage(load_json(USAGE_PATH, {}))
        except UnreadableFile as e:
            log_error(f"history unreadable for now, will merge later: {e}")
            self.usage = {}
            self._usage_pending_merge = True
        self._prune_usage()
        self._last_day = today_key()

        self.session_sec = 0.0            # continuous active time since last break
        self.break_count = 0              # bumps every time a real break is detected
        self.next_break_at = self.config["break_interval_min"] * 60
        self.dnd_until = self._load_dnd()  # Do not disturb: 0 = off, inf = until turned off
        self.limit_notices = {}           # date -> {"80": bool, "100": bool, "last": ts}
        self.current_app = ""
        self.is_idle = False

        self._last_tick = uptime()
        self._prev_idle = 0.0
        self._away_logged = False
        self._menu_sig = None
        self._dash_tab = "overview"
        self._dash_hover = None
        self._dash_hits = []
        self._last_save = uptime()
        self._popup = None
        self._break_win = None
        self._break_open = False
        self._stats_win = None
        self._settings_win = None
        self._tray_key = None
        self._theme_checked = 0.0
        self.apply_theme()

        self.cmd_q = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.report_callback_exception = self._report_tk_error
        # Windows shutdown / log off: save before the process is ended
        self.root.protocol("WM_SAVE_YOURSELF", self._save_safely)
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.renderer = RenderWorker(self.root)
        self.animator = Animator(self.root)

        self._dark_taskbar = taskbar_is_dark()
        self.icon = pystray.Icon(APP_NAME, design.render_tray(0, False, self._dark_taskbar), APP_NAME,
                                 self._build_menu())

    def _heal_windows(self):
        """If a window vanished behind our back (e.g. Alt+F4), forget it so nothing stays stuck."""
        for attr in ("_popup", "_break_win", "_stats_win", "_settings_win"):
            w = getattr(self, attr)
            try:
                alive = w is not None and bool(w.winfo_exists())
            except tk.TclError:
                alive = False
            if w is not None and not alive:
                setattr(self, attr, None)

    def _report_tk_error(self, exc, val, tb):
        # pythonw has no console: send errors from Tk callbacks to the log instead of nowhere
        log_error("UI callback failed:" + os.linesep + "".join(traceback.format_exception(exc, val, tb)))

    # ---------------- theme ----------------
    def _wants_dark(self) -> bool:
        mode = self.config.get("theme", "system")
        return windows_uses_dark_mode() if mode == "system" else mode == "dark"

    def apply_theme(self) -> bool:
        """Switch the palette if needed. Returns True when it changed."""
        dark = self._wants_dark()
        if THEME.get("dark") == dark:
            return False
        design.set_theme(dark)
        win = getattr(self, "_stats_win", None)
        if win is not None and win.winfo_exists():
            win.configure(bg=hexc(THEME["bg"]))
            self._stats_label.configure(bg=hexc(THEME["bg"]))
            style_titlebar(win)
            self._refresh_stats()
        return True

    # ---------------- data ----------------
    def _prune_usage(self):
        cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
        for k in [k for k in self.usage if k < cutoff]:
            del self.usage[k]

    def _day(self, key=None):
        day = self.usage.setdefault(key or today_key(), {})
        day.setdefault("total", 0.0)
        day.setdefault("apps", {})
        day.setdefault("hours", {})
        day.setdefault("breaks", 0)
        day.setdefault("break_sec", 0.0)
        day.setdefault("longest", 0.0)
        return day

    def _record_break(self, seconds):
        """Count a finished break (timer, stepping away, or the PC sleeping)."""
        if seconds <= 0:
            return
        day = self._day()
        day["breaks"] += 1
        day["break_sec"] += seconds

    def save(self):
        if self._usage_pending_merge:
            try:
                on_disk = clean_usage(load_json(USAGE_PATH, {}))
            except UnreadableFile:
                return  # still locked: try again at the next save, never overwrite blindly
            self.usage = merge_usage(on_disk, self.usage)
            self._usage_pending_merge = False
        save_json(USAGE_PATH, self.usage)

    def _save_safely(self):
        """Save without ever raising (a locked file must not stall the clock or block Quit)."""
        try:
            self.save()
            return True
        except Exception:
            log_error("saving failed (will retry):" + os.linesep + traceback.format_exc())
            return False

    def _limit_sec(self):
        return self.config["daily_limit_hours"] * 3600

    # ---------------- tray ----------------
    def _build_menu(self):
        post = lambda cmd: (lambda icon, item: self.cmd_q.put(cmd))
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Today: {fmt_dur(self._day()['total'])}", post("stats"), default=True),
            pystray.MenuItem(lambda item: self._break_in_text(precise=False), None, enabled=False),
            pystray.MenuItem(lambda item: f"Since last break: {fmt_dur(self.session_sec)}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", post("stats")),
            pystray.MenuItem(lambda item: f"Take a {self.config['break_length_min']:g}-min break",
                             post("break_now")),
            pystray.MenuItem("Take an open break (until I'm back)", post("open_break")),
            pystray.MenuItem(
                lambda item: f"Do not disturb ({self._dnd_label()})" if self._dnd_active() else "Do not disturb",
                pystray.Menu(
                    pystray.MenuItem("For 30 minutes", post("dnd_30")),
                    pystray.MenuItem("For 1 hour", post("dnd_60")),
                    pystray.MenuItem("For 2 hours", post("dnd_120")),
                    pystray.MenuItem("Until I turn it off", post("dnd_forever")),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Turn off", post("dnd_off"), enabled=lambda item: self._dnd_active()),
                ),
            ),
            pystray.MenuItem("Settings", post("settings")),
            pystray.MenuItem("Start with Windows", post("toggle_autostart"),
                             checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", post("quit")),
        )

    # ---------------- do not disturb ----------------
    @staticmethod
    def _state_path():
        return os.path.join(DATA_DIR, "state.json")

    def _load_dnd(self):
        """Do not disturb survives restarts until its end time."""
        try:
            raw = load_json(self._state_path(), {}).get("dnd_until", 0)
        except UnreadableFile:
            return 0.0
        if raw == "forever":
            return float("inf")
        try:
            until = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return until if until > time.time() else 0.0

    def _save_dnd(self):
        value = "forever" if self.dnd_until == float("inf") else self.dnd_until
        try:
            save_json(self._state_path(), {"dnd_until": value})
        except OSError:
            log_error("could not save Do not disturb state:\n" + traceback.format_exc())

    def _dnd_active(self):
        return time.time() < self.dnd_until

    def _dnd_label(self):
        """'until 15:30' or 'until turned off'."""
        if self.dnd_until == float("inf"):
            return "until turned off"
        end = dt.datetime.fromtimestamp(self.dnd_until)
        return f"until {end.hour}:{end.minute:02d}"

    def _set_dnd(self, minutes):
        """minutes: number, None = until turned off, 0 = off."""
        if minutes == 0:
            self.dnd_until = 0.0
        else:
            self.dnd_until = float("inf") if minutes is None else time.time() + minutes * 60
            # silence anything already on screen (the break reminder will come back after DND)
            if self._popup is not None:
                self._popup.destroy()
                self._popup = None
        self._save_dnd()
        self._tray_key = None
        self._update_tray()
        if self._stats_win is not None:
            self._refresh_stats()

    def _cmd_dnd_30(self):
        self._set_dnd(30)

    def _cmd_dnd_60(self):
        self._set_dnd(60)

    def _cmd_dnd_120(self):
        self._set_dnd(120)

    def _cmd_dnd_forever(self):
        self._set_dnd(None)

    def _cmd_dnd_off(self):
        self._set_dnd(0)

    def _break_in(self):
        """Seconds of active use left before the next break reminder (None during DND)."""
        if self._dnd_active():
            return None
        return self.next_break_at - self.session_sec

    def _break_in_text(self, precise=True):
        """precise: 'Break in: 12:34' (tooltip, dashboard). Otherwise minutes only, for the
        tray menu, which Windows has to rebuild every time its text changes."""
        if self._break_win is not None:
            return "On an open break" if self._break_open else "On a break"
        if self._dnd_active():
            return f"Do not disturb {self._dnd_label()}"
        left = self._break_in()
        if left <= 0:
            return "Break in: now"
        if precise:
            return f"Break in: {design.fmt_clock(left)}"
        return f"Break in: {math.ceil(left / 60)} min" if left > 60 else "Break in: under a minute"

    def _update_tray(self):
        total = self._day()["total"]
        limit = self._limit_sec()
        ratio = total / limit if limit else 0
        paused = self._dnd_active()
        key = (round(ratio, 2), paused, self._dark_taskbar)
        if key != self._tray_key:      # only re-render when it visibly changes
            self._tray_key = key
            self.icon.icon = design.render_tray(ratio, paused, self._dark_taskbar)
        status = "idle" if self.is_idle else "active"
        limit_txt = f" / {fmt_dur(limit)}" if limit else ""
        self.icon.title = (f"Screen Balance\n{self._break_in_text()}\n"
                           f"Today: {fmt_dur(total)}{limit_txt} ({status})")[:127]
        # rebuild the tray menu only when something in it changed: rebuilding every second
        # could close or glitch the menu while you have it open
        dnd = self._dnd_active()
        sig = (self._break_in_text(precise=False), fmt_dur(self.session_sec), fmt_dur(total),
               dnd, self._dnd_label() if dnd else "", self.config["break_length_min"])
        if sig != self._menu_sig:
            self._menu_sig = sig
            self.icon.update_menu()

    # ---------------- main loop ----------------
    def run(self):
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.root.after(50, self._poll_commands)
        self.root.after(ms_to_next_second(), self._tick)
        self.root.after(1500, self._startup_notice)
        self.root.mainloop()

    def _startup_notice(self, tries=0):
        """A Windows notification saying the app is running, since Windows 11 hides new
        tray icons behind the ^ arrow. Respects Do not disturb and the Settings switch."""
        if not self.config.get("startup_notice", True) or self._dnd_active():
            return
        if not self.icon.visible:
            if tries < 20:   # the tray icon can take a moment to appear right after login
                self.root.after(1000, self._startup_notice, tries + 1)
            return
        try:
            self.icon.notify(f"{self._break_in_text(precise=False)} · today {fmt_dur(self._day()['total'])}.\n"
                             "It lives in the tray: click ^ next to the clock if you don't see it.",
                             "Screen Balance is running")
            # take it back out of the notification centre after a while
            self.root.after(15000, self._clear_notice)
        except Exception:
            log_error("startup notification failed:" + os.linesep + traceback.format_exc())

    def _clear_notice(self):
        try:
            self.icon.remove_notification()
        except Exception:
            pass

    def _poll_commands(self):
        try:
            while True:
                cmd = self.cmd_q.get_nowait()
                try:
                    getattr(self, f"_cmd_{cmd}")()
                except Exception:
                    log_error(f"command {cmd!r} failed:\n{traceback.format_exc()}")
        except queue.Empty:
            pass
        self.root.after(50, self._poll_commands)  # tray clicks feel instant

    def _tick(self):
        # always reschedule, so one unexpected error can never stop tracking for good
        try:
            self._tick_inner()
        except Exception:
            log_error(f"tick failed:\n{traceback.format_exc()}")
        # land on the next whole second, so the countdowns never stutter or skip a number
        self.root.after(ms_to_next_second(), self._tick)

    def _tick_inner(self):
        now = uptime()
        gap = now - self._last_tick
        elapsed = gap if gap <= 30 else 0  # a big gap means sleep/hibernate, not screen time
        self._last_tick = now
        self._heal_windows()

        # follow Windows switching between light and dark (checked every few seconds)
        if now - self._theme_checked >= 3 or now < self._theme_checked:
            self._theme_checked = now
            self._dark_taskbar = taskbar_is_dark()   # the tray icon tile follows the taskbar
            if self.config.get("theme") == "system":
                self.apply_theme()

        if today_key() != self._last_day:   # midnight: drop old history, keep running
            self._last_day = today_key()
            self._prune_usage()

        idle = idle_seconds()
        break_len = self.config["break_length_min"] * 60
        # stop counting at the idle threshold, and never past the break length (otherwise the
        # same minutes would count as screen time AND as a break); a locked PC never counts
        self.is_idle = (idle >= min(self.config["idle_threshold_sec"], break_len)) or session_locked()

        # a long enough idle stretch (or the PC sleeping that long) counts as a break
        if (idle >= break_len or gap >= break_len) and self.session_sec > 0:
            self.session_sec = 0
            self.break_count += 1
            self.next_break_at = self.config["break_interval_min"] * 60

        # log breaks taken by stepping away (counted once you come back) or by sleeping the PC
        if self._break_win is None:
            if gap >= break_len:
                self._record_break(gap)
                # after waking, idle still includes the sleep until you type your PIN:
                # don't log that same away stretch a second time
                self._away_logged = True
            elif self._prev_idle >= break_len and idle < self._prev_idle:
                if not self._away_logged:
                    self._record_break(self._prev_idle)
                self._away_logged = False
            elif idle < break_len:
                self._away_logged = False
            self._prev_idle = idle
        else:
            self._prev_idle = 0.0

        if self._break_win is not None:
            pass  # on a break: nothing counts as screen time
        elif not self.is_idle and elapsed > 0:
            app = foreground_app()
            self.current_app = app
            day = self._day()
            day["total"] += elapsed
            day["apps"][app] = day["apps"].get(app, 0.0) + elapsed
            clock = dt.datetime.now()
            hour = str(clock.hour)
            day["hours"][hour] = day["hours"].get(hour, 0.0) + elapsed
            stamp = f"{clock.hour}:{clock.minute:02d}"
            day.setdefault("first", stamp)
            day["last"] = stamp
            self.session_sec += elapsed
            day["longest"] = max(day["longest"], self.session_sec)
            self._check_break()
            self._check_limit()

        if now - self._last_save >= SAVE_EVERY_SEC:
            self._save_safely()
            self._last_save = now

        self._update_tray()
        # redraw the dashboard only while it's actually on screen (it's the costly part)
        if self._stats_win is not None and self._stats_win.winfo_exists() \
                and self._stats_win.state() == "normal":
            self._refresh_stats()

    # ---------------- rules ----------------
    def _check_break(self):
        if self._dnd_active() or self._popup is not None:
            return
        if self.session_sec >= self.next_break_at:
            self._show_popup(
                "break", "Time for a break",
                f"You've been at the screen for {fmt_dur(self.session_sec)} straight. "
                f"Stand up, stretch, and look at something far away.",
            )

    def _check_limit(self):
        if self._dnd_active() or self._popup is not None:
            return
        total = self._day()["total"]
        limit = self._limit_sec()
        if limit <= 0:
            return
        n = self.limit_notices.setdefault(today_key(), {"80": False, "100": False, "last": 0})
        if total >= limit:
            repeat = self.config["over_limit_repeat_min"] * 60
            if not n["100"] or time.time() - n["last"] >= repeat:
                n["100"] = True
                n["last"] = time.time()
                over = total - limit
                extra = f" — {fmt_dur(over)} over" if over >= 60 else ""
                self._show_popup(
                    "limit", "Daily limit reached",
                    f"You've used your PC for {fmt_dur(total)} today{extra}. "
                    f"Your limit is {fmt_dur(limit)}. Maybe call it a day?",
                )
        elif total >= 0.8 * limit and not n["80"]:
            n["80"] = True
            self._show_popup(
                "warn", "Heads up",
                f"You've used {fmt_dur(total)} of your {fmt_dur(limit)} daily limit. "
                f"About {fmt_dur(limit - total)} left.",
            )

    # ---------------- popup ----------------
    def _show_popup(self, kind, title, message):
        snooze = self.config["snooze_min"]
        if kind == "break":
            buttons = [("snooze", f"Snooze {snooze} min", False), ("ok", "Taking a break", True)]
        else:
            buttons = [("ok", "Got it", True)]

        win = tk.Toplevel(self.root)
        self._popup = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        key_rgb = design.popup_key()          # fixed for this popup, even if the theme flips
        key = hexc(key_rgb)
        win.configure(bg=key)
        win.attributes("-transparentcolor", key)
        win.attributes("-alpha", 0.0)
        canvas = tk.Canvas(win, bg=key, highlightthickness=0, bd=0)
        canvas.pack()
        state = {"hover": None, "hits": [], "photo": None}

        item = canvas.create_image(0, 0, anchor="nw")
        # The popup never changes except on hover, so draw every hover look up front
        # (a few ms each): hovering then swaps images on the very next display frame.
        base_img, state["hits"] = design.render_popup(kind, title, message, buttons, None, self.scale, key_rgb)
        looks = {None: ImageTk.PhotoImage(base_img)}
        for bid, _ in state["hits"]:
            img, _ = design.render_popup(kind, title, message, buttons, bid, self.scale, key_rgb)
            looks[bid] = ImageTk.PhotoImage(img)
        state["looks"] = looks
        canvas.config(width=base_img.width, height=base_img.height)

        def draw(sync=False):
            canvas.itemconfig(item, image=state["looks"].get(state["hover"], state["looks"][None]))

        def hit(x, y):
            for bid, (x0, y0, x1, y1) in state["hits"]:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return bid
            return None

        def on_move(e):
            h = hit(e.x, e.y)
            if h != state["hover"]:
                state["hover"] = h
                canvas.config(cursor="hand2" if h else "")
                draw()

        def on_leave(_):
            if state["hover"]:
                state["hover"] = None
                draw()

        breaks_at_open = self.break_count

        def close(action="close"):
            if self._popup is not win:
                return
            self._popup = None
            win.destroy()
            if kind == "break" and action == "ok":
                self._start_break()
            elif kind == "break" and self.break_count == breaks_at_open:
                # snoozed / dismissed and no real break happened yet: remind again after the snooze.
                # (if they already stepped away, the normal full interval applies)
                self.next_break_at = self.session_sec + snooze * 60

        def on_click(e):
            action = hit(e.x, e.y)
            if action:
                close(action)

        canvas.bind("<Motion>", on_move)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", on_click)
        win.protocol("WM_DELETE_WINDOW", close)          # Alt+F4 acts like the close button
        # an ignored popup must not block every later reminder: it quietly snoozes itself
        win.after(POPUP_TIMEOUT_SEC * 1000, close)

        draw(sync=True)
        win.update_idletasks()
        w, h = canvas.winfo_reqwidth(), canvas.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = sw - w - round(20 * self.scale)
        y_end = sh - h - round(64 * self.scale)
        slide = round(24 * self.scale)

        def slide_in(t):
            # vsync-locked, time-based: one step per display refresh (240 per second on 240 Hz)
            if self._popup is not win or not win.winfo_exists():
                return False
            ease = 1 - (1 - t) ** 3
            win.geometry(f"+{x}+{round(y_end + slide * (1 - ease))}")
            win.attributes("-alpha", ease)

        win.geometry(f"+{x}+{y_end + slide}")
        self.animator.play(0.24, slide_in)
        win.bell()

    # ---------------- full-screen break timer ----------------
    def _start_break(self, open_ended=False):
        """Cover every screen, on top of everything, until the break is over.

        Timed break: counts down from the break length, then waits for "Back to work".
        Open break (open_ended=True): counts up until you click "I'm back"."""
        if self._break_win is not None:
            return
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None

        total = self.config["break_length_min"] * 60
        started = uptime()             # keeps counting while asleep, ignores clock changes
        end_at = started + total
        tip_offset = random.randrange(len(design.BREAK_TIPS))

        # cover the whole virtual desktop (all monitors), centre the timer on the main one
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if vw <= 0 or vh <= 0:
            vx, vy, vw, vh = 0, 0, sw, sh

        bg = hexc(THEME["bg"])
        win = tk.Toplevel(self.root)
        self._break_win = win
        self._break_open = open_ended
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.configure(bg=bg, cursor="arrow")
        win.maxsize(vw, vh)                     # allow spanning every monitor
        win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        canvas = tk.Canvas(win, width=vw, height=vh, bg=bg, highlightthickness=0, bd=0)
        canvas.pack()
        px, py = -vx + sw // 2, -vy + sh // 2   # main monitor centre, in canvas coords
        item = canvas.create_image(px, py, anchor="center")
        hover = HoverLayer(self, canvas, "break")
        state = {"photo": None, "done": False}
        breaks_next = self._day()["breaks"] + 1   # "your 3rd break today"

        def page_args():
            now = uptime()
            elapsed = max(0.0, now - started)
            tip = design.BREAK_TIPS[(tip_offset + int(elapsed // 20)) % len(design.BREAK_TIPS)]
            if open_ended:
                return (lambda h, a=(elapsed, total, tip): design.render_open_break(*a, h, self.scale))
            remaining = max(0.0, end_at - now)
            if state["done"]:
                tip = "Your eyes, back and mind will thank you."
            done = state["done"]
            return (lambda h, a=(remaining, total, tip, done):
                    design.render_break(*a, h, self.scale, breaks_today=breaks_next))

        def apply(result, render):
            if self._break_win is not win:
                return
            img, hits = result
            # follow a light/dark switch that happens mid-break
            page = hexc(THEME["bg"])
            if canvas.cget("bg") != page:
                win.configure(bg=page)
                canvas.configure(bg=page)
            state["photo"] = ImageTk.PhotoImage(img)
            canvas.itemconfig(item, image=state["photo"])
            origin = (px - img.width // 2, py - img.height // 2)
            context = (open_ended, state["done"], THEME["dark"], self.scale)
            hover.update(hits, origin, context, render)

        def draw(sync=False):
            render = page_args()          # the page is always drawn without hover
            if sync:
                apply(render(None), render)
            else:   # drawn in the background so the countdown never stutters
                self.renderer.submit("break", render, (None,), lambda res: apply(res, render))

        def hit(x, y):
            return hover.hit(x, y)

        def on_move(e):
            h = hit(e.x, e.y)
            if h != hover.hover:
                canvas.config(cursor="hand2" if h else "arrow")
                hover.set_hover(h)          # instant: pre-drawn, shown on the next frame

        def finish(action):
            if self._break_win is not win:
                return
            self._break_win = None
            took = max(0.0, uptime() - started)
            if action == "skip":
                # ended a timed break early: acts like a snooze, not a break
                self.next_break_at = self.session_sec + self.config["snooze_min"] * 60
            elif action == "back" and took < MIN_OPEN_BREAK_SEC:
                pass  # opened by mistake: nothing to record
            else:
                self._record_break(took)
                if action == "finish" or took >= total:
                    # a full break: start a fresh stretch
                    self.session_sec = 0
                    self.break_count += 1
                    self.next_break_at = self.config["break_interval_min"] * 60
            self._prev_idle = 0.0
            self.renderer.cancel("break")
            win.destroy()
            self._update_tray()
            if self._stats_win is not None:
                self._refresh_stats()

        def on_click(e):
            action = hit(e.x, e.y)
            if action in ("skip", "finish", "back"):
                finish(action)

        def loop():
            if self._break_win is not win:
                return
            if not open_ended and not state["done"] and uptime() >= end_at:
                state["done"] = True
                win.bell()
            draw()
            # stay above anything that tried to come to the front
            win.attributes("-topmost", True)
            win.lift()
            # next update exactly when the clock's second changes (no drift, no skipped numbers)
            win.after(ms_to_next_second(started), loop)

        def fade(t):
            # vsync-locked, time-based fade (one step per display refresh)
            if self._break_win is not win:
                return False
            win.attributes("-alpha", 1 - (1 - t) ** 3)

        canvas.bind("<Motion>", on_move)
        canvas.bind("<Button-1>", on_click)
        # Alt+F4 ends the break the same way the on-screen button would
        win.protocol("WM_DELETE_WINDOW",
                     lambda: finish("back" if open_ended else ("finish" if state["done"] else "skip")))
        draw(sync=True)
        win.focus_force()
        self.animator.play(0.28, fade)
        win.after(ms_to_next_second(started), loop)

    def _cmd_break_now(self):
        self._start_break()

    def _cmd_open_break(self):
        self._start_break(open_ended=True)

    # ---------------- dashboard ----------------
    def _dash_data(self):
        day = self._day()
        today = dt.date.today()
        week = []
        for i in range(6, -1, -1):
            d = today - dt.timedelta(days=i)
            rec = self.usage.get(d.isoformat(), {})
            week.append({
                "label": d.strftime("%a"),
                "total": rec.get("total", 0.0),
                "breaks": rec.get("breaks", 0),
                "break_sec": rec.get("break_sec", 0.0),
                "longest": rec.get("longest", 0.0),
                "today": i == 0,
            })
        if self._dnd_active():
            state = "dnd"
        elif self.is_idle:
            state = "idle"
        else:
            state = "active"
        return {
            "total": day["total"],
            "limit": self._limit_sec(),
            "session": self.session_sec,
            "break_in": self._break_in(),
            "apps": sorted(day["apps"].items(), key=lambda kv: kv[1], reverse=True),
            "week": week,
            "date_str": today.strftime("%A, %B %d").replace(" 0", " "),
            "state": state,
            "dnd_label": self._dnd_label() if state == "dnd" else "",
            "current_app": self.current_app or "—",
            "break_len_min": self.config["break_length_min"],
            "breaks": day["breaks"],
            "break_sec": day["break_sec"],
            "longest": day["longest"],
            "first": day.get("first"),
            "hours": [day["hours"].get(str(h), 0.0) for h in range(24)],
            "hour_now": dt.datetime.now().hour,
        }

    def _cmd_stats(self):
        if self._stats_win is not None:
            self._stats_win.deiconify()
            self._stats_win.lift()
            self._stats_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        self._stats_win = win
        win.title("Screen Balance")
        win.configure(bg=hexc(THEME["bg"]))
        win.resizable(False, False)
        # a canvas: the page image, plus a hover layer on top for instant button feedback
        self._stats_label = tk.Canvas(win, bg=hexc(THEME["bg"]), bd=0, highlightthickness=0)
        self._stats_label.pack()
        self._dash_item = self._stats_label.create_image(0, 0, anchor="nw")
        self._dash_layer = HoverLayer(self, self._stats_label, "dash")

        def dash_hit(x, y):
            return self._dash_layer.hit(x, y)

        def on_move(e):
            h = dash_hit(e.x, e.y)
            if h != self._dash_hover:
                self._dash_hover = h
                self._stats_label.config(cursor="hand2" if h else "")
                self._dash_layer.set_hover(h)     # instant, no redraw needed

        def on_leave(_):
            if self._dash_hover:
                self._dash_hover = None
                self._dash_layer.set_hover(None)

        def on_click(e):
            h = dash_hit(e.x, e.y)
            if h and h.startswith("tab_"):
                self._dash_tab = h[4:]
                self._refresh_stats()
            elif h in ("break_now", "open_break"):
                self._dash_hover = None
                self._dash_layer.set_hover(None)
                self._start_break(open_ended=(h == "open_break"))
                self._refresh_stats()
            elif h == "dnd":
                # dashboard button: 1 hour of quiet, or switch it off if it's on
                self._set_dnd(0 if self._dnd_active() else 60)

        self._stats_label.bind("<Motion>", on_move)
        self._stats_label.bind("<Leave>", on_leave)
        self._stats_label.bind("<Button-1>", on_click)

        def on_close():
            self._stats_win = None
            self.renderer.cancel("dash")
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)
        self._refresh_stats(sync=True)
        style_titlebar(win)
        win.lift()
        win.focus_force()

    def _refresh_stats(self, sync=False):
        win = self._stats_win
        if win is None or not win.winfo_exists():
            self._stats_win = None
            return
        data, tab, scale = self._dash_data(), self._dash_tab, self.scale
        # the page is drawn without hover; hovered buttons come from the hover layer
        render = lambda hover, d=data, t=tab: design.render_dashboard(d, scale, hover, t)
        context = (tab, data["state"] == "dnd", data["break_len_min"], THEME["dark"], scale)
        if sync:   # first frame when the window opens, so it appears at full size at once
            self._apply_stats(render(None), render, context)
        else:      # every later frame (ticks, tabs) is drawn in the background
            self.renderer.submit("dash", render, (None,),
                                 lambda res: self._apply_stats(res, render, context))

    def _apply_stats(self, result, render, context):
        win = self._stats_win
        if win is None or not win.winfo_exists():
            return
        img, self._dash_hits = result
        self._stats_photo = ImageTk.PhotoImage(img)
        c = self._stats_label
        if int(c.cget("width")) != img.width or int(c.cget("height")) != img.height:
            c.config(width=img.width, height=img.height)
        c.itemconfig(self._dash_item, image=self._stats_photo)
        self._dash_layer.update(self._dash_hits, (0, 0), context, render)

    # ---------------- settings ----------------
    def _cmd_settings(self):
        if self._settings_win is not None:
            self._settings_win.lift()
            self._settings_win.focus_force()
            return
        T = THEME
        bg = hexc(T["bg"])
        card = bg  # Notion style: rows sit directly on the white page, split by thin lines
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.title("Screen Balance — Settings")
        win.configure(bg=bg)
        win.resizable(False, False)

        outer = tk.Frame(win, bg=bg, padx=28, pady=22)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Settings", bg=bg, fg=hexc(T["text"]),
                 font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(outer, text="Tune how Screen Balance keeps you in check.", bg=bg, fg=hexc(T["muted"]),
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(0, 14))

        # key, title, hint, lo, hi, step, unit
        fields = [
            ("break_interval_min", "Break reminder", "Remind me after this much continuous use", 5, 240, 5, "min"),
            ("break_length_min", "Break length", "Being away this long resets the timer", 1, 60, 1, "min"),
            ("snooze_min", "Snooze", "How long “Snooze” waits", 1, 60, 1, "min"),
            ("daily_limit_hours", "Daily limit", "Total screen time per day (0 = off)", 0, 24, 0.5, "h"),
            ("idle_threshold_sec", "Idle after", "Stop counting with no mouse/keyboard input", 30, 1800, 30, "sec"),
            ("over_limit_repeat_min", "Over-limit nudge", "Repeat the limit warning every", 5, 240, 5, "min"),
        ]
        values = {k: float(self.config[k]) for k, *_ in fields}

        def fmt(key, v, unit):
            return f"{v:g} {unit}"

        card_frame = tk.Frame(outer, bg=card)
        card_frame.pack(fill="x")
        tk.Frame(card_frame, bg=hexc(T["border"]), height=1).pack(fill="x")
        for i, (key, title, hint, lo, hi, step, unit) in enumerate(fields):
            row = tk.Frame(card_frame, bg=card, padx=2, pady=7)
            row.pack(fill="x")
            tk.Frame(card_frame, bg=hexc(T["border"]), height=1).pack(fill="x")
            text = tk.Frame(row, bg=card)
            text.pack(side="left", fill="x", expand=True)
            tk.Label(text, text=title, bg=card, fg=hexc(T["text"]), font=("Segoe UI", 10)).pack(anchor="w")
            tk.Label(text, text=hint, bg=card, fg=hexc(T["muted"]), font=("Segoe UI", 9)).pack(anchor="w")

            stepper = tk.Frame(row, bg=bg, highlightthickness=1, highlightbackground=hexc(T["border_hi"]))
            stepper.pack(side="right", padx=(16, 0))
            val_lbl = tk.Label(stepper, text=fmt(key, values[key], unit), width=8, bg=bg,
                               fg=hexc(T["text"]), font=("Segoe UI", 10))

            def make_step(key=key, lo=lo, hi=hi, step=step, unit=unit, lbl=val_lbl, sign=1):
                def go():
                    values[key] = round(max(lo, min(hi, values[key] + sign * step)), 2)
                    lbl.config(text=fmt(key, values[key], unit))
                return go

            for sign, sym in ((-1, "−"), (1, "+")):
                b = tk.Label(stepper, text=sym, bg=bg, fg=hexc(T["muted"]), cursor="hand2",
                             font=("Segoe UI", 12), width=2)
                b.bind("<Button-1>", lambda e, f=make_step(sign=sign): f())
                b.bind("<Enter>", lambda e, b=b: b.config(bg=hexc(T["card_hi"]), fg=hexc(T["text"])))
                b.bind("<Leave>", lambda e, b=b: b.config(bg=bg, fg=hexc(T["muted"])))
                if sign < 0:
                    b.pack(side="left")
                    val_lbl.pack(side="left")
                else:
                    b.pack(side="left")

        # appearance: System / Light / Dark
        theme = {"mode": self.config.get("theme", "system")}
        look_row = tk.Frame(outer, bg=bg, pady=9)
        look_row.pack(fill="x")
        look_text = tk.Frame(look_row, bg=bg)
        look_text.pack(side="left")
        tk.Label(look_text, text="Appearance", bg=bg, fg=hexc(T["text"]), font=("Segoe UI", 10)).pack(anchor="w")
        tk.Label(look_text, text="System follows your Windows light/dark setting", bg=bg, fg=hexc(T["muted"]),
                 font=("Segoe UI", 9)).pack(anchor="w")
        seg = tk.Frame(look_row, bg=bg, highlightthickness=1, highlightbackground=hexc(T["border_hi"]))
        seg.pack(side="right")
        seg_btns = {}

        def draw_seg():
            for mode, lbl in seg_btns.items():
                on = mode == theme["mode"]
                lbl.config(bg=hexc(T["card_hi"]) if on else bg, fg=hexc(T["text"] if on else T["muted"]),
                           font=("Segoe UI Semibold" if on else "Segoe UI", 9))

        for mode in THEMES:
            lbl = tk.Label(seg, text=mode.capitalize(), bg=bg, cursor="hand2", padx=10, pady=3)
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, m=mode: (theme.update(mode=m), draw_seg()))
            seg_btns[mode] = lbl
        draw_seg()
        tk.Frame(outer, bg=hexc(T["border"]), height=1).pack(fill="x")

        # autostart toggle
        auto_row = tk.Frame(outer, bg=bg, pady=10)
        auto_row.pack(fill="x")
        auto = {"on": autostart_enabled()}
        tk.Label(auto_row, text="Start with Windows", bg=bg, fg=hexc(T["text"]),
                 font=("Segoe UI", 10)).pack(side="left")
        toggle = tk.Canvas(auto_row, width=34, height=20, bg=bg, highlightthickness=0, cursor="hand2")
        toggle.pack(side="right")

        def draw_toggle():
            toggle.delete("all")
            on = auto["on"]
            col = hexc(T["a1"]) if on else hexc(T["border_hi"])
            toggle.create_oval(0, 0, 20, 20, fill=col, outline=col)
            toggle.create_oval(14, 0, 34, 20, fill=col, outline=col)
            toggle.create_rectangle(10, 0, 24, 20, fill=col, outline=col)
            x = 16 if on else 2
            toggle.create_oval(x, 2, x + 16, 18, fill="white", outline="white")
        toggle.bind("<Button-1>", lambda e: (auto.update(on=not auto["on"]), draw_toggle()))
        draw_toggle()

        # startup notification toggle
        tk.Frame(outer, bg=hexc(T["border"]), height=1).pack(fill="x")
        notice_row = tk.Frame(outer, bg=bg, pady=9)
        notice_row.pack(fill="x")
        notice = {"on": bool(self.config.get("startup_notice", True))}
        notice_text = tk.Frame(notice_row, bg=bg)
        notice_text.pack(side="left")
        tk.Label(notice_text, text="Notify me when it starts", bg=bg, fg=hexc(T["text"]),
                 font=("Segoe UI", 10)).pack(anchor="w")
        tk.Label(notice_text, text="Shows that Screen Balance is running, even if its icon is hidden",
                 bg=bg, fg=hexc(T["muted"]), font=("Segoe UI", 9)).pack(anchor="w")
        notice_toggle = tk.Canvas(notice_row, width=34, height=20, bg=bg, highlightthickness=0, cursor="hand2")
        notice_toggle.pack(side="right")

        def draw_notice_toggle():
            c = notice_toggle
            c.delete("all")
            on = notice["on"]
            col = hexc(T["a1"]) if on else hexc(T["border_hi"])
            c.create_oval(0, 0, 20, 20, fill=col, outline=col)
            c.create_oval(14, 0, 34, 20, fill=col, outline=col)
            c.create_rectangle(10, 0, 24, 20, fill=col, outline=col)
            x = 16 if on else 2
            c.create_oval(x, 2, x + 16, 18, fill="white", outline="white")
        notice_toggle.bind("<Button-1>", lambda e: (notice.update(on=not notice["on"]), draw_notice_toggle()))
        draw_notice_toggle()

        def close():
            self._settings_win = None
            win.destroy()

        def save():
            old_interval = self.config["break_interval_min"]
            for key, *_ in fields:
                v = values[key]
                self.config[key] = v if key == "daily_limit_hours" else int(v)
            self.config["theme"] = theme["mode"]
            self.config["startup_notice"] = notice["on"]
            try:
                save_json(CONFIG_PATH, self.config)
            except OSError:
                log_error("could not save settings:" + os.linesep + traceback.format_exc())
            self.apply_theme()
            if self.config["break_interval_min"] != old_interval:
                # new interval applies to the current session; if already past it, remind in a minute
                self.next_break_at = max(self.config["break_interval_min"] * 60, self.session_sec + 60)
            if auto["on"] != autostart_enabled():
                try:
                    set_autostart(auto["on"])
                except OSError:
                    log_error("could not change Start with Windows:" + os.linesep + traceback.format_exc())
            self._menu_sig = None
            self.limit_notices.pop(today_key(), None)
            self._tray_key = None
            self._update_tray()
            if self._stats_win is not None:
                self._refresh_stats()
            close()

        btns = tk.Frame(outer, bg=bg)
        btns.pack(fill="x", pady=(6, 0))
        flat_button(btns, "Save", save, primary=True).pack(side="right")
        flat_button(btns, "Cancel", close).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", close)
        style_titlebar(win)
        win.lift()
        win.focus_force()

    # ---------------- other commands ----------------
    def _cmd_toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self._menu_sig = None
        self.icon.update_menu()

    def _cmd_quit(self):
        self._save_safely()
        try:
            self.icon.stop()
        finally:
            self.root.quit()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    if not single_instance():
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    if autostart_enabled():
        set_autostart(True)  # keep the startup entry pointing at wherever the app lives now
    app = ScreenBalance()
    try:
        app.run()
    finally:
        app._save_safely()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # pythonw has no console: make sure a startup crash leaves a trace
        log_error("Screen Balance crashed:" + os.linesep + traceback.format_exc())
        raise
