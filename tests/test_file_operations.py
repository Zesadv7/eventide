"""Actual per-operation recovery, fault windows and control-plane boundaries."""

import asyncio
import json
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.file_operations import MAX_FILE_BYTES, FileOperationConflict, FileOperationUnknown
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.secrets import SecretBox
from tests.test_host import make_repo
from tests.test_runtime import settings_for


@pytest.fixture
def host(isolated_workspace):
    repo = make_repo(isolated_workspace / "project")
    runtime = RuntimeHost(
        replace(settings_for(repo), state_dir=isolated_workspace / "state"),
        ScriptedProvider([]),
        enable_mcp=False,
    )
    yield runtime
    asyncio.run(runtime.close())


def write(host, path="code.py", content="new\n", *, tool="write_file", session=None, **arguments):
    session = session or host.create_session()
    run = host.store.create_run("run_" + os.urandom(8).hex(), session)
    # create_run returns turn_id, the persistent identity is chosen by us.
    run_id = host.store.latest_run(session)["id"]
    result = host.file_operations.write(
        tool,
        {"path": path, "content": content, **arguments},
        run_id=run_id,
        call_id="call",
        workspace_id=host.default_workspace.workspace_id,
        cwd=host.settings.workdir,
    )
    assert run
    return host.file_operations.get(session, result.metadata["file_operation_id"])


def test_diff_and_undo_preserve_actual_dirty_bytes_attributes(host):
    target = host.settings.workdir / "code.py"
    target.write_bytes(b"# committed\r\nold\r\n")
    # No Git HEAD lookup: these pre-existing dirty changes are the recovery baseline.
    target.write_bytes(b"# user uncommitted\r\nold\r\n")
    target.chmod(0o751)
    if sys.platform == "darwin":
        subprocess.run(
            ["xattr", "-w", "user.eventide", "user metadata", str(target)],
            check=True,
            capture_output=True,
        )
    else:
        os.setxattr(target, "user.eventide", b"user metadata")
    os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
    before = target.stat()
    record = write(host, tool="edit_file", old_text="old", new_text="new")
    detail = host.file_operations.detail(record["session_id"], record["operation_id"])
    assert "-old\r\n+new\r\n" in detail["patch"]
    assert " # user uncommitted\r\n" in detail["patch"]
    assert record["change_kind"] == "modified"
    undone = host.file_operations.undo(record["session_id"], record["operation_id"])
    assert undone["status"] == "undone"
    after = target.stat()
    assert after.st_ino == before.st_ino
    assert stat.S_IMODE(after.st_mode) == 0o751
    assert after.st_mtime_ns == before.st_mtime_ns
    if sys.platform == "darwin":
        assert (
            subprocess.check_output(["xattr", "-p", "user.eventide", str(target)]).strip()
            == b"user metadata"
        )
    else:
        assert os.getxattr(target, "user.eventide") == b"user metadata"
    assert target.read_bytes() == b"# user uncommitted\r\nold\r\n"
    assert host.file_operations.undo(record["session_id"], record["operation_id"]) == undone
    events = host.store.session_events(record["session_id"])
    assert [e["payload"]["status"] for e in events if e["type"] == "file.operation"] == [
        "prepared",
        "completed",
        "undo_prepared",
        "undone",
    ]
    assert "user uncommitted" not in json.dumps(events)
    assert "user uncommitted" not in host.store.file_operation_backup(record["operation_id"])


@pytest.mark.parametrize("content", ["", "<script>alert('hello')</script>", "new\n"])
def test_new_file_diff_undo_only_removes_file(host, content):
    record = write(host, path="new/nested.py", content=content)
    assert record["change_kind"] == "added"
    detail = host.file_operations.detail(record["session_id"], record["operation_id"])
    if content:
        assert "--- /dev/null\n" in detail["patch"]
        assert "+" + content in detail["patch"]
    root = host.settings.workdir
    (root / "new" / "other").write_text("keep")
    host.file_operations.undo(record["session_id"], record["operation_id"])
    assert not (root / "new" / "nested.py").exists()
    assert (root / "new" / "other").read_text() == "keep"


