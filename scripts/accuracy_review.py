"""Read-only accuracy probes against the current local sampler.

Run with the project Python environment. No network or API calls are performed.
"""
from collections import Counter
from pathlib import Path
import subprocess
import sys
import time
import types

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import pattern
from app.palette import BEAD_PALETTE

COLOURS = {item['code']: item['rgb'] for item in BEAD_PALETTE}


def probes():
    image = Image.new('RGB', (200, 200), COLOURS['E3'])
    draw = ImageDraw.Draw(image)
    draw.rectangle((54, 74, 65, 85), fill=COLOURS['H7'])
    draw.rectangle((134, 74, 145, 85), fill=COLOURS['H7'])
    yield 'two_12px_eyes_on_pink_20px_cells', image, 10
    image = Image.new('RGB', (200, 200), COLOURS['E3'])
    ImageDraw.Draw(image).rectangle((78, 20, 81, 179), fill=COLOURS['H7'])
    yield '4px_black_line_on_opaque_pink_20px_cells', image, 10
    image = Image.new('RGBA', (200, 200))
    ImageDraw.Draw(image).rectangle((78, 20, 81, 179), fill=(*COLOURS['H7'], 255))
    yield 'same_black_line_on_transparency_20px_cells', image, 10


if __name__ == '__main__':
    for name, image, size in probes():
        cells = pattern._sample_cells(image, size, size, BEAD_PALETTE)
        counts = Counter(BEAD_PALETTE[int(i)]['code'] if i >= 0 else None for i in cells.flat)
        print(name, dict(counts))
    if len(sys.argv) > 1:
        image = Image.open(sys.argv[1])
        baseline = types.ModuleType('app.pattern_accuracy_baseline')
        baseline.__package__ = 'app'
        exec(compile(subprocess.check_output(['git', 'show', 'HEAD:app/pattern.py']),
                     '<baseline pattern.py>', 'exec'), baseline.__dict__)
        for board in (52, 72, 78, 104):
            start = time.perf_counter()
            before = baseline._sample_cells(image, board, board, BEAD_PALETTE)
            old_seconds = time.perf_counter() - start
            start = time.perf_counter()
            after = pattern._sample_cells(image, board, board, BEAD_PALETTE)
            new_seconds = time.perf_counter() - start
            changes = [(tuple(position.tolist()),
                        BEAD_PALETTE[int(before[tuple(position)])]['code'] if before[tuple(position)] >= 0 else None,
                        BEAD_PALETTE[int(after[tuple(position)])]['code'] if after[tuple(position)] >= 0 else None)
                       for position in np.argwhere(before != after)]
            print('board', board, 'seconds', round(old_seconds, 3), round(new_seconds, 3),
                  'changes', changes)
        pixels = pattern._prepare_source(image)
        if np.all(pixels[..., 3] == 255):
            pixels[pattern._discardable_white_mask(pixels), 3] = 0
        labels = pattern._pixel_palette_indices(pixels[..., :3], BEAD_PALETTE, pixels[..., 3] >= 8)
        exact_keys = np.array([int(item['hex'][1:], 16) for item in BEAD_PALETTE])
        exact = np.isin(pattern._packed_rgb(pixels[..., :3]), exact_keys)
        cells = pattern._sample_cells(image, 52, 52, BEAD_PALETTE)
        print('source', image.size, 'grid', dict(Counter(BEAD_PALETTE[int(i)]['code'] if i >= 0 else None for i in cells.flat)))
        for label in np.unique(cells):
            if label < 0:
                continue
            mask = labels == label
            component_map, components = ndimage.label(mask)
            sizes = np.bincount(component_map.ravel())[1:]
            print(BEAD_PALETTE[int(label)]['code'], 'source_pixels', int(mask.sum()),
                  'exact_pixels', int((mask & exact).sum()), 'components', components,
                  'largest', sorted(sizes.tolist(), reverse=True)[:5])
