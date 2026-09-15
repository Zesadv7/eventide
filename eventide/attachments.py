"""Session-scoped attachment storage and provider-neutral prompt composition."""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any

from eventide.config import normalize_provider

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_INLINE_BYTES = 512 * 1024

_ATTACHMENT_ID = re.compile(r"^att_[0-9a-f]{12}$")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")
_MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/ld+json",
    "application/javascript",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
}


def new_attachment_id() -> str:
    return f"att_{uuid.uuid4().hex[:12]}"


def _safe_session_dir(session_id: str | None) -> str:
    safe = _SAFE_COMPONENT.sub("_", str(session_id or "unknown"))
    if safe in {".", ".."} or _WINDOWS_RESERVED.match(safe):
        return f"_{safe}"
    return safe


def _decode_base64(content_base64: str | None) -> bytes:
    if content_base64 is None:
        content_base64 = ""
    if not isinstance(content_base64, str):
        raise ValueError("附件内容不是有效的 base64")
    if len(content_base64) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
        raise ValueError(f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception as exc:
        raise ValueError("附件内容不是有效的 base64") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    return raw


def _normalized_media_type(name: str, media_type: str | None) -> str:
    value = (media_type or mimetypes.guess_type(name)[0] or "application/octet-stream").strip()
    value = value.split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE.fullmatch(value):
        raise ValueError("附件 media_type 无效")
    return value


def _kind(media_type: str, raw: bytes, *, guess_text: bool = False) -> tuple[str, str | None]:
    if media_type.startswith("image/"):
        return "image", None
    if media_type == "application/pdf":
        return "document", None
    if media_type.startswith("text/") or media_type in _TEXT_MEDIA_TYPES:
        try:
            return "text", raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("文本附件必须使用 UTF-8 编码") from exc
    if guess_text:
        try:
            return "text", raw.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return "file", None


def decode_attachment(name: str, content_base64: str) -> str:
    """Backwards-compatible UTF-8 decoder used by callers and tests."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("附件缺少名称")
    raw = _decode_base64(content_base64)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("暂仅支持 UTF-8 文本附件") from exc


def attachment_path(state_dir: str | Path, session_id: str, attachment_id: str) -> Path:
    if not isinstance(attachment_id, str) or not _ATTACHMENT_ID.fullmatch(attachment_id):
        raise ValueError(f"非法附件 id：{attachment_id!r}")
    return Path(state_dir) / "attachments" / _safe_session_dir(session_id) / attachment_id


def save_attachment(
    state_dir: str | Path,
    session_id: str,
    attachment_id: str,
    name: str,
    content_base64: str,
    media_type: str | None = None,
) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("附件缺少名称")
    raw = _decode_base64(content_base64)
    normalized_type = _normalized_media_type(name, media_type)
    kind, text = _kind(normalized_type, raw, guess_text=media_type is None)
    if kind == "text" and normalized_type == "application/octet-stream":
        normalized_type = "text/plain"
    path = attachment_path(state_dir, session_id, attachment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "version": 2,
        "name": name,
        "size": len(raw),
        "media_type": normalized_type,
        "kind": kind,
        "created_at": time.time(),
        "used": False,
    }
    if text is not None:
        meta["text"] = text
    else:
        meta["content_base64"] = base64.b64encode(raw).decode("ascii")
    path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return _public_metadata(attachment_id, meta)


def _read_meta(path: Path, attachment_id: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"附件不存在：{attachment_id}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"附件数据损坏：{attachment_id}") from exc
    if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
        raise ValueError(f"附件数据损坏：{attachment_id}")
    if "version" not in meta and isinstance(meta.get("text"), str):
        meta = {
            **meta,
            "version": 1,
            "size": len(meta["text"].encode("utf-8")),
            "media_type": mimetypes.guess_type(meta["name"])[0] or "text/plain",
            "kind": "text",
            "created_at": path.stat().st_mtime,
            "used": False,
        }
    required = ("size", "media_type", "kind", "created_at", "used")
    if any(key not in meta for key in required):
        raise ValueError(f"附件数据损坏：{attachment_id}")
    if meta["kind"] == "text" and not isinstance(meta.get("text"), str):
        raise ValueError(f"附件数据损坏：{attachment_id}")
    if meta["kind"] != "text" and not isinstance(meta.get("content_base64"), str):
        raise ValueError(f"附件数据损坏：{attachment_id}")
    return meta


def _public_metadata(attachment_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "attachment_id": attachment_id,
        "name": meta["name"],
        "size": int(meta["size"]),
        "media_type": str(meta["media_type"]),
        "kind": str(meta["kind"]),
        "created_at": float(meta["created_at"]),
        "used": bool(meta["used"]),
    }


def load_attachment(state_dir: str | Path, session_id: str, attachment_id: str) -> dict[str, Any]:
    path = attachment_path(state_dir, session_id, attachment_id)
    meta = _read_meta(path, attachment_id)
    record = _public_metadata(attachment_id, meta)
    if meta["kind"] == "text":
        record["content"] = meta["text"]
    else:
        record["data"] = meta["content_base64"]
    return record


def load_attachments(
    state_dir: str | Path, session_id: str, attachment_ids: list[str]
) -> list[dict[str, Any]]:
    return [load_attachment(state_dir, session_id, item) for item in (attachment_ids or [])]


def list_attachments(
    state_dir: str | Path, session_id: str, *, include_used: bool = False
) -> list[dict[str, Any]]:
    root = Path(state_dir) / "attachments" / _safe_session_dir(session_id)
    if not root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_file() or not _ATTACHMENT_ID.fullmatch(path.name):
            continue
        meta = _read_meta(path, path.name)
        if include_used or not meta["used"]:
            records.append(_public_metadata(path.name, meta))
    return sorted(records, key=lambda item: (item["created_at"], item["attachment_id"]))


def mark_attachments_used(
    state_dir: str | Path, session_id: str, attachment_ids: list[str]
) -> None:
    for attachment_id in attachment_ids:
        path = attachment_path(state_dir, session_id, attachment_id)
        meta = _read_meta(path, attachment_id)
        if meta["used"]:
            continue
        meta["used"] = True
        path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def delete_attachment(state_dir: str | Path, session_id: str, attachment_id: str) -> dict[str, Any]:
    path = attachment_path(state_dir, session_id, attachment_id)
    meta = _read_meta(path, attachment_id)
    if meta["used"]:
        raise ValueError("附件已用于工作记录，不能删除")
    path.unlink()
    return _public_metadata(attachment_id, meta)


def attachment_bytes(record: dict[str, Any]) -> bytes:
    if record["kind"] == "text":
        return str(record.get("content", "")).encode("utf-8")
    return _decode_base64(str(record.get("data", "")))


def validate_for_provider(records: list[dict[str, Any]], provider: str) -> None:
    normalized = normalize_provider(provider)
    for record in records:
        kind = record["kind"]
        if kind in {"text", "image"}:
            continue
        if kind == "document" and normalized in {"anthropic", "openai_responses", "scripted"}:
            continue
        raise ValueError(
            f"当前 Provider（{normalized}）不能把附件 {record['name']} 发送给模型；"
            "可先转换为 UTF-8 文本或图片"
        )


def compose_prompt(prompt: str, records: list[dict[str, Any]]) -> str:
    """Inline text attachments into a prompt; retained for backwards compatibility."""
    parts = [prompt or ""]
    for record in records or []:
        if record.get("kind", "text") != "text":
            continue
        text = record.get("content") or ""
        encoded = text.encode("utf-8")
        marker = ""
        if len(encoded) > MAX_INLINE_BYTES:
            text = encoded[:MAX_INLINE_BYTES].decode("utf-8", "ignore")
            marker = "\n[附件内容过长，已截断]"
        parts.append(f"\n\n--- 附件：{record.get('name') or '未命名'} ---\n{text}{marker}")
    return "".join(parts)


def compose_message(prompt: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    text = compose_prompt(prompt, records)
    binary = [record for record in records if record.get("kind") != "text"]
    if not binary:
        return {"role": "user", "content": text}
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    blocks.extend(
        {
            "type": "attachment",
            "attachment_id": record["attachment_id"],
            "name": record["name"],
            "media_type": record["media_type"],
            "kind": record["kind"],
            "size": record["size"],
        }
        for record in binary
    )
    return {"role": "user", "content": blocks}


def hydrate_messages(
    state_dir: str | Path, session_id: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            hydrated.append(message)
            continue
        blocks: list[Any] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "attachment":
                blocks.append(block)
                continue
            record = load_attachment(state_dir, session_id, str(block.get("attachment_id", "")))
            blocks.append(
                {
                    "type": "image" if record["kind"] == "image" else "file",
                    "name": record["name"],
                    "media_type": record["media_type"],
                    "data": record["data"],
                }
            )
        hydrated.append({**message, "content": blocks})
    return hydrated
