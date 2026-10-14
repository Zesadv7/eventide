"""Real Git delivery of bounded changes without rewriting the user's index."""

import hashlib
import subprocess

import pytest

from eventide import delivery
from eventide.delivery import apply_delivery, build_patch, capture_delivery, verify_source_unchanged
from tests.test_host import make_repo


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout


def commit(root):
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )


@pytest.fixture
def checkouts(isolated_workspace):
    source = make_repo(isolated_workspace / "source").resolve()
    (source / "app.py").write_text("old = 1\n", encoding="utf-8")
    (source / "remove.txt").write_text("remove me\n", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "test_app.py").write_text("assert True\n", encoding="utf-8")
    (source / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    commit(source)
    checkout = isolated_workspace / "checkout"
    git(source, "worktree", "add", "-b", "codex/delivery", str(checkout))
    return source, checkout.resolve()


def test_patch_delivers_new_deleted_renamed_binary_and_unstaged_files(checkouts):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("new = 2\n", encoding="utf-8")
    (checkout / "remove.txt").rename(checkout / "renamed space.txt")
    binary = bytes(range(256)) * 8
    (checkout / "data.bin").write_bytes(binary)
    weird = 'new "quoted" 中文\nfile.txt'
    try:
        (checkout / weird).write_text("new file\n", encoding="utf-8")
    except OSError:
        weird = "new quoted 中文 file.txt"
        (checkout / weird).write_text("new file\n", encoding="utf-8")
    index_before = git(checkout, "ls-files", "--stage", "-z")
    patch = build_patch(source, checkout, receipt)
    assert b"GIT binary patch" in patch
    assert git(checkout, "ls-files", "--stage", "-z") == index_before
    assert not git(source, "status", "--porcelain")
    result = apply_delivery(source, patch, receipt)
    assert result["patch_sha256"] == hashlib.sha256(patch).hexdigest()
    assert (source / "app.py").read_text(encoding="utf-8") == "new = 2\n"
    assert not (source / "remove.txt").exists()
    assert (source / "renamed space.txt").read_text(encoding="utf-8") == "remove me\n"
    assert (source / "data.bin").read_bytes() == binary
    assert (source / weird).is_file()
    assert git(source, "diff", "--cached") == b""
    assert git(source, "rev-parse", "HEAD").decode().strip() == receipt["base_commit"]


@pytest.mark.parametrize("change", ["tracked", "untracked", "branch"])
def test_source_changes_are_never_overwritten(checkouts, change):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("delivery\n", encoding="utf-8")
    patch = build_patch(source, checkout, receipt)
    if change == "tracked":
        (source / "app.py").write_text("user work\n", encoding="utf-8")
    elif change == "untracked":
        (source / "user.txt").write_text("user work\n", encoding="utf-8")
    else:
        git(source, "switch", "-c", "user-branch")
    with pytest.raises(ValueError, match="changed"):
        verify_source_unchanged(source, receipt)
    with pytest.raises(ValueError):
        apply_delivery(source, patch, receipt)
    assert (source / "app.py").read_text(encoding="utf-8") != "delivery\n"


def test_capture_rejects_dirty_source_and_unrelated_checkout(checkouts, isolated_workspace):
    source, checkout = checkouts
    (source / "app.py").write_text("user work", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        capture_delivery(source, checkout)
    git(source, "restore", "app.py")
    unrelated = make_repo(isolated_workspace / "other")
    with pytest.raises(ValueError, match="same repository"):
        capture_delivery(source, unrelated)
    with pytest.raises(ValueError, match="separate"):
        capture_delivery(source, source)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_app.py",
        "nested/tests/new.py",
        "test_new.py",
        "app_test.py",
        "pyproject.toml",
        "package.json",
        "uv.lock",
        ".gitignore",
        ".env.example",
        "conftest.py",
    ],
)
def test_test_and_configuration_changes_are_refused(checkouts, path):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    target = checkout / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("pretend tests passed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Protected"):
        build_patch(source, checkout, receipt)
    assert not git(source, "status", "--porcelain")


def test_deleted_tests_and_custom_protected_patterns_are_refused(checkouts):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout, protected_patterns=["*.py"])
    (checkout / "app.py").write_text("new", encoding="utf-8")
    with pytest.raises(ValueError, match="Protected"):
        build_patch(source, checkout, receipt)
    git(checkout, "restore", "app.py")
    (checkout / "tests/test_app.py").unlink()
    with pytest.raises(ValueError, match="Protected"):
        build_patch(source, checkout, receipt)


