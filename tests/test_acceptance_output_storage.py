"""Archive failure injection, bounded storage, permissions and lifecycle."""

import errno
import hashlib
import shlex
import sys
from pathlib import Path

import pytest

from eventide.acceptance_output import AcceptanceOutputs, OutputLimits
from eventide.executor import LocalCommandExecutor
from eventide.normalization import normalize
from eventide.store import RuntimeStore
from eventide.tool_results import reader


@pytest.fixture
def archive(tmp_path):
    store = RuntimeStore(tmp_path / "state/runtime.sqlite")
    workspace = store.register_workspace(tmp_path, None)
    store.create_session("owner", workspace.workspace_id)
    store.create_session("other", workspace.workspace_id)
    store.create_run("control", "owner")
    manager = AcceptanceOutputs(store, tmp_path / "state")
    try:
        yield manager, store
    finally:
        store.close()


def begin(manager, call="check", *, run="control", secrets=()):
    return manager.start(
        "owner", run, call, secrets=secrets,
        clean=lambda text: normalize(text, secrets),
    )


def test_utf8_secret_boundaries_and_character_pages(archive):
    manager, store = archive
    secret = "sk-demo-sensitive-value"
    capture = begin(manager, secrets=(secret,))
    raw = ('前文\n"' + secret + '"\\中段🙂\n尾部').encode()
    for start in range(0, len(raw), 2):
        capture.feed("stdout", raw[start:start + 2])
    capture.feed("stderr", "另一条流\n".encode())
    evidence = capture.finish("complete")
    expected = raw.decode().replace(secret, "[REDACTED]")
    stdout = evidence["streams"]["stdout"]
    assert stdout["received_bytes"] == len(raw)
    assert stdout["stored_bytes"] == len(expected.encode())
    assert stdout["stored_chars"] == len(expected)
    assert stdout["sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    read = reader(store, "owner", outputs=manager)
    pages = [read("control", "check", stream="stdout", offset=i, limit=3).split("\n", 1)[1]
             for i in range(0, len(expected), 3)]
    assert "".join(pages) == expected
    assert "另一条流" in read("control", "check", stream="stderr")
    assert all(secret.encode() not in path.read_bytes() for path in manager.root.rglob("*.log"))


def test_stream_quota_retains_prefix_and_counts_all_received_bytes(archive):
    manager, store = archive
    manager.limits = OutputLimits(stream_bytes=32)
    capture = begin(manager)
    capture.feed("stdout", b"a" * 20)
    capture.feed("stdout", b"b" * 50)
    capture.feed("stdout", b"c" * 100_000)
    value = capture.finish("complete")["streams"]["stdout"]
    assert value["capture_state"] == "quota_exceeded" and value["reason"] == "stream_quota"
    assert value["stored_bytes"] == 32 and value["received_bytes"] == 100_070
    page = reader(store, "owner", outputs=manager)("control", "check", stream="stdout")
    assert page.endswith("a" * 20 + "b" * 12)
    assert "capture_state=quota_exceeded" in page


def test_control_run_and_global_quota_are_shared_across_calls_and_streams(archive):
    manager, store = archive
    manager.limits = OutputLimits(stream_bytes=100, run_bytes=60, total_bytes=80)
    first = begin(manager, "first")
    second = begin(manager, "second")
    first.feed("stdout", b"a" * 40)
    second.feed("stderr", b"b" * 30)
    assert second.finish("complete")["streams"]["stderr"]["reason"] == "run_quota"
    first.finish("complete")
    store.create_run("another", "owner")
    third = begin(manager, "third", run="another")
    third.feed("stdout", b"c" * 40)
    assert third.finish("complete")["streams"]["stdout"]["reason"] == "global_quota"
    assert manager.total_bytes == 80
    recovered = AcceptanceOutputs(store, manager.root.parent, limits=manager.limits)
    assert recovered.total_bytes == 80
    assert sorted(recovered.run_bytes.values()) == [20, 60]


def test_write_error_continues_counting_received_bytes(archive, monkeypatch):
    manager, store = archive
    capture = begin(manager)
    file = capture.streams["stdout"].file

    def fail(_data):
        raise OSError(errno.ENOSPC, "injected full disk")

    monkeypatch.setattr(file, "write", fail)
    capture.feed("stdout", b"x" * 50_000)
    capture.feed("stdout", b"y" * 50_000)
    value = capture.finish("complete")["streams"]["stdout"]
    assert value["capture_state"] == "write_failed"
    assert value["received_bytes"] == 100_000 and value["stored_bytes"] == 0
    assert "capture_state=write_failed" in reader(store, "owner", outputs=manager)(
        "control", "check", stream="stdout",
    )


async def test_disk_failure_does_not_block_real_subprocess_pipes(archive, monkeypatch, tmp_path):
    manager, _store = archive
    capture = begin(manager)

    def fail(_data):
        raise OSError(errno.ENOSPC, "injected full disk")

    monkeypatch.setattr(capture.streams["stdout"].file, "write", fail)
    program = "import sys; sys.stdout.write('x'*200_000); sys.stderr.write('done')"
    command = shlex.quote(sys.executable) + " -B -c " + shlex.quote(program)
    result = await LocalCommandExecutor(timeout=3, output_observer=capture.feed).execute(
        command, tmp_path,
    )
    evidence = capture.finish("complete")
    assert result.exit_code == 0 and not result.timed_out
    assert evidence["streams"]["stdout"]["received_bytes"] == 200_000
    assert evidence["streams"]["stdout"]["capture_state"] == "write_failed"
    assert evidence["streams"]["stderr"]["capture_state"] == "complete"


def test_open_failure_and_redaction_unavailable_are_explicit(archive, monkeypatch):
    manager, store = archive
    with monkeypatch.context() as patch:
        patch.setattr("eventide.acceptance_output.os.open", lambda *_a, **_k: (
            (_ for _ in ()).throw(OSError(errno.ENOSPC, "injected open failure"))
        ))
        capture = begin(manager, "disk")
    capture.feed("stdout", b"not saved")
    assert capture.finish("complete")["streams"]["stdout"]["reason"] == "open_failed"
    assert "availability=not_saved" in reader(store, "owner", outputs=manager)(
        "control", "disk", stream="stdout",
    )
    secret = "s" * 65_538
    capture = begin(manager, "redaction", secrets=(secret,))
    capture.feed("stdout", secret.encode())
    value = capture.finish("complete")["streams"]["stdout"]
    assert value["capture_state"] == "redaction_unavailable"
    assert value["stored_bytes"] == 0 and value["received_bytes"] == len(secret)


def test_sync_error_is_not_published_as_complete(archive, monkeypatch):
    manager, _store = archive
    capture = begin(manager)
    capture.feed("stdout", b"saved prefix")
    with monkeypatch.context() as patch:
        patch.setattr("eventide.acceptance_output.os.fsync", lambda _fd: (
            (_ for _ in ()).throw(OSError(errno.EIO, "injected sync failure"))
        ))
        value = capture.finish("complete")["streams"]["stdout"]
    assert value["capture_state"] == "write_failed" and value["reason"] == "sync_failed"
    assert not value["sealed"] and value["stored_bytes"] == 12


def test_finalize_path_error_preserves_both_stream_records(archive, monkeypatch):
    manager, _store = archive
    capture = begin(manager)
    capture.feed("stdout", b"saved stdout")
    capture.feed("stderr", b"saved stderr")
    original = manager._path

    def deny_seal(*args, **kwargs):
        if kwargs["sealed"]:
            raise OSError(errno.ELOOP, "injected changed ancestor")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_path", deny_seal)
    evidence = capture.finish("complete")
    for stream in evidence["streams"].values():
        assert stream["capture_state"] == "write_failed"
        assert stream["reason"] == "finalize_failed" and stream["stored_bytes"] == 12


def test_invalid_utf8_is_declared_and_empty_stream_is_not_missing(archive):
    manager, store = archive
    capture = begin(manager)
    capture.feed("stdout", b"before\xffafter")
    value = capture.finish("complete")["streams"]["stdout"]
    assert value["encoding_loss"] is True
    read = reader(store, "owner", outputs=manager)
    assert read("control", "check", stream="stdout").endswith("before\ufffdafter")
    empty = read("control", "check", stream="stderr")
    assert "availability=available" in empty and "end_of_stream" in empty


@pytest.mark.parametrize("renamed", [False, True])
def test_restart_never_promotes_unpublished_files_to_complete(archive, renamed):
    manager, store = archive
    capture = begin(manager)
    capture.feed("stdout", "崩溃前的片段".encode())
    for stream in capture.streams.values():
        stream.file.close()
        if renamed:
            stream.path.rename(stream.path.with_suffix(".log"))
    store.finish_run("control", status="interrupted", reason="process_restart")
    restarted = AcceptanceOutputs(store, manager.root.parent)
    restarted.recover()
    value = store.get_acceptance_output("owner", "control", "check")["streams"]["stdout"]
    assert value["capture_state"] == "interrupted" and value["received_bytes"] is None
    assert "崩溃前的片段" in reader(store, "owner", outputs=restarted)(
        "control", "check", stream="stdout",
    )


def test_active_protection_expiry_and_restart(archive):
    manager, store = archive
    manager.clock = lambda: 10**12
    capture = begin(manager)
    capture.feed("stdout", b"keep active evidence")
    capture.finish("complete")
    manager.cleanup()
    assert manager.total_bytes == 20 and not manager.expired
    store.finish_run("control", status="completed")
    manager.cleanup()
    assert manager.total_bytes == 0 and manager.expired
    restarted = AcceptanceOutputs(store, manager.root.parent, clock=manager.clock)
    page = reader(store, "owner", outputs=restarted)("control", "check", stream="stdout")
    assert "availability=expired" in page


def test_delete_failure_stays_charged_and_is_recorded(archive, monkeypatch):
    manager, store = archive
    capture = begin(manager)
    capture.feed("stdout", b"keep")
    capture.finish("complete")
    store.finish_run("control", status="completed")
    manager.clock = lambda: 10**12
    original = Path.unlink

    def deny(path, *args, **kwargs):
        if path.name == "stdout.log":
            raise OSError(errno.EACCES, "injected deletion failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", deny)
        manager.cleanup()
    assert manager.total_bytes == 4
    assert any(e["type"] == "acceptance.output_cleanup_failed"
               for e in store.acceptance_output_events())
    manager.cleanup()
    assert manager.total_bytes == 0


@pytest.mark.parametrize("change", ["missing", "corrupt", "symlink"])
def test_missing_modified_and_linked_files_are_not_returned(archive, tmp_path, change):
    manager, store = archive
    capture = begin(manager)
    capture.feed("stdout", b"evidence")
    capture.finish("complete")
    path = capture.streams["stdout"].path
    if change == "missing":
        path.unlink()
    elif change == "corrupt":
        path.write_bytes(b"modified")
    else:
        path.unlink()
        other = tmp_path / "private.txt"
        other.write_text("PRIVATE")
        path.symlink_to(other)
    result = reader(store, "owner", outputs=manager)("control", "check", stream="stdout")
    assert "Error:" in result and "availability=" in result and "PRIVATE" not in result


def test_authorization_precedes_file_access_and_legacy_is_unchanged(archive, monkeypatch):
    manager, store = archive
    capture = begin(manager)
    capture.feed("stdout", b"private")
    capture.finish("complete")
    store.append_fact("owner", "tool.completed", {
        "call_id": "old", "content": "stdout:\nold combined content",
    }, run_id="control")
    read = reader(store, "owner", outputs=manager)
    assert read("control", "old", offset=8, limit=3).endswith("old")
    assert "no separate" in read("control", "old", stream="stdout")

    def forbidden(*_a, **_k):
        raise AssertionError("unauthorized file access")

    monkeypatch.setattr(manager, "page", forbidden)
    assert "not found" in reader(store, "other", outputs=manager)(
        "control", "check", stream="stdout",
    )
    grant = {"reader_run_id": "writer", "run_id": "control", "session_id": "owner",
             "call_ids": ["check"], "delivery_id": "delivery", "round": 1,
             "patch_sha256": "a" * 64}
    assert "not found" in reader(
        store, "other", outputs=manager, acceptance=grant, current_run_id="later",
    )("control", "check", stream="stdout")


def test_writer_stream_grant_is_bound_to_exact_run_and_call(archive):
    manager, store = archive
    capture = begin(manager)
    capture.feed("stdout", b"authorized evidence")
    capture.finish("complete")
    grant = {"reader_run_id": "writer", "run_id": "control", "session_id": "owner",
             "call_ids": ["check"], "delivery_id": "delivery", "round": 2,
             "patch_sha256": "a" * 64}
    read = reader(store, "other", outputs=manager, acceptance=grant, current_run_id="writer")
    result = read("control", "check", stream="stdout")
    assert "round=2" in result and result.endswith("authorized evidence")
    assert "not found" in read("control", "different", stream="stdout")
    assert "not found" in read("different", "check", stream="stdout")
