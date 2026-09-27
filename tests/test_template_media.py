"""冷触达模板富媒体：仅跟进 / 自动回复可带图片或视频，复用本地上传。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_follow_up_template_accepts_media(admin_client) -> None:
    created = await admin_client.post(
        "/api/outreach/templates",
        headers=_headers(),
        json={
            "name": "跟进带图",
            "kind": "follow_up",
            "text": "补充一份资料给你看 https://example.com",
            "media_path": "assets/uploads/demo.png",
            "media_kind": "image",
        },
    )

    assert created.status_code == 201
    body = created.json()
    assert body["media_kind"] == "image"
    assert body["media_path"] == "assets/uploads/demo.png"
    assert body["media_url"] == "/uploads/demo.png"


async def test_first_contact_template_rejects_media(admin_client) -> None:
    created = await admin_client.post(
        "/api/outreach/templates",
        headers=_headers(),
        json={
            "name": "首条带图",
            "kind": "first_contact",
            "text": "你好，想确认一下你是否愿意了解。",
            "media_path": "assets/uploads/demo.png",
            "media_kind": "image",
        },
    )

    assert created.status_code == 400
    assert "首条招呼" in created.json()["detail"]


async def test_upload_video_endpoint(admin_client) -> None:
    ok = await admin_client.post(
        "/api/uploads/video",
        headers=_headers(),
        files={"file": ("demo.mp4", b"\x00\x00\x00\x18ftypmp42demo", "video/mp4")},
    )
    assert ok.status_code == 201
    body = ok.json()
    assert body["path"].startswith("assets/uploads/")
    assert body["url"].startswith("/uploads/")
    assert body["kind"] == "video"

    bad = await admin_client.post(
        "/api/uploads/video",
        headers=_headers(),
        files={"file": ("demo.txt", b"not a video", "text/plain")},
    )
    assert bad.status_code == 400
    assert "视频格式" in bad.json()["detail"]


class FakeMediaClient:
    """记录 send_file / send_message 的替身。"""

    def __init__(self) -> None:
        self.files: list[tuple[str, str | None]] = []
        self.messages: list[str] = []

    async def get_entity(self, identifier):
        return SimpleNamespace(id=1)

    async def send_file(self, entity, path, caption=None, **kwargs):
        self.files.append((str(path), caption))
        return SimpleNamespace(id=900)

    async def send_message(self, entity, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(id=901)


async def test_send_template_uses_send_file_when_media(admin_client, api_config, tmp_path) -> None:
    from app.db.models import OutreachTemplate
    from app.services import outreach_sender_service as sender

    media = tmp_path / "demo.png"
    media.write_bytes(b"png")

    template = OutreachTemplate(
        tenant_id=1,
        name="跟进带图",
        kind="follow_up",
        text="补充资料",
        media_path=str(media),
        media_kind="image",
    )
    client = FakeMediaClient()

    await sender.send_template(
        client, object(), template=template, text="补充资料", config=api_config
    )
    assert client.files and client.files[0][0] == str(media)
    assert client.files[0][1] == "补充资料"

    plain = OutreachTemplate(tenant_id=1, name="纯文本", kind="follow_up", text="只有文字")
    await sender.send_template(client, object(), template=plain, text="只有文字", config=api_config)
    assert client.messages == ["只有文字"]
