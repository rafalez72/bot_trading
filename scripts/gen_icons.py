"""Genera íconos PWA placeholder (PNG) sin dependencias externas.

Usa stdlib: zlib + struct para emitir un PNG válido. Diseño simple:
fondo dark indigo + letra "C" centrada.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "api" / "static" / "icons"
OUT.mkdir(parents=True, exist_ok=True)


def png(size: int, path: Path) -> None:
    bg = (15, 23, 42)        # slate-950
    accent = (99, 102, 241)  # indigo-500
    fg = (226, 232, 240)     # slate-100

    pixels = bytearray()
    cx, cy = size / 2, size / 2
    r_outer = size * 0.42
    r_inner = size * 0.30
    bar_w = size * 0.10
    bar_h = size * 0.32

    for y in range(size):
        pixels.append(0)  # filter byte
        for x in range(size):
            dx, dy = x - cx, y - cy
            d2 = dx * dx + dy * dy
            # Círculo accent con borde y "C" interior (anillo)
            if d2 < r_outer * r_outer:
                if d2 > r_inner * r_inner:
                    # anillo accent
                    c = accent
                else:
                    c = bg
                # mordida derecha del anillo (forma de "C")
                if dx > 0 and abs(dy) < bar_h * 0.55:
                    c = bg
                pixels.extend(c)
            else:
                pixels.extend(bg)
            # marca diagonal sutil (chip de copy)
            if size * 0.55 < x < size * 0.85 and size * 0.55 < y < size * 0.85:
                if (x + y) % 4 == 0:
                    pixels[-3:] = bytes(fg)

    # Construcción PNG
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(pixels), 9)
    png_bytes = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    path.write_bytes(png_bytes)
    print(f"  wrote {path.name} ({len(png_bytes):,} bytes)")


if __name__ == "__main__":
    for s in (192, 512):
        png(s, OUT / f"icon-{s}.png")
    print(f"✓ Íconos generados en {OUT}")
