from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.windows_launch_identity import (
    InstanceDecision,
    LaunchIdentityError,
    LaunchOwnerIdentity,
    decide_single_instance,
    owner_identity_for_process,
    parse_owner_identity,
    resolve_active_generation,
    validate_stable_launch_surface,
)


def _manifest(tmp_path: Path, *, generation: int = 7, user_scope: str = "user-A"):
    exe = tmp_path / "versions" / str(generation) / "Autosport.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"autosport-binary-v7")
    payload = {
        "schema_version": 1,
        "product": "autosport",
        "generation": generation,
        "version": "7.0.0",
        "executable_relpath": f"versions/{generation}/Autosport.exe",
        "executable_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "user_scope": user_scope,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return payload, raw, hashlib.sha256(raw).hexdigest(), exe


def _resolve(tmp_path: Path):
    _, raw, manifest_sha, _ = _manifest(tmp_path)
    return resolve_active_generation(
        manifest_bytes=raw,
        expected_manifest_sha256=manifest_sha,
        install_root=tmp_path,
        expected_user_scope="user-A",
    )


def _owner(*, generation: int = 4, nonce: str = "a" * 64) -> LaunchOwnerIdentity:
    return parse_owner_identity(
        {
            "schema_version": 1,
            "pid": 1234,
            "process_start_nonce": nonce,
            "user_scope": "user-A",
            "generation": generation,
            "version": f"{generation}.0.0",
            "executable_sha256": "b" * 64,
        }
    )


def test_resolves_integrity_bound_active_generation(tmp_path: Path):
    resolved = _resolve(tmp_path)
    assert resolved.generation == 7
    assert resolved.version == "7.0.0"
    assert resolved.executable_path == tmp_path / "versions" / "7" / "Autosport.exe"


def test_manifest_digest_tampering_fails_closed(tmp_path: Path):
    _, raw, _, _ = _manifest(tmp_path)
    with pytest.raises(LaunchIdentityError, match="manifest digest mismatch"):
        resolve_active_generation(
            manifest_bytes=raw + b" ",
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            install_root=tmp_path,
            expected_user_scope="user-A",
        )


def test_manifest_extra_field_is_rejected(tmp_path: Path):
    payload, _, _, _ = _manifest(tmp_path)
    payload["caller_claim"] = True
    raw = json.dumps(payload).encode()
    with pytest.raises(LaunchIdentityError, match="schema mismatch"):
        resolve_active_generation(
            manifest_bytes=raw,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            install_root=tmp_path,
            expected_user_scope="user-A",
        )


def test_generation_bool_is_not_accepted_as_integer(tmp_path: Path):
    payload, _, _, _ = _manifest(tmp_path)
    payload["generation"] = True
    raw = json.dumps(payload).encode()
    with pytest.raises(LaunchIdentityError, match="generation"):
        resolve_active_generation(
            manifest_bytes=raw,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            install_root=tmp_path,
            expected_user_scope="user-A",
        )


def test_path_escape_is_rejected(tmp_path: Path):
    payload, _, _, _ = _manifest(tmp_path)
    payload["executable_relpath"] = r"..\Autosport.exe"
    raw = json.dumps(payload).encode()
    with pytest.raises(LaunchIdentityError, match="unsafe path component"):
        resolve_active_generation(
            manifest_bytes=raw,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            install_root=tmp_path,
            expected_user_scope="user-A",
        )


def test_absolute_executable_path_is_rejected(tmp_path: Path):
    payload, _, _, _ = _manifest(tmp_path)
    payload["executable_relpath"] = r"C:\Autosport\Autosport.exe"
    raw = json.dumps(payload).encode()
    with pytest.raises(LaunchIdentityError, match="relative"):
        resolve_active_generation(
            manifest_bytes=raw,
            expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            install_root=tmp_path,
            expected_user_scope="user-A",
        )


def test_binary_digest_mismatch_is_rejected(tmp_path: Path):
    _, raw, manifest_sha, exe = _manifest(tmp_path)
    exe.write_bytes(b"tampered")
    with pytest.raises(LaunchIdentityError, match="executable digest mismatch"):
        resolve_active_generation(
            manifest_bytes=raw,
            expected_manifest_sha256=manifest_sha,
            install_root=tmp_path,
            expected_user_scope="user-A",
        )


def test_manifest_user_scope_mismatch_is_rejected(tmp_path: Path):
    _, raw, manifest_sha, _ = _manifest(tmp_path)
    with pytest.raises(LaunchIdentityError, match="user scope mismatch"):
        resolve_active_generation(
            manifest_bytes=raw,
            expected_manifest_sha256=manifest_sha,
            install_root=tmp_path,
            expected_user_scope="user-B",
        )


def test_stable_shortcut_target_allows_windows_case_variation():
    validate_stable_launch_surface(
        shortcut_target=r"C:\Program Files\Autosport\AutosportLauncher.exe",
        stable_launcher_path=r"c:\program files\autosport\autosportlauncher.exe",
    )


def test_versioned_shortcut_target_is_rejected():
    with pytest.raises(LaunchIdentityError, match="stable launcher"):
        validate_stable_launch_surface(
            shortcut_target=r"C:\Program Files\Autosport\versions\7\Autosport.exe",
            stable_launcher_path=r"C:\Program Files\Autosport\AutosportLauncher.exe",
        )


def test_missing_owner_allows_acquire():
    assert (
        decide_single_instance(
            existing_owner=None,
            requested_user_scope="user-A",
            observe_process_start_nonce=lambda _: None,
        )
        is InstanceDecision.ACQUIRE
    )


def test_live_old_generation_blocks_after_active_generation_changes():
    old_owner = _owner(generation=4)
    assert (
        decide_single_instance(
            existing_owner=old_owner,
            requested_user_scope="user-A",
            observe_process_start_nonce=lambda pid: "a" * 64,
        )
        is InstanceDecision.BLOCK_ACTIVE
    )


def test_dead_owner_is_reclaimable():
    assert (
        decide_single_instance(
            existing_owner=_owner(),
            requested_user_scope="user-A",
            observe_process_start_nonce=lambda pid: None,
        )
        is InstanceDecision.RECLAIM_STALE
    )


def test_pid_reuse_with_different_process_start_nonce_is_reclaimable():
    assert (
        decide_single_instance(
            existing_owner=_owner(nonce="a" * 64),
            requested_user_scope="user-A",
            observe_process_start_nonce=lambda pid: "c" * 64,
        )
        is InstanceDecision.RECLAIM_STALE
    )


def test_malformed_observed_process_identity_fails_closed():
    with pytest.raises(LaunchIdentityError, match="observed_process_start_nonce"):
        decide_single_instance(
            existing_owner=_owner(),
            requested_user_scope="user-A",
            observe_process_start_nonce=lambda pid: "pid-only",
        )


def test_foreign_user_owner_does_not_authorize_reclaim():
    with pytest.raises(LaunchIdentityError, match="user scope mismatch"):
        decide_single_instance(
            existing_owner=_owner(),
            requested_user_scope="user-B",
            observe_process_start_nonce=lambda pid: None,
        )


def test_pid_only_owner_record_is_rejected():
    payload = {
        "schema_version": 1,
        "pid": 1234,
        "user_scope": "user-A",
        "generation": 1,
        "version": "1.0",
        "executable_sha256": "b" * 64,
    }
    with pytest.raises(LaunchIdentityError, match="schema mismatch"):
        parse_owner_identity(payload)


def test_owner_identity_binds_resolved_generation(tmp_path: Path):
    resolved = _resolve(tmp_path)
    owner = owner_identity_for_process(
        pid=77,
        process_start_nonce="d" * 64,
        resolved=resolved,
    )
    assert owner.pid == 77
    assert owner.generation == resolved.generation
    assert owner.version == resolved.version
    assert owner.executable_sha256 == resolved.executable_sha256
