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

DEMO = dict(
    total=4 * 3600 + 12 * 60, limit=6 * 3600, session=38 * 60, interval=50 * 60,
    apps=[("chrome", 6200), ("Code", 4300), ("Discord", 1900), ("Spotify", 1100), ("Steam", 600)],
    week=[("Mon", 5.2 * 3600, False), ("Tue", 6.8 * 3600, False), ("Wed", 3.9 * 3600, False),
          ("Thu", 4.6 * 3600, False), ("Fri", 5.5 * 3600, False), ("Sat", 2.4 * 3600, False),
          ("Sun", 4 * 3600 + 12 * 60, True)],
    date_str="Sunday, November 22", state="active", current_app="Code",
)


def save_popup(name, img):
    # popups are flattened onto a color key for transparency; turn that into real alpha
    rgb = img.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, design.POPUP_KEY))
    alpha = diff.convert("L").point(lambda v: 255 if v else 0)
    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    rgba.save(os.path.join(OUT, name))


def settings_shot():
    import screen_balance as sb
    tmp = tempfile.mkdtemp()
    sb.DATA_DIR = tmp
    sb.CONFIG_PATH = os.path.join(tmp, "config.json")
    sb.USAGE_PATH = os.path.join(tmp, "usage.json")
    app = sb.ScreenBalance()
    sb.autostart_enabled = lambda: True  # show the toggle switched on
    app._cmd_settings()
    win = app._settings_win
    win.update()
    win.after(600)
    win.update()
    x, y = win.winfo_rootx(), win.winfo_rooty()
    ImageGrab.grab((x, y, x + win.winfo_width(), y + win.winfo_height()), all_screens=True) \
        .save(os.path.join(OUT, "settings.png"))
    app.root.destroy()


def main():
    os.makedirs(OUT, exist_ok=True)
    design.render_dashboard(DEMO, SCALE).save(os.path.join(OUT, "dashboard.png"))
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

    icons = [design.render_tray(r, p) for r, p in ((0.35, False), (0.85, False), (1.1, False), (0.5, True))]
    strip = Image.new("RGBA", (64 * 4 + 24 * 3, 64), (0, 0, 0, 0))
    for i, ic in enumerate(icons):
        strip.paste(ic, (i * 88, 0), ic)
    strip.save(os.path.join(OUT, "tray-icons.png"))

    settings_shot()
    print("Saved images to", OUT)


if __name__ == "__main__":
    main()
