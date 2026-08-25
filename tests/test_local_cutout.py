from io import BytesIO

from PIL import Image

from app import local_cutout


def test_local_engine_returns_a_transparent_png(monkeypatch):
    source = Image.new("RGB", (4, 3), (20, 40, 60))
    result = Image.new("RGBA", (4, 3), (20, 40, 60, 255))
    result.putpixel((0, 0), (20, 40, 60, 0))

    monkeypatch.setattr(local_cutout, "new_session", lambda model: object())
    monkeypatch.setattr(local_cutout, "remove", lambda image, *, session: result)
    local_cutout._session.cache_clear()

    source_bytes = BytesIO()
    source.save(source_bytes, format="PNG")
    output = local_cutout.remove_background_locally(source_bytes.getvalue())

    output_image = Image.open(BytesIO(output)).convert("RGBA")
    assert output_image.getpixel((0, 0))[3] == 0
    assert output_image.getpixel((1, 1))[3] == 255
