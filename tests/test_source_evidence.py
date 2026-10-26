from eventide.host import RuntimeHost
from eventide.models import RunRequest, ToolOutput
from eventide.providers import ScriptedProvider
from eventide.tools.filesystem import read_file_output, run_read, search_text_output
from tests.test_runtime import settings_for


def test_page_evidence_carries_exact_coordinates_and_rejects_changed_version(isolated_workspace):
    path = isolated_workspace / "long.txt"
    path.write_text("中" * 12000 + "\nnext")
    first = read_file_output("long.txt", cwd=isolated_workspace)
    evidence = first.metadata["source_evidence"]
    assert first.content == run_read("long.txt", cwd=isolated_workspace)
    assert evidence["start_line"] == evidence["end_line"] == 1
    assert evidence["start_character"] == 0
    assert evidence["end_character"] < 12000
    second = read_file_output(
        "long.txt", continuation=evidence["next_token"], cwd=isolated_workspace,
    )
    assert second.metadata["source_evidence"]["start_character"] == evidence["end_character"]
    path.write_text("changed")
    rejected = read_file_output(
        "long.txt", continuation=evidence["next_token"], cwd=isolated_workspace,
    )
    assert rejected.is_error
    assert rejected.metadata["code"] == "stale_page"
    assert rejected.metadata["source_evidence"]["status"] == "error"
    assert "start_line" not in rejected.metadata["source_evidence"]


def test_search_evidence_does_not_parse_filenames_or_content_as_headers(isolated_workspace):
    name = "strange:12-sha256=fake.txt"
    (isolated_workspace / name).write_text("context\nneedle <script>alert(1)</script>\nlast")
    result = search_text_output("needle", context_lines=1, cwd=isolated_workspace)
    assert isinstance(result, ToolOutput)
    evidence = result.metadata["source_evidence"]
    source = evidence["sources"][0]
    assert source["path"] == name
    assert [line["line"] for line in source["lines"]] == [1, 2, 3]
    assert [line["match"] for line in source["lines"]] == [False, True, False]
    assert evidence["partial"] is False


async def test_actual_host_persists_read_evidence_without_rereading_current_file(
    isolated_workspace,
):
    (isolated_workspace / "evidence.txt").write_text("original needle")
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "evidence.txt"}},
                        {"id": "search", "name": "search_text", "arguments": {"query": "needle"}}]},
        {"text": "done"},
    ])
    host = RuntimeHost(settings_for(isolated_workspace), provider)
    try:
        result = await host.run(RunRequest("read and search"))
        assert result.status == "completed"
        events = [e for e in host.store.run_events(result.run_id) if e["type"] == "tool.completed"]
        first = events[0]["payload"]["metadata"]["source_evidence"]
        second = events[1]["payload"]["metadata"]["source_evidence"]
        assert first["sha256"] == second["sources"][0]["sha256"]
        (isolated_workspace / "evidence.txt").write_text("new content")
        saved = host.store.get_run(result.run_id)["tools"]["read"]["metadata"]["source_evidence"]
        assert saved == first
    finally:
        await host.close()
