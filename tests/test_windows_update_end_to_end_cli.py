"""Real Ed25519 + installed trust + snapshot + CLI chain, no installation.

Only the canonical release-package verifier is stubbed because the standalone
A3 source bundle deliberately omits the main repository's release_package.py.
Full-repository CI must separately execute the real ZIP writer/verifier suite.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport import windows_update_cli, windows_update_signature


def _canonical_json(data: dict[str, object]) -> bytes:
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _make_request(tmp_path: Path, *, version: str = "1.2.0", schema: int = 2):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()  # ephemeral, never persisted
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    package = tmp_path / "оновлення з пробілом.zip"
    package.write_bytes(b"test-only immutable package candidate")
    package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
    installed = tmp_path / "встановлена версія.json"
    installed_bytes = _canonical_json({
        "format": "autosport-installed-update-authority-v1",
        "product": "Autosport", "version": "1.1.0", "schema": 1,
        "package_sha256": "a" * 64,
        "trusted_ed25519_public_key_hex": public.hex(),
    })
    installed.write_bytes(installed_bytes)
    source_sha = "b" * 40
    statement = windows_update_signature._signed_statement(
        package_sha256=package_sha, source_sha=source_sha,
        version=version, schema=schema,
    )
    request = {
        "package_path": package.name,
        "expected_package_sha256": package_sha,
        "installed_manifest_path": installed.name,
        "expected_installed_manifest_sha256": hashlib.sha256(installed_bytes).hexdigest(),
        "signature_hex": private.sign(statement).hex(),
        "candidate_version": version,
        "candidate_schema": schema,
        "expected_source_sha": source_sha,
        "supported_schema_migrations": [[1, 2]],
    }
    request_file = tmp_path / "діагностичний запит.json"
    request_file.write_bytes(_canonical_json(request))
    return request_file, request, package, installed


def _attest_embedded_identity(monkeypatch, *, version="1.2.0", schema=2):
    def canonical_verifier(snapshot, *, expected_source_sha):
        snapshot.recheck()
        return {
            "status": "PASS", "package_sha256": snapshot.sha256,
            "source_sha": expected_source_sha,
            "update_version": version, "update_schema": schema,
        }
    monkeypatch.setattr(windows_update_signature, "verify_pinned_release_package", canonical_verifier)


def test_signed_cli_preflight_survives_restart_without_install_authority(tmp_path, monkeypatch):
    request_file, request, package, installed = _make_request(tmp_path)
    _attest_embedded_identity(monkeypatch)
    first_code, first = windows_update_cli.run(request_file)
    second_code, second = windows_update_cli.run(request_file)
    assert first_code == second_code == 0
    assert first == second  # no mutable session state or nonce drift
    assert first["status"] == "DIAGNOSTIC_PASS_UNTRUSTED_ROOT"
    assert first["disposition"] == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert first["transaction_preview"]["migration_steps"] == [
        {"from_schema": 1, "to_schema": 2}
    ]
    assert first["transaction_preview"]["backup_required"] is True
    assert first["install_authorized"] is False
    assert first["independent_trust_root_verified"] is False
    assert request_file.exists() and package.exists() and installed.exists()


@pytest.mark.parametrize("tamper", ["package", "installed", "signature", "embedded", "downgrade"])
def test_signed_cli_rejects_cross_boundary_tampering_without_leaking_paths(
    tmp_path, monkeypatch, tamper,
):
    request_file, request, package, installed = _make_request(tmp_path)
    _attest_embedded_identity(monkeypatch)
    if tamper == "package":
        package.write_bytes(b"replaced package")
    elif tamper == "installed":
        installed.write_bytes(installed.read_bytes().replace(b'"1.1.0"', b'"1.0.0"'))
    elif tamper == "signature":
        request["signature_hex"] = "00" * 64
    elif tamper == "embedded":
        _attest_embedded_identity(monkeypatch, version="1.1.9")
    else:
        request["candidate_version"] = "1.0.0"
    if tamper in ("signature", "downgrade"):
        request_file.write_bytes(_canonical_json(request))
    code, result = windows_update_cli.run(request_file)
    assert code == 2
    assert result == {
        "status": "DIAGNOSTIC_REJECTED",
        "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False,
        "nvda_verified": False,
    }
    assert str(tmp_path) not in json.dumps(result)
    assert package.exists() and installed.exists()
