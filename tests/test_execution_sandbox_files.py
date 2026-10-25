"""Isolated copies and file-tool boundaries, with a deterministic fake container."""

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

import pytest

from eventide import execution_sandbox, workers
from eventide.container_executor import ContainerCleanupError
from eventide.execution_sandbox import SandboxFileTools, SandboxWorkspaces
from eventide.executor import LocalCommandExecutor, ToolContext, ToolExecutor
from eventide.models import CommandResult, ToolCall
from eventide.policy import PolicyEngine
from tests.test_host import make_repo


def sandbox(tmp_path, working_directory="."):
    source = make_repo(tmp_path / "source")
    (source / "sample.py").write_text("value = 1\n")
    if working_directory != ".":
        (source / working_directory).mkdir(parents=True)
    service = SandboxWorkspaces(tmp_path / "state")
    binding = service.create("source-workspace", source, working_directory)
    root, cwd = service.restore(binding)
    return service, binding, source, root, cwd


class FakeContainer:
    """Run only the fixed helper against temporary fixture paths, never Docker."""

    backend_kind = "container"

    def __init__(self):
        self.calls = []

    async def execute(self, command, cwd):
        argv = shlex.split(command)
        assert argv[:3] == ["python3", "-I", "-c"]
        assert argv[3] == execution_sandbox._CONTAINER_FILE_HELPER
        assert len(argv) == 5
        self.calls.append((json.loads(argv[4]), cwd))
        helper = argv[3].replace('ROOT = "/workspace"', f"ROOT = {str(cwd)!r}")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", helper, argv[4], cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return CommandResult(process.returncode, stdout.decode(), stderr.decode())


def test_independent_mutable_copies_preserve_source_and_original_baseline(tmp_path):
    service, binding, source, root, cwd = sandbox(tmp_path)
    original_checkpoint = service.checkpoint(binding)
    second = service.create("source-workspace", source, ".")
    other, _ = service.restore(second)
    assert second["sandbox_id"] != binding["sandbox_id"]
    assert binding["kind"] == "sandbox"
    assert cwd == root
    assert not (root / ".git").exists()
    (root / "sample.py").write_text("value = 2\n")
    assert service.checkpoint(binding) != original_checkpoint
    assert (source / "sample.py").read_text() == "value = 1\n"
    assert (other / "sample.py").read_text() == "value = 1\n"
    source.rename(tmp_path / "source-moved")
    restored = SandboxWorkspaces(service.state_dir)
    assert restored.restore(binding) == (root, root)
    assert "-value = 1\n+value = 2\n" in restored.diff(binding)
    assert binding["source_checkpoint"] == second["source_checkpoint"]


def test_copy_filters_secrets_links_and_binaries_recursively_case_insensitively(tmp_path):
    source = make_repo(tmp_path / "source")
    (source / "okay.py").write_text("print('okay')\n")
    for name in [".ENV", "mcp.json", "auth.PEM", ".AWS/key", "nested/.env.local/password"]:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("not copied\n")
    (source / "linked.txt").symlink_to(outside)
    (source / "hard.txt").hardlink_to(outside)
    (source / "binary").write_bytes(b"\0\xff")
    service = SandboxWorkspaces(tmp_path / "state")
    binding = service.create("workspace", source, ".")
    root, _ = service.restore(binding)
    assert set(path.name for path in root.iterdir()) == {"okay.py"}
    assert binding["skipped"] >= 7


