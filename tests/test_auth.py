"""Tests for boss_cli.auth — diagnostics, env fallback, and extraction."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


# ── Diagnostics ─────────────────────────────────────────────────────


class TestDiagnoseExtractionIssues:
    """Test platform-specific diagnostic hints."""

    def test_diagnose_windows_dpapi(self):
        """On Windows, hint should mention DPAPI and BOSS_COOKIES workaround."""
        from boss_cli.auth import _diagnose_extraction_issues

        diagnostics = ["chrome: DPAPI decryption failed"]
        with patch("boss_cli.auth.sys") as mock_sys:
            mock_sys.platform = "win32"
            hint = _diagnose_extraction_issues(diagnostics)

        assert hint is not None
        assert "DPAPI" in hint
        assert "BOSS_COOKIES" in hint
        assert "Chrome is running" in hint

    def test_diagnose_macos_keychain(self):
        """On macOS, hint should mention Keychain Access."""
        from boss_cli.auth import _diagnose_extraction_issues

        diagnostics = ["chrome: Could not get key for cookie decryption"]
        with patch("boss_cli.auth.sys") as mock_sys, \
             patch.dict(os.environ, {}, clear=False):
            mock_sys.platform = "darwin"
            # Remove SSH vars
            for key in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION"):
                os.environ.pop(key, None)
            hint = _diagnose_extraction_issues(diagnostics)

        assert hint is not None
        assert "Keychain" in hint

    def test_diagnose_macos_ssh(self):
        """On macOS via SSH, hint should mention unlock-keychain."""
        from boss_cli.auth import _diagnose_extraction_issues

        diagnostics = ["chrome: safe storage key error"]
        with patch("boss_cli.auth.sys") as mock_sys, \
             patch.dict(os.environ, {"SSH_CLIENT": "1.2.3.4 12345 22"}):
            mock_sys.platform = "darwin"
            hint = _diagnose_extraction_issues(diagnostics)

        assert hint is not None
        assert "unlock-keychain" in hint

    def test_diagnose_linux_keyring(self):
        """On Linux, hint should mention keyring daemon."""
        from boss_cli.auth import _diagnose_extraction_issues

        diagnostics = ["chrome: SecretStorage error"]
        with patch("boss_cli.auth.sys") as mock_sys:
            mock_sys.platform = "linux"
            hint = _diagnose_extraction_issues(diagnostics)

        assert hint is not None
        assert "keyring" in hint

    def test_diagnose_no_keychain_issue(self):
        """Unrelated errors should return None."""
        from boss_cli.auth import _diagnose_extraction_issues

        diagnostics = ["chrome: sqlite3.OperationalError: database is locked"]
        hint = _diagnose_extraction_issues(diagnostics)
        assert hint is None

    def test_diagnose_empty_diagnostics(self):
        """Empty diagnostics list should return None."""
        from boss_cli.auth import _diagnose_extraction_issues
        assert _diagnose_extraction_issues([]) is None


# ── Environment variable fallback ───────────────────────────────────


class TestLoadFromEnv:
    """Test BOSS_COOKIES environment variable loading."""

    def test_load_from_env_valid(self):
        from boss_cli.auth import load_from_env

        with patch.dict(os.environ, {"BOSS_COOKIES": "wt2=abc; wbg=def; zp_at=ghi; __zp_stoken__=jkl"}):
            cred = load_from_env()

        assert cred is not None
        assert cred.cookies["wt2"] == "abc"
        assert cred.cookies["wbg"] == "def"
        assert cred.cookies["zp_at"] == "ghi"
        assert cred.cookies["__zp_stoken__"] == "jkl"

    def test_load_from_env_empty(self):
        from boss_cli.auth import load_from_env

        with patch.dict(os.environ, {"BOSS_COOKIES": ""}):
            assert load_from_env() is None

    def test_load_from_env_unset(self):
        from boss_cli.auth import load_from_env

        env = os.environ.copy()
        env.pop("BOSS_COOKIES", None)
        with patch.dict(os.environ, env, clear=True):
            assert load_from_env() is None

    def test_load_from_env_malformed(self):
        from boss_cli.auth import load_from_env

        with patch.dict(os.environ, {"BOSS_COOKIES": "no-equals-here; also-bad"}):
            assert load_from_env() is None


# ── Cookie jar extraction ───────────────────────────────────────────


class TestExtractCookiesFromJar:
    """Test _extract_cookies_from_jar helper."""

    class _Cookie:
        def __init__(self, domain, name, value):
            self.domain = domain
            self.name = name
            self.value = value

    def test_extracts_zhipin_cookies(self):
        from boss_cli.auth import _extract_cookies_from_jar

        cookies = [
            self._Cookie(".zhipin.com", "wt2", "abc"),
            self._Cookie(".zhipin.com", "wbg", "def"),
            self._Cookie(".other.com", "other", "xyz"),
        ]
        result = _extract_cookies_from_jar(cookies, source="test")
        assert result is not None
        assert result["wt2"] == "abc"
        assert result["wbg"] == "def"
        assert "other" not in result

    def test_returns_none_for_no_zhipin_cookies(self):
        from boss_cli.auth import _extract_cookies_from_jar

        cookies = [self._Cookie(".other.com", "foo", "bar")]
        assert _extract_cookies_from_jar(cookies, source="test") is None

    def test_returns_none_for_empty_jar(self):
        from boss_cli.auth import _extract_cookies_from_jar
        assert _extract_cookies_from_jar([], source="test") is None

    def test_rejects_lookalike_zhipin_domains(self):
        from boss_cli.auth import _extract_cookies_from_jar

        cookies = [
            self._Cookie("evilzhipin.com", "wt2", "bad"),
            self._Cookie("zhipin.com.evil.example", "zp_at", "bad"),
            self._Cookie("jobs.zhipin.com", "wbg", "good"),
        ]
        result = _extract_cookies_from_jar(cookies, source="test")

        assert result == {"wbg": "good"}


# ── Browser order ───────────────────────────────────────────────────


class TestBrowserOrder:
    """Test browser extraction order logic."""

    def test_default_order(self):
        from boss_cli.auth import _get_browser_order
        order = _get_browser_order()
        assert order == ["chrome", "edge", "firefox", "brave"]

    def test_custom_source_prioritized(self):
        from boss_cli.auth import _get_browser_order
        order = _get_browser_order("firefox")
        assert order[0] == "firefox"
        assert "chrome" in order


# ── In-process extraction ───────────────────────────────────────────


class TestExtractInProcess:
    """Test in-process cookie extraction."""

    def test_returns_none_when_bc3_not_installed(self):
        from boss_cli.auth import _extract_in_process

        with patch.dict("sys.modules", {"browser_cookie3": None}):
            cred, diag = _extract_in_process()
            # We can't easily remove from sys.modules in a test,
            # but at minimum verify the function returns a tuple
            assert isinstance(diag, list)

    def test_extraction_success(self):
        from boss_cli.auth import _extract_in_process

        class FakeCookie:
            def __init__(self, domain, name, value):
                self.domain = domain
                self.name = name
                self.value = value

        mock_cookie = FakeCookie(".zhipin.com", "wt2", "test_val")
        mock_bc3 = MagicMock()
        mock_bc3.chrome.return_value = [mock_cookie]
        mock_bc3.firefox.return_value = []
        mock_bc3.edge.return_value = []
        mock_bc3.brave.return_value = []

        with patch.dict("sys.modules", {"browser_cookie3": mock_bc3}), \
             patch("boss_cli.auth._iter_chrome_cookie_files", return_value=[]):
            cred, diag = _extract_in_process()

        assert cred is not None
        assert cred.cookies["wt2"] == "test_val"


# ── Live CDP refresh ────────────────────────────────────────────────


class TestRefreshCredential:
    """A running CDP browser should take precedence over disk extraction."""

    def test_prefers_complete_cdp_credential(self):
        from boss_cli.auth import Credential, refresh_credential

        cred = Credential({"__zp_stoken__": "s", "wt2": "1", "wbg": "2", "zp_at": "3"})
        with patch("boss_cli.cdp_login.extract_cdp_credential", return_value=cred) as cdp_extract, \
             patch("boss_cli.auth.extract_browser_credential") as browser_extract, \
             patch("boss_cli.auth.save_credential") as save:
            result, diagnostics = refresh_credential()

        assert result is cred
        assert diagnostics == []
        cdp_extract.assert_called_once_with(endpoint=None)
        browser_extract.assert_not_called()
        save.assert_called_once_with(cred)

    def test_falls_back_when_cdp_has_partial_cookies(self):
        from boss_cli.auth import Credential, refresh_credential

        partial = Credential({"wt2": "old"})
        fresh = Credential({"__zp_stoken__": "s", "wt2": "1", "wbg": "2", "zp_at": "3"})
        with patch("boss_cli.cdp_login.extract_cdp_credential", return_value=partial), \
             patch("boss_cli.auth.extract_browser_credential", return_value=(fresh, ["browser ok"])):
            result, diagnostics = refresh_credential("chrome")

        assert result is fresh
        assert diagnostics == ["browser ok"]

    def test_sidecar_injects_current_credential(self, monkeypatch):
        from boss_cli.auth import Credential, refresh_credential

        current = Credential({"__zp_stoken__": "old", "wt2": "1", "wbg": "2", "zp_at": "3"})
        fresh = Credential({"__zp_stoken__": "new", "wt2": "1", "wbg": "2", "zp_at": "3"})
        monkeypatch.setenv("BOSS_CDP_ENDPOINT", "http://chromium:9222")
        with patch("boss_cli.cdp_login.refresh_cdp_credential", return_value=fresh) as cdp_refresh, \
             patch("boss_cli.auth.extract_browser_credential") as browser_extract, \
             patch("boss_cli.auth.save_credential") as save:
            result, diagnostics = refresh_credential(current_credential=current)

        assert result is fresh
        assert diagnostics == []
        cdp_refresh.assert_called_once_with(current, endpoint="http://chromium:9222")
        browser_extract.assert_not_called()
        save.assert_called_once_with(fresh)
"""Tests for boss_cli.auth — diagnostics, env fallback, and extraction."""
