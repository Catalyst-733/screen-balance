"""
Regenerates the README images in docs/ using demo data (never your real usage).

    python tools/make_screenshots.py
"""

import ctypes
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
ctypes.windll.shcore.SetProcessDpiAwareness(1)

from PIL import Image, ImageChops, ImageGrab  # noqa: E402

import design  # noqa: E402

OUT = os.path.join(ROOT, "docs")
SCALE = 2  # crisp on high-DPI screens; the README displays them at half size

_HOURS = [0.0] * 24
for _h, _v in {9: 1800, 10: 3300, 11: 2900, 12: 900, 14: 2400, 15: 3100, 16: 1500}.items():
    _HOURS[_h] = _v

DEMO = dict(
    total=4 * 3600 + 12 * 60, limit=6 * 3600, session=38 * 60, break_in=12 * 60,
    apps=[("chrome", 6200), ("Code", 4300), ("Discord", 1900), ("Spotify", 1100), ("Steam", 600)],
    week=[dict(label=lbl, total=t * 3600, breaks=b, break_sec=bm * 60, today=(lbl == "Sun"))
          for lbl, t, b, bm in [("Mon", 5.2, 4, 22), ("Tue", 6.8, 2, 10), ("Wed", 3.9, 5, 27),
                                ("Thu", 4.6, 3, 16), ("Fri", 5.5, 4, 20), ("Sat", 2.4, 1, 6),
                                ("Sun", 4.2, 3, 15)]],
    date_str="Sunday, November 22", state="active", current_app="Code", break_len_min=5,
    breaks=3, break_sec=15 * 60, longest=72 * 60, first="9:12", hours=_HOURS, hour_now=16,
)


_SPLIT = [("chrome", .30), ("Code", .25), ("Discord", .12), ("Figma", .10), ("Spotify", .08),
          ("Notion", .06), ("Slack", .05), ("Steam", .04)]
for _i, _d in enumerate(DEMO["week"]):
    _d["apps"] = {n: _d["total"] * f * (0.6 + 0.8 * ((_i * 7 + k * 3) % 5) / 4) / 1.0
                  for k, (n, f) in enumerate(_SPLIT)}


def save_popup(name, img):
    # popups are flattened onto a color key for transparency; turn that into real alpha
    rgb = img.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, design.popup_key()))
    alpha = diff.convert("L").point(lambda v: 255 if v else 0)
    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    rgba.save(os.path.join(OUT, name))


def settings_shot(theme, name):
    import json
    import screen_balance as sb
    tmp = tempfile.mkdtemp()
    sb.DATA_DIR = tmp
    sb.CONFIG_PATH = os.path.join(tmp, "config.json")
    sb.USAGE_PATH = os.path.join(tmp, "usage.json")
    with open(sb.CONFIG_PATH, "w") as f:
        json.dump({"theme": theme}, f)
    app = sb.ScreenBalance()
    sb.autostart_enabled = lambda: True  # show the toggle switched on
    app._cmd_settings()
    win = app._settings_win
    win.update()
    win.after(600)
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    ImageGrab.grab((x, y, x + win.winfo_width(), y + win.winfo_height()), all_screens=True) \
        .save(os.path.join(OUT, name))
    app.root.destroy()


def main():
    os.makedirs(OUT, exist_ok=True)
    design.set_theme(False)
    design.render_dashboard(DEMO, SCALE, tab="overview")[0].save(os.path.join(OUT, "dashboard.png"))
    design.render_dashboard(DEMO, SCALE, tab="analytics")[0].save(os.path.join(OUT, "analytics.png"))
    design.render_dashboard(DEMO, SCALE, tab="apps")[0].save(os.path.join(OUT, "apps.png"))
    img, _ = design.render_break(0, 300, "Your eyes, back and mind will thank you.", True, None, SCALE,
                                 breaks_today=3)
    img.save(os.path.join(OUT, "break-done.png"))
    img, _ = design.render_popup(
        "break", "Time for a break",
        "You've been at the screen for 52m straight. Stand up, stretch, and look at something far away.",
        [("snooze", "Snooze 10 min", False), ("ok", "Taking a break", True)], None, SCALE)
    save_popup("popup-break.png", img)
    img, _ = design.render_popup(
        "limit", "Daily limit reached",
        "You've used your PC for 6h 05m today. Your limit is 6h 00m. Maybe call it a day?",
        [("ok", "Got it", True)], None, SCALE)
    save_popup("popup-limit.png", img)

    img, _ = design.render_break(187, 300, design.BREAK_TIPS[1], False, None, SCALE)
    img.save(os.path.join(OUT, "break-timer.png"))
    design.render_open_break(412, 300, design.BREAK_TIPS[0], None, SCALE)[0]         .save(os.path.join(OUT, "open-break.png"))

    states = ((0.35, False), (0.85, False), (1.1, False), (0.5, True))
    for dark, name in ((False, "tray-icons.png"), (True, "tray-icons-dark.png")):
        strip = Image.new("RGBA", (64 * 4 + 24 * 3, 64), (0, 0, 0, 0))
        for i, (r, dnd) in enumerate(states):
            ic = design.render_tray(r, dnd, dark_taskbar=dark)
            strip.paste(ic, (i * 88, 0), ic)
        strip.save(os.path.join(OUT, name))

    # dark versions (the README shows them to people using GitHub's dark theme)
    design.set_theme(True)
    design.render_dashboard(DEMO, SCALE, tab="overview")[0].save(os.path.join(OUT, "dashboard-dark.png"))
    design.render_dashboard(DEMO, SCALE, tab="analytics")[0].save(os.path.join(OUT, "analytics-dark.png"))
    design.render_dashboard(DEMO, SCALE, tab="apps")[0].save(os.path.join(OUT, "apps-dark.png"))
    img, _ = design.render_break(187, 300, design.BREAK_TIPS[1], False, None, SCALE)
    img.save(os.path.join(OUT, "break-timer-dark.png"))
    design.render_open_break(412, 300, design.BREAK_TIPS[0], None, SCALE)[0]         .save(os.path.join(OUT, "open-break-dark.png"))
    design.set_theme(False)

    settings_shot("light", "settings.png")
    settings_shot("dark", "settings-dark.png")
    print("Saved images to", OUT)


if __name__ == "__main__":
    main()
