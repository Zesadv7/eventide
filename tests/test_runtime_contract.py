"""MK-00 contract identity: explicit versions, real capabilities, honest failures."""

import sqlite3

from fastapi.testclient import TestClient

import eventide
from eventide.api import create_app
from eventide.host import RuntimeHost
from eventide.http_api import contracts
from tests.test_runtime import settings_for


def contract_app(isolated_workspace):
    runtime = RuntimeHost(settings_for(isolated_workspace))
    return runtime, TestClient(create_app(runtime))


def test_contract_reports_store_schema_and_exact_payload_shape(isolated_workspace):
    runtime, client = contract_app(isolated_workspace)
    try:
        response = client.get("/api/runtime/contract")
        assert response.status_code == 200
        body = response.json()
        # The payload shape is part of the contract; extra fields need a version bump.
        assert set(body) == {
            "contract_version",
            "schema_version",
            "runtime_version",
            "capabilities",
        }
        assert body["contract_version"] == contracts.CONTRACT_VERSION == 1
        applied = sqlite3.connect(runtime.store.path).execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        assert body["schema_version"] == applied == 6
        assert body["runtime_version"] == eventide.__version__
    finally:
        client.close()
        runtime.store.close()


def test_capabilities_list_only_shipped_features(isolated_workspace):
    runtime, client = contract_app(isolated_workspace)
    try:
        capabilities = client.get("/api/runtime/contract").json()["capabilities"]
        assert len(capabilities) == len(set(capabilities))
        assert capabilities == list(contracts.IMPLEMENTED_CAPABILITIES)
        for known in ("projects", "sessions", "delivery", "file_undo", "memory"):
            assert known in capabilities
        # Planned MK-xx work must never be advertised as available.
        for planned in ("graph", "recovery", "side_questions", "artifacts", "attention"):
            assert planned not in capabilities
    finally:
        client.close()
        runtime.store.close()


def test_unreadable_store_version_answers_503_without_guessing(isolated_workspace, monkeypatch):
    runtime, client = contract_app(isolated_workspace)
    try:
        monkeypatch.setattr(contracts, "store_schema_version", lambda _path: None)
        response = client.get("/api/runtime/contract")
        assert response.status_code == 503
        assert "不可读" in response.json()["message"]
    finally:
        client.close()
        runtime.store.close()


def test_store_schema_version_reads_readonly_without_mutating(tmp_path):
    database = tmp_path / "runtime.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE schema_migrations"
            " (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);"
            "INSERT INTO schema_migrations VALUES (6, 0);"
        )
    assert contracts.store_schema_version(database) == 6
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0] == 6


def test_store_schema_version_returns_none_for_missing_or_legacy_layout(tmp_path):
    assert contracts.store_schema_version(tmp_path / "absent.sqlite") is None
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript("CREATE TABLE sessions (id TEXT PRIMARY KEY);")
    # Pre-schema_migrations databases cannot claim a data version.
    assert contracts.store_schema_version(database) is None
