"""Saved summary comparison must point to the actual frozen model input."""
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from eventide.api import create_app
from eventide.context_builder import ContextBuilder
from eventide.host import RuntimeHost
from eventide.models import RunRequest
from eventide.providers import ScriptedProvider
from eventide.workspace import digest
from tests.test_host import make_repo
from tests.test_runtime import settings_for


def test_summary_api_compares_real_checkpoint_and_original_input(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(replace(settings_for(repo), state_dir=isolated_workspace / "state"),
                       ScriptedProvider([{"text": "已检查"}, {"text": "later"}]))
    summary = json.dumps({
        "schema_version": 2, "goal": "定位问题", "hard_constraints": [
            {"text": "不要修改API", "src": [{"msg": 0, "quote": "不要修改API"}]},
        ], "facts": [], "unverified": ["尚未确认原因"], "checks": [],
        "failures": [], "next_steps": ["继续调查"],
    }, ensure_ascii=False)
    summarizer = ScriptedProvider([{"text": summary}])

    async def prepare():
        sid = host.create_session()
        await host.run(RunRequest("不要修改API。" + "详细调查记录。" * 600, sid))
        await ContextBuilder(host.store).build(
            sid, provider=summarizer, provider_name=host.settings.provider,
            model=host.settings.model, budget=20000, force=True,
        )
        return sid

    with TestClient(create_app(host)) as client:
        sid = client.portal.call(prepare)
        versions = client.get(f"/api/sessions/{sid}/context-summaries").json()
        assert len(versions["versions"]) == 1
        cp = versions["versions"][0]
        base = f"/api/sessions/{sid}/context-summaries/{cp['id']}"
        detail = client.get(base).json()
        assert detail["source_verified"] is True
        assert detail["input_verification"] == "matched"
        assert detail["structured"]["hard_constraints"][0]["text"] == "不要修改API"
        history = json.loads(summarizer.requests[0].messages[0]["content"])
        assert detail["history_hash"] == digest(history)
        first = client.get(f"{base}/messages/0?limit=100").json()
        assert first["next_offset"] == 100
        rest = client.get(f"{base}/messages/0?offset=100").json()
        assert json.loads(first["content"] + rest["content"]) == history[0]
        assert rest["history_hash"] == first["history_hash"]
        assert rest["next_offset"] is None
        before = host.store.session_events(sid)
        assert client.get(base).json() == detail
        assert host.store.session_events(sid) == before  # reads do not mutate evidence
        client.portal.call(host.run, RunRequest("later history must not enter the cut", sid))
        assert client.get(base).json() == detail
        assert "later history" not in client.get(f"{base}/messages/0").text
        assert client.get(f"{base}/messages/99").status_code == 404
        assert client.get(f"{base}/messages/0?offset=999999").status_code == 422
        assert client.get(f"{base}/messages/0?limit=12001").status_code == 422
        other = host.create_session()
        assert client.get(f"/api/sessions/{other}/context-summaries/{cp['id']}").status_code == 404
        assert client.get("/api/sessions/missing/context-summaries").status_code == 404


def test_legacy_missing_claim_changed_projection_and_bounded_version_paging(isolated_workspace):
    repo = make_repo(isolated_workspace / "repo")
    host = RuntimeHost(replace(settings_for(repo), state_dir=isolated_workspace / "state"),
                       ScriptedProvider([{"text": "done"}]))
    with TestClient(create_app(host)) as client:
        sid = host.create_session()
        client.portal.call(host.run, RunRequest("source fact", sid))
        events = host.store.session_events(sid)
        cut, fingerprint = events[-1]["session_seq"], digest(events)
        for text in ["legacy plain text", "z" * 25000]:
            host.store.save_checkpoint(sid, cut, fingerprint, text, "scripted", "m")
        page = client.get(f"/api/sessions/{sid}/context-summaries?limit=1").json()
        assert len(page["versions"]) == 1 and page["next_before"] is not None
        cp = page["versions"][0]["id"]
        base = f"/api/sessions/{sid}/context-summaries/{cp}"
        detail = client.get(base).json()
        assert detail["source_verified"] is True
        assert detail["input_verification"] == "claim_missing"
        assert detail["summary_truncated"] is True
        assert len(detail["summary"]) == 24000
        earlier = client.get(f"/api/sessions/{sid}/context-summaries?before={cp}&limit=1").json()
        assert earlier["next_before"] is None
        assert earlier["versions"][0]["id"] != cp
        host.store.append_fact(sid, "context.summary_claimed", {
            "covered_seq": cut, "provider": "scripted", "model": "m", "prefix_hash": "old",
        })
        assert client.get(base).json()["input_verification"] == "projection_changed"
        host.store.save_checkpoint(sid, cut, "invalid", "saved text", "scripted", "m")
        bad = host.store.checkpoints(sid)[0]["id"]
        bad_base = f"/api/sessions/{sid}/context-summaries/{bad}"
        assert client.get(bad_base).json()["source_verified"] is False
        assert client.get(f"{bad_base}/messages/0").status_code == 409
