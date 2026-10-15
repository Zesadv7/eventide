"""Shared provider resolution without opening a Host or changing runtime state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from eventide.config import Settings
from eventide.secrets import SecretBox, SecretKeyError


@dataclass(frozen=True, slots=True)
class ResolvedProviderConfiguration:
    settings: Settings
    secret_error: str | None = None


def apply_provider_configuration(
    startup: Settings, record: dict[str, Any] | None, secret_box: SecretBox
) -> ResolvedProviderConfiguration:
    """Apply persisted-over-startup precedence for both Host and live eval.

    An unreadable saved secret blocks environment fallback. The Host can expose
    the safe error in its configuration screen; callers making requests must
    refuse the resolution instead.
    """
    if not record:
        return ResolvedProviderConfiguration(startup)
    api_key = startup.api_key
    secret_error = None
    if encrypted := record.get("api_key_ciphertext"):
        try:
            api_key = secret_box.decrypt(str(encrypted))
        except SecretKeyError as exc:
            api_key = None
            secret_error = str(exc)
    return ResolvedProviderConfiguration(
        replace(
            startup,
            provider=str(record["provider"]),
            base_url=str(record["base_url"]) if record["base_url"] else None,
            model=str(record["model"]),
            api_key=api_key,
        ),
        secret_error,
    )


def _read_provider_record(database: Path) -> dict[str, Any] | None:
    if not database.exists():
        return None
    # Never initialize schema, acquire a HostLease, or recover interrupted runs.
    # A normal read transaction also sees committed settings in an active WAL.
    connection = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_config'"
        ).fetchone()
        if not exists:
            return None
        row = connection.execute("SELECT * FROM provider_config WHERE id=1").fetchone()
        return dict(row) if row else None
    except sqlite3.Error as exc:
        raise RuntimeError("无法只读解析已保存的模型配置；请检查状态数据库") from exc
    finally:
        if connection is not None:
            connection.close()


def resolve_provider_configuration(settings: Settings) -> Settings:
    """Resolve credentials into memory before isolating live eval state.

    No network calls, RuntimeStore construction, or Host construction occur.
    Offline evaluation must not call this function.
    """
    resolved = apply_provider_configuration(
        settings,
        _read_provider_record(settings.state_dir / "runtime.sqlite"),
        SecretBox(settings.state_dir),
    )
    if resolved.secret_error:
        raise SecretKeyError(resolved.secret_error)
    return resolved.settings
