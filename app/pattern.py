from __future__ import annotations

from collections import Counter, deque
from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .palette import BEAD_PALETTE


OVERSAMPLE = 8
MIN_CELL_COVERAGE = 0.08
DARK_CELL_COVERAGE = 0.10


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
    return np.stack(
        (116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])),
        axis=-1,
    )


def _fit_image(image: Image.Image, width: int, height: int) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGBA")
    method = (
        Image.Resampling.LANCZOS
        if image.width > width or image.height > height
        else Image.Resampling.NEAREST
    )
    return ImageOps.fit(image, (width, height), method=method)


def _map_to_palette(pixels: np.ndarray, palette: list[dict]) -> np.ndarray:
    """Match colours while preventing neutral greys from drifting into tinted families."""
    flat = pixels.reshape(-1, 3)
    pixel_lab = _srgb_to_lab(flat)
    palette_rgb = np.array([item["rgb"] for item in palette])
    palette_lab = _srgb_to_lab(palette_rgb)

    base_distances = np.sum((pixel_lab[:, None, :] - palette_lab[None, :, :]) ** 2, axis=2)
    distances = base_distances.copy()

    pixel_spread = np.ptp(flat.astype(np.int16), axis=1)
    palette_spread = np.ptp(palette_rgb.astype(np.int16), axis=1)
    neutral_pixels = pixel_spread <= 12
    neutral_palette = palette_spread <= 6
    distances[np.ix_(neutral_pixels, ~neutral_palette)] = np.inf

    pixel_chroma = np.hypot(pixel_lab[:, 1], pixel_lab[:, 2])
    palette_chroma = np.hypot(palette_lab[:, 1], palette_lab[:, 2])
    pixel_hue = np.degrees(np.arctan2(pixel_lab[:, 2], pixel_lab[:, 1]))
    palette_hue = np.degrees(np.arctan2(palette_lab[:, 2], palette_lab[:, 1]))
    hue_delta = np.abs((pixel_hue[:, None] - palette_hue[None, :] + 180) % 360 - 180)

    chromatic_pixels = pixel_chroma >= 12
    compatible_hue = (palette_chroma[None, :] >= 7) & (hue_delta <= 55)
    hue_penalty = (hue_delta / 30) ** 2 * 16
    chromatic_cost = np.where(compatible_hue, distances + hue_penalty, np.inf)
    distances[chromatic_pixels] = chromatic_cost[chromatic_pixels]

    no_candidate = ~np.any(np.isfinite(distances), axis=1)
    distances[no_candidate] = base_distances[no_candidate]
    return np.argmin(distances, axis=1).reshape(pixels.shape[:-1])


def _exterior_white_mask(pixels: np.ndarray) -> np.ndarray:
    """Find near-white pixels connected to the outside edge of the image."""
    rgb = pixels[..., :3].astype(np.int16)
    alpha = pixels[..., 3]
    candidate = (
        (alpha >= 32)
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


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> int:
    order = np.argsort(values, kind="stable")
    ordered_weights = weights[order]
    midpoint = ordered_weights.sum() / 2
    index = int(np.searchsorted(np.cumsum(ordered_weights), midpoint, side="left"))
    return int(values[order[min(index, len(order) - 1)]])


def _representative_cell_colour(block: np.ndarray) -> tuple[int, int, int] | None:
    """Choose a real dominant fill colour instead of averaging edge colours together."""
    rgb = block[..., :3].reshape(-1, 3).astype(np.int16)
    weights = block[..., 3].reshape(-1).astype(np.float64) / 255
    active = weights >= 0.03
    if not np.any(active) or weights.sum() / len(weights) < MIN_CELL_COVERAGE:
        return None

    rgb = rgb[active]
    weights = weights[active]
    dark = (np.max(rgb, axis=1) <= 64) & (np.mean(rgb, axis=1) <= 48)
    dark_coverage = weights[dark].sum() / block[..., 3].size

    if dark_coverage >= DARK_CELL_COVERAGE:
        selected = dark
    else:
        neutral = np.ptp(rgb, axis=1) <= 18
        neutral_weight = weights[neutral].sum()
        chromatic_weight = weights[~neutral].sum()
        if neutral_weight >= chromatic_weight * 1.2:
            selected = neutral
        elif chromatic_weight >= neutral_weight * 1.2:
            selected = ~neutral
        else:
            selected = np.ones(len(rgb), dtype=bool)

    if not np.any(selected):
        selected = np.ones(len(rgb), dtype=bool)
    chosen = rgb[selected]
    chosen_weights = weights[selected]
    colour = np.array(
        [_weighted_median(chosen[:, channel], chosen_weights) for channel in range(3)],
        dtype=np.int16,
    )

    spread = int(np.ptp(colour))
    brightness = float(np.mean(colour))
    if spread <= 10 and brightness <= 30:
        colour[:] = 0
    elif spread <= 10 and brightness >= 238:
        colour[:] = 255
    return tuple(int(value) for value in colour)


def _sample_cells(image: Image.Image, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Sample each bead from an oversampled, background-aware block."""
    scale = OVERSAMPLE
    fitted = _fit_image(image, width * scale, height * scale)
    pixels = np.array(fitted, dtype=np.uint8, copy=True)
    pixels[_exterior_white_mask(pixels), 3] = 0

    colours = np.zeros((height, width, 3), dtype=np.uint8)
    occupied = np.zeros((height, width), dtype=bool)
    for row in range(height):
        for column in range(width):
            block = pixels[
                row * scale : (row + 1) * scale,
                column * scale : (column + 1) * scale,
            ]
            colour = _representative_cell_colour(block)
            if colour is not None:
                colours[row, column] = colour
                occupied[row, column] = True
    return colours, occupied


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


def generate_pattern(image: Image.Image, width: int, height: int) -> tuple[bytes, list[dict], list[list[str | None]]]:
    sampled, occupied = _sample_cells(image, width, height)
    if not np.any(occupied):
        raise ValueError("抠图结果中没有可用的前景")

    palette = BEAD_PALETTE
    indices = np.full((height, width), -1, dtype=np.int16)
    indices[occupied] = _map_to_palette(sampled[occupied], palette)
    counts = Counter(indices[occupied].tolist())
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
    grid = [
        [palette[int(index)]["code"] if int(index) >= 0 else None for index in row]
        for row in indices
    ]
    return _render_pattern(indices, palette), summary, grid
