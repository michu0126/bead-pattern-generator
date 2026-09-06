import base64
from collections import Counter
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest

from app.main import app
from app.palette import BEAD_PALETTE


@pytest.mark.parametrize('size', [52, 72, 78, 104])
def test_local_generate_response_grid_and_materials_stay_consistent(size):
    colours = {item['code']: item['rgb'] for item in BEAD_PALETTE}
    source = Image.new('RGBA', (300, 100))
    draw = ImageDraw.Draw(source)
    draw.rectangle((10, 10, 289, 89), fill=(*colours['H2'], 255))
    draw.rectangle((10, 10, 29, 89), fill=(*colours['F5'], 255))
    draw.rectangle((270, 10, 289, 89), fill=(*colours['C8'], 255))
    raw = BytesIO()
    source.save(raw, format='PNG')
    with TestClient(app) as client:
        response = client.post('/api/generate', files={'image': ('cutout.png', raw.getvalue(), 'image/png')}, data={'board': f'{size}x{size}'})
    assert response.status_code == 200
    data = response.json()
    assert len(data['grid']) == size
    assert all(len(row) == size for row in data['grid'])
    counts = Counter(code for row in data['grid'] for code in row if code is not None)
    assert set(counts) == {'F5', 'C8', 'H2'}
    assert dict(counts) == {item['code']: item['count'] for item in data['palette']}
    assert data['total'] == sum(counts.values())
    assert data['total'] + data['empty'] == size * size
    assert len(data['colours']) == 221
    image_bytes = base64.b64decode(data['image'].split(',')[1])
    assert image_bytes.startswith(b'\x89PNG')
    with Image.open(BytesIO(image_bytes)) as rendered:
        assert rendered.width == rendered.height


def test_local_generate_rejects_empty_alpha_with_readable_error():
    raw = BytesIO()
    Image.new('RGBA', (10, 10)).save(raw, format='PNG')
    with TestClient(app) as client:
        response = client.post('/api/generate', files={'image': ('empty.png', raw.getvalue(), 'image/png')}, data={'board': '52x52'})
    assert response.status_code == 400
    assert '没有可用的前景' in response.json()['detail']
