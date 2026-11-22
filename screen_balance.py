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
}
THEMES = ("system", "light", "dark")

TICK_MS = 1000  # 1 s so the "Break in" countdown ticks smoothly
SAVE_EVERY_SEC = 60
KEEP_DAYS = 60

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.GetTickCount.restype = wt.DWORD
user32.GetForegroundWindow.restype = wt.HWND
user32.GetParent.restype = wt.HWND
user32.GetParent.argtypes = [wt.HWND]


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


def foreground_app() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "Desktop"
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return "Screen Balance"
    try:
        name = psutil.Process(pid.value).name()
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


def windows_uses_dark_mode() -> bool:
    """True when Windows' app mode is set to Dark (Settings > Personalization > Colors)."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


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
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        raise ValueError("not a JSON object")
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        # keep the damaged file instead of silently overwriting the user's history
        backup = f"{path}.corrupt-{dt.datetime.now():%Y%m%d-%H%M%S}"
        try:
            os.replace(path, backup)
        except OSError:
            pass
        log_error(f"Could not read {path} ({e}); moved it to {backup}")
        return default


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
# App
# --------------------------------------------------------------------------- #
class ScreenBalance:
    def __init__(self):
        self.config = clean_config(load_json(CONFIG_PATH, {}))
        self.usage = clean_usage(load_json(USAGE_PATH, {}))
        self._prune_usage()
        self._last_day = today_key()

        self.session_sec = 0.0            # continuous active time since last break
        self.break_count = 0              # bumps every time a real break is detected
        self.next_break_at = self.config["break_interval_min"] * 60
        self.reminders_paused_until = 0.0
        self.limit_notices = {}           # date -> {"80": bool, "100": bool, "last": ts}
        self.current_app = ""
        self.is_idle = False

        self._last_tick = time.monotonic()
        self._last_wall = time.time()
        self._prev_idle = 0.0
        self._dash_tab = "overview"
        self._dash_hover = None
        self._dash_hits = []
        self._last_save = time.monotonic()
        self._popup = None
        self._break_win = None
        self._stats_win = None
        self._settings_win = None
        self._tray_key = None
        self._theme_checked = 0.0
        self.apply_theme()

        self.cmd_q = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.scale = self.root.winfo_fpixels("1i") / 96.0

        self.icon = pystray.Icon(APP_NAME, design.render_tray(0), APP_NAME, self._build_menu())

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
        save_json(USAGE_PATH, self.usage)

    def _limit_sec(self):
        return self.config["daily_limit_hours"] * 3600

    # ---------------- tray ----------------
    def _build_menu(self):
        post = lambda cmd: (lambda icon, item: self.cmd_q.put(cmd))
        return pystray.Menu(
            pystray.MenuItem(lambda item: f"Today: {fmt_dur(self._day()['total'])}", post("stats"), default=True),
            pystray.MenuItem(lambda item: self._break_in_text(), None, enabled=False),
            pystray.MenuItem(lambda item: f"Since last break: {fmt_dur(self.session_sec)}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", post("stats")),
            pystray.MenuItem("Take a break now", post("break_now")),
            pystray.MenuItem("Settings", post("settings")),
            pystray.MenuItem(
                lambda item: "Resume reminders" if self._reminders_paused() else "Pause reminders for 1 hour",
                post("toggle_pause"),
            ),
            pystray.MenuItem("Start with Windows", post("toggle_autostart"),
                             checked=lambda item: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", post("quit")),
        )

    def _reminders_paused(self):
        return time.time() < self.reminders_paused_until

    def _break_in(self):
        """Seconds of active use left before the next break reminder (None while paused)."""
        if self._reminders_paused():
            return None
        return self.next_break_at - self.session_sec

    def _break_in_text(self):
        if self._break_win is not None:
            return "On a break"
        left = self._break_in()
        if left is None:
            return "Break in: paused"
        return f"Break in: {design.fmt_clock(left)}" if left > 0 else "Break in: now"

    def _update_tray(self):
        total = self._day()["total"]
        limit = self._limit_sec()
        ratio = total / limit if limit else 0
        paused = self._reminders_paused()
        key = (round(ratio, 2), paused)
        if key != self._tray_key:      # only re-render when it visibly changes
            self._tray_key = key
            self.icon.icon = design.render_tray(ratio, paused)
        status = "idle" if self.is_idle else "active"
        limit_txt = f" / {fmt_dur(limit)}" if limit else ""
        self.icon.title = (f"Screen Balance\n{self._break_in_text()}\n"
                           f"Today: {fmt_dur(total)}{limit_txt} ({status})")[:127]
        self.icon.update_menu()

    # ---------------- main loop ----------------
    def run(self):
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.root.after(200, self._poll_commands)
        self.root.after(1000, self._tick)
        self.root.mainloop()

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
        self.root.after(200, self._poll_commands)

    def _tick(self):
        # always reschedule, so one unexpected error can never stop tracking for good
        try:
            self._tick_inner()
        except Exception:
            log_error(f"tick failed:\n{traceback.format_exc()}")
        self.root.after(TICK_MS, self._tick)

    def _tick_inner(self):
        now = time.monotonic()
        wall = time.time()
        gap = max(now - self._last_tick, wall - self._last_wall)
        elapsed = gap if gap <= 30 else 0  # a big gap means sleep/hibernate, not screen time
        self._last_tick, self._last_wall = now, wall

        # follow Windows switching between light and dark (checked every few seconds)
        if self.config.get("theme") == "system" and now - self._theme_checked >= 3:
            self._theme_checked = now
            self.apply_theme()

        if today_key() != self._last_day:   # midnight: drop old history, keep running
            self._last_day = today_key()
            self._prune_usage()

        idle = idle_seconds()
        self.is_idle = idle >= self.config["idle_threshold_sec"]

        # a long enough idle stretch (or the PC sleeping that long) counts as a break
        break_len = self.config["break_length_min"] * 60
        if (idle >= break_len or gap >= break_len) and self.session_sec > 0:
            self.session_sec = 0
            self.break_count += 1
            self.next_break_at = self.config["break_interval_min"] * 60

        # log breaks taken by stepping away (counted once you come back) or by sleeping the PC
        if self._break_win is None:
            if gap >= break_len:
                self._record_break(gap)
            elif self._prev_idle >= break_len and idle < self._prev_idle:
                self._record_break(self._prev_idle)
            self._prev_idle = idle
        else:
            self._prev_idle = 0.0

        if self._break_win is not None:
            pass  # on a break: nothing counts as screen time
        elif not self.is_idle:
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
            self.save()
            self._last_save = now

        self._update_tray()
        # redraw the dashboard only while it's actually on screen (it's the costly part)
        if self._stats_win is not None and self._stats_win.winfo_exists() \
                and self._stats_win.state() == "normal":
            self._refresh_stats()

    # ---------------- rules ----------------
    def _check_break(self):
        if self._reminders_paused() or self._popup is not None:
            return
        if self.session_sec >= self.next_break_at:
            self._show_popup(
                "break", "Time for a break",
                f"You've been at the screen for {fmt_dur(self.session_sec)} straight. "
                f"Stand up, stretch, and look at something far away.",
            )

    def _check_limit(self):
        if self._reminders_paused() or self._popup is not None:
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
        key = hexc(design.popup_key())
        win.configure(bg=key)
        win.attributes("-transparentcolor", key)
        win.attributes("-alpha", 0.0)
        canvas = tk.Canvas(win, bg=key, highlightthickness=0, bd=0)
        canvas.pack()
        state = {"hover": None, "hits": [], "photo": None}

        def draw():
            img, hits = design.render_popup(kind, title, message, buttons, state["hover"], self.scale)
            state["photo"] = ImageTk.PhotoImage(img)
            state["hits"] = hits
            canvas.config(width=img.width, height=img.height)
            canvas.delete("all")
            canvas.create_image(0, 0, image=state["photo"], anchor="nw")

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

        draw()
        win.update_idletasks()
        w, h = canvas.winfo_reqwidth(), canvas.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = sw - w - round(20 * self.scale)
        y_end = sh - h - round(64 * self.scale)
        slide = round(24 * self.scale)

        def animate(step=0, frames=14):
            t = step / frames
            ease = 1 - (1 - t) ** 3
            if not win.winfo_exists():
                return
            win.geometry(f"+{x}+{round(y_end + slide * (1 - ease))}")
            win.attributes("-alpha", 0.98 * ease)
            if step < frames:
                win.after(16, animate, step + 1, frames)

        win.geometry(f"+{x}+{y_end + slide}")
        animate()
        win.bell()

    # ---------------- full-screen break timer ----------------
    def _start_break(self):
        """Dim every screen and show a countdown on top of everything until the break is over."""
        if self._break_win is not None:
            return
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None

        total = self.config["break_length_min"] * 60
        started = time.monotonic()
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
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.0)
        win.configure(bg=bg, cursor="arrow")
        win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        canvas = tk.Canvas(win, width=vw, height=vh, bg=bg, highlightthickness=0, bd=0)
        canvas.pack()
        px, py = -vx + sw // 2, -vy + sh // 2   # main monitor centre, in canvas coords
        item = canvas.create_image(px, py, anchor="center")
        state = {"hover": None, "hits": [], "photo": None, "done": False, "origin": (0, 0)}

        def draw():
            remaining = max(0.0, end_at - time.monotonic())
            if state["done"]:
                tip = "Your eyes, back and mind will thank you."
            else:
                elapsed = total - remaining
                tip = design.BREAK_TIPS[(tip_offset + int(elapsed // 20)) % len(design.BREAK_TIPS)]
            img, hits = design.render_break(remaining, total, tip, state["done"], state["hover"], self.scale,
                                            breaks_today=self._day()["breaks"] + 1)
            state["photo"] = ImageTk.PhotoImage(img)
            canvas.itemconfig(item, image=state["photo"])
            ox, oy = px - img.width // 2, py - img.height // 2
            state["hits"] = [(bid, (x0 + ox, y0 + oy, x1 + ox, y1 + oy)) for bid, (x0, y0, x1, y1) in hits]

        def hit(x, y):
            for bid, (x0, y0, x1, y1) in state["hits"]:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return bid
            return None

        def on_move(e):
            h = hit(e.x, e.y)
            if h != state["hover"]:
                state["hover"] = h
                canvas.config(cursor="hand2" if h else "arrow")
                draw()

        def finish(completed):
            if self._break_win is not win:
                return
            self._break_win = None
            if completed:
                self._record_break(time.monotonic() - started)
                self.session_sec = 0
                self.break_count += 1
                self.next_break_at = self.config["break_interval_min"] * 60
            else:
                self.next_break_at = self.session_sec + self.config["snooze_min"] * 60
            win.destroy()
            self._update_tray()

        def on_click(e):
            action = hit(e.x, e.y)
            if action == "skip":
                finish(False)
            elif action == "finish":
                finish(True)

        def loop():
            if self._break_win is not win:
                return
            if not state["done"] and time.monotonic() >= end_at:
                state["done"] = True
                win.bell()
            draw()
            # stay above anything that tried to come to the front
            win.attributes("-topmost", True)
            win.lift()
            win.after(1000, loop)

        def fade(step=0, frames=16):
            if self._break_win is not win:
                return
            win.attributes("-alpha", 1.0 if step >= frames else (1 - (1 - step / frames) ** 3))
            if step < frames:
                win.after(16, fade, step + 1, frames)

        canvas.bind("<Motion>", on_move)
        canvas.bind("<Button-1>", on_click)
        draw()
        win.focus_force()
        fade()
        win.after(1000, loop)

    def _cmd_break_now(self):
        self._start_break()

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
        if self._reminders_paused():
            state = "paused"
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
        self._stats_label = tk.Label(win, bg=hexc(THEME["bg"]), bd=0)
        self._stats_label.pack()

        def dash_hit(x, y):
            for bid, (x0, y0, x1, y1) in self._dash_hits:
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return bid
            return None

        def on_move(e):
            h = dash_hit(e.x, e.y)
            if h != self._dash_hover:
                self._dash_hover = h
                self._stats_label.config(cursor="hand2" if h else "")
                self._refresh_stats()

        def on_leave(_):
            if self._dash_hover:
                self._dash_hover = None
                self._refresh_stats()

        def on_click(e):
            h = dash_hit(e.x, e.y)
            if h and h.startswith("tab_"):
                self._dash_tab = h[4:]
                self._refresh_stats()
            elif h == "break_now":
                self._dash_hover = None
                self._start_break()
                self._refresh_stats()

        self._stats_label.bind("<Motion>", on_move)
        self._stats_label.bind("<Leave>", on_leave)
        self._stats_label.bind("<Button-1>", on_click)

        def on_close():
            self._stats_win = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", on_close)
        self._refresh_stats()
        style_titlebar(win)
        win.lift()
        win.focus_force()

    def _refresh_stats(self):
        win = self._stats_win
        if win is None or not win.winfo_exists():
            self._stats_win = None
            return
        img, self._dash_hits = design.render_dashboard(self._dash_data(), self.scale,
                                                       hover=self._dash_hover, tab=self._dash_tab)
        self._stats_photo = ImageTk.PhotoImage(img)
        self._stats_label.config(image=self._stats_photo)

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
            row = tk.Frame(card_frame, bg=card, padx=2, pady=10)
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
        look_row = tk.Frame(outer, bg=bg, pady=12)
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
        auto_row = tk.Frame(outer, bg=bg, pady=14)
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

        def close():
            self._settings_win = None
            win.destroy()

        def save():
            old_interval = self.config["break_interval_min"]
            for key, *_ in fields:
                v = values[key]
                self.config[key] = v if key == "daily_limit_hours" else int(v)
            self.config["theme"] = theme["mode"]
            save_json(CONFIG_PATH, self.config)
            self.apply_theme()
            if self.config["break_interval_min"] != old_interval:
                # new interval applies to the current session; if already past it, remind in a minute
                self.next_break_at = max(self.config["break_interval_min"] * 60, self.session_sec + 60)
            if auto["on"] != autostart_enabled():
                set_autostart(auto["on"])
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
    def _cmd_toggle_pause(self):
        self.reminders_paused_until = 0.0 if self._reminders_paused() else time.time() + 3600
        self._update_tray()

    def _cmd_toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self.icon.update_menu()

    def _cmd_quit(self):
        self.save()
        self.icon.stop()
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
        app.save()


if __name__ == "__main__":
    main()
