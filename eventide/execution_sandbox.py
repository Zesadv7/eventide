"""Optional container-only file tools and persistent, independently mutable text copies.

The host owns bounded snapshot materialization, integrity checks and patch export.
Agent-requested file operations run only in the selected preinstalled container
image. Neither the mutable copy nor its export is automatically applied to source.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shlex
import stat
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from eventide.executor import CommandExecutor
from eventide.models import CommandResult
from eventide.workers import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
    MAX_SOURCE_BYTES,
    SourceSnapshot,
    _excluded,
    _open_directory,
    _read_regular_file,
)
from eventide.workspace import digest

MAX_SANDBOX_ENTRIES = 50_000
MAX_PATCH_BYTES = 2 * 1024 * 1024


def _relative_directory(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid sandbox working directory")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Sandbox working directory escapes its root")
    if relative.parts and _excluded(relative):
        raise ValueError("Sandbox working directory is protected")
    return relative


def _identity(descriptor: int) -> dict[str, int]:
    metadata = os.fstat(descriptor)
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _scan_tree(root: Path, expected_identity: dict[str, int] | None = None) -> dict[str, bytes]:
    """Read a bounded tree without ever following symbolic links or hardlinks."""
    files: dict[str, bytes] = {}
    entries, total = 0, 0

    def visit(directory: int, prefix: PurePosixPath) -> None:
        nonlocal entries, total
        for name in sorted(os.listdir(directory)):
            entries += 1
            if entries > MAX_SANDBOX_ENTRIES:
                raise ValueError("Sandbox exceeds its entry limit")
            relative = prefix / name
            if "\\" in name or any(ord(char) < 32 for char in name) or _excluded(relative):
                raise ValueError("Sandbox contains a protected or unsupported path")
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory,
                )
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                if len(files) >= MAX_SNAPSHOT_FILES:
                    raise ValueError("Sandbox exceeds its file limit")
                raw = _read_regular_file(directory, name, MAX_SOURCE_BYTES)
                total += len(raw)
                if len(raw) > MAX_SOURCE_BYTES or total > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Sandbox exceeds its byte limit")
                files[relative.as_posix()] = raw
            else:
                raise ValueError("Sandbox contains an unsafe resource or hardlink")

    with _open_directory(root) as descriptor:
        if expected_identity is not None and _identity(descriptor) != expected_identity:
            raise ValueError("Sandbox directory identity changed")
        visit(descriptor, PurePosixPath())
    return files


def _manifest(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        for name, raw in files.items()
    }


def _materialize(root: Path, files: dict[str, bytes], *, mutable: bool) -> None:
    # root is freshly created inside the host-only identity directory. It has not
    # been exposed to any container, so no untrusted process can race these writes.
    root.mkdir(mode=0o700)
    for name, raw in files.items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.write_bytes(raw)
        destination.chmod(0o666 if mutable else 0o400)
    for directory, _children, _files in os.walk(root, topdown=False):
        Path(directory).chmod(0o777 if mutable else 0o700)


class SandboxWorkspaces:
    """Content artifacts; canonical Run admission remains the binding authority."""

    def __init__(
        self, state_dir: Path, *, checkpoint_files: tuple[Path, ...] = (),
        checkpoint_directories: tuple[Path, ...] = (),
    ):
        self.state_dir = state_dir.absolute()
        self.directory = self.state_dir / "execution-sandboxes"
        self.checkpoint_files = checkpoint_files
        self.checkpoint_directories = checkpoint_directories

    def create(self, workspace_id: str, root: Path, working_directory: str) -> dict[str, Any]:
        relative = _relative_directory(working_directory)
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("Sandbox requires its source Workspace identity")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _open_directory(self.directory):
            pass
        copied = SourceSnapshot.create(
            root, root, self.state_dir, checkpoint_files=self.checkpoint_files,
            checkpoint_directories=self.checkpoint_directories, secure_copy=True,
        )
        try:
            files = _scan_tree(copied.root)
            if {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()} != {
                name: metadata["sha256"] for name, metadata in copied.manifest.items()
            }:
                raise ValueError("Source snapshot changed before sandbox creation")
            sandbox_id = uuid.uuid4().hex
            storage = self.directory / sandbox_id
            storage.mkdir(mode=0o700)
            baseline, content = storage / "baseline", storage / "content"
            _materialize(baseline, files, mutable=False)
            _materialize(content, files, mutable=True)
            working = content / str(relative)
            working.mkdir(parents=True, exist_ok=True, mode=0o777)
            for directory in (working, *working.parents):
                if not directory.is_relative_to(content):
                    break
                directory.chmod(0o777)
            with _open_directory(content) as descriptor:
                filesystem_identity = _identity(descriptor)
            binding = {
                "kind": "sandbox",
                "version": 1,
                "workspace_id": workspace_id,
                "sandbox_id": sandbox_id,
                "working_directory": str(relative),
                "source_checkpoint": copied.checkpoint,
                "manifest": copied.manifest,
                "skipped": copied.skipped,
                "filesystem_identity": filesystem_identity,
            }
            encoded = json.dumps(binding, sort_keys=True, ensure_ascii=True).encode("utf-8")
            metadata_path = storage / "binding.json"
            with metadata_path.open("xb") as stream:
                stream.write(encoded)
            metadata_path.chmod(0o400)
            self.restore(binding)
            return binding
        finally:
            copied.cleanup()

    def _root(self, binding: dict[str, Any]) -> Path:
        identity = binding.get("sandbox_id")
        if (
            binding.get("kind") != "sandbox" or binding.get("version") != 1
            or not isinstance(identity, str) or len(identity) != 32
            or any(char not in "0123456789abcdef" for char in identity)
        ):
            raise ValueError("Invalid sandbox identity")
        _relative_directory(binding.get("working_directory"))
        storage = self.directory / identity
        with _open_directory(storage) as descriptor:
            encoded = _read_regular_file(descriptor, "binding.json", MAX_PATCH_BYTES)
        if len(encoded) > MAX_PATCH_BYTES or json.loads(encoded) != binding:
            raise ValueError("Sandbox binding does not match its original identity")
        return storage / "content"

    def restore(self, binding: dict[str, Any]) -> tuple[Path, Path]:
        root = self._root(binding)
        _scan_tree(root, binding["filesystem_identity"])
        working = root / binding["working_directory"]
        with _open_directory(working):
            pass
        return root, working

    def checkpoint(self, binding: dict[str, Any]) -> dict[str, Any]:
        root = self._root(binding)
        files = _scan_tree(root, binding["filesystem_identity"])
        with _open_directory(root / binding["working_directory"]):
            pass
        return {
            "execution_sandbox_id": binding["sandbox_id"],
            "manifest_digest": digest(_manifest(files)),
        }

    def diff(self, binding: dict[str, Any]) -> str:
        """Export a bounded UTF-8 unified patch; never execute Git or touch source."""
        root = self._root(binding)
        current = _scan_tree(root, binding["filesystem_identity"])
        baseline = _scan_tree(root.parent / "baseline")
        if {name: hashlib.sha256(raw).hexdigest() for name, raw in baseline.items()} != {
            name: metadata["sha256"] for name, metadata in binding["manifest"].items()
        }:
            raise ValueError("Sandbox baseline no longer matches the source manifest")
        chunks: list[str] = []
        size = 0
        for name in sorted(baseline.keys() | current.keys()):
            before, after = baseline.get(name, b""), current.get(name, b"")
            if before == after and (name in baseline) == (name in current):
                continue
            if b"\0" in before or b"\0" in after:
                raise ValueError("Sandbox patch export supports UTF-8 text files only")
            try:
                old, new = before.decode("utf-8"), after.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("Sandbox patch export supports UTF-8 text files only") from exc
            # Quotes and whitespace in filenames require Git's quoted-path dialect,
            # which this deliberately narrow, Git-free exporter does not generate.
            if any(char.isspace() for char in name) or '"' in name:
                raise ValueError("Sandbox patch export does not support quoted filenames")
            if not before and not after:
                change = "new file mode" if name in current else "deleted file mode"
                header = f"diff --git a/{name} b/{name}\n{change} 100644\n"
                size += len(header.encode("utf-8"))
                if size > MAX_PATCH_BYTES:
                    raise ValueError("Sandbox patch exceeds its export byte limit")
                chunks.append(header)
                continue
            lines = difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"a/{name}" if name in baseline else "/dev/null",
                tofile=f"b/{name}" if name in current else "/dev/null",
            )
            for line in lines:
                if not line.endswith("\n"):
                    line += "\n\\ No newline at end of file\n"
                size += len(line.encode("utf-8"))
                if size > MAX_PATCH_BYTES:
                    raise ValueError("Sandbox patch exceeds its export byte limit")
                chunks.append(line)
        return "".join(chunks)


# Executed by the image's preinstalled Python with isolated import configuration.
# No workspace Python modules, sitecustomize, shell interpolation or host file
# handlers participate in the operation. All user data is a literal JSON argument.
_CONTAINER_FILE_HELPER = r'''
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import PurePosixPath

MAX_FILE = 1048576
MAX_TOTAL = 10485760
MAX_FILES = 2000
MAX_ENTRIES = 50000
ROOT = "/workspace"
PROTECTED = {".git", ".eventide", ".nexus", ".ssh", ".aws", "mcp.json", "secret.key",
             "credentials.json", "credentials", "id_rsa", "id_ed25519"}
READ_PAGE_CHARS = 10000
PAGE_RESERVE_CHARS = 400
TOKEN_PATTERN = re.compile(r"^evtfile1:(\d+):(\d+):([0-9a-f]{16}):([0-9a-f]{64})$")


def protected(parts):
    return any(part.lower() in PROTECTED or part.lower() == ".env"
               or part.lower().startswith(".env.")
               or part.lower().endswith((".pem", ".key", ".p12", ".pfx", ".keystore"))
               for part in parts)


def relative(raw, cwd, pattern=False):
    if not isinstance(raw, str) or "\\" in raw or "\x00" in raw:
        raise ValueError("Invalid sandbox path")
    path = PurePosixPath(raw)
    if ".." in path.parts:
        raise ValueError("Path escapes sandbox")
    if path.is_absolute():
        if path.parts[:2] != ("/", "workspace"):
            raise ValueError("Absolute paths must be inside /workspace")
        parts = path.parts[2:]
    else:
        parts = PurePosixPath(cwd).parts + path.parts
    if any(any(ord(char) < 32 for char in part) for part in parts) or protected(parts):
        raise ValueError("Path is protected or unsupported")
    if not pattern and any(char in part for part in parts for char in "*?["):
        raise ValueError("Wildcards are only supported by glob")
    return parts


def open_directory(parts, create=False):
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o777, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_at(parent, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("File is an unsafe resource or hardlink")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE + 1)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns,
                before.st_nlink) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                                    after.st_ctime_ns, after.st_nlink):
            raise ValueError("File changed while being read")
        if len(raw) > MAX_FILE:
            raise ValueError("File exceeds its byte limit")
        return raw
    finally:
        os.close(fd)


def scan():
    files, paths = {}, []
    total = 0
    def visit(fd, prefix):
        nonlocal total
        for name in sorted(os.listdir(fd)):
            parts = prefix + (name,)
            paths.append(parts)
            if len(paths) > MAX_ENTRIES:
                raise ValueError("Sandbox exceeds its entry limit")
            if protected(parts) or "\\" in name or any(ord(char) < 32 for char in name):
                raise ValueError("Sandbox contains a protected or unsupported path")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(child, parts)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if len(files) >= MAX_FILES:
                    raise ValueError("Sandbox exceeds its file limit")
                if info.st_size > MAX_FILE:
                    raise ValueError("File exceeds its byte limit")
                total += info.st_size
                if total > MAX_TOTAL:
                    raise ValueError("Sandbox exceeds its byte limit")
                files[parts] = info.st_size
            else:
                raise ValueError("Sandbox contains an unsafe resource or hardlink")
    root = open_directory(())
    try:
        visit(root, ())
    finally:
        os.close(root)
    return files, paths, total


def matches(path, pattern):
    if not pattern:
        return not path
    if pattern[0] == "**":
        return matches(path, pattern[1:]) or bool(
            path and not path[0].startswith(".") and matches(path[1:], pattern))
    return bool(path and (not path[0].startswith(".") or pattern[0].startswith("."))
                and fnmatch.fnmatchcase(path[0], pattern[0]) and matches(path[1:], pattern[1:]))


def path_id(cwd_abs, rel_path):
    material = "evtfile1\x00" + cwd_abs + "\x00" + rel_path
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def edit_match_diagnostics(path, old_text, text):
    count = text.count(old_text)
    if count == 0:
        return (f"old_text not found in {path}; the file is unchanged. Check exact whitespace, "
                "indentation and line endings; CRLF files include the carriage-return "
                "characters.")
    lines = []
    start = text.find(old_text)
    while start != -1 and len(lines) < 5:
        lines.append(text.count("\n", 0, start) + 1)
        start = text.find(old_text, start + 1)
    listed = ", ".join(str(number) for number in lines)
    count_label = str(len(lines)) if start == -1 else f"at least {max(count, len(lines) + 1)}"
    more = "" if start == -1 else "; more matches"
    return (f"old_text matches {count_label} locations in {path} (lines {listed}{more}); "
            "the file is unchanged. Extend old_text with surrounding context so "
            "exactly one location matches.")


def version_conflict_message(path, sha):
    return (f"Content version conflict in {path}: the file changed since it was read (current "
            f"sha256 starts with {sha[:12]}); re-read the file; no change was applied.")


def read_page(text, sha, cwd_abs, rel_path, path, offset=0, limit=None, continuation=None):
    lines = text.splitlines()
    total = len(lines)
    if continuation is not None:
        if offset:
            return "Error: pass either continuation or a line offset, not both."
        match = TOKEN_PATTERN.fullmatch(continuation)
        if match is None:
            return "Error: Invalid continuation token; re-read the file from offset 0."
        start_line, start_char = int(match.group(1)), int(match.group(2))
        if match.group(3) != path_id(cwd_abs, rel_path):
            return ("Error: Continuation token was issued for a different file or working "
                    "copy; re-read this file from offset 0.")
        if match.group(4) != sha:
            return "Error: File changed since the previous page; re-read it from offset 0."
        if start_line >= total:
            return (f"[{path} — {total} lines total; sha256={sha}; continuation is past the "
                    "end of the file; re-read from offset 0]")
        if start_char > len(lines[start_line]):
            return ("Error: Continuation character offset is beyond that line; re-read the "
                    "file from offset 0.")
    else:
        if not total:
            return f"[{path} — 0 lines total; empty file; sha256={sha}]"
        start_line, start_char = max(int(offset or 0), 0), 0
        if start_line >= total:
            return (f"[{path} — {total} lines total; sha256={sha}; "
                    f"offset {start_line} is past the end]")
    page_limit = None if limit is None else int(limit)
    budget = READ_PAGE_CHARS - PAGE_RESERVE_CHARS - len(path)
    entries = []
    index, char, entered = start_line, start_char, 0
    while index < total and budget >= 2 and (page_limit is None or entered < page_limit):
        rest = lines[index][char:]
        prefix = f"{index + 1}: " if char == 0 else f"{index + 1}:{char}: "
        if len(prefix) + len(rest) + 1 <= budget:
            entries.append((index, char, rest, True))
            budget -= len(prefix) + len(rest) + 1
            index += 1
            char = 0
        else:
            available = budget - len(f"{index + 1}:{char}: ") - 1
            if available <= 0:
                break
            entries.append((index, char, lines[index][char:char + available], False))
            char += available
            budget = 0
        entered += 1
    if not entries:
        return f"[{path} — {total} lines total; sha256={sha}; no lines returned]"
    body = "\n".join(
        f"{number + 1}: {segment}" if whole and at == 0
        else f"{number + 1}:{at}: {segment}"
        for number, at, segment, whole in entries
    )
    end_of_file = index >= total
    if end_of_file and start_line == 0 and start_char == 0:
        header = f"[{path} — {total} lines total; sha256={sha}]"
    else:
        span = f"showing lines {entries[0][0] + 1}-{entries[-1][0] + 1}"
        if end_of_file:
            header = f"[{path} — {total} lines total; sha256={sha}; {span}; end of file]"
        else:
            token = f"evtfile1:{index}:{char}:{path_id(cwd_abs, rel_path)}:{sha}"
            header = (f"[{path} — {total} lines total; sha256={sha}; {span}; "
                      f"continue with continuation {token}]")
    if end_of_file:
        footer = ""
    elif entries[-1][3]:
        footer = f"... ({total - index} more lines)"
    else:
        footer = f"... (line {entries[-1][0] + 1} continues at char {char})"
    return "\n".join(part for part in (header, body, footer) if part)


def main():
    request = json.loads(sys.argv[1])
    operation, cwd, args = request["operation"], request["cwd"], request["arguments"]
    cwd_parts = relative(cwd, ".")
    cwd_abs = request["workspace_identity"] + "/" + "/".join(cwd_parts)
    cwd_fd = open_directory(cwd_parts)
    os.close(cwd_fd)
    files, paths, total = scan()
    if operation == "glob":
        pattern = relative(args["pattern"], cwd, pattern=True)
        found = ["/".join(path[len(cwd_parts):]) for path in paths
                 if path[:len(cwd_parts)] == cwd_parts and matches(path, pattern)]
        return "\n".join(found) if found else "(no matches)"
    parts = relative(args["path"], cwd)
    if not parts:
        raise ValueError("A file path is required")
    if operation in {"read_file", "edit_file"}:
        parent = open_directory(parts[:-1])
        try:
            raw = read_at(parent, parts[-1])
        finally:
            os.close(parent)
        sha = hashlib.sha256(raw).hexdigest()
        if operation == "read_file":
            return read_page(raw.decode("utf-8"), sha, cwd_abs, "/".join(parts),
                             args["path"], args.get("offset", 0), args.get("limit"),
                             args.get("continuation"))
        if not args["old_text"]:
            raise ValueError("old_text must not be empty.")
        expected = args.get("expected_sha256")
        if expected is not None:
            expected = expected.lower()
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                raise ValueError("expected_sha256 must be a 64-character lowercase sha256.")
            if expected != sha:
                raise ValueError(version_conflict_message(args["path"], sha))
        text = raw.decode("utf-8")
        first = text.find(args["old_text"])
        if first < 0 or text.find(args["old_text"], first + 1) >= 0:
            raise ValueError(edit_match_diagnostics(args["path"], args["old_text"], text))
        content = text.replace(args["old_text"], args["new_text"], 1)
    elif operation == "write_file":
        content = args["content"]
    else:
        raise ValueError("Unsupported sandbox operation")
    raw = content.encode("utf-8")
    if len(raw) > MAX_FILE or total - files.get(parts, 0) + len(raw) > MAX_TOTAL:
        raise ValueError("Write exceeds sandbox byte limit")
    if parts not in files and len(files) >= MAX_FILES:
        raise ValueError("Write exceeds sandbox file limit")
    parent = open_directory(parts[:-1], create=True)
    try:
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o666, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("File is an unsafe resource or hardlink")
            os.ftruncate(fd, 0)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    return ("Edited " + args["path"] if operation == "edit_file" else
            f"Wrote {len(raw)} bytes to {args['path']}")


try:
    print(main())
except Exception as exc:
    print("Error: " + str(exc))
    sys.exit(1)
'''


class SandboxFileTools:
    """Async ToolExecutor handlers; the injected executor must be container-backed."""

    def __init__(self, executor: CommandExecutor, sandbox_root: Path):
        if getattr(executor, "backend_kind", None) != "container":
            raise ValueError("Sandbox file tools require a container command executor")
        self.executor = executor
        self.root = sandbox_root.absolute()
        with _open_directory(self.root) as descriptor:
            self.filesystem_identity = _identity(descriptor)

    @property
    def handlers(self) -> dict[str, Callable[..., object]]:
        return {
            "read_file": self.read_file,
            "glob": self.glob,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
        }

    async def _execute(
        self, operation: str, cwd: Path | None, **arguments: Any,
    ) -> str | CommandResult:
        working = (cwd or self.root).absolute()
        if not working.is_relative_to(self.root):
            raise ValueError("Sandbox tool working directory escapes its root")
        relative = _relative_directory(working.relative_to(self.root).as_posix())
        with _open_directory(self.root) as descriptor:
            if _identity(descriptor) != self.filesystem_identity:
                raise ValueError("Sandbox directory identity changed")
        payload = json.dumps(
            {"operation": operation, "cwd": str(relative), "arguments": arguments,
             "workspace_identity": f"{self.root}:{self.filesystem_identity!r}"},
            ensure_ascii=True, separators=(",", ":"),
        )
        command = shlex.join(["python3", "-I", "-c", _CONTAINER_FILE_HELPER, payload])
        result = await self.executor.execute(command, self.root)
        if isinstance(result, CommandResult):
            if result.is_error or result.truncated:
                return result
            return result.stdout.removesuffix("\n")
        # Production ContainerCommandExecutor always returns CommandResult. Do not
        # silently convert an unsupported/custom backend response into success.
        raise RuntimeError("Sandbox container did not return a command result")

    async def read_file(
        self, path: str, limit: int | None = None, offset: int = 0,
        continuation: str | None = None, cwd: Path | None = None,
    ) -> str | CommandResult:
        return await self._execute(
            "read_file", cwd, path=path, limit=limit, offset=offset,
            continuation=continuation,
        )

    async def glob(self, pattern: str, cwd: Path | None = None) -> str | CommandResult:
        return await self._execute("glob", cwd, pattern=pattern)

    async def write_file(
        self, path: str, content: str, cwd: Path | None = None,
    ) -> str | CommandResult:
        return await self._execute("write_file", cwd, path=path, content=content)

    async def edit_file(
        self, path: str, old_text: str, new_text: str, expected_sha256: str | None = None,
        cwd: Path | None = None,
    ) -> str | CommandResult:
        return await self._execute(
            "edit_file", cwd, path=path, old_text=old_text, new_text=new_text,
            expected_sha256=expected_sha256,
        )
