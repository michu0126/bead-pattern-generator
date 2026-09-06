from __future__ import annotations

from collections import Counter
from functools import lru_cache
from io import BytesIO
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy import ndimage

from .palette import BEAD_PALETTE


MIN_CELL_COVERAGE = 0.08
MAX_ANALYSIS_SIDE = 4096
PALETTE_DISTANCE_BATCH = 512
MIN_VISIBLE_ALPHA = 8


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


@lru_cache(maxsize=8)
def _palette_lookup(colours: tuple[tuple[int, int, int], ...]) -> tuple[np.ndarray, ...]:
    """Cache only palette data, never uploaded images or their colours."""
    rgb = np.array(colours, dtype=np.uint8)
    keys = _packed_rgb(rgb)
    order = np.argsort(keys, kind="stable")
    result = (keys[order], order, _srgb_to_lab(rgb))
    for value in result:
        value.flags.writeable = False
    return result


def _map_to_palette(pixels: np.ndarray, palette: list[dict]) -> np.ndarray:
    """Exact 24-bit HEX first, then bounded-memory CIEDE2000 over every MARD colour."""
    if not palette:
        raise ValueError("色卡不能为空")
    flat = pixels.reshape(-1, 3).astype(np.uint8, copy=False)
    sorted_keys, order, palette_lab = _palette_lookup(
        tuple(tuple(item["rgb"]) for item in palette)
    )
    pixel_keys = _packed_rgb(flat)
    positions = np.searchsorted(sorted_keys, pixel_keys)
    bounded_positions = np.minimum(positions, len(sorted_keys) - 1)
    exact = (positions < len(sorted_keys)) & (sorted_keys[bounded_positions] == pixel_keys)

    mapped = np.empty(len(flat), dtype=np.int16)
    mapped[exact] = order[bounded_positions[exact]]
    unmatched = np.flatnonzero(~exact)
    # The old 4096 x 221 distance matrix created many large float64 intermediates.
    # Chunk here (also protecting direct callers), without pruning candidates or
    # approximating HEX values. Results are identical to a full distance matrix.
    for start in range(0, len(unmatched), PALETTE_DISTANCE_BATCH):
        selection = unmatched[start:start + PALETTE_DISTANCE_BATCH]
        pixel_lab = _srgb_to_lab(flat[selection])
        distances = _delta_e_2000(pixel_lab[:, None, :], palette_lab[None, :, :])
        mapped[selection] = np.argmin(distances, axis=1)
    return mapped.reshape(pixels.shape[:-1])


def _pixel_palette_indices(
    rgb: np.ndarray, palette: list[dict], active: np.ndarray | None = None,
) -> np.ndarray:
    """Match each visible source HEX once; hidden RGB must never cost a lookup."""
    if active is None:
        active = np.ones(rgb.shape[:-1], dtype=bool)
    labels = np.full(rgb.shape[:-1], -1, dtype=np.int16)
    if not np.any(active):
        return labels
    unique_keys, inverse = np.unique(_packed_rgb(rgb[active]), return_inverse=True)
    unique_rgb = np.stack(
        ((unique_keys >> 16) & 255, (unique_keys >> 8) & 255, unique_keys & 255),
        axis=1,
    ).astype(np.uint8)
    labels[active] = _map_to_palette(unique_rgb, palette)[inverse]
    return labels


def _prepare_source(image: Image.Image) -> np.ndarray:
    source = ImageOps.exif_transpose(image).convert("RGBA")
    if max(source.size) > MAX_ANALYSIS_SIDE:
        ratio = MAX_ANALYSIS_SIDE / max(source.size)
        size = (
            max(1, round(source.width * ratio)),
            max(1, round(source.height * ratio)),
        )
        # Never interpolate RGB while preparing the source: every retained sample
        # must remain an original pixel colour before it is matched to MARD.
        source = source.resize(size, Image.Resampling.NEAREST)
    return np.array(source, dtype=np.uint8, copy=True)


def _discardable_white_mask(pixels: np.ndarray) -> np.ndarray:
    """Four-connected white-background cleanup for an opaque source.

    This is a colour heuristic, not semantic segmentation. Callers must honour
    existing transparency instead of deleting white from a supplied cutout.
    Warm off-whites/pastels are not background just because they are bright.
    """
    rgb = pixels[..., :3]
    alpha = pixels[..., 3]
    white = (
        (alpha >= MIN_VISIBLE_ALPHA)
        & (np.min(rgb, axis=2) >= 245)
        & (np.ptp(rgb, axis=2) <= 10)
    )
    subject = (alpha >= MIN_VISIBLE_ALPHA) & ~white
    if not np.any(white) or not np.any(subject):
        # A white-only picture has no evidence of a separable background.
        return np.zeros(white.shape, dtype=bool)

    # SciPy uses a four-connected cross by default. Compiled component labelling
    # replaces a Python queue/list containing millions of coordinate tuples.
    components, count = ndimage.label(white)
    touches_subject = ndimage.binary_dilation(subject) & white
    keep = np.zeros(count + 1, dtype=bool)
    keep[np.unique(components[touches_subject])] = True
    border = np.concatenate((
        components[0], components[-1], components[:, 0], components[:, -1],
    ))
    keep[np.unique(border)] = False
    keep[0] = False
    return white & ~keep[components]


