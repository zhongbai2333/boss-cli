"""Persistent Chrome login through the Chrome DevTools Protocol.

This module keeps authentication inside a real, isolated Chrome profile and
exports only zhipin.com cookies for the existing HTTP client.  It is optional:
normal browser-cookie3 and QR login flows continue to work without the CDP
dependencies installed.
"""

from __future__ import annotations

import json
import logging
import ntpath
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .auth import Credential, save_credential
from .constants import BASE_URL, CDP_PROFILE_DIR, DEFAULT_CDP_PORT, REQUIRED_COOKIES, is_zhipin_cookie_domain

logger = logging.getLogger(__name__)


class CDPLoginUnavailable(RuntimeError):
    """Raised when Chrome or the optional CDP runtime is unavailable."""


def find_chrome_executable() -> str | None:
    """Locate a Chrome/Chromium executable on supported desktop platforms."""
    system = platform.system()
    candidates: list[str] = []
    if system == "Windows":
        for base in (
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        ):
            if base:
                candidates.append(ntpath.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        edge_base = os.environ.get("PROGRAMFILES(X86)") or os.environ.get("PROGRAMFILES")
        if edge_base:
            candidates.append(ntpath.join(edge_base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif system == "Darwin":
        candidates.extend((
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ))
    else:
        candidates.extend((
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")


def _version_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/json/version"


def is_cdp_ready(port: int = DEFAULT_CDP_PORT) -> bool:
    """Return whether a local CDP endpoint is reachable."""
    try:
        response = httpx.get(_version_url(port), timeout=1.5)
        return response.status_code == 200 and bool(response.json().get("webSocketDebuggerUrl"))
    except (httpx.HTTPError, ValueError):
        return False


def launch_cdp_chrome(
    *,
    port: int = DEFAULT_CDP_PORT,
    profile_dir: Path = CDP_PROFILE_DIR,
) -> subprocess.Popen[Any] | None:
    """Start a visible Chrome using a persistent, isolated profile.

    If the requested endpoint already exists, it is reused and no process is
    started.  The profile is never populated from the user's main Chrome data.
    """
    if is_cdp_ready(port):
        return None

    executable = find_chrome_executable()
    if not executable:
        raise CDPLoginUnavailable("未找到 Chrome/Chromium，请先安装浏览器或改用普通 boss login")

    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        executable,
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=http://127.0.0.1",
        f"{BASE_URL}/web/user/",
    ]
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if is_cdp_ready(port):
            return process
        time.sleep(0.5)
    raise CDPLoginUnavailable(f"Chrome 已启动，但 CDP 端口 {port} 在 30 秒内未就绪")


class _CDPConnection:
    def __init__(self, port: int):
        try:
            import websocket
        except ImportError as exc:
            raise CDPLoginUnavailable(
                "CDP 登录依赖 websocket-client；请安装 kabi-boss-cli[cdp]"
            ) from exc

        try:
            data = httpx.get(_version_url(port), timeout=5).json()
            ws_url = data["webSocketDebuggerUrl"]
            self._ws = websocket.create_connection(
                ws_url,
                timeout=10,
                origin="http://127.0.0.1",
            )
        except Exception as exc:
            raise CDPLoginUnavailable(f"无法连接本机 Chrome CDP 端口 {port}: {exc}") from exc
        self._message_id = 0

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._message_id += 1
        message_id = self._message_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = json.loads(self._ws.recv())
            if response.get("id") != message_id:
                continue
            if "error" in response:
                raise CDPLoginUnavailable(f"CDP {method} 失败: {response['error']}")
            return response.get("result", {})
        raise CDPLoginUnavailable(f"CDP {method} 响应超时")

    def close(self) -> None:
        self._ws.close()


def extract_cdp_credential(port: int = DEFAULT_CDP_PORT) -> Credential | None:
    """Export the live zhipin.com cookie jar from a running Chrome."""
    if not is_cdp_ready(port):
        return None
    connection = _CDPConnection(port)
    try:
        entries = connection.send("Storage.getCookies").get("cookies", [])
    finally:
        connection.close()

    cookies: dict[str, str] = {}
    for entry in entries:
        domain = str(entry.get("domain") or "")
        name = entry.get("name")
        value = entry.get("value")
        if is_zhipin_cookie_domain(domain) and isinstance(name, str) and isinstance(value, str) and value:
            cookies[name] = value
    return Credential(cookies) if cookies else None


def cdp_login(
    *,
    port: int = DEFAULT_CDP_PORT,
    timeout: int = 300,
    profile_dir: Path = CDP_PROFILE_DIR,
) -> Credential:
    """Open/reuse persistent Chrome and wait for a complete BOSS cookie set."""
    launch_cdp_chrome(port=port, profile_dir=profile_dir)
    deadline = time.monotonic() + timeout
    last_missing = sorted(REQUIRED_COOKIES)
    while time.monotonic() <= deadline:
        credential = extract_cdp_credential(port)
        if credential:
            last_missing = credential.missing_required_cookies
            if credential.has_required_cookies:
                save_credential(credential)
                return credential
        time.sleep(2)
    missing = ", ".join(last_missing) if last_missing else "未知"
    raise RuntimeError(f"等待浏览器登录超时（{timeout}s），仍缺少 Cookie: {missing}")