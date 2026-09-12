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
# Windows device names are unusable as directory names; keep them out of the
# session path so a hostile session id cannot break save/load on Windows.
_WINDOWS_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE)


def new_attachment_id() -> str:
    return f"att_{uuid.uuid4().hex[:12]}"


def _safe_session_dir(session_id: str | None) -> str:
    """Flatten a session id into one path component that can never traverse."""
    safe = _SAFE_COMPONENT.sub("_", str(session_id or "unknown"))
    if safe in {".", ".."} or _WINDOWS_RESERVED.match(safe):
        return f"_{safe}"
    return safe


def decode_attachment(name: str, content_base64: str) -> str:
    """Validate one upload and return its decoded UTF-8 text."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("附件缺少名称")
    if content_base64 is None:
        content_base64 = ""
    if not isinstance(content_base64, str):
        raise ValueError("附件内容不是有效的 base64")
    # Valid base64 of N bytes is exactly 4*ceil(N/3) characters, so anything
    # longer than the byte budget cannot fit under the cap; reject before the
    # memory-heavy decode instead of after.
    if len(content_base64) > 4 * ((MAX_UPLOAD_BYTES + 2) // 3):
        raise ValueError(f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except Exception as exc:  # binascii.Error / ValueError / AttributeError
        raise ValueError("附件内容不是有效的 base64") from exc
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限")
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
) -> dict:
    """Persist one attachment; returns the API response payload."""
    text = decode_attachment(name, content_base64)
    path = attachment_path(state_dir, session_id, attachment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.dumps(
        {"name": name, "size": len(text.encode("utf-8")), "text": text},
        ensure_ascii=False,
    )
    path.write_text(meta, encoding="utf-8")
    return {"attachment_id": attachment_id, "name": name, "size": len(text.encode("utf-8"))}


def load_attachment(state_dir: str | Path, session_id: str, attachment_id: str) -> dict:
    """Load one attachment for injection; raises ValueError when missing/foreign."""
    path = attachment_path(state_dir, session_id, attachment_id)
    if not path.is_file():
        raise ValueError(f"附件不存在：{attachment_id}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"附件数据损坏：{attachment_id}") from exc
    if not isinstance(meta, dict) or not isinstance(meta.get("text", ""), str):
        raise ValueError(f"附件数据损坏：{attachment_id}")
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
        text = attachment.get("content") or ""
        encoded = text.encode("utf-8")
        marker = ""
        if len(encoded) > MAX_INLINE_BYTES:
            # Cut on the encoded bytes, then drop any incomplete trailing
            # character: the inline text is valid UTF-8 and within the budget.
            text = encoded[:MAX_INLINE_BYTES].decode("utf-8", "ignore")
            marker = "\n[附件内容过长，已截断]"
        parts.append(f"\n\n--- 附件：{attachment.get('name') or '未命名'} ---\n{text}{marker}")
    return "".join(parts)
