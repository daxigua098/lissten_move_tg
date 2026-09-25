"""图片上传接口测试。"""

from __future__ import annotations

import io

from conftest import ADMIN_API_TOKEN, auth_header, login
from PIL import Image


def _image_bytes(fmt: str = "PNG", size: tuple[int, int] = (64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (220, 40, 40)).save(buffer, format=fmt)
    return buffer.getvalue()


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_upload_image_and_access_url(admin_client, api_config) -> None:
    response = await admin_client.post(
        "/api/uploads/image",
        headers=_headers(),
        files={"file": ("广告图.png", _image_bytes(), "image/png")},
    )

    body = response.json()
    assert response.status_code == 201
    assert body["path"].startswith("assets/uploads/")
    assert body["path"].endswith(".png")
    assert body["url"] == f"/uploads/{body['filename']}"
    assert body["width"] == 64
    assert body["height"] == 48
    assert body["original_name"] == "广告图.png"
    assert (api_config.project_root / body["path"]).is_file()

    served = await admin_client.get(body["url"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")


async def test_uploaded_image_can_be_referenced_by_ad_asset(admin_client) -> None:
    upload = await admin_client.post(
        "/api/uploads/image",
        headers=_headers(),
        files={"file": ("banner.jpg", _image_bytes("JPEG"), "image/jpeg")},
    )
    uploaded_path = upload.json()["path"]
    assert uploaded_path.endswith(".jpg")

    asset = await admin_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "带图的素材", "text": "文案", "image_path": uploaded_path},
    )

    assert asset.status_code == 201
    assert asset.json()["image_path"] == uploaded_path


async def test_upload_rejects_non_image(admin_client) -> None:
    response = await admin_client.post(
        "/api/uploads/image",
        headers=_headers(),
        files={"file": ("notes.txt", b"just some text", "text/plain")},
    )

    assert response.status_code == 400
    assert "有效的图片" in response.json()["detail"]


async def test_upload_rejects_oversize(admin_client) -> None:
    from app.services.upload_service import MAX_UPLOAD_BYTES

    payload = _image_bytes() + b"\x00" * (MAX_UPLOAD_BYTES + 1)

    response = await admin_client.post(
        "/api/uploads/image",
        headers=_headers(),
        files={"file": ("big.png", payload, "image/png")},
    )

    assert response.status_code == 400
    assert "MB" in response.json()["detail"]


async def test_upload_requires_permission(admin_client, api_config) -> None:
    anonymous = await admin_client.post(
        "/api/uploads/image",
        files={"file": ("a.png", _image_bytes(), "image/png")},
    )
    assert anonymous.status_code == 401

    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        await user_service.create_user(
            session,
            api_config,
            username="erge",
            password="Pass1234",
            role="viewer",
            must_change_password=False,
        )
    token = (await login(admin_client, username="erge", password="Pass1234")).json()["token"]

    forbidden = await admin_client.post(
        "/api/uploads/image",
        headers=auth_header(token),
        files={"file": ("a.png", _image_bytes(), "image/png")},
    )
    assert forbidden.status_code == 403


async def test_delete_uploaded_image(admin_client) -> None:
    upload = await admin_client.post(
        "/api/uploads/image",
        headers=_headers(),
        files={"file": ("c.png", _image_bytes(), "image/png")},
    )
    filename = upload.json()["filename"]

    removed = await admin_client.delete(f"/api/uploads/image/{filename}", headers=_headers())
    assert removed.status_code == 200
    assert removed.json()["deleted"] is True

    missing = await admin_client.get(f"/uploads/{filename}")
    assert missing.status_code == 404

    again = await admin_client.delete(f"/api/uploads/image/{filename}", headers=_headers())
    assert again.status_code == 400