def test_later_edits_and_reverse_order_undo(host):
    target = host.settings.workdir / "code.py"
    target.write_text("user dirty\n")
    a = write(host, content="a\n")
    b = write(host, content="b\n", session=a["session_id"])
    with pytest.raises(FileOperationConflict, match="冲突"):
        host.file_operations.undo(a["session_id"], a["operation_id"])
    assert target.read_text() == "b\n"
    host.file_operations.undo(b["session_id"], b["operation_id"])
    host.file_operations.undo(a["session_id"], a["operation_id"])
    assert target.read_text() == "user dirty\n"
    c = write(host)
    target.write_text("later external edit")
    with pytest.raises(FileOperationConflict, match="冲突"):
        host.file_operations.undo(c["session_id"], c["operation_id"])
    assert target.read_text() == "later external edit"


def test_no_change_has_no_undo(host):
    (host.settings.workdir / "code.py").write_text("same")
    record = write(host, content="same")
    assert record["status"] == "no_change"
    assert record["change_kind"] == "unchanged"
    assert host.file_operations.detail(record["session_id"], record["operation_id"])["patch"] == ""
    with pytest.raises(FileOperationConflict):
        host.file_operations.undo(record["session_id"], record["operation_id"])


@pytest.mark.parametrize(
    "boundary",
    [
        "outside",
        "link",
        "parent_link",
        "hardlink",
        "fifo",
        "binary",
        "large",
        "new_binary",
        "new_large",
        "state",
    ],
)
def test_unsafe_or_unsupported_rejected_before_mutation(host, boundary):
    root = host.settings.workdir
    target = root / "code.py"
    target.write_bytes(b"original")
    path, content = "code.py", "new"
    outside = root.parent / "outside"
    outside.write_bytes(b"outside")
    if boundary == "outside":
        path = "../outside"
    elif boundary == "link":
        target.unlink()
        target.symlink_to(outside)
    elif boundary == "parent_link":
        (root / "linked").symlink_to(root.parent, target_is_directory=True)
        path = "linked/outside"
    elif boundary == "hardlink":
        os.link(target, root / "alias")
    elif boundary == "fifo":
        target.unlink()
        os.mkfifo(target)
    elif boundary == "binary":
        target.write_bytes(b"\x00raw")
    elif boundary == "large":
        target.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    elif boundary == "new_binary":
        content = "\x00new"
    elif boundary == "new_large":
        content = "x" * (MAX_FILE_BYTES + 1)
    elif boundary == "state":
        path = str(host.store.path)
    with pytest.raises((ValueError, PermissionError, OSError)):
        write(host, path=path, content=content)
    assert host.file_operations.records() == []
    assert outside.read_bytes() == b"outside"
    if boundary not in {"link", "fifo", "binary", "large"}:
        assert target.read_bytes() == b"original"


def test_backup_failure_never_writes(host, monkeypatch):
    target = host.settings.workdir / "code.py"
    target.write_text("old")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(host.store, "prepare_file_operation", fail)
    with pytest.raises(OSError, match="disk full"):
        write(host)
    assert target.read_text() == "old"
    assert not host.file_operations.records()


@pytest.mark.parametrize("failure", ["write", "completion", "all_journal"])
def test_post_backup_failures_never_report_success(host, monkeypatch, failure):
    target = host.settings.workdir / "code.py"
    target.write_text("old")
    original_record = host.file_operations._record
    if failure == "write":

        def partial(*args, **kwargs):
            target.write_text("partial")
            raise OSError("injected partial write")

        monkeypatch.setattr(host.file_operations, "_write_file", partial)
    else:

        def fail_record(record, status, **extra):
            if failure == "all_journal" or status == "completed":
                raise OSError("injected completion journal failure")
            return original_record(record, status, **extra)

        monkeypatch.setattr(host.file_operations, "_record", fail_record)
    with pytest.raises(FileOperationUnknown):
        write(host)
    record = host.file_operations.records()[0]
    assert record["status"] in {"prepared", "needs_review"}
    assert target.read_text() == ("partial" if failure == "write" else "new\n")
    with pytest.raises(FileOperationConflict):
        host.file_operations.undo(record["session_id"], record["operation_id"])
    monkeypatch.setattr(host.file_operations, "_record", original_record)
    host.file_operations.recover()
    assert host.file_operations.records()[0]["status"] == "needs_review"


