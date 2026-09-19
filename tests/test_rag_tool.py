"""Runtime integration for the dynamically advertised retrieval tool."""

from dataclasses import replace
from pathlib import Path

from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.rag.embedding import HASH_ALGORITHM_VERSION, EmbeddingProfile, HashEmbedder
from eventide.rag.index import IndexStore
from eventide.rag.tool import handler, index_available
from tests.test_host import make_repo
from tests.test_runtime import settings_for


async def test_tool_appears_on_next_run_and_is_audited_read_only(
    isolated_workspace: Path,
) -> None:
    repo = make_repo(isolated_workspace / "repo")
    with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(".eventide/\n")
    (repo / "knowledge.txt").write_text(
        "verify_token checks the current authentication token", encoding="utf-8"
    )
    provider = ScriptedProvider(
        [
            {"text": "index absent"},
            {
                "tool_calls": [
                    {
                        "id": "search",
                        "name": "search_knowledge",
                        "arguments": {"query": "verify_token", "k": 3},
                    }
                ]
            },
            {"text": "retrieval complete"},
        ]
    )
    settings = replace(settings_for(repo), state_dir=isolated_workspace / "state")
    host = RuntimeHost(settings, provider)
    try:
        first = await host.run(RunRequest("before indexing"))
        assert first.status == "completed"
        assert "search_knowledge" not in {tool["name"] for tool in provider.requests[0].tools}

        IndexStore(repo).build()
        second = await host.run(RunRequest("search now", mode="plan"))

        assert second.status == "completed"
        visible = {tool["name"] for tool in provider.requests[1].tools}
        assert "search_knowledge" in visible
        assert not visible & {"bash", "write_file", "edit_file"}
        events = host.store.run_events(second.run_id)
        prepared = next(event for event in events if event["type"] == "tool.prepared")
        completed = next(event for event in events if event["type"] == "tool.completed")
        assert prepared["payload"]["name"] == "search_knowledge"
        assert prepared["payload"]["readonly"] is True
        assert completed["payload"]["is_error"] is False
        assert "knowledge.txt:1-1" in completed["payload"]["content"]
        assert len([event for event in events if event["type"] == "workspace.checkpoint"]) == 1
    finally:
        await host.close()


async def test_index_is_isolated_per_workspace(isolated_workspace: Path) -> None:
    indexed = make_repo(isolated_workspace / "indexed")
    plain = make_repo(isolated_workspace / "plain")
    with (indexed / ".git" / "info" / "exclude").open("a", encoding="utf-8") as handle:
        handle.write(".eventide/\n")
    (indexed / "doc.txt").write_text("indexed text", encoding="utf-8")
    IndexStore(indexed).build()
    provider = ScriptedProvider([{"text": "one"}, {"text": "two"}])
    settings = replace(settings_for(indexed), state_dir=isolated_workspace / "state")
    host = RuntimeHost(settings, provider)
    try:
        other = host.resolve_or_register_workspace(plain)
        await host.run(RunRequest("indexed"))
        session = host.create_session(workspace_id=other.workspace_id)
        await host.run(RunRequest("plain", session))
        assert "search_knowledge" in {tool["name"] for tool in provider.requests[0].tools}
        assert "search_knowledge" not in {tool["name"] for tool in provider.requests[1].tools}
    finally:
        await host.close()


def test_tool_defends_against_a_missing_or_invalid_index(isolated_workspace: Path) -> None:
    repo = make_repo(isolated_workspace / "repo")
    profile = EmbeddingProfile(
        "hash", "hash-v1", "local", HASH_ALGORITHM_VERSION, 256
    )
    assert index_available(repo, profile) is False
    result = handler(repo, HashEmbedder(), profile)("query")
    assert result == "Error: no knowledge index; run 'eventide rag build'"
