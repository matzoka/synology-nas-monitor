"""Lightweight Telegram snapshot image renderer.

The NAS package deliberately does not require a browser, Chromium, Pillow, or a
system font at runtime.  It draws a compact PNG report with a tiny built-in
ASCII bitmap font, which keeps automatic alert delivery reliable on DSM.
"""
import re
import struct
import time
import zlib
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

BG = (20, 22, 26)
PANEL = (29, 32, 38)
BORDER = (66, 71, 80)
TEXT = (235, 238, 244)
MUTED = (154, 161, 174)
BLUE = (48, 126, 240)
TEAL = (36, 192, 176)
PINK = (238, 100, 148)
ORANGE = (245, 153, 51)
GREEN = (51, 190, 111)
YELLOW = (239, 186, 42)
RED = (236, 80, 80)

# 5x7 bitmap glyphs.  A report image only needs concise, language-neutral
# metric labels; Japanese remains in the Telegram caption and web interface.
FONT = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "11100"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "?": ("01110", "10001", "00010", "00100", "00100", "00000", "00100"),
}


class Canvas:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.pixels = bytearray(BG * (width * height))

    def pixel(self, x, y, color):
        if 0 <= x < self.width and 0 <= y < self.height:
            p = (y * self.width + x) * 3
            self.pixels[p:p + 3] = bytes(color)

    def rect(self, x, y, width, height, color, outline=None):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.width, int(x + width)), min(self.height, int(y + height))
        row = bytes(color) * max(0, x1 - x0)
        for yy in range(y0, y1):
            p = (yy * self.width + x0) * 3
            self.pixels[p:p + len(row)] = row
        if outline:
            self.line(x0, y0, x1 - 1, y0, outline)
            self.line(x0, y1 - 1, x1 - 1, y1 - 1, outline)
            self.line(x0, y0, x0, y1 - 1, outline)
            self.line(x1 - 1, y0, x1 - 1, y1 - 1, outline)

    def line(self, x0, y0, x1, y1, color, width=1):
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            for ox in range(-(width // 2), width // 2 + 1):
                for oy in range(-(width // 2), width // 2 + 1):
                    self.pixel(x0 + ox, y0 + oy, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def text(self, x, y, value, color=TEXT, scale=2):
        x = int(x)
        for ch in str(value).upper():
            glyph = FONT.get(ch, FONT["?"])
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(x + gx * scale, y + gy * scale, scale, scale, color)
            x += 6 * scale
        return x

    def png(self):
        raw = b"".join(b"\x00" + self.pixels[y * self.width * 3:(y + 1) * self.width * 3]
                       for y in range(self.height))
        def chunk(name, payload):
            return (struct.pack(">I", len(payload)) + name + payload +
                    struct.pack(">I", zlib.crc32(name + payload) & 0xffffffff))
        return (b"\x89PNG\r\n\x1a\n" +
                chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _num(value, suffix="", decimals=0):
    if value is None:
        return "--"
    try:
        return (f"{float(value):.{decimals}f}" if decimals else str(int(round(float(value))))) + suffix
    except (TypeError, ValueError):
        return "--"


def _ascii(value, fallback="NAS"):
    safe = "".join(ch for ch in str(value or "") if 32 <= ord(ch) < 127)
    return (safe.strip() or fallback)[:36]


def _severity_color(severity):
    return {"NORMAL": GREEN, "WARNING": YELLOW, "CRITICAL": ORANGE, "EMERGENCY": RED}.get(severity, MUTED)


def _draw_graph(canvas, x, y, width, height, samples, series, minimum, maximum, thresholds=None):
    canvas.rect(x, y, width, height, PANEL, BORDER)
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        yy = int(y + 18 + (height - 36) * frac)
        canvas.line(x + 12, yy, x + width - 12, yy, BORDER)
    if thresholds:
        for value, color in thresholds:
            if value is None or maximum <= minimum:
                continue
            ratio = max(0.0, min(1.0, (float(value) - minimum) / (maximum - minimum)))
            yy = int(y + 18 + (height - 36) * (1.0 - ratio))
            canvas.line(x + 12, yy, x + width - 12, yy, color)
    if not samples:
        canvas.text(x + 28, y + height // 2 - 10, "NO HISTORY", MUTED, 3)
        return
    for item in series:
        points = []
        for idx, sample in enumerate(samples):
            value = item["value"](sample)
            if not isinstance(value, (int, float)):
                points.append(None)
                continue
            px = int(x + 12 + (width - 24) * idx / max(1, len(samples) - 1))
            ratio = max(0.0, min(1.0, (float(value) - minimum) / max(0.1, maximum - minimum)))
            py = int(y + 18 + (height - 36) * (1.0 - ratio))
            points.append((px, py))
        previous = None
        for point in points:
            if previous and point:
                canvas.line(previous[0], previous[1], point[0], point[1], item["color"], 2)
            previous = point


def _max_disk(sample):
    values = [v for v in (sample.get("disks") or {}).values() if isinstance(v, (int, float))]
    return max(values) if values else None


def drive_label(disk, fallback_index):
    """Return a physical-location label instead of the collection-list index.

    DSM returns one flat list containing NAS bays, expansion bays, and M.2
    slots.  The list index is not a drive number, so it must never be shown to
    the user as ``DRIVE 6`` or similar.
    """
    name = str(disk.get("name") or "")
    unit = _ascii(disk.get("unit"), "").strip().upper()
    compact = name.upper().replace(" ", "")
    if "M.2" in compact or "M2" in compact or "Ｍ．２" in compact:
        slots = re.findall(r"\d+", name)
        slot = slots[-1] if slots else str(fallback_index)
        return f"M2 DRIVE {slot}"
    # Prefer the actual disk/bay number in the label.  A unit name such as
    # DX517-1 also contains numbers but those are not the bay number.
    match = re.search(r"(?:ディスク|DISK|DRIVE)\s*(\d+)", name, re.IGNORECASE)
    slot = match.group(1) if match else str(fallback_index)
    if unit.startswith(("DX", "RX")) or "EXPANSION" in unit:
        return f"{unit[:14] or 'EXPANSION'} BAY {slot}"
    return f"NAS BAY {slot}"


def render(data, samples, cfg, state, fetched_at=None, ok=True, error=None):
    """Return a PNG byte string for one Telegram-friendly monitoring report."""
    data = data or {}
    disks = list(data.get("disks") or [])
    width = 1200
    height = 1120 + min(len(disks), 12) * 46
    c = Canvas(width, height)
    severity = state.get("severity", "NORMAL")
    sev_color = _severity_color(severity)

    c.rect(28, 24, width - 56, 74, PANEL, BORDER)
    c.text(52, 45, "NAS MONITOR SNAPSHOT", TEXT, 4)
    c.text(780, 48, "STATUS " + severity, sev_color, 3)
    c.text(52, 112, "TARGET " + _ascii(data.get("model")), MUTED, 2)
    stamp = fetched_at or datetime.now(JST).isoformat(timespec="seconds")
    c.text(52, 136, "CAPTURED " + _ascii(stamp, "UNKNOWN"), MUTED if ok else RED, 2)
    if not ok:
        c.text(52, 160, "LIVE FETCH ERROR - CACHED DATA", RED, 2)

    cards = [
        ("CPU", _num((data.get("cpu") or {}).get("total"), "%"), BLUE),
        ("MEMORY", _num((data.get("memory") or {}).get("usage"), "%"), TEAL),
        ("SYSTEM TEMP", _num(data.get("temperature"), "C"), sev_color),
        ("MAX DISK", _num(max((d.get("temp") for d in disks if isinstance(d.get("temp"), (int, float))), default=None), "C"), ORANGE),
    ]
    card_y = 186
    card_w = 270
    for index, (title, value, color) in enumerate(cards):
        x = 28 + index * (card_w + 20)
        c.rect(x, card_y, card_w, 138, PANEL, BORDER)
        c.text(x + 22, card_y + 22, title, MUTED, 2)
        c.text(x + 22, card_y + 65, value, color, 5)

    graph_y = 352
    c.text(42, graph_y, "TEMPERATURE 24H  SYSTEM / MAX DISK", TEXT, 2)
    graph_y += 28
    temp_values = [s.get("sys") for s in samples if isinstance(s.get("sys"), (int, float))]
    temp_values += [_max_disk(s) for s in samples if _max_disk(s) is not None]
    temp_values += [data.get("temperature")] if isinstance(data.get("temperature"), (int, float)) else []
    min_temp = min([20] + [float(v) - 3 for v in temp_values])
    max_temp = max([float(cfg.get("emergTemp", 90)) + 5] + [float(v) + 3 for v in temp_values])
    _draw_graph(c, 28, graph_y, width - 56, 250, samples, [
        {"color": BLUE, "value": lambda s: s.get("sys")},
        {"color": ORANGE, "value": _max_disk},
    ], min_temp, max_temp, [
        (cfg.get("warnTemp"), YELLOW), (cfg.get("critTemp"), ORANGE), (cfg.get("emergTemp"), RED),
    ])
    c.text(48, graph_y + 12, "SYS", BLUE, 2)
    c.text(120, graph_y + 12, "MAX DISK", ORANGE, 2)
    c.text(250, graph_y + 12, "WARN", YELLOW, 2)
    c.text(330, graph_y + 12, "CRIT", ORANGE, 2)
    c.text(410, graph_y + 12, "EMERG", RED, 2)
    c.text(48, graph_y + 220, "24H AGO", MUTED, 2)
    c.text(width - 145, graph_y + 220, "NOW", MUTED, 2)

    graph2_y = graph_y + 290
    c.text(42, graph2_y, "CPU / MEMORY 24H", TEXT, 2)
    _draw_graph(c, 28, graph2_y + 28, width - 56, 190, samples, [
        {"color": BLUE, "value": lambda s: s.get("cpu")},
        {"color": TEAL, "value": lambda s: s.get("mem")},
    ], 0, 100)
    c.text(48, graph2_y + 42, "CPU", BLUE, 2)
    c.text(115, graph2_y + 42, "MEMORY", TEAL, 2)

    table_y = graph2_y + 250
    c.text(42, table_y, "DRIVE TEMPERATURES", TEXT, 2)
    c.text(800, table_y, "TEMP", MUTED, 2)
    c.text(980, table_y, "STATUS", MUTED, 2)
    rows = sorted(enumerate(disks, 1), key=lambda item: item[1].get("temp") if isinstance(item[1].get("temp"), (int, float)) else -999, reverse=True)
    for row, (original_index, disk) in enumerate(rows[:12]):
        y = table_y + 32 + row * 46
        c.line(42, y - 8, width - 42, y - 8, BORDER)
        label = drive_label(disk, original_index)
        temp = disk.get("temp")
        color = RED if isinstance(temp, (int, float)) and temp >= cfg.get("emergTemp", 90) else ORANGE if isinstance(temp, (int, float)) and temp >= cfg.get("critTemp", 85) else YELLOW if isinstance(temp, (int, float)) and temp >= cfg.get("warnTemp", 75) else GREEN
        c.text(52, y + 3, label, TEXT, 2)
        c.text(810, y + 3, _num(temp, "C"), color, 2)
        c.text(980, y + 3, "OK" if str(disk.get("status", "")).lower() in ("normal", "healthy") else _ascii(disk.get("status"), "UNKNOWN"), color, 2)

    footer_y = height - 66
    c.rect(28, footer_y, width - 56, 42, PANEL, BORDER)
    net = data.get("network") or {}
    c.text(44, footer_y + 13, "NETWORK RX " + _num(net.get("rxBps")) + " BPS  TX " + _num(net.get("txBps")) + " BPS", MUTED, 2)
    return c.png()
