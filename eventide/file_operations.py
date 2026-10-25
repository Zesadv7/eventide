"""Recoverable local builtin text mutations; status lives only in the Event Log."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import threading
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from eventide.models import ToolOutput
from eventide.secrets import SecretBox
from eventide.store import RuntimeStore
from eventide.tools.filesystem import (
    SHA256_PATTERN,
    edit_match_diagnostics,
    version_conflict_message,
)
from eventide.workspace import runtime_checkpoint_files

MAX_FILE_BYTES = 1_048_576
MAX_DIFF_CHARS = 200_000


def _path_key(path: Path) -> str:
    # Conservatively coordinate/protect aliases on case-insensitive Unicode volumes.
    return unicodedata.normalize("NFD", str(path)).casefold()


class FileOperationUnknown(RuntimeError):
    outcome_unknown = True


class FileOperationConflict(ValueError):
    pass


class FileOperations:
    def __init__(self, store: RuntimeStore, state_dir: Path):
        self.store = store
        self.state_dir = state_dir.resolve()
        self.box = SecretBox(state_dir)
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)

    def records(self, session_id: str | None = None) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        events = (
            self.store.session_events(session_id)
            if session_id
            else self.store.facts_by_type(("file.operation",))
        )
        for event in events:
            if event["type"] == "file.operation":
                record = event["payload"]
                records[record["operation_id"]] = record
        return list(records.values())

    def get(self, session_id: str, operation_id: str) -> dict[str, Any]:
        record = next(
            (r for r in self.records(session_id) if r["operation_id"] == operation_id), None
        )
        if record is None:
            raise ValueError("File operation not found in this session")
        return record

    def _record(self, record: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
        updated = {**record, "status": status, **extra}
        self.store.append_fact(
            record["session_id"],
            "file.operation",
            updated,
            author="user" if status.startswith("undo") else "runtime",
        )
        return updated

    def recover(self) -> None:
        for record in self.records():
            if record["status"] in {"prepared", "undo_prepared"}:
                self._record(
                    record, "needs_review", message="进程中断，文件结果待核验，禁止自动撤销"
                )

    @staticmethod
    def _text(raw: bytes) -> str:
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Controlled text writes support at most 1 MiB per file")
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise ValueError("Binary files are not supported by controlled text writes")
        return text

    def _target(self, cwd: Path, raw: str, workspace_root: Path) -> Path:
        if os.name != "posix":
            raise ValueError("Controlled builtin text writes currently require POSIX file APIs")
        # Do not resolve links away before walking with O_NOFOLLOW.
        path = Path(raw)
        if path.is_absolute():
            path = path.relative_to(cwd)
        if not path.parts or ".." in path.parts:
            raise PermissionError("Path escapes workspace or names a directory")
        target = cwd / path
        state_key, root_key, target_key = map(_path_key, (self.state_dir, workspace_root, target))
        if (
            (state_key == root_key or state_key.startswith(root_key + "/"))
            and (target_key == state_key or target_key.startswith(state_key + "/"))
        ) or target_key in {
            _path_key(file) for file in runtime_checkpoint_files(self.store.path, self.state_dir)
        }:
            raise PermissionError("Runtime state cannot be changed by builtin file tools")
        if any(
            part.casefold() == ".git" for part in (cwd.relative_to(workspace_root) / path).parts
        ):
            raise PermissionError("Git metadata cannot be changed by builtin file tools")
        return path

    @contextmanager
    def _parent(
        self, root: Path, relative: Path, *, create: bool = False
    ) -> Iterator[tuple[int, str]]:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, relative.name
        finally:
            os.close(fd)

    def _read(self, root: Path, relative: Path) -> tuple[bytes, dict[str, Any]]:
        try:
            with self._parent(root, relative) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("Only regular text files with one link are supported")
                    if info.st_size > MAX_FILE_BYTES:
                        raise ValueError("Controlled text writes support at most 1 MiB per file")
                    raw = source.read(MAX_FILE_BYTES + 1)
                    after = os.fstat(source.fileno())
                    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise FileOperationConflict("File changed while reading")
                    self._text(raw)
                    return raw, {
                        "exists": True,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "mode": stat.S_IMODE(info.st_mode),
                        "size": len(raw),
                        "mtime_ns": info.st_mtime_ns,
                        "atime_ns": info.st_atime_ns,
                    }
        except FileNotFoundError:
            return b"", {"exists": False}

    @staticmethod
    def _fingerprint(info: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(info.get(key) for key in ("exists", "sha256", "mode", "size"))

    def validate(self, record: dict[str, Any]) -> tuple[Path, Path]:
        workspace = self.store.get_workspace(record["workspace_id"])
        root = Path(workspace.path)
        info = root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or [info.st_dev, info.st_ino] != record["root_identity"]:
            raise PermissionError("Workspace directory identity changed")
        cwd = root / record["cwd"]
        path = self._target(cwd, record["path"], root)
        relative = Path(record["cwd"]) / path
        # The traversal checks every parent and the target, even for historical reads.
        self._read(root, relative)
        return root, relative

    def _write_file(
        self,
        root: Path,
        relative: Path,
        raw: bytes,
        info: dict[str, Any],
        expected: dict[str, Any],
        *,
        restore_times: bool = False,
    ) -> None:
        # In-place writes preserve ownership/extended attributes and leave no plaintext
        # recovery temp files in the checkout. A partial write stays needs_review.
        with self._parent(root, relative, create=True) as (parent, name):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if not expected["exists"]:
                flags |= os.O_CREAT | os.O_EXCL
            fd = os.open(name, flags, info["mode"], dir_fd=parent)
            with os.fdopen(fd, "r+b") as output:
                observed = os.fstat(output.fileno())
                if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                    raise FileOperationConflict("Target is no longer a regular single-link file")
                if expected["exists"]:
                    current = {
                        "exists": True,
                        "mode": stat.S_IMODE(observed.st_mode),
                        "size": observed.st_size,
                        "sha256": hashlib.sha256(output.read(MAX_FILE_BYTES + 1)).hexdigest(),
                    }
                    if self._fingerprint(current) != self._fingerprint(expected):
                        raise FileOperationConflict("File changed immediately before write")
                output.seek(0)
                output.write(raw)
                output.truncate()
                output.flush()
                os.fchmod(output.fileno(), info["mode"])
                if restore_times:
                    os.utime(output.fileno(), ns=(info["atime_ns"], info["mtime_ns"]))
                os.fsync(output.fileno())
            os.fsync(parent)

    def write(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        run_id: str,
        call_id: str,
        workspace_id: str,
        cwd: Path,
    ) -> ToolOutput:
        workspace = self.store.get_workspace(workspace_id)
        root = Path(workspace.path)
        path = self._target(cwd, arguments["path"], root)
        relative = cwd.relative_to(root) / path
        with self._locks[_path_key(root / relative)]:
            before_raw, before = self._read(root, relative)
            if tool == "edit_file":
                if not before["exists"]:
                    raise FileNotFoundError(str(path))
                if not arguments["old_text"]:
                    raise ValueError("old_text must not be empty.")
                expected = arguments.get("expected_sha256")
                if expected is not None:
                    expected = str(expected).lower()
                    if not SHA256_PATTERN.fullmatch(expected):
                        raise ValueError(
                            "expected_sha256 must be a 64-character lowercase sha256."
                        )
                    if expected != before["sha256"]:
                        raise ValueError(
                            version_conflict_message(str(path), before["sha256"])
                        )
                text = self._text(before_raw)
                first = text.find(arguments["old_text"])
                if first < 0 or text.find(arguments["old_text"], first + 1) >= 0:
                    raise ValueError(
                        edit_match_diagnostics(str(path), arguments["old_text"], text)
                    )
                after_raw = text.replace(arguments["old_text"], arguments["new_text"], 1).encode()
            else:
                after_raw = arguments["content"].encode("utf-8")
            self._text(after_raw)
            after = {
                "exists": True,
                "sha256": hashlib.sha256(after_raw).hexdigest(),
                "mode": before.get("mode", 0o644),
                "size": len(after_raw),
            }
            identity = self.store.get_run_identity(run_id)
            if identity is None:
                raise ValueError("File operation requires a persistent Run")
            info = root.stat(follow_symlinks=False)
            record = {
                "operation_id": "fileop_" + uuid.uuid4().hex,
                "session_id": identity["session_id"],
                "run_id": run_id,
                "call_id": call_id,
                "workspace_id": workspace_id,
                "cwd": cwd.relative_to(root).as_posix(),
                "path": path.as_posix(),
                "root_identity": [info.st_dev, info.st_ino],
                "tool": tool,
                "before": before,
                "after": after,
                "status": "prepared",
                "change_kind": (
                    "added"
                    if not before["exists"]
                    else "unchanged"
                    if before_raw == after_raw
                    else "modified"
                ),
            }
            encrypted = self.box.encrypt(
                json.dumps({"before": self._text(before_raw), "after": self._text(after_raw)})
            )
            self.store.prepare_file_operation(record, encrypted)
            try:
                current = self._read(root, relative)[1]
                if self._fingerprint(current) != self._fingerprint(before):
                    raise FileOperationConflict("File changed before write")
                if record["change_kind"] != "unchanged":
                    self._write_file(root, relative, after_raw, after, before)
                try:
                    verified = self._read(root, relative)[1]
                except FileOperationConflict as exc:
                    raise FileOperationUnknown(
                        "File changed during post-write verification"
                    ) from exc
                if self._fingerprint(verified) != self._fingerprint(after):
                    raise FileOperationUnknown("File changed before completion was recorded")
                self._record(
                    record, "no_change" if record["change_kind"] == "unchanged" else "completed"
                )
            except FileOperationConflict as exc:
                self._record(record, "failed", message=str(exc))
                raise
            except Exception as exc:
                try:
                    self._record(record, "needs_review", message="写入或完成记录失败，结果待核验")
                except Exception as journal_error:
                    raise FileOperationUnknown(
                        f"File operation {record['operation_id']}: recovery journal unavailable"
                    ) from journal_error
                raise FileOperationUnknown(
                    f"File operation {record['operation_id']} needs verification: {exc}"
                ) from exc
            return ToolOutput(
                f"{'Edited' if tool == 'edit_file' else 'Wrote'} {path}",
                metadata={"file_operation_id": record["operation_id"]},
            )

    def detail(self, session_id: str, operation_id: str) -> dict[str, Any]:
        record = self.get(session_id, operation_id)
        self.validate(record)
        content = json.loads(self.box.decrypt(self.store.file_operation_backup(operation_id)))
        for key in ("before", "after"):
            raw = content[key].encode("utf-8")
            self._text(raw)
            if record[key]["exists"] and hashlib.sha256(raw).hexdigest() != record[key]["sha256"]:
                raise ValueError("File operation backup fingerprint mismatch")
        # Bound both difflib work and browser output. Full recovery content is never truncated.
        if (
            sum(len(content[key]) for key in ("before", "after")) > MAX_DIFF_CHARS
            or sum(content[key].count("\n") for key in ("before", "after")) > 4_000
        ):
            return {
                **record,
                "patch": None,
                "diff_status": "too_large",
                "diff_message": "差异超过展示上限；完整恢复备份已保留",
            }
        lines = difflib.unified_diff(
            content["before"].splitlines(keepends=True),
            content["after"].splitlines(keepends=True),
            fromfile="/dev/null" if not record["before"]["exists"] else "a/" + record["path"],
            tofile="b/" + record["path"],
        )
        patch = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in lines
        )
        return {**record, "patch": patch, "diff_status": "available", "diff_message": None}

    def undo(self, session_id: str, operation_id: str) -> dict[str, Any]:
        record = self.get(session_id, operation_id)
        workspace = self.store.get_workspace(record["workspace_id"])
        target = Path(workspace.path) / record["cwd"] / record["path"]
        with self._locks[_path_key(target)]:
            record = self.get(session_id, operation_id)
            root, relative = self.validate(record)
            if record["status"] == "undone":
                return record
            if record["status"] != "completed":
                raise FileOperationConflict("操作未完成、无变化或待核验，无法安全撤销")
            current = self._read(root, relative)[1]
            if self._fingerprint(current) != self._fingerprint(record["after"]):
                self._record(record, "completed", message="撤销冲突：文件后来已被修改，未覆盖")
                raise FileOperationConflict("撤销冲突：文件后来已被修改，未覆盖")
            self.detail(session_id, operation_id)
            content = json.loads(self.box.decrypt(self.store.file_operation_backup(operation_id)))
            self._record(record, "undo_prepared", message=None)
            try:
                if record["before"]["exists"]:
                    self._write_file(
                        root,
                        relative,
                        content["before"].encode(),
                        record["before"],
                        record["after"],
                        restore_times=True,
                    )
                else:
                    if self._fingerprint(self._read(root, relative)[1]) != self._fingerprint(
                        record["after"]
                    ):
                        raise FileOperationConflict("File changed immediately before removal")
                    with self._parent(root, relative) as (parent, name):
                        os.unlink(name, dir_fd=parent)
                        os.fsync(parent)
                if self._fingerprint(self._read(root, relative)[1]) != self._fingerprint(
                    record["before"]
                ):
                    raise FileOperationUnknown("File changed during undo")
                return self._record(record, "undone", message="已撤销本次修改")
            except Exception as exc:
                try:
                    self._record(record, "needs_review", message="撤销或完成记录失败，结果待核验")
                except Exception as journal_error:
                    raise FileOperationUnknown(
                        "Undo recovery journal unavailable"
                    ) from journal_error
                raise FileOperationUnknown(f"Undo needs verification: {exc}") from exc
