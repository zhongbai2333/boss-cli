"""Tests for persistent Chrome CDP authentication."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def test_find_chrome_executable_windows(monkeypatch):
    from boss_cli import cdp_login

    monkeypatch.setattr(cdp_login.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    expected = r"C:\Users\test\AppData\Local\Google\Chrome\Application\chrome.exe"
    monkeypatch.setattr(cdp_login.os.path, "isfile", lambda path: path == expected)
    monkeypatch.setattr(cdp_login.shutil, "which", lambda name: None)

    assert cdp_login.find_chrome_executable() == expected


def test_extract_cdp_credential_filters_domains():
    from boss_cli.cdp_login import extract_cdp_credential

    connection = MagicMock()
    connection.send.return_value = {
        "cookies": [
            {"domain": ".zhipin.com", "name": "wt2", "value": "one"},
            {"domain": "www.zhipin.com", "name": "zp_at", "value": "two"},
            {"domain": ".example.com", "name": "secret", "value": "ignored"},
        ]
    }
    with patch("boss_cli.cdp_login.is_cdp_ready", return_value=True), \
         patch("boss_cli.cdp_login._CDPConnection", return_value=connection):
        credential = extract_cdp_credential(9222)

    assert credential is not None
    assert credential.cookies == {"wt2": "one", "zp_at": "two"}
    connection.send.assert_called_once_with("Storage.getCookies")
    connection.close.assert_called_once()


def test_launch_reuses_existing_cdp(tmp_path):
    from boss_cli.cdp_login import launch_cdp_chrome

    with patch("boss_cli.cdp_login.is_cdp_ready", return_value=True), \
         patch("boss_cli.cdp_login.subprocess.Popen") as popen:
        result = launch_cdp_chrome(profile_dir=tmp_path)

    assert result is None
    popen.assert_not_called()


def test_cdp_login_saves_complete_cookie_set(tmp_path):
    from boss_cli.auth import Credential
    from boss_cli.cdp_login import cdp_login

    credential = Credential({"__zp_stoken__": "s", "wt2": "1", "wbg": "2", "zp_at": "3"})
    with patch("boss_cli.cdp_login.launch_cdp_chrome"), \
         patch("boss_cli.cdp_login.extract_cdp_credential", return_value=credential), \
         patch("boss_cli.cdp_login.save_credential") as save:
        result = cdp_login(profile_dir=tmp_path, timeout=1)

    assert result is credential
    save.assert_called_once_with(credential)


def test_cdp_endpoint_rejects_public_and_accepts_sidecar():
    from boss_cli.cdp_login import CDPLoginUnavailable, _cdp_endpoint

    assert _cdp_endpoint(endpoint="http://chromium:9222") == "http://chromium:9222"
    assert _cdp_endpoint(endpoint="http://127.0.0.1:9222") == "http://127.0.0.1:9222"
    with pytest.raises(CDPLoginUnavailable):
        _cdp_endpoint(endpoint="http://example.com:9222")
    with pytest.raises(CDPLoginUnavailable):
        _cdp_endpoint(endpoint="http://user:password@chromium:9222")


def test_cdp_connection_rewrites_advertised_localhost(monkeypatch):
    from boss_cli import cdp_login

    response = MagicMock()
    response.json.return_value = {
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/browser-id"
    }
    monkeypatch.setattr(cdp_login.socket, "gethostbyname", lambda host: "172.18.0.3")
    monkeypatch.setattr(cdp_login.httpx, "get", lambda *args, **kwargs: response)
    websocket = MagicMock()
    monkeypatch.setitem(__import__("sys").modules, "websocket", websocket)

    connection = cdp_login._CDPConnection(endpoint="http://chromium:9222")
    connection.close()

    websocket.create_connection.assert_called_once_with(
        "ws://172.18.0.3:9222/devtools/browser/browser-id",
        timeout=10,
        origin="http://chromium",
    )


def test_transport_endpoint_rejects_public_dns(monkeypatch):
    from boss_cli.cdp_login import CDPLoginUnavailable, _transport_endpoint

    monkeypatch.setattr("boss_cli.cdp_login.socket.gethostbyname", lambda host: "8.8.8.8")
    with pytest.raises(CDPLoginUnavailable):
        _transport_endpoint(endpoint="http://chromium:9223")


def test_refresh_cdp_credential_replaces_stoken_and_exports_complete_session(monkeypatch):
    from boss_cli.auth import Credential
    from boss_cli.cdp_login import refresh_cdp_credential

    original = Credential({"wt2": "one", "wbg": "two", "zp_at": "three", "__zp_stoken__": "expired"})
    refreshed_entries = [
        {"domain": ".zhipin.com", "name": "wt2", "value": "one"},
        {"domain": ".zhipin.com", "name": "wbg", "value": "two"},
        {"domain": ".zhipin.com", "name": "zp_at", "value": "three"},
        {"domain": ".zhipin.com", "name": "__zp_stoken__", "value": "fresh"},
    ]
    connection = MagicMock()

    def send(method, params=None):
        if method == "Target.createTarget":
            return {"targetId": "target"}
        if method == "Storage.getCookies":
            return {"cookies": refreshed_entries}
        return {}

    connection.send.side_effect = send
    monkeypatch.setattr("boss_cli.cdp_login.is_cdp_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr("boss_cli.cdp_login._CDPConnection", lambda *args, **kwargs: connection)

    result = refresh_cdp_credential(original, endpoint="http://chromium:9222", timeout=1)

    assert result is not None
    assert result.cookies["__zp_stoken__"] == "fresh"
    set_call = next(call for call in connection.send.call_args_list if call.args[0] == "Storage.setCookies")
    serialized = json.dumps(set_call.args[1], ensure_ascii=False)
    assert "expired" not in serialized
    assert "__zp_stoken__" not in serialized
    connection.send.assert_any_call("Storage.clearCookies")
    connection.send.assert_any_call("Target.closeTarget", {"targetId": "target"})
    connection.close.assert_called_once()