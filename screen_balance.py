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
from PIL import Image, ImageTk

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
    "count_video": True,        # a playing video counts as screen time without any input
    "respect_windows_dnd": True,  # stay quiet while Windows' Do not disturb / Focus is on
}
THEMES = ("system", "light", "dark")

TICK_MS = 1000  # 1 s so the "Break in" countdown ticks smoothly
SAVE_EVERY_SEC = 30
POPUP_TIMEOUT_SEC = 300  # an unanswered popup snoozes itself after 5 minutes
MIN_OPEN_BREAK_SEC = 10  # an open break shorter than this was probably a misclick
VIDEO_MAX_IDLE_SEC = 3 * 3600  # "keep the screen on" without any input for longer: stop counting
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


_powrprof = ctypes.WinDLL("powrprof")
_shell32 = ctypes.WinDLL("shell32")


def screen_kept_on() -> bool:
    """True while some app asks Windows to keep the display on, which is what browsers and
    video players do while a video plays (and stop doing when it's paused)."""
    state = wt.ULONG()
    rc = _powrprof.CallNtPowerInformation(16, None, 0, ctypes.byref(state), ctypes.sizeof(state))
    return rc == 0 and bool(state.value & 0x2)   # SystemExecutionState / ES_DISPLAY_REQUIRED


_ntdll = ctypes.WinDLL("ntdll")
_WNF_QUIET_HOURS = 0x0D83063EA3BF1C75   # WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED


def windows_dnd_on() -> bool:
    """True while Windows' Do not disturb / Focus (Focus assist on Windows 10) is on.
    Read from the shell's quiet-hours state: 0 = off, anything else = quiet."""
    name = ctypes.c_ulonglong(_WNF_QUIET_HOURS)
    stamp, value, size = wt.ULONG(), wt.DWORD(), wt.ULONG(4)
    try:
        status = _ntdll.NtQueryWnfStateData(ctypes.byref(name), None, None, ctypes.byref(stamp),
                                            ctypes.byref(value), ctypes.byref(size))
    except (OSError, AttributeError):
        return False
    return status == 0 and size.value >= 4 and value.value != 0


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(_MONITORINFO)]
user32.GetShellWindow.restype = wt.HWND
user32.GetDesktopWindow.restype = wt.HWND
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]


def fullscreen_app_running() -> bool:
    """True while a full-screen app, game, video or presentation is in front.

    Checks the foreground window itself (does it cover its whole monitor?). Windows'
    own "busy" flag isn't used for this, because Windows also raises it whenever its
    Do not disturb / Focus is on, which has nothing to do with full screen."""
    st = ctypes.c_int()
    if _shell32.SHQueryUserNotificationState(ctypes.byref(st)) == 0 and st.value in (3, 4):
        return True   # QUNS_RUNNING_D3D_FULL_SCREEN (exclusive full-screen game), QUNS_PRESENTATION_MODE
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in (user32.GetDesktopWindow(), user32.GetShellWindow()):
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):   # desktop / taskbar
        return False
    r = wt.RECT()
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    return r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom


class AudioMeter:
    """Is any sound playing right now? Reads the Windows output peak meter through
    Core Audio (raw COM via ctypes, no extra packages). Used to tell a video that's
    actually playing apart from apps that just keep the screen on all the time
    (Discord, Medal, OBS, keep-awake tools)."""

    _CLSID_ENUM = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    _IID_ENUM = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    _IID_METER = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"

    class _GUID(ctypes.Structure):
        _fields_ = [("d1", wt.DWORD), ("d2", wt.WORD), ("d3", wt.WORD), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self):
        self._ole32 = ctypes.WinDLL("ole32")
        self._meter = None
        self._device = None
        self._opened_at = -1e9
        self._last_sound = -1e9
        self._inited = False

    def _guid(self, text):
        g = self._GUID()
        self._ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    @staticmethod
    def _vcall(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])

    def _release(self, obj):
        if obj:
            try:
                vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                ctypes.WINFUNCTYPE(wt.ULONG, ctypes.c_void_p)(vtbl[2])(obj)
            except Exception:
                pass

    def _open(self):
        """(Re)connect to the current default speakers/headphones."""
        self._release(self._meter)
        self._release(self._device)
        self._meter = self._device = None
        self._opened_at = uptime()
        if not self._inited:
            self._ole32.CoInitializeEx(None, 2)   # COINIT_APARTMENTTHREADED (Tk thread)
            self._inited = True
        enum = ctypes.c_void_p()
        if self._ole32.CoCreateInstance(ctypes.byref(self._guid(self._CLSID_ENUM)), None, 0x17,
                                        ctypes.byref(self._guid(self._IID_ENUM)), ctypes.byref(enum)):
            return
        try:
            dev = ctypes.c_void_p()
            get_default = self._vcall(enum, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))
            if get_default(enum, 0, 0, ctypes.byref(dev)):      # eRender, eConsole
                return
            meter = ctypes.c_void_p()
            activate = self._vcall(dev, 3, ctypes.POINTER(self._GUID), wt.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p))
            if activate(dev, ctypes.byref(self._guid(self._IID_METER)), 0x17, None, ctypes.byref(meter)):
                self._release(dev)
                return
            self._device, self._meter = dev, meter
        finally:
            self._release(enum)

    def sound_recently(self, within=15.0) -> bool:
        """True if any sound played in the last `within` seconds (videos have quiet moments)."""
        now = uptime()
        try:
            # reconnect now and then, so a switch to headphones/another output is picked up
            if self._meter is None or now - self._opened_at > 60:
                self._open()
            if self._meter is not None:
                peak = ctypes.c_float()
                if self._vcall(self._meter, 3, ctypes.POINTER(ctypes.c_float))(self._meter, ctypes.byref(peak)):
                    self._meter = None        # device gone: reconnect next time
                elif peak.value > 0.0005:
                    self._last_sound = now
        except (OSError, ValueError):
            self._meter = None
        return now - self._last_sound <= within


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


