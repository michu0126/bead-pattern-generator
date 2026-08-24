import numpy as np
from PIL import Image, ImageDraw

from app.palette import BEAD_PALETTE
from app.pattern import _map_to_palette, generate_pattern


def test_generate_pattern_returns_png_and_correct_total():
    image = Image.new("RGB", (64, 64), (220, 50, 50))
    output, summary, grid = generate_pattern(image, 16, 12)
    assert output.startswith(b"\x89PNG")
    assert sum(item["count"] for item in summary) == 16 * 12
    assert len(summary) >= 1
    assert len(grid) == 12
    assert len(grid[0]) == 16


def test_transparent_pixels_are_empty_cells():
    image = Image.new("RGBA", (10, 10), (20, 100, 200, 255))
    for y in range(10):
        for x in range(5):
            image.putpixel((x, y), (20, 100, 200, 0))
    output, summary, grid = generate_pattern(image, 10, 10)
    assert output.startswith(b"\x89PNG")
    assert sum(item["count"] for item in summary) == 50
    assert sum(code is None for row in grid for code in row) == 50


def test_mard_classic_palette_contains_221_colours():
    assert len(BEAD_PALETTE) == 221
    assert len({item["code"] for item in BEAD_PALETTE}) == 221


def test_exterior_white_is_empty_but_enclosed_white_is_kept():
    image = Image.new("RGB", (9, 9), "white")
    for y in range(2, 7):
        for x in range(2, 7):
            image.putpixel((x, y), (30, 30, 30))
    image.putpixel((4, 4), (255, 255, 255))
    output, summary, grid = generate_pattern(image, 9, 9)
    assert output.startswith(b"\x89PNG")
    assert sum(item["count"] for item in summary) == 25
    assert grid[4][4] is not None


def test_neutral_pixels_only_match_neutral_palette_entries():
    pixels = np.array([[123, 123, 123], [72, 72, 72], [205, 205, 205]], dtype=np.uint8)
    indices = _map_to_palette(pixels, BEAD_PALETTE)
    codes = [BEAD_PALETTE[int(index)]["code"] for index in indices]
    assert all(code.startswith("H") for code in codes)


def test_antialiased_black_and_white_do_not_create_tinted_greys():
    image = Image.new("RGB", (400, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((55, 55, 345, 345), fill="black")
    draw.ellipse((135, 135, 265, 265), fill="white")
    _, summary, _ = generate_pattern(image, 20, 20)
    codes = {item["code"] for item in summary}
    assert all(code.startswith("H") for code in codes)
    assert "H7" in codes
    assert "H2" in codes


def test_thin_black_line_survives_block_sampling():
    image = Image.new("RGBA", (160, 160), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((78, 0, 81, 159), fill=(0, 0, 0, 255))
    _, summary, grid = generate_pattern(image, 10, 10)
    assert any(item["code"] == "H7" for item in summary)
    assert any(code == "H7" for row in grid for code in row)


def test_solid_pink_is_not_split_into_neutral_or_brown_families():
    image = Image.new("RGBA", (320, 320), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 280, 280), fill=(245, 155, 220, 255))
    draw.rectangle((40, 40, 280, 280), outline=(0, 0, 0, 255), width=14)
    _, summary, _ = generate_pattern(image, 16, 16)
    chromatic_codes = {item["code"] for item in summary if not item["code"].startswith("H")}
    assert chromatic_codes
    assert all(code.startswith("E") for code in chromatic_codes)
    assert len(chromatic_codes) <= 2
