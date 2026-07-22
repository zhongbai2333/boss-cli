"""Tests for encrypted full-configuration migration packages."""

from __future__ import annotations

import json

import pytest
from dotenv import dotenv_values
from dotenv import load_dotenv

from boss_cli.auth import Credential
from boss_cli.config_transfer import (
    collect_portable_settings,
    create_config_package,
    export_config_file,
    import_config_file,
    parse_config_package,
    write_portable_settings,
)
from boss_cli.credential_transfer import CredentialTransferError


@pytest.fixture
def complete_credential() -> Credential:
    return Credential({
        "__zp_stoken__": "secret-stoken",
        "wt2": "secret-wt2",
        "wbg": "secret-wbg",
        "zp_at": "secret-zp-at",
    })


def test_full_package_round_trip_hides_all_secrets(complete_credential):
    settings = {
        "PLUGIN_API_KEY": "plugin-super-secret",
        "LLM_API_KEY": "llm-super-secret",
        "LLM_MODEL": "test-model",
        "MOHRSS_AUTHORIZED": "true",
    }

    package = create_config_package(complete_credential, settings, "correct horse battery staple")
    serialized = json.dumps(package)

    for secret in ("secret-stoken", "__zp_stoken__", "plugin-super-secret", "llm-super-secret", "LLM_API_KEY"):
        assert secret not in serialized
    restored = parse_config_package(package, "correct horse battery staple")
    assert restored.credential.cookies == complete_credential.cookies
    assert restored.settings == settings


def test_collect_settings_uses_whitelist_and_environment_priority(tmp_path, monkeypatch):
    project_env = tmp_path / ".env"
    persistent_env = tmp_path / "plugin.env"
    project_env.write_text(
        "PLUGIN_PORT=7000\nBOSS_COOKIES=must-not-export\nBOSS_CREDENTIAL_PASSPHRASE=never\nUNKNOWN=value\n",
        encoding="utf-8",
    )
    persistent_env.write_text("PLUGIN_PORT=8000\nLLM_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("PLUGIN_PORT", "9000")
    monkeypatch.setenv("LLM_API_KEY", "runtime-secret")

    settings = collect_portable_settings(
        project_env_file=project_env,
        persistent_env_file=persistent_env,
    )

    assert settings["PLUGIN_PORT"] == "9000"
    assert settings["LLM_MODEL"] == "from-file"
    assert settings["LLM_API_KEY"] == "runtime-secret"
    assert "BOSS_COOKIES" not in settings
    assert "BOSS_CREDENTIAL_PASSPHRASE" not in settings
    assert "UNKNOWN" not in settings


def test_unknown_setting_is_rejected(complete_credential):
    with pytest.raises(CredentialTransferError, match="不允许"):
        create_config_package(
            complete_credential,
            {"CACHE_DB_FILE": "C:/private/cache.db"},
            "correct horse battery staple",
        )


def test_config_file_round_trip_and_overwrite_guard(tmp_path, complete_credential):
    output = tmp_path / "cloud.bossconfig"
    export_config_file(
        output,
        complete_credential,
        {"PLUGIN_PORT": "8000"},
        "correct horse battery staple",
    )

    assert import_config_file(output, "correct horse battery staple").settings == {"PLUGIN_PORT": "8000"}
    with pytest.raises(CredentialTransferError, match="已存在"):
        export_config_file(
            output,
            complete_credential,
            {},
            "correct horse battery staple",
        )


def test_write_settings_is_atomic_private_and_dotenv_compatible(tmp_path):
    output = tmp_path / "plugin.env"
    settings = {
        "PLUGIN_API_KEY": "secret with spaces and # hash",
        "LLM_BASE_URL": "https://example.test/v1",
        "MOHRSS_AUTHORIZED": "true",
    }

    write_portable_settings(settings, output)

    loaded = dotenv_values(output)
    assert loaded == settings
    assert "BOSS_CREDENTIAL_PASSPHRASE" not in output.read_text(encoding="utf-8")


def test_imported_snapshot_can_override_cloud_placeholder(tmp_path, monkeypatch):
    output = tmp_path / "plugin.env"
    write_portable_settings({"PLUGIN_API_KEY": "imported-secret"}, output)
    monkeypatch.setenv("PLUGIN_API_KEY", "cloud-placeholder")

    load_dotenv(output, override=True)

    assert __import__("os").environ["PLUGIN_API_KEY"] == "imported-secret"