@pytest.mark.parametrize("window", ["prepared", "changed_before_complete", "undo_prepared"])
def test_crash_windows_recovered_conservatively(host, monkeypatch, window):
    target = host.settings.workdir / "code.py"
    target.write_text("old")

    class Crash(BaseException):
        pass

    if window in {"prepared", "changed_before_complete"}:

        def crash(*args, **kwargs):
            if window == "changed_before_complete":
                target.write_text("new\n")
            raise Crash()

        monkeypatch.setattr(host.file_operations, "_write_file", crash)
        with pytest.raises(Crash):
            write(host)
    else:
        record = write(host)

        def crash(*args, **kwargs):
            raise Crash()

        monkeypatch.setattr(host.file_operations, "_write_file", crash)
        with pytest.raises(Crash):
            host.file_operations.undo(record["session_id"], record["operation_id"])
    assert host.file_operations.records()[0]["status"] in {"prepared", "undo_prepared"}
    settings = host.settings
    asyncio.run(host.close())
    restarted = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        record = restarted.file_operations.records()[0]
        assert record["status"] == "needs_review"
        with pytest.raises(FileOperationConflict):
            restarted.file_operations.undo(record["session_id"], record["operation_id"])
        assert restarted.file_operations.detail(record["session_id"], record["operation_id"])[
            "patch"
        ]
    finally:
        asyncio.run(restarted.close())


def test_completed_and_undone_survive_restart(host):
    record = write(host)
    settings = host.settings
    asyncio.run(host.close())
    restarted = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        assert restarted.file_operations.get(record["session_id"], record["operation_id"]) == record
        restarted.file_operations.undo(record["session_id"], record["operation_id"])
    finally:
        asyncio.run(restarted.close())
    restarted = RuntimeHost(settings, ScriptedProvider([]), enable_mcp=False)
    try:
        assert (
            restarted.file_operations.get(record["session_id"], record["operation_id"])["status"]
            == "undone"
        )
    finally:
        asyncio.run(restarted.close())


def test_concurrent_controlled_write_and_undo_do_not_lose_edits(host, monkeypatch):
    (host.settings.workdir / "code.py").write_text("dirty")
    first = write(host, content="a")
    entered, release, attempting = threading.Event(), threading.Event(), threading.Event()
    original = host.file_operations._write_file

    def pause(root, relative, raw, *args, **kwargs):
        if raw == b"b":
            entered.set()
            assert release.wait(5)
        return original(root, relative, raw, *args, **kwargs)

    monkeypatch.setattr(host.file_operations, "_write_file", pause)

    def undo():
        attempting.set()
        return host.file_operations.undo(first["session_id"], first["operation_id"])

    with ThreadPoolExecutor(2) as pool:
        writing = pool.submit(write, host, content="b", session=first["session_id"])
        assert entered.wait(5)
        undoing = pool.submit(undo)
        assert attempting.wait(5)
        assert not undoing.done()
        release.set()
        second = writing.result()
        with pytest.raises(FileOperationConflict):
            undoing.result()
    assert (host.settings.workdir / "code.py").read_text() == "b"
    host.file_operations.undo(second["session_id"], second["operation_id"])
    host.file_operations.undo(first["session_id"], first["operation_id"])
    assert (host.settings.workdir / "code.py").read_text() == "dirty"


def test_secret_creation_is_safe_across_instances(host):
    root = host.settings.state_dir
    with ThreadPoolExecutor(8) as pool:
        encrypted = list(pool.map(lambda i: SecretBox(root).encrypt(str(i)), range(20)))
    assert [SecretBox(root).decrypt(value) for value in encrypted] == [str(i) for i in range(20)]