kernel32.CreateEventW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
QUIT_EVENT = f"Local\\{APP_NAME}_quit"
_quit_event = None


def listen_for_quit_requests():
    """A named signal another copy can raise to ask this one to save and quit."""
    global _quit_event
    _quit_event = kernel32.CreateEventW(None, False, False, QUIT_EVENT)


def quit_requested() -> bool:
    return bool(_quit_event) and kernel32.WaitForSingleObject(_quit_event, 0) == 0


def take_over_from_running_copy(timeout=10.0) -> bool:
    """Starting while another copy runs (a restart, or after an update): ask it to save
    and quit cleanly, then take its place. Nothing is lost, unlike killing it."""
    if single_instance():
        return True
    ev = kernel32.OpenEventW(0x0002, False, QUIT_EVENT)     # EVENT_MODIFY_STATE
    if not ev:
        return False        # an old copy that doesn't understand the request: leave it be
    kernel32.SetEvent(ev)
    kernel32.CloseHandle(ev)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _mutex:
            kernel32.CloseHandle(_mutex)
        time.sleep(0.25)
        if single_instance():
            return True
    return False


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


def autostart_target_exists() -> bool:
    """Does the startup entry still point at an existing copy of the app?"""
    import shlex
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            cmd = winreg.QueryValueEx(k, APP_NAME)[0]
        parts = shlex.split(cmd, posix=False)
        script = parts[-1].strip('"') if parts else ""
        return bool(script) and os.path.isfile(script)
    except (OSError, ValueError):
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


def load_json(path, default, attempts=5):
    """Read a JSON object. Missing file -> default. Damaged JSON -> kept aside as a backup,
    default returned. Locked file -> retried, then UnreadableFile (never treated as damaged,
    so a temporary lock can't wipe your history)."""
    last_err = None
    for attempt in range(attempts):
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
            if attempt + 1 < attempts:
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
    for key in ("startup_notice", "count_video", "respect_windows_dnd"):
        if isinstance(raw.get(key), bool):
            cfg[key] = raw[key]
    return cfg


def _secs(v) -> float:
    """A sane, finite, non-negative number of seconds (at most one day)."""
    v = float(v)
    if not math.isfinite(v):
        raise ValueError("not a finite number")
    return min(max(v, 0.0), 86400.0)


def clean_usage(raw: dict) -> dict:
    out = {}
    for day, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        try:
            dt.date.fromisoformat(day)
            apps = {str(k)[:200]: _secs(v) for k, v in (rec.get("apps") or {}).items()}
            hours = {str(int(k)): _secs(v) for k, v in (rec.get("hours") or {}).items() if 0 <= int(k) < 24}
            breaks = float(rec.get("breaks", 0))
            if not math.isfinite(breaks):
                raise ValueError("not a finite number")
            entry = {
                "total": _secs(rec.get("total", 0)),
                "apps": apps,
                "hours": hours,
                "breaks": int(min(max(breaks, 0), 10000)),
                "break_sec": _secs(rec.get("break_sec", 0)),
                "longest": _secs(rec.get("longest", 0)),
            }
            for k in ("first", "last"):
                if isinstance(rec.get(k), str):
                    entry[k] = rec[k][:5]
            manual = []
            for m in rec.get("manual") or []:
                try:
                    manual.append({"id": str(m["id"])[:40], "start": str(m["start"])[:5],
                                   "secs": _secs(m["secs"]), "label": str(m.get("label") or MANUAL_LABEL)[:60]})
                except (KeyError, TypeError, ValueError):
                    continue
            if manual:
                entry["manual"] = manual
            out[day] = entry
        except (TypeError, ValueError, AttributeError, OverflowError):
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
        if rec.get("manual"):
            known = {m["id"] for m in cur.get("manual", [])}
            cur.setdefault("manual", []).extend(m for m in rec["manual"] if m["id"] not in known)
    return out


MANUAL_LABEL = "Added manually"


