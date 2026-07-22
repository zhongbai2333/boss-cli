"""Encrypted credential packages for moving a BOSS session between hosts."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .auth import Credential

PACKAGE_FORMAT = "boss-cli-credential"
PACKAGE_VERSION = 1
MAX_PACKAGE_BYTES = 1024 * 1024
MIN_PASSPHRASE_LENGTH = 12
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1


class CredentialTransferError(ValueError):
    """Raised when an encrypted credential package is invalid or unsafe."""


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise CredentialTransferError(f"凭证包字段 {field} 格式错误")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise CredentialTransferError(f"凭证包字段 {field} 不是有效 Base64") from exc


def _validate_passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise CredentialTransferError(f"导出密码至少需要 {MIN_PASSPHRASE_LENGTH} 个字符")
    return passphrase.encode("utf-8")


def _derive_key(passphrase: bytes, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase)


def _authenticated_metadata(package: dict[str, Any]) -> bytes:
    metadata = {
        "format": package["format"],
        "version": package["version"],
        "created_at": package["created_at"],
        "cipher": package["cipher"],
        "kdf": package["kdf"],
    }
    return json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")


def create_credential_package(credential: Credential, passphrase: str) -> dict[str, Any]:
    """Create an authenticated encrypted package without exposing cookie names."""
    if not credential.is_valid:
        raise CredentialTransferError("没有可导出的登录凭证")
    if not credential.has_required_cookies:
        raise CredentialTransferError(
            f"登录凭证不完整，缺少 Cookie: {', '.join(credential.missing_required_cookies)}"
        )

    password = _validate_passphrase(passphrase)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    package: dict[str, Any] = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
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
        {"cookies": credential.cookies},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    key = _derive_key(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    package["ciphertext"] = _b64encode(AESGCM(key).encrypt(nonce, payload, _authenticated_metadata(package)))
    return package


def parse_credential_package(package: dict[str, Any], passphrase: str) -> Credential:
    """Authenticate, decrypt, and validate a credential package."""
    if package.get("format") != PACKAGE_FORMAT or package.get("version") != PACKAGE_VERSION:
        raise CredentialTransferError("不支持的凭证包格式或版本")
    if package.get("cipher") != "AES-256-GCM":
        raise CredentialTransferError("不支持的凭证包加密算法")
    if not isinstance(package.get("created_at"), int):
        raise CredentialTransferError("凭证包缺少有效创建时间")

    kdf = package.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise CredentialTransferError("不支持的凭证包密钥派生算法")
    parameters = (kdf.get("n"), kdf.get("r"), kdf.get("p"))
    if parameters != (SCRYPT_N, SCRYPT_R, SCRYPT_P):
        raise CredentialTransferError("凭证包使用了不受支持或不安全的 Scrypt 参数")

    password = _validate_passphrase(passphrase)
    salt = _b64decode(kdf.get("salt"), "kdf.salt")
    nonce = _b64decode(package.get("nonce"), "nonce")
    ciphertext = _b64decode(package.get("ciphertext"), "ciphertext")
    if len(salt) != 16 or len(nonce) != 12 or not ciphertext:
        raise CredentialTransferError("凭证包密码学参数长度错误")

    key = _derive_key(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _authenticated_metadata(package))
    except (InvalidTag, KeyError, TypeError, ValueError) as exc:
        raise CredentialTransferError("密码错误，或凭证包已被篡改") from exc

    try:
        payload = json.loads(plaintext)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CredentialTransferError("凭证包解密内容无效") from exc
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, dict) or not cookies:
        raise CredentialTransferError("凭证包中没有有效 Cookie")
    if not all(isinstance(name, str) and isinstance(value, str) and name and value for name, value in cookies.items()):
        raise CredentialTransferError("凭证包中的 Cookie 格式无效")

    credential = Credential(cookies=dict(cookies))
    if not credential.has_required_cookies:
        raise CredentialTransferError(
            f"凭证包缺少关键 Cookie: {', '.join(credential.missing_required_cookies)}"
        )
    return credential


def export_credential_file(path: Path, credential: Credential, passphrase: str, *, overwrite: bool = False) -> None:
    """Atomically write an encrypted credential package with private permissions."""
    path = path.expanduser()
    if path.exists() and not overwrite:
        raise CredentialTransferError(f"目标文件已存在: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    package = create_credential_package(credential, passphrase)
    content = json.dumps(package, indent=2, ensure_ascii=False).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def import_credential_file(path: Path, passphrase: str) -> Credential:
    """Read and decrypt a bounded credential package."""
    path = path.expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CredentialTransferError(f"无法读取凭证包: {path}") from exc
    if size <= 0 or size > MAX_PACKAGE_BYTES:
        raise CredentialTransferError(f"凭证包大小无效（最大 {MAX_PACKAGE_BYTES} 字节）")
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialTransferError("凭证包不是有效的 UTF-8 JSON 文件") from exc
    if not isinstance(package, dict):
        raise CredentialTransferError("凭证包根节点必须是 JSON 对象")
    return parse_credential_package(package, passphrase)