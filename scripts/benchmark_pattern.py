"""Reproducible local-only baseline comparison (no API/model calls).

Run from the repository: python scripts/benchmark_pattern.py
Each measurement uses a fresh process. Source files are never modified.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def peak_mib():
    if sys.platform == 'win32':
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
                    'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage',
                )
            ]
        handle = ctypes.windll.kernel32.GetCurrentProcess
        handle.restype = wintypes.HANDLE
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if query(handle(), ctypes.byref(counters), counters.cb):
            return round(counters.PeakWorkingSetSize / 1024**2, 1)
        return None
    import resource
    amount = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(amount / (1024**2 if sys.platform == 'darwin' else 1024), 1)


def fixture(name):
    from PIL import Image, ImageDraw
    import numpy as np
    from app.palette import BEAD_PALETTE
    colours = {item['code']: item['rgb'] for item in BEAD_PALETTE}
    if name == 'wide':
        image = Image.new('RGB', (900, 300), colours['B8'])
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 89, 299), fill=colours['F5'])
        draw.rectangle((810, 0, 899, 299), fill=colours['C8'])
    elif name == 'white_cutout':
        image = Image.new('RGBA', (600, 600))
        draw = ImageDraw.Draw(image)
        draw.ellipse((0, 0, 599, 599), fill=(*colours['H2'], 255))
        draw.ellipse((130, 180, 200, 250), fill=(*colours['H7'], 255))
        draw.ellipse((400, 180, 470, 250), fill=(*colours['H7'], 255))
    elif name == 'hidden_rgb':
        pixels = np.random.default_rng(45).integers(0, 256, (256, 256, 4), dtype=np.uint8)
        pixels[..., 3] = 0
        pixels[80:176, 80:176] = (*colours['F5'], 255)
        image = Image.fromarray(pixels)
    elif name == 'white_canvas':
        image = Image.new('RGB', (1200, 1200), 'white')
        draw = ImageDraw.Draw(image)
        draw.ellipse((260, 180, 940, 860), fill=colours['E9'], outline='black', width=24)
        draw.rectangle((572, 840, 628, 1080), fill=colours['B8'])
    else:
        image = Image.open(name)
    return image


def worker(args):
    import numpy as np
    from PIL import Image
    from app.palette import BEAD_PALETTE
    if args.engine == 'baseline':
        code = subprocess.check_output(['git', 'show', args.baseline_ref + ':app/pattern.py'], cwd=ROOT)
        engine = types.ModuleType('app.pattern_baseline')
        engine.__package__ = 'app'
        exec(compile(code, '<baseline pattern.py>', 'exec'), engine.__dict__)
    else:
        engine = importlib.import_module('app.pattern')
    image = fixture(args.fixture)
    started = time.perf_counter()
    cells = engine._sample_cells(image, 52, 52, BEAD_PALETTE)
    elapsed = time.perf_counter() - started
    result = dict(fixture=args.fixture, engine=args.engine, seconds=round(elapsed, 3),
                  peak_mib=peak_mib(), occupied=int(np.sum(cells >= 0)),
                  colours=[BEAD_PALETTE[int(i)]['code'] for i in np.unique(cells) if i >= 0])
    if args.output_dir:
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        name = Path(args.fixture).stem
        output.joinpath(f'{name}-{args.engine}.png').write_bytes(engine._render_pattern(cells, BEAD_PALETTE))
        preview = np.full((*cells.shape, 3), 250, dtype=np.uint8)
        for i in np.unique(cells):
            if i >= 0:
                preview[cells == i] = BEAD_PALETTE[int(i)]['rgb']
        Image.fromarray(preview).resize((520, 520), Image.Resampling.NEAREST).save(output / f'{name}-{args.engine}-preview.png')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-ref', default='1d811c20b6eb493475bebaa74b4a40a7a5c02310')
    parser.add_argument('--engine', choices=['baseline', 'optimized'])
    parser.add_argument('--fixture')
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    if args.engine:
        worker(args)
    else:
        for case in ([args.fixture] if args.fixture else ['wide', 'white_cutout', 'hidden_rgb', 'white_canvas']):
            for engine in ['baseline', 'optimized']:
                command = [sys.executable, __file__, '--engine', engine, '--fixture', case, '--baseline-ref', args.baseline_ref]
                if args.output_dir:
                    command.extend(['--output-dir', args.output_dir])
                subprocess.run(command, check=True, cwd=ROOT, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