def _focused_geometry(
    pixels: np.ndarray,
    target_width: int,
    target_height: int,
) -> tuple[float, float, float, float]:
    """Fit the whole image; focus small boards without ever clipping the subject."""
    source_height, source_width = pixels.shape[:2]
    normal = _fit_geometry(source_width, source_height, target_width, target_height)
    if not (50 <= target_width <= 52 and 50 <= target_height <= 52):
        return normal
    active = pixels[..., 3] >= MIN_VISIBLE_ALPHA
    rows = np.flatnonzero(np.any(active, axis=1))
    columns = np.flatnonzero(np.any(active, axis=0))
    if not len(rows) or not len(columns):
        return normal
    min_row, max_row = int(rows[0]), int(rows[-1]) + 1
    min_column, max_column = int(columns[0]), int(columns[-1]) + 1
    # Reserve two bead cells on each side, rather than wasting 24% extra canvas.
    scale = max(
        (max_column - min_column) / (target_width - 4),
        (max_row - min_row) / (target_height - 4),
    )
    frame_width = target_width * scale
    frame_height = target_height * scale
    if frame_width >= normal[2] * 0.9:
        return normal
    centre_x = (min_column + max_column) / 2
    centre_y = (min_row + max_row) / 2
    # A virtual frame may extend outside the source. Those parts contribute zero
    # area/alpha. Clamping this frame used to crop off wide/tall or off-centre art.
    return centre_x - frame_width / 2, centre_y - frame_height / 2, frame_width, frame_height


def _fit_geometry(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> tuple[float, float, float, float]:
    """Centred contain-fit: preserve aspect ratio and pad, never crop to fill."""
    scale = max(source_width / target_width, source_height / target_height)
    frame_width = target_width * scale
    frame_height = target_height * scale
    return (
        (source_width - frame_width) / 2,
        (source_height - frame_height) / 2,
        frame_width,
        frame_height,
    )


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


def _choose_cell_palette(
    labels: np.ndarray,
    alpha: np.ndarray,
    area: np.ndarray,
    center_weight: np.ndarray,
    cell_area: float,
    palette_size: int,
) -> int:
    """Categorically downsample already-quantised MARD labels, never RGB values."""
    raw_weight = np.where(labels >= 0, area * (alpha.astype(np.float64) / 255.0), 0.0)
    coverage = raw_weight.sum() / max(cell_area, 1e-12)
    active = (labels >= 0) & (raw_weight > 1e-12)
    if coverage < MIN_CELL_COVERAGE or not np.any(active):
        return -1

    # Every original pixel has already been matched by its HEX/RGB value to one
    # MARD code.  The board only votes among those discrete codes by true covered
    # area; no average RGB is ever created at an edge.
    votes = np.bincount(
        labels[active],
        weights=raw_weight[active],
        minlength=palette_size,
    )
    highest = float(votes.max())
    if highest <= 0:
        return -1
    candidates = np.flatnonzero(np.isclose(votes, highest, rtol=0.0, atol=1e-12))
    if len(candidates) == 1:
        return int(candidates[0])

    # A mathematically exact tie is resolved by the label whose original pixel
    # lies closest to the bead centre, still without blending any colours.
    distances = np.where(active, -center_weight, np.inf)
    for flat_index in np.argsort(distances, axis=None, kind="stable"):
        row, column = np.unravel_index(flat_index, labels.shape)
        label = int(labels[row, column])
        if label in candidates:
            return label
    return int(candidates[0])


def _sample_cells(image: Image.Image, width: int, height: int, palette: list[dict]) -> np.ndarray:
    """Quantise source pixels first, then downsample MARD labels by exact area."""
    pixels = _prepare_source(image)
    if np.all(pixels[..., 3] == 255):
        pixels[_discardable_white_mask(pixels), 3] = 0

    active = pixels[..., 3] >= MIN_VISIBLE_ALPHA
    labels = _pixel_palette_indices(pixels[..., :3], palette, active)
    left, top, crop_width, crop_height = _focused_geometry(pixels, width, height)
    indices = np.full((height, width), -1, dtype=np.int16)

    # Axis footprints are reused across all rows/columns instead of rebuilding
    # the same x overlaps for every bead. Coordinates outside the frame stay empty.
    x_footprints = []
    for column in range(width):
        x0 = left + column * crop_width / width
        x1 = left + (column + 1) * crop_width / width
        x_indices, x_overlap = _axis_overlaps(pixels.shape[1], x0, x1)
        x_distance = (x_indices + 0.5 - (x0 + x1) / 2) / max((x1 - x0) / 2, 1e-12)
        x_footprints.append((x_indices, x_overlap, x_distance))

    for row in range(height):
        y0 = top + row * crop_height / height
        y1 = top + (row + 1) * crop_height / height
        y_indices, y_overlap = _axis_overlaps(pixels.shape[0], y0, y1)
        y_center = (y0 + y1) / 2
        y_radius = max((y1 - y0) / 2, 1e-12)
        y_distance = (y_indices + 0.5 - y_center) / y_radius

        for column, (x_indices, x_overlap, x_distance) in enumerate(x_footprints):
            area = np.outer(y_overlap, x_overlap)
            cell_area = (y1 - y0) * crop_width / width
            distance_squared = y_distance[:, None] ** 2 + x_distance[None, :] ** 2
            # Do not clip distances: clipping made many non-central pixels tie.
            center_weight = -distance_squared

            selection = np.ix_(y_indices, x_indices)
            indices[row, column] = _choose_cell_palette(
                labels[selection],
                pixels[..., 3][selection],
                area,
                center_weight,
                cell_area,
                len(palette),
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