@pytest.mark.parametrize("field,value", [
    ("sandbox_id", "../outside"),
    ("sandbox_id", "z" * 32),
    ("kind", "snapshot"),
    ("working_directory", "../outside"),
    ("working_directory", "/tmp"),
    ("working_directory", ".git"),
    ("working_directory", "nested/.ENV.local"),
    ("workspace_id", "other"),
    ("source_checkpoint", {}),
    ("manifest", {}),
])
def test_restore_rejects_binding_tampering(tmp_path, field, value):
    service, binding, _source, _root, _cwd = sandbox(tmp_path)
    with pytest.raises((ValueError, OSError)):
        service.restore({**binding, field: value})


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "protected", "protected_dir"])
def test_restore_checkpoint_and_export_reject_unsafe_outputs(tmp_path, kind):
    service, binding, _source, root, _cwd = sandbox(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me\n")
    if kind == "symlink":
        (root / "escaped").symlink_to(outside)
    elif kind == "hardlink":
        (root / "escaped").hardlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(root / "pipe")
    elif kind == "protected":
        (root / ".ENV").write_text("SECRET")
    else:
        (root / ".AWS").mkdir()
    for action in (service.restore, service.checkpoint, service.diff):
        with pytest.raises(ValueError, match="unsafe|hardlink|protected"):
            action(binding)
    assert outside.read_text() == "keep me\n"


@pytest.mark.parametrize("component", ["root", "ancestor", "cwd", "metadata"])
def test_restore_rejects_symlinks_at_each_storage_boundary(tmp_path, component):
    service, binding, _source, root, cwd = sandbox(tmp_path, "nested")
    path = {
        "root": root, "ancestor": service.directory,
        "cwd": cwd, "metadata": root.parent / "binding.json",
    }[component]
    moved = path.with_name(path.name + "-saved")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=component != "metadata")
    with pytest.raises((ValueError, OSError)):
        service.restore(binding)


def test_restore_rejects_root_replacement_even_with_same_contents(tmp_path):
    service, binding, _source, root, _cwd = sandbox(tmp_path)
    old = root.with_name("old")
    root.rename(old)
    shutil.copytree(old, root)
    with pytest.raises(ValueError, match="identity changed"):
        service.restore(binding)


@pytest.mark.parametrize("limit", ["file", "total", "count", "entries"])
def test_output_scan_enforces_resource_limits(tmp_path, monkeypatch, limit):
    service, binding, _source, root, _cwd = sandbox(tmp_path)
    constant, value = {
        "file": ("MAX_SOURCE_BYTES", 2), "total": ("MAX_SNAPSHOT_BYTES", 2),
        "count": ("MAX_SNAPSHOT_FILES", 1), "entries": ("MAX_SANDBOX_ENTRIES", 1),
    }[limit]
    (root / "extra").write_text("more")
    monkeypatch.setattr(execution_sandbox, constant, value)
    with pytest.raises(ValueError, match="limit"):
        service.checkpoint(binding)


def test_diff_handles_new_deleted_nonterminated_and_empty_files(tmp_path):
    service, binding, _source, root, _cwd = sandbox(tmp_path)
    (root / "sample.py").unlink()
    (root / "new.txt").write_text("new text")
    (root / "empty.txt").touch()
    patch = service.diff(binding)
    assert "--- a/sample.py\n+++ /dev/null\n" in patch
    assert "--- /dev/null\n+++ b/new.txt\n" in patch
    assert "+new text\n\\ No newline at end of file\n" in patch
    assert "diff --git a/empty.txt b/empty.txt\nnew file mode 100644\n" in patch


@pytest.mark.parametrize("kind", ["baseline", "binary", "filename", "size"])
def test_diff_fails_closed_for_unexportable_or_tampered_content(tmp_path, monkeypatch, kind):
    service, binding, _source, root, _cwd = sandbox(tmp_path)
    if kind == "baseline":
        (root.parent / "baseline" / "sample.py").chmod(0o600)
        (root.parent / "baseline" / "sample.py").write_text("tampered\n")
    elif kind == "binary":
        (root / "binary").write_bytes(b"\0\xff")
        assert service.checkpoint(binding)
    elif kind == "filename":
        (root / "space name").write_text("hello\n")
    else:
        (root / "sample.py").write_text("different\n")
        monkeypatch.setattr(execution_sandbox, "MAX_PATCH_BYTES", 1500)
        (root / "huge").write_text("x" * 2000)
    with pytest.raises(ValueError):
        service.diff(binding)


