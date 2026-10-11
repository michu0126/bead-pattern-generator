import base64
from collections import Counter
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from app import ai, main
from app.palette import BEAD_PALETTE


def png(image):
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def generated_chart(size):
    cell = 18
    colours = {item['code']: item['rgb'] for item in BEAD_PALETTE}
    chart = Image.new('RGBA', (size * cell, size * cell), (*colours['H2'], 255))
    draw = ImageDraw.Draw(chart)
    draw.rectangle((cell, cell, 2 * cell - 1, 2 * cell - 1), fill=(*colours['F5'], 255))
    draw.rectangle((2 * cell, cell, 3 * cell - 1, 2 * cell - 1), fill=(0, 0, 0, 0))
    for position in range(0, size * cell, cell):
        draw.line((position, 0, position, size * cell - 1), fill=(100, 100, 100, 255))
        draw.line((0, position, size * cell - 1, position), fill=(100, 100, 100, 255))
    return png(chart)


@pytest.mark.parametrize('size', [52, 72, 78, 104])
def test_image2_response_has_one_authoritative_grid_and_exact_materials(monkeypatch, size):
    chart = generated_chart(size)
    requests = []

    async def generate(raw, filename, mime_type, width, height):
        requests.append((width, height))
        return chart

    async def forbidden_material_request(*args):
        pytest.fail('Image2 materials must be counted from the grid without a second vision request')

    monkeypatch.setattr(main, 'generate_direct_bead_pattern', generate)
    monkeypatch.setattr(main, 'image_model', lambda: 'test-image-model')
    monkeypatch.setattr(ai, 'extract_direct_pattern_materials', forbidden_material_request)
    original = png(Image.new('RGB', (40, 30), 'white'))
    with TestClient(main.app) as client:
        response = client.post('/api/ai/generate-pattern', files={'image': ('original.png', original, 'image/png')}, data={'board': f'{size}x{size}'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert requests == [(size, size)]
    assert data['width'] == data['height'] == size
    assert len(data['grid']) == size
    assert all(len(row) == size for row in data['grid'])
    assert data['grid'][0][0] == 'H2', 'Opaque edge-connected white must not be removed'
    assert data['grid'][1][1] == 'F5'
    assert data['grid'][1][2] is None
    counts = Counter(code for row in data['grid'] for code in row if code is not None)
    assert counts == {'H2': size * size - 2, 'F5': 1}
    assert dict(counts) == {item['code']: item['count'] for item in data['palette']}
    assert data['total'] == sum(counts.values())
    assert data['empty'] == 1
    assert data['total'] + data['empty'] == size * size
    assert len(data['colours']) == 221
    assert isinstance(data['warnings'], list)
    assert base64.b64decode(data['image'].split(',')[1]) == chart


@pytest.mark.parametrize('failure, message', [
    (ValueError('网格数量不符'), '网格数量不符'),
    (OSError('invalid chart'), '无法读取'),
])
def test_image2_parse_failures_are_explicit_without_local_fallback(monkeypatch, failure, message):
    async def generate(*args):
        return png(Image.new('RGB', (520, 520), 'white'))

    def cannot_parse(*args, **kwargs):
        raise failure

    def cannot_use_original(*args, **kwargs):
        pytest.fail('Malformed AI charts must not silently fall back to local image generation')

    monkeypatch.setattr(main, 'generate_direct_bead_pattern', generate)
    monkeypatch.setattr(main, 'parse_image2_chart', cannot_parse)
    monkeypatch.setattr(main, 'generate_pattern', cannot_use_original)
    with TestClient(main.app) as client:
        response = client.post('/api/ai/generate-pattern', files={'image': ('original.png', png(Image.new('RGB', (20, 20))), 'image/png')})
    assert response.status_code == 502
    assert message in response.json()['detail']


def test_image2_rejects_non_image_result(monkeypatch):
    async def generate(*args):
        return b'not an image'

    monkeypatch.setattr(main, 'generate_direct_bead_pattern', generate)
    with TestClient(main.app) as client:
        response = client.post('/api/ai/generate-pattern', files={'image': ('original.png', png(Image.new('RGB', (20, 20))), 'image/png')})
    assert response.status_code == 502
    assert '无法读取' in response.json()['detail']
