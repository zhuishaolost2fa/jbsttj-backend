"""文件名 / 对象 key / 分片计算相关的纯函数。"""

from __future__ import annotations

import math
import mimetypes
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.core.config import Settings
from app.core.exceptions import ValidationError

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_DOT = re.compile(r"\.{2,}")


def sanitize_filename(filename: str, max_length: int = 200) -> str:
    """清洗文件名：去掉路径穿越、控制字符、首尾空白。"""
    if not filename or not filename.strip():
        raise ValidationError("文件名不能为空")

    name = unicodedata.normalize("NFC", filename.strip())
    name = os.path.basename(name.replace("\\", "/"))
    name = _UNSAFE_CHARS.sub("_", name)
    name = _MULTI_DOT.sub(".", name).strip(". ")

    if not name:
        raise ValidationError("文件名非法")

    if len(name) > max_length:
        stem, ext = os.path.splitext(name)
        name = stem[: max_length - len(ext)] + ext
    return name


def get_extension(filename: str) -> str:
    return os.path.splitext(filename)[1].lower().lstrip(".")


def validate_extension(filename: str, settings: Settings) -> None:
    ext = get_extension(filename)
    blocked = settings.blocked_extension_set
    allowed = settings.allowed_extension_set

    if ext and ext in blocked:
        raise ValidationError(f"不允许上传 .{ext} 类型的文件", code="extension_blocked")
    if allowed and ext not in allowed:
        raise ValidationError(
            f"只允许上传以下类型: {', '.join(sorted(allowed))}", code="extension_not_allowed"
        )


def guess_content_type(filename: str, provided: Optional[str] = None) -> str:
    if provided and provided != "application/octet-stream":
        return provided
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def build_object_key(user_id: str, filename: str, prefix: str = "uploads") -> str:
    """对象 key 结构: prefix/user_id/YYYY/MM/uuid.ext

    用 uuid 而不是原文件名，避免同名覆盖、中文编码和路径穿越问题；
    原始文件名保存在数据库里，下载时通过 content-disposition 还原。
    """
    now = datetime.now(timezone.utc)
    ext = get_extension(filename)
    unique = uuid.uuid4().hex
    name = f"{unique}.{ext}" if ext else unique
    return f"{prefix.strip('/')}/{user_id}/{now:%Y/%m}/{name}"


def calc_total_parts(file_size: int, chunk_size: int) -> int:
    if file_size <= 0:
        raise ValidationError("文件大小必须大于 0")
    return max(1, math.ceil(file_size / chunk_size))


def resolve_chunk_size(file_size: int, requested: Optional[int], settings: Settings) -> int:
    """确定分片大小。

    分片数不能超过 OSS 上限 10000，文件过大时自动上调分片大小，
    并向上取整到 1MB，保证前端切片边界与服务端计算一致。
    """
    chunk = requested or settings.upload_chunk_size
    chunk = max(chunk, settings.min_part_size)

    if calc_total_parts(file_size, chunk) > settings.max_part_count:
        required = math.ceil(file_size / settings.max_part_count)
        mb = 1024 * 1024
        chunk = math.ceil(required / mb) * mb
    return chunk


def validate_file_size(file_size: int, settings: Settings) -> None:
    if file_size <= 0:
        raise ValidationError("文件大小必须大于 0")
    if file_size > settings.max_file_size:
        limit_gb = settings.max_file_size / (1024**3)
        raise ValidationError(f"文件超过大小限制 {limit_gb:.2f} GB", code="file_too_large")


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} TB"