def test_long_diff_is_not_truncated_recovery(host):
    (host.settings.workdir / "code.py").write_text("old" * 100_000)
    record = write(host, content="new" * 100_000)
    detail = host.file_operations.detail(record["session_id"], record["operation_id"])
    assert detail["diff_status"] == "too_large" and detail["patch"] is None
    host.file_operations.undo(record["session_id"], record["operation_id"])
    assert (host.settings.workdir / "code.py").read_text() == "old" * 100_000


async def test_actual_host_executor_metadata_and_model_does_not_receive_backup(host):
    target = host.settings.workdir / "code.py"
    target.write_text("user secret old")
    host.provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "edit",
                        "name": "edit_file",
                        "arguments": {"path": "code.py", "old_text": "old", "new_text": "new"},
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    result = await host.run(RunRequest("edit", mode="agent"))
    assert result.status == "completed"
    event = next(e for e in host.store.run_events(result.run_id) if e["type"] == "tool.completed")
    operation_id = event["payload"]["metadata"]["file_operation_id"]
    assert operation_id
    assert not any(t["name"] == "undo" for t in host.tools)
    assert "user secret old" not in str(host.provider.requests)
    assert target.read_text() == "user secret new"
    undone = await host.undo_file_operation(result.session_id, operation_id)
    assert undone["status"] == "undone"
    assert target.read_text() == "user secret old"


