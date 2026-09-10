"""Session-scoped attachment storage and prompt injection (UTF-8 text only).

Attachments live under the state root (never inside a workspace) at
``attachments/{session_id}/{attachment_id}`` as a JSON document holding the
original name and decoded text. Uploads are base64 JSON so no multipart
dependency is introduced; binary/image support is contract-reserved until the
runtime can use it.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_INLINE_BYTES = 512 * 1024

_ATTACHMENT_ID = re.compile(r"^att_[0-9a-f]{12}$")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")


def new_attachment_id() -> str:
    return f"att_{uuid.uuid4().hex[:12]}"


def decode_attachment(name: str, content_base64: str) -> str:
    """Validate one upload and return its decoded UTF-8 text."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("附件缺少名称")
    try:
        raw = base64.b64decode(content_base64 or "", validate=True)
    except Exception as exc:  # binascii.Error / ValueError
        raise ValueError("附件内容不是有效的 base64") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("暂仅支持 UTF-8 文本附件") from exc


def attachment_path(state_dir: str | Path, session_id: str, attachment_id: str) -> Path:
    if not _ATTACHMENT_ID.match(attachment_id or ""):
        raise ValueError(f"非法附件 id：{attachment_id!r}")
    safe_session = _SAFE_COMPONENT.sub("_", session_id or "unknown")
    return Path(state_dir) / "attachments" / safe_session / attachment_id


def save_attachment(
    state_dir: str | Path,
    session_id: str,
    attachment_id: str,
    name: str,
    content_base64: str,
) -> dict:
    """Persist one attachment; returns the API response payload."""
    text = decode_attachment(name, content_base64)
    path = attachment_path(state_dir, session_id, attachment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"name": name, "size": len(text.encode("utf-8")), "text": text}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"attachment_id": attachment_id, "name": name, "size": len(text.encode("utf-8"))}


def load_attachment(state_dir: str | Path, session_id: str, attachment_id: str) -> dict:
    """Load one attachment for injection; raises ValueError when missing/foreign."""
    path = attachment_path(state_dir, session_id, attachment_id)
    if not path.is_file():
        raise ValueError(f"附件不存在：{attachment_id}")
    meta = json.loads(path.read_text(encoding="utf-8"))
    return {
        "attachment_id": attachment_id,
        "name": meta.get("name") or attachment_id,
        "content": meta.get("text", ""),
    }


def load_attachments(
    state_dir: str | Path, session_id: str, attachment_ids: list[str]
) -> list[dict]:
    return [
        load_attachment(state_dir, session_id, attachment_id)
        for attachment_id in (attachment_ids or [])
    ]


def compose_prompt(prompt: str, attachments: list[dict]) -> str:
    """Inline attachments into the user prompt with explicit delimiters."""
    if not attachments:
        return prompt
    parts = [prompt or ""]
    for attachment in attachments:
        text = attachment.get("content", "")
        encoded = text.encode("utf-8")
        marker = ""
        if len(encoded) > MAX_INLINE_BYTES:
            text = encoded[:MAX_INLINE_BYTES].decode("utf-8", "ignore")
            marker = "\n[附件内容过长，已截断]"
        parts.append(f"\n\n--- 附件：{attachment.get('name', '未命名')} ---\n{text}{marker}")
    return "".join(parts)
