"""Git worktree creation, removal, and binding to tasks."""

import json
import re
import subprocess
import time
from pathlib import Path

from nexus_agent.config import WORKDIR, WORKTREES_DIR
from nexus_agent.tasks.store import load_task, save_task
from nexus_agent.utils import decode_output

WORKTREES_DIR.mkdir(exist_ok=True)

VALID_WT_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate_worktree_name(name: str) -> str | None:
    if not name:
        return "Worktree name cannot be empty"
    if name in (".", ".."):
        return f"'{name}' is not a valid worktree name"
    if not VALID_WT_NAME.match(name):
        return (
            f"Invalid worktree name '{name}': "
            "only letters, digits, dots, underscores, dashes (1-64 chars)"
        )
    return None


def run_git(args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=WORKDIR,
            capture_output=True,
            timeout=30,
        )
        output = (decode_output(result.stdout) + decode_output(result.stderr)).strip()
        return result.returncode == 0, output[:5000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return False, "Error: git timeout"


def log_event(event_type: str, worktree_name: str, task_id: str = "") -> None:
    event = {
        "type": event_type,
        "worktree": worktree_name,
        "task_id": task_id,
        "ts": time.time(),
    }
    events_file = WORKTREES_DIR / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def create_worktree(name: str, task_id: str = "") -> str:
    err = validate_worktree_name(name)
    if err:
        return f"Error: {err}"
    if task_id:
        try:
            load_task(task_id)
        except FileNotFoundError:
            return f"Error: task {task_id} not found"
    path = WORKTREES_DIR / name
    if path.exists():
        return f"Worktree '{name}' already exists at {path}"
    base_ok, base_commit = run_git(["rev-parse", "HEAD"])
    if not base_ok:
        return f"Git error: {base_commit}"
    ok, result = run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Git error: {result}"
    if task_id:
        bind_task_to_worktree(task_id, name)
    (WORKTREES_DIR / f"{name}.meta.json").write_text(
        json.dumps({"base_commit": base_commit.strip(), "branch": f"wt/{name}"}),
        encoding="utf-8",
    )
    log_event("create", name, task_id)
    return f"Worktree '{name}' created at {path}"


def bind_task_to_worktree(task_id: str, worktree_name: str) -> None:
    task = load_task(task_id)
    task.worktree = worktree_name
    save_task(task)


def _count_worktree_changes(path: Path, base_commit: str) -> tuple[int, int]:
    try:
        r1 = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            timeout=10,
        )
        files = len(
            [line for line in decode_output(r1.stdout).strip().splitlines() if line.strip()]
        )
        r2 = subprocess.run(
            ["git", "rev-list", "--count", f"{base_commit}..HEAD"],
            cwd=path,
            capture_output=True,
            timeout=10,
        )
        if r1.returncode != 0 or r2.returncode != 0:
            return -1, -1
        commits = int(decode_output(r2.stdout).strip() or "0")
        return files, commits
    except Exception:
        return -1, -1


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    path = WORKTREES_DIR / name
    if not path.exists():
        return f"Worktree '{name}' not found"
    if not discard_changes:
        metadata_path = WORKTREES_DIR / f"{name}.meta.json"
        if not metadata_path.exists():
            return "Cannot verify worktree base. Use discard_changes=true to force."
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        files, commits = _count_worktree_changes(path, metadata["base_commit"])
        if files < 0:
            return "Cannot verify status. Use discard_changes=true to force."
        if files > 0 or commits > 0:
            return (
                f"Worktree '{name}' has {files} file(s), {commits} commit(s). "
                "Use discard_changes=true or keep_worktree."
            )
    ok1, _ = run_git(["worktree", "remove", str(path), "--force"])
    if not ok1:
        return f"Failed to remove worktree '{name}'"
    run_git(["branch", "-D", f"wt/{name}"])
    metadata_path = WORKTREES_DIR / f"{name}.meta.json"
    if metadata_path.exists():
        metadata_path.unlink()
    log_event("remove", name)
    return f"Worktree '{name}' removed"


def keep_worktree(name: str) -> str:
    err = validate_worktree_name(name)
    if err:
        return err
    log_event("keep", name)
    return f"Worktree '{name}' kept for review (branch: wt/{name})"