def test_api_permissions_binding_conflict_and_idempotency(host):
    record = write(host)
    session, op = record["session_id"], record["operation_id"]
    route = f"/api/sessions/{session}/file-operations/{op}"
    with TestClient(create_app(host)) as client:
        assert client.get(route).status_code == 200
        assert client.get(route, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert client.post(route + "/undo").status_code == 422
        assert (
            client.post(route + "/undo", headers={"Origin": "https://evil.invalid"}).status_code
            == 403
        )
        other = host.create_session()
        assert client.get(route.replace(session, other)).status_code == 409
        (host.settings.workdir / "code.py").write_text("later")
        headers = {"Idempotency-Key": "undo-1"}
        assert client.post(route + "/undo", headers=headers).status_code == 409
        assert (host.settings.workdir / "code.py").read_text() == "later"
        (host.settings.workdir / "code.py").write_text("new\n")
        headers = {"Idempotency-Key": "undo-2"}
        response = client.post(route + "/undo", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "undone"
        assert client.post(route + "/undo", headers=headers).json() == response.json()
        assert (
            client.post(route + "/undo", headers={"Idempotency-Key": "undo-3"}).json()
            == response.json()
        )
        assert (
            client.get(f"/api/sessions/{session}/file-operations").json()["operations"][0]["status"]
            == "undone"
        )


def test_api_revalidates_symlink_and_workspace_identity(host):
    record = write(host)
    path = host.settings.workdir / "code.py"
    path.unlink()
    path.symlink_to(host.settings.workdir.parent / "outside")
    route = f"/api/sessions/{record['session_id']}/file-operations/{record['operation_id']}"
    with TestClient(create_app(host)) as client:
        assert client.get(route).status_code in {403, 409}
        assert client.post(route + "/undo", headers={"Idempotency-Key": "link"}).status_code in {
            403,
            409,
        }


@pytest.mark.parametrize("failure", ["restore", "completion"])
def test_undo_failure_is_unknown_and_never_replayed(host, monkeypatch, failure):
    target = host.settings.workdir / "code.py"
    target.write_text("dirty")
    record = write(host)
    if failure == "restore":

        def fail_restore(*args, **kwargs):
            target.write_text("part")
            raise OSError("partial restore failure")

        monkeypatch.setattr(host.file_operations, "_write_file", fail_restore)
    else:
        original = host.file_operations._record

        def fail_completion(record, status, **extra):
            if status == "undone":
                raise OSError("undo journal failure")
            return original(record, status, **extra)

        monkeypatch.setattr(host.file_operations, "_record", fail_completion)
    with pytest.raises(FileOperationUnknown):
        host.file_operations.undo(record["session_id"], record["operation_id"])
    assert (
        host.file_operations.get(record["session_id"], record["operation_id"])["status"]
        == "needs_review"
    )
    with pytest.raises(FileOperationConflict):
        host.file_operations.undo(record["session_id"], record["operation_id"])
    assert target.read_text() == ("part" if failure == "restore" else "dirty")


def test_backup_corruption_and_missing_key_do_not_modify_file(host, monkeypatch):
    from eventide.secrets import SecretKeyError

    record = write(host)
    target = host.settings.workdir / "code.py"
    original = host.store.file_operation_backup
    monkeypatch.setattr(host.store, "file_operation_backup", lambda _op: "bad ciphertext")
    with pytest.raises(SecretKeyError):
        host.file_operations.undo(record["session_id"], record["operation_id"])
    assert target.read_text() == "new\n"
    monkeypatch.setattr(host.store, "file_operation_backup", original)
    host.file_operations.box = SecretBox(host.settings.state_dir)
    (host.settings.state_dir / "secret.key").unlink()
    with pytest.raises(SecretKeyError):
        host.file_operations.undo(record["session_id"], record["operation_id"])
    assert target.read_text() == "new\n"


def test_policy_and_active_workspace_rechecked_by_api(host, monkeypatch):
    from eventide.policy import PolicyDecision, PolicyResult

    record = write(host)
    route = f"/api/sessions/{record['session_id']}/file-operations/{record['operation_id']}"
    with TestClient(create_app(host)) as client:
        with monkeypatch.context() as patch:
            patch.setattr(host, "workspace_busy", lambda _id: True)
            response = client.post(route + "/undo", headers={"Idempotency-Key": "busy"})
            assert response.status_code == 409
        monkeypatch.setattr(
            host.policy,
            "evaluate",
            lambda *args, **kwargs: PolicyResult(PolicyDecision.DENY, "policy changed"),
        )
        assert (
            client.post(route + "/undo", headers={"Idempotency-Key": "denied"}).status_code == 403
        )
        assert (host.settings.workdir / "code.py").read_text() == "new\n"


async def test_worktree_operation_targets_execution_copy_and_subdirectory(host):
    source = host.settings.workdir
    (source / "src").mkdir()
    (source / "src" / "code.py").write_text("original\n")
    import subprocess

    subprocess.run(["git", "-C", str(source), "add", "src"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "fixture"], check=True, capture_output=True
    )
    session = host.create_session(working_directory="src")
    host.provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "edit",
                        "name": "edit_file",
                        "arguments": {
                            "path": "code.py",
                            "old_text": "original",
                            "new_text": "changed",
                        },
                    }
                ]
            },
            {"text": "done"},
        ]
    )
    result = await host.run(
        RunRequest("edit", session_id=session, mode="agent", execution_target="worktree")
    )
    assert result.status == "completed"
    record = host.file_operations.records(session)[0]
    assert record["workspace_id"] != host.default_workspace.workspace_id
    assert record["cwd"] == "src"
    copy = host.store.get_workspace(record["workspace_id"])
    assert (source / "src" / "code.py").read_text() == "original\n"
    assert (
        host.settings.workdir.__class__(copy.path) / "src" / "code.py"
    ).read_text() == "changed\n"
    await host.undo_file_operation(session, record["operation_id"])
    assert (source / "src" / "code.py").read_text() == "original\n"
    assert (
        host.settings.workdir.__class__(copy.path) / "src" / "code.py"
    ).read_text() == "original\n"


def test_schema_five_migration_preserves_existing_facts(host):
    import sqlite3

    from eventide.store import RuntimeStore

    session = host.create_session()
    host.store.append_fact(session, "memory.settings", {"revision": 1, "enabled": False})
    before = host.store.session_events(session)
    path = host.store.path
    asyncio.run(host.close())
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE file_operation_backups")
        connection.execute("DELETE FROM schema_migrations WHERE version=6")
    store = RuntimeStore(path)
    try:
        assert store.session_events(session) == before
        assert (
            store._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 6
        )
    finally:
        store.close()


