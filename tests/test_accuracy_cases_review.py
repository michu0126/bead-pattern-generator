"""Detail-preservation probes: preserve source labels, never invent blended RGB."""
import numpy as np
from PIL import Image, ImageDraw

from app import pattern
from app.palette import BEAD_PALETTE


COLOURS = {item['code']: item['rgb'] for item in BEAD_PALETTE}
INDICES = {item['code']: i for i, item in enumerate(BEAD_PALETTE)}


def test_two_same_colour_eyes_straddling_grid_intersections_each_survive():
    source = Image.new('RGB', (200, 200), COLOURS['E3'])
    draw = ImageDraw.Draw(source)
    # Each eye is 0.36 cell in area, divided equally across four cells (9% each).
    # Per-colour rather than per-component accounting would preserve only one eye.
    draw.rectangle((54, 74, 65, 85), fill=COLOURS['H7'])
    draw.rectangle((134, 74, 145, 85), fill=COLOURS['H7'])
    cells = pattern._sample_cells(source, 10, 10, BEAD_PALETTE)
    assert np.sum(cells[3:5, 2:4] == INDICES['H7']) == 1
    assert np.sum(cells[3:5, 6:8] == INDICES['H7']) == 1
    assert set(np.unique(cells)) == {INDICES['E3'], INDICES['H7']}


def test_larger_existing_black_outline_does_not_suppress_missing_black_eyes():
    source = Image.new('RGB', (200, 200), COLOURS['E3'])
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 199, 199), outline=COLOURS['H7'], width=20)
    draw.rectangle((54, 74, 65, 85), fill=COLOURS['H7'])
    draw.rectangle((134, 74, 145, 85), fill=COLOURS['H7'])
    cells = pattern._sample_cells(source, 10, 10, BEAD_PALETTE)
    assert np.sum(cells[3:5, 2:4] == INDICES['H7']) == 1
    assert np.sum(cells[3:5, 6:8] == INDICES['H7']) == 1
    assert np.all(cells[0] == INDICES['H7'])


def test_low_contrast_subcell_highlight_is_not_promoted_to_a_full_bead():
    source = Image.new('RGB', (200, 200), COLOURS['H2'])
    ImageDraw.Draw(source).rectangle((44, 44, 55, 55), fill=COLOURS['H1'])
    cells = pattern._sample_cells(source, 10, 10, BEAD_PALETTE)
    assert np.all(cells == INDICES['H2'])


def test_single_source_pixel_noise_is_not_promoted_when_downsampling():
    source = Image.new('RGB', (200, 200), COLOURS['E3'])
    source.putpixel((55, 55), COLOURS['H7'])
    cells = pattern._sample_cells(source, 10, 10, BEAD_PALETTE)
    assert np.all(cells == INDICES['E3'])


def test_exact_one_to_one_full_palette_is_preserved_with_authoritative_alpha():
    # Existing transparency is authoritative; white bead codes are foreground.
    pixels = np.array([[(*item['rgb'], 255) for item in BEAD_PALETTE]], dtype=np.uint8)
    pixels[0, 0, 3] = 254
    cells = pattern._sample_cells(Image.fromarray(pixels), len(BEAD_PALETTE), 1, BEAD_PALETTE)
    np.testing.assert_array_equal(cells[0], np.arange(len(BEAD_PALETTE)))


def test_existing_small_feature_is_not_grown_by_the_rescue_pass():
    source = Image.new('RGB', (200, 200), COLOURS['E3'])
    # 16 x 16 pixels centered within one 20 x 20 bead already wins that cell.
    ImageDraw.Draw(source).rectangle((42, 42, 57, 57), fill=COLOURS['H7'])
    cells = pattern._sample_cells(source, 10, 10, BEAD_PALETTE)
    assert np.sum(cells == INDICES['H7']) == 1


def test_transparent_fringe_is_not_promoted_as_a_closed_eye():
    source = Image.new('RGBA', (200, 200), (*COLOURS['E3'], 255))
    draw = ImageDraw.Draw(source)
    draw.rectangle((54, 74, 65, 85), fill=(*COLOURS['H7'], 255))
    # Touching transparency indicates a boundary feature, not enclosed detail.
    draw.rectangle((52, 72, 53, 87), fill=(*COLOURS['H7'], 0))
    cells = pattern._sample_cells(source, 10, 10, BEAD_PALETTE)
    assert not np.any(cells == INDICES['H7'])


def test_fragmented_image_only_allocates_bounding_boxes_for_eligible_features(monkeypatch):
    # Hundreds of disconnected one-pixel specks and one eligible feature. The
    # production 4096-square equivalent contains millions of specks: find_objects
    # must not allocate Python slice tuples for each irrelevant component.
    labels = np.full((64, 64), INDICES['E3'], dtype=np.int16)
    labels[::3, ::3] = INDICES['H7']
    labels[20:26, 20:26] = INDICES['H7']
    alpha = np.full(labels.shape, 255, dtype=np.uint8)
    cells = np.full((8, 8), INDICES['E3'], dtype=np.int16)
    counts = []
    original = pattern.ndimage.find_objects

    def bounded_objects(values, *args, **kwargs):
        boxes = original(values, *args, **kwargs)
        counts.append(len(boxes))
        return boxes

    monkeypatch.setattr(pattern.ndimage, 'find_objects', bounded_objects)
    pattern._preserve_small_details(cells, labels, alpha, BEAD_PALETTE, (0, 0, 64, 64))
    assert not counts or max(counts) <= 4
