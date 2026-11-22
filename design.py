"""
Rendering for Screen Balance.

Everything visual (dashboard, popups, tray icon) is drawn with Pillow at 2x the
screen resolution and downsampled, which gives smooth anti-aliased rings,
gradients and glows that plain Tk widgets can't do.
"""

import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

THEME = {
    "bg": (12, 14, 21),
    "card": (21, 24, 34),
    "card_hi": (31, 35, 49),
    "track": (27, 31, 44),
    "border": (36, 41, 57),
    "text": (236, 238, 244),
    "muted": (138, 144, 164),
    "dim": (92, 98, 118),
    "a1": (34, 211, 238),     # cyan
    "a2": (167, 139, 250),    # violet
    "good": (52, 211, 153),
    "warn": (251, 191, 36),
    "danger": (248, 113, 113),
}
APP_COLORS = [THEME["a1"], THEME["a2"], (96, 165, 250), (244, 114, 182), THEME["good"], THEME["warn"]]
POPUP_KEY = (1, 2, 3)  # transparent color key for borderless popups

_FONT_FILES = {
    "regular": ["segoeui.ttf"],
    "semibold": ["seguisb.ttf", "segoeuib.ttf", "segoeui.ttf"],
    "bold": ["segoeuib.ttf", "segoeui.ttf"],
    "light": ["segoeuil.ttf", "segoeui.ttf"],
}
_FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
_font_cache = {}


def font(size, weight="regular"):
    key = (size, weight)
    if key not in _font_cache:
        f = None
        for name in _FONT_FILES[weight]:
            try:
                f = ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
                break
            except OSError:
                continue
        _font_cache[key] = f or ImageFont.load_default(size)
    return _font_cache[key]


def hexc(rgb):
    return "#%02x%02x%02x" % tuple(rgb[:3])


