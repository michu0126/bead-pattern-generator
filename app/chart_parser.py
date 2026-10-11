"""Recover editable bead colours from a rendered, labelled Image2 chart.

This is raster parsing, not OCR: colours come from cell fills, not the printed
codes. A model can still draw a wrong colour or irregular grid, so uncertainty
is exposed instead of treating every generated image as a valid chart.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage
from scipy.signal import find_peaks

from .palette import BEAD_PALETTE
from .pattern import _map_to_palette, _packed_rgb


MAX_CHART_SIDE = 4096


def _line_positions(pixels: np.ndarray, count: int, axis: int) -> np.ndarray | None:
    """Locate thin, long grid strokes; short code glyphs have weaker support."""
    rgb = pixels[..., :3]
    # Move the measured axis first. For a column profile each entry contains
    # all pixels in that column; the other orientation works identically.
    values = np.moveaxis(rgb, axis, 0)
    length = len(values)
    if count < 2 or length < count * 4:
        return None
    radius = max(1, min(4, round(length / count * 0.10)))
    profile = np.zeros(length, dtype=np.float32)
    for offset in range(1, radius + 1):
        # Bound temporary arrays on NAS hosts: never expand the whole RGBA
        # image into several float32 / quantile intermediates at once.
        for start in range(offset, length - offset, 128):
            stop = min(start + 128, length - offset)
            centre = values[start:stop].astype(np.float32)
            left = np.mean(np.abs(centre - values[start - offset:stop - offset]), axis=2)
            right = np.mean(np.abs(centre - values[start + offset:stop + offset]), axis=2)
            contrast = np.minimum(left, right)
            score = np.quantile(contrast, 0.60, axis=1) + 0.2 * np.mean(contrast, axis=1)
            profile[start:stop] = np.maximum(profile[start:stop], score)
    maximum = float(profile.max())
    if maximum < 8:
        return None
    threshold = max(7, maximum * 0.30)
    peaks, _ = find_peaks(
        profile, height=threshold, prominence=threshold * 0.50,
        distance=max(2, int(length / count * 0.55)),
    )
    # scipy does not return endpoint peaks. Include a real outer grid stroke,
    # but not a broad white margin (whose inward contrast is zero).
    for position, neighbour in ((0, radius), (length - 1, length - radius - 1)):
        contrast = np.mean(np.abs(values[position].astype(np.float32) - values[neighbour]), axis=1)
        score = float(np.quantile(contrast, 0.60) + 0.2 * np.mean(contrast))
        if score >= threshold and not np.any(np.abs(peaks - position) <= radius):
            peaks = np.append(peaks, position)
    peaks = np.sort(peaks)
    if len(peaks) < 5:
        return None
    gaps = np.diff(peaks)
    pitch = float(np.median(gaps))
    # Requiring almost all intervals to agree avoids treating illustration
    # edges or a few decorative rules as proof of a regular bead grid.
    if pitch < 4 or np.mean(np.abs(gaps - pitch) <= max(2, pitch * 0.20)) < 0.92:
        return None
    if np.max(np.abs(gaps - pitch)) > max(3, pitch * 0.40):
        return None
    # A chart can reach the image edge without drawing its final outer stroke.
    # Complete only whole, regular periods that terminate at the actual canvas
    # edge. Never invent an extra interval inside a surrounding white margin.
    if len(peaks) in (count, count - 1):
        leading_gap = float(peaks[0])
        trailing_gap = float(length - peaks[-1])
        missing_left = abs(leading_gap - pitch) <= max(1, pitch * 0.10)
        missing_right = abs(trailing_gap - pitch) <= max(1, pitch * 0.10)
        left_at_edge = peaks[0] <= radius
        right_at_edge = length - 1 - peaks[-1] <= radius
        if missing_left and (right_at_edge or missing_right):
            peaks = np.insert(peaks, 0, 0)
        if missing_right and (left_at_edge or missing_left):
            peaks = np.append(peaks, length)
    return peaks.astype(np.float64)


def _cell_fill(patch: np.ndarray) -> tuple[np.ndarray | None, float]:
    """Use fill-colour consensus around the centre, where code ink is sparse.

    Coarse bins identify one robust cluster in JPEG/antialiased output. The
    returned HEX is always an observed pixel, never a blend of bead colours.
    """
    height, width = patch.shape[:2]
    if not height or not width:
        return None, 0.0
    yy, xx = np.indices((height, width))
    x = (xx + 0.5) / width
    y = (yy + 0.5) / height
    ring = (x < 0.25) | (x > 0.75) | (y < 0.28) | (y > 0.72)
    visible = patch[..., 3] >= 128
    if np.mean(visible) < 0.08:
        return None, 1.0
    selection = visible & ring
    if not np.any(selection):
        selection = visible
    colours = patch[..., :3][selection]
    keys = _packed_rgb(colours >> 3)
    bins, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    winning = int(np.argmax(counts))
    members = colours[inverse == winning]
    actual, frequency = np.unique(members, axis=0, return_counts=True)
    if frequency.max() > 1:
        fill = actual[np.argmax(frequency)]
    else:
        # A medoid preserves an existing HEX even when compression makes each
        # sampled pixel slightly different.
        median = np.median(members, axis=0)
        fill = members[np.argmin(np.sum((members.astype(float) - median) ** 2, axis=1))]
    return fill, float(counts[winning] / len(colours))


def parse_image2_chart(
    image: Image.Image, width: int, height: int, *, source_image: Image.Image | None = None,
) -> dict:
    """Parse a chart, preserving opaque whites and reporting layout uncertainty.

    A source alpha mask is NOT projected onto a newly generated composition:
    the model may have moved the subject. For an already transparent source,
    a confidently located, labelled chart may instead expose unlabelled white
    cells connected to the board edge as empty. Enclosed whites stay intact.
    """
    if not (1 <= width <= 256 and 1 <= height <= 256):
        raise ValueError("图纸的行列数必须在 1 到 256 之间")
    if max(image.size) > MAX_CHART_SIDE:
        raise ValueError("Image2 返回的图纸超过 4096 像素，无法安全解析，请选择较小输出尺寸后重新生成")
    rgba = np.asarray(ImageOps.exif_transpose(image).convert("RGBA"))
    if rgba.shape[0] < height * 2 or rgba.shape[1] < width * 2:
        raise ValueError("Image2 返回的图纸分辨率过低，无法可靠拆解，请重新生成")
    warnings: list[str] = []
    x_lines = _line_positions(rgba, width, 1)
    y_lines = _line_positions(rgba, height, 0)
    grid_detected = x_lines is not None and y_lines is not None
    if grid_detected:
        detected_width, detected_height = len(x_lines) - 1, len(y_lines) - 1
        if (detected_width, detected_height) != (width, height):
            raise ValueError(
                f"Image2 返回的网格为 {detected_width} × {detected_height}，"
                f"与所选的 {width} × {height} 不符，请重新生成"
            )
    else:
        x_lines = np.linspace(0, rgba.shape[1], width + 1)
        y_lines = np.linspace(0, rgba.shape[0], height + 1)
        warnings.append("未能可靠定位完整网格，暂按整幅图片划分；请核对边距、行列数和色号后使用材料清单。")

    fills: list[np.ndarray] = []
    locations: list[tuple[int, int]] = []
    grid: list[list[str | None]] = [[None] * width for _ in range(height)]
    blank_white = np.zeros((height, width), dtype=bool)
    ink_evidence = np.zeros((height, width), dtype=bool)
    uncertain = 0
    for row in range(height):
        y0, y1 = y_lines[row:row + 2]
        for column in range(width):
            x0, x1 = x_lines[column:column + 2]
            # Remove stroke edges before looking for fill consensus. This
            # follows detected intervals, including slightly uneven AI cells.
            inset_x, inset_y = max(1, (x1 - x0) * 0.13), max(1, (y1 - y0) * 0.13)
            left, right = int(np.ceil(x0 + inset_x)), int(np.floor(x1 - inset_x)) + 1
            top, bottom = int(np.ceil(y0 + inset_y)), int(np.floor(y1 - inset_y)) + 1
            patch = rgba[top:bottom, left:right]
            fill, confidence = _cell_fill(patch)
            if fill is not None:
                fills.append(fill)
                locations.append((row, column))
                uncertain += confidence < 0.45
                visible = patch[..., 3] >= 128
                difference = np.max(np.abs(patch[..., :3].astype(np.int16) - fill), axis=2)
                # This is evidence of contrasting ink, not OCR. Err on the
                # side of keeping a white bead if even a small label is present.
                ink_evidence[row, column] = np.count_nonzero(visible & (difference > 48)) >= max(2, int(patch.shape[0] * patch.shape[1] * 0.005))
                blank_white[row, column] = (
                    int(fill.min()) >= 245 and int(fill.max()) - int(fill.min()) <= 8
                    and confidence >= 0.75 and not ink_evidence[row, column]
                )
    if fills:
        mapped = _map_to_palette(np.asarray(fills, dtype=np.uint8), BEAD_PALETTE)
        for (row, column), index in zip(locations, mapped):
            grid[row][column] = BEAD_PALETTE[int(index)]["code"]
    if uncertain:
        warnings.append(f"有 {uncertain} 格的填色不够明确，可能受字码、渐变或压缩影响，建议在编辑器中核对。")
    if min(np.min(np.diff(x_lines)), np.min(np.diff(y_lines))) < 8:
        warnings.append("部分格子的像素尺寸过小，字码可能遮住填色；建议使用更高分辨率输出或较小板型，并核对色号。")
    cleared_background_cells = 0
    if source_image is not None:
        source = ImageOps.exif_transpose(source_image)
        has_alpha = "A" in source.getbands() or "transparency" in source.info
        source_transparent = has_alpha and np.any(np.asarray(source.convert("RGBA").getchannel("A")) < 128)
        # Transparency in a decorative outer margin does not establish that
        # actual bead cells are transparent; trust the parsed cells themselves.
        chart_transparent = any(code is None for row in grid for code in row)
        if source_transparent and not chart_transparent:
            if grid_detected and np.any(ink_evidence):
                seeds = blank_white.copy()
                seeds[1:-1, 1:-1] = False
                exterior_blank = ndimage.binary_propagation(seeds, mask=blank_white)
                for row, column in np.argwhere(exterior_blank):
                    grid[int(row)][int(column)] = None
                cleared_background_cells = int(exterior_blank.sum())
                if cleared_background_cells:
                    warnings.append(f"已将 {cleared_background_cells} 个与板边相连的无字白格按透明背景留空；带字白格和内部白区已保留，请核对。")
                else:
                    warnings.append("原图含透明区域，但 Image2 返回了不透明图纸；未发现可确认的无字外围白格，未自动清除白格，请核对留空区域。")
            else:
                warnings.append("原图含透明区域，但 Image2 返回了不透明图纸；缺少可靠网格或字码证据，未自动清除白格，请核对留空区域。")
    if not fills:
        raise ValueError("Image2 返回的图纸没有可识别的可见色块")
    counts = Counter(code for row in grid for code in row if code is not None)
    palette = [
        {**colour, "rgb": list(colour["rgb"]), "count": counts[colour["code"]]}
        for colour in BEAD_PALETTE if counts[colour["code"]]
    ]
    palette.sort(key=lambda item: (-item["count"], item["code"]))
    total = sum(counts.values())
    return {
        "grid": grid, "width": width, "height": height, "total": total,
        "empty": width * height - total, "palette": palette, "warnings": warnings,
        "grid_detected": grid_detected,
        "bounds": [float(x_lines[0]), float(y_lines[0]), float(x_lines[-1]), float(y_lines[-1])],
        "uncertain_cells": uncertain,
        "cleared_background_cells": cleared_background_cells,
    }
