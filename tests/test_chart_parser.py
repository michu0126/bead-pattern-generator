from collections import Counter

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.chart_parser import parse_image2_chart
from app.palette import BEAD_PALETTE
from app.pattern import _map_to_palette


COLOURS = {item["code"]: item for item in BEAD_PALETTE}


def render_chart(grid, *, margin=(0, 0), cell=32, labels=True, transparent=False):
    rows, columns = len(grid), len(grid[0])
    mx, my = margin
    image = Image.new("RGBA", (columns * cell + 2 * mx + 1, rows * cell + 2 * my + 1), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(8, int(cell * 0.35)))
    for row in range(rows):
        for column in range(columns):
            code = grid[row][column]
            colour = tuple(COLOURS[code]["rgb"]) + (255,) if code else (255, 255, 255, 0 if transparent else 255)
            x, y = mx + column * cell, my + row * cell
            draw.rectangle((x, y, x + cell, y + cell), fill=colour)
            if code and labels:
                ink = "white" if sum(colour[:3]) < 380 else "black"
                draw.text((x + cell / 2, y + cell / 2), code, font=font, anchor="mm", fill=ink)
    for column in range(columns + 1):
        x = mx + column * cell
        draw.line((x, my, x, my + rows * cell), fill=(130, 130, 130), width=1)
    for row in range(rows + 1):
        y = my + row * cell
        draw.line((mx, y, mx + columns * cell, y), fill=(130, 130, 130), width=1)
    return image


@pytest.mark.parametrize("margin", [(0, 0), (19, 27), (47, 31)])
def test_recovers_cell_fills_without_grid_or_code_ink(margin):
    grid = [["H7", "H2", "D12", "C8", "F5", "H1"] for _ in range(6)]
    result = parse_image2_chart(render_chart(grid, margin=margin), 6, 6)
    assert result["grid_detected"]
    assert result["grid"] == grid
    assert result["bounds"] == [margin[0], margin[1], margin[0] + 192, margin[1] + 192]
    assert not result["warnings"]
    counts = Counter(code for row in grid for code in row)
    assert {item["code"]: item["count"] for item in result["palette"]} == counts
    assert result["total"] == 36
    assert result["empty"] == 0


@pytest.mark.parametrize("count", [52, 72, 78, 104])
def test_requested_real_board_sizes(count):
    grid = [["H7" if (x + y) % 3 == 0 else "D12" if x % 3 == 1 else "H2" for x in range(count)] for y in range(count)]
    result = parse_image2_chart(render_chart(grid, cell=14, margin=(13, 17)), count, count)
    assert result["grid_detected"]
    assert result["grid"] == grid


def test_white_subject_at_outer_edge_is_never_erased():
    grid = [["H2"] * 6 for _ in range(6)]
    grid[2][2] = "D12"
    result = parse_image2_chart(render_chart(grid), 6, 6)
    assert result["grid"] == grid
    assert result["empty"] == 0


def test_all_221_exact_mard_fills_roundtrip_with_code_ink():
    codes = list(COLOURS)
    grid = [codes[start:start + 13] for start in range(0, 221, 13)]
    result = parse_image2_chart(render_chart(grid, cell=24, margin=(21, 19)), 13, 17)
    assert result["grid_detected"]
    assert result["grid"] == grid


def test_returned_real_alpha_preserves_empty_cells_and_internal_white():
    grid = [[None] * 6 for _ in range(6)]
    for row in range(1, 5):
        for column in range(1, 5):
            grid[row][column] = "H2" if row == 2 else "D12"
    result = parse_image2_chart(render_chart(grid, transparent=True), 6, 6)
    assert result["grid"] == grid
    assert result["empty"] == 20


def test_source_alpha_is_not_assumed_aligned_to_ai_composition():
    source = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((10, 10, 40, 40), fill="white")
    grid = [["H2"] * 6 for _ in range(6)]
    result = parse_image2_chart(render_chart(grid), 6, 6, source_image=source)
    assert result["grid"] == grid
    assert any("未自动清除白格" in warning for warning in result["warnings"])


def test_transparent_source_allows_only_unlabelled_exterior_white_to_be_empty():
    grid = [[None] * 6 for _ in range(6)]
    for row in range(1, 5):
        for column in range(1, 5):
            grid[row][column] = "H7"
    grid[0][2] = "H2"  # Even a white subject on the board edge must survive.
    grid[2][2] = None  # Unlabelled but enclosed white inside the subject.
    source = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    ImageDraw.Draw(source).rectangle((15, 15, 45, 45), fill="white")
    result = parse_image2_chart(render_chart(grid), 6, 6, source_image=source)
    expected = [row.copy() for row in grid]
    expected[2][2] = "H2"
    assert result["grid"] == expected
    assert result["cleared_background_cells"] == 19
    assert result["empty"] == 19
    assert result["total"] == 17