def lerp(c1, c2, t):
    t = max(0.0, min(1.0, t))
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def fmt_dur(seconds):
    m = int(max(0, seconds) // 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def status_colors(ratio):
    """Gradient pair for a usage ratio (0..1+)."""
    if ratio < 0.8:
        return THEME["a1"], THEME["a2"]
    if ratio < 1:
        return THEME["a2"], THEME["warn"]
    return THEME["warn"], THEME["danger"]


class Painter:
    """Draws in logical units on a supersampled RGBA canvas."""

    def __init__(self, w, h, scale, bg):
        self.w, self.h, self.S = w, h, scale
        bg = tuple(bg) + ((255,) if len(bg) == 3 else ())
        self.img = Image.new("RGBA", (round(w * scale), round(h * scale)), bg)
        self.d = ImageDraw.Draw(self.img)

    def _b(self, box):
        return tuple(v * self.S for v in box)

    def rrect(self, box, r, fill=None, outline=None, width=1):
        x0, y0, x1, y1 = box
        r = min(r, (x1 - x0) / 2, (y1 - y0) / 2)
        self.d.rounded_rectangle(self._b(box), radius=r * self.S, fill=fill, outline=outline,
                                 width=max(1, round(width * self.S)))

    def circle(self, c, r, fill=None, outline=None, width=1):
        x, y = c
        self.d.ellipse(self._b((x - r, y - r, x + r, y + r)), fill=fill, outline=outline,
                       width=max(1, round(width * self.S)))

    def line(self, pts, fill, width=1):
        self.d.line([(x * self.S, y * self.S) for x, y in pts], fill=fill, width=max(1, round(width * self.S)))

    def dashed(self, x0, x1, y, fill, dash=5, gap=4, width=1):
        x = x0
        while x < x1:
            self.line([(x, y), (min(x + dash, x1), y)], fill, width)
            x += dash + gap

    def text(self, xy, txt, size, color, weight="regular", anchor="la"):
        self.d.text((xy[0] * self.S, xy[1] * self.S), txt, font=font(round(size * self.S), weight),
                    fill=color, anchor=anchor)

    def textlen(self, txt, size, weight="regular"):
        return font(round(size * self.S), weight).getlength(txt) / self.S

    def fit(self, txt, size, weight, max_w):
        if self.textlen(txt, size, weight) <= max_w:
            return txt
        while txt and self.textlen(txt + "…", size, weight) > max_w:
            txt = txt[:-1]
        return txt + "…"

    def wrap(self, txt, size, max_w, weight="regular"):
        lines = []
        for para in txt.split("\n"):
            cur = ""
            for word in para.split(" "):
                trial = f"{cur} {word}".strip()
                if self.textlen(trial, size, weight) <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = word
            lines.append(cur)
        return lines

    def grad_rrect(self, box, r, c1, c2, vertical=False):
        x0, y0, x1, y1 = (round(v * self.S) for v in box)
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        g = Image.linear_gradient("L")
        if not vertical:
            g = g.rotate(90)  # left -> right
        g = g.resize((w, h))
        grad = Image.composite(Image.new("RGBA", (w, h), tuple(c2) + (255,)),
                               Image.new("RGBA", (w, h), tuple(c1) + (255,)), g)
        mask = Image.new("L", (w, h), 0)
        rr = min(r * self.S, w / 2, h / 2)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=rr, fill=255)
        self.img.paste(grad, (x0, y0), mask)

    def glow_blob(self, c, r, color, alpha):
        """Large soft light, rendered small and upscaled so it's cheap."""
        k = 8
        size = int((r * 3) * self.S / k) or 1
        small = Image.new("RGBA", (size, size), tuple(color) + (0,))
        ImageDraw.Draw(small).ellipse((size / 3, size / 3, size * 2 / 3, size * 2 / 3),
                                      fill=tuple(color) + (alpha,))
        small = small.filter(ImageFilter.GaussianBlur(size / 8))
        big = small.resize((size * k, size * k), Image.BICUBIC)
        x = round(c[0] * self.S - size * k / 2)
        y = round(c[1] * self.S - size * k / 2)
        self._composite(big, x, y)

    def _composite(self, layer, x, y):
        # alpha_composite needs non-negative destinations, so crop as needed
        cx, cy = max(0, -x), max(0, -y)
        if cx or cy:
            layer = layer.crop((cx, cy, layer.width, layer.height))
        self.img.alpha_composite(layer, dest=(x + cx, y + cy))

    def ring(self, c, radius, thick, frac, c1, c2, track, glow=True):
        S = self.S
        pad = thick * 2
        size = round((radius + pad) * 2 * S)
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        o = pad * S
        box = (o, o, o + radius * 2 * S, o + radius * 2 * S)
        w = round(thick * S)
        d.arc(box, 0, 360, fill=tuple(track) + (255,), width=w)
        ox = round(c[0] * S - size / 2)
        oy = round(c[1] * S - size / 2)
        self._composite(layer, ox, oy)

        frac = max(0.0, min(1.0, frac))
        if frac <= 0.002:
            return
        prog = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(prog)
        total = 360 * frac
        steps = max(2, int(total / 1.5))
        for i in range(steps):
            a0 = -90 + total * i / steps
            a1 = -90 + total * (i + 1) / steps + 0.6
            d.arc(box, a0, min(a1, -90 + total), fill=lerp(c1, c2, i / (steps - 1)) + (255,), width=w)
        mid = (radius - thick / 2) * S
        cxl = cyl = size / 2
        for ang, col in ((-90, c1), (-90 + total, c2)):
            x = cxl + mid * math.cos(math.radians(ang))
            y = cyl + mid * math.sin(math.radians(ang))
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=tuple(col) + (255,))
        if glow:
            g = prog.filter(ImageFilter.GaussianBlur(thick * S * 0.9))
            g.putalpha(g.getchannel("A").point(lambda v: int(v * 0.55)))
            self._composite(g, ox, oy)
        self._composite(prog, ox, oy)

    def result(self, out_scale, w=None, h=None):
        size = (round((w or self.w) * out_scale), round((h or self.h) * out_scale))
        return self.img.resize(size, Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
DASH_W, DASH_H = 760, 440


def render_dashboard(data, out_scale):
    """data keys: total, limit, session, interval, apps[(name, secs)], week[(label, secs, is_today)],
    date_str, state ('active'|'idle'|'paused'), current_app"""
    T = THEME
    p = Painter(DASH_W, DASH_H, out_scale * 2, T["bg"])
    total, limit = data["total"], data["limit"]
    ratio = total / limit if limit else 0

    p.glow_blob((180, 230), 150, T["a2"], 55)
    p.glow_blob((120, 150), 110, T["a1"], 35)

    # --- header
    p.text((28, 36), "Screen Balance", 19, T["text"], "semibold", "lm")
    p.text((28, 58), data["date_str"], 11.5, T["muted"], anchor="lm")
    state = data["state"]
    if state == "paused":
        dot, label = T["warn"], "Reminders paused"
    elif state == "idle":
        dot, label = T["dim"], "Idle — not counting"
    else:
        dot, label = T["good"], f"Active · {data['current_app']}"
    label = p.fit(label, 11, "regular", 220)
    pw = p.textlen(label, 11) + 34
    p.rrect((28, 76, 28 + pw, 100), 12, fill=T["card"], outline=T["border"])
    p.circle((43, 88), 4, fill=dot)
    p.text((54, 88), label, 11, T["muted"], anchor="lm")

    # --- big ring
    c1, c2 = status_colors(ratio)
    p.ring((180, 228), 92, 15, ratio if limit else 0, c1, c2, T["track"])
    p.text((180, 196), f"{round(ratio * 100)}% of limit" if limit else "today", 11, T["dim"], anchor="mm")
    p.text((180, 230), fmt_dur(total), 34, T["text"], "semibold", "mm")
    p.text((180, 262), f"of {fmt_dur(limit)}" if limit else "no daily limit", 12, T["muted"], anchor="mm")

    # --- chips
    def chip(x0, x1, title, value, value_color, frac, fc1, fc2, sub=None):
        y0, y1 = 346, 412
        p.rrect((x0, y0, x1, y1), 14, fill=T["card"], outline=T["border"])
        p.text((x0 + 14, y0 + 17), title, 9.5, T["dim"], "semibold", "lm")
        p.text((x0 + 14, y0 + 39), value, 18, value_color, "semibold", "lm")
        if frac is not None:
            p.rrect((x0 + 14, y0 + 54, x1 - 14, y0 + 58), 2, fill=T["track"])
            if frac > 0:
                p.grad_rrect((x0 + 14, y0 + 54, x0 + 14 + (x1 - x0 - 28) * min(frac, 1), y0 + 58), 2, fc1, fc2)
        elif sub:
            p.text((x0 + 14, y0 + 57), sub, 9.5, T["dim"], anchor="lm")

    interval = data["interval"]
    sfrac = data["session"] / interval if interval else 0
    chip(28, 174, "SINCE BREAK", fmt_dur(data["session"]),
         T["warn"] if sfrac >= 1 else T["text"], sfrac,
         *((T["warn"], T["danger"]) if sfrac >= 1 else (T["a1"], T["a2"])))
    if not limit:
        chip(186, 332, "LEFT TODAY", "∞", T["text"], None, None, None, "no limit set")
    elif total <= limit:
        chip(186, 332, "LEFT TODAY", fmt_dur(limit - total), T["text"], None, None, None, "until your limit")
    else:
        chip(186, 332, "OVER LIMIT", "+" + fmt_dur(total - limit), T["danger"], None, None, None, "time to wind down")

    # --- right column
    x0, x1 = 356, 732
    # top apps
    p.rrect((x0, 24, x1, 232), 16, fill=T["card"], outline=T["border"])
    p.text((x0 + 18, 47), "Top apps", 13, T["text"], "semibold", "lm")
    p.text((x1 - 18, 47), "today", 10.5, T["dim"], anchor="rm")
    apps = data["apps"][:5]
    if not apps:
        p.text(((x0 + x1) / 2, 140), "No activity yet", 12, T["dim"], anchor="mm")
    top = apps[0][1] if apps else 1
    for i, (name, secs) in enumerate(apps):
        ry = 78 + i * 31
        col = APP_COLORS[i % len(APP_COLORS)]
        p.circle((x0 + 22, ry), 3.5, fill=col)
        t = fmt_dur(secs)
        tw = p.textlen(t, 11)
        p.text((x0 + 32, ry), p.fit(name, 12, "regular", x1 - x0 - 70 - tw), 12, T["text"], anchor="lm")
        p.text((x1 - 18, ry), t, 11, T["muted"], anchor="rm")
        bx0, bx1 = x0 + 18, x1 - 18
        p.rrect((bx0, ry + 10, bx1, ry + 14), 2, fill=T["track"])
        p.grad_rrect((bx0, ry + 10, bx0 + max(6, (bx1 - bx0) * secs / top), ry + 14), 2,
                     lerp(col, T["bg"], 0.45), col)

    # week
    p.rrect((x0, 244, x1, 416), 16, fill=T["card"], outline=T["border"])
    p.text((x0 + 18, 267), "Last 7 days", 13, T["text"], "semibold", "lm")
    vals = [v for _, v, _ in data["week"]]
    active_days = [v for v in vals if v > 0]
    avg = sum(active_days) / len(active_days) if active_days else 0
    p.text((x1 - 18, 267), f"avg {fmt_dur(avg)}", 10.5, T["dim"], anchor="rm")
    top_y, base = 296, 380
    peak = max(max(vals) if vals else 0, limit, 3600) * 1.08
    inner0, inner1 = x0 + 18, x1 - 18
    colw = (inner1 - inner0) / 7
    for i in range(7):
        cx = inner0 + colw * (i + 0.5)
        p.rrect((cx - 10, top_y, cx + 10, base), 7, fill=T["track"])
    if limit:
        ly = base - (base - top_y) * limit / peak
        p.dashed(inner0, inner1, ly, lerp(T["danger"], T["card"], 0.35), 4, 4, 1)
    for i, (lbl, v, is_today) in enumerate(data["week"]):
        cx = inner0 + colw * (i + 0.5)
        if v > 0:
            h = max(8, (base - top_y) * v / peak)
            if limit and v > limit:
                g1, g2 = T["danger"], T["warn"]
            elif is_today:
                g1, g2 = T["a2"], T["a1"]
            else:
                g1, g2 = (88, 96, 130), (58, 64, 88)
            p.grad_rrect((cx - 10, base - h, cx + 10, base), 7, g1, g2, vertical=True)
            if is_today:
                p.text((cx, base - h - 9), fmt_dur(v), 9.5, T["text"], "semibold", "mm")
        p.text((cx, 399), lbl, 10, T["text"] if is_today else T["dim"],
               "semibold" if is_today else "regular", "mm")

    return p.result(out_scale)


# --------------------------------------------------------------------------- #
# Popup
# --------------------------------------------------------------------------- #
POPUP_W = 384


def render_popup(kind, title, message, buttons, hover, out_scale):
    """buttons: list of (id, label, primary). Returns (image, hits) with hits in output pixels."""
    T = THEME
    p = Painter(POPUP_W, 400, out_scale * 2, POPUP_KEY)
    lines = p.wrap(message, 12, POPUP_W - 76 - 22)
    msg_top = 62
    btn_y0 = msg_top + len(lines) * 19 + 14
    btn_y1 = btn_y0 + 34
    H = btn_y1 + 18

    p.rrect((0, 0, POPUP_W - 1, H - 1), 18, fill=T["card"], outline=T["border"])
    # soft accent light in the corner (clipped to card by pasting on a mask)
    accent = {"break": (T["a1"], T["a2"]), "warn": (T["a2"], T["warn"]),
              "limit": (T["warn"], T["danger"])}[kind]
    glow = Painter(POPUP_W, H, p.S, T["card"])
    glow.glow_blob((40, 40), 70, accent[1], 70)
    mask = Image.new("L", glow.img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((p.S, p.S, glow.img.width - p.S * 2, glow.img.height - p.S * 2),
                                           radius=17 * p.S, fill=255)
    p.img.paste(glow.img, (0, 0), mask)

    # icon
    p.grad_rrect((22, 22, 58, 58), 18, accent[0], accent[1])
    white = (255, 255, 255)
    if kind == "break":
        p.rrect((33, 32, 37.5, 48), 2, fill=white)
        p.rrect((42.5, 32, 47, 48), 2, fill=white)
    elif kind == "limit":
        p.circle((40, 40), 10, outline=white, width=2.2)
        p.line([(40, 40), (40, 33.5)], white, 2.2)
        p.line([(40, 40), (44.5, 42.5)], white, 2.2)
    else:
        p.rrect((38.3, 30, 41.7, 43), 1.7, fill=white)
        p.circle((40, 48), 2, fill=white)

    p.text((76, 38), title, 15.5, T["text"], "semibold", "lm")
    for i, ln in enumerate(lines):
        p.text((76, msg_top + i * 19 + 2), ln, 12, T["muted"], anchor="lm")

    hits = []
    # close button
    close_c = (POPUP_W - 24, 24)
    if hover == "close":
        p.circle(close_c, 12, fill=T["card_hi"])
    p.line([(close_c[0] - 4.5, close_c[1] - 4.5), (close_c[0] + 4.5, close_c[1] + 4.5)], T["muted"], 1.6)
    p.line([(close_c[0] - 4.5, close_c[1] + 4.5), (close_c[0] + 4.5, close_c[1] - 4.5)], T["muted"], 1.6)
    hits.append(("close", (close_c[0] - 14, close_c[1] - 14, close_c[0] + 14, close_c[1] + 14)))

    x = POPUP_W - 22
    for bid, label, primary in reversed(buttons):
        w = p.textlen(label, 12, "semibold") + 36
        box = (x - w, btn_y0, x, btn_y1)
        hov = hover == bid
        if primary:
            g1, g2 = accent
            if hov:
                g1, g2 = lerp(g1, (255, 255, 255), 0.18), lerp(g2, (255, 255, 255), 0.18)
            p.grad_rrect(box, 17, g1, g2)
            p.text(((box[0] + box[2]) / 2, (btn_y0 + btn_y1) / 2), label, 12, T["bg"], "semibold", "mm")
        else:
            p.rrect(box, 17, fill=T["border"] if hov else T["card_hi"])
            p.text(((box[0] + box[2]) / 2, (btn_y0 + btn_y1) / 2), label, 12, T["text"], "semibold", "mm")
        hits.append((bid, box))
        x -= w + 8

    img = p.img.crop((0, 0, round(POPUP_W * p.S), round(H * p.S)))
    out = img.resize((round(POPUP_W * out_scale), round(H * out_scale)), Image.LANCZOS)
    # flatten onto the key color so the rounded corners become transparent
    flat = Image.new("RGB", out.size, POPUP_KEY)
    flat.paste(out, (0, 0), out)
    hits = [(bid, tuple(v * out_scale for v in box)) for bid, box in hits]
    return flat, hits


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #
def render_tray(ratio, paused=False):
    p = Painter(64, 64, 4, (0, 0, 0, 0))
    if paused:
        p.ring((32, 32), 28, 9, 0, THEME["dim"], THEME["dim"], (120, 126, 140), glow=False)
        p.rrect((24, 21, 29, 43), 2, fill=(160, 166, 180))
        p.rrect((35, 21, 40, 43), 2, fill=(160, 166, 180))
    else:
        c1, c2 = status_colors(ratio)
        p.ring((32, 32), 28, 9, max(ratio, 0.03), c1, c2, (80, 86, 104), glow=False)
        p.circle((32, 32), 9, fill=c2)
    return p.img.resize((64, 64), Image.LANCZOS)
