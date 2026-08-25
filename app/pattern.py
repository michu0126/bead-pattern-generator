from __future__ import annotations

from collections import Counter, deque
from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .palette import BEAD_PALETTE


MIN_CELL_COVERAGE = 0.08
DARK_CELL_COVERAGE = 0.07
MAX_ANALYSIS_SIDE = 2048
QUANTIZATION_LEVELS = 32
_PALETTE_LUT_CACHE: dict[tuple[tuple[int, int, int], ...], np.ndarray] = {}


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


def _palette_lookup(palette: list[dict]) -> np.ndarray:
    """Map a compact RGB cube to palette indices once, then reuse it per source pixel."""
    key = tuple(tuple(int(channel) for channel in item["rgb"]) for item in palette)
    cached = _PALETTE_LUT_CACHE.get(key)
    if cached is not None:
        return cached

    levels = np.arange(QUANTIZATION_LEVELS, dtype=np.uint8) * 8 + 4
    levels[0] = 0
    levels[-1] = 255
    red, green, blue = np.meshgrid(levels, levels, levels, indexing="ij")
    samples = np.stack((red, green, blue), axis=-1).reshape(-1, 3)
    lookup = np.empty(len(samples), dtype=np.int16)
    for start in range(0, len(samples), 4096):
        stop = min(start + 4096, len(samples))
        lookup[start:stop] = _map_to_palette(samples[start:stop], palette).reshape(-1)
    _PALETTE_LUT_CACHE[key] = lookup
    return lookup


def _pixel_palette_indices(rgb: np.ndarray, palette: list[dict]) -> np.ndarray:
    bins = rgb.astype(np.uint16) >> 3
    keys = (bins[..., 0] * 1024 + bins[..., 1] * 32 + bins[..., 2]).astype(np.int32)
    return _palette_lookup(palette)[keys]


def _prepare_source(image: Image.Image) -> np.ndarray:
    source = ImageOps.exif_transpose(image).convert("RGBA")
    if max(source.size) > MAX_ANALYSIS_SIDE:
        ratio = MAX_ANALYSIS_SIDE / max(source.size)
        size = (
            max(1, round(source.width * ratio)),
            max(1, round(source.height * ratio)),
        )
        # NEAREST may discard detail on unusually large uploads, but never invents mixed RGB values.
        source = source.resize(size, Image.Resampling.NEAREST)
    return np.array(source, dtype=np.uint8, copy=True)


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


def _local_label_agreement(labels: np.ndarray, active: np.ndarray) -> np.ndarray:
    """Rate palette labels by local support so one-pixel antialias colours lose influence."""
    matches = np.zeros(labels.shape, dtype=np.float32)
    neighbours = np.zeros(labels.shape, dtype=np.float32)

    valid = active[1:, :] & active[:-1, :]
    same = valid & (labels[1:, :] == labels[:-1, :])
    neighbours[1:, :] += valid
    neighbours[:-1, :] += valid
    matches[1:, :] += same
    matches[:-1, :] += same

    valid = active[:, 1:] & active[:, :-1]
    same = valid & (labels[:, 1:] == labels[:, :-1])
    neighbours[:, 1:] += valid
    neighbours[:, :-1] += valid
    matches[:, 1:] += same
    matches[:, :-1] += same

    agreement = np.ones(labels.shape, dtype=np.float32)
    np.divide(matches, neighbours, out=agreement, where=neighbours > 0)
    agreement[~active] = 0
    return agreement


def _fit_geometry(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[float, float, float, float]:
    """Return the centred source crop used to fill the requested bead board."""
    target_ratio = target_width / target_height
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        crop_height = float(source_height)
        crop_width = crop_height * target_ratio
        left = (source_width - crop_width) / 2
        top = 0.0
    else:
        crop_width = float(source_width)
        crop_height = crop_width / target_ratio
        left = 0.0
        top = (source_height - crop_height) / 2
    return left, top, crop_width, crop_height


def _axis_overlaps(length: int, start: float, end: float) -> tuple[np.ndarray, np.ndarray]:
    """Return source-pixel indices and their exact geometric overlap with one cell."""
    start = max(0.0, min(float(length), start))
    end = max(start, min(float(length), end))
    first = max(0, int(math.floor(start)))
    last = min(length, int(math.ceil(end)))
    indices = np.arange(first, last, dtype=np.int32)
    if not len(indices):
        index = min(max(int(math.floor(start)), 0), length - 1)
        return np.array([index], dtype=np.int32), np.array([0.0], dtype=np.float64)
    overlap = np.minimum(indices + 1.0, end) - np.maximum(indices.astype(np.float64), start)
    valid = overlap > 1e-12
    return indices[valid], overlap[valid]


def _palette_luminance(palette: list[dict]) -> np.ndarray:
    rgb = np.array([item["rgb"] for item in palette], dtype=np.float64)
    return 0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]