async def test_file_operations_run_fixed_helper_only_and_mount_whole_root(tmp_path):
    _service, _binding, source, root, cwd = sandbox(tmp_path, "nested")
    backend = FakeContainer()
    files = SandboxFileTools(backend, root)
    assert await files.write_file("sub/new.txt", "one\ntwo\n", cwd=cwd) == (
        "Wrote 8 bytes to sub/new.txt"
    )
    assert await files.edit_file("sub/new.txt", "two", "three", cwd=cwd) == "Edited sub/new.txt"
    assert (await files.read_file("sub/new.txt", cwd=cwd)).splitlines()[1:] == [
        "1: one", "2: three",
    ]
    assert (await files.read_file("/workspace/nested/sub/new.txt", cwd=cwd)).splitlines()[1:] == [
        "1: one", "2: three",
    ]
    assert await files.glob("**/*.txt", cwd=cwd) == "sub/new.txt"
    assert all(call_cwd == root for _payload, call_cwd in backend.calls)
    assert all(payload["cwd"] == "nested" for payload, _cwd in backend.calls)
    assert not (source / "nested" / "sub").exists()


async def test_file_tools_keep_builtin_signatures_through_executor(tmp_path):
    _service, _binding, _source, root, cwd = sandbox(tmp_path)
    backend = FakeContainer()
    files = SandboxFileTools(backend, root)
    executor = ToolExecutor(files.handlers, command_executor=backend)
    context = ToolContext(run_id="test", cwd=cwd, policy=PolicyEngine())
    written = await executor.execute(ToolCall("w", "write_file", {
        "path": "new.txt", "content": "hello\n",
    }), context)
    read = await executor.execute(ToolCall("r", "read_file", {"path": "new.txt"}), context)
    assert not written.is_error
    assert not read.is_error
    assert read.content.splitlines()[1:] == ["1: hello"]
    assert len(backend.calls) == 2


