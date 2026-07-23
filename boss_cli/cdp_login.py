"""Persistent Chrome login through the Chrome DevTools Protocol.

This module keeps authentication inside a real, isolated Chrome profile and
exports only zhipin.com cookies for the existing HTTP client.  It is optional:
normal browser-cookie3 and QR login flows continue to work without the CDP
dependencies installed.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import ntpath
import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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


def _cdp_endpoint(port: int = DEFAULT_CDP_PORT, endpoint: str | None = None) -> str:
    raw = (endpoint or os.environ.get("BOSS_CDP_ENDPOINT") or f"http://127.0.0.1:{port}").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise CDPLoginUnavailable("CDP endpoint 必须是无凭据的内网 HTTP 地址")
    if parsed.path or parsed.query or parsed.fragment:
        raise CDPLoginUnavailable("CDP endpoint 不得包含路径、查询参数或片段")
    hostname = parsed.hostname.lower()
    allowed_names = {"localhost", "chromium"}
    try:
        address = ipaddress.ip_address(hostname)
        allowed = address.is_loopback or address.is_private
    except ValueError:
        allowed = hostname in allowed_names
    if not allowed:
        raise CDPLoginUnavailable("拒绝连接公网或未授权的 CDP 主机")
    return raw


def _version_url(port: int = DEFAULT_CDP_PORT, endpoint: str | None = None) -> str:
    return f"{_transport_endpoint(port, endpoint)}/json/version"


def _transport_endpoint(port: int = DEFAULT_CDP_PORT, endpoint: str | None = None) -> str:
    """Resolve the trusted sidecar name so Chromium receives an IP Host header."""
    trusted = urlsplit(_cdp_endpoint(port, endpoint))
    hostname = trusted.hostname or ""
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname == "localhost":
            resolved = "127.0.0.1"
        else:
            try:
                resolved = socket.gethostbyname(hostname)
            except OSError as exc:
                raise CDPLoginUnavailable("无法解析 CDP 内网主机") from exc
        address = ipaddress.ip_address(resolved)
    if not (address.is_loopback or address.is_private):
        raise CDPLoginUnavailable("CDP 服务名解析到了非私网地址")
    netloc = f"[{address}]" if address.version == 6 else str(address)
    if trusted.port:
        netloc = f"{netloc}:{trusted.port}"
    return urlunsplit((trusted.scheme, netloc, "", "", ""))


def is_cdp_ready(port: int = DEFAULT_CDP_PORT, *, endpoint: str | None = None) -> bool:
    """Return whether a trusted local/private CDP endpoint is reachable."""
    try:
        response = httpx.get(_version_url(port, endpoint), timeout=1.5)
        return response.status_code == 200 and bool(response.json().get("webSocketDebuggerUrl"))
    except (CDPLoginUnavailable, httpx.HTTPError, ValueError):
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
    def __init__(self, port: int = DEFAULT_CDP_PORT, *, endpoint: str | None = None):
        try:
            import websocket
        except ImportError as exc:
            raise CDPLoginUnavailable(
                "CDP 登录依赖 websocket-client；请安装 kabi-boss-cli[cdp]"
            ) from exc

        try:
            trusted_endpoint = urlsplit(_cdp_endpoint(port, endpoint))
            transport_endpoint = urlsplit(_transport_endpoint(port, endpoint))
            data = httpx.get(_version_url(port, endpoint), timeout=5).json()
            advertised = urlsplit(data["webSocketDebuggerUrl"])
            if advertised.scheme not in {"ws", "wss"} or not advertised.path.startswith("/devtools/browser/"):
                raise ValueError("invalid browser WebSocket URL")
            ws_scheme = "wss" if trusted_endpoint.scheme == "https" else "ws"
            ws_url = urlunsplit((ws_scheme, transport_endpoint.netloc, advertised.path, advertised.query, ""))
            origin_host = trusted_endpoint.hostname or "127.0.0.1"
            origin = f"http://{origin_host}"
            self._ws = websocket.create_connection(
                ws_url,
                timeout=10,
                origin=origin,
            )
        except Exception as exc:
            raise CDPLoginUnavailable(f"无法连接 Chrome CDP: {type(exc).__name__}") from exc
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


def _credential_from_entries(entries: list[dict[str, Any]]) -> Credential | None:
    cookies: dict[str, str] = {}
    for entry in entries:
        domain = str(entry.get("domain") or "")
        name = entry.get("name")
        value = entry.get("value")
        if is_zhipin_cookie_domain(domain) and isinstance(name, str) and isinstance(value, str) and value:
            cookies[name] = value
    return Credential(cookies) if cookies else None


def extract_cdp_credential(
    port: int = DEFAULT_CDP_PORT,
    *,
    endpoint: str | None = None,
) -> Credential | None:
    """Export the live zhipin.com cookie jar from a running Chrome."""
    if not is_cdp_ready(port, endpoint=endpoint):
        return None
    connection = _CDPConnection(port, endpoint=endpoint)
    try:
        entries = connection.send("Storage.getCookies").get("cookies", [])
    finally:
        connection.close()
    return _credential_from_entries(entries)


def refresh_cdp_credential(
    credential: Credential,
    *,
    endpoint: str | None = None,
    timeout: float = 20.0,
) -> Credential | None:
    """Inject a session into a private Chromium and let page JS replace stoken."""
    if not is_cdp_ready(endpoint=endpoint):
        return None
    connection = _CDPConnection(endpoint=endpoint)
    target_id: str | None = None
    try:
        connection.send("Storage.clearCookies")
        injected = [
            {"name": name, "value": value, "url": f"{BASE_URL}/"}
            for name, value in credential.cookies.items()
            if name != "__zp_stoken__" and value
        ]
        if not injected:
            return None
        connection.send("Storage.setCookies", {"cookies": injected})
        target_id = connection.send("Target.createTarget", {"url": f"{BASE_URL}/web/user/"}).get("targetId")

        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            entries = connection.send("Storage.getCookies").get("cookies", [])
            refreshed = _credential_from_entries(entries)
            if refreshed and refreshed.has_required_cookies:
                return refreshed
            time.sleep(1)
        return None
    finally:
        if target_id:
            try:
                connection.send("Target.closeTarget", {"targetId": target_id})
            except CDPLoginUnavailable:
                logger.debug("Unable to close temporary CDP target")
        connection.close()


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