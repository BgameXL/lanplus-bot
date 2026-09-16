from __future__ import annotations

import colorsys
import io

from PIL import Image


def dominant_color(image_bytes: bytes | None, default: int) -> int:
    if not image_bytes:
        return default
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail((80, 80))
        quant = img.quantize(colors=8)
        palette = quant.getpalette() or []
        counts = quant.getcolors() or []
    except (OSError, ValueError, Image.DecompressionBombError):
        return default

    if not counts or not palette:
        return default

    total = sum(count for count, _ in counts) or 1
    first_color = counts[0][1] * 3
    best_rgb = (
        palette[first_color],
        palette[first_color + 1],
        palette[first_color + 2],
    )
    best_score = -1.0

    for count, idx in counts:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if v < 0.15 or (s < 0.12 and (v < 0.25 or v > 0.9)):
            weight = 0.15
        else:
            weight = 1.0
        score = (count / total) * (0.3 + s) * (0.3 + v) * weight
        if score > best_score:
            best_score = score
            best_rgb = (r, g, b)

    r, g, b = best_rgb
    return (r << 16) | (g << 8) | b
