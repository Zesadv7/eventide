"""Real Git lifecycle checks for managed, Workspace-scoped worktrees."""

import json
import subprocess
from pathlib import Path

import pytest

from eventide.worktrees import WorktreeService


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )
    return result.stdout.decode().strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("original", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-qm", "initial")
    return path


def service(path: Path) -> WorktreeService:
    return WorktreeService(make_repo(path / "repo"), path / "state", "ws_test")


def test_worktree_lifecycle_survives_restart_and_retains_branch(isolated_workspace):
    manager = service(isolated_workspace)
    assert not manager.state_dir.exists()
    assert manager.list() == []
    assert not manager.state_dir.exists()
    record = manager.create("feature")
    assert record.branch == "codex/ws_test/feature"
    assert record.base_commit == git(manager.root, "rev-parse", "HEAD")
    assert not Path(record.path).is_relative_to(manager.root)
    restarted = WorktreeService(manager.root, manager.state_dir, "ws_test")
    assert restarted.list() == [record]
    kept = restarted.keep("feature")
    assert kept.kept is True
    assert restarted.list() == [kept]
    assert restarted.remove("feature") == kept
    assert not Path(record.path).exists()
    assert restarted.list() == []
    assert git(manager.root, "rev-parse", f"refs/heads/{record.branch}") == record.base_commit


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "-flag", "a/b", "CON", "x."])
def test_worktree_name_validation_does_not_write(isolated_workspace, name):
    manager = service(isolated_workspace)
    with pytest.raises(ValueError, match="name"):
        manager.create(name)
    assert not manager.state_dir.exists()


@pytest.mark.parametrize("change", ["tracked", "untracked", "ignored", "commit"])
def test_removal_refuses_all_unreviewed_changes(isolated_workspace, change):
    manager = service(isolated_workspace)
    record = manager.create("review")
    checkout = Path(record.path)
    if change == "tracked":
        (checkout / "README.md").write_text("changed", encoding="utf-8")
    elif change == "untracked":
        (checkout / "new.txt").write_text("new", encoding="utf-8")
    elif change == "ignored":
        exclude = Path(record.git_common_dir) / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write("\nignored.txt\n")
        (checkout / "ignored.txt").write_text("user data", encoding="utf-8")
    else:
        (checkout / "README.md").write_text("committed", encoding="utf-8")
        git(checkout, "add", "README.md")
        git(checkout, "commit", "-qm", "work")
    manager.keep("review")
    with pytest.raises(ValueError, match="keep it for review"):
        manager.remove("review")
    assert checkout.exists()
    assert manager.list()[0].kept is True


def test_explicit_ref_and_unmanaged_checkouts(isolated_workspace):
    manager = service(isolated_workspace)
    base = git(manager.root, "rev-parse", "HEAD")
    (manager.root / "README.md").write_text("second", encoding="utf-8")
    git(manager.root, "commit", "-qam", "second")
    unmanaged = isolated_workspace / "other"
    git(manager.root, "worktree", "add", "-b", "other", str(unmanaged))
    record = manager.create("historical", base)
    assert Path(record.path, "README.md").read_text() == "original"
    assert manager.list() == [record]
    manager.remove("historical")
    assert unmanaged.exists()


@pytest.mark.parametrize(
    "field", ["path", "repository", "workspace_id", "git_common_dir", "branch"]
)
def test_foreign_or_tampered_records_are_refused(isolated_workspace, field):
    manager = service(isolated_workspace)
    record = manager.create("owned")
    record_file = manager.records / "owned.json"
    data = record.to_dict()
    data[field] = "foreign"
    record_file.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="does not belong"):
        manager.remove("owned")
    assert Path(record.path).exists()


def test_symlinked_storage_and_checkout_are_refused(isolated_workspace):
    manager = service(isolated_workspace)
    target = isolated_workspace / "outside"
    target.mkdir()
    manager.directory.mkdir(parents=True)
    try:
        manager.checkouts.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symbolic links unavailable on this platform")
    with pytest.raises(ValueError, match="symbolic links"):
        manager.create("escape")
    assert list(target.iterdir()) == []
    manager.checkouts.unlink()
    record = manager.create("owned")
    checkout = Path(record.path)
    moved = checkout.with_name("moved")
    checkout.rename(moved)
    checkout.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        manager.remove("owned")
    assert moved.exists()


def test_non_git_nested_source_and_in_source_state_are_refused(isolated_workspace):
    plain = isolated_workspace / "plain"
    plain.mkdir()
    manager = WorktreeService(plain, isolated_workspace / "state", "ws_test")
    with pytest.raises(ValueError):
        manager.create("feature")
    assert not manager.state_dir.exists()
    repo = make_repo(isolated_workspace / "repo")
    with pytest.raises(ValueError, match="outside"):
        WorktreeService(repo, repo / ".state", "ws_test")
    nested = repo / "nested"
    nested.mkdir()
    manager = WorktreeService(nested, isolated_workspace / "state", "ws_test")
    with pytest.raises(ValueError, match="exact Git Workspace root"):
        manager.create("feature")


def test_submodule_and_invalid_starting_ref_are_refused(isolated_workspace):
    manager = service(isolated_workspace)
    with pytest.raises(ValueError, match="starting ref"):
        manager.create("feature", "--help")
    with pytest.raises(ValueError):
        manager.create("feature", "missing")
    head = git(manager.root, "rev-parse", "HEAD")
    git(manager.root, "update-index", "--add", "--cacheinfo", f"160000,{head},module")
    git(manager.root, "commit", "-qm", "gitlink")
    with pytest.raises(ValueError):
        manager.create("feature")
    assert not manager.state_dir.exists()


def test_registered_branch_change_and_duplicate_are_refused(isolated_workspace):
    manager = service(isolated_workspace)
    record = manager.create("owned")
    with pytest.raises(ValueError, match="already exists"):
        manager.create("owned")
    git(Path(record.path), "checkout", "-b", "unexpected")
    with pytest.raises(ValueError, match="registered branch changed"):
        manager.remove("owned")
    assert Path(record.path).exists()


def test_git_timeout_is_reported_without_writing(isolated_workspace, monkeypatch):
    manager = service(isolated_workspace)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 0.1)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ValueError, match="operation failed"):
        manager.create("feature")
    assert not manager.state_dir.exists()


def test_actual_foreign_git_directory_is_refused(isolated_workspace):
    manager = service(isolated_workspace)
    record = manager.create("owned")
    foreign = make_repo(isolated_workspace / "foreign")
    git_file = Path(record.path) / ".git"
    git_file.write_text(f"gitdir: {foreign / '.git'}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="another Git repository"):
        manager.remove("owned")
    assert Path(record.path).exists()
    assert foreign.exists()


def test_corrupt_record_and_failed_creation_preserve_git(isolated_workspace):
    manager = service(isolated_workspace)
    record = manager.create("owned")
    record_file = manager.records / "owned.json"
    record_file.write_text("invalid JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read managed"):
        manager.list()
    assert Path(record.path).exists()
    record_file.write_text(json.dumps(record.to_dict()), encoding="utf-8")
    manager.remove("owned")
    # The retained branch cannot be silently reused or overwritten.
    with pytest.raises(ValueError):
        manager.create("owned")
    assert not record_file.exists()
    assert git(manager.root, "rev-parse", record.branch) == record.base_commit
