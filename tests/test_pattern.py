from PIL import Image

from app.palette import BEAD_PALETTE
from app.pattern import generate_pattern


def test_generate_pattern_returns_png_and_correct_total():
    image = Image.new("RGB", (64, 64), (220, 50, 50))
    output, summary = generate_pattern(image, 16, 12)
    assert output.startswith(b"\x89PNG")
    assert sum(item["count"] for item in summary) == 16 * 12
    assert len(summary) >= 1


def test_transparent_pixels_are_empty_cells():
    image = Image.new("RGBA", (10, 10), (20, 100, 200, 255))
    for y in range(10):
        for x in range(5):
            image.putpixel((x, y), (20, 100, 200, 0))
    output, summary = generate_pattern(image, 10, 10)
    assert output.startswith(b"\x89PNG")
    assert sum(item["count"] for item in summary) == 50


def test_mard_classic_palette_contains_221_colours():
    assert len(BEAD_PALETTE) == 221
    assert len({item["code"] for item in BEAD_PALETTE}) == 221
