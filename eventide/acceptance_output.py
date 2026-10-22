"""Bounded, redacted text artifacts for independent acceptance only."""

from __future__ import annotations

import codecs
import errno
import hashlib
import os
import re
import stat
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from eventide.previews import SafeTextBuffer
from eventide.store import RuntimeStore

STREAMS = ("stdout", "stderr")
_BAD_UTF8 = re.compile("[\udc80-\udcff]")


@dataclass(frozen=True)
class OutputLimits:
    stream_bytes: int = 1_048_576
    run_bytes: int = 25_165_824
    total_bytes: int = 268_435_456
    retention_seconds: float = 604_800


DEFAULT_LIMITS = OutputLimits()


class AcceptanceOutputs:
    def __init__(
        self, store: RuntimeStore, state_dir: Path, *, limits: OutputLimits = DEFAULT_LIMITS,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.root = state_dir.resolve() / "acceptance-output"
        self.limits = limits
        self.clock = clock
        self.active: set[str] = set()
        self.total_bytes = 0
        self.run_bytes: dict[str, int] = defaultdict(int)
        self.storage_error: int | None = None
        self.records: dict[str, dict[str, Any]] = {}
        self.expired: set[str] = set()
        for event in store.acceptance_output_events():
            payload = event["payload"]
            artifact = payload["artifact_id"]
            if event["type"] == "acceptance.output_expired":
                self.expired.add(artifact)
            elif event["type"] != "acceptance.output_cleanup_failed":
                self.records[artifact] = event
        self._scan_usage()

    @staticmethod
    def _run_key(run_id: str) -> str:
        return hashlib.sha256(run_id.encode()).hexdigest()

    def _safe(self, path: Path) -> Path:
        current = path
        while current != self.root.parent:
            if current.is_symlink():
                raise OSError(errno.ELOOP, "Acceptance output contains a symbolic link")
            current = current.parent
        return path

    def _path(self, run_id: str, artifact: str, stream: str, *, sealed: bool) -> Path:
        # Identities come from canonical facts; no client path is accepted.
        if not re.fullmatch(r"[a-f0-9]{32}", artifact) or stream not in STREAMS:
            raise OSError(errno.EINVAL, "Invalid acceptance output identity")
        suffix = "log" if sealed else "part"
        return self._safe(self.root / self._run_key(run_id) / artifact / f"{stream}.{suffix}")

    def _scan_usage(self) -> None:
        self.total_bytes = 0
        self.run_bytes.clear()
        self.storage_error = None
        try:
            self._safe(self.root)
            if not self.root.exists():
                return
            for run in self.root.iterdir():
                self._safe(run)
                for artifact in run.iterdir():
                    self._safe(artifact)
                    for path in artifact.iterdir():
                        self._safe(path)
                        size = path.stat().st_size
                        self.run_bytes[run.name] += size
                        self.total_bytes += size
        except OSError as exc:
            # Unknown disk usage must not become an apparent empty quota.
            self.storage_error = exc.errno or errno.EIO

    def start(
        self, session_id: str, run_id: str, call_id: str, *, secrets: tuple[str, ...],
        clean: Callable[[str], str],
    ) -> OutputCapture:
        self.cleanup()
        artifact = uuid.uuid4().hex
        event = self.store.append_fact(session_id, "acceptance.output_started", {
            "version": 1, "artifact_id": artifact, "call_id": call_id, "source_run_id": run_id,
        })
        self.records[artifact] = event
        self.active.add(artifact)
        return OutputCapture(self, event, secrets, clean)

    def _read(
        self, path: Path, max_bytes: int, *, offset: int = 0, limit: int = 0,
    ) -> dict[str, Any]:
        self._safe(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
                raise OSError(errno.EINVAL, "Invalid acceptance output file")
            decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
            digest = hashlib.sha256()
            size = characters = 0
            lossy = False
            parts: list[str] = []
            while True:
                raw = source.read(8192)
                digest.update(raw)
                size += len(raw)
                if size > max_bytes:
                    raise OSError(errno.EFBIG, "Acceptance output grew during reading")
                text = decoder.decode(raw, final=not raw)
                lossy = lossy or bool(_BAD_UTF8.search(text))
                text = _BAD_UTF8.sub("\ufffd", text)
                start = max(0, offset - characters)
                end = max(0, min(len(text), offset + limit - characters))
                if end > start:
                    parts.append(text[start:end])
                characters += len(text)
                if not raw:
                    break
        return {"stored_bytes": size, "stored_chars": characters,
                "sha256": digest.hexdigest(), "encoding_loss": lossy, "text": "".join(parts)}

    def _record(self, event: dict[str, Any], streams: dict[str, Any]) -> dict[str, Any]:
        payload = {**event["payload"], "streams": streams}
        record = self.store.append_fact(
            event["session_id"], "acceptance.output_recorded", payload,
        )
        self.records[payload["artifact_id"]] = record
        return payload

    def recover(self) -> None:
        """Only Host startup calls this, after interrupted runs have been recovered."""
        for event in list(self.records.values()):
            if event["type"] != "acceptance.output_started":
                continue
            streams = {}
            for stream in STREAMS:
                value: dict[str, Any] = {
                    "received_bytes": None, "stored_bytes": 0, "stored_chars": 0,
                    "sha256": None, "sealed": False, "encoding_loss": False,
                    "capture_state": "interrupted", "reason": "process_restart",
                }
                # A crash may fall between rename and publishing the final fact.
                for sealed in (True, False):
                    try:
                        path = self._path(
                            event["payload"]["source_run_id"],
                            event["payload"]["artifact_id"], stream,
                            sealed=sealed,
                        )
                        snapshot = self._read(path, self.limits.stream_bytes)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        value["storage_errno"] = exc.errno
                        break
                    snapshot.pop("text")
                    value.update(snapshot, sealed=sealed)
                    break
                streams[stream] = value
            self._record(event, streams)
        self.cleanup()

    def _expires(self, event: dict[str, Any]) -> bool:
        terminal = self.store.run_terminal_at(event["payload"]["source_run_id"])
        return terminal is not None and self.clock() - terminal >= self.limits.retention_seconds

    def cleanup(self) -> None:
        for artifact, event in list(self.records.items()):
            if artifact in self.active or not self._expires(event):
                continue
            if artifact not in self.expired:
                self.store.append_fact(event["session_id"], "acceptance.output_expired", {
                    "artifact_id": artifact, "call_id": event["payload"]["call_id"],
                    "source_run_id": event["payload"]["source_run_id"],
                })
                self.expired.add(artifact)
            try:
                directory = self._path(
                    event["payload"]["source_run_id"], artifact, "stdout", sealed=False,
                ).parent
                if directory.exists():
                    for path in directory.iterdir():
                        self._safe(path).unlink()
                    directory.rmdir()
                    if not any(directory.parent.iterdir()):
                        directory.parent.rmdir()
            except OSError as exc:
                self.store.append_fact(event["session_id"], "acceptance.output_cleanup_failed", {
                    "artifact_id": artifact, "call_id": event["payload"]["call_id"],
                    "source_run_id": event["payload"]["source_run_id"],
                    "storage_errno": exc.errno,
                })
        self._scan_usage()

    def page(
        self, run_id: str, evidence: dict[str, Any], stream: str, offset: int, limit: int,
    ) -> str:
        artifact = evidence["artifact_id"]
        event = self.records.get(artifact)
        if artifact in self.expired or (event is not None and self._expires(event)):
            return "Error: acceptance output unavailable; availability=expired"
        value = evidence["streams"][stream]
        if value["sha256"] is None:
            return (
                "Error: acceptance output unavailable; availability=not_saved; "
                f"capture_state={value['capture_state']}; reason={value['reason']}"
            )
        try:
            path = self._path(run_id, artifact, stream, sealed=value["sealed"])
            found = self._read(path, value["stored_bytes"], offset=offset, limit=limit)
        except FileNotFoundError:
            return "Error: acceptance output unavailable; availability=missing"
        except OSError:
            return "Error: acceptance output unavailable; availability=corrupt_or_unreadable"
        if found["sha256"] != value["sha256"] or found["stored_chars"] != value["stored_chars"]:
            return "Error: acceptance output unavailable; availability=corrupt"
        total = found["stored_chars"]
        start, end = min(offset, total), min(offset + limit, total)
        next_hint = f"next_offset={end}" if end < total else "end_of_stream"
        header = (
            f"[Acceptance output stream={stream}; offset_unit=characters; {start}:{end} "
            f"of {total}; {next_hint}; availability=available; "
            f"capture_state={value['capture_state']}; reason={value['reason']}; "
            f"encoding_loss={value['encoding_loss']}]\n"
        )
        return header + found["text"]


class _Stream:
    def __init__(
        self, capture: OutputCapture, name: str, secrets: tuple[str, ...],
        clean: Callable[[str], str],
    ):
        self.capture, self.name = capture, name
        self.decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
        self.redactor = SafeTextBuffer(secrets, clean)
        self.received_bytes = self.stored_bytes = 0
        self.state = "collecting"
        self.reason: str | None = None
        self.encoding_loss = False
        self.file: BinaryIO | None = None
        self.path: Path | None = None
        try:
            owner = capture.owner
            if owner.storage_error is not None:
                raise OSError(owner.storage_error, "Acceptance output storage unavailable")
            self.path = owner._path(capture.run_id, capture.artifact, name, sealed=False)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self.file = os.fdopen(descriptor, "wb", buffering=0)
        except OSError:
            self.state, self.reason = "write_failed", "open_failed"
        if not self.redactor.enabled:
            self.state, self.reason = "redaction_unavailable", "secret_lookahead_limit"

    def feed(self, data: bytes, *, final: bool = False) -> None:
        self.received_bytes += len(data)
        if self.state != "collecting":
            return
        text = self.decoder.decode(data, final=final)
        self.encoding_loss = self.encoding_loss or bool(_BAD_UTF8.search(text))
        safe = self.redactor.feed(_BAD_UTF8.sub("\ufffd", text), final=final).encode("utf-8")
        owner = self.capture.owner
        choices = {
            "stream_quota": owner.limits.stream_bytes - self.stored_bytes,
            "run_quota": owner.limits.run_bytes - owner.run_bytes[self.capture.run_key],
            "global_quota": owner.limits.total_bytes - owner.total_bytes,
        }
        reason = min(choices, key=lambda key: choices[key])
        available = max(0, choices[reason])
        if len(safe) > available:
            safe = safe[:available].decode("utf-8", errors="ignore").encode("utf-8")
            self.state, self.reason = "quota_exceeded", reason
        if not safe:
            return
        assert self.file is not None
        before = self.stored_bytes
        try:
            position = 0
            while position < len(safe):
                written = self.file.write(safe[position:])
                if not written:
                    raise OSError(errno.EIO, "Acceptance output write made no progress")
                position += written
        except OSError:
            self.state, self.reason = "write_failed", "write_failed"
        # These synchronous, bounded writes have no await between quota check
        # and settlement; all observers execute on the one Host event loop.
        try:
            self.stored_bytes = os.fstat(self.file.fileno()).st_size
        except OSError:
            self.state, self.reason = "write_failed", "stat_failed"
            # Keep the attempted space reserved until a filesystem rescan.
            self.stored_bytes = before + len(safe)
        delta = self.stored_bytes - before
        owner.run_bytes[self.capture.run_key] += delta
        owner.total_bytes += delta

    def finish(self, outcome: str) -> dict[str, Any]:
        self.feed(b"", final=True)
        if self.state == "collecting":
            self.state = (outcome if outcome in ("complete", "not_started") else "interrupted")
            self.reason = None if outcome == "complete" else outcome
        sealed = False
        if self.file is not None:
            try:
                os.fsync(self.file.fileno())
            except OSError:
                self.state, self.reason = "write_failed", "sync_failed"
            finally:
                try:
                    self.file.close()
                except OSError:
                    self.state, self.reason = "write_failed", "close_failed"
            if self.state == "complete":
                assert self.path is not None
                try:
                    destination = self.capture.owner._path(
                        self.capture.run_id, self.capture.artifact, self.name, sealed=True,
                    )
                    self.path.replace(destination)
                    sealed, self.path = True, destination
                    if os.name != "nt":
                        directory = os.open(destination.parent, os.O_RDONLY)
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                except OSError:
                    self.state, self.reason = "write_failed", "finalize_failed"
        value: dict[str, Any] = {
            "received_bytes": self.received_bytes, "stored_bytes": 0, "stored_chars": 0,
            "sha256": None, "sealed": sealed, "encoding_loss": self.encoding_loss,
            "capture_state": self.state, "reason": self.reason,
        }
        if self.path is not None and self.file is not None:
            try:
                snapshot = self.capture.owner._read(
                    self.path, self.capture.owner.limits.stream_bytes,
                )
            except OSError:
                value.update(capture_state="write_failed", reason="snapshot_failed")
            else:
                snapshot.pop("text")
                snapshot["encoding_loss"] |= self.encoding_loss
                value.update(snapshot)
        return value


class OutputCapture:
    def __init__(
        self, owner: AcceptanceOutputs, event: dict[str, Any], secrets: tuple[str, ...],
        clean: Callable[[str], str],
    ):
        self.owner, self.event = owner, event
        self.run_id = event["payload"]["source_run_id"]
        self.run_key = owner._run_key(self.run_id)
        self.artifact = event["payload"]["artifact_id"]
        self.streams = {name: _Stream(self, name, secrets, clean) for name in STREAMS}

    def feed(self, stream: str, data: bytes) -> None:
        self.streams[stream].feed(data)

    def finish(self, outcome: str) -> dict[str, Any]:
        try:
            streams = {name: stream.finish(outcome) for name, stream in self.streams.items()}
            return self.owner._record(self.event, streams)
        finally:
            self.owner.active.discard(self.artifact)
