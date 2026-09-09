"""Encrypted, restart-safe provider configuration tests."""

import sqlite3

import pytest
from cryptography.fernet import Fernet

from eventide.runtime import AgentRuntime
from eventide.secrets import SecretKeyError
from tests.test_runtime import settings_for


async def test_provider_config_is_encrypted_and_restored(isolated_workspace):
    settings = settings_for(isolated_workspace)
    runtime = AgentRuntime(settings)
    await runtime.configure_provider(
        provider="openai_responses",
        api_key="persistent-test-secret",
        base_url="https://model.example/v1",
        model="response-model",
        persist=True,
    )
    record = runtime.store.get_provider_config()
    assert record and record["api_key_ciphertext"]
    assert "persistent-test-secret" not in record["api_key_ciphertext"]
    database = settings.state_dir / "runtime.sqlite"
    await runtime.close()

    raw_database = database.read_bytes()
    assert b"persistent-test-secret" not in raw_database
    restored = AgentRuntime(settings)
    assert restored.settings.provider == "openai_responses"
    assert restored.settings.api_key == "persistent-test-secret"
    status = restored.provider_config_status()
    assert status["persistence"] == "sqlite_encrypted"
    assert status["api_key_source"] == "sqlite_encrypted"
    await restored.close()


async def test_wrong_master_key_is_reported_without_plaintext_fallback(
    isolated_workspace, monkeypatch
):
    monkeypatch.setenv("EVENTIDE_SECRET_KEY", Fernet.generate_key().decode())
    settings = settings_for(isolated_workspace)
    runtime = AgentRuntime(settings)
    await runtime.configure_provider(
        provider="anthropic",
        api_key="encrypted-value",
        base_url=None,
        model="model",
        persist=True,
    )
    await runtime.close()

    monkeypatch.setenv("EVENTIDE_SECRET_KEY", Fernet.generate_key().decode())
    restored = AgentRuntime(settings)
    status = restored.provider_config_status()
    assert restored.settings.api_key is None
    assert status["api_key_status"] == "decrypt_error"
    assert "无法解密" in status["configuration_error"]
    with pytest.raises(SecretKeyError, match="请输入新密钥"):
        await restored.configure_provider(
            provider="anthropic",
            api_key=None,
            base_url=None,
            model="other-model",
            persist=True,
        )
    with pytest.raises(SecretKeyError, match="无法解密"):
        await restored.probe_provider(
            provider="anthropic", api_key=None, base_url=None, model="model"
        )
    await restored.close()


async def test_clear_key_and_reset_to_startup_config(isolated_workspace):
    settings = settings_for(
        isolated_workspace,
        provider="anthropic",
        api_key="environment-key",
        model="startup-model",
    )
    runtime = AgentRuntime(settings)
    await runtime.configure_provider(
        provider="openai_compatible",
        api_key="saved-key",
        base_url="https://example.invalid/v1",
        model="saved-model",
        persist=True,
    )
    await runtime.configure_provider(
        provider="openai_compatible",
        api_key=None,
        base_url="https://example.invalid/v1",
        model="saved-model",
        persist=True,
        clear_api_key=True,
    )
    record = runtime.store.get_provider_config()
    assert record and record["api_key_ciphertext"] is None
    assert runtime.settings.api_key == "environment-key"

    await runtime.reset_provider_configuration()
    assert runtime.store.get_provider_config() is None
    assert runtime.settings.provider == "anthropic"
    assert runtime.settings.model == "startup-model"
    await runtime.close()


async def test_provider_config_table_contains_single_row(isolated_workspace):
    runtime = AgentRuntime(settings_for(isolated_workspace))
    runtime.store.save_provider_config(
        provider="anthropic", base_url=None, model="one", api_key_ciphertext=None
    )
    runtime.store.save_provider_config(
        provider="anthropic", base_url=None, model="two", api_key_ciphertext=None
    )
    connection = sqlite3.connect(runtime.store.path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM provider_config").fetchone()[0]
    finally:
        connection.close()
    assert count == 1
    await runtime.close()
