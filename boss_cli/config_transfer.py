"""Encrypted full-configuration transfer between local and cloud hosts."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values

from .auth import Credential
from .constants import PLUGIN_ENV_FILE
from .credential_transfer import (
    MAX_PACKAGE_BYTES,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    CredentialTransferError,
    _b64decode,
    _b64encode,
    _derive_key,
    _validate_passphrase,
)
from .io_utils import atomic_write_private

CONFIG_PACKAGE_FORMAT = "boss-cli-config"
CONFIG_PACKAGE_VERSION = 2

# Explicitly excludes BOSS_COOKIES and BOSS_CREDENTIAL_PASSPHRASE. Cookies are
# carried as structured encrypted credentials; transfer passwords must remain
# out-of-band.
PORTABLE_CONFIG_KEYS = (
    "MOHRSS_AUTHORIZED",
    "PLUGIN_API_KEY",
    "PLUGIN_ALLOW_ANONYMOUS",
    "PLUGIN_HOST",
    "PLUGIN_PORT",
    "BOSS_PLUGIN_CACHE_TTL_S",
    "BOSS_PLUGIN_MIN_INTERVAL_S",
    "PUBLIC_PLUGIN_CACHE_TTL_S",
    "PUBLIC_PLUGIN_MIN_INTERVAL_S",
    "LLM_SEMANTIC_CACHE_ENABLED",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_SEMANTIC_CONFIDENCE",
    "LLM_SEMANTIC_ALIAS_TTL_S",
    "LLM_TIMEOUT_S",
)


@dataclass(frozen=True)
class ConfigurationBundle:
    """Validated portable configuration decrypted from a package."""

    credential: Credential
    settings: dict[str, str]


def collect_portable_settings(
    *,
    project_env_file: Path | None = None,
    persistent_env_file: Path = PLUGIN_ENV_FILE,
) -> dict[str, str]:
    """Collect whitelisted settings with process environment taking priority."""
    project_file = project_env_file or (Path.cwd() / ".env")
    collected: dict[str, str] = {}
    for path in (project_file, persistent_env_file):
        if path.exists():
            for name, value in dotenv_values(path).items():
                if name in PORTABLE_CONFIG_KEYS and isinstance(value, str):
                    collected[name] = value
    for name in PORTABLE_CONFIG_KEYS:
        value = os.environ.get(name)
        if value is not None:
            collected[name] = value
    return _validate_settings(collected)


def create_config_package(
    credential: Credential,
    settings: dict[str, str],
    passphrase: str,
) -> dict[str, Any]:
    """Create an authenticated encrypted package containing credentials and settings."""
    if not credential.is_valid or not credential.has_required_cookies:
        missing = ", ".join(credential.missing_required_cookies)
        raise CredentialTransferError(f"登录凭证不完整，缺少 Cookie: {missing}")
    validated_settings = _validate_settings(settings)
    password = _validate_passphrase(passphrase)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    package: dict[str, Any] = {
        "format": CONFIG_PACKAGE_FORMAT,
        "version": CONFIG_PACKAGE_VERSION,
        "created_at": int(time.time()),
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "salt": _b64encode(salt),
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
        },
        "nonce": _b64encode(nonce),
    }
    payload = json.dumps(
        {
            "payload_version": 1,
            "credential": {"cookies": credential.cookies},
            "settings": validated_settings,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    key = _derive_key(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    package["ciphertext"] = _b64encode(AESGCM(key).encrypt(nonce, payload, _metadata(package)))
    return package


def parse_config_package(package: dict[str, Any], passphrase: str) -> ConfigurationBundle:
    """Authenticate, decrypt and fully validate a version-2 configuration package."""
    if package.get("format") != CONFIG_PACKAGE_FORMAT or package.get("version") != CONFIG_PACKAGE_VERSION:
        raise CredentialTransferError("不支持的完整配置包格式或版本")
    if package.get("cipher") != "AES-256-GCM" or not isinstance(package.get("created_at"), int):
        raise CredentialTransferError("完整配置包密码学元数据无效")
    kdf = package.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise CredentialTransferError("不支持的完整配置包密钥派生算法")
    if (kdf.get("n"), kdf.get("r"), kdf.get("p")) != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
        raise CredentialTransferError("完整配置包使用了不安全的 Scrypt 参数")

    password = _validate_passphrase(passphrase)
    salt = _b64decode(kdf.get("salt"), "kdf.salt")
    nonce = _b64decode(package.get("nonce"), "nonce")
    ciphertext = _b64decode(package.get("ciphertext"), "ciphertext")
    if len(salt) != 16 or len(nonce) != 12 or not ciphertext:
        raise CredentialTransferError("完整配置包密码学参数长度错误")
    key = _derive_key(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _metadata(package))
        payload = json.loads(plaintext)
    except (InvalidTag, KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialTransferError("密码错误，或完整配置包已被篡改") from exc
    if not isinstance(payload, dict) or payload.get("payload_version") != 1:
        raise CredentialTransferError("完整配置包解密内容版本无效")
    credential_data = payload.get("credential")
    cookies = credential_data.get("cookies") if isinstance(credential_data, dict) else None
    if not isinstance(cookies, dict) or not cookies:
        raise CredentialTransferError("完整配置包中没有有效登录凭证")
    if not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in cookies.items()):
        raise CredentialTransferError("完整配置包中的 Cookie 格式无效")
    credential = Credential(dict(cookies))
    if not credential.has_required_cookies:
        raise CredentialTransferError(f"完整配置包缺少关键 Cookie: {', '.join(credential.missing_required_cookies)}")
    settings = _validate_settings(payload.get("settings"))
    return ConfigurationBundle(credential=credential, settings=settings)


def export_config_file(
    path: Path,
    credential: Credential,
    settings: dict[str, str],
    passphrase: str,
    *,
    overwrite: bool = False,
) -> None:
    """Atomically write an encrypted full-configuration package."""
    path = path.expanduser()
    if path.exists() and not overwrite:
        raise CredentialTransferError(f"目标文件已存在: {path}")
    package = create_config_package(credential, settings, passphrase)
    atomic_write_private(path, json.dumps(package, indent=2, ensure_ascii=False))


def import_config_file(path: Path, passphrase: str) -> ConfigurationBundle:
    """Read a bounded encrypted full-configuration package."""
    package = _read_package(path)
    return parse_config_package(package, passphrase)


def read_package_format(path: Path) -> str:
    """Return the bounded package format without decrypting its payload."""
    package = _read_package(path)
    value = package.get("format")
    return value if isinstance(value, str) else ""


def write_portable_settings(settings: dict[str, str], path: Path = PLUGIN_ENV_FILE) -> None:
    """Replace the managed persistent environment file atomically."""
    validated = _validate_settings(settings)
    lines = ["# Managed by boss config-import. Do not commit this file."]
    lines.extend(f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in sorted(validated.items()))
    atomic_write_private(path, "\n".join(lines) + "\n")


def _validate_settings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise CredentialTransferError("完整配置中的 settings 必须是对象")
    unknown = set(value) - set(PORTABLE_CONFIG_KEYS)
    if unknown:
        raise CredentialTransferError(f"完整配置包含不允许的字段: {', '.join(sorted(unknown))}")
    validated: dict[str, str] = {}
    for name, setting in value.items():
        if not isinstance(name, str) or not isinstance(setting, str):
            raise CredentialTransferError("完整配置字段名称和值必须是字符串")
        if len(setting) > 8192 or "\x00" in setting or "\r" in setting or "\n" in setting:
            raise CredentialTransferError(f"完整配置字段 {name} 的值无效")
        validated[name] = setting
    return validated


def _metadata(package: dict[str, Any]) -> bytes:
    return json.dumps(
        {name: package[name] for name in ("format", "version", "created_at", "cipher", "kdf")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _read_package(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CredentialTransferError(f"无法读取配置包: {path}") from exc
    if size <= 0 or size > MAX_PACKAGE_BYTES:
        raise CredentialTransferError(f"配置包大小无效（最大 {MAX_PACKAGE_BYTES} 字节）")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialTransferError("配置包不是有效的 UTF-8 JSON 文件") from exc
    if not isinstance(value, dict):
        raise CredentialTransferError("配置包根节点必须是 JSON 对象")
    return value