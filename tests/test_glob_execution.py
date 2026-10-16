"""Recursive discovery has the same scope in source and frozen Plan copies."""

from dataclasses import replace

import pytest

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.tools.filesystem import run_glob
from tests.test_host import make_repo
from tests.test_runtime import settings_for


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("**/root.py", {"root.py"}),
        ("**/*.py", {"root.py", "pkg/child.py", "pkg/deep/last.py"}),
        ("**/last.py", {"pkg/deep/last.py"}),
        ("**/pkg/", {"pkg/"}),
        ("**/*", {"root.py", "pkg", "pkg/child.py", "pkg/deep", "pkg/deep/last.py"}),
        ("**/.hidden.py", {".hidden.py"}),
        ("**/.private.py", {"pkg/.private.py"}),
        ("**/.hidden_dir/*.py", {".hidden_dir/secret.py"}),
    ],
)
def test_glob_recurses_from_zero_directories_and_preserves_explicit_hidden_paths(
    isolated_workspace, pattern, expected,
):
    for name in (
        "root.py", "pkg/child.py", "pkg/deep/last.py", ".hidden.py",
        "pkg/.private.py", ".hidden_dir/secret.py",
    ):
        path = isolated_workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    assert set(run_glob(pattern, cwd=isolated_workspace).splitlines()) == expected


async def test_plan_recursive_glob_discovers_only_frozen_snapshot_paths(isolated_workspace):
    source = make_repo(isolated_workspace / "source")
    for name in ("root.py", "pkg/child.py", "pkg/deep/last.py", ".hidden.py"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n")
    (source / ".env").write_text("EXCLUDED=private\n")
    (source / "binary.py").write_bytes(b"binary\0data")
    (source / "linked.py").symlink_to(source / "root.py")
    provider = ScriptedProvider([
        {"tool_calls": [
            {"id": "files", "name": "glob", "arguments": {"pattern": "**/*.py"}},
            {"id": "dirs", "name": "glob", "arguments": {"pattern": "**/pkg/"}},
            {"id": "hidden", "name": "glob", "arguments": {"pattern": "**/.*"}},
        ]},
        {"text": "done"},
    ])
    host = RuntimeHost(
        replace(settings_for(source), state_dir=isolated_workspace / "state"), provider,
    )
    try:
        run_id = await host.admit_run(RunRequest(
            "Find source files", mode="plan", execution_target="snapshot",
        ))
        (source / "late.py").write_text("not part of the frozen source\n")
        result = await host.start_admitted_run(run_id)
        assert result.status == "completed"
        results = {
            event["payload"]["call_id"]: event["payload"]
            for event in host.store.run_events(run_id)
            if event["type"] == "tool.completed"
        }
        assert not any(item["is_error"] for item in results.values())
        files = set(results["files"]["content"].splitlines())
        assert files == {"root.py", "pkg/child.py", "pkg/deep/last.py"}
        assert results["dirs"]["content"] == "pkg/"
        assert results["hidden"]["content"] == ".hidden.py"
        assert files <= host._execution_binding(run_id)["manifest"].keys()
    finally:
        await host.close()