async def test_completion_record_failure_parks_actual_host(host, monkeypatch):
    target = host.settings.workdir / "code.py"
    target.write_text("old")
    host.provider = ScriptedProvider(
        [
            {
                "tool_calls": [
                    {
                        "id": "edit",
                        "name": "edit_file",
                        "arguments": {"path": "code.py", "old_text": "old", "new_text": "new"},
                    }
                ]
            },
            {"text": "must not finalize"},
        ]
    )
    original = host.file_operations._record

    def fail(record, status, **extra):
        if status == "completed":
            raise OSError("journal unavailable")
        return original(record, status, **extra)

    monkeypatch.setattr(host.file_operations, "_record", fail)
    result = await host.run(RunRequest("edit", mode="agent"))
    assert result.status == "interrupted"
    events = host.store.run_events(result.run_id)
    assert any(e["type"] == "tool.outcome_unknown" for e in events)
    assert not any(e["type"] == "tool.completed" for e in events)
    assert host.file_operations.records(result.session_id)[0]["status"] == "needs_review"
    assert len(host.provider.requests) == 1
    assert target.read_text() == "new"


def test_http_receipt_failure_does_not_repeat_undo(host, monkeypatch):
    import eventide.http_api.file_operations as api_module

    record = write(host)
    route = f"/api/sessions/{record['session_id']}/file-operations/{record['operation_id']}/undo"

    def receipt_failure(*args):
        raise OSError("receipt commit failure")

    monkeypatch.setattr(api_module, "finish_mutation", receipt_failure)
    with TestClient(create_app(host), raise_server_exceptions=False) as client:
        headers = {"Idempotency-Key": "receipt-failure"}
        assert client.post(route, headers=headers).status_code == 500
        assert client.post(route, headers=headers).status_code == 409
        assert (
            host.file_operations.get(record["session_id"], record["operation_id"])["status"]
            == "undone"
        )
        assert not (host.settings.workdir / "code.py").exists()
        events = host.store.session_events(record["session_id"])
        assert (
            sum(
                e["type"] == "file.operation" and e["payload"]["status"] == "undone" for e in events
            )
            == 1
        )


def test_nested_state_cwd_and_case_aliases_cannot_bypass_protection(host):
    nested = RuntimeHost(
        replace(host.settings, state_dir=host.settings.workdir / ".eventide"),
        ScriptedProvider([]),
        enable_mcp=False,
    )
    try:
        state = nested.settings.state_dir
        (state / "attachments").mkdir()
        session = nested.create_session(working_directory=".eventide/attachments")
        run_id = "run_nested_state_guard"
        nested.store.create_run(run_id, session)
        with pytest.raises(PermissionError, match="Runtime state"):
            nested.file_operations.write(
                "write_file",
                {"path": "record", "content": "bad"},
                run_id=run_id,
                call_id="bad",
                workspace_id=nested.default_workspace.workspace_id,
                cwd=state / "attachments",
            )
        for path in (".EVENTIDE/secret.key", ".GiT/config"):
            with pytest.raises(PermissionError):
                write(nested, path=path)
        git_session = nested.create_session(working_directory=".git")
        nested.store.create_run("run_git_metadata_guard", git_session)
        with pytest.raises(PermissionError, match="Git metadata"):
            nested.file_operations.write(
                "write_file",
                {"path": "config", "content": "bad"},
                run_id="run_git_metadata_guard",
                call_id="bad-git",
                workspace_id=nested.default_workspace.workspace_id,
                cwd=nested.settings.workdir / ".git",
            )
        assert nested.file_operations.records() == []
        assert not (state / "attachments" / "record").exists()
    finally:
        asyncio.run(nested.close())


def test_interrupted_schema_migration_can_reopen_existing_artifact_table(host):
    import sqlite3

    from eventide.store import RuntimeStore

    path = host.store.path
    asyncio.run(host.close())
    # Simulate interruption after SQLite DDL committed but before the version row.
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=6")
    store = RuntimeStore(path)
    try:
        assert (
            store._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 6
        )
    finally:
        store.close()
