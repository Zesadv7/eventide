"""Offline protocol tests for the isolated file worker (in process and real subprocess)."""

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from eventide.native_sandbox import worker as worker_module
from eventide.native_sandbox.worker import (
    GRANT_ENV_VAR,
    PROTOCOL_VERSION,
    WORKER_BOOTSTRAP,
    FileWorker,
    WorkerGrant,
    run_worker,
)


def make_grant(tmp_path: Path, *, profile: str = "workspace-write") -> WorkerGrant:
    return WorkerGrant(
        profile=profile,
        workspace_root=str(tmp_path / "ws"),
        tmp_root=str(tmp_path / "scratch"),
        read_only_roots=(),
        protected_roots=(str(tmp_path / "state"),),
    )


def make_worker(tmp_path: Path, *, profile: str = "workspace-write") -> FileWorker:
    (tmp_path / "ws").mkdir()
    (tmp_path / "scratch").mkdir()
    return FileWorker(make_grant(tmp_path, profile=profile), io.BytesIO(), io.BytesIO())


def call(worker: FileWorker, op: str, identifier: str = "1", **params: object) -> dict:
    return worker.handle({"v": PROTOCOL_VERSION, "id": identifier, "op": op, "params": params})


def test_grant_round_trip_and_rejections(tmp_path: Path) -> None:
    grant = make_grant(tmp_path)
    parsed = WorkerGrant.parse(grant.encode_env())
    assert parsed == grant
    for raw in ("not-base64!!", "", "YmFk"):
        with pytest.raises(ValueError):
            WorkerGrant.parse(raw)
    payload = json.loads(base64.b64decode(grant.encode_env()))
    payload["v"] = 99
    with pytest.raises(ValueError):
        WorkerGrant.parse(base64.b64encode(json.dumps(payload).encode()).decode())
    payload["v"] = PROTOCOL_VERSION
    payload["workspace_root"] = "relative/path"
    with pytest.raises(ValueError):
        WorkerGrant.parse(base64.b64encode(json.dumps(payload).encode()).decode())


