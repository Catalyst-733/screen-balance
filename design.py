"""
Rendering for Screen Balance, in a Notion-inspired style.

Light pages, warm dark-gray text, thin borders, emoji icons, gray callouts and
soft colored tags. Everything is drawn with Pillow at 2x the screen resolution
and downsampled so text, rounded corners and emoji stay crisp.
"""

import math
import os

from PIL import Image, ImageDraw, ImageFont

# Notion-like palettes
LIGHT = {
    "dark": False,
    "bg": (255, 255, 255),        # page
    "card": (247, 247, 245),      # sidebar / soft panel
    "card_hi": (241, 241, 239),   # callout, hover, stepper
    "track": (237, 237, 235),     # empty progress
    "border": (233, 233, 231),
    "border_hi": (217, 217, 214),
    "text": (55, 53, 47),
    "muted": (120, 119, 116),
    "dim": (165, 164, 161),
    "a1": (35, 131, 226),         # Notion blue
    "a1_hover": (0, 117, 222),
    "good": (68, 131, 97),
    "warn": (217, 115, 13),
    "danger": (212, 76, 71),
    "bar_other": (215, 214, 211),  # past days in charts
    "bar_over": (235, 160, 156),   # past days over the limit
    "bar_hour": (182, 206, 232),   # hourly bars
    "key": (250, 251, 252),        # transparent color key for popups (never drawn)
}
DARK = {
    "dark": True,
    "bg": (25, 25, 25),
    "card": (32, 32, 32),
    "card_hi": (40, 40, 40),
    "track": (47, 47, 47),
    "border": (47, 47, 47),
    "border_hi": (64, 64, 64),
    "text": (213, 213, 211),
    "muted": (155, 155, 152),
    "dim": (110, 110, 108),
    "a1": (35, 131, 226),
    "a1_hover": (56, 146, 235),
    "good": (82, 158, 114),
    "warn": (219, 132, 62),
    "danger": (223, 84, 82),
    "bar_other": (72, 72, 72),
    "bar_over": (122, 62, 58),
    "bar_hour": (42, 82, 128),
    "key": (24, 26, 28),
}

# (background, text) pairs, like Notion's select tags
LIGHT_TAGS = {
    "gray": ((241, 241, 239), (120, 119, 116)),
    "blue": ((231, 243, 248), (51, 126, 169)),
    "green": ((237, 243, 236), (68, 131, 97)),
    "orange": ((251, 236, 221), (217, 115, 13)),
    "yellow": ((251, 243, 219), (203, 145, 47)),
    "red": ((253, 235, 236), (212, 76, 71)),
    "purple": ((246, 243, 249), (144, 101, 176)),
    "pink": ((250, 241, 245), (193, 76, 138)),
}
DARK_TAGS = {
    "gray": ((55, 55, 55), (175, 175, 172)),
    "blue": ((31, 57, 88), (92, 164, 214)),
    "green": ((36, 66, 49), (96, 172, 128)),
    "orange": ((92, 55, 27), (222, 144, 86)),
    "yellow": ((86, 66, 30), (214, 168, 84)),
    "red": ((92, 42, 40), (232, 110, 106)),
    "purple": ((60, 42, 82), (168, 128, 222)),
    "pink": ((82, 38, 62), (226, 100, 168)),
}
APP_TAGS = ["blue", "purple", "green", "orange", "pink", "yellow"]

# the live palette; set_theme() swaps its contents in place so every module sees the change
THEME = dict(LIGHT)
TAGS = dict(LIGHT_TAGS)


def set_theme(dark: bool) -> None:
    # update() (never clear()) so a background render reading the palette mid-switch
    # always finds every key; both palettes have exactly the same keys
    THEME.update(DARK if dark else LIGHT)
    TAGS.update(DARK_TAGS if dark else LIGHT_TAGS)


def popup_key():
    return THEME["key"]

