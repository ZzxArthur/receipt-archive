"""Decode local desktop image drops without relying on file names or MIME hints.

Qt chat clients may offer a temporary file URI, HTML, or image bytes instead of
the browser File objects produced by a file manager. Only explicitly dropped
local images are read. No URL is downloaded and no source file is changed.
"""
from __future__ import annotations

import base64
import binascii
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, unquote_to_bytes, urlsplit


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024 * 1024
MAX_FILES = 500
MAX_TEXT_BYTES = 1024 * 1024


class DropImportError(ValueError):
    """A user-readable import failure without a private path in its message."""


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if len(data) >= 14 and data[:2] == b"BM":
        return "image/bmp", ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "image/tiff", ".tiff"
    return None


def _payload(data: bytes, name: str = "微信拖入图片") -> dict:
    if len(data) > MAX_FILE_BYTES:
        raise DropImportError("图片超过 20 MB，请压缩后再导入。")
    detected = detect_image_type(data)
    if not detected:
        raise DropImportError("拖入的内容不是可读取的图片，请在微信中打开原图后再拖入，或复制图片后按 Ctrl+V。")
    mime, suffix = detected
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 32 and char != "\x7f").strip(" .")
    if not name:
        name = "微信拖入图片"
    if Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
        name = (Path(name).stem or name) + suffix
    encoded_name = name.encode("utf-8")
    if len(encoded_name) > 200:
        name = encoded_name[:180].decode("utf-8", "ignore") + suffix
    return {"name": name, "mime": mime, "data": base64.b64encode(data).decode("ascii")}


def _text(data: bytes) -> str:
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16").strip("\x00\ufeff \r\n")
        return data.decode("utf-8-sig").strip("\x00\ufeff \r\n")
    except UnicodeDecodeError as exc:
        raise DropImportError("未能读取微信拖入的信息，请打开原图后重试，或复制图片后按 Ctrl+V。") from exc


class _ImageReferences(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.references = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            values = dict(attrs)
            source = values.get("src")
            if source:
                self.references.append(source)


def _local_path(reference: str) -> Path | None:
    reference = reference.strip().strip('"')
    if "\x00" in reference:
        return None
    try:
        parsed = urlsplit(reference)
        if parsed.scheme.lower() == "file":
            if parsed.netloc.lower() not in ("", "localhost") or parsed.query or parsed.fragment:
                return None
            path = unquote(parsed.path, errors="strict")
        elif not parsed.scheme and reference.startswith("/"):
            # A plain local path may contain literal % characters; URI decode
            # only file: URLs, otherwise those valid file names would change.
            path = reference
        else:
            return None
    except (ValueError, UnicodeError):
        return None
    return Path(path) if path.startswith("/") and "\x00" not in path else None


def _read_image(path: Path) -> bytes | None:
    descriptor = None
    try:
        # NONBLOCK avoids hanging on a mistakenly dropped pipe/device. Validate
        # the actual opened object before reading; symlinked image files work.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > MAX_FILE_BYTES:
            raise DropImportError("图片超过 20 MB，请压缩后再导入。")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            header = source.read(32)
            if not detect_image_type(header):
                return None
            result = header + source.read(MAX_FILE_BYTES + 1 - len(header))
            if len(result) > MAX_FILE_BYTES:
                raise DropImportError("图片超过 20 MB，请压缩后再导入。")
            return result
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _data_image(reference: str) -> bytes | None:
    if not reference.lower().startswith("data:image/"):
        return None
    try:
        header, content = reference.split(",", 1)
        if len(content) > (MAX_FILE_BYTES + 2) // 3 * 4 + 1024:
            raise DropImportError("图片超过 20 MB，请压缩后再导入。")
        if ";base64" in header.lower():
            return base64.b64decode(re.sub(r"\s", "", content), validate=True)
        return unquote_to_bytes(content)
    except (ValueError, binascii.Error) as exc:
        if isinstance(exc, DropImportError):
            raise
        return None


def resolve_drop_payload(mime: str, data: bytes) -> list[dict]:
    """Return images for the existing JS import queue, with normalized names.

    ``mime`` identifies one selected GTK drag target. Local file references,
    HTML inline images, and encoded image bytes share the same content checks.
    """
    if not isinstance(data, bytes) or not data:
        raise DropImportError("没有收到图片内容，请打开微信原图后重试，或复制图片后按 Ctrl+V。")
    if detect_image_type(data[:32]):
        return [_payload(data)]
    base_mime = mime.split(";", 1)[0].strip().lower()
    if base_mime.startswith("image/") or base_mime == "application/x-qt-image":
        raise DropImportError("微信未提供可读取的原图，请打开原图后再拖入，或复制图片后按 Ctrl+V。")
    allowed = {"text/uri-list", "text/plain", "text/html", "text/x-moz-url",
               "x-special/gnome-copied-files", "utf8_string", "string", "text"}
    if base_mime not in allowed:
        raise DropImportError("这种拖入格式暂不支持，请打开微信原图后重试，或复制图片后按 Ctrl+V。")
    limit = ((MAX_FILE_BYTES + 2) // 3 * 4 + MAX_TEXT_BYTES) if base_mime == "text/html" else MAX_TEXT_BYTES
    if len(data) > limit:
        raise DropImportError("拖入内容过多，请减少图片数量后重试。")
    text = _text(data)
    if base_mime == "text/html":
        parser = _ImageReferences()
        parser.feed(text)
        references = parser.references
    else:
        references = [line.strip() for line in text.replace("\x00", "\n").splitlines()
                      if line.strip() and not line.lstrip().startswith("#")
                      and line.strip() not in ("copy", "cut")]
    if len(references) > MAX_FILES:
        raise DropImportError("每次最多导入 500 张图片，请分批添加。")
    output = []
    seen_paths = set()
    total_bytes = 0
    for reference in references:
        image = _data_image(reference)
        name = "微信拖入图片"
        if image is None:
            path = _local_path(reference)
            if path is None:
                continue
            path_key = str(path)
            if path_key in seen_paths:
                continue
            seen_paths.add(path_key)
            image = _read_image(path)
            name = path.name
        if image is None:
            continue
        item = _payload(image, name)
        total_bytes += len(image)
        if total_bytes > MAX_BATCH_BYTES:
            raise DropImportError("拖入图片总大小超过 512 MB，请分批添加。")
        output.append(item)
    if not output:
        raise DropImportError("没有收到可读取的本地图片，请在微信中打开原图后重试，或复制图片后按 Ctrl+V。")
    return output
