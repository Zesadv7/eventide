"""Bounded Git delivery evidence and explicit patch adoption.

The caller owns Workspace locks and independent test execution. These helpers do
not run tests, commit, reset, or merge; Git worktrees are not an OS sandbox.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

MAX_PATCH_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TREE_BYTES = 50 * 1024 * 1024
MAX_TREE_FILES = 5_000

_PROTECTED_NAMES = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        ".pytest.ini",
        "setup.cfg",
        "tox.ini",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "uv.lock",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "conftest.py",
        ".coveragerc",
        ".gitignore",
        ".gitattributes",
        ".gitmodules",
        ".gitconfig",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
    }
)


def _git(root: Path, *args: str, data: bytes | None = None, index: Path | None = None) -> bytes:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"}
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=data,
            capture_output=True,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Cannot inspect delivery Git evidence") from exc
    if result.returncode:
        reason = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git delivery refused: {reason[:1_000]}")
    return result.stdout


def _root(path: Path) -> Path:
    root = path.expanduser().resolve(strict=True)
    if (
        not root.is_dir()
        or Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root
    ):
        raise ValueError("Delivery requires the exact Git Workspace root")
    return root


def _common(root: Path) -> str:
    path = Path(os.fsdecode(_git(root, "rev-parse", "--git-common-dir")).strip())
    return str((root / path).resolve())


def _branch(root: Path) -> str:
    return os.fsdecode(_git(root, "rev-parse", "--abbrev-ref", "HEAD")).strip()


def _protected(path: str, patterns: Sequence[str]) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    return (
        any(part.lower() in {"test", "tests", "__tests__"} for part in parts[:-1])
        or any(part.lower() in {".ssh", ".aws"} for part in parts[:-1])
        or name in _PROTECTED_NAMES
        or fnmatch.fnmatchcase(name, "test*.py")
        or fnmatch.fnmatchcase(name, "*_test.py")
        or ".test." in name
        or ".spec." in name
        or name.startswith(("requirements", "jest.config.", "vitest.config.", "cypress.config."))
        or name == ".env"
        or name.startswith(".env.")
        or name in {"mcp.json", "secret.key"}
        or name.endswith((".pem", ".key", ".p12", ".pfx"))
        or path.startswith(".github/workflows/")
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    )


def _validate_path(name: str) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or path.as_posix() != name
    ):
        raise ValueError("Delivery contains an unsafe path")
    lowered = tuple(part.lower() for part in path.parts)
    if any(part in {".git", ".eventide", ".nexus"} for part in lowered):
        raise ValueError("Delivery cannot include Git or runtime state")


def _index_modes(root: Path, index: Path | None = None) -> dict[str, str]:
    modes: dict[str, str] = {}
    for entry in _git(root, "ls-files", "--stage", "-z", index=index).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, _object_id, stage = metadata.decode("ascii").split()
        name = os.fsdecode(raw_name)
        _validate_path(name)
        if mode not in {"100644", "100755"} or stage != "0":
            raise ValueError(
                "Delivery cannot contain symbolic links, submodules or merge conflicts"
            )
        modes[name] = mode
    return modes


def _reject_hidden_index(root: Path) -> None:
    if any(
        entry and (chr(entry[0]).islower() or entry[:1] == b"S")
        for entry in _git(root, "ls-files", "-v", "-z").split(b"\0")
    ):
        raise ValueError("Delivery index hides tracked files")


def _files(root: Path) -> dict[str, dict[str, Any]]:
    _reject_hidden_index(root)
    modes = _index_modes(root)
    names = {
        os.fsdecode(raw)
        for raw in _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(
            b"\0"
        )
        if raw
    }
    if len(names) > MAX_TREE_FILES:
        raise ValueError("Delivery exceeds its file limit")
    manifest: dict[str, dict[str, Any]] = {}
    total = 0
    filemode = _git(root, "config", "--get", "core.filemode").strip() != b"false"
    for name in sorted(names):
        _validate_path(name)
        path = root / name
        current = path
        while current != root:
            if current.is_symlink():
                raise ValueError("Delivery cannot traverse symbolic links")
            current = current.parent
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("Delivery must contain ordinary files")
        with path.open("rb") as stream:
            body = stream.read(MAX_FILE_BYTES + 1)
        total += len(body)
        if len(body) > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
            raise ValueError("Delivery exceeds its byte limit")
        mode = (
            "100755"
            if os.name != "nt" and filemode and path.stat().st_mode & stat.S_IXUSR
            else "100644"
        )
        if not filemode or os.name == "nt":
            mode = modes.get(name, mode)
        manifest[name] = {
            "sha256": hashlib.sha256(body).hexdigest(),
            "mode": mode,
            "size": len(body),
        }
    return manifest


def _checkpoint(root: Path) -> dict[str, Any]:
    if _git(root, "submodule", "status", "--recursive").strip():
        raise ValueError("Delivery requires a Git HEAD without submodules")
    evidence: dict[str, Any] = {
        "head": _git(root, "rev-parse", "--verify", "HEAD").decode().strip()
    }
    for kind, arguments in (
        ("staged", ("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")),
        ("unstaged", ("diff", "--binary", "--no-ext-diff", "--no-textconv")),
    ):
        evidence[kind] = hashlib.sha256(_git(root, *arguments)).hexdigest()
    untracked = []
    for raw in _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        _validate_path(name)
        path = root / name
        if path.is_symlink():
            raise ValueError("Delivery cannot traverse symbolic links")
        with path.open("rb") as stream:
            body = stream.read(MAX_FILE_BYTES + 1)
        if len(body) > MAX_FILE_BYTES:
            raise ValueError("Delivery exceeds its byte limit")
        untracked.append([name, hashlib.sha256(body).hexdigest()])
    evidence["untracked"] = sorted(untracked)
    return evidence


def _protected_manifest(
    manifest: dict[str, dict[str, Any]], patterns: Sequence[str]
) -> dict[str, dict[str, Any]]:
    return {path: metadata for path, metadata in manifest.items() if _protected(path, patterns)}


def validate_delivery_source(source: Path) -> dict[str, Any]:
    root = _root(source)
    _reject_hidden_index(root)
    if _git(root, "status", "--porcelain", "-z", "--untracked-files=all"):
        raise ValueError("Source Workspace must be clean before delivery")
    _files(root)
    return _checkpoint(root)


def capture_delivery(
    source: Path, checkout: Path, *, protected_patterns: Sequence[str] = ()
) -> dict[str, Any]:
    source, checkout = _root(source), _root(checkout)
    patterns = list(protected_patterns)
    if any(
        not isinstance(item, str)
        or not item
        or len(item) > 256
        or PurePosixPath(item).is_absolute()
        or ".." in PurePosixPath(item).parts
        for item in patterns
    ):
        raise ValueError("Invalid protected delivery pattern")
    if source == checkout or _common(source) != _common(checkout):
        raise ValueError("Delivery requires a separate checkout of the same repository")
    if _git(source, "status", "--porcelain", "-z", "--untracked-files=all"):
        raise ValueError("Source Workspace must be clean before delivery")
    source_files = _files(source)
    source_checkpoint = _checkpoint(source)
    checkout_files = _files(checkout)
    checkpoint = _checkpoint(checkout)
    if checkpoint["head"] != source_checkpoint["head"]:
        raise ValueError("Checkout must start at the source HEAD")
    protected = _protected_manifest(source_files, patterns)
    if protected != _protected_manifest(checkout_files, patterns):
        raise ValueError("Protected tests or configuration already changed")
    receipt = {
        "version": 1,
        "source": str(source),
        "checkout": str(checkout),
        "git_common_dir": _common(source),
        "base_commit": source_checkpoint["head"],
        "source_branch": _branch(source),
        "checkout_branch": _branch(checkout),
        "source_checkpoint": source_checkpoint,
        "checkpoint": checkpoint,
        "protected_patterns": patterns,
        "protected_manifest": protected,
    }
    verify_source_unchanged(source, receipt)
    return receipt


def verify_source_unchanged(source: Path, receipt: dict[str, Any]) -> None:
    source = _root(source)
    evidence = receipt.get("source_checkpoint")
    if not isinstance(evidence, dict) or receipt.get("base_commit") != evidence.get("head"):
        raise ValueError("Delivery base does not match its source evidence")
    if (
        str(source) != receipt.get("source")
        or _common(source) != receipt.get("git_common_dir")
        or _branch(source) != receipt.get("source_branch")
    ):
        raise ValueError("Source identity changed since delivery capture")
    _files(source)
    if _checkpoint(source) != receipt.get("source_checkpoint") or _git(
        source, "status", "--porcelain", "-z", "--untracked-files=all"
    ):
        raise ValueError("Source Workspace changed since delivery capture")


def _verify_checkout(checkout: Path, receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checkout = _root(checkout)
    if (
        str(checkout) != receipt.get("checkout")
        or _common(checkout) != receipt.get("git_common_dir")
        or _branch(checkout) != receipt.get("checkout_branch")
    ):
        raise ValueError("Checkout identity changed since delivery capture")
    _git(checkout, "merge-base", "--is-ancestor", receipt["base_commit"], "HEAD")
    files = _files(checkout)
    if _protected_manifest(files, receipt["protected_patterns"]) != receipt["protected_manifest"]:
        raise ValueError("Protected tests or configuration changed")
    return files


def build_patch(source: Path, checkout: Path, receipt: dict[str, Any]) -> bytes:
    verify_source_unchanged(source, receipt)
    checkout = _root(checkout)
    files = _verify_checkout(checkout, receipt)
    source_files = _files(_root(source))
    before = _checkpoint(checkout)
    with tempfile.TemporaryDirectory(prefix="eventide-delivery-index-") as temporary:
        index = Path(temporary) / "index"
        _git(checkout, "read-tree", receipt["base_commit"], index=index)
        entries = bytearray()
        for name in source_files.keys() - files.keys():
            entries.extend(
                b"0 " + b"0" * len(receipt["base_commit"]) + b"\t" + os.fsencode(name) + b"\0"
            )
        for name, metadata in files.items():
            if source_files.get(name) == metadata:
                continue
            with (checkout / name).open("rb") as stream:
                body = stream.read(MAX_FILE_BYTES + 1)
            if hashlib.sha256(body).hexdigest() != metadata["sha256"]:
                raise ValueError("Checkout changed while building its patch")
            object_id = _git(
                checkout, "hash-object", "-w", "--no-filters", "--stdin", data=body
            ).strip()
            entries.extend(
                metadata["mode"].encode() + b" " + object_id + b"\t" + os.fsencode(name) + b"\0"
            )
        _git(checkout, "update-index", "-z", "--index-info", data=bytes(entries), index=index)
        patch = _git(
            checkout,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            receipt["base_commit"],
            "--",
            index=index,
        )
    if len(patch) > MAX_PATCH_BYTES:
        raise ValueError("Delivery patch exceeds its byte limit")
    if before != _checkpoint(checkout) or files != _verify_checkout(checkout, receipt):
        raise ValueError("Checkout changed while building its patch")
    verify_source_unchanged(source, receipt)
    receipt["checkout_checkpoint"] = before
    receipt["patch_sha256"] = hashlib.sha256(patch).hexdigest()
    return patch


def apply_delivery(source: Path, patch: bytes | str, receipt: dict[str, Any]) -> dict[str, Any]:
    patch = patch.encode("utf-8") if isinstance(patch, str) else patch
    if not isinstance(patch, bytes) or len(patch) > MAX_PATCH_BYTES:
        raise ValueError("Delivery patch exceeds its byte limit")
    if hashlib.sha256(patch).hexdigest() != receipt.get("patch_sha256"):
        raise ValueError("Delivery patch does not match its receipt")
    source = _root(source)
    verify_source_unchanged(source, receipt)
    checkout = _root(Path(receipt["checkout"]))
    _verify_checkout(checkout, receipt)
    if _checkpoint(checkout) != receipt.get("checkout_checkpoint"):
        raise ValueError("Checkout changed after its patch was built")
    if not patch:
        raise ValueError("Delivery patch has no changes")
    if build_patch(source, checkout, dict(receipt)) != patch:
        raise ValueError("Delivery patch does not match the current checkout")
    # Independently check tree modes and protected content of the offered patch.
    with tempfile.TemporaryDirectory(prefix="eventide-delivery-check-") as temporary:
        index = Path(temporary) / "index"
        _git(source, "read-tree", receipt["base_commit"], index=index)
        _git(source, "apply", "--cached", "--binary", "--check", data=patch, index=index)
        _git(source, "apply", "--cached", "--binary", data=patch, index=index)
        modes = _index_modes(source, index)
        for name in modes:
            if (
                _protected(name, receipt["protected_patterns"])
                and name not in receipt["protected_manifest"]
            ):
                raise ValueError("Delivery patch adds protected tests or configuration")
        changed = _git(
            source,
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "-z",
            receipt["base_commit"],
            "--",
            index=index,
        )
        for raw in changed.split(b"\0"):
            if raw and _protected(os.fsdecode(raw), receipt["protected_patterns"]):
                raise ValueError("Delivery patch changes protected tests or configuration")
    _git(source, "apply", "--binary", "--check", data=patch)
    verify_source_unchanged(source, receipt)
    if _checkpoint(checkout) != receipt["checkout_checkpoint"]:
        raise ValueError("Checkout changed before patch adoption")
    _git(source, "apply", "--binary", data=patch)
    return {"patch_sha256": receipt["patch_sha256"], "checkpoint": _checkpoint(source)}
