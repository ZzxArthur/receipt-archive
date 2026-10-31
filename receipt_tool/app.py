"""Local-only HTTP service for receipt archiving, OCR, matching, and export.

The HTTP server, routing, and multipart parsing use only the Python standard
library (`http.server.ThreadingHTTPServer` + `email.parser`), so the app runs
offline with a small dependency set. Every route listens on loopback only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as default_email_policy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from io import BytesIO
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any
from urllib.parse import quote, unquote, urlsplit
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from .exporting import export_documents
from .recognition import match_records, recognize_image

ROOT = Path(__file__).resolve().parent
if os.name == "nt":
    DEFAULT_DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ReceiptArchive" / "data"
else:
    DEFAULT_DATA = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "receipt-archive" / "data"
DATA = Path(os.environ.get("RECEIPT_DATA_DIR", str(DEFAULT_DATA))).resolve()
DATA.mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="receipt-ocr")
LOG = logging.getLogger(__name__)
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024 * 1024
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
Image.MAX_IMAGE_PIXELS = 30_000_000


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class UploadFile:
    filename: str
    file: BytesIO


def _directory(batch_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id):
        raise HTTPException(404, "批次不存在。")
    return DATA / batch_id


def _load(batch_id: str) -> dict:
    path = _directory(batch_id) / "batch.json"
    if not path.exists():
        raise HTTPException(404, "批次不存在或已清空，请重新导入截图。")
    return json.loads(path.read_text("utf-8"))


def _save(batch: dict) -> None:
    path = _directory(batch["id"]) / "batch.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(batch, ensure_ascii=False, indent=2), "utf-8")
    temporary.replace(path)
    active = DATA / ".active-batch"
    pending_active = active.with_suffix(".tmp")
    pending_active.write_text(batch["id"], "utf-8")
    pending_active.replace(active)


def _public(batch: dict) -> dict:
    value = copy.deepcopy(batch)
    value["pending_count"] = len(_pending_records(batch))
    value["recognized_count"] = len(batch["records"]) - value["pending_count"]
    for record in value["records"]:
        record.pop("file", None)
        record.pop("sha256", None)
    return value


def _editable(batch: dict) -> None:
    if batch["status"] in ("processing", "queued"):
        raise HTTPException(409, "正在识别，请识别完成后操作。")


def _pending_records(batch: dict) -> list[dict]:
    return [record for record in batch["records"] if record.get("ocr_status") == "pending"]


def _recognized(batch: dict) -> None:
    if _pending_records(batch):
        raise HTTPException(409, "还有待识别截图，请先点击“开始识别”或“识别新增截图”。")


def _rematch(batch: dict, preserve_manual: bool = False) -> None:
    preserved = [
        copy.deepcopy(group)
        for group in batch["groups"]
        if preserve_manual
        and (group.get("manual") or group["id"].startswith("manual-") or "手动确认" in group.get("reason", ""))
    ]
    records = {record["id"]: record for record in batch["records"]}
    for group in preserved:
        order = records[group["order_id"]]
        payments = [records[rid] for rid in group["payment_ids"]]
        if (
            order.get("kind") != "order"
            or not order.get("amount_cents")
            or not payments
            or any(payment.get("kind") != "payment" or not payment.get("amount_cents") for payment in payments)
            or sum(payment.get("amount_cents") or 0 for payment in payments) != order["amount_cents"]
        ):
            group["status"] = "review"
            group["reason"] = "重新识别后金额或类型需要核对，已保留手动关联。"
    reserved = {rid for group in preserved for rid in [group["order_id"], *group["payment_ids"]]}
    result = match_records([record for record in batch["records"] if record["id"] not in reserved])
    batch["groups"] = preserved + result["groups"]
    assigned = {rid for group in batch["groups"] for rid in [group["order_id"], *group["payment_ids"]]}
    batch["unmatched_ids"] = [record["id"] for record in batch["records"] if record["id"] not in assigned]


def _process(batch_id: str) -> None:
    with LOCK:
        batch = _load(batch_id)
        batch["status"] = "processing"
        _save(batch)
        ids = [record["id"] for record in _pending_records(batch)]
    for record_id in ids:
        with LOCK:
            batch = _load(batch_id)
            record = next(r for r in batch["records"] if r["id"] == record_id)
            path = _directory(batch_id) / record["file"]
        try:
            result = recognize_image(path)
            result["ocr_status"] = "done"
            result["ocr_error"] = False
        except Exception:
            LOG.exception("OCR failed for %s", record_id)
            result = {
                "warnings": ["这张截图识别失败，请手动填写类型和金额。"],
                "ocr_error": True,
                "ocr_status": "error",
                "kind": "unknown",
                "amount_cents": None,
                "amount_candidates": [],
                "confidence": 0,
            }
        with LOCK:
            batch = _load(batch_id)
            record = next(r for r in batch["records"] if r["id"] == record_id)
            record.update(result)
            batch["progress"] += 1
            _save(batch)
    with LOCK:
        batch = _load(batch_id)
        try:
            _rematch(batch, preserve_manual=True)
            batch["status"] = "ready"
        except Exception:
            LOG.exception("Matching failed")
            batch["status"] = "error"
            batch["errors"].append("自动配对失败，识别结果已保留，可手动配对。")
        _save(batch)


# --- Request validation happens once, at the HTTP boundary. ---
def _validate_record_edit(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(422, "请求格式不正确。")
    allowed = {"kind", "amount_cents", "date", "merchant", "crop"}
    if set(payload) - allowed:
        raise HTTPException(422, "包含不支持的字段。")
    values: dict[str, Any] = {}
    if "kind" in payload:
        kind = payload["kind"]
        if kind is None:
            raise HTTPException(422, "请选择截图类型。")
        if kind not in ("order", "payment", "unknown"):
            raise HTTPException(422, "截图类型无效。")
        values["kind"] = kind
    if "amount_cents" in payload:
        amt = payload["amount_cents"]
        if amt is not None:
            if isinstance(amt, bool) or not isinstance(amt, int) or not (1 <= amt <= 100_000_000):
                raise HTTPException(422, "金额需为 0.01 至 1,000,000.00 元之间的有效数值。")
        values["amount_cents"] = amt
    if "date" in payload:
        date = payload["date"]
        if date is not None and (not isinstance(date, str) or len(date) > 32):
            raise HTTPException(422, "日期格式无效。")
        values["date"] = date
    if "merchant" in payload:
        merchant = payload["merchant"]
        if merchant is not None and (not isinstance(merchant, str) or len(merchant) > 100):
            raise HTTPException(422, "商户名称过长或格式无效。")
        values["merchant"] = merchant
    if "crop" in payload:
        crop = payload["crop"]
        if crop is not None:
            if (
                not isinstance(crop, list)
                or len(crop) != 4
                or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= float(n) <= 1 for n in crop)
                or float(crop[2]) - float(crop[0]) < 0.02
                or float(crop[3]) - float(crop[1]) < 0.02
            ):
                raise HTTPException(422, "裁剪范围应为 0–1 之间的左、上、右、下边界，且宽高至少为 2%。")
            crop = [float(n) for n in crop]
        values["crop"] = crop
    return values


def _validate_groups_edit(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        raise HTTPException(422, "配对列表格式不正确。")
    raw_groups = payload["groups"]
    if len(raw_groups) > 500:
        raise HTTPException(422, "配对组数量超过上限。")
    allowed = {"id", "order_id", "payment_ids", "status", "reason", "manual"}
    cleaned: list[dict[str, Any]] = []
    for item in raw_groups:
        if not isinstance(item, dict) or set(item) - allowed or not isinstance(item.get("order_id"), str):
            raise HTTPException(422, "配对项格式无效。")
        gid = item.get("id") or uuid.uuid4().hex[:12]
        if not isinstance(gid, str) or len(gid) > 80:
            raise HTTPException(422, "配对编号无效。")
        pids = item.get("payment_ids", [])
        if not isinstance(pids, list) or len(pids) > 100 or not all(isinstance(x, str) for x in pids):
            raise HTTPException(422, "支付凭证列表格式无效。")
        status = item.get("status", "matched")
        if status not in ("matched", "review"):
            raise HTTPException(422, "配对状态无效。")
        reason = item.get("reason", "手动确认")
        if not isinstance(reason, str) or len(reason) > 500:
            raise HTTPException(422, "配对说明格式无效。")
        cleaned.append({
            "id": gid,
            "order_id": item["order_id"],
            "payment_ids": list(pids),
            "status": status,
            "reason": reason,
            "manual": bool(item.get("manual", False)),
        })
    return cleaned


def _validate_export_options(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(422, "导出选项格式不正确。")
    fmt = payload.get("format", "zip")
    if fmt not in ("docx", "pdf", "zip"):
        raise HTTPException(422, "不支持的导出格式。")
    title = payload.get("title", "")
    if not isinstance(title, str) or len(title) > 80:
        raise HTTPException(422, "文档标题不能超过 80 个字符。")
    return {
        "format": fmt,
        "crop": bool(payload.get("crop", True)),
        "title": title.strip(),
        "allow_incomplete": bool(payload.get("allow_incomplete", False)),
    }


def health() -> dict:
    return {
        "status": "ok",
        "app_id": "receipt-archive",
        "ocr_ready": importlib.util.find_spec("rapidocr") is not None
                     and all((ROOT / "models" / name).is_file() for name in (
                         "PP-OCRv6_det_tiny.onnx", "PP-OCRv6_rec_tiny.onnx",
                         "ppocrv6_tiny_dict.txt", "ch_ppocr_mobile_v2.0_cls_infer.onnx")),
        "local_only": True,
        "max_files": 500,
        "max_file_mb": 20,
    }


def create_batch(files: list[UploadFile]) -> dict:
    batch_id = uuid.uuid4().hex
    directory = _directory(batch_id)
    directory.mkdir(mode=0o700)
    batch = {
        "id": batch_id,
        "status": "collecting",
        "progress": 0,
        "total": 0,
        "records": [],
        "groups": [],
        "unmatched_ids": [],
        "errors": [],
        "duplicates": [],
    }
    try:
        with LOCK:
            _ingest_uploads(batch, files, directory)
            _save(batch)
        return _public(batch)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _ingest_uploads(batch: dict, files: list[UploadFile], directory: Path) -> None:
    """Stage an upload atomically, retaining the batch and existing files on error."""
    if not 1 <= len(files) <= 500:
        raise HTTPException(422, "每次请添加 1–500 张截图，每批合计最多 500 张。")
    staged = copy.deepcopy(batch)
    hashes = {record["sha256"]: record["id"] for record in batch["records"] if record.get("sha256")}
    created: list[Path] = []
    errors: list[str] = []
    added = 0
    duplicates = 0
    total_bytes = sum(
        record["source_bytes"] if "source_bytes" in record else (directory / record["file"]).stat().st_size
        for record in batch["records"]
    )
    try:
        for upload in files:
            name = Path((upload.filename or "截图").replace("\\", "/")).name[:200]
            raw = upload.file.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                errors.append(f"{name}：超过 20 MB，未导入。")
                continue
            image_path = None
            try:
                with Image.open(BytesIO(raw)) as source:
                    if source.width * source.height > Image.MAX_IMAGE_PIXELS:
                        raise ValueError("图片尺寸超过 3000 万像素")
                    image = ImageOps.exif_transpose(source).convert("RGB")
                    if image.width < 20 or image.height < 20:
                        raise ValueError("图片尺寸太小")
                    digest = hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()
                    if digest in hashes:
                        staged["duplicates"].append({"name": name, "duplicate_of": hashes[digest]})
                        duplicates += 1
                        continue
                    if len(staged["records"]) >= 500:
                        raise HTTPException(422, "当前批次合计最多 500 张截图，请先整理并导出这一批。")
                    if total_bytes + len(raw) > MAX_BATCH_BYTES:
                        raise HTTPException(413, "当前批次截图总大小不能超过 512 MB。")
                    record_id = uuid.uuid4().hex[:16]
                    filename = f"{record_id}.png"
                    image_path = directory / filename
                    created.append(image_path)
                    image.save(image_path, "PNG")
                    total_bytes += len(raw)
                    hashes[digest] = record_id
                    staged["records"].append({
                        "id": record_id,
                        "name": name,
                        "filename": name,
                        "file": filename,
                        "sha256": digest,
                        "source_bytes": len(raw),
                        "kind": "unknown",
                        "amount_cents": None,
                        "amount_candidates": [],
                        "date": None,
                        "time": None,
                        "merchant": "",
                        "order_no": None,
                        "confidence": 0,
                        "text": "",
                        "crop": [0, 0, 1, 1],
                        "warnings": [],
                        "ocr_status": "pending",
                    })
                    staged["unmatched_ids"].append(record_id)
                    added += 1
            except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
                if image_path is not None:
                    image_path.unlink(missing_ok=True)
                errors.append(f"{name}：无法读取图片，未导入（{type(exc).__name__}）。")
        if not added and not duplicates:
            raise HTTPException(422, "没有有效图片。" + "；".join(errors[:5]))
        staged["errors"].extend(errors)
        staged["total"] = len(staged["records"])
        if added:
            staged["status"] = "collecting"
        batch.clear()
        batch.update(staged)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        for upload in files:
            upload.file.close()


def append_images(batch_id: str, files: list[UploadFile]) -> dict:
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        _ingest_uploads(batch, files, _directory(batch_id))
        _save(batch)
        return _public(batch)


def start_recognition(batch_id: str) -> dict:
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        pending = len(_pending_records(batch))
        if not pending:
            raise HTTPException(422, "没有待识别截图，可以继续添加截图。")
        batch["status"] = "queued"
        batch["progress"] = len(batch["records"]) - pending
        _save(batch)
        try:
            WORKER.submit(_process, batch_id)
        except RuntimeError as exc:
            batch["status"] = "collecting"
            _save(batch)
            raise HTTPException(503, "识别服务暂不可用，请稍后重试。") from exc
        return _public(_load(batch_id))


def retry_recognition(batch_id: str) -> dict:
    """Retry unresolved OCR fields while retaining deliberate user corrections."""
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        _recognized(batch)
        candidates = [
            record
            for record in batch["records"]
            if not record.get("manually_edited")
            and (
                record.get("amount_cents") is None
                or record.get("kind") == "unknown"
                or record.get("ocr_error")
                or record.get("ocr_status") == "error"
            )
        ]
        if not candidates:
            raise HTTPException(422, "没有可重新识别的截图；已保留手动修改的内容。")
        original = copy.deepcopy(batch)
        for record in candidates:
            record["ocr_status"] = "pending"
        batch["status"] = "queued"
        batch["progress"] = len(batch["records"]) - len(candidates)
        _save(batch)
        try:
            WORKER.submit(_process, batch_id)
        except RuntimeError as exc:
            _save(original)
            raise HTTPException(503, "识别服务暂不可用，请稍后重试。") from exc
        return _public(_load(batch_id))


def get_latest_batch() -> dict | None:
    """Resume the current batch when opening the native app for the first time."""
    with LOCK:
        active = DATA / ".active-batch"
        if active.exists():
            batch_id = active.read_text("utf-8").strip()
            if not batch_id:
                return None
            if re.fullmatch(r"[a-f0-9]{32}", batch_id) and (_directory(batch_id) / "batch.json").exists():
                return _public(_load(batch_id))
        candidates = sorted(DATA.glob("*/batch.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                batch = _load(path.parent.name)
                return _public(batch)
            except (HTTPException, OSError, ValueError, KeyError):
                continue
        return None


def get_batch(batch_id: str) -> dict:
    with LOCK:
        return _public(_load(batch_id))


def get_image_bytes(batch_id: str, record_id: str) -> bytes:
    with LOCK:
        batch = _load(batch_id)
        record = next((r for r in batch["records"] if r["id"] == record_id), None)
        if not record:
            raise HTTPException(404, "截图不存在。")
        path = _directory(batch_id) / record["file"]
        if not path.is_file():
            raise HTTPException(404, "截图文件不存在。")
        return path.read_bytes()


def edit_record(batch_id: str, record_id: str, payload: Any) -> dict:
    values = _validate_record_edit(payload)
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        record = next((r for r in batch["records"] if r["id"] == record_id), None)
        if not record:
            raise HTTPException(404, "截图不存在。")
        record.update(values)
        record["manually_edited"] = True
        if set(values) & {"kind", "amount_cents", "date", "merchant"}:
            for group in batch["groups"]:
                if record_id in [group["order_id"], *group["payment_ids"]]:
                    group["status"] = "review"
                    group["reason"] = "截图信息已修改，请重新配对或手动确认。"
        _save(batch)
        return _public(batch)


def rematch(batch_id: str) -> dict:
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        _recognized(batch)
        _rematch(batch)
        _save(batch)
        return _public(batch)


def edit_groups(batch_id: str, payload: Any) -> dict:
    incoming_groups = _validate_groups_edit(payload)
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        _recognized(batch)
        records = {r["id"]: r for r in batch["records"]}
        previous = {group["id"]: group for group in batch["groups"]}
        seen: set[str] = set()
        groups: list[dict] = []
        for group in incoming_groups:
            old = previous.get(group["id"])
            unchanged = old and all(group[key] == old.get(key) for key in ("order_id", "payment_ids", "status", "reason"))
            group["manual"] = bool(old.get("manual")) if unchanged else True
            order = records.get(group["order_id"])
            if not order or order["kind"] != "order":
                raise HTTPException(422, "每组顶部必须选择一张订单截图。")
            ids = [group["order_id"], *group["payment_ids"]]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise HTTPException(422, "同一张截图不能重复分配给多笔订单。")
            if any(rid not in records or records[rid]["kind"] != "payment" for rid in group["payment_ids"]):
                raise HTTPException(422, "订单下方只能选择支付凭证。")
            amounts = [records[rid].get("amount_cents") for rid in group["payment_ids"]]
            if not order.get("amount_cents") or any(amount is None for amount in amounts):
                group["status"] = "review"
                group["reason"] = "金额信息不完整，请补全后确认。"
            elif sum(amounts) != order["amount_cents"]:
                group["status"] = "review"
                group["reason"] = "扣款合计与订单金额不一致，请核对。"
            else:
                group["reason"] = old["reason"] if unchanged else "手动确认，扣款合计与订单一致。"
            seen.update(ids)
            groups.append(group)
        batch["groups"] = groups
        batch["unmatched_ids"] = [rid for rid in records if rid not in seen]
        _save(batch)
        return _public(batch)


def export(batch_id: str, payload: Any) -> tuple[bytes, str, str]:
    options = _validate_export_options(payload)
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        _recognized(batch)
        incomplete = batch["unmatched_ids"] or any(g["status"] != "matched" for g in batch["groups"])
        if incomplete and not options["allow_incomplete"]:
            raise HTTPException(422, "还有未配对或待核对的截图。请先核对，或勾选“同时导出待核对截图”。")
        try:
            return export_documents(
                batch,
                _directory(batch_id),
                options["format"],
                options["crop"],
                options["title"],
                options["allow_incomplete"],
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


def delete_batch(batch_id: str) -> dict:
    with LOCK:
        batch = _load(batch_id)
        _editable(batch)
        shutil.rmtree(_directory(batch_id))
        active = DATA / ".active-batch"
        if not active.exists() or active.read_text("utf-8").strip() == batch_id:
            active.write_text("", "utf-8")
    return {"deleted": True}


# Recover interrupted batches after a local service restart.
for _path in DATA.glob("*/batch.json"):
    try:
        _batch = json.loads(_path.read_text("utf-8"))
        if _batch.get("status") in ("queued", "processing"):
            _batch["status"] = "error"
            _batch["errors"].append("上次识别被中断，请重新导入截图，或手动补全已保留的结果。")
            _save(_batch)
    except (OSError, ValueError, KeyError):
        LOG.exception("Could not recover batch")


def _parse_multipart(content_type: str, body: bytes) -> list[UploadFile]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(422, "请使用 multipart/form-data 上传截图。")
    raw_msg = b"Content-Type: " + content_type.encode("latin1", errors="ignore") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    msg = BytesParser(policy=default_email_policy).parsebytes(raw_msg)
    uploads: list[UploadFile] = []
    if msg.is_multipart():
        for part in msg.iter_parts():
            if part.get_param("name", header="content-disposition") != "files":
                continue
            filename = part.get_filename() or "截图"
            payload = part.get_payload(decode=True) or b""
            uploads.append(UploadFile(filename=filename, file=BytesIO(payload)))
    if not uploads:
        raise HTTPException(422, "未收到上传的图片文件。")
    return uploads


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(422, "JSON 请求格式错误。") from exc


def _valid_host(host_header: str) -> bool:
    if not host_header:
        return True
    host = host_header.strip()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[: end + 1]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host.lower() in ALLOWED_HOSTS


def _dispatch(method: str, raw_path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    if not _valid_host(headers.get("host", "")):
        return 400, {"Content-Type": "text/plain; charset=utf-8"}, "Invalid host header".encode("utf-8")

    path = unquote(urlsplit(raw_path).path)
    resp_headers: dict[str, str] = {}
    if path in ("/", "/index.html") or path.startswith("/api/"):
        resp_headers["Cache-Control"] = "no-store"
    elif path.startswith("/static/"):
        resp_headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"

    try:
        if path == "/api/health" and method == "GET":
            data = health()
        elif path == "/api/batches" and method == "POST":
            data = create_batch(_parse_multipart(headers.get("content-type", ""), body))
        elif path == "/api/batches/latest" and method == "GET":
            data = get_latest_batch()
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/images/([^/]+)", path)) and method == "GET":
            png_bytes = get_image_bytes(m.group(1), m.group(2))
            resp_headers["Content-Type"] = "image/png"
            resp_headers["Content-Length"] = str(len(png_bytes))
            return 200, resp_headers, png_bytes
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/images", path)) and method == "POST":
            data = append_images(m.group(1), _parse_multipart(headers.get("content-type", ""), body))
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/recognize", path)) and method == "POST":
            data = start_recognition(m.group(1))
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/retry-recognition", path)) and method == "POST":
            data = retry_recognition(m.group(1))
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/records/([^/]+)", path)) and method == "PATCH":
            data = edit_record(m.group(1), m.group(2), _parse_json(body))
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/rematch", path)) and method == "POST":
            data = rematch(m.group(1))
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/groups", path)) and method == "PUT":
            data = edit_groups(m.group(1), _parse_json(body))
        elif (m := re.fullmatch(r"/api/batches/([^/]+)/export", path)) and method == "POST":
            doc_bytes, filename, media_type = export(m.group(1), _parse_json(body))
            resp_headers["Content-Type"] = media_type
            resp_headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
            resp_headers["Content-Length"] = str(len(doc_bytes))
            return 200, resp_headers, doc_bytes
        elif (m := re.fullmatch(r"/api/batches/([^/]+)", path)) and method in ("GET", "DELETE"):
            data = get_batch(m.group(1)) if method == "GET" else delete_batch(m.group(1))
        elif method in ("GET", "HEAD") and not path.startswith("/api/"):
            static_root = (ROOT / "static").resolve()
            rel = path[len("/static/") :] if path.startswith("/static/") else path.lstrip("/")
            if not rel:
                rel = "index.html"
            candidate = (static_root / rel).resolve()
            if not candidate.is_relative_to(static_root) or not candidate.is_file():
                raise HTTPException(404, "页面不存在。")
            content = candidate.read_bytes() if method == "GET" else b""
            mime, _ = mimetypes.guess_type(str(candidate))
            if candidate.suffix == ".js":
                mime = "application/javascript; charset=utf-8"
            elif candidate.suffix == ".css":
                mime = "text/css; charset=utf-8"
            elif candidate.suffix == ".html":
                mime = "text/html; charset=utf-8"
            resp_headers["Content-Type"] = mime or "application/octet-stream"
            resp_headers["Content-Length"] = str(candidate.stat().st_size)
            return 200, resp_headers, content
        else:
            raise HTTPException(404, "接口不存在。")

        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        resp_headers["Content-Type"] = "application/json; charset=utf-8"
        resp_headers["Content-Length"] = str(len(encoded))
        return 200, resp_headers, encoded
    except HTTPException as exc:
        encoded = json.dumps({"detail": exc.detail}, ensure_ascii=False).encode("utf-8")
        resp_headers["Content-Type"] = "application/json; charset=utf-8"
        resp_headers["Content-Length"] = str(len(encoded))
        return exc.status_code, resp_headers, encoded


class _ReceiptHTTPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        length = int(headers.get("content-length", "0") or "0")
        body = self.rfile.read(length) if length > 0 else b""
        status, resp_headers, payload = _dispatch(self.command, self.path, headers, body)
        self.send_response(status)
        self.send_header("Connection", "close")
        for k, v in resp_headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and payload:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), _ReceiptHTTPHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="凭证归档本地服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RECEIPT_PORT", "8765")))
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
