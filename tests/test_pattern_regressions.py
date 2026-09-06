"""Pixel/geometry regression fixtures; no AI or external image services required."""
import numpy as np
import pytest
from PIL import Image, ImageDraw

from app import pattern
from app.palette import BEAD_PALETTE


def rgb(code):
    return next(item['rgb'] for item in BEAD_PALETTE if item['code'] == code)


@pytest.mark.parametrize('size', [(300, 100), (100, 300)])
@pytest.mark.parametrize('board', [52, 72, 78, 104])
def test_rectangular_source_keeps_both_ends(size, board):
    source = Image.new('RGB', size, rgb('B8'))
    draw = ImageDraw.Draw(source)
    if size[0] > size[1]:
        draw.rectangle((0, 0, 29, size[1] - 1), fill=rgb('F5'))
        draw.rectangle((size[0] - 30, 0, size[0] - 1, size[1] - 1), fill=rgb('C8'))
    else:
        draw.rectangle((0, 0, size[0] - 1, 29), fill=rgb('F5'))
        draw.rectangle((0, size[1] - 30, size[0] - 1, size[1] - 1), fill=rgb('C8'))
    cells = pattern._sample_cells(source, board, board, BEAD_PALETTE)
    codes = {BEAD_PALETTE[int(i)]['code'] for i in np.unique(cells) if i >= 0}
    assert codes == {'F5', 'C8', 'B8'}
    assert np.any(cells == -1)  # letterbox instead of stretch/crop
    rows, columns = np.nonzero(cells >= 0)
    ratio = (columns.max() - columns.min() + 1) / (rows.max() - rows.min() + 1)
    assert ratio == pytest.approx(size[0] / size[1], rel=.12)


def test_small_board_focus_cannot_clip_a_wide_subject():
    source = Image.new('RGBA', (300, 100))
    draw = ImageDraw.Draw(source)
    draw.rectangle((10, 42, 289, 57), fill=(*rgb('B8'), 255))
    draw.rectangle((10, 42, 29, 57), fill=(*rgb('F5'), 255))
    draw.rectangle((270, 42, 289, 57), fill=(*rgb('C8'), 255))
    cells = pattern._sample_cells(source, 52, 52, BEAD_PALETTE)
    assert {BEAD_PALETTE[int(i)]['code'] for i in np.unique(cells) if i >= 0} == {'F5', 'C8', 'B8'}


def test_cutout_alpha_preserves_detached_white_foreground():
    source = Image.new('RGBA', (10, 10))
    ImageDraw.Draw(source).rectangle((2, 2, 7, 7), fill=(*rgb('H2'), 255))
    _, materials, grid = pattern.generate_pattern(source, 10, 10)
    assert [(item['code'], item['count']) for item in materials] == [('H2', 36)]
    assert grid[2][2] == 'H2'
    assert grid[0][0] is None


@pytest.mark.parametrize('code', [item['code'] for item in BEAD_PALETTE])
def test_solid_palette_colours_are_not_deleted_as_background(code):
    source = Image.new('RGB', (3, 3), rgb(code))
    cells = pattern._sample_cells(source, 3, 3, BEAD_PALETTE)
    assert np.all(cells == next(i for i, item in enumerate(BEAD_PALETTE) if item['code'] == code))


def test_warm_offwhite_is_not_classified_as_white_background():
    source = Image.new('RGB', (9, 9), rgb('E16'))
    ImageDraw.Draw(source).rectangle((3, 3, 5, 5), fill=rgb('H7'))
    _, _, grid = pattern.generate_pattern(source, 9, 9)
    assert grid[0][0] == 'E16'


def test_invisible_rgb_is_not_sent_to_colour_matching(monkeypatch):
    pixels = np.random.default_rng(42).integers(0, 256, (50, 50, 4), dtype=np.uint8)
    pixels[..., 3] = 0
    pixels[20:30, 20:30] = (*rgb('F5'), 255)
    seen = []
    original = pattern._map_to_palette
    def record(values, palette):
        seen.extend(values.reshape(-1, 3).tolist())
        return original(values, palette)
    monkeypatch.setattr(pattern, '_map_to_palette', record)
    pattern._sample_cells(Image.fromarray(pixels), 10, 10, BEAD_PALETTE)
    assert seen == [list(rgb('F5'))]


def test_unlabelled_alpha_cannot_inflate_cell_coverage():
    index = pattern._choose_cell_palette(
        np.array([[0, -1]]), np.array([[255, 7]], dtype=np.uint8),
        np.array([[.079, .921]]), np.ones((1, 2)), 1., len(BEAD_PALETTE),
    )
    assert index == -1


def test_nearest_colour_distance_batches_are_bounded(monkeypatch):
    values = np.random.default_rng(8).integers(0, 256, (1200, 3), dtype=np.uint8)
    sizes = []
    original = pattern._delta_e_2000
    def record(a, b):
        sizes.append(a.shape[0])
        return original(a, b)
    monkeypatch.setattr(pattern, '_delta_e_2000', record)
    mapped = pattern._map_to_palette(values, BEAD_PALETTE)
    assert mapped.shape == (1200,)
    assert max(sizes) <= 512


def test_equal_area_tie_uses_true_distance_even_outside_central_circle():
    source = Image.new('RGBA', (8, 8))
    for x, y in [(0, 0), (0, 7), (7, 7)]:
        source.putpixel((x, y), (*rgb('F5'), 255))
    for x, y in [(1, 0), (6, 0), (6, 7)]:
        source.putpixel((x, y), (*rgb('C8'), 255))
    _, _, grid = pattern.generate_pattern(source, 1, 1)
    assert grid == [['C8']]


def test_batched_colour_matching_agrees_with_full_distance_reference():
    values = np.random.default_rng(7).integers(0, 256, (1300, 3), dtype=np.uint8)
    palette_rgb = np.array([item['rgb'] for item in BEAD_PALETTE], dtype=np.uint8)
    full = pattern._delta_e_2000(pattern._srgb_to_lab(values)[:, None], pattern._srgb_to_lab(palette_rgb)[None])
    np.testing.assert_array_equal(pattern._map_to_palette(values, BEAD_PALETTE), np.argmin(full, axis=1))


def test_fully_transparent_source_remains_empty():
    with pytest.raises(ValueError, match='没有可用的前景'):
        pattern.generate_pattern(Image.new('RGBA', (32, 32)), 52, 52)
