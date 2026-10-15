"""Source copies contain current scoped code, with checked citations and bounds."""

import hashlib
import json
import subprocess
from dataclasses import FrozenInstanceError

import pytest

from eventide import workers
from eventide.workers import ReadonlyProfile, SourceSnapshot, validate_report, validate_tasks
from tests.test_host import make_repo


def commit(repo):
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )


def test_snapshot_copies_current_scoped_source_and_can_only_cleanup_itself(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    source = repo / "package"
    source.mkdir()
    (source / "app.py").write_text("old = 1\n", encoding="utf-8")
    (repo / "outside.py").write_text("outside = 1\n", encoding="utf-8")
    commit(repo)
    body = "changed = 2\n# 中文\n"
    (source / "app.py").write_text(body, encoding="utf-8")
    (source / "new.md").write_text("new source\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
    (source / "mcp.json").write_text('{"env": "private"}', encoding="utf-8")
    (source / "private.pem").write_text("private key", encoding="utf-8")
    (source / "binary.dat").write_bytes(b"\0not source")
    (source / "invalid.dat").write_bytes(b"\xff\xfe")
    (source / "large.txt").write_bytes(b"x" * (workers.MAX_SOURCE_BYTES + 1))
    state = isolated_workspace / "state"
    snapshot = SourceSnapshot.create(repo, source, state)
    try:
        assert set(snapshot.manifest) == {"app.py", "new.md"}
        assert snapshot.manifest["app.py"] == {
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "lines": 2,
        }
        assert snapshot.skipped == 6
        assert (snapshot.root / "app.py").read_text(encoding="utf-8") == body
        assert not (snapshot.root / ".git").exists()
        assert snapshot.checkpoint["head"]
        assert (source / "app.py").read_text(encoding="utf-8") == body
        sibling = snapshot.root.parent / "keep-me.txt"
        sibling.write_text("keep", encoding="utf-8")
        original = snapshot.root
        with pytest.raises(FrozenInstanceError):
            snapshot.root = repo
        snapshot.cleanup()
        snapshot.cleanup()
        assert not original.exists() and repo.is_dir() and sibling.is_file()
    finally:
        snapshot.cleanup()


def test_snapshot_excludes_links_and_runtime_state_inside_source(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "app.py").write_text("ok\n", encoding="utf-8")
    state = repo / "runtime-state"
    state.mkdir()
    (state / "private.json").write_text("runtime secret", encoding="utf-8")
    outside = isolated_workspace / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    try:
        (repo / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    snapshot = SourceSnapshot.create(repo, repo, state)
    try:
        assert set(snapshot.manifest) == {"app.py"}
        assert snapshot.skipped == 2
        assert not snapshot.root.is_relative_to(repo.resolve())
    finally:
        snapshot.cleanup()


def test_snapshot_rejects_drift_and_removes_partial_copy(isolated_workspace, monkeypatch):
    repo = make_repo(isolated_workspace / "repo")
    source = repo / "app.py"
    source.write_text("before\n", encoding="utf-8")
    original = workers.workspace_checkpoint
    calls = 0

    def changing_checkpoint(root, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_text("after\n", encoding="utf-8")
        return original(root, **kwargs)

    monkeypatch.setattr(workers, "workspace_checkpoint", changing_checkpoint)
    state = isolated_workspace / "state"
    with pytest.raises(ValueError, match="changed"):
        SourceSnapshot.create(repo, repo, state)
    assert list((state / "workers").iterdir()) == []


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_snapshot_rejects_limits_instead_of_returning_partial_source(
    isolated_workspace, monkeypatch, limit
):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "one.py").write_text("one\n", encoding="utf-8")
    (repo / "two.py").write_text("two\n", encoding="utf-8")
    monkeypatch.setattr(
        workers, "MAX_SNAPSHOT_FILES" if limit == "files" else "MAX_SNAPSHOT_BYTES", 1
    )
    state = isolated_workspace / "state"
    with pytest.raises(ValueError, match="limit"):
        SourceSnapshot.create(repo, repo, state)
    assert not (state / "workers").exists() or list((state / "workers").iterdir()) == []


def test_snapshot_requires_git_evidence_and_a_scoped_directory(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    other = isolated_workspace / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        SourceSnapshot.create(repo, other, isolated_workspace / "state")
    with pytest.raises(ValueError, match="Git"):
        SourceSnapshot.create(other, other, isolated_workspace / "state")
    with pytest.raises(ValueError, match="directories"):
        file = repo / "file"
        file.write_text("x", encoding="utf-8")
        SourceSnapshot.create(repo, file, isolated_workspace / "state")


@pytest.mark.parametrize(
    "tasks",
    [
        None,
        [],
        [{}],
        [{"role": "writer", "goal": "x"}],
        [{"role": "researcher", "goal": " "}],
        [{"role": "reviewer", "goal": "x" * 2001}],
        [{"role": [], "goal": "x"}],
        [{"role": "researcher", "goal": "x", "model": "unapproved"}],
        [{"role": "reviewer", "goal": "x"}] * 3,
    ],
)
def test_worker_tasks_are_bounded_and_have_fixed_roles(tasks):
    with pytest.raises(ValueError):
        validate_tasks(tasks)


def test_worker_profile_is_immutable_and_declares_checked_report_format():
    profile = ReadonlyProfile("parent", "reviewer", "snapshot")
    prompt = profile.prompt("Review app.py")
    assert "read-only reviewer" in prompt and "Return only one JSON object" in prompt
    assert "at most 16" in prompt and "not independent verification" in prompt
    assert (profile.max_steps, profile.token_limit, profile.max_tokens) == (8, 4000, 1200)
    with pytest.raises(FrozenInstanceError):
        profile.max_steps = 100
    assert validate_tasks([{"role": "researcher", "goal": "x"}]) == [
        {"role": "researcher", "goal": "x"}
    ]


def test_report_requires_real_paths_exact_lines_and_a_strict_schema(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    (repo / "app.py").write_text("one\ntwo\n", encoding="utf-8")
    snapshot = SourceSnapshot.create(repo, repo, isolated_workspace / "state")
    finding = {"path": "app.py", "start_line": 1, "end_line": 2, "message": "Observed code"}
    valid = {"summary": "Observations only", "findings": [finding]}
    try:
        assert validate_report(json.dumps(valid), snapshot) == valid
        bad = [
            "```json\n{}\n```",
            "[]",
            '{"summary":"a","summary":"b","findings":[]}',
            json.dumps({**valid, "verified": True}),
            json.dumps({**valid, "summary": "x" * 2001}),
            json.dumps({**valid, "findings": [finding] * 17}),
            "x" * 20001,
        ]
        for fields in [
            {"path": "../app.py"},
            {"path": "/app.py"},
            {"path": "./app.py"},
            {"path": "missing.py"},
            {"path": "a\\b.py"},
            {"start_line": True},
            {"start_line": 0},
            {"start_line": 3},
            {"end_line": 0},
            {"end_line": 3},
            {"message": "x" * 1001},
            {"extra": "field"},
        ]:
            bad.append(json.dumps({**valid, "findings": [{**finding, **fields}]}))
        for output in bad:
            with pytest.raises(ValueError):
                validate_report(output, snapshot)
    finally:
        snapshot.cleanup()
