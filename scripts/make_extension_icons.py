"""Draws the LMS Sync extension icons (an orange rounded square with a white
"L", in Moodle's orange) with no dependencies: python3 scripts/make_extension_icons.py"""
import struct
import zlib
from pathlib import Path

ORANGE = (249, 128, 18)  # Moodle's orange
WHITE = (255, 255, 255)
OUT = Path(__file__).parent.parent / "extension" / "icons"


def inside_rounded(x, y, r):  # unit square [0,1]²
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def in_l(x, y):
    return (0.30 <= x <= 0.47 and 0.20 <= y <= 0.80) or (0.30 <= x <= 0.74 and 0.63 <= y <= 0.80)


def render(size, ss=4):
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            r = g = b = a = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = (px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size
                    if inside_rounded(x, y, 0.22):
                        c = WHITE if in_l(x, y) else ORANGE
                        r, g, b, a = r + c[0], g + c[1], b + c[2], a + 1
            n = ss * ss
            row += bytes([r // a, g // a, b // a, 255 * a // n]) if a else bytes([0, 0, 0, 0])
        rows.append(bytes(row))
    return b"".join(rows)


def png(size):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


OUT.mkdir(parents=True, exist_ok=True)
for s in (16, 32, 48, 128):
    (OUT / f"icon{s}.png").write_bytes(png(s))
    print("wrote", OUT / f"icon{s}.png")