def test_same_unlabelled_exterior_white_is_retained_for_opaque_source():
    grid = [[None] * 6 for _ in range(6)]
    grid[2][2] = "D12"
    result = parse_image2_chart(render_chart(grid), 6, 6, source_image=Image.new("RGB", (60, 60), "white"))
    assert result["total"] == 36
    assert result["empty"] == 0
    assert result["cleared_background_cells"] == 0


def test_transparent_source_without_any_label_evidence_does_not_guess_blanks():
    grid = [[None] * 6 for _ in range(6)]
    grid[2][2] = "D12"
    result = parse_image2_chart(
        render_chart(grid, labels=False), 6, 6,
        source_image=Image.new("RGBA", (60, 60), (0, 0, 0, 0)),
    )
    assert result["total"] == 36
    assert result["cleared_background_cells"] == 0
    assert any("缺少可靠网格或字码证据" in warning for warning in result["warnings"])


def test_detected_wrong_grid_count_is_rejected_instead_of_rescaled():
    grid = [["D12", "H2"] * 24 for _ in range(48)]
    with pytest.raises(ValueError, match="48 × 48.*52 × 52"):
        parse_image2_chart(render_chart(grid, cell=20, margin=(17, 25)), 52, 52)


def test_valid_grid_at_canvas_edge_may_omit_the_final_outer_stroke():
    grid = [["H7", "H2", "D12", "C8", "F5", "H1"] for _ in range(6)]
    image = render_chart(grid)
    image = image.crop((0, 0, image.width - 1, image.height - 1))
    result = parse_image2_chart(image, 6, 6)
    assert result["grid_detected"]
    assert result["grid"] == grid


def test_missing_grid_reports_approximate_fallback_and_uses_ciede2000():
    rgb = (92, 139, 153)
    expected = BEAD_PALETTE[int(_map_to_palette(np.array([[rgb]], dtype=np.uint8), BEAD_PALETTE)[0, 0])]["code"]
    result = parse_image2_chart(Image.new("RGB", (120, 120), rgb), 6, 6)
    assert result["grid"] == [[expected] * 6 for _ in range(6)]
    assert not result["grid_detected"]
    assert any("未能可靠定位完整网格" in warning for warning in result["warnings"])


def test_jpeg_noise_and_codes_do_not_produce_extra_colours():
    from io import BytesIO

    grid = [["H7", "H2", "D12", "C8", "F5", "H2"] for _ in range(6)]
    buffer = BytesIO()
    render_chart(grid, cell=40, margin=(25, 33)).convert("RGB").save(buffer, format="JPEG", quality=85)
    result = parse_image2_chart(Image.open(BytesIO(buffer.getvalue())), 6, 6)
    assert result["grid_detected"]
    assert result["grid"] == grid


def test_too_small_or_invisible_chart_is_rejected():
    with pytest.raises(ValueError, match="分辨率过低"):
        parse_image2_chart(Image.new("RGB", (10, 10)), 52, 52)
    with pytest.raises(ValueError, match="没有可识别"):
        parse_image2_chart(Image.new("RGBA", (120, 120), (0, 0, 0, 0)), 6, 6)
    with pytest.raises(ValueError, match="超过 4096"):
        parse_image2_chart(Image.new("RGBA", (4097, 1)), 6, 6)


def test_transparent_outer_margin_does_not_hide_opaque_empty_cells():
    grid = [[None] * 6 for _ in range(6)]
    grid[2][2] = "D12"
    image = render_chart(grid, margin=(13, 13))
    alpha = Image.new("L", image.size, 0)
    ImageDraw.Draw(alpha).rectangle((13, 13, 13 + 192, 13 + 192), fill=255)
    image.putalpha(alpha)
    source = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    result = parse_image2_chart(image, 6, 6, source_image=source)
    assert result["grid_detected"]
    assert result["grid"] == grid
    assert result["cleared_background_cells"] == 35


def test_tiny_cells_warn_about_ink_covering_the_fill():
    image = render_chart([["D12"] * 6 for _ in range(6)], cell=6, labels=False)
    result = parse_image2_chart(image, 6, 6)
    assert any("像素尺寸过小" in warning for warning in result["warnings"])