@pytest.mark.parametrize("path", [
    "../outside", "/etc/passwd", "/workspace-other/test", "/workspace/../outside",
    ".env", ".git/config", "nested/.AWS/key", "nested/credentials.json", "a\\b", "a\x00b",
])
async def test_container_file_paths_cannot_escape_or_access_protected_names(tmp_path, path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    files = SandboxFileTools(FakeContainer(), root)
    result = await files.write_file(path, "no")
    assert isinstance(result, CommandResult)
    assert result.is_error
    assert "Error:" in result.stdout


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
async def test_container_tools_refuse_unsafe_resources_without_touching_targets(tmp_path, kind):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    path = root / "unsafe"
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        path.hardlink_to(outside)
    else:
        os.mkfifo(path)
    files = SandboxFileTools(FakeContainer(), root)
    for result in [await files.read_file("unsafe"), await files.write_file("unsafe", "no"),
                   await files.glob("**")]:
        assert isinstance(result, CommandResult)
        assert result.is_error
    assert outside.read_text() == "untouched\n"


async def test_container_glob_recursion_hidden_paths_and_read_paging(tmp_path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    (root / ".hidden.py").write_text("hidden\n")
    (root / "nested").mkdir()
    (root / "nested" / "second.py").write_text("a\nb\nc\n")
    files = SandboxFileTools(FakeContainer(), root)
    assert await files.glob("**/*.py") == "nested/second.py\nsample.py"
    assert await files.glob(".*.py") == ".hidden.py"
    page = await files.read_file("nested/second.py", offset=1, limit=1)
    assert isinstance(page, str)
    assert "3 lines total; sha256=" in page
    assert "showing lines 2-2; continue with continuation " in page
    token = re.search(r"continue with continuation (\S+)\]", page).group(1)
    rest = await files.read_file("nested/second.py", continuation=token)
    assert isinstance(rest, str)
    assert "3: c" in rest
    assert "end of file" in rest
    assert "past the end" in await files.read_file("nested/second.py", offset=4)
    assert await files.glob("missing*") == "(no matches)"


async def test_container_python_isolated_mode_ignores_workspace_modules(tmp_path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    (root / "json.py").write_text("raise RuntimeError('workspace module was imported')\n")
    (root / "sitecustomize.py").write_text("raise RuntimeError('site module was imported')\n")
    files = SandboxFileTools(FakeContainer(), root)
    assert (await files.read_file("sample.py")).splitlines()[1:] == ["1: value = 1"]


async def test_cleanup_unknown_propagates_without_retry(tmp_path):
    _service, _binding, _source, root, cwd = sandbox(tmp_path)

    class UnknownContainer(FakeContainer):
        async def execute(self, command, cwd):
            self.calls.append(command)
            raise ContainerCleanupError("Container cleanup not confirmed")

    backend = UnknownContainer()
    files = SandboxFileTools(backend, root)
    executor = ToolExecutor(files.handlers, command_executor=backend)
    result = await executor.execute(
        ToolCall("w", "write_file", {"path": "new", "content": "text"}),
        ToolContext(run_id="test", cwd=cwd, policy=PolicyEngine()),
    )
    assert result.is_error
    assert result.metadata["outcome_unknown"]
    assert result.metadata["code"] == "outcome_unknown"
    assert len(backend.calls) == 1


async def test_file_backend_and_working_root_are_pinned(tmp_path):
    _service, _binding, _source, root, _cwd = sandbox(tmp_path)
    with pytest.raises(ValueError, match="container"):
        SandboxFileTools(LocalCommandExecutor(), root)
    files = SandboxFileTools(FakeContainer(), root)
    with pytest.raises(ValueError, match="escapes"):
        await files.read_file("sample.py", cwd=tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        await files.read_file("sample.py", cwd=root / "nested" / "..")
    root.rename(root.with_name("old"))
    root.mkdir()
    with pytest.raises(ValueError, match="identity"):
        await files.read_file("sample.py")


def test_source_snapshot_copy_rejects_file_swap_to_symlink(tmp_path, monkeypatch):
    source = make_repo(tmp_path / "source")
    target = source / "race.txt"
    target.write_text("safe\n")
    outside = tmp_path / "outside"
    outside.write_text("must not read\n")
    original_open = os.open
    swapped = False

    def swap(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "race.txt" and dir_fd is not None and not swapped:
            swapped = True
            target.unlink()
            target.symlink_to(outside)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(workers.os, "open", swap)
    with pytest.raises(OSError):
        workers.SourceSnapshot.create(source, source, tmp_path / "state", secure_copy=True)
    assert swapped


@pytest.mark.parametrize("change", ["new", "deleted", "no_newline", "empty_new", "empty_deleted"])
def test_export_passes_git_apply_check_against_original_source(tmp_path, change):
    source = make_repo(tmp_path / "source")
    (source / "sample.txt").write_text("old\n" if change != "empty_deleted" else "")
    subprocess.run(["git", "add", "sample.txt"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
    service = SandboxWorkspaces(tmp_path / "state")
    binding = service.create("workspace", source, ".")
    root, _cwd = service.restore(binding)
    if change == "new":
        (root / "new.txt").write_text("new text\n")
    elif change in {"deleted", "empty_deleted"}:
        (root / "sample.txt").unlink()
    elif change == "no_newline":
        (root / "sample.txt").write_text("replacement without newline")
    else:
        (root / "new.txt").touch()
    patch = service.diff(binding)
    assert patch
    result = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=source, input=patch,
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert (source / "sample.txt").read_text() == (
        "" if change == "empty_deleted" else "old\n"
    )
    assert not (source / "new.txt").exists()


async def test_container_edit_then_read_and_export_keeps_source_unchanged(tmp_path):
    service, binding, source, root, _cwd = sandbox(tmp_path)
    files = SandboxFileTools(FakeContainer(), root)
    assert await files.edit_file("sample.py", "value = 1", "value = 2") == "Edited sample.py"
    assert (await files.read_file("sample.py")).splitlines()[1:] == ["1: value = 2"]
    assert not (root / "__pycache__").exists()
    patch = service.diff(binding)
    result = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=source, input=patch,
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert (source / "sample.py").read_text() == "value = 1\n"


def test_secure_copy_opt_in_preserves_original_snapshot_without_nofollow(tmp_path, monkeypatch):
    source = make_repo(tmp_path / "source")
    (source / "sample.txt").write_text("hello\n")
    monkeypatch.delattr(os, "O_NOFOLLOW")
    copied = workers.SourceSnapshot.create(source, source, tmp_path / "state")
    try:
        assert (copied.root / "sample.txt").read_text() == "hello\n"
    finally:
        copied.cleanup()
    with pytest.raises(ValueError, match="no-follow"):
        workers.SourceSnapshot.create(source, source, tmp_path / "state", secure_copy=True)