def test_checkout_commits_are_delivered_from_base_and_branch_changes_are_refused(checkouts):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("committed = 2\n", encoding="utf-8")
    commit(checkout)
    (checkout / "new.txt").write_text("untracked\n", encoding="utf-8")
    patch = build_patch(source, checkout, receipt)
    git(checkout, "switch", "-c", "different-worker-branch")
    with pytest.raises(ValueError, match="identity"):
        apply_delivery(source, patch, receipt)
    git(checkout, "switch", receipt["checkout_branch"])
    apply_delivery(source, patch, receipt)
    assert (source / "app.py").read_text() == "committed = 2\n"


def test_stale_checkout_and_tampered_patch_cannot_be_adopted(checkouts):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("delivery\n", encoding="utf-8")
    patch = build_patch(source, checkout, receipt)
    with pytest.raises(ValueError, match="receipt"):
        apply_delivery(source, patch + b"extra", receipt)
    (checkout / "new.txt").write_text("not independently checked\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        apply_delivery(source, patch, receipt)


def test_symlinks_and_hidden_index_flags_are_refused(checkouts, isolated_workspace):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    outside = isolated_workspace / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (checkout / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError, match="symbolic"):
        build_patch(source, checkout, receipt)
    (checkout / "escape.txt").unlink()
    git(checkout, "update-index", "--assume-unchanged", "app.py")
    with pytest.raises(ValueError, match="hides"):
        build_patch(source, checkout, receipt)


@pytest.mark.parametrize("limit", ["patch", "files", "bytes"])
def test_delivery_limits_are_explicit(checkouts, monkeypatch, limit):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("new = 2\n", encoding="utf-8")
    setting = {"patch": "MAX_PATCH_BYTES", "files": "MAX_TREE_FILES", "bytes": "MAX_TREE_BYTES"}[
        limit
    ]
    monkeypatch.setattr(delivery, setting, 1)
    with pytest.raises(ValueError, match="limit"):
        build_patch(source, checkout, receipt)


def test_empty_patch_is_not_claimed_as_adoption(checkouts):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    patch = build_patch(source, checkout, receipt)
    assert patch == b""
    with pytest.raises(ValueError, match="no changes"):
        apply_delivery(source, patch, receipt)


def test_ignored_user_file_is_not_overwritten_by_forced_worker_add(checkouts):
    source, checkout = checkouts
    (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    commit(source)
    git(checkout, "reset", "--hard", git(source, "rev-parse", "HEAD").decode().strip())
    (source / "ignored.txt").write_text("user ignored artifact\n", encoding="utf-8")
    receipt = capture_delivery(source, checkout)
    (checkout / "ignored.txt").write_text("worker overwrite\n", encoding="utf-8")
    git(checkout, "add", "-f", "ignored.txt")
    patch = build_patch(source, checkout, receipt)
    with pytest.raises(ValueError, match="already exists"):
        apply_delivery(source, patch, receipt)
    assert (source / "ignored.txt").read_text() == "user ignored artifact\n"


def test_forged_protected_patch_is_rejected_again_by_adoption(checkouts):
    source, checkout = checkouts
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("legitimate delivery\n", encoding="utf-8")
    build_patch(source, checkout, receipt)
    (checkout / "tests/test_app.py").write_text("assert False\n", encoding="utf-8")
    forged = git(checkout, "diff", "--binary", receipt["base_commit"])
    git(checkout, "restore", "tests/test_app.py")
    receipt["patch_sha256"] = hashlib.sha256(forged).hexdigest()
    with pytest.raises(ValueError, match="checkout|protected"):
        apply_delivery(source, forged, receipt)
    assert (source / "tests/test_app.py").read_text() == "assert True\n"


def test_crlf_working_tree_preserves_unchanged_protected_files(isolated_workspace):
    source = make_repo(isolated_workspace / "source").resolve()
    git(source, "config", "core.autocrlf", "true")
    (source / "app.py").write_bytes(b"old = 1\r\n")
    (source / "tests").mkdir()
    (source / "tests/test_app.py").write_bytes(b"assert True\r\n")
    commit(source)
    checkout = isolated_workspace / "checkout"
    git(source, "worktree", "add", "-b", "codex/delivery", str(checkout))
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_bytes(b"new = 2\r\n")
    patch = build_patch(source, checkout, receipt)
    apply_delivery(source, patch, receipt)
    assert (source / "app.py").read_bytes() == b"new = 2\r\n"
    assert (source / "tests/test_app.py").read_bytes() == b"assert True\r\n"


def test_delivery_ignores_unrelated_git_environment(checkouts, isolated_workspace, monkeypatch):
    source, checkout = checkouts
    other = make_repo(isolated_workspace / "other").resolve()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git/index"))
    receipt = capture_delivery(source, checkout)
    (checkout / "app.py").write_text("new = 2\n", encoding="utf-8")
    patch = build_patch(source, checkout, receipt)
    apply_delivery(source, patch, receipt)
    assert (source / "app.py").read_text() == "new = 2\n"
    assert not (other / "app.py").exists()