def test_ping_reports_protocol_and_profile(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    response = call(worker, "ping")
    assert response["ok"] is True
    assert response["result"] == {"protocol": PROTOCOL_VERSION, "profile": "workspace-write"}


def test_write_and_read_file_round_trip(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    payload = "héllo wörld".encode() + bytes(range(256))
    response = call(
        worker,
        "write_file",
        path=str(tmp_path / "ws" / "notes.bin"),
        content_b64=base64.b64encode(payload).decode("ascii"),
    )
    assert response["ok"] is True and response["result"] == {"size": len(payload)}
    back = call(worker, "read_file", path=str(tmp_path / "ws" / "notes.bin"))
    assert back["ok"] is True
    assert base64.b64decode(back["result"]["content_b64"]) == payload


def test_read_only_profile_refuses_writes(tmp_path: Path) -> None:
    worker = make_worker(tmp_path, profile="read-only")
    response = call(
        worker,
        "write_file",
        path=str(tmp_path / "ws" / "x.txt"),
        content_b64="",
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "profile_denied"
    removal = call(worker, "remove", path=str(tmp_path / "ws" / "x.txt"))
    assert removal["error"]["code"] == "profile_denied"


def test_paths_outside_roots_and_protected_denied(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    outside = call(worker, "read_file", path=str(tmp_path / "outside.txt"))
    assert outside["error"]["code"] == "path_denied"
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "secret.txt").write_text("token")
    denied = call(worker, "read_file", path=str(tmp_path / "state" / "secret.txt"))
    assert denied["error"]["code"] == "path_denied"
    inside = call(worker, "read_file", path=str(tmp_path / "ws" / "missing.txt"))
    assert inside["error"]["code"] == "io_error"


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    (tmp_path / "secret.txt").write_text("outside")
    os.symlink(tmp_path / "secret.txt", tmp_path / "ws" / "link.txt")
    response = call(worker, "read_file", path=str(tmp_path / "ws" / "link.txt"))
    assert response["error"]["code"] == "path_denied"


def test_paged_read_handles(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    data = bytes(range(256)) * 8
    (tmp_path / "ws" / "page.bin").write_bytes(data)
    opened = call(worker, "open_read", path=str(tmp_path / "ws" / "page.bin"))
    assert opened["ok"] is True
    token = opened["result"]["handle"]
    assert opened["result"]["size"] == len(data)
    first = call(worker, "read_page", handle=token, offset=0, length=1000)
    second = call(worker, "read_page", handle=token, offset=1000, length=1000)
    assert base64.b64decode(first["result"]["content_b64"]) == data[:1000]
    assert first["result"]["eof"] is False
    assert base64.b64decode(second["result"]["content_b64"]) == data[1000:2000]
    bad_pages = (
        {"offset": -1, "length": 1},
        {"offset": 0, "length": 0},
        {"offset": 0, "length": 999999},
    )
    for bad in bad_pages:
        bad_response = call(worker, "read_page", handle=token, **bad)
        assert bad_response["error"]["code"] == "invalid_request"
    assert call(worker, "close_handle", handle=token)["ok"] is True
    stale = call(worker, "read_page", handle=token, offset=0, length=1)
    assert stale["error"]["code"] == "not_found"
    unknown = call(worker, "read_page", handle="nope", offset=0, length=1)
    assert unknown["error"]["code"] == "not_found"


def test_list_dir_and_remove(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    (tmp_path / "ws" / "a.txt").write_text("a")
    (tmp_path / "ws" / "sub").mkdir()
    listed = call(worker, "list_dir", path=str(tmp_path / "ws"))
    assert listed["ok"] is True
    names = {entry["name"]: entry["type"] for entry in listed["result"]["entries"]}
    assert names == {"a.txt": "file", "sub": "dir"}
    (tmp_path / "ws" / "sub" / "keep.txt").write_text("keep")
    blocked = call(worker, "remove", path=str(tmp_path / "ws" / "sub"))
    assert blocked["error"]["code"] == "io_error"
    assert call(worker, "remove", path=str(tmp_path / "ws" / "a.txt"))["ok"] is True
    os.unlink(tmp_path / "ws" / "sub" / "keep.txt")
    assert call(worker, "remove", path=str(tmp_path / "ws" / "sub"))["ok"] is True
    missing = call(worker, "remove", path=str(tmp_path / "ws" / "a.txt"))
    assert missing["error"]["code"] == "io_error"


def test_write_file_overwrite_semantics(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    target = str(tmp_path / "ws" / "doc.txt")
    first = call(worker, "write_file", path=target, content_b64="YQ==")  # "a"
    assert first["ok"] is True
    clash = call(worker, "write_file", path=target, content_b64="Yg==")
    assert clash["error"]["code"] == "io_error"
    replace = call(worker, "write_file", path=target, content_b64="Yg==", overwrite=True)
    assert replace["ok"] is True
    content = call(worker, "read_file", path=target)
    assert base64.b64decode(content["result"]["content_b64"]) == b"b"


def test_size_and_entry_limits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    monkeypatch.setattr(worker_module, "MAX_READ_FILE_BYTES", 4)
    (tmp_path / "ws" / "big.bin").write_bytes(b"x" * 10)
    response = call(worker, "read_file", path=str(tmp_path / "ws" / "big.bin"))
    assert response["error"]["code"] == "too_large"
    monkeypatch.setattr(worker_module, "MAX_LIST_ENTRIES", 1)
    (tmp_path / "ws" / "one.txt").write_text("1")
    (tmp_path / "ws" / "two.txt").write_text("2")
    listed = call(worker, "list_dir", path=str(tmp_path / "ws"))
    assert listed["error"]["code"] == "too_large"


def test_envelope_validation(tmp_path: Path) -> None:
    worker = make_worker(tmp_path)
    assert worker.handle("not-a-dict")["error"]["code"] == "protocol_error"
    assert worker.handle({"v": 2, "id": "1", "op": "ping"})["error"]["code"] == "protocol_error"
    assert worker.handle({"v": PROTOCOL_VERSION, "op": "ping"})["error"]["code"] == "protocol_error"
    empty_op = worker.handle({"v": PROTOCOL_VERSION, "id": "1", "op": ""})
    assert empty_op["error"]["code"] == "protocol_error"
    assert worker.handle({"v": PROTOCOL_VERSION, "id": "1", "op": "ping", "params": []})["error"][
        "code"
    ] == "protocol_error"
    response = worker.handle({"v": PROTOCOL_VERSION, "id": "1", "op": "teleport"})
    assert response["error"]["code"] == "unknown_op"
    ok = call(worker, "ping", identifier="abc")
    assert ok["id"] == "abc"


def test_serve_loop_and_shutdown(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "scratch").mkdir()
    stdin = io.BytesIO(
        json.dumps({"v": PROTOCOL_VERSION, "id": "a", "op": "ping"}).encode()
        + b"\n"
        + b"not json\n"
        + json.dumps({"v": PROTOCOL_VERSION, "id": "b", "op": "shutdown"}).encode()
        + b"\n"
        + json.dumps({"v": PROTOCOL_VERSION, "id": "c", "op": "ping"}).encode()
        + b"\n"
    )
    stdout = io.BytesIO()
    worker = FileWorker(make_grant(tmp_path), stdin, stdout)
    worker.serve()
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["id"] for line in lines] == ["a", "", "b"]
    assert [line["ok"] for line in lines] == [True, False, True]


def test_oversized_line_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(worker_module, "MAX_MESSAGE_BYTES", 64)
    stdin = io.BytesIO(
        b"x" * 80
        + b"\n"
        + json.dumps({"v": PROTOCOL_VERSION, "id": "a", "op": "shutdown"}).encode()
        + b"\n"
    )
    stdout = io.BytesIO()
    worker = FileWorker(make_grant(tmp_path), stdin, stdout)
    worker.serve()
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["id"] for line in lines] == ["", "a"]
    assert lines[0]["error"]["code"] == "too_large"


def test_run_worker_requires_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRANT_ENV_VAR, raising=False)
    assert run_worker() == 2
    monkeypatch.setenv(GRANT_ENV_VAR, "!!!not-base64!!!")
    assert run_worker() == 2


def test_real_worker_subprocess_round_trip(tmp_path: Path) -> None:
    """The real worker process, launched unsandboxed by the test harness only."""
    (tmp_path / "ws").mkdir()
    (tmp_path / "scratch").mkdir()
    env = {"TMPDIR": str(tmp_path / "scratch"), GRANT_ENV_VAR: make_grant(tmp_path).encode_env()}
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            WORKER_BOOTSTRAP,
            str(Path(worker_module.__file__).resolve().parents[2]),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    def exchange(op: str, identifier: str, **params: object) -> dict:
        assert process.stdin is not None and process.stdout is not None
        line = json.dumps({"v": PROTOCOL_VERSION, "id": identifier, "op": op, "params": params})
        process.stdin.write(line.encode() + b"\n")
        process.stdin.flush()
        raw = process.stdout.readline()
        return json.loads(raw.decode("utf-8"))

    try:
        ping = exchange("ping", "1")
        assert ping["ok"] is True and ping["result"]["protocol"] == PROTOCOL_VERSION
        write = exchange(
            "write_file", "2", path=str(tmp_path / "ws" / "via.txt"), content_b64="aGVsbG8="
        )
        assert write["ok"] is True
        read = exchange("read_file", "3", path=str(tmp_path / "ws" / "via.txt"))
        assert base64.b64decode(read["result"]["content_b64"]) == b"hello"
        exchange("shutdown", "4")
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