def _choose_cell_palette(
    labels: np.ndarray,
    alpha: np.ndarray,
    agreement: np.ndarray,
    area: np.ndarray,
    center_weight: np.ndarray,
    cell_area: float,
    palette_size: int,
    dark_palette: np.ndarray,
) -> int:
    raw_weight = area * (alpha.astype(np.float64) / 255.0)
    coverage = raw_weight.sum() / max(cell_area, 1e-12)
    active = (labels >= 0) & (raw_weight > 1e-12)
    if coverage < MIN_CELL_COVERAGE or not np.any(active):
        return -1

    active_labels = labels[active]
    dark = dark_palette[active_labels]
    dark_coverage = raw_weight[active][dark].sum() / max(cell_area, 1e-12)
    if dark_coverage >= DARK_CELL_COVERAGE:
        votes = np.bincount(
            active_labels[dark],
            weights=(raw_weight * center_weight)[active][dark],
            minlength=palette_size,
        )
    else:
        reliability = 0.20 + 0.80 * np.square(agreement.astype(np.float64))
        votes = np.bincount(
            active_labels,
            weights=(raw_weight * center_weight * reliability)[active],
            minlength=palette_size,
        )

    highest = float(votes.max())
    if highest <= 0:
        return -1
    candidates = np.flatnonzero(votes >= highest * 0.96)
    if len(candidates) == 1:
        return int(candidates[0])

    # When two real colours divide a cell almost equally, use the closest active
    # source pixel to the cell centre. It resolves the boundary without blending.
    distances = np.where(active, -center_weight, np.inf)
    for flat_index in np.argsort(distances, axis=None, kind="stable"):
        row, column = np.unravel_index(flat_index, labels.shape)
        label = int(labels[row, column])
        if label in candidates:
            return label
    return int(candidates[0])


def _sample_cells(image: Image.Image, width: int, height: int, palette: list[dict]) -> np.ndarray:
    """Choose one palette colour per bead by exact source-pixel area voting."""
    pixels = _prepare_source(image)
    pixels[_exterior_white_mask(pixels), 3] = 0

    active = pixels[..., 3] >= 8
    labels = _pixel_palette_indices(pixels[..., :3], palette)
    labels[~active] = -1
    agreement = _local_label_agreement(labels, active)
    dark_palette = _palette_luminance(palette) <= 55

    left, top, crop_width, crop_height = _fit_geometry(
        pixels.shape[1],
        pixels.shape[0],
        width,
        height,
    )
    indices = np.full((height, width), -1, dtype=np.int16)

    for row in range(height):
        y0 = top + row * crop_height / height
        y1 = top + (row + 1) * crop_height / height
        y_indices, y_overlap = _axis_overlaps(pixels.shape[0], y0, y1)
        y_center = (y0 + y1) / 2
        y_radius = max((y1 - y0) / 2, 1e-12)
        y_distance = (y_indices + 0.5 - y_center) / y_radius

        for column in range(width):
            x0 = left + column * crop_width / width
            x1 = left + (column + 1) * crop_width / width
            x_indices, x_overlap = _axis_overlaps(pixels.shape[1], x0, x1)
            area = np.outer(y_overlap, x_overlap)
            cell_area = (y1 - y0) * (x1 - x0)

            x_center = (x0 + x1) / 2
            x_radius = max((x1 - x0) / 2, 1e-12)
            x_distance = (x_indices + 0.5 - x_center) / x_radius
            distance_squared = y_distance[:, None] ** 2 + x_distance[None, :] ** 2
            center_weight = 1.0 + 0.35 * np.clip(1.0 - distance_squared, 0.0, 1.0)

            selection = np.ix_(y_indices, x_indices)
            indices[row, column] = _choose_cell_palette(
                labels[selection],
                pixels[..., 3][selection],
                agreement[selection],
                area,
                center_weight,
                cell_area,
                len(palette),
                dark_palette,
            )
    return indices


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
    palette = BEAD_PALETTE
    indices = _sample_cells(image, width, height, palette)
    occupied = indices >= 0
    if not np.any(occupied):
        raise ValueError("抠图结果中没有可用的前景")

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
