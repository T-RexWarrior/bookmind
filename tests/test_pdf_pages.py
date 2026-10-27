"""Reader regressions: original pixels, text positions, cache and access scope."""
import pymupdf

from test_learning import client, material, space  # shared isolated test fixture
from app import store as db
from app.learning import source_context, verified_selection


def upload_pdf(client, sid):
    with pymupdf.open() as doc:
        page = doc.new_page(width=320, height=480)
        page.draw_rect(pymupdf.Rect(20, 20, 70, 70), color=(1, 0, 0), fill=(1, 0, 0))
        page.insert_text((30, 100), "Original selectable textbook text", fontsize=12)
        # An image-only page must render without requiring OCR or text spans.
        second = doc.new_page(width=320, height=480)
        second.draw_circle((100, 150), 30, color=(0, 0, 1), fill=(0, 0, 1))
        content = doc.tobytes()
    response = client.post(f"/api/spaces/{sid}/materials", files={"file": ("reader.pdf", content, "application/pdf")})
    assert response.status_code == 200
    return response.json()["id"]


def test_compatible_page_preserves_original_pixels_and_reuses_cached_image(client, monkeypatch):
    sid = space(client)
    mid = upload_pdf(client, sid)
    url = f"/api/spaces/{sid}/materials/{mid}/pages/1/image?width=640"
    first = client.get(url)
    assert first.status_code == 200 and first.headers["content-type"] == "image/png"
    pixmap = pymupdf.Pixmap(first.content)
    assert (pixmap.width, pixmap.height) == (640, 960)
    assert pixmap.pixel(80, 80) == (255, 0, 0)
    assert first.headers["cache-control"].startswith("private")
    cached = list((db.DATA / "pdf-pages").rglob("*.png"))
    assert len(cached) == 1
    timestamp = cached[0].stat().st_mtime_ns

    def must_not_render_again(*args, **kwargs):
        raise AssertionError("The second request should reuse the rendered page")

    monkeypatch.setattr(pymupdf, "open", must_not_render_again)
    second = client.get(url)
    assert second.content == first.content and cached[0].stat().st_mtime_ns == timestamp


def test_compatible_text_positions_match_source_and_empty_pages_still_render(client):
    sid = space(client)
    mid = upload_pdf(client, sid)
    prefix = f"/api/spaces/{sid}/materials/{mid}/pages"
    response = client.get(prefix + "/1/layout")
    assert response.status_code == 200
    layout = response.json()
    text = client.get(prefix + "/1").json()["text"]
    assert (layout["width"], layout["height"], layout["rotation"]) == (320, 480, 0)
    assert layout["spans"] and all(span["text"] in text for span in layout["spans"])
    assert all(span["size"] > 0 and span["width"] > 0 for span in layout["spans"])
    assert len(response.content) < 5000
    assert client.get(prefix + "/2/layout").json()["spans"] == []
    assert client.get(prefix + "/2/image").status_code == 200


def test_page_endpoints_reject_wrong_space_invalid_pages_and_unbounded_sizes(client):
    a, b = space(client), space(client)
    mid = upload_pdf(client, a)
    for suffix in ("image", "layout"):
        assert client.get(f"/api/spaces/{b}/materials/{mid}/pages/1/{suffix}").status_code == 404
        for number in (0, 3):
            assert client.get(f"/api/spaces/{a}/materials/{mid}/pages/{number}/{suffix}").status_code == 404
    assert client.get(f"/api/spaces/{a}/materials/{mid}/pages/1/image?width=99999").status_code == 422
    md = material(client, a)
    assert client.get(f"/api/spaces/{a}/materials/{md}/pages/1/image").status_code == 404


def test_copied_text_can_omit_pdf_line_spacing_but_cannot_change_wording(client):
    import pytest
    from fastapi import HTTPException

    assert verified_selection("left,\n root, right", "left,root,right") == "left,\n root, right"
    with pytest.raises(HTTPException) as exc:
        verified_selection("left, root, right", "left, leaf, right")
    assert exc.value.status_code == 422
    sid = space(client)
    mid = material(client, sid)
    context, refs = source_context(sid, {"material_id":mid,"page":1,"selection":"中序遍历中序遍历的顺序"})
    assert "用户划选的原文：中序遍历\n\n中序遍历的顺序" in context
    assert refs[0]["material_id"] == mid
