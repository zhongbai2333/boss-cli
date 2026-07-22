"""Tests for encrypted credential import and export."""

from __future__ import annotations

import json

import pytest

from boss_cli.auth import Credential
from boss_cli.credential_transfer import (
    CredentialTransferError,
    create_credential_package,
    export_credential_file,
    import_credential_file,
    parse_credential_package,
)


@pytest.fixture
def complete_credential() -> Credential:
    return Credential({
        "__zp_stoken__": "secret-stoken",
        "wt2": "secret-wt2",
        "wbg": "secret-wbg",
        "zp_at": "secret-zp-at",
        "extra": "secret-extra",
    })


def test_package_round_trip_hides_cookie_data(complete_credential):
    package = create_credential_package(complete_credential, "correct horse battery staple")
    serialized = json.dumps(package)

    assert "secret-stoken" not in serialized
    assert "__zp_stoken__" not in serialized
    restored = parse_credential_package(package, "correct horse battery staple")
    assert restored.cookies == complete_credential.cookies


def test_wrong_password_is_rejected(complete_credential):
    package = create_credential_package(complete_credential, "correct horse battery staple")

    with pytest.raises(CredentialTransferError, match="密码错误|篡改"):
        parse_credential_package(package, "wrong password long enough")


def test_tampered_metadata_is_rejected(complete_credential):
    package = create_credential_package(complete_credential, "correct horse battery staple")
    package["created_at"] += 1

    with pytest.raises(CredentialTransferError, match="密码错误|篡改"):
        parse_credential_package(package, "correct horse battery staple")


def test_short_password_is_rejected(complete_credential):
    with pytest.raises(CredentialTransferError, match="12"):
        create_credential_package(complete_credential, "too-short")


def test_partial_credential_is_not_exported():
    credential = Credential({"wt2": "only-one"})

    with pytest.raises(CredentialTransferError, match="不完整"):
        create_credential_package(credential, "correct horse battery staple")


def test_file_round_trip_and_overwrite_guard(tmp_path, complete_credential):
    output = tmp_path / "boss-session.bosscred"
    export_credential_file(output, complete_credential, "correct horse battery staple")

    assert output.exists()
    assert import_credential_file(output, "correct horse battery staple").cookies == complete_credential.cookies
    with pytest.raises(CredentialTransferError, match="已存在"):
        export_credential_file(output, complete_credential, "correct horse battery staple")


def test_oversized_file_is_rejected(tmp_path):
    from boss_cli.credential_transfer import MAX_PACKAGE_BYTES

    package = tmp_path / "oversized.bosscred"
    package.write_bytes(b"x" * (MAX_PACKAGE_BYTES + 1))

    with pytest.raises(CredentialTransferError, match="大小无效"):
        import_credential_file(package, "correct horse battery staple")