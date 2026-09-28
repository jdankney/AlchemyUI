"""Every image carries three dominant colours, so the UI can make it glow in
its own light without blurring the image itself on the client."""

from __future__ import annotations

import io
import re

from PIL import Image

from alchemyui.storage import palette_of

HEX = re.compile(r"^#[0-9a-f]{6}$")


def _png(draw) -> bytes:
    im = Image.new("RGB", (64, 64), (10, 10, 10))
    draw(im)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _close(a: str, b: tuple[int, int, int], tol: int = 40) -> bool:
    rgb = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    return all(abs(x - y) <= tol for x, y in zip(rgb, b, strict=True))


def test_palette_finds_the_colours_that_are_there():
    def draw(im):
        im.paste((230, 60, 40), (0, 0, 32, 64))     # red half
        im.paste((40, 90, 230), (32, 0, 64, 64))    # blue half
    pal = palette_of(_png(draw))
    assert len(pal) == 3 and all(HEX.match(c) for c in pal)
    assert any(_close(c, (230, 60, 40)) for c in pal)
    assert any(_close(c, (40, 90, 230)) for c in pal)


def test_palette_puts_vivid_colours_before_dull_ones():
    def draw(im):
        im.paste((128, 128, 128), (0, 0, 64, 48))   # mostly grey
        im.paste((250, 180, 20), (0, 48, 64, 64))   # a strip of amber
    pal = palette_of(_png(draw))
    assert _close(pal[0], (250, 180, 20))


def test_palette_of_something_that_is_not_an_image_is_empty():
    assert palette_of(b"not an image") == []
    assert palette_of(b"") == []


def test_a_finished_job_stores_its_palette(client):
    from tests.conftest import wait_for

    r = client.post("/api/generate", json={"workflow_id": "txt2img-sdxl", "prompt": "a lighthouse"})
    assert r.status_code == 200, r.text
    images = wait_for(client, lambda: client.get("/api/images").json()["images"])
    assert all(HEX.match(c) for c in images[0]["palette"])


async def test_backfill_fills_rows_that_have_no_palette(tmp_db_path, tmp_path):
    from alchemyui.db import Database
    from alchemyui.storage import backfill_palettes

    db = Database(tmp_db_path)
    f = tmp_path / "x.png"
    f.write_bytes(_png(lambda im: im.paste((200, 30, 160), (0, 0, 64, 64))))
    await db.execute("INSERT INTO jobs (id, workflow_id, status, params_json, created_at) "
                     "VALUES ('j1', 'txt2img-sdxl', 'completed', '{}', 1)")
    await db.execute(
        "INSERT INTO images (id, job_id, filename, path, workflow_id, params_json, created_at) "
        "VALUES ('i1', 'j1', 'x.png', ?, 'txt2img-sdxl', '{}', 1)", (str(f),))
    await db.execute(
        "INSERT INTO images (id, job_id, filename, path, workflow_id, params_json, created_at) "
        "VALUES ('i2', 'j1', 'gone.png', '/nope/gone.png', 'txt2img-sdxl', '{}', 1)")
    assert await backfill_palettes(db) == 1
    row = await db.fetch_one("SELECT palette FROM images WHERE id = 'i1'")
    assert _close(__import__("json").loads(row["palette"])[0], (200, 30, 160))
    # a missing file is marked so it is not retried on every boot
    assert (await db.fetch_one("SELECT palette FROM images WHERE id = 'i2'"))["palette"] == "[]"
    db.close()
