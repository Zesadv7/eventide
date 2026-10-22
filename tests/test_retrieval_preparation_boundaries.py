"""Preparation keeps actual-copy identity, observed limits and durable history intact."""

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eventide import embedding_runtime
from eventide.host import RuntimeHost
from eventide.index_control import IndexController
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.retrieval_runtime import index_status, managed_index_dir
from eventide.usage import build_usage_ledger
from tests.test_embedding_runtime import Client
from tests.test_evaluation_control import host_for
from tests.test_host import make_repo
from tests.test_runtime import settings_for


async def test_prepare_stops_before_second_batch_after_observed_run_limit(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    for number in range(65):
        (root / f"document-{number}.txt").write_text(f"searchable source document {number}")
    provider = ScriptedProvider([])
    host = RuntimeHost(replace(
        settings_for(root), state_dir=tmp_path / "state", run_token_limit=20_000,
        embedding_provider="openai_compatible", embedding_model="fixture-model",
        embedding_base_url="https://embedding.invalid/v1", embedding_api_key="fixture-secret",
    ), provider, enable_mcp=False)
    clients = []

    class BatchClient(Client):
        async def create(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(index=i, embedding=[1.0, 0.0])
                      for i in range(len(kwargs["input"]))],
                usage=SimpleNamespace(prompt_tokens=20_001, total_tokens=20_001),
            )

    def client_factory(_settings):
        client = BatchClient()
        clients.append(client)
        return client

    monkeypatch.setattr(embedding_runtime, "_client", client_factory)
    try:
        result = await host.run(RunRequest("inspect current code", knowledge_index="prepare"))
        requests = [request for client in clients for request in client.requests]
        assert len(requests) == 1 and len(requests[0]["input"]) == 64
        assert all(client.closed for client in clients)
        assert result.status == "interrupted" and not provider.requests
        state = host.store.get_run(result.run_id)
        assert state["retrieval"]["status"] == "failed"
        assert state["retrieval"]["reason"] == "token_budget"
        ledger = build_usage_ledger(host.store.usage_events(run_id=result.run_id))
        assert ledger["attempt_count"] == 1
        assert ledger["reported_totals"] == {"input_tokens": 20_001}
        assert ledger["unknown_attempts"] == 0
    finally:
        await host.close()


async def test_registered_root_replaced_with_other_repository_link_is_rejected(tmp_path):
    host = host_for(tmp_path)
    workspace = host.default_workspace
    root = Path(workspace.path)
    (root / "document.txt").write_text("original project source")
    other = make_repo(tmp_path / "other")
    (other / "document.txt").write_text("unrelated project source")
    controller = IndexController(host)
    try:
        await controller.admit(workspace.project_id, "initial-index",
                               config_hash=controller.configuration()["config_hash"])
        await controller.start("initial-index")
        artifact = managed_index_dir(host, workspace) / "index.sqlite"
        before = artifact.read_bytes()
        saved = tmp_path / "saved-source"
        root.rename(saved)
        root.symlink_to(other, target_is_directory=True)
        try:
            status = index_status(host, workspace)
            assert status["status"] == "unavailable"
            with pytest.raises(ValueError):
                await controller.admit(workspace.project_id, "redirected-index",
                                       config_hash=controller.configuration()["config_hash"])
            assert host.store.get_run_identity("redirected-index") is None
            assert artifact.read_bytes() == before
        finally:
            root.unlink()
            saved.rename(root)
    finally:
        await host.close()


async def test_nested_state_checks_the_registered_execution_workspace(tmp_path):
    root = make_repo(tmp_path / "repo")
    (root / "source.txt").write_text("tracked current source")
    (root / ".git/info/exclude").write_text(".eventide/\n")
    subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "source fixture"], check=True,
                   capture_output=True)
    # This copy is user-created outside the source. Host's managed-worktree creation
    # still requires its state root outside the source; this test does not change that rule.
    copy = tmp_path / "external-copy"
    subprocess.run(["git", "-C", str(root), "worktree", "add", "--detach", str(copy), "HEAD"],
                   check=True, capture_output=True)
    host = RuntimeHost(replace(settings_for(root), state_dir=root / ".eventide"),
                       ScriptedProvider([{"text": "done"}]), enable_mcp=False)
    try:
        with pytest.raises(ValueError, match="outside source"):
            await host.admit_run(RunRequest("source task", knowledge_index="prepare"))
        actual = host.resolve_or_register_workspace(copy)
        assert actual.project_id == host.default_workspace.project_id
        assert actual.workspace_id != host.default_workspace.workspace_id
        session = host.create_session(workspace_id=actual.workspace_id)
        result = await host.run(RunRequest("inspect", session_id=session,
                                          knowledge_index="prepare"))
        assert result.status == "completed"
        evidence = host.store.get_run(result.run_id)["retrieval"]
        assert evidence["workspace_id"] == actual.workspace_id
        assert evidence["status"] == "available"
        assert not host.state_dir.is_relative_to(Path(actual.path))
        assert (managed_index_dir(host, actual) / "index.sqlite").is_file()
        assert not (copy / ".eventide").exists()
        assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"]) == b""
    finally:
        await host.close()


async def test_completed_index_history_remains_visible_when_source_is_temporarily_missing(tmp_path):
    host = host_for(tmp_path)
    workspace = host.default_workspace
    root = Path(workspace.path)
    (root / "document.txt").write_text("retained index history")
    controller = IndexController(host)
    try:
        await controller.admit(workspace.project_id, "recorded-index",
                               config_hash=controller.configuration()["config_hash"])
        await controller.start("recorded-index")
        expected = controller.detail("recorded-index")
        saved = tmp_path / "temporarily-moved-source"
        root.rename(saved)
        try:
            assert controller.list(workspace.project_id) == [expected]
            assert controller.detail("recorded-index") == expected
            assert expected["status"] == "completed"
        finally:
            saved.rename(root)
    finally:
        await host.close()