def _spread_over_hours(rec, start_min, secs, sign):
    """Add (sign=1) or take back (sign=-1) `secs` across the hours it covers, from start_min."""
    t = start_min * 60.0
    left = min(secs, 86400.0 - t)
    applied = 0.0
    while left > 0.5:
        h = int(t // 3600)
        chunk = min(left, (h + 1) * 3600.0 - t)
        key = str(h)
        rec["hours"][key] = max(0.0, rec["hours"].get(key, 0.0) + sign * chunk)
        if rec["hours"][key] <= 0.5:
            del rec["hours"][key]
        t += chunk
        left -= chunk
        applied += chunk
    return applied


def add_manual_time(rec, start_min, secs, label):
    """Record time you spent on screen that the app didn't see (another PC, before it was
    installed...). It counts everywhere, like tracked time, and can be removed again."""
    label = (label or "").strip()[:60] or MANUAL_LABEL
    applied = _spread_over_hours(rec, start_min, secs, +1)
    if applied <= 0:
        return None
    rec["total"] += applied
    rec["apps"][label] = rec["apps"].get(label, 0.0) + applied
    start = f"{start_min // 60}:{start_min % 60:02d}"
    if not rec.get("first") or _hm(start) < _hm(rec["first"]):
        rec["first"] = start
    entry = {"id": f"m{int(time.time() * 1000)}{random.randrange(1000)}", "start": start,
             "secs": applied, "label": label}
    rec.setdefault("manual", []).append(entry)
    return entry


def remove_manual_time(rec, entry_id):
    for i, m in enumerate(rec.get("manual", [])):
        if m["id"] != entry_id:
            continue
        _spread_over_hours(rec, _hm(m["start"]), m["secs"], -1)
        rec["total"] = max(0.0, rec["total"] - m["secs"])
        left = rec["apps"].get(m["label"], 0.0) - m["secs"]
        if left > 0.5:
            rec["apps"][m["label"]] = left
        else:
            rec["apps"].pop(m["label"], None)
        del rec["manual"][i]
        if not rec["manual"]:
            del rec["manual"]
        return True
    return False


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
        f.flush()
        os.fsync(f.fileno())       # on disk before it replaces the old file (power cuts)
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
        self._vsync_off_until = 0.0
        try:
            self._vsync = ctypes.WinDLL("dwmapi").DwmFlush
            self._vsync.restype = ctypes.c_long   # HRESULT, checked by hand (0 = ok)
        except (OSError, AttributeError):
            self._vsync = None

    def play(self, duration, step):
        """step(t) gets progress 0..1 each frame; return False to stop early."""
        self._anims.append((time.perf_counter(), max(duration, 1e-3), step))
        if not self._running:
            self._running = True
            self.root.after(0, self._frame)

    def _frame(self):
        delay = 8                      # fallback pacing when vsync isn't usable
        if self._vsync is not None and uptime() >= self._vsync_off_until:
            started = time.perf_counter()
            hr = self._vsync()         # wait for the next display refresh
            waited = time.perf_counter() - started
            if hr != 0 or waited > 0.1:
                # display off, locked, remote desktop... or it stalled: pace by timer for a while
                self._vsync_off_until = uptime() + 30
            elif waited < 0.0005:
                delay = 4              # returned instantly: don't spin the CPU
            else:
                delay = 0              # just waited for the refresh: draw straight away
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
            self.root.after(delay, self._frame)
        else:
            self._running = False


class HoverLayer:
    """Smooth hover for a rendered page.

    The page itself is always drawn without hover. For each button, the background
    draws its hovered look once, crops it to the button and pre-blends a short ramp of
    in-between looks (page -> hovered). Hovering then fades that ramp in, and leaving
    fades it back out, one step per display refresh, with no page redraw at all."""

    STEPS = 12           # in-between looks per button
    FADE_IN = 0.11       # seconds
    FADE_OUT = 0.16

    def __init__(self, app, canvas, name):
        self.app, self.canvas, self.name = app, canvas, name
        self.cache = {}        # (context, bid) -> ([PhotoImage ramp], (x, y) in page pixels)
        self.pending = set()
        self.items = {}        # bid -> canvas image item
        self.level = {}        # bid -> 0..1 (how "hovered" it currently looks)
        self.hits = []         # [(bid, box)] in page pixels
        self.origin = (0, 0)   # where the page's top-left sits on the canvas
        self.context = None
        self.hover = None
        self._ticking = False
        self._last = 0.0

    def update(self, hits, origin, context, render_hovered, base_img):
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
            self.app.renderer.submit(f"{self.name}:hover:{ck}", self._ramp_job,
                                     (render_hovered, bid, box, base_img),
                                     lambda res, ck=ck: self._store(ck, res))
        for bid in list(self.items):
            self._draw(bid)

    @classmethod
    def _ramp_job(cls, render_hovered, bid, box, base_img):
        img, _ = render_hovered(bid)
        x0, y0, x1, y1 = box
        x0, y0 = max(0, int(x0) - 2), max(0, int(y0) - 2)
        x1, y1 = min(img.width, math.ceil(x1) + 2), min(img.height, math.ceil(y1) + 2)
        hov = img.crop((x0, y0, x1, y1)).convert("RGB")
        base = base_img.crop((x0, y0, x1, y1)).convert("RGB")
        ramp = [Image.blend(base, hov, (i + 1) / cls.STEPS) for i in range(cls.STEPS)]
        return ramp, (x0, y0)

    def _store(self, ck, result):
        self.pending.discard(ck)
        try:
            if not self.canvas.winfo_exists():
                return
        except tk.TclError:
            return
        ramp, pos = result
        self.cache[ck] = ([ImageTk.PhotoImage(im) for im in ramp], pos)
        if ck[0] == self.context:
            self._draw(ck[1])

    def hit(self, x, y):
        ox, oy = self.origin
        for bid, (x0, y0, x1, y1) in self.hits:
            if x0 + ox <= x <= x1 + ox and y0 + oy <= y <= y1 + oy:
                return bid
        return None

    def set_hover(self, bid):
        if bid == self.hover:
            return
        self.hover = bid
        if bid is not None:
            self.level.setdefault(bid, 0.0)
        if not self._ticking:
            self._ticking = True
            self._last = time.perf_counter()
            self.app.animator.play(3600, self._tick)   # stops itself once everything settles

    def _tick(self, _t):
        now = time.perf_counter()
        dt, self._last = now - self._last, now
        moving = False
        for bid, lv in list(self.level.items()):
            target = 1.0 if bid == self.hover else 0.0
            if lv != target:
                rate = dt / (self.FADE_IN if target > lv else self.FADE_OUT)
                lv = min(target, lv + rate) if target > lv else max(target, lv - rate)
                self.level[bid] = lv
                moving = moving or lv != target
            self._draw(bid)
            if lv == 0.0 and bid != self.hover:
                del self.level[bid]
        if not moving:
            self._ticking = False
            return False

    def _draw(self, bid):
        try:
            canvas = self.canvas
            item = self.items.get(bid)
            lv = self.level.get(bid, 0.0)
            found = self.cache.get((self.context, bid))
            if lv <= 0.0 or not found:
                if item is not None:
                    canvas.itemconfig(item, state="hidden")
                return
            ramp, (x, y) = found
            eased = 1 - (1 - lv) ** 2
            photo = ramp[min(len(ramp) - 1, max(0, round(eased * len(ramp)) - 1))]
            ox, oy = self.origin
            if item is None:
                item = self.items[bid] = canvas.create_image(0, 0, anchor="nw")
            canvas.coords(item, ox + x, oy + y)
            canvas.itemconfig(item, image=photo, state="normal")
            canvas.tag_raise(item)
        except tk.TclError:
            pass   # the window was closed mid-fade


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
        if not self._usage_pending_merge:
            self._merge_pending_file()
        self._prune_usage()
        self._last_day = today_key()

        self.session_sec = 0.0            # continuous active time since last break
        self.break_count = 0              # bumps every time a real break is detected
        self.next_break_at = self.config["break_interval_min"] * 60
        self.dnd_until = self._load_dnd()  # Do not disturb: 0 = off, inf = until turned off
        self._restore_session()
        self.limit_notices = {}           # date -> {"80": bool, "100": bool, "last": ts}
        self.current_app = ""
        self.is_idle = False
        self.is_watching = False          # a video (or full-screen app) is keeping the screen on
        self._win_dnd = False             # Windows' Do not disturb / Focus is on (and we respect it)
        self.audio = AudioMeter()
        self._idle_offset = 0.0           # input-idle time that was spent watching

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
        self._addtime_win = None
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
        self._menu_text = self._menu_snapshot()
        self._menu_lock = threading.Lock()
        self.icon = pystray.Icon(APP_NAME, design.render_tray(0, False, self._dark_taskbar), APP_NAME,
                                 self._build_menu())
        # pystray rebuilds the menu on its own thread after each click, and we rebuild it on
        # ours when the text changes: never let the two rebuild at the same moment
        unlocked_update = self.icon.update_menu

        def locked_update():
            with self._menu_lock:
                unlocked_update()
        self.icon.update_menu = locked_update

    def _fade_window(self, win, to, duration, then=None):
        """Ease a window's opacity to `to`, one step per display refresh; then() at the end."""
        try:
            start = float(win.attributes("-alpha"))
        except tk.TclError:
            return

        def step(t):
            try:
                if not win.winfo_exists():
                    return False
                e = 1 - (1 - t) ** 3
                win.attributes("-alpha", start + (to - start) * e)
                if t >= 1.0 and then:
                    then()
            except tk.TclError:
                return False
        self.animator.play(duration, step)

    def _fade_out_and_destroy(self, win, duration=0.14, slide=0):
        """Fade (and optionally slide down) a window that's already been let go of, then destroy it."""
        try:
            x, y = win.winfo_x(), win.winfo_y()
            start = float(win.attributes("-alpha"))
        except tk.TclError:
            return

        def step(t):
            try:
                if not win.winfo_exists():
                    return False
                e = t * t            # ease-in: leave gently, then quickly
                win.attributes("-alpha", start * (1 - e))
                if slide:
                    win.geometry(f"+{x}+{round(y + slide * e)}")
                if t >= 1.0:
                    win.destroy()
            except tk.TclError:
                return False
        self.animator.play(duration, step)

    def _heal_windows(self):
        """If a window vanished behind our back (e.g. Alt+F4), forget it so nothing stays stuck."""
        for attr in ("_popup", "_break_win", "_stats_win", "_settings_win", "_addtime_win"):
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

    @staticmethod
    def _pending_path():
        return os.path.join(DATA_DIR, "usage.pending.json")

    def _merge_pending_file(self):
        """Fold in time tracked during an earlier run while usage.json was locked."""
        pend = self._pending_path()
        if not os.path.exists(pend):
            return
        try:
            extra = clean_usage(load_json(pend, {}, attempts=1))
            self.usage = merge_usage(self.usage, extra)
            save_json(USAGE_PATH, self.usage)
            os.remove(pend)
        except (UnreadableFile, OSError):
            log_error("could not merge usage.pending.json yet:" + os.linesep + traceback.format_exc())

    def save(self):
        if self._usage_pending_merge:
            try:
                # one quick try, no waiting: this runs on the UI thread
                on_disk = clean_usage(load_json(USAGE_PATH, {}, attempts=1))
            except UnreadableFile:
                # still locked: keep this session's time in a side file so quitting or a
                # crash can't lose it; it's merged in on the next successful start
                save_json(self._pending_path(), self.usage)
                return
            self.usage = merge_usage(on_disk, self.usage)
            self._usage_pending_merge = False
            save_json(USAGE_PATH, self.usage)
            try:
                os.remove(self._pending_path())
            except OSError:
                pass
            return
        save_json(USAGE_PATH, self.usage)

    def _save_safely(self):
        """Save without ever raising (a locked file must not stall the clock or block Quit)."""
        try:
            self.save()
            self._save_state()
            return True
        except Exception:
            log_error("saving failed (will retry):" + os.linesep + traceback.format_exc())
            return False

    def _limit_sec(self):
        return self.config["daily_limit_hours"] * 3600

    # ---------------- tray ----------------
    def _menu_snapshot(self):
        """Everything the tray menu shows, computed on the Tk thread. The menu itself is
        built on pystray's thread, so it only ever reads this ready-made snapshot and never
        touches live app data (which could change under it)."""
        dnd = self._dnd_active()
        return {
            "today": f"Today: {fmt_dur(self._day()['total'])}",
            "break_in": self._break_in_text(precise=False),
            "since": f"Since last break: {fmt_dur(self.session_sec)}",
            "timed": f"Take a {self.config['break_length_min']:g}-min break",
            "dnd": f"Do not disturb ({self._dnd_label()})" if dnd else "Do not disturb",
            "dnd_on": dnd,
            "autostart": autostart_enabled(),
        }

    def _build_menu(self):
        post = lambda cmd: (lambda icon, item: self.cmd_q.put(cmd))
        m = lambda key: (lambda item: self._menu_text[key])
        return pystray.Menu(
            pystray.MenuItem(m("today"), post("stats"), default=True),
            pystray.MenuItem(m("break_in"), None, enabled=False),
            pystray.MenuItem(m("since"), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", post("stats")),
            pystray.MenuItem(m("timed"), post("break_now")),
            pystray.MenuItem("Take an open break (until I'm back)", post("open_break")),
            pystray.MenuItem(
                m("dnd"),
                pystray.Menu(
                    pystray.MenuItem("For 30 minutes", post("dnd_30")),
                    pystray.MenuItem("For 1 hour", post("dnd_60")),
                    pystray.MenuItem("For 2 hours", post("dnd_120")),
                    pystray.MenuItem("Until I turn it off", post("dnd_forever")),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Turn off", post("dnd_off"), enabled=m("dnd_on")),
                ),
            ),
            pystray.MenuItem("Settings", post("settings")),
            pystray.MenuItem("Start with Windows", post("toggle_autostart"), checked=m("autostart")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", post("quit")),
        )

    # ---------------- do not disturb ----------------
    @staticmethod
    def _state_path():
        return os.path.join(DATA_DIR, "state.json")

    def _restore_session(self):
        """After a quick restart (shorter than a break), carry on with the same stretch and
        break countdown instead of starting from zero. A longer gap was a real break."""
        try:
            sess = load_json(self._state_path(), {}).get("session") or {}
            sec, nxt, saved = float(sess["sec"]), float(sess["next_break_at"]), float(sess["saved_at"])
        except (UnreadableFile, KeyError, TypeError, ValueError, AttributeError):
            return
        gap = time.time() - saved
        if not all(map(math.isfinite, (sec, nxt, saved))) or not 0 <= gap < self.config["break_length_min"] * 60:
            return
        self.session_sec = min(max(sec, 0.0), 86400.0)
        self.next_break_at = min(max(nxt, 60.0), self.session_sec + 240 * 60)

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
        now = time.time()
        if not math.isfinite(until) or until <= now or until > now + 7 * 86400:
            return 0.0             # expired, or nonsense from a damaged/edited file
        return until

    def _save_dnd(self):
        self._save_state()

    def _save_state(self):
        """Do not disturb, plus the current stretch, so a restart doesn't reset your break timer."""
        value = "forever" if self.dnd_until == float("inf") else self.dnd_until
        session = {"sec": self.session_sec, "next_break_at": self.next_break_at, "saved_at": time.time()}
        try:
            save_json(self._state_path(), {"dnd_until": value, "session": session})
        except OSError:
            log_error("could not save Do not disturb state:\n" + traceback.format_exc())

    def _dnd_active(self):
        """The app's own Do not disturb."""
        return time.time() < self.dnd_until

    def _quiet(self):
        """No popups right now: the app's DND, or Windows' Do not disturb / Focus."""
        return self._dnd_active() or self._win_dnd

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
        """Seconds of active use left before the next break reminder (None while quiet)."""
        if self._quiet():
            return None
        return self.next_break_at - self.session_sec

    def _break_in_text(self, precise=True):
        """precise: 'Break in: 12:34' (tooltip, dashboard). Otherwise minutes only, for the
        tray menu, which Windows has to rebuild every time its text changes."""
        if self._break_win is not None:
            return "On an open break" if self._break_open else "On a break"
        if self._dnd_active():
            return f"Do not disturb {self._dnd_label()}"
        if self._win_dnd:
            return "Windows Do not disturb is on"
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
        paused = self._quiet()
        key = (round(ratio, 2), paused, self._dark_taskbar)
        if key != self._tray_key:      # only re-render when it visibly changes
            self._tray_key = key
            self.icon.icon = design.render_tray(ratio, paused, self._dark_taskbar)
        status = "idle" if self.is_idle else ("watching video" if self.is_watching else "active")
        limit_txt = f" / {fmt_dur(limit)}" if limit else ""
        self.icon.title = (f"Screen Balance\n{self._break_in_text()}\n"
                           f"Today: {fmt_dur(total)}{limit_txt} ({status})")[:127]
        # rebuild the tray menu only when something in it changed: rebuilding every second
        # could close or glitch the menu while you have it open
        snap = self._menu_snapshot()
        sig = tuple(sorted(snap.items()))
        if sig != self._menu_sig:
            self._menu_sig = sig
            self._menu_text = snap
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
        if quit_requested():          # another copy is taking over: save and step aside
            self._cmd_quit()
            return

        # follow Windows switching between light and dark (checked every few seconds)
        if now - self._theme_checked >= 3 or now < self._theme_checked:
            self._theme_checked = now
            self._dark_taskbar = taskbar_is_dark()   # the tray icon tile follows the taskbar
            if self.config.get("theme") == "system":
                self.apply_theme()

        if today_key() != self._last_day:   # midnight: drop old history, keep running
            self._last_day = today_key()
            self._prune_usage()

        # Windows' Do not disturb / Focus: going quiet closes a popup that's showing
        # (the break reminder comes back once it's off), like the app's own DND
        win_dnd = self.config.get("respect_windows_dnd", True) and windows_dnd_on()
        if win_dnd != self._win_dnd:
            self._win_dnd = win_dnd
            if win_dnd and self._popup is not None:
                self._discard_window("_popup")
            self._tray_key = None
            self._menu_sig = None

        raw_idle = idle_seconds()
        locked = session_locked()
        # watching a video: no mouse/keyboard input, but you're still looking at the screen
        # Needs sound actually playing: some apps (Discord, Medal, OBS, keep-awake tools)
        # keep the screen on all the time, which alone would hide every real break.
        watching = (self.config.get("count_video", True) and not locked
                    and self._break_win is None
                    and raw_idle < VIDEO_MAX_IDLE_SEC
                    and self.audio.sound_recently()
                    and (screen_kept_on() or fullscreen_app_running()))
        if watching:
            self._idle_offset = raw_idle          # time spent watching isn't time away
        elif raw_idle < self._idle_offset:
            self._idle_offset = 0.0               # you've touched something since: back to normal
        idle = 0.0 if watching else max(0.0, raw_idle - self._idle_offset)
        self.is_watching = watching
        break_len = self.config["break_length_min"] * 60
        # stop counting at the idle threshold, and never past the break length (otherwise the
        # same minutes would count as screen time AND as a break); a locked PC never counts
        self.is_idle = (idle >= min(self.config["idle_threshold_sec"], break_len)) or locked

        # a long enough idle stretch (or the PC sleeping that long) counts as a break
        if (idle >= break_len or gap >= break_len) and self.session_sec > 0:
            self.session_sec = 0
            self.break_count += 1
            self.next_break_at = self.config["break_interval_min"] * 60

        # log breaks taken by stepping away (counted once you come back) or by sleeping the PC
        if self._break_win is None:
            if gap >= break_len:
                self._record_break(max(gap, idle))   # idle time before the sleep counts too
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
        if self._quiet() or self._popup is not None:
            return
        if self.session_sec >= self.next_break_at:
            self._show_popup(
                "break", "Time for a break",
                f"You've been at the screen for {fmt_dur(self.session_sec)} straight. "
                f"Stand up, stretch, and look at something far away.",
            )

    def _check_limit(self):
        if self._quiet() or self._popup is not None:
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
        try:
            self._show_popup_impl(kind, title, message)
        except Exception:
            log_error("could not show a popup:" + os.linesep + traceback.format_exc())
            self._discard_window("_popup")

    def _discard_window(self, attr):
        w = getattr(self, attr)
        setattr(self, attr, None)
        if w is not None:
            try:
                w.destroy()
            except tk.TclError:
                pass

    def _show_popup_impl(self, kind, title, message):
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
            canvas.unbind("<Button-1>")
            canvas.unbind("<Motion>")
            self._fade_out_and_destroy(win, 0.16, slide=round(16 * self.scale))
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
        try:
            self._start_break_impl(open_ended)
        except Exception:
            log_error("could not start the break screen:" + os.linesep + traceback.format_exc())
            self._discard_window("_break_win")

    def _start_break_impl(self, open_ended=False):
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
            hover.update(hits, origin, context, render, img)

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
            if action == "skip" and not open_ended and took >= total:
                action = "finish"   # the countdown already reached 0: it's a full break
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
            canvas.unbind("<Button-1>")
            canvas.unbind("<Motion>")
            self._fade_out_and_destroy(win, 0.24)
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
                "apps": dict(rec.get("apps", {})),
                "today": i == 0,
            })
        if self._dnd_active():
            state = "dnd"
        elif self._win_dnd:
            state = "windows_dnd"
        elif self.is_idle:
            state = "idle"
        elif self.is_watching:
            state = "video"
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
            "manual_today": sum(m["secs"] for m in day.get("manual", [])),
        }

    def _cmd_stats(self):
        if self._stats_win is not None:
            self._stats_win.deiconify()
            self._stats_win.lift()
            self._stats_win.focus_force()
            return
        win = tk.Toplevel(self.root)
        self._stats_win = win
        win.attributes("-alpha", 0.0)         # faded in once the first frame is ready
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
            elif h == "add_time":
                self._cmd_add_time()
            elif h == "dnd":
                # dashboard button: 1 hour of quiet, or switch it off if it's on
                self._set_dnd(0 if self._dnd_active() else 60)

        self._stats_label.bind("<Motion>", on_move)
        self._stats_label.bind("<Leave>", on_leave)
        self._stats_label.bind("<Button-1>", on_click)

        def on_close():
            self._stats_win = None
            self._dash_shown_tab = None
            self._dash_fading = False
            self.renderer.cancel("dash")
            self._fade_out_and_destroy(win, 0.13)
        win.protocol("WM_DELETE_WINDOW", on_close)
        self._refresh_stats(sync=True)
        style_titlebar(win)
        win.lift()
        win.focus_force()
        self._fade_window(win, 1.0, 0.16)

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
        c = self._stats_label
        if int(c.cget("width")) != img.width or int(c.cget("height")) != img.height:
            c.config(width=img.width, height=img.height)
        tab = context[0]
        old = getattr(self, "_dash_pil", None)
        new = img.convert("RGB")
        self._dash_pil = new
        if getattr(self, "_dash_shown_tab", None) not in (None, tab) and old is not None \
                and old.size == new.size:
            self._crossfade_dash(old, new)        # switching tabs: blend instead of snapping
        elif not getattr(self, "_dash_fading", False):
            self._stats_photo = ImageTk.PhotoImage(img)
            c.itemconfig(self._dash_item, image=self._stats_photo)
        self._dash_shown_tab = tab
        self._dash_layer.update(self._dash_hits, (0, 0), context, render, img)

    def _crossfade_dash(self, old, new, duration=0.2, steps=10):
        """Blend the old tab into the new one. Each in-between look is built only when the
        fade reaches it, so the work is spread over the frames instead of one big hitch."""
        self._dash_fading = True
        self._dash_fade_gen = getattr(self, "_dash_fade_gen", 0) + 1
        gen = self._dash_fade_gen
        shown = {"i": -1}
        target = {"img": new}

        def step(t):
            if gen != self._dash_fade_gen:
                return False                 # a newer tab switch took over
            win = self._stats_win
            if win is None or not win.winfo_exists():
                self._dash_fading = False
                return False
            latest = self._dash_pil          # a fresh render may land mid-fade: fade into it
            if latest is not target["img"] and latest.size == new.size:
                target["img"] = latest
            i = min(steps, max(1, round((1 - (1 - t) ** 3) * steps)))
            if i != shown["i"]:
                shown["i"] = i
                frame = target["img"] if i >= steps else Image.blend(old, target["img"], i / steps)
                self._stats_photo = ImageTk.PhotoImage(frame)
                self._stats_label.itemconfig(self._dash_item, image=self._stats_photo)
            if t >= 1.0:
                self._dash_fading = False
                self._stats_photo = ImageTk.PhotoImage(self._dash_pil)
                self._stats_label.itemconfig(self._dash_item, image=self._stats_photo)

        self.animator.play(duration, step)

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
        win.attributes("-alpha", 0.0)         # faded in once it's built
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

        def toggle_row(title, hint, initial):
            """A switch row with a title and a one-line hint. Returns {"on": bool}."""
            tk.Frame(outer, bg=hexc(T["border"]), height=1).pack(fill="x")
            row = tk.Frame(outer, bg=bg, pady=9)
            row.pack(fill="x")
            value = {"on": bool(initial)}
            text = tk.Frame(row, bg=bg)
            text.pack(side="left")
            tk.Label(text, text=title, bg=bg, fg=hexc(T["text"]), font=("Segoe UI", 10)).pack(anchor="w")
            tk.Label(text, text=hint, bg=bg, fg=hexc(T["muted"]), font=("Segoe UI", 9)).pack(anchor="w")
            c = tk.Canvas(row, width=34, height=20, bg=bg, highlightthickness=0, cursor="hand2")
            c.pack(side="right")

            def draw():
                c.delete("all")
                col = hexc(T["a1"]) if value["on"] else hexc(T["border_hi"])
                c.create_oval(0, 0, 20, 20, fill=col, outline=col)
                c.create_oval(14, 0, 34, 20, fill=col, outline=col)
                c.create_rectangle(10, 0, 24, 20, fill=col, outline=col)
                x = 16 if value["on"] else 2
                c.create_oval(x, 2, x + 16, 18, fill="white", outline="white")
            c.bind("<Button-1>", lambda e: (value.update(on=not value["on"]), draw()))
            draw()
            return value

        respect_win = toggle_row("Respect Windows Do not disturb",
                                 "No break popups while Windows' Do not disturb or Focus is on",
                                 self.config.get("respect_windows_dnd", True))
        video = toggle_row("Count videos as screen time",
                           "Watching a video counts even without touching the mouse or keyboard",
                           self.config.get("count_video", True))
        notice = toggle_row("Notify me when it starts",
                            "Shows that Screen Balance is running, even if its icon is hidden",
                            self.config.get("startup_notice", True))

        def close():
            if self._settings_win is not win:
                return
            self._settings_win = None
            self._fade_out_and_destroy(win, 0.13)

        def save():
            if self._settings_win is not win:
                return     # already closing (e.g. Cancel then a quick click on Save)
            old_interval = self.config["break_interval_min"]
            old_limit = self.config["daily_limit_hours"]
            for key, *_ in fields:
                v = values[key]
                self.config[key] = v if key == "daily_limit_hours" else int(v)
            self.config["theme"] = theme["mode"]
            self.config["startup_notice"] = notice["on"]
            self.config["count_video"] = video["on"]
            self.config["respect_windows_dnd"] = respect_win["on"]
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
            if self.config["daily_limit_hours"] != old_limit:
                self.limit_notices.pop(today_key(), None)   # new limit: warn again at 80%/100%
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
        self._fade_window(win, 1.0, 0.16)

    # ---------------- add time by hand ----------------
    def _cmd_add_time(self):
        """Add screen time the app didn't see (another device, PC off, before installing).
        Pick a day, a start time and how long; entries can be removed again."""
        if self._addtime_win is not None:
            self._addtime_win.lift()
            self._addtime_win.focus_force()
            return
        T = THEME
        bg, fg, muted = hexc(T["bg"]), hexc(T["text"]), hexc(T["muted"])
        win = tk.Toplevel(self.root)
        self._addtime_win = win
        win.attributes("-alpha", 0.0)
        win.title("Screen Balance — Add time")
        win.configure(bg=bg)
        win.resizable(False, False)
        outer = tk.Frame(win, bg=bg, padx=28, pady=22)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="Add time", bg=bg, fg=fg, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(outer, text="Screen time the app didn't see, like another device or while it wasn't running.",
                 bg=bg, fg=muted, font=("Segoe UI", 10), wraplength=460, justify="left").pack(anchor="w", pady=(0, 14))

        today = dt.date.today()
        days = [today - dt.timedelta(days=i) for i in range(6, -1, -1)]
        now = dt.datetime.now()
        st = {"day": today, "start": max(0, now.hour - 1) * 60, "dur": 60}

        def row(title):
            tk.Frame(outer, bg=hexc(T["border"]), height=1).pack(fill="x")
            r = tk.Frame(outer, bg=bg, pady=9)
            r.pack(fill="x")
            tk.Label(r, text=title, bg=bg, fg=fg, font=("Segoe UI", 10)).pack(side="left")
            return r

        # day picker (last 7 days)
        day_row = row("Day")
        seg = tk.Frame(day_row, bg=bg, highlightthickness=1, highlightbackground=hexc(T["border_hi"]))
        seg.pack(side="right")
        day_btns = {}

        def draw_days():
            for d, lbl in day_btns.items():
                on = d == st["day"]
                lbl.config(bg=hexc(T["card_hi"]) if on else bg, fg=fg if on else muted,
                           font=("Segoe UI Semibold" if on else "Segoe UI", 9))

        def pick_day(d):
            st["day"] = d
            draw_days()
            refresh_list()
            check()

        for d in days:
            text = "Today" if d == today else d.strftime("%a %d").replace(" 0", " ")
            lbl = tk.Label(seg, text=text, bg=bg, cursor="hand2", padx=7, pady=3)
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, d=d: pick_day(d))
            day_btns[d] = lbl
        draw_days()

        def stepper(parent, get, change, fmt):
            box = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=hexc(T["border_hi"]))
            val = tk.Label(box, text=fmt(get()), width=8, bg=bg, fg=fg, font=("Segoe UI", 10))

            def bump(sign):
                change(sign)
                val.config(text=fmt(get()))
                check()
            for sign, sym in ((-1, "−"), (1, "+")):
                b = tk.Label(box, text=sym, bg=bg, fg=muted, cursor="hand2", font=("Segoe UI", 12), width=2)
                b.bind("<Button-1>", lambda e, sg=sign: bump(sg))
                b.bind("<Enter>", lambda e, b=b: b.config(bg=hexc(T["card_hi"]), fg=fg))
                b.bind("<Leave>", lambda e, b=b: b.config(bg=bg, fg=muted))
                b.pack(side="left")
                if sign < 0:
                    val.pack(side="left")
            return box, val

        # start time in 15-minute steps
        start_row = row("From")
        box, _ = stepper(start_row, lambda: st["start"],
                         lambda sg: st.update(start=max(0, min(23 * 60 + 45, st["start"] + sg * 15))),
                         lambda m: f"{m // 60}:{m % 60:02d}")
        box.pack(side="right")

        # duration: hours and minutes steppers, both showing the total
        dur_row = row("How long")
        labels = []

        def set_dur(minutes):
            st["dur"] = max(5, min(16 * 60, minutes))
            for lbl in labels:
                lbl.config(text=fmt_len(st["dur"]))

        def fmt_len(m):
            return f"{m // 60}h {m % 60:02d}m"
        box_m, lbl_m = stepper(dur_row, lambda: st["dur"], lambda sg: set_dur(st["dur"] + sg * 5),
                               lambda m: "± 5 min")
        box_h, lbl_h = stepper(dur_row, lambda: st["dur"], lambda sg: set_dur(st["dur"] + sg * 60), fmt_len)
        labels.append(lbl_h)
        box_m.pack(side="right")
        box_h.pack(side="right", padx=(0, 8))

        what_row = row("What")
        what = tk.Entry(what_row, bg=hexc(T["card_hi"]), fg=fg, insertbackground=fg, relief="flat",
                        font=("Segoe UI", 10), width=24)
        what.insert(0, MANUAL_LABEL)
        what.pack(side="right", ipady=3)

        tk.Frame(outer, bg=hexc(T["border"]), height=1).pack(fill="x")
        msg = tk.Label(outer, text="", bg=bg, fg=hexc(T["danger"]), font=("Segoe UI", 9))
        msg.pack(anchor="w", pady=(6, 0))

        # what's already been added that day, each with Remove
        tk.Label(outer, text="Added by hand that day", bg=bg, fg=muted,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(6, 2))
        listing = tk.Frame(outer, bg=bg)
        listing.pack(fill="x")

        def day_rec():
            return self._day(st["day"].isoformat())

        def refresh_list():
            for w in listing.winfo_children():
                w.destroy()
            entries = self.usage.get(st["day"].isoformat(), {}).get("manual", [])
            if not entries:
                tk.Label(listing, text="Nothing yet", bg=bg, fg=hexc(T["dim"]),
                         font=("Segoe UI", 9)).pack(anchor="w")
            for m in entries:
                r = tk.Frame(listing, bg=bg)
                r.pack(fill="x", pady=1)
                tk.Label(r, text=f"{m['start']}  ·  {fmt_dur(m['secs'])}  ·  {m['label']}", bg=bg, fg=fg,
                         font=("Segoe UI", 9)).pack(side="left")
                rm = tk.Label(r, text="Remove", bg=bg, fg=hexc(T["danger"]), cursor="hand2",
                              font=("Segoe UI", 9, "underline"))
                rm.pack(side="right")
                rm.bind("<Button-1>", lambda e, mid=m["id"]: remove(mid))

        def check():
            if st["day"] == today and st["start"] > now.hour * 60 + now.minute:
                msg.config(text="That start time hasn't happened yet today.")
                return False
            if st["start"] + st["dur"] > 24 * 60:
                msg.config(text="That runs past midnight: only the part before 0:00 is added.")
                return True
            msg.config(text="")
            return True

        def finish_change():
            self._save_safely()
            self._tray_key = None
            self._update_tray()
            if self._stats_win is not None:
                self._refresh_stats()
            refresh_list()

        def remove(mid):
            if remove_manual_time(day_rec(), mid):
                finish_change()

        def close():
            if self._addtime_win is not win:
                return
            self._addtime_win = None
            self._fade_out_and_destroy(win, 0.13)

        def add():
            if self._addtime_win is not win or not check():
                return
            if add_manual_time(day_rec(), st["start"], st["dur"] * 60, what.get()):
                finish_change()
                close()

        self._addtime_add = add          # for tests
        self._addtime_state = st

        btns = tk.Frame(outer, bg=bg)
        btns.pack(fill="x", pady=(14, 0))
        flat_button(btns, "Add", add, primary=True).pack(side="right")
        flat_button(btns, "Cancel", close).pack(side="right", padx=(0, 8))
        refresh_list()
        check()
        win.protocol("WM_DELETE_WINDOW", close)
        style_titlebar(win)
        win.lift()
        win.focus_force()
        self._fade_window(win, 1.0, 0.16)

    # ---------------- other commands ----------------
    def _cmd_toggle_autostart(self):
        set_autostart(not autostart_enabled())
        self._menu_sig = None
        self._menu_text = self._menu_snapshot()
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
    if not take_over_from_running_copy():
        return
    listen_for_quit_requests()
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    if autostart_enabled() and not autostart_target_exists():
        set_autostart(True)  # the copy it pointed at was moved or deleted: point at this one
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
