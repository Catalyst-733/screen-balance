"""
Draws the app icon (the same hourglass tile as the tray icon) for the builds:

    python tools/make_icon.py ico  screen_balance.ico     # icon of ScreenBalance.exe
    python tools/make_icon.py msix packaging/msix/Assets  # logos of the Store package
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import design  # noqa: E402

RATIO = 0.35  # a little sand in the bottom bulb: reads as an hourglass at any size

# Store package logos: file name -> size in pixels
MSIX_LOGOS = {
    "Square44x44Logo.png": 44,
    "Square150x150Logo.png": 150,
    "StoreLogo.png": 50,
}


def tile(size):
    return design.render_tray(RATIO, paused=False, dark_taskbar=True, size=size)


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "ico"
    if kind == "ico":
        out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "screen_balance.ico")
        tile(256).save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        print(f"Wrote {out}")
    elif kind == "msix":
        out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "packaging", "msix", "Assets")
        os.makedirs(out, exist_ok=True)
        for name, size in MSIX_LOGOS.items():
            tile(size).save(os.path.join(out, name))
            print(f"Wrote {os.path.join(out, name)}")
    else:
        sys.exit(f"unknown kind {kind!r}: use ico or msix")


if __name__ == "__main__":
    main()
