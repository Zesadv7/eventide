"""Integration boundaries for human side investigations."""
import asyncio

import pytest

from eventide.models import ModelResponse, RunRequest
from eventide.providers import ScriptedProvider
from eventide.side_questions import (
    SIDE_QUESTION_EVENT,
    return_side_question,
    source_context,
)
from tests.test_readonly_workers import worker_host
from tests.test_side_questions import _wait_for
from tests.test_worktree_execution import make_host


@pytest.mark.parametrize("target", ["worktree", "snapshot"])
async def test_side_reads_actual_execution_copy(isolated_workspace, target):
    provider = ScriptedProvider([
        {"text": "main done"},
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "code.py"}}]},
        {"text": "side done"},
    ])
    host = make_host(isolated_workspace, provider)
    try:
        main = await host.run(RunRequest(
            "inspect", mode="plan" if target == "snapshot" else "auto", execution_target=target,
        ))
        binding = host._execution_binding(main.run_id)
        root, _ = host._execution_paths(binding)
        expected = "original"
        if target == "worktree":
            (root / "code.py").write_text("only in working copy\n")
            expected = "only in working copy"
        (host.settings.workdir / "code.py").write_text("new logical source\n")
        record = await host.create_side_question(main.run_id, "read code.py")
        await _wait_for(lambda: host.store.run_is_terminal(record["side_run_id"]))
        side = host.store.get_run(record["side_run_id"])
        assert side["status"] == "completed", side
        assert expected in side["tools"]["read"]["content"]
        assert "new logical source" not in side["tools"]["read"]["content"]
        assert record["source_execution"]["kind"] == target
        assert record["files_captured_at"]
    finally:
        await host.close()


async def test_saved_errors_and_continued_goal_respect_event_cut(isolated_workspace):
    host = worker_host(isolated_workspace, ScriptedProvider([{"text": "first answer"}]))
    try:
        main = await host.run(RunRequest("original goal"))
        sid = host.store.get_run_identity(main.run_id)["session_id"]
        host.store.admit_run("continued", sid, [
            ("run.admitted", host._admitted_intent(main.run_id), "system", "runtime"),
            ("tool.completed", {
                "call_id": "failure", "name": "read_file", "content": "FileNotFound: parser.py",
                "is_error": True,
            }, "tool", "runtime"),
        ], continuation_of=main.run_id)
        cut = host.store.run_snapshot("continued")["high_water"]
        host.store.append_event("continued", "tool.completed", {
            "call_id": "later", "name": "read_file", "content": "NOT AT CUT", "is_error": False,
        })
        evidence = source_context(host.store, "continued", cut)
        assert evidence["user_prompt"] == "original goal"
        assert evidence["tool_results"][-1]["content"] == "FileNotFound: parser.py"
        assert evidence["tool_results"][-1]["is_error"] is True
        assert "NOT AT CUT" not in str(evidence)
    finally:
        await host.close()


async def test_admission_failure_cleans_snapshot_and_finishes_child(
    isolated_workspace, monkeypatch,
):
    host = worker_host(isolated_workspace, ScriptedProvider([{"text": "main done"}]))
    try:
        main = await host.run(RunRequest("goal"))
        original = host.store.append_fact

        def fail_source_link(session_id, event_type, payload, *args, **kwargs):
            if event_type == SIDE_QUESTION_EVENT:
                raise OSError("receipt disk failure")
            return original(session_id, event_type, payload, *args, **kwargs)

        monkeypatch.setattr(host.store, "append_fact", fail_source_link)
        with pytest.raises(OSError, match="receipt disk failure"):
            await host.create_side_question(main.run_id, "inspect")
        assert not list((host.state_dir / "workers").glob("eventide-readonly-*"))
        facts = host.store.facts_by_type((SIDE_QUESTION_EVENT,))
        assert len(facts) == 1
        assert host.store.get_run(facts[0]["run_id"])["status"] == "failed"
        assert not host._run_tasks
    finally:
        await host.close()


async def test_side_capacity_is_bounded_and_stop_before_start_cleans(isolated_workspace):
    class BlockSide:
        async def complete(self, request):
            if "side-question investigator" in request.messages[0]["content"]:
                await asyncio.Event().wait()
            return ModelResponse(text="main done")

    host = worker_host(isolated_workspace, BlockSide())
    try:
        main = await host.run(RunRequest("goal"))
        results = await asyncio.gather(*[
            host.create_side_question(main.run_id, f"question {i}") for i in range(5)
        ], return_exceptions=True)
        assert sum(isinstance(r, dict) for r in results) == 4
        assert "four side questions" in str(results[-1])
        for item in results[:4]:
            await host.cancel_run(item["side_run_id"])
        await _wait_for(lambda: not host._run_tasks)
        instant = await host.create_side_question(main.run_id, "cancel immediately")
        task = host._run_tasks[instant["side_run_id"]]
        task.cancel()  # no event-loop yield: coroutine has not started
        await asyncio.gather(task, return_exceptions=True)
        await _wait_for(lambda: not host._run_tasks)
        assert not list((host.state_dir / "workers").glob("eventide-readonly-*"))
    finally:
        await host.close()


async def test_failed_side_output_cannot_be_returned(isolated_workspace):
    class FailSide:
        async def complete(self, request):
            if "side-question investigator" in request.messages[0]["content"]:
                raise ValueError("side failed")
            return ModelResponse(text="main done")

    host = worker_host(isolated_workspace, FailSide())
    try:
        main = await host.run(RunRequest("goal"))
        record = await host.create_side_question(main.run_id, "inspect")
        await _wait_for(lambda: host.store.run_is_terminal(record["side_run_id"]))
        assert host.store.get_run(record["side_run_id"])["status"] == "failed"
        with pytest.raises(ValueError, match="no completed answer"):
            return_side_question(host, record["side_question_id"])
    finally:
        await host.close()
