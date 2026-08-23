from __future__ import annotations

from collections import Counter, deque
from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .palette import BEAD_PALETTE


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert an (..., 3) sRGB array in the 0..255 range to CIE Lab."""
    value = rgb.astype(np.float64) / 255.0
    value = np.where(value <= 0.04045, value / 12.92, ((value + 0.055) / 1.055) ** 2.4)
    xyz = value @ np.array(
        [
            [0.4124564, 0.2126729, 0.0193339],
            [0.3575761, 0.7151522, 0.1191920],
            [0.1804375, 0.0721750, 0.9503041],
        ]
    )
    xyz /= np.array([0.95047, 1.0, 1.08883])
    delta = 6 / 29
    f = np.where(xyz > delta**3, np.cbrt(xyz), xyz / (3 * delta**2) + 4 / 29)
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])), axis=-1)


def _fit_image(image: Image.Image, width: int, height: int) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGBA")
    return ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)


def _map_to_palette(pixels: np.ndarray, palette: list[dict]) -> np.ndarray:
    pixel_lab = _srgb_to_lab(pixels.reshape(-1, 3))
    palette_lab = _srgb_to_lab(np.array([item["rgb"] for item in palette]))
    distances = np.sum((pixel_lab[:, None, :] - palette_lab[None, :, :]) ** 2, axis=2)
    return np.argmin(distances, axis=1).reshape(pixels.shape[:-1])


def _exterior_white_mask(pixels: np.ndarray) -> np.ndarray:
    """Find near-white pixels connected to the outside edge of the image."""
    rgb = pixels[..., :3].astype(np.int16)
    alpha = pixels[..., 3]
    candidate = (
        (alpha >= 64)
        & (np.min(rgb, axis=2) >= 230)
        & ((np.max(rgb, axis=2) - np.min(rgb, axis=2)) <= 28)
    )
    rows, columns = candidate.shape
    exterior = np.zeros_like(candidate)
    queue: deque[tuple[int, int]] = deque()

    for column in range(columns):
        if candidate[0, column]:
            queue.append((0, column))
        if rows > 1 and candidate[rows - 1, column]:
            queue.append((rows - 1, column))
    for row in range(1, rows - 1):
        if candidate[row, 0]:
            queue.append((row, 0))
        if columns > 1 and candidate[row, columns - 1]:
            queue.append((row, columns - 1))

    while queue:
        row, column = queue.popleft()
        if exterior[row, column] or not candidate[row, column]:
            continue
        exterior[row, column] = True
        if row > 0:
            queue.append((row - 1, column))
        if row + 1 < rows:
            queue.append((row + 1, column))
        if column > 0:
            queue.append((row, column - 1))
        if column + 1 < columns:
            queue.append((row, column + 1))
    return exterior


def _text_colour(rgb: tuple[int, int, int]) -> str:
    luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    return "#111111" if luminance > 145 else "#ffffff"


def _render_pattern(indices: np.ndarray, palette: list[dict]) -> bytes:
    rows, columns = indices.shape
    cell = max(20, min(36, math.floor(1800 / max(columns, 1))))
    margin = 2
    canvas = Image.new("RGB", (columns * cell + margin * 2, rows * cell + margin * 2), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=max(10, cell // 3))

    for row in range(rows):
        for column in range(columns):
            x0, y0 = margin + column * cell, margin + row * cell
            x1, y1 = x0 + cell, y0 + cell
            palette_index = int(indices[row, column])
            if palette_index < 0:
                draw.rectangle((x0, y0, x1, y1), fill=(250, 250, 250), outline=(180, 180, 180), width=1)
                continue
            colour = palette[palette_index]
            draw.rectangle((x0, y0, x1, y1), fill=colour["rgb"], outline=(85, 85, 85), width=1)
            label = colour["code"]
            box = draw.textbbox((0, 0), label, font=font)
            draw.text(
                (x0 + (cell - (box[2] - box[0])) / 2, y0 + (cell - (box[3] - box[1])) / 2 - 1),
                label,
                fill=_text_colour(colour["rgb"]),
                font=font,
            )

    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def generate_pattern(image: Image.Image, width: int, height: int) -> tuple[bytes, list[dict]]:
    resized = _fit_image(image, width, height)
    pixels = np.asarray(resized)
    palette = BEAD_PALETTE
    opaque = pixels[..., 3] >= 64
    opaque &= ~_exterior_white_mask(pixels)
    if not np.any(opaque):
        raise ValueError("抠图结果中没有可用的前景")
    indices = np.full((height, width), -1, dtype=np.int16)
    indices[opaque] = _map_to_palette(pixels[..., :3][opaque], palette)
    counts = Counter(indices[opaque].tolist())
    summary = [
        {
            "code": item["code"],
            "name": item["name"],
            "rgb": list(item["rgb"]),
            "count": counts.get(index, 0),
        }
        for index, item in enumerate(palette)
        if counts.get(index, 0) > 0
    ]
    summary.sort(key=lambda item: item["count"], reverse=True)
    return _render_pattern(indices, palette), summary
