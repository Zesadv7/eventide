"""Authenticated encryption for persisted provider credentials."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretKeyError(RuntimeError):
    """Raised when the installation key is missing or cannot decrypt stored data."""


class SecretBox:
    """Loads one stable installation key and encrypts individual database fields."""

    def __init__(self, state_dir: Path):
        self.key_path = state_dir / "secret.key"
        self._fernet: Fernet | None = None

    def _load(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        configured = os.getenv("EVENTIDE_SECRET_KEY")
        if configured:
            try:
                raw_key = configured.strip().encode("ascii", errors="strict")
            except UnicodeEncodeError as exc:
                raise SecretKeyError("EVENTIDE_SECRET_KEY 必须使用 URL-safe Base64 字符") from exc
        elif self.key_path.exists():
            raw_key = self.key_path.read_bytes().strip()
        else:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            raw_key = Fernet.generate_key()
            temporary = self.key_path.with_suffix(".tmp")
            temporary.write_bytes(raw_key + b"\n")
            with suppress(OSError):
                temporary.chmod(0o600)
            os.replace(temporary, self.key_path)
        try:
            self._fernet = Fernet(raw_key)
        except (ValueError, TypeError) as exc:
            raise SecretKeyError(
                "EVENTIDE_SECRET_KEY 格式无效，应为 Fernet 生成的 URL-safe Base64 密钥"
            ) from exc
        return self._fernet

    def encrypt(self, value: str) -> str:
        return self._load().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._load().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretKeyError(
                "已保存的 API Key 无法解密；请恢复原 EVENTIDE_SECRET_KEY 或重新保存密钥"
            ) from exc
