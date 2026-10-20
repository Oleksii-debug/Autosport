"""Run in full Autosport repository after release_package integration patch.

These tests deliberately exercise the *real* canonical ZIP writer/verifier,
not a mocked package report. In the standalone A3 checkpoint, that existing
module is absent; full-repository CI must execute this suite before merge.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

release_package = pytest.importorskip("autosport.release_package")
from autosport.windows_update_snapshot import (  # noqa: E402
    pinned_windows_update_package,
    verify_pinned_release_package,
)

SOURCE_SHA = "a" * 40


def _fixture(tmp_path: Path) -> dict[str, Path]:
    exe = tmp_path / "Autosport.exe"
    exe.write_bytes(b"test-executable")
    start = tmp_path / "START.txt"
    start.write_text("start\n", encoding="utf-8")
    examples = tmp_path / "example"
    examples.mkdir()
    (examples / "demo.jsonl").write_text("{}\n", encoding="utf-8")
    common = {
        "status": "PASS", "real_money_execution": False,
        "human_tested": False, "nvda_verified": False,
    }
    paths = {}
    for label in ("diagnostic", "accessibility", "keyboard", "restart"):
        path = tmp_path / f"{label}.json"
        payload = dict(common)
        if label == "restart":
            payload.update({
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS",
                "process_kill_stage_pid": 101,
                "process_kill_return_code": -15,
                "process_recovery_pid": 202,
                "process_recovery_run_id": "test-recovery",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_base_decision_ledger_sha256": "2" * 64,
                "process_recovery_new_paper_book_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            })
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        paths[label] = path
    return {"exe": exe, "start": start, "examples": examples, **paths}


def _build(tmp_path: Path, **options):
    f = _fixture(tmp_path)
    return release_package.build_windows_package(
        f["exe"], f["start"], f["examples"],
        f["diagnostic"], f["accessibility"], f["keyboard"],
        f["restart"], tmp_path / "candidate.zip", SOURCE_SHA, **options,
    )[0]


def test_legacy_package_remains_verifiable_but_not_updatable(tmp_path):
    package = _build(tmp_path)
    report = release_package.verify_windows_package(package, expected_source_sha=SOURCE_SHA)
    assert report["status"] == "PASS"
    with pinned_windows_update_package(
        package, expected_sha256=hashlib.sha256(package.read_bytes()).hexdigest()
    ) as snapshot:
        attested = verify_pinned_release_package(snapshot, expected_source_sha=SOURCE_SHA)
        assert "update_version" not in attested
        assert "update_schema" not in attested


def test_real_canonical_builder_and_verifier_attest_explicit_update_identity(tmp_path):
    package = _build(tmp_path, update_version="1.2.3", update_schema=2)
    report = release_package.verify_windows_package(package, expected_source_sha=SOURCE_SHA)
    assert report["status"] == "PASS"
    with pinned_windows_update_package(
        package, expected_sha256=hashlib.sha256(package.read_bytes()).hexdigest()
    ) as snapshot:
        attested = verify_pinned_release_package(snapshot, expected_source_sha=SOURCE_SHA)
        assert attested["update_version"] == "1.2.3"
        assert attested["update_schema"] == 2
        assert attested["package_sha256"] == snapshot.sha256


@pytest.mark.parametrize("options", [
    {"update_version": "1.2.3"},
    {"update_schema": 2},
    {"update_version": "01.2.3", "update_schema": 2},
    {"update_version": "1.2.3", "update_schema": True},
])
def test_release_writer_rejects_invalid_update_metadata(tmp_path, options):
    with pytest.raises(ValueError):
        _build(tmp_path, **options)


def test_invalid_update_request_does_not_delete_existing_release(tmp_path):
    existing = tmp_path / "Autosport-V1"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("previous release", encoding="utf-8")
    with pytest.raises(ValueError):
        _build(tmp_path, update_version="1.2.3", update_schema=True)
    assert sentinel.read_text(encoding="utf-8") == "previous release"


def _signed_real_release_case(
    tmp_path: Path, *, candidate_version: str = "1.2.3",
    candidate_schema: int = 2, installed_version: str = "1.1.0",
    installed_schema: int = 1, same_package: bool = False,
):
    """Build actual release ZIP, installed trust anchor and detached signature.

    No signer material is persisted; the private key lives only in test memory.
    Unlike the standalone CLI fixture this does not replace the real package
    verifier. The resulting request is operator-controlled and non-authorizing.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from autosport.windows_update_signature import _signed_statement

    package = _build(
        tmp_path, update_version=candidate_version, update_schema=candidate_schema
    )
    package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    installed = tmp_path / "встановлений стан.json"
    installed_bytes = (
        json.dumps({
            "format": "autosport-installed-update-authority-v1",
            "product": "Autosport",
            "version": candidate_version if same_package else installed_version,
            "schema": candidate_schema if same_package else installed_schema,
            "package_sha256": package_digest if same_package else "b" * 64,
            "trusted_ed25519_public_key_hex": public.hex(),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")
    installed.write_bytes(installed_bytes)
    signature = private.sign(_signed_statement(
        package_sha256=package_digest, source_sha=SOURCE_SHA,
        version=candidate_version, schema=candidate_schema,
    ))
    migrations = () if same_package or candidate_schema == installed_schema else (
        tuple((n, n + 1) for n in range(installed_schema, candidate_schema))
    )
    args = {
        "package_path": package,
        "expected_package_sha256": package_digest,
        "installed_manifest_path": installed,
        "expected_installed_manifest_sha256": hashlib.sha256(installed_bytes).hexdigest(),
        "signature": signature,
        "candidate_version": candidate_version,
        "candidate_schema": candidate_schema,
        "expected_source_sha": SOURCE_SHA,
        "supported_schema_migrations": migrations,
    }
    return args, package, installed, private


def test_real_release_signed_update_preflight_preview_and_cli_restart(tmp_path):
    """No verifier stubs: package -> Ed25519 -> installed trust -> preview -> CLI."""
    from autosport.windows_update_preflight import preflight_signed_windows_update
    from autosport.windows_update_transaction_plan import preview_windows_update_transaction
    from autosport.windows_update_cli import run

    args, package, installed, _private = _signed_real_release_case(tmp_path)
    receipt = preflight_signed_windows_update(**args)
    assert receipt.install_authorized is False
    assert receipt.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    plan = preview_windows_update_transaction(
        receipt, supported_schema_migrations=args["supported_schema_migrations"]
    )
    assert plan["migration_steps"] == [{"from_schema": 1, "to_schema": 2}]
    assert plan["backup_required"] is True
    assert plan["install_authorized"] is False
    request = {
        "package_path": package.name,
        "expected_package_sha256": args["expected_package_sha256"],
        "installed_manifest_path": installed.name,
        "expected_installed_manifest_sha256": args["expected_installed_manifest_sha256"],
        "signature_hex": args["signature"].hex(),
        "candidate_version": args["candidate_version"],
        "candidate_schema": args["candidate_schema"],
        "expected_source_sha": SOURCE_SHA,
        "supported_schema_migrations": [[1, 2]],
    }
    request_file = tmp_path / "запит на оновлення.json"
    request_file.write_bytes((json.dumps(request, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True) + "\n").encode("ascii"))
    original_package = package.read_bytes()
    original_installed = installed.read_bytes()
    original_request = request_file.read_bytes()
    first = run(request_file)
    second = run(request_file)
    assert first == second
    assert first[0] == 0
    assert first[1]["status"] == "DIAGNOSTIC_PASS_UNTRUSTED_ROOT"
    assert first[1]["independent_trust_root_verified"] is False
    assert first[1]["install_authorized"] is False
    assert first[1]["transaction_preview"] == plan
    assert package.read_bytes() == original_package
    assert installed.read_bytes() == original_installed
    assert request_file.read_bytes() == original_request


def test_real_release_signed_no_change_is_non_authorizing(tmp_path):
    from autosport.windows_update_preflight import preflight_signed_windows_update
    args, package, installed, _private = _signed_real_release_case(
        tmp_path, same_package=True
    )
    receipt = preflight_signed_windows_update(**args)
    assert receipt.decision.disposition == "NO_CHANGE"
    assert receipt.install_authorized is False
    assert package.is_file() and installed.is_file()


@pytest.mark.parametrize("attack", [
    "package_modified", "installed_key_modified", "envelope_relabel",
    "installed_schema_downgrade", "installed_version_downgrade",
])
def test_real_release_signed_update_rejects_cross_boundary_tampering(tmp_path, attack):
    from autosport.windows_update_preflight import (
        WindowsUpdatePreflightError, preflight_signed_windows_update,
    )
    from autosport.windows_update_signature import _signed_statement
    args, package, installed, private = _signed_real_release_case(tmp_path)
    if attack == "package_modified":
        with package.open("ab") as stream:
            stream.write(b"untrusted trailing bytes")
    elif attack == "installed_key_modified":
        raw = installed.read_bytes()
        installed.write_bytes(raw.replace(b'"trusted_ed25519_public_key_hex":"',
                                          b'"trusted_ed25519_public_key_hex":"00'))
    elif attack == "envelope_relabel":
        args["candidate_version"] = "1.2.4"
        args["signature"] = private.sign(_signed_statement(
            package_sha256=args["expected_package_sha256"],
            source_sha=SOURCE_SHA, version="1.2.4", schema=2,
        ))
    elif attack == "installed_schema_downgrade":
        # The installed root is pinned to schema 3, higher than candidate 2.
        # Re-pin the legitimate manifest to isolate the downgrade policy.
        obj = json.loads(installed.read_text(encoding="ascii"))
        obj["schema"] = 3
        raw = (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        installed.write_bytes(raw)
        args["expected_installed_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        obj = json.loads(installed.read_text(encoding="ascii"))
        obj["version"] = "1.9.0"
        raw = (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        installed.write_bytes(raw)
        args["expected_installed_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(WindowsUpdatePreflightError):
        preflight_signed_windows_update(**args)
    assert package.is_file() and installed.is_file()


def test_real_release_without_update_identity_cannot_be_signed_into_update(tmp_path):
    from autosport.windows_update_preflight import (
        WindowsUpdatePreflightError, preflight_signed_windows_update,
    )
    args, _package, _installed, private = _signed_real_release_case(tmp_path)
    legacy = _build(tmp_path)
    args["expected_package_sha256"] = hashlib.sha256(legacy.read_bytes()).hexdigest()
    from autosport.windows_update_signature import _signed_statement
    args["signature"] = private.sign(_signed_statement(
        package_sha256=args["expected_package_sha256"],
        source_sha=SOURCE_SHA, version="1.2.3", schema=2,
    ))
    with pytest.raises(WindowsUpdatePreflightError):
        preflight_signed_windows_update(**args)
