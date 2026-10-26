"""Isolated file worker executed under the native sandbox boundary.

The worker is the only entry point for file operations inside the native
sandbox: it is fixed Eventide code, started as ``<python> -I -c <bootstrap>
<package_parent>`` with a base64 JSON grant on the environment. Requests and
responses are newline-delimited JSON over stdin/stdout; resources are opaque
handle tokens, never arbitrary code. The module intentionally depends on the
standard library only so the sandbox needs to mount no third-party packages.

This process is not a tool executor and has no model, policy or host access.
Path authorization here is one layer of defense; the OS sandbox (Seatbelt or
bubblewrap) is the second. Real-kernel boundary verification is a separate
acceptance step.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1
GRANT_ENV_VAR = "EVENTIDE_NATIVE_WORKER_GRANT"
WORKER_BOOTSTRAP = (
    "import runpy, sys\n"
    "runpy.run_path(sys.argv[1] + '/eventide/native_sandbox/worker.py', run_name='__main__')\n"
)

# Request lines carry base64 file content, so the cap must exceed the largest
# write (8 MiB * 4/3) plus envelope room; the runner stream limit matches this.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_PATH_STRING = 4096
MAX_REQUEST_ID = 128
MAX_OP_NAME = 64
MAX_READ_FILE_BYTES = 4 * 1024 * 1024
MAX_PAGE_BYTES = 512 * 1024
MAX_WRITE_FILE_BYTES = 8 * 1024 * 1024
MAX_LIST_ENTRIES = 5_000
MAX_OPEN_HANDLES = 64

_KNOWN_PROFILES = ("read-only", "workspace-write")


class WorkerError(Exception):
    """Domain error reported to the host as a coded protocol error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkerGrant:
    """Sandbox-side grant derived from the host policy; paths only, no secrets."""

    profile: str
    workspace_root: str
    tmp_root: str
    read_only_roots: tuple[str, ...]
    protected_roots: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str) -> WorkerGrant:
        try:
            payload = json.loads(base64.b64decode(raw.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeError, binascii.Error) as error:
            raise ValueError(f"Invalid worker grant encoding: {type(error).__name__}") from error
        if not isinstance(payload, dict) or payload.get("v") != PROTOCOL_VERSION:
            raise ValueError("Unsupported worker grant version")
        profile = payload.get("profile")
        if profile not in _KNOWN_PROFILES:
            raise ValueError("Unknown worker grant profile")
        fields: dict[str, Any] = {}
        for name in ("workspace_root", "tmp_root"):
            value = payload.get(name)
            if not cls._valid_path_string(value):
                raise ValueError(f"Invalid grant field {name}")
            fields[name] = value
        for name in ("read_only_roots", "protected_roots"):
            value = payload.get(name)
            if not isinstance(value, list) or not all(
                cls._valid_path_string(item) for item in value
            ):
                raise ValueError(f"Invalid grant field {name}")
            fields[name] = value
        return cls(
            profile=profile,
            workspace_root=fields["workspace_root"],
            tmp_root=fields["tmp_root"],
            read_only_roots=tuple(fields["read_only_roots"]),
            protected_roots=tuple(fields["protected_roots"]),
        )

    @staticmethod
    def _valid_path_string(value: object) -> bool:
        if not isinstance(value, str) or not value or len(value) > MAX_PATH_STRING:
            return False
        if "\x00" in value or any(ord(char) < 0x20 for char in value):
            return False
        return os.path.isabs(value)

    def encode_env(self) -> str:
        payload = {
            "v": PROTOCOL_VERSION,
            "profile": self.profile,
            "workspace_root": self.workspace_root,
            "tmp_root": self.tmp_root,
            "read_only_roots": list(self.read_only_roots),
            "protected_roots": list(self.protected_roots),
        }
        return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    def _is_within(self, path: str, root: str) -> bool:
        if path == root:
            return True
        return path.startswith(os.path.join(root, ""))

    def authorize(self, raw: object, *, need_write: bool) -> str:
        """Resolve a requested path and enforce grant boundaries.

        Symbolic links are resolved before checks, so links pointing outside
        every allowed root are rejected. Protected roots deny both read and
        write even when nested inside an allowed root.
        """
        if not self._valid_path_string(raw):
            raise WorkerError(
                "invalid_request", "Path must be an absolute string without control characters"
            )
        assert isinstance(raw, str)
        resolved = os.path.abspath(raw)
        # Reject links at every component, including links that remain inside
        # the grant. Operations also open components with O_NOFOLLOW below.
        current = os.path.sep
        for part in resolved.split(os.path.sep)[1:]:
            current = os.path.join(current, part)
            if os.path.islink(current):
                raise WorkerError("path_denied", "Symbolic links are not file resources")
        for protected in self.protected_roots:
            if self._is_within(resolved, protected):
                raise WorkerError("path_denied", "Path is inside a protected runtime root")
        if need_write:
            if self.profile != "workspace-write":
                raise WorkerError(
                    "profile_denied", "The read-only profile refuses write operations"
                )
            allowed: tuple[str, ...] = (self.workspace_root, self.tmp_root)
        else:
            allowed = (self.workspace_root, self.tmp_root, *self.read_only_roots)
        if not any(self._is_within(resolved, root) for root in allowed):
            raise WorkerError("path_denied", "Path escapes every granted root")
        return resolved


class FileWorker:
    """Request/response loop over bounded JSON lines with opaque file handles."""

    def __init__(self, grant: WorkerGrant, reader: BinaryIO, writer: BinaryIO) -> None:
        self._grant = grant
        self._reader = reader
        self._writer = writer
        self._handles: dict[str, tuple[BinaryIO, int]] = {}
        self._stop = False

    # -- protocol ---------------------------------------------------------

    def serve(self) -> None:
        while not self._stop:
            kind, line = self._read_bounded_line()
            if kind == "eof":
                return
            if kind == "too_large":
                self._emit(
                    self._error_response("", "too_large", "Request line exceeds the protocol limit")
                )
                continue
            try:
                request = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._emit(self._error_response("", "protocol_error", "Request is not valid JSON"))
                continue
            self._emit(self.handle(request))

    def handle(self, request: object) -> dict[str, Any]:
        """Process one decoded request; never raises for domain errors."""
        if not isinstance(request, dict):
            return self._error_response("", "protocol_error", "Request must be a JSON object")
        version = request.get("v")
        identifier = request.get("id")
        operation = request.get("op")
        params = request.get("params", {})
        if version != PROTOCOL_VERSION:
            return self._error_response("", "protocol_error", "Unsupported protocol version")
        if not isinstance(identifier, str) or not identifier or len(identifier) > MAX_REQUEST_ID:
            return self._error_response("", "protocol_error", "Missing or invalid request id")
        if not isinstance(operation, str) or not operation or len(operation) > MAX_OP_NAME:
            return self._error_response(
                identifier, "protocol_error", "Missing or invalid operation name"
            )
        if not isinstance(params, dict):
            return self._error_response(
                identifier, "protocol_error", "Parameters must be a JSON object"
            )
        try:
            result = self._dispatch(operation, params)
        except WorkerError as error:
            return self._error_response(identifier, error.code, error.message)
        except OSError as error:
            detail = error.strerror or type(error).__name__
            return self._error_response(identifier, "io_error", detail)
        return {"v": PROTOCOL_VERSION, "id": identifier, "ok": True, "result": result}

    def _dispatch(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "ping": self._op_ping,
            "stat": self._op_stat,
            "list_dir": self._op_list_dir,
            "glob": self._op_glob,
            "edit_file": self._op_edit_file,
            "read_file": self._op_read_file,
            "open_read": self._op_open_read,
            "read_page": self._op_read_page,
            "close_handle": self._op_close_handle,
            "write_file": self._op_write_file,
            "remove": self._op_remove,
            "shutdown": self._op_shutdown,
        }.get(operation)
        if handler is None:
            raise WorkerError("unknown_op", f"Unknown operation: {operation}")
        return handler(params)

    # -- operations -------------------------------------------------------

    def _op_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        if params:
            raise WorkerError("invalid_request", "ping takes no parameters")
        return {"protocol": PROTOCOL_VERSION, "profile": self._grant.profile}

    def _op_stat(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=False)
        metadata = os.lstat(path)
        return {"type": _kind_of(metadata), "size": int(metadata.st_size)}

    def _op_list_dir(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=False)
        if not os.path.isdir(path):
            raise WorkerError("not_found", "Directory does not exist")
        entries: list[dict[str, Any]] = []
        with os.scandir(path) as scan:
            for entry in scan:
                if len(entries) >= MAX_LIST_ENTRIES:
                    raise WorkerError("too_large", "Directory exceeds the entry limit")
                metadata = entry.stat(follow_symlinks=False)
                entries.append(
                    {
                        "name": entry.name,
                        "type": "symlink" if entry.is_symlink() else _kind_of(metadata),
                        "size": int(metadata.st_size),
                    }
                )
        return {"entries": entries}

    def _op_read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=False)
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerError("not_found", "Path is not a regular file")
        if metadata.st_size > MAX_READ_FILE_BYTES:
            raise WorkerError("too_large", "File exceeds the whole-read limit")
        parent = self._open_parent(path, create=False)
        try:
            descriptor = os.open(os.path.basename(path),
                                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise WorkerError("path_denied", "Path is not a regular file with one link")
            content = handle.read(MAX_READ_FILE_BYTES + 1)
        if len(content) > MAX_READ_FILE_BYTES:
            raise WorkerError("too_large", "File exceeds the whole-read limit")
        return {"content_b64": base64.b64encode(content).decode("ascii"), "size": len(content)}

    def _op_open_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=False)
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkerError("not_found", "Path is not a regular file")
        if len(self._handles) >= MAX_OPEN_HANDLES:
            raise WorkerError("too_large", "Too many open read handles")
        parent = self._open_parent(path, create=False)
        try:
            descriptor = os.open(os.path.basename(path),
                                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            os.close(descriptor)
            raise WorkerError("path_denied", "Path is not a regular file with one link")
        handle = os.fdopen(descriptor, "rb")
        token = secrets.token_hex(8)
        self._handles[token] = (handle, int(metadata.st_size))
        return {"handle": token, "size": int(metadata.st_size)}

    def _op_read_page(self, params: dict[str, Any]) -> dict[str, Any]:
        token = params.get("handle")
        offset = params.get("offset")
        length = params.get("length")
        entry = self._handles.get(token) if isinstance(token, str) else None
        if entry is None:
            raise WorkerError("not_found", "Unknown read handle")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise WorkerError("invalid_request", "offset must be a non-negative integer")
        if (
            not isinstance(length, int)
            or isinstance(length, bool)
            or not 0 < length <= MAX_PAGE_BYTES
        ):
            raise WorkerError("invalid_request", f"length must be between 1 and {MAX_PAGE_BYTES}")
        handle, size = entry
        handle.seek(offset)
        content = handle.read(length)
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "size": len(content),
            "eof": offset + len(content) >= size,
        }

    def _op_close_handle(self, params: dict[str, Any]) -> dict[str, Any]:
        token = params.get("handle")
        entry = self._handles.pop(token, None) if isinstance(token, str) else None
        if entry is None:
            raise WorkerError("not_found", "Unknown read handle")
        entry[0].close()
        return {}

    def _open_parent(
        self, path: str, *, create: bool, expected_parent: int | None = None,
    ) -> int:
        descriptor = os.open(os.path.sep, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in os.path.dirname(path).split(os.path.sep)[1:]:
                if create:
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, mode=0o755, dir_fd=descriptor)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            if expected_parent is not None:
                current, expected = os.fstat(descriptor), os.fstat(expected_parent)
                if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                    raise WorkerError("path_changed", "Parent directory identity changed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _op_write_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=True)
        encoded = params.get("content_b64")
        overwrite = params.get("overwrite", False)
        if not isinstance(encoded, str) or not isinstance(overwrite, bool):
            raise WorkerError("invalid_request", "content_b64 and boolean overwrite are required")
        try:
            content = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeError, binascii.Error) as error:
            raise WorkerError("invalid_request", "content_b64 is not valid base64") from error
        if len(content) > MAX_WRITE_FILE_BYTES:
            raise WorkerError("too_large", "Write exceeds the size limit")
        parent = self._open_parent(path, create=True)
        name = os.path.basename(path)
        temporary = ".eventide-write-" + secrets.token_hex(8)
        try:
            try:
                existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if not overwrite:
                    raise WorkerError("already_exists", "File already exists")
                if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                    raise WorkerError("path_denied", "File is not a regular file with one link")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=parent)
            try:
                if existing is not None:
                    os.fchmod(descriptor, stat.S_IMODE(existing.st_mode))
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                    published = os.fstat(handle.fileno())
                try:
                    current_parent = self._open_parent(
                        path, create=False, expected_parent=parent,
                    )
                    os.close(current_parent)
                except (OSError, WorkerError) as error:
                    raise WorkerError(
                        "path_changed_before_write",
                        f"Parent directory changed before publication; no write applied: {error}",
                    ) from error
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                try:
                    current_parent = self._open_parent(
                        path, create=False, expected_parent=parent,
                    )
                    try:
                        descriptor = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=current_parent,
                        )
                        with os.fdopen(descriptor, "rb") as observed_file:
                            observed = os.fstat(observed_file.fileno())
                            if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
                                    or (observed.st_dev, observed.st_ino)
                                    != (published.st_dev, published.st_ino)):
                                raise WorkerError("path_changed", "Published file identity changed")
                            current_content = observed_file.read(MAX_WRITE_FILE_BYTES + 1)
                            final_parent = self._open_parent(
                                path, create=False, expected_parent=parent,
                            )
                            try:
                                current = os.stat(name, dir_fd=final_parent, follow_symlinks=False)
                            finally:
                                os.close(final_parent)
                    finally:
                        os.close(current_parent)
                except (OSError, WorkerError) as error:
                    raise WorkerError(
                        "path_changed",
                        f"File was published but current path cannot be confirmed: {error}",
                    ) from error
                if (current_content != content
                        or (current.st_dev, current.st_ino, current.st_size,
                            current.st_mtime_ns, current.st_ctime_ns)
                        != (observed.st_dev, observed.st_ino, observed.st_size,
                            observed.st_mtime_ns, observed.st_ctime_ns)):
                    raise WorkerError(
                        "path_changed",
                        "File was published but current content changed before confirmation",
                    )
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)
        finally:
            os.close(parent)
        return {"size": len(content)}

    def _op_edit_file(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=True)
        old, new = params.get("old_text"), params.get("new_text")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise WorkerError(
                    "invalid_request", "old_text must not be empty; edit text is required",
                )
        expected = params.get("expected_sha256")
        if expected is not None and (
            not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None
        ):
            raise WorkerError("invalid_request", "expected_sha256 must be a 64-character sha256")
        parent = self._open_parent(path, create=False)
        try:
            descriptor = os.open(os.path.basename(path), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
            with os.fdopen(descriptor, "r+b") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise WorkerError("path_denied", "File is not a regular file with one link")
                raw = handle.read(MAX_READ_FILE_BYTES + 1)
                if len(raw) > MAX_READ_FILE_BYTES:
                    raise WorkerError("too_large", "File exceeds the edit byte limit")
                actual = hashlib.sha256(raw).hexdigest()
                if expected is not None and expected.lower() != actual:
                    raise WorkerError(
                        "hash_mismatch", "Content hash mismatch; re-read the changed file",
                    )
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise WorkerError("not_text", "File is not valid UTF-8") from error
                first = text.find(old)
                if first < 0:
                    raise WorkerError("not_found", "old_text not found; file unchanged")
                if text.find(old, first + 1) >= 0:
                    raise WorkerError(
                        "ambiguous", "old_text matches multiple locations; file unchanged",
                    )
                updated = (text[:first] + new + text[first + len(old):]).encode("utf-8")
                if len(updated) > MAX_WRITE_FILE_BYTES:
                    raise WorkerError("too_large", "Edited file exceeds the size limit")
                # Hash validation and mutation happen in the same worker operation,
                # against the same descriptor, with no host read/write round trip.
                handle.seek(0)
                if handle.read(MAX_READ_FILE_BYTES + 1) != raw:
                    raise WorkerError("hash_mismatch", "File changed during edit; no write applied")
                try:
                    current_parent = self._open_parent(
                        path, create=False, expected_parent=parent,
                    )
                except (OSError, WorkerError) as error:
                    raise WorkerError(
                        "path_changed_before_write",
                        f"Parent directory changed before edit; no write applied: {error}",
                    ) from error
                try:
                    current = os.stat(
                        os.path.basename(path), dir_fd=current_parent, follow_symlinks=False,
                    )
                except FileNotFoundError as error:
                    raise WorkerError(
                        "hash_mismatch", "File path disappeared; no write applied",
                    ) from error
                finally:
                    os.close(current_parent)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise WorkerError("hash_mismatch", "File path was replaced; no write applied")
                handle.seek(0)
                handle.write(updated)
                handle.truncate()
                handle.flush()
                current_parent = self._open_parent(path, create=False, expected_parent=parent)
                try:
                    current = os.stat(
                        os.path.basename(path), dir_fd=current_parent, follow_symlinks=False,
                    )
                except FileNotFoundError as error:
                    raise WorkerError(
                        "path_changed", "File path disappeared during edit",
                    ) from error
                finally:
                    os.close(current_parent)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise WorkerError("path_changed", "File path was replaced during edit")
            return {"size": len(updated)}
        finally:
            os.close(parent)

    def _op_glob(self, params: dict[str, Any]) -> dict[str, Any]:
        base = self._grant.authorize(params.get("cwd"), need_write=False)
        pattern = params.get("pattern")
        if not isinstance(pattern, str) or os.path.isabs(pattern) or ".." in pattern.split("/"):
            raise WorkerError("invalid_request", "glob pattern must stay inside the workspace")
        parts = pattern.split("/")

        def matches(names: list[str], patterns: list[str]) -> bool:
            if not patterns:
                return not names
            if patterns[0] == "**":
                return matches(names, patterns[1:]) or bool(
                    names and not names[0].startswith(".") and matches(names[1:], patterns)
                )
            return bool(names and (not names[0].startswith(".") or patterns[0].startswith("."))
                        and fnmatch.fnmatchcase(names[0], patterns[0])
                        and matches(names[1:], patterns[1:]))

        pending = [base]
        found: list[str] = []
        visited = 0
        truncated = False
        while pending and not truncated:
            directory = pending.pop()
            with os.scandir(directory) as scan:
                for entry in scan:
                    visited += 1
                    if visited > 20_000 or len(found) >= 2_000:
                        truncated = True
                        break
                    if entry.is_symlink():
                        continue
                    try:
                        child = self._grant.authorize(entry.path, need_write=False)
                    except WorkerError as error:
                        if error.code == "path_denied":
                            continue
                        raise
                    relative = os.path.relpath(child, base)
                    if matches(relative.split(os.path.sep), parts):
                        found.append(relative)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
        content = "\n".join(sorted(found)) or "(no matches)"
        if truncated:
            content += "\n[results truncated: enumeration limit reached]"
        return {"content": content}

    def _op_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._grant.authorize(params.get("path"), need_write=True)
        metadata = os.lstat(path)
        if stat.S_ISDIR(metadata.st_mode):
            os.rmdir(path)
        elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            os.unlink(path)
        else:
            raise WorkerError("io_error", "Refusing to remove a special file")
        return {}

    def _op_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        if params:
            raise WorkerError("invalid_request", "shutdown takes no parameters")
        self._stop = True
        return {}

    # -- io ---------------------------------------------------------------

    def _read_bounded_line(self) -> tuple[str, bytes]:
        while True:
            line = self._reader.readline(MAX_MESSAGE_BYTES + 1)
            if not line:
                return "eof", b""
            if line.endswith(b"\n"):
                if len(line) > MAX_MESSAGE_BYTES:
                    return "too_large", b""
                return "line", line.rstrip(b"\n")
            # Limit hit or EOF without newline: drain the remainder of the line.
            if len(line) > MAX_MESSAGE_BYTES:
                while True:
                    rest = self._reader.readline(MAX_MESSAGE_BYTES + 1)
                    if not rest or rest.endswith(b"\n"):
                        return "too_large", b""

    def _emit(self, response: dict[str, Any]) -> None:
        self._writer.write(json.dumps(response).encode("utf-8") + b"\n")
        self._writer.flush()

    @staticmethod
    def _error_response(identifier: str, code: str, message: str) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "id": identifier,
            "ok": False,
            "error": {"code": code, "message": message},
        }


def _kind_of(metadata: os.stat_result) -> str:
    if stat.S_ISDIR(metadata.st_mode):
        return "dir"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    return "other"


def run_worker(reader: BinaryIO | None = None, writer: BinaryIO | None = None) -> int:
    """Entry point inside the sandbox; grant comes from the environment only."""
    raw = os.environ.get(GRANT_ENV_VAR)
    if not raw:
        sys.stderr.write("native worker: missing grant environment\n")
        return 2
    try:
        grant = WorkerGrant.parse(raw)
    except ValueError as error:
        sys.stderr.write(f"native worker: {error}\n")
        return 2
    worker = FileWorker(
        grant,
        reader if reader is not None else sys.stdin.buffer,
        writer if writer is not None else sys.stdout.buffer,
    )
    worker.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_worker())
