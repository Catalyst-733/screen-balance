"""
Draws the app icon (the same hourglass tile as the tray icon) into a Windows .ico file.
Used when building ScreenBalance.exe.

    python tools/make_icon.py [output.ico]
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import design  # noqa: E402


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "screen_balance.ico")
    img = design.render_tray(0.35, paused=False, dark_taskbar=True)
    img.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
