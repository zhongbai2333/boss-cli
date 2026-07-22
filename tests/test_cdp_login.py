"""Tests for persistent Chrome CDP authentication."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_find_chrome_executable_windows(monkeypatch):
    from boss_cli import cdp_login

    monkeypatch.setattr(cdp_login.platform, "system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\test\AppData\Local")
    expected = r"C:\Users\test\AppData\Local\Google\Chrome\Application\chrome.exe"
    monkeypatch.setattr(cdp_login.os.path, "isfile", lambda path: path == expected)

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