_FONT_FILES = {
    "regular": ["segoeui.ttf"],
    "semibold": ["seguisb.ttf", "segoeuib.ttf", "segoeui.ttf"],
    "bold": ["segoeuib.ttf", "segoeui.ttf"],
    "light": ["segoeuil.ttf", "segoeui.ttf"],
    "symbol": ["seguisym.ttf", "segoeui.ttf"],
    "emoji": ["seguiemj.ttf", "segoeui.ttf"],
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


def fmt_clock(seconds):
    """Countdown style: 12:34, or 1:05:00 past an hour."""
    s = int(math.ceil(max(0, seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ordinal(n):
    """1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st, 22nd, 23rd."""
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def usage_color(ratio):
    if ratio < 0.8:
        return THEME["a1"]
    if ratio < 1:
        return THEME["warn"]
    return THEME["danger"]


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

    def hline(self, x0, x1, y, fill=None):
        self.line([(x0, y), (x1, y)], fill or THEME["border"], 1)

    def dashed(self, x0, x1, y, fill, dash=4, gap=4, width=1):
        x = x0
        while x < x1:
            self.line([(x, y), (min(x + dash, x1), y)], fill, width)
            x += dash + gap

    def text(self, xy, txt, size, color, weight="regular", anchor="la"):
        self.d.text((xy[0] * self.S, xy[1] * self.S), txt, font=font(round(size * self.S), weight),
                    fill=color, anchor=anchor)

    def emoji(self, xy, ch, size, anchor="mm"):
        self.d.text((xy[0] * self.S, xy[1] * self.S), ch, font=font(round(size * self.S), "emoji"),
                    anchor=anchor, embedded_color=True)

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

    def tag(self, x, cy, label, color, size=12):
        """Notion select-style pill. Returns its right edge."""
        bg, fg = TAGS[color]
        w = self.textlen(label, size) + 12
        self.rrect((x, cy - 10, x + w, cy + 10), 4, fill=bg)
        self.text((x + 6, cy), label, size, fg, anchor="lm")
        return x + w

    def bar(self, x0, x1, cy, frac, color, h=6):
        self.rrect((x0, cy - h / 2, x1, cy + h / 2), h / 2, fill=THEME["track"])
        if frac > 0:
            self.rrect((x0, cy - h / 2, x0 + max(h, (x1 - x0) * min(frac, 1)), cy + h / 2), h / 2, fill=color)

    def button(self, box, label, primary, hover, icon=None, size=13):
        T = THEME
        x0, y0, x1, y1 = box
        if primary:
            self.rrect(box, 6, fill=T["a1_hover"] if hover else T["a1"])
            color = (255, 255, 255)
        else:
            self.rrect(box, 6, fill=T["card_hi"] if hover else T["bg"], outline=T["border_hi"])
            color = T["text"]
        cy = (y0 + y1) / 2
        if icon:
            tw = self.textlen(label, size, "semibold")
            start = (x0 + x1) / 2 - (tw + size + 8) / 2
            self.emoji((start + size / 2, cy), icon, size)
            self.text((start + size + 8, cy), label, size, color, "semibold", "lm")
        else:
            self.text(((x0 + x1) / 2, cy), label, size, color, "semibold", "mm")

    @staticmethod
    def button_width(p, label, size=13, icon=False):
        return p.textlen(label, size, "semibold") + 28 + (size + 8 if icon else 0)

    def result(self, out_scale, w=None, h=None):
        size = (round((w or self.w) * out_scale), round((h or self.h) * out_scale))
        return self.img.resize(size, Image.LANCZOS)


# --------------------------------------------------------------------------- #
# Dashboard (a Notion-style page)
# --------------------------------------------------------------------------- #
DASH_W, DASH_H = 780, 510
L, LR, R0, R1 = 44, 372, 420, 736


def render_dashboard(data, out_scale, hover=None, tab="overview"):
    """A Notion-style page with two views (tabs).

    data keys: total, limit, session, break_in, apps[(name, secs)], date_str, state, current_app,
    break_len_min, breaks, break_sec, longest, first, hours[24],
    week[dict(label, total, breaks, break_sec, longest, today)].
    Returns (image, hits) with hits in output pixels."""
    T = THEME
    p = Painter(DASH_W, DASH_H, out_scale * 2, T["bg"])
    hits = []

    # header
    p.emoji((L + 14, 42), "⏳", 26)
    p.text((L + 38, 42), "Screen Balance", 24, T["text"], "bold", "lm")
    p.text((R1, 44), data["date_str"], 13, T["muted"], anchor="rm")

    # view tabs
    x = L
    for tid, glyph, label in (("overview", "▦", "Overview"), ("analytics", "◫", "Analytics"),
                              ("apps", "☰", "Apps")):
        w = p.textlen(label, 13, "semibold") + 30
        box = (x - 6, 72, x - 6 + w, 98)
        active = tab == tid
        if hover == f"tab_{tid}" and not active:
            p.rrect(box, 4, fill=T["card_hi"])
        col = T["text"] if active else T["muted"]
        p.text((x + 3, 85), glyph, 12, col, "symbol", "mm")
        p.text((x + 14, 85), label, 13, col, "semibold" if active else "regular", "lm")
        if active:
            p.line([(x - 6, 104), (x - 6 + w, 104)], T["text"], 2)
        hits.append((f"tab_{tid}", box))
        x += w + 8
    p.hline(L, R1, 105)

    if tab == "analytics":
        _analytics(p, data, hover, hits)
    elif tab == "apps":
        _apps(p, data)
    else:
        _overview(p, data, hover, hits)

    hits = [(bid, tuple(v * out_scale for v in b)) for bid, b in hits]
    return p.result(out_scale), hits


def _overview(p, data, hover, hits):
    T = THEME
    total, limit = data["total"], data["limit"]
    ratio = total / limit if limit else 0

    def prop(cy, glyph, label):
        p.text((L + 7, cy), glyph, 12.5, T["dim"], "symbol", "mm")
        p.text((L + 22, cy), label, 13, T["muted"], anchor="lm")
        return L + 140

    vx = prop(134, "◉", "Status")
    state = data["state"]
    if state == "dnd":
        p.tag(vx, 134, f"Do not disturb · {data.get('dnd_label', '')}".rstrip(" ·"), "purple")
    elif state == "windows_dnd":
        p.tag(vx, 134, "Windows Do not disturb", "purple")
    elif state == "idle":
        p.tag(vx, 134, "Idle", "gray")
    elif state == "video":
        p.tag(vx, 134, p.fit(f"Watching video · {data['current_app']}", 12, "regular", LR - vx - 14), "blue")
    else:
        p.tag(vx, 134, p.fit(f"Active · {data['current_app']}", 12, "regular", LR - vx - 14), "green")

    vx = prop(166, "◷", "Break in")
    break_in = data.get("break_in")
    if state in ("dnd", "windows_dnd") or break_in is None:
        p.tag(vx, 166, "Silenced", "gray")
    elif break_in <= 0:
        p.tag(vx, 166, "Now", "orange")
    else:
        p.text((vx, 166), fmt_clock(break_in), 13.5, T["warn"] if break_in <= 60 else T["text"], "semibold", "lm")

    vx = prop(198, "↺", "Since break")
    p.text((vx, 198), fmt_dur(data["session"]), 13.5, T["text"], anchor="lm")

    vx = prop(230, "☕", "Breaks today")
    n = data.get("breaks", 0)
    end = p.tag(vx, 230, f"{n} break{'s' if n != 1 else ''}", "blue" if n else "gray")
    if data.get("break_sec", 0) >= 60:
        p.text((end + 8, 230), f"{fmt_dur(data['break_sec'])} total", 12.5, T["muted"], anchor="lm")

    vx = prop(262, "◔", "Left today")
    if not limit:
        p.tag(vx, 262, "No limit", "gray")
    elif total <= limit:
        p.text((vx, 262), fmt_dur(limit - total), 13.5, T["text"], anchor="lm")
    else:
        p.tag(vx, 262, f"Over by {fmt_dur(total - limit)}", "red")

    # action buttons: timed break, open break, do not disturb
    dnd_on = state == "dnd"
    buttons = [
        ("break_now", "☕", f"{data.get('break_len_min', 5):g} min break"),
        ("open_break", "🧘", "Open break"),
        ("dnd", "🌙", "DND on" if dnd_on else "DND"),
    ]
    size = 12.5
    while True:
        widths = [p.textlen(lbl, size, "semibold") + size + 8 + 22 for _, _, lbl in buttons]
        if sum(widths) + 8 * (len(buttons) - 1) <= LR - L or size <= 10:
            break
        size -= 0.5
    x0 = L
    for (bid, icon, label), w in zip(buttons, widths):
        box = (x0, 284, x0 + w, 316)
        if bid == "dnd" and dnd_on:
            bg, fg = TAGS["purple"]
            p.rrect(box, 6, fill=lerp(bg, fg, 0.12) if hover == "dnd" else bg)
            tw = p.textlen(label, size, "semibold")
            start = (box[0] + box[2]) / 2 - (tw + size + 8) / 2
            p.emoji((start + size / 2, 300), icon, size)
            p.text((start + size + 8, 300), label, size, fg, "semibold", "lm")
        else:
            p.button(box, label, False, hover == bid, icon=icon, size=size)
        hits.append((bid, box))
        x0 = box[2] + 8

    p.hline(L, LR, 334)
    p.text((L, 358), "Today", 16, T["text"], "semibold", "lm")
    big = fmt_dur(total)
    p.text((L, 394), big, 32, T["text"], "bold", "lm")
    bigw = p.textlen(big, 32, "bold")
    p.text((L + bigw + 10, 399), f"of {fmt_dur(limit)}" if limit else "today", 14, T["muted"], anchor="lm")
    p.bar(L, LR - 48, 426, ratio if limit else 0, usage_color(ratio), h=6)
    p.text((LR, 426), f"{round(ratio * 100)}%" if limit else "—", 12, T["muted"], anchor="rm")

    p.rrect((L, 446, LR, 482), 6, fill=T["card_hi"])
    p.emoji((L + 18, 464), "💡", 15)
    tip = f"Being away {data.get('break_len_min', 5):g} min counts as a break."
    p.text((L + 36, 464), p.fit(tip, 12.5, "regular", LR - L - 46), 12.5, T["text"], anchor="lm")

    # top apps
    p.text((R0, 134), "Top apps", 16, T["text"], "semibold", "lm")
    p.text((R1, 134), "Today", 12, T["dim"], anchor="rm")
    p.hline(R0, R1, 152)
    apps = data["apps"][:5]
    top = max(apps[0][1], 1e-9) if apps else 1
    for i in range(5):
        y0 = 152 + i * 34
        cy = y0 + 17
        if i < len(apps):
            name, secs = apps[i]
            tfg = TAGS[APP_TAGS[i % len(APP_TAGS)]][1]
            p.rrect((R0, cy - 5, R0 + 10, cy + 5), 2.5, fill=tfg)
            p.text((R0 + 20, cy), p.fit(name, 13, "regular", 120), 13, T["text"], anchor="lm")
            p.bar(R0 + 150, R1 - 70, cy, secs / top, tfg, h=6)
            p.text((R1, cy), fmt_dur(secs), 12.5, T["muted"], anchor="rm")
        elif i == 0:
            p.text((R0 + 2, cy), "No activity yet", 13, T["dim"], anchor="lm")
        p.hline(R0, R1, y0 + 34)

    # last 7 days chart
    week = data["week"]
    p.text((R0, 358), "Last 7 days", 16, T["text"], "semibold", "lm")
    vals = [d["total"] for d in week]
    active_days = [v for v in vals if v > 0]
    avg = sum(active_days) / len(active_days) if active_days else 0
    p.text((R1, 358), f"Avg {fmt_dur(avg)}", 12, T["dim"], anchor="rm")
    top_y, base = 384, 470
    peak = max(max(vals) if vals else 0, limit, 3600) * 1.12
    colw = (R1 - R0) / 7
    p.hline(R0, R1, base)
    if limit:
        ly = base - (base - top_y) * limit / peak
        p.dashed(R0, R1, ly, lerp(T["danger"], T["bg"], 0.45))
    for i, d in enumerate(week):
        cx = R0 + colw * (i + 0.5)
        v = d["total"]
        if v > 0:
            h = max(3, (base - top_y) * v / peak)
            if limit and v > limit:
                col = T["danger"] if d["today"] else T["bar_over"]
            else:
                col = T["a1"] if d["today"] else T["bar_other"]
            p.rrect((cx - 12, base - h, cx + 12, base - 1), 3, fill=col)
        p.text((cx, base + 15), d["label"], 12, T["text"] if d["today"] else T["muted"],
               "semibold" if d["today"] else "regular", "mm")


# apps that are the app itself or the desktop, rather than something you "use"
_SYSTEM_APPS = {"Screen Balance", "Lock screen", "Unknown"}


def _apps(p, data):
    """A Notion-style table: each app's time on each of the last 7 days, its average per
    day and its week total, plus an "All apps" row with the daily totals."""
    T = THEME
    week = data["week"]
    totals = {}
    for d in week:
        for name, secs in (d.get("apps") or {}).items():
            if name not in _SYSTEM_APPS:
                totals[name] = totals.get(name, 0.0) + secs
    ranked = sorted(totals, key=totals.get, reverse=True)
    shown = ranked[:9]
    rest = ranked[9:]
    active_days = max(1, sum(1 for d in week if d["total"] > 0))

    def per_day(name):
        return [(d.get("apps") or {}).get(name, 0.0) for d in week]

    def other_per_day():
        return [sum((d.get("apps") or {}).get(n, 0.0) for n in rest) for d in week]

    rows = [(name, per_day(name)) for name in shown]
    if rest:
        rows.append((f"Other ({len(rest)})", other_per_day()))

    # columns
    name_x0, name_w = L, 136
    day_x0 = name_x0 + name_w
    day_w = 56
    avg_x1 = day_x0 + 7 * day_w + 78
    tot_x1 = R1

    p.text((L, 126), "App usage", 16, T["text"], "semibold", "lm")
    used = sum(1 for d in week if d["total"] > 0)
    note = f"Last 7 days · average over {used} day{'s' if used != 1 else ''} used" if used else "Last 7 days"
    p.text((R1, 126), note, 12, T["dim"], anchor="rm")
    hy = 156
    p.text((name_x0, hy), "App", 12, T["muted"], anchor="lm")
    for i, d in enumerate(week):
        cx = day_x0 + day_w * (i + 0.5)
        p.text((cx, hy), d["label"], 12, T["text"] if d["today"] else T["muted"],
               "semibold" if d["today"] else "regular", "mm")
    p.text((avg_x1, hy), "Avg / day", 12, T["muted"], anchor="rm")
    p.text((tot_x1, hy), "Week", 12, T["muted"], anchor="rm")
    p.hline(L, R1, hy + 12)

    if not rows:
        p.text(((L + R1) / 2, 300), "No app usage recorded in the last 7 days yet", 13, T["dim"], anchor="mm")
        return

    peak = max(max(v) for _, v in rows) or 1
    row_h = 29
    y = hy + 12
    for idx, (name, vals) in enumerate(rows):
        cy = y + row_h / 2
        is_other = name.startswith("Other (")
        if not is_other:
            tfg = TAGS[APP_TAGS[idx % len(APP_TAGS)]][1]
            p.rrect((name_x0, cy - 4.5, name_x0 + 9, cy + 4.5), 2.5, fill=tfg)
        p.text((name_x0 + (16 if not is_other else 0), cy), p.fit(name, 12.5, "regular", name_w - 22), 12.5,
               T["muted"] if is_other else T["text"], anchor="lm")
        for i, v in enumerate(vals):
            cx = day_x0 + day_w * (i + 0.5)
            if v >= 1:
                # soft heat shading: busier days stand out at a glance
                shade = lerp(T["bg"], T["a1"], 0.10 + 0.32 * (v / peak))
                p.rrect((cx - day_w / 2 + 3, cy - 11, cx + day_w / 2 - 3, cy + 11), 4, fill=shade)
                p.text((cx, cy), fmt_short(v), 11.5, T["text"], anchor="mm")
            else:
                p.text((cx, cy), "—", 11.5, T["dim"], anchor="mm")
        total = sum(vals)
        p.text((avg_x1, cy), fmt_short(total / active_days, long=True), 12, T["text"], anchor="rm")
        p.text((tot_x1, cy), fmt_short(total, long=True), 12, T["muted"], anchor="rm")
        y += row_h
        p.hline(L, R1, y)

    # all apps: the daily totals, the overall average and the week
    cy = y + row_h / 2 + 2
    p.text((name_x0, cy), "All apps", 12.5, T["text"], "semibold", "lm")
    for i, d in enumerate(week):
        cx = day_x0 + day_w * (i + 0.5)
        p.text((cx, cy), fmt_short(d["total"]) if d["total"] >= 1 else "—", 11.5,
               T["text"] if d["total"] >= 1 else T["dim"], "semibold", "mm")
    week_total = sum(d["total"] for d in week)
    p.text((avg_x1, cy), fmt_dur(week_total / active_days), 12, T["text"], "semibold", "rm")
    p.text((tot_x1, cy), fmt_dur(week_total), 12, T["text"], "semibold", "rm")


def fmt_short(seconds, long=False):
    """Compact time for table cells: <1m, 45m, 1h05 (or 1h 05m with long=True)."""
    if 0 < seconds < 60:
        return "<1m"
    if long:
        return fmt_dur(seconds)
    m = int(max(0, seconds) // 60)
    h, m = divmod(m, 60)
    if not h:
        return f"{m}m"
    return f"{h}h{m:02d}" if m else f"{h}h"


def _analytics(p, data, hover=None, hits=None):
    T = THEME
    week = data["week"]
    vals = [d["total"] for d in week]
    active_days = [v for v in vals if v > 0]
    avg = sum(active_days) / len(active_days) if active_days else 0
    n = data.get("breaks", 0)

    # stat cards
    cards = [
        ("Screen time", fmt_dur(data["total"]),
         (f"incl. {fmt_dur(data['manual_today'])} added" if data.get("manual_today", 0) >= 60
          else f"Active since {data['first']}" if data.get("first") else "No activity yet")),
        ("Breaks", str(n), f"{fmt_dur(data.get('break_sec', 0))} in total" if n else "None yet today"),
        ("Longest stretch", fmt_dur(data.get("longest", 0)), "Without a break"),
        ("Daily average", fmt_dur(avg), "Last 7 days"),
    ]
    gap = 12
    cw = (R1 - L - gap * 3) / 4
    for i, (label, value, sub) in enumerate(cards):
        x0 = L + i * (cw + gap)
        p.rrect((x0, 122, x0 + cw, 194), 6, fill=T["bg"], outline=T["border_hi"])
        p.text((x0 + 14, 138), label, 12, T["muted"], anchor="lm")
        p.text((x0 + 14, 162), value, 21, T["text"], "bold", "lm")
        p.text((x0 + 14, 182), p.fit(sub, 11.5, "regular", cw - 24), 11.5, T["dim"], anchor="lm")

    # activity by hour
    p.text((L, 224), "Activity by hour", 16, T["text"], "semibold", "lm")
    p.text((LR, 224), "Today", 12, T["dim"], anchor="rm")
    hours = data.get("hours") or [0] * 24
    top_y, base = 248, 398
    peak = max(max(hours), 600)
    colw = (LR - L) / 24
    cur = data.get("hour_now", -1)
    p.hline(L, LR, base)
    for h, v in enumerate(hours):
        cx = L + colw * (h + 0.5)
        if v > 0:
            bh = max(2, (base - top_y) * v / peak)
            col = T["a1"] if h == cur else T["bar_hour"]
            p.rrect((cx - colw * 0.32, base - bh, cx + colw * 0.32, base - 1), 2, fill=col)
        if h % 6 == 0:
            p.text((cx, base + 14), ("12a", "6a", "12p", "6p")[h // 6], 11, T["muted"], anchor="mm")
    busiest = max(range(24), key=lambda i: hours[i])
    p.rrect((L, 434, LR, 482), 6, fill=T["card_hi"])
    p.emoji((L + 18, 458), "📊", 15)
    if hours[busiest] > 0:
        b12 = f"{(busiest % 12) or 12}{'am' if busiest < 12 else 'pm'}"
        if n:
            per = data["total"] / (n + 1)
            line1 = f"Busiest hour: {b12} ({fmt_dur(hours[busiest])})."
            line2 = f"About one break every {fmt_dur(per)}."
        else:
            line1 = f"Busiest hour: {b12} ({fmt_dur(hours[busiest])})."
            line2 = "No breaks yet today — time for one?"
    else:
        line1, line2 = "Your hourly activity shows up here", "as you use your PC today."
    p.text((L + 36, 449), line1, 12.5, T["text"], anchor="lm")
    p.text((L + 36, 467), line2, 12.5, T["muted"], anchor="lm")

    # week table
    p.text((R0, 224), "Last 7 days", 16, T["text"], "semibold", "lm")
    # add time you spent on screen that the app didn't see
    label = "Add time"
    w = Painter.button_width(p, label, 12, icon=True)
    box = (R1 - w, 210, R1, 238)
    p.button(box, label, False, hover == "add_time", icon="➕", size=12)
    if hits is not None:
        hits.append(("add_time", box))
    cols = [("Day", R0, "lm"), ("Screen time", R0 + 52, "lm"), ("Breaks", R0 + 222, "mm"), ("Break time", R1, "rm")]
    for label, x, anchor in cols:
        p.text((x, 252), label, 12, T["muted"], anchor=anchor)
    p.hline(R0, R1, 264)
    peak = max(max(vals) if vals else 0, 1)
    limit = data["limit"]
    for i, d in enumerate(week):
        y0 = 264 + i * 31
        cy = y0 + 15.5
        bold = "semibold" if d["today"] else "regular"
        p.text((R0, cy), d["label"], 12.5, T["text"], bold, anchor="lm")
        if d["total"] > 0:
            p.text((R0 + 52, cy), fmt_dur(d["total"]), 12.5, T["text"], bold, anchor="lm")
            over = limit and d["total"] > limit
            p.bar(R0 + 112, R0 + 180, cy, d["total"] / peak, T["danger"] if over else T["a1"], h=5)
            p.text((R0 + 222, cy), str(d["breaks"]), 12.5, T["text"], anchor="mm")
            p.text((R1, cy), fmt_dur(d["break_sec"]) if d["break_sec"] else "—", 12.5, T["muted"], anchor="rm")
        else:
            p.text((R0 + 52, cy), "—", 12.5, T["dim"], anchor="lm")
        p.hline(R0, R1, y0 + 31)


# --------------------------------------------------------------------------- #
# Popup (a Notion-style toast)
# --------------------------------------------------------------------------- #
POPUP_W = 380
_POPUP_ICONS = {"break": "☕", "warn": "⚠️", "limit": "⏰"}


def render_popup(kind, title, message, buttons, hover, out_scale, key=None):
    """buttons: list of (id, label, primary). Returns (image, hits) with hits in output pixels."""
    T = THEME
    key = tuple(key or T["key"])
    p = Painter(POPUP_W, 400, out_scale * 2, key)
    lines = p.wrap(message, 12.5, POPUP_W - 56 - 22)
    msg_top = 56
    btn_y0 = msg_top + len(lines) * 19 + 12
    btn_y1 = btn_y0 + 30
    H = btn_y1 + 16

    p.rrect((0, 0, POPUP_W - 1, H - 1), 8, fill=T["bg"], outline=T["border_hi"])
    p.emoji((30, 33), _POPUP_ICONS[kind], 20)
    p.text((56, 33), title, 14.5, T["text"], "semibold", "lm")
    for i, ln in enumerate(lines):
        p.text((56, msg_top + i * 19 + 8), ln, 12.5, T["muted"], anchor="lm")

    hits = []
    close_c = (POPUP_W - 22, 22)
    if hover == "close":
        p.rrect((close_c[0] - 11, close_c[1] - 11, close_c[0] + 11, close_c[1] + 11), 4, fill=T["card_hi"])
    for dx, dy in ((1, 1), (1, -1)):
        p.line([(close_c[0] - 4.5 * dx, close_c[1] - 4.5 * dy), (close_c[0] + 4.5 * dx, close_c[1] + 4.5 * dy)],
               T["muted"], 1.4)
    hits.append(("close", (close_c[0] - 13, close_c[1] - 13, close_c[0] + 13, close_c[1] + 13)))

    x = POPUP_W - 18
    for bid, label, primary in reversed(buttons):
        w = Painter.button_width(p, label, 12.5)
        box = (x - w, btn_y0, x, btn_y1)
        p.button(box, label, primary, hover == bid, size=12.5)
        hits.append((bid, box))
        x -= w + 8

    img = p.img.crop((0, 0, round(POPUP_W * p.S), round(H * p.S)))
    out = img.resize((round(POPUP_W * out_scale), round(H * out_scale)), Image.LANCZOS)
    flat = Image.new("RGB", out.size, key)
    flat.paste(out, (0, 0), out)
    hits = [(bid, tuple(v * out_scale for v in box)) for bid, box in hits]
    return flat, hits


# --------------------------------------------------------------------------- #
# Full-screen break timer
# --------------------------------------------------------------------------- #
BREAK_W, BREAK_H = 560, 560
BREAK_TIPS = [
    "Stand up and stretch your back and shoulders.",
    "Look at something 6 metres away to rest your eyes.",
    "Drink a glass of water.",
    "Roll your neck slowly, both directions.",
    "Take a few slow, deep breaths.",
    "Walk around for a minute.",
    "Shake out your hands and wrists.",
]


def _tip_callout(p, tip):
    T = THEME
    lines = p.wrap(tip, 13.5, BREAK_W - 150)[:2]
    ch = 24 + 20 * len(lines)
    top = 380
    p.rrect((60, top, BREAK_W - 60, top + ch), 6, fill=T["card_hi"])
    p.emoji((86, top + 22), "💡", 16)
    for i, ln in enumerate(lines):
        p.text((108, top + 22 + i * 20), ln, 13.5, T["text"], anchor="lm")


def render_open_break(elapsed, suggested, tip, hover, out_scale):
    """Open (untimed) break: a stopwatch that counts up until you click "I'm back"."""
    T = THEME
    p = Painter(BREAK_W, BREAK_H, out_scale * 2, T["bg"])
    cx = BREAK_W / 2
    p.emoji((cx, 62), "🧘", 54)
    p.text((cx, 138), "Open break", 30, T["text"], "bold", "mm")
    p.text((cx, 172), "Take as long as you need · come back when you're ready", 14, T["muted"], anchor="mm")
    p.text((cx, 254), fmt_clock(elapsed), 80, T["text"], "light", "mm")
    if suggested and elapsed >= suggested:
        label = f"{fmt_dur(suggested)} reached · good break"
        w = p.textlen(label, 12.5) + 16
        bg, fg = TAGS["green"]
        p.rrect((cx - w / 2, 312, cx + w / 2, 334), 4, fill=bg)
        p.text((cx, 323), label, 12.5, fg, anchor="mm")
    else:
        p.text((cx, 323), f"Away for {fmt_clock(elapsed)}"
               + (f" · {fmt_dur(suggested)} recommended" if suggested else ""), 12.5, T["muted"], anchor="mm")
    _tip_callout(p, tip)
    label = "I'm back"
    w = Painter.button_width(p, label, 13.5) + 20
    box = (cx - w / 2, 486, cx + w / 2, 522)
    p.button(box, label, True, hover == "back", size=13.5)
    return p.result(out_scale), [("back", tuple(v * out_scale for v in box))]


def render_break(remaining, total, tip, done, hover, out_scale, breaks_today=None):
    """Centre panel of the break overlay, drawn on the page background.
    Returns (image, hits) with hits in output pixels relative to the image."""
    T = THEME
    p = Painter(BREAK_W, BREAK_H, out_scale * 2, T["bg"])
    cx = BREAK_W / 2

    p.emoji((cx, 62), "🌿" if done else "☕", 54)
    p.text((cx, 138), "Break complete" if done else "Break time", 30, T["text"], "bold", "mm")
    if done:
        sub = "Welcome back"
        if breaks_today:
            nth = ordinal(breaks_today)
            sub = f"Welcome back · your {nth} break today"
    else:
        sub = "Step away from the screen for a bit"
    p.text((cx, 172), sub, 14, T["muted"], anchor="mm")

    if done:
        p.text((cx, 254), "Nice.", 72, T["text"], "bold", "mm")
        p.bar(130, BREAK_W - 130, 322, 1, T["good"], h=6)
        p.text((cx, 344), f"{fmt_dur(total)} break done", 12.5, T["muted"], anchor="mm")
    else:
        p.text((cx, 254), fmt_clock(remaining), 80, T["text"], "light", "mm")
        p.bar(130, BREAK_W - 130, 322, 1 - (remaining / total if total else 0), T["a1"], h=6)
        p.text((cx, 344), f"{fmt_clock(total - remaining)} of {fmt_clock(total)}", 12.5, T["muted"], anchor="mm")

    _tip_callout(p, tip)

    if done:
        bid, label, primary = "finish", "Back to work", True
    else:
        bid, label, primary = "skip", "End break early", False
    w = Painter.button_width(p, label, 13.5)
    box = (cx - w / 2, 486, cx + w / 2, 522)
    p.button(box, label, primary, hover == bid, size=13.5)
    hits = [(bid, tuple(v * out_scale for v in box))]
    return p.result(out_scale), hits


# --------------------------------------------------------------------------- #
# Tray icon
# --------------------------------------------------------------------------- #
def render_tray(ratio, paused=False, dark_taskbar=True, size=64):
    """Notion-style app tile: a rounded square with a minimal hourglass.

    The sand shows the day: it drains from the top bulb into the bottom one as you
    approach your daily limit (blue -> orange -> red). Do not disturb shows a moon.
    The tile is light or dark to sit naturally on the taskbar."""
    S = 8  # draw big, then shrink, so it stays crisp at 16-32 px
    img = Image.new("RGBA", (64 * S, 64 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def b(*v):
        return tuple(x * S for x in v)

    if dark_taskbar:
        tile, edge, ink = (47, 47, 47, 255), (84, 84, 84, 255), (236, 236, 234, 255)
        palette = DARK_TAGS
    else:
        tile, edge, ink = (255, 255, 255, 255), (55, 53, 47, 255), (55, 53, 47, 255)
        palette = LIGHT_TAGS
    d.rounded_rectangle(b(2, 2, 62, 62), radius=15 * S, fill=tile, outline=edge, width=3 * S)

    if paused:
        moon = palette["purple"][1] + (255,)
        d.ellipse(b(15, 15, 49, 49), fill=moon)
        d.ellipse(b(25, 9, 55, 39), fill=tile)
        return img.resize((size, size), Image.LANCZOS)

    r = max(0.0, min(1.0, ratio))
    sand = (LIGHT if not dark_taskbar else DARK)["a1"] if ratio < 0.8 else (
        palette["orange"][1] if ratio < 1 else palette["red"][1])
    sand = tuple(sand) + (255,)

    top, mid, bot = 18, 32, 46       # bulb geometry
    left, right, cx = 20, 44, 32
    half = (right - left) / 2
    # sand left in the top bulb (an inverted triangle that shrinks as the day goes on)
    if r < 1:
        yt = top + (mid - top) * r
        w = half * (mid - yt) / (mid - top)
        d.polygon([b(cx - w, yt), b(cx + w, yt), b(cx, mid)], fill=sand)
    # sand piled in the bottom bulb (fills from the base up)
    if r > 0:
        yb = bot - (bot - mid) * r
        w = half * (yb - mid) / (bot - mid)
        d.polygon([b(cx - w, yb), b(cx + w, yb), b(right, bot), b(left, bot)], fill=sand)
    # glass outline and caps
    lw = int(3.2 * S)
    d.line([b(left, top), b(cx, mid), b(left, bot)], fill=ink, width=lw, joint="curve")
    d.line([b(right, top), b(cx, mid), b(right, bot)], fill=ink, width=lw, joint="curve")
    d.rounded_rectangle(b(15, 12, 49, 17), radius=2.5 * S, fill=ink)
    d.rounded_rectangle(b(15, 47, 49, 52), radius=2.5 * S, fill=ink)
    return img.resize((size, size), Image.LANCZOS)
