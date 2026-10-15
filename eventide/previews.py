"""Bounded disposable previews; they are never canonical messages or usage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SafeTextBuffer:
    """Hold enough lookahead that known secrets cannot leak across stream chunks."""

    def __init__(self, secrets: tuple[str, ...], clean: Callable[[str], str]):
        self.secrets = tuple(secret for secret in secrets if secret)
        self.lookahead = max((len(secret) - 1 for secret in self.secrets), default=0)
        self.clean = clean
        self.pending = ""
        # An unusually large credential disables preview rather than allocating
        # an unbounded lookahead or leaking part of that credential.
        self.enabled = self.lookahead <= 65_536

    def feed(self, text: str, *, final: bool = False) -> str:
        if not self.enabled:
            return ""
        self.pending += text
        cut = len(self.pending) if final else max(0, len(self.pending) - self.lookahead)
        for secret in self.secrets:
            start = self.pending.find(secret)
            while start >= 0:
                if start < cut < start + len(secret):
                    cut = start
                    break
                start = self.pending.find(secret, start + 1)
        safe, self.pending = self.pending[:cut], self.pending[cut:]
        return self.clean(safe)


class PreviewStore:
    def __init__(self, limit: int = 16_000, active_limit: int = 64):
        self.limit = limit
        self.active_limit = active_limit
        self._items: dict[str, dict[str, Any]] = {}
        self._revision = 0

    def append(self, run_id: str, request_id: str, text: str) -> None:
        if not text:
            return
        previous = self._items.get(run_id)
        if previous and previous["request_id"] != request_id:
            previous = None
        full = (previous["text"] if previous else "") + text
        omitted = (previous["omitted_chars"] if previous else 0) + max(0, len(full) - self.limit)
        self._revision += 1
        self._items[run_id] = {
            "type": "model.preview",
            "run_id": run_id,
            "request_id": request_id,
            "revision": self._revision,
            "text": full[-self.limit :],
            "omitted_chars": omitted,
            "active": True,
            "ephemeral": True,
        }
        while len(self._items) > self.active_limit:
            self._items.pop(next(iter(self._items)))

    def get(self, run_id: str) -> dict[str, Any] | None:
        item = self._items.get(run_id)
        return dict(item) if item else None

    def clear(self, run_id: str) -> None:
        self._items.pop(run_id, None)
