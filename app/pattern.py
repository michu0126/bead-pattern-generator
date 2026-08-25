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


def _delta_e_2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """Vectorised CIEDE2000 distance for broadcast-compatible Lab arrays."""
    lightness1, a1, b1 = np.moveaxis(lab1, -1, 0)
    lightness2, a2, b2 = np.moveaxis(lab2, -1, 0)

    chroma1 = np.hypot(a1, b1)
    chroma2 = np.hypot(a2, b2)
    mean_chroma = (chroma1 + chroma2) / 2
    mean_chroma7 = mean_chroma**7
    correction = 0.5 * (1 - np.sqrt(mean_chroma7 / (mean_chroma7 + 25.0**7)))

    adjusted_a1 = (1 + correction) * a1
    adjusted_a2 = (1 + correction) * a2
    adjusted_chroma1 = np.hypot(adjusted_a1, b1)
    adjusted_chroma2 = np.hypot(adjusted_a2, b2)
    hue1 = np.degrees(np.arctan2(b1, adjusted_a1)) % 360
    hue2 = np.degrees(np.arctan2(b2, adjusted_a2)) % 360

    delta_lightness = lightness2 - lightness1
    delta_chroma = adjusted_chroma2 - adjusted_chroma1
    hue_difference = hue2 - hue1
    chroma_product = adjusted_chroma1 * adjusted_chroma2
    hue_difference = np.where(chroma_product == 0, 0, hue_difference)
    hue_difference = np.where(hue_difference > 180, hue_difference - 360, hue_difference)
    hue_difference = np.where(hue_difference < -180, hue_difference + 360, hue_difference)
    delta_hue = 2 * np.sqrt(chroma_product) * np.sin(np.radians(hue_difference / 2))

    mean_lightness = (lightness1 + lightness2) / 2
    mean_adjusted_chroma = (adjusted_chroma1 + adjusted_chroma2) / 2
    absolute_hue_difference = np.abs(hue1 - hue2)
    hue_sum = hue1 + hue2
    mean_hue = np.where(
        chroma_product == 0,
        hue_sum,
        np.where(
            absolute_hue_difference <= 180,
            hue_sum / 2,
            np.where(hue_sum < 360, (hue_sum + 360) / 2, (hue_sum - 360) / 2),
        ),
    )

    hue_factor = (
        1
        - 0.17 * np.cos(np.radians(mean_hue - 30))
        + 0.24 * np.cos(np.radians(2 * mean_hue))
        + 0.32 * np.cos(np.radians(3 * mean_hue + 6))
        - 0.20 * np.cos(np.radians(4 * mean_hue - 63))
    )
    lightness_scale = 1 + (
        0.015 * (mean_lightness - 50) ** 2
        / np.sqrt(20 + (mean_lightness - 50) ** 2)
    )
    chroma_scale = 1 + 0.045 * mean_adjusted_chroma
    hue_scale = 1 + 0.015 * mean_adjusted_chroma * hue_factor
    rotation_angle = 30 * np.exp(-((mean_hue - 275) / 25) ** 2)
    mean_adjusted_chroma7 = mean_adjusted_chroma**7
    rotation_chroma = 2 * np.sqrt(
        mean_adjusted_chroma7 / (mean_adjusted_chroma7 + 25.0**7)
    )
    rotation = -np.sin(np.radians(2 * rotation_angle)) * rotation_chroma

    lightness_term = delta_lightness / lightness_scale
    chroma_term = delta_chroma / chroma_scale
    hue_term = delta_hue / hue_scale
    return np.maximum(
        lightness_term**2
        + chroma_term**2
        + hue_term**2
        + rotation * chroma_term * hue_term,
        0,
    )


def _packed_rgb(rgb: np.ndarray) -> np.ndarray:
    value = rgb.astype(np.uint32)
    return (value[..., 0] << 16) | (value[..., 1] << 8) | value[..., 2]


def _map_to_palette(pixels: np.ndarray, palette: list[dict]) -> np.ndarray:
    """Exact 24-bit HEX match first; CIEDE2000 nearest MARD colour otherwise."""
    flat = pixels.reshape(-1, 3).astype(np.uint8, copy=False)
    palette_rgb = np.array([item["rgb"] for item in palette], dtype=np.uint8)
    pixel_keys = _packed_rgb(flat)
    palette_keys = _packed_rgb(palette_rgb)

    order = np.argsort(palette_keys, kind="stable")
    sorted_keys = palette_keys[order]
    positions = np.searchsorted(sorted_keys, pixel_keys)
    bounded_positions = np.minimum(positions, len(sorted_keys) - 1)
    exact = (positions < len(sorted_keys)) & (sorted_keys[bounded_positions] == pixel_keys)

    mapped = np.empty(len(flat), dtype=np.int16)
    mapped[exact] = order[bounded_positions[exact]]
    unmatched = ~exact
    if np.any(unmatched):
        pixel_lab = _srgb_to_lab(flat[unmatched])
        palette_lab = _srgb_to_lab(palette_rgb)
        distances = _delta_e_2000(pixel_lab[:, None, :], palette_lab[None, :, :])
        mapped[unmatched] = np.argmin(distances, axis=1)
    return mapped.reshape(pixels.shape[:-1])


def _pixel_palette_indices(rgb: np.ndarray, palette: list[dict]) -> np.ndarray:
    """Match every distinct source HEX losslessly without reducing colour depth."""
    flat_keys = _packed_rgb(rgb).reshape(-1)
    unique_keys, inverse = np.unique(flat_keys, return_inverse=True)
    unique_rgb = np.stack(
        (
            (unique_keys >> 16) & 255,
            (unique_keys >> 8) & 255,
            unique_keys & 255,
        ),
        axis=1,
    ).astype(np.uint8)
    mapped = np.empty(len(unique_rgb), dtype=np.int16)
    for start in range(0, len(unique_rgb), 4096):
        stop = min(start + 4096, len(unique_rgb))
        mapped[start:stop] = _map_to_palette(unique_rgb[start:stop], palette).reshape(-1)
    return mapped[inverse].reshape(rgb.shape[:-1])


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
            "hex": item["hex"],
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
