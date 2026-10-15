"""Live configuration resolution and disposable evaluation ownership."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import eventide.evaluation as evaluation
from eventide.host import RuntimeHost
from eventide.provider_config import resolve_provider_configuration
from eventide.providers import ScriptedProvider
from eventide.secrets import SecretKeyError
from tests.test_runtime import settings_for


def _suite(root, cases):
    path = root / "suite.json"
    path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
    return path


def _direct_case(**changes):
    return {"id": "direct", "prompt": "answer", "script": [{"text": "done"}], **changes}


async def test_resolver_reads_live_wal_without_host_lease_or_recovery(isolated_workspace):
    startup = settings_for(isolated_workspace, api_key="environment-value")
    runtime = RuntimeHost(startup)
    try:
        await runtime.configure_provider(
            provider="openai_responses", api_key="persisted-value",
            base_url="https://example.invalid/v1", model="saved-model", persist=True,
        )
        session = runtime.create_session("pending")
        runtime.store.create_run("unfinished", session)
        runtime.store.append_event("unfinished", "run.started", {"prompt": "unfinished"})
        before = runtime.store.run_events("unfinished")

        resolved = resolve_provider_configuration(startup)

        assert resolved == runtime.settings
        assert resolved.api_key == "persisted-value"
        assert runtime.store.run_events("unfinished") == before
        # Constructing another Host here would fail its lease and/or recover the run.
        assert not runtime.store.run_events("unfinished")[-1]["type"].startswith("run.interrupted")
    finally:
        await runtime.close()


def test_resolver_does_not_create_missing_state_or_secret(isolated_workspace):
    settings = settings_for(isolated_workspace)
    assert resolve_provider_configuration(settings) is settings
    assert not settings.state_dir.exists()


async def test_missing_key_is_explicit_and_not_regenerated(isolated_workspace, monkeypatch):
    monkeypatch.delenv("EVENTIDE_SECRET_KEY", raising=False)
    settings = settings_for(isolated_workspace, api_key="environment-fallback-forbidden")
    runtime = RuntimeHost(settings)
    await runtime.configure_provider(
        provider="openai_responses", api_key="saved-value", base_url=None,
        model="saved", persist=True,
    )
    await runtime.close()
    key = settings.state_dir / "secret.key"
    key.unlink()
    database = settings.state_dir / "runtime.sqlite"
    before = database.read_bytes()

    with pytest.raises(SecretKeyError, match="缺少解密密钥"):
        resolve_provider_configuration(settings)

    assert not key.exists()
    assert database.read_bytes() == before


async def test_decryption_failure_prevents_provider_or_sandbox_creation(
    isolated_workspace, monkeypatch,
):
    monkeypatch.setenv("EVENTIDE_SECRET_KEY", Fernet.generate_key().decode())
    settings = settings_for(isolated_workspace, api_key="environment-fallback-forbidden")
    runtime = RuntimeHost(settings)
    await runtime.configure_provider(
        provider="openai_responses", api_key="saved-value", base_url=None,
        model="saved", persist=True,
    )
    await runtime.close()
    monkeypatch.setenv("EVENTIDE_SECRET_KEY", Fernet.generate_key().decode())

    def forbidden(*args, **kwargs):
        raise AssertionError("failed config must not create provider or sandbox")

    monkeypatch.setattr(evaluation, "build_provider", forbidden)
    monkeypatch.setattr(evaluation, "_create_sandbox", forbidden)
    with pytest.raises(SecretKeyError, match="无法解密"):
        await evaluation.run_evaluations(
            _suite(isolated_workspace, [_direct_case()]), live=True, settings=settings,
        )


def test_resolver_legacy_without_configuration_is_read_only(isolated_workspace):
    settings = settings_for(isolated_workspace)
    settings.state_dir.mkdir()
    database = settings.state_dir / "runtime.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_data (value TEXT)")
    before = database.read_bytes()
    assert resolve_provider_configuration(settings) is settings
    assert database.read_bytes() == before
    assert not (settings.state_dir / "secret.key").exists()


def test_resolver_corrupt_database_has_explicit_error(isolated_workspace):
    settings = settings_for(isolated_workspace)
    settings.state_dir.mkdir()
    (settings.state_dir / "runtime.sqlite").write_text("not sqlite", encoding="utf-8")
    with pytest.raises(RuntimeError, match="只读解析"):
        resolve_provider_configuration(settings)


async def test_offline_never_resolves_user_config_or_opens_network(isolated_workspace, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline eval must not resolve credentials or use a network provider")

    monkeypatch.setattr(evaluation, "resolve_provider_configuration", forbidden)
    monkeypatch.setattr(evaluation, "build_provider", forbidden)
    captured = []
    original = evaluation.RuntimeHost

    def host(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(evaluation, "RuntimeHost", host)
    settings = settings_for(isolated_workspace, api_key="unused-secret")
    report = await evaluation.run_evaluations(
        _suite(isolated_workspace, [_direct_case()]), settings=settings,
    )
    assert report["passed"] == 1
    assert captured[0]["settings"].api_key is None
    assert captured[0]["enable_mcp"] is False
    assert not settings.state_dir.exists()


async def test_live_uses_persisted_config_but_does_not_copy_secret_into_artifacts(
    isolated_workspace, monkeypatch,
):
    settings = settings_for(isolated_workspace, api_key="startup-secret")
    runtime = RuntimeHost(settings)
    await runtime.configure_provider(
        provider="openai_responses", api_key="memory-only-test-secret",
        base_url="https://example.invalid/v1", model="saved-model", persist=True,
    )
    await runtime.close()
    source_database = settings.state_dir / "runtime.sqlite"
    before = source_database.read_bytes()
    selected = []

    def provider(config):
        selected.append(config)
        return ScriptedProvider([{"text": "done"}])

    monkeypatch.setattr(evaluation, "build_provider", provider)
    report = await evaluation.run_evaluations(
        _suite(isolated_workspace, [_direct_case()]), live=True, settings=settings,
    )

    assert report["passed"] == 1
    assert selected[0].provider == "openai_responses"
    assert selected[0].model == "saved-model"
    assert selected[0].api_key == "memory-only-test-secret"
    assert selected[0].state_dir != settings.state_dir
    assert source_database.read_bytes() == before
    for file in Path(report["artifact_dir"]).rglob("*"):
        if file.is_file():
            assert b"memory-only-test-secret" not in file.read_bytes(), file.name
            assert file.name != "secret.key"


async def test_every_case_is_disposable_and_mcp_is_off_even_live(isolated_workspace, monkeypatch):
    captured = []
    original = evaluation.RuntimeHost

    def host(**kwargs):
        captured.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(evaluation, "RuntimeHost", host)
    monkeypatch.setattr(evaluation, "build_provider", lambda settings: ScriptedProvider([
        {"tool_calls": [{"id": "write", "name": "write_file", "arguments": {
            "path": "candidate.txt", "content": "changed",
        }}]}, {"text": "done"},
    ]))
    (isolated_workspace / "candidate.txt").write_text("user data", encoding="utf-8")
    cases = [_direct_case(), _direct_case(id="fixture", workspace={"mcp.json": "invalid"})]
    output = isolated_workspace / "explicit-output"
    report = await evaluation.run_evaluations(
        _suite(isolated_workspace, cases), live=True,
        settings=replace(settings_for(isolated_workspace), api_key="in-memory"),
        output_dir=output,
    )

    assert report["passed"] == 2
    assert [item["artifact_dir"] for item in report["results"]] == [
        str(item["settings"].workdir) for item in captured
    ]
    assert len({item["settings"].state_dir for item in captured}) == 2
    for item in captured:
        assert item["enable_mcp"] is False
        assert item["settings"].workdir != isolated_workspace
        assert (item["settings"].workdir / "candidate.txt").read_text() == "changed"
    assert (isolated_workspace / "candidate.txt").read_text() == "user data"
    assert not (isolated_workspace / ".eventide").exists()
    assert list(output.iterdir()) == [output / "live-report.json"]
    assert json.loads((output / "live-report.json").read_text()) == json.loads(json.dumps(report))


async def test_failed_evaluation_keeps_artifacts_without_changing_user_state(isolated_workspace):
    settings = settings_for(isolated_workspace)
    report = await evaluation.run_evaluations(
        _suite(isolated_workspace, [_direct_case(expected={"final_contains": "missing"})]),
        settings=settings,
    )
    assert report["passed"] == 0
    result = report["results"][0]
    assert result["reasons"]
    assert Path(result["artifact_dir"]).is_dir()
    assert Path(report["report_path"]).is_file()
    assert not settings.state_dir.exists()


def test_unreadable_saved_key_is_explicit_without_fallback(isolated_workspace, monkeypatch):
    from eventide.secrets import SecretBox

    monkeypatch.delenv("EVENTIDE_SECRET_KEY", raising=False)
    box = SecretBox(isolated_workspace)
    encrypted = box.encrypt("saved-value")
    original = Path.read_bytes

    def unreadable(path):
        if path == box.key_path:
            raise PermissionError("fixture key denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    with pytest.raises(SecretKeyError, match="无法读取解密密钥"):
        SecretBox(isolated_workspace).decrypt(encrypted)
