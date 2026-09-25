"""图片上传：校验、落盘与访问地址。"""

from __future__ import annotations

import contextlib
import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
from app.core.paths import ensure_dir

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_SUBDIR = "assets/uploads"
URL_PREFIX = "/uploads"

# Pillow 识别的格式 → 落盘扩展名
FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
}


def uploads_directory(config: AppConfig) -> Path:
    """上传目录（绝对路径）。"""
    return config.path(UPLOAD_SUBDIR)


def normalize_relative_path(value: str | None) -> str | None:
    """把外部传入的路径规范成 `assets/uploads/xxx` 这样的相对路径。"""
    text = (value or "").strip().replace("\\", "/")
    if not text:
        return None
    if text.startswith(f"{URL_PREFIX}/"):
        return f"{UPLOAD_SUBDIR}/{text[len(URL_PREFIX) + 1 :]}"
    if text.startswith(f"{UPLOAD_SUBDIR}/"):
        return text
    # 只接受文件名的情况
    return f"{UPLOAD_SUBDIR}/{Path(text).name}"


def save_image(
    config: AppConfig,
    *,
    filename: str | None,
    content: bytes,
) -> dict[str, Any]:
    """保存上传的图片，返回 {filename, path, url, size, width, height}。"""
    if not content:
        raise ValidationFailedError("上传内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise ValidationFailedError(f"图片不能超过 {limit_mb} MB")

    try:
        with Image.open(BytesIO(content)) as probe:
            probe.verify()
        with Image.open(BytesIO(content)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationFailedError("这不是有效的图片文件（支持 JPG / PNG / GIF / WEBP）") from exc

    extension = FORMAT_EXTENSIONS.get(image_format)
    if extension is None:
        raise ValidationFailedError(f"不支持的图片格式：{image_format or filename or '未知'}")

    target_dir = ensure_dir(uploads_directory(config))
    stored_name = f"{uuid4().hex}{extension}"
    target = target_dir / stored_name

    # 先写临时文件再替换，避免上传中断留下半张图
    handle_fd, temp_name = tempfile.mkstemp(prefix=f"{stored_name}.", dir=str(target_dir))
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise

    return {
        "filename": stored_name,
        "original_name": Path(filename or "").name or None,
        "path": f"{UPLOAD_SUBDIR}/{stored_name}",
        "url": f"{URL_PREFIX}/{stored_name}",
        "size": len(content),
        "width": width,
        "height": height,
    }


def delete_image(config: AppConfig, filename: str) -> bool:
    """删除已上传的图片（只允许删除上传目录下的文件）。"""
    name = Path((filename or "").replace("\\", "/")).name
    if not name:
        return False
    target = uploads_directory(config) / name
    if not target.is_file():
        return False
    target.unlink()
    return True


def public_url(path: str | None) -> str | None:
    """把存储路径转换成前端可访问的 URL。"""
    normalized = normalize_relative_path(path)
    if normalized is None:
        return None
    return f"{URL_PREFIX}/{Path(normalized).name}"
