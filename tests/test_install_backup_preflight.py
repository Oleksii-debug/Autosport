"""Plan6 deterministic read-only source-contract fixtures; NO install/build/restore."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from autosport.install_backup_preflight import (
    InstallPreflightError,
    require_non_downgrade_transition,
    verify_backup_preflight,
)

PREVIOUS_SHA = "a" * 40
NEW_SHA = "b" * 40


def _fixture(tmp_path):
    live = tmp_path / "поточні дані — Autosport"
    backup = tmp_path / "попередня версія — резервна копія"
    data = {
        "agent memory/memory.json": b'{"sequence":2,"memory":["old"]}',
        "campaigns/campaign IDs.json": b'{"ids":["c-001","c-002"]}',
        "economics/decisions.jsonl": b'{"event":"old"}\n',
        "settings/параметри.json": b'{"currency":"UAH"}',
        "diagnostics/last-state.json": b'{"status":"clean"}',
    }
    for root in (live, backup):
        for name, value in data.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
    manifest = {k: hashlib.sha256(v).hexdigest() for k,v in data.items()}
    return live, backup, manifest, data


def _digest_manifest(manifest):
    payload = "".join(f"{key}\0{manifest[key]}\n" for key in sorted(manifest)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _verify(live, backup, manifest, backup_manifest=None, pinned=None):
    return verify_backup_preflight(
        live, backup,
        live_manifest_files=manifest,
        backup_manifest_files=backup_manifest if backup_manifest is not None else manifest,
        previous_source_sha=PREVIOUS_SHA,
        previous_version="0.1.0-prehuman",
        previous_schema=1,
        trusted_inventory_sha256=pinned if pinned is not None else _digest_manifest(manifest),
    )


def test_exact_live_to_backup_readback_unicode_without_mutation(tmp_path):
    live, backup, manifest, original = _fixture(tmp_path)
    report = _verify(live, backup, manifest)
    assert report.file_count == 5
    assert report.previous_source_sha == PREVIOUS_SHA
    assert report.previous_schema == 1
    assert report.total_bytes == sum(map(len, original.values()))
    for root in (live, backup):
        assert {k:(root / k).read_bytes() for k in original} == original
    # Readback must not leak user values, secret material or full paths.
    assert "memory" not in repr(report)
    assert str(tmp_path) not in repr(report)


@pytest.mark.parametrize("damage", ["missing", "torn", "changed", "extra", "live_changed", "backup_manifest_short", "hash_tampered", "interrupted_tmp"])
def test_partial_or_corrupt_backup_refuses_without_live_mutation(tmp_path, damage):
    live, backup, manifest, original = _fixture(tmp_path)
    target = backup / "agent memory/memory.json"
    if damage == "missing": target.unlink()
    elif damage == "torn": target.write_bytes(original["agent memory/memory.json"][:4])
    elif damage == "changed": target.write_bytes(b"invalid")
    elif damage == "extra": (backup / "unexpected.dat").write_bytes(b"foreign")
    elif damage == "live_changed": (live / "settings/параметри.json").write_bytes(b"changed")
    elif damage == "interrupted_tmp": (backup / "settings/.incomplete.tmp").write_bytes(b"half")
    elif damage == "hash_tampered": manifest["agent memory/memory.json"] = "0" * 64
    backup_manifest = dict(manifest)
    if damage == "backup_manifest_short": backup_manifest.pop("campaigns/campaign IDs.json")
    original_live = {p.relative_to(live).as_posix():p.read_bytes() for p in live.rglob("*") if p.is_file()}
    with pytest.raises(InstallPreflightError): _verify(live, backup, manifest, backup_manifest)
    assert original_live == {p.relative_to(live).as_posix():p.read_bytes() for p in live.rglob("*") if p.is_file()}


@pytest.mark.parametrize("unsafe", ["../secret.json", "C:/private/file", "folder/../../escape", "x\\y.txt", "foo//bar", "a/./b", "a./x", "a/x ", "/root/file"])
def test_hostile_manifest_names_are_not_interpreted_as_paths(tmp_path, unsafe):
    live, backup, manifest, _ = _fixture(tmp_path)
    manifest[unsafe] = "f" * 64
    with pytest.raises(InstallPreflightError): _verify(live, backup, manifest)


def test_casefold_collision_fails_closed(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    manifest["Settings/ПАРАМЕТРИ.json"] = "f" * 64
    with pytest.raises(InstallPreflightError): _verify(live, backup, manifest)


def test_backup_symlink_escape_rejected_before_read(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    target = backup / "settings/параметри.json"
    target.unlink()
    target.symlink_to(live / "settings/параметри.json")
    with pytest.raises(InstallPreflightError): _verify(live, backup, manifest)


def test_workspace_and_backup_must_not_overlap(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    with pytest.raises(InstallPreflightError): _verify(live, live, manifest)
    with pytest.raises(InstallPreflightError): _verify(live, live / "settings", manifest)


def test_hardlinked_snapshot_member_rejected(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    extra = tmp_path / "hardlink"
    os.link(backup / "settings/параметри.json", extra)
    with pytest.raises(InstallPreflightError): _verify(live, backup, manifest)


def test_foreign_manifest_or_missing_backup_root_fails(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    with pytest.raises(InstallPreflightError): _verify(live, tmp_path / "missing", manifest)
    with pytest.raises(InstallPreflightError): _verify(live, backup, manifest, {**manifest,"foreign": "f"*64})


def test_update_matrix_and_previous_identity_preserved():
    allowed = frozenset({(1,2)})
    basic = dict(installed_version="0.1.0-prehuman", installed_source_sha=PREVIOUS_SHA,
                 installed_schema=1, approved_schema_edges=allowed)
    require_non_downgrade_transition(**basic, target_version="0.2.0-prehuman",
                                     target_source_sha=NEW_SHA,target_schema=2)
    for replacement in (
        dict(target_version="0.0.9-prehuman",target_source_sha=NEW_SHA,target_schema=1),
        dict(target_version="0.2.0-prehuman",target_source_sha=NEW_SHA,target_schema=0),
        dict(target_version="0.2.0-prehuman",target_source_sha=NEW_SHA,target_schema=3),
        dict(target_version="0.1.0-prehuman",target_source_sha=NEW_SHA,target_schema=1),
        dict(target_version="0.1.0-other",target_source_sha=NEW_SHA,target_schema=1),
    ):
        with pytest.raises(InstallPreflightError): require_non_downgrade_transition(**basic,**replacement)


def test_schema_version_boolean_not_accepted():
    with pytest.raises(InstallPreflightError):
        require_non_downgrade_transition(installed_version="0.1.0",target_version="0.2.0",
            installed_source_sha=PREVIOUS_SHA,target_source_sha=NEW_SHA,
            installed_schema=True,target_schema=2,approved_schema_edges=frozenset({(1,2)}))


def test_shared_manifest_and_bytes_replacement_rejected_by_trusted_inventory(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    pinned = _digest_manifest(manifest)
    victim = "campaigns/campaign IDs.json"
    # Attacker changes both source and backup to fool same-filesystem readback.
    for root in (live, backup):
        (root / victim).write_bytes(b"replaced")
    manifest[victim] = hashlib.sha256(b"replaced").hexdigest()
    with pytest.raises(InstallPreflightError, match="trusted authority"):
        _verify(live, backup, manifest, pinned=pinned)


def test_unreadable_backup_file_fails_closed(tmp_path, monkeypatch):
    live, backup, manifest, _ = _fixture(tmp_path)
    real_open = os.open
    def deny(path, flags, *args, **kwargs):
        if Path(path).is_relative_to(backup):
            raise PermissionError("locked by another process: DO_NOT_LEAK_SECRET")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(InstallPreflightError) as caught:
        _verify(live, backup, manifest)
    assert "DO_NOT_LEAK_SECRET" not in str(caught.value)


def test_interrupted_read_fails_closed(tmp_path, monkeypatch):
    live, backup, manifest, _ = _fixture(tmp_path)
    def full(_fd, _size):
        raise OSError(28, "disk full: PRIVATE_MARKER")
    monkeypatch.setattr(os, "read", full)
    with pytest.raises(InstallPreflightError) as caught:
        _verify(live, backup, manifest)
    assert "PRIVATE_MARKER" not in str(caught.value)


def _verify_rollback(live, backup, manifest, pinned=None):
    from autosport.install_backup_preflight import verify_rollback_snapshot_preflight
    return verify_rollback_snapshot_preflight(
        live, backup,
        trusted_backup_manifest_files=manifest,
        previous_source_sha=PREVIOUS_SHA,
        previous_version="0.1.0-prehuman",
        previous_schema=1,
        trusted_inventory_sha256=pinned if pinned is not None else _digest_manifest(manifest),
    )


@pytest.mark.parametrize("interruption", ["torn_live", "deleted_live", "new_schema_live", "workspace_missing"])
def test_interrupted_migration_allows_validated_old_backup_without_restore(tmp_path, interruption):
    live, backup, manifest, initial = _fixture(tmp_path)
    before_backup = {key:(backup / key).read_bytes() for key in initial}
    if interruption == "torn_live":
        (live / "agent memory/memory.json").write_bytes(b'{"partial":')
    elif interruption == "deleted_live":
        (live / "settings/параметри.json").unlink()
    elif interruption == "new_schema_live":
        (live / "campaigns/campaign IDs.json").write_bytes(b'{"schema":2,"ids":[]}')
    elif interruption == "workspace_missing":
        import shutil
        shutil.rmtree(live)

    # The independently pinned old snapshot remains valid even when live
    # migration has partly committed; no restoration occurs in preflight.
    if interruption != "workspace_missing":
        with pytest.raises(InstallPreflightError):
            _verify(live, backup, manifest)
    readback = _verify_rollback(live, backup, manifest)
    assert readback.file_count == len(initial)
    assert readback.previous_source_sha == PREVIOUS_SHA
    assert readback.previous_schema == 1
    assert readback.previous_version == "0.1.0-prehuman"
    assert {key:(backup / key).read_bytes() for key in initial} == before_backup
    assert "попередня" not in repr(readback)


@pytest.mark.parametrize("interruption", ["missing", "partial", "foreign", "tamper_both", "old_manifest_mismatch", "symlink"])
def test_interrupted_or_tampered_backup_rejected_even_if_live_broken(tmp_path, interruption):
    live, backup, manifest, initial = _fixture(tmp_path)
    (live / "agent memory/memory.json").write_bytes(b'{"incomplete":')
    pinned = _digest_manifest(manifest)
    backup_file = backup / "agent memory/memory.json"
    if interruption == "missing":
        backup_file.unlink()
    elif interruption == "partial":
        backup_file.write_bytes(b'{"torn":')
    elif interruption == "foreign":
        (backup / "foreign.txt").write_bytes(b"untrusted")
    elif interruption == "tamper_both":
        backup_file.write_bytes(b"attacker old state")
        manifest["agent memory/memory.json"] = hashlib.sha256(b"attacker old state").hexdigest()
    elif interruption == "old_manifest_mismatch":
        manifest["settings/параметри.json"] = "0" * 64
    elif interruption == "symlink":
        backup_file.unlink()
        backup_file.symlink_to(live / "agent memory/memory.json")
    with pytest.raises(InstallPreflightError):
        _verify_rollback(live, backup, manifest, pinned)
    assert (live / "agent memory/memory.json").read_bytes() == b'{"incomplete":'


def test_rollback_does_not_consult_damaged_live_data_contents(tmp_path, monkeypatch):
    live, backup, manifest, _ = _fixture(tmp_path)
    orig_scandir = os.scandir
    def fail_live_scandir(path):
        if Path(path).is_relative_to(live):
            raise AssertionError("rollback preflight must never inspect live state")
        return orig_scandir(path)
    monkeypatch.setattr(os, "scandir", fail_live_scandir)
    assert _verify_rollback(live, backup, manifest).file_count == len(manifest)


def test_rollback_parent_symlink_rejected_without_backup_mutation(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    escape = tmp_path / "other"
    escape.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(escape, target_is_directory=True)
    with pytest.raises(InstallPreflightError):
        _verify_rollback(redirected / "workspace", backup, manifest)


def test_rollback_path_overlap_rejected_even_when_live_root_missing(tmp_path):
    live, backup, manifest, _ = _fixture(tmp_path)
    with pytest.raises(InstallPreflightError):
        _verify_rollback(backup / "deleted-workspace", backup, manifest)


def test_rollback_report_never_exports_secrets_from_unreadable_backup(tmp_path, monkeypatch):
    live, backup, manifest, _ = _fixture(tmp_path)
    real_open = os.open
    def secret_failure(path, flags, *args, **kwargs):
        if Path(path).is_relative_to(backup):
            raise PermissionError("TOP_SECRET_TOKEN_123")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", secret_failure)
    with pytest.raises(InstallPreflightError) as caught:
        _verify_rollback(live, backup, manifest)
    assert "TOP_SECRET_TOKEN_123" not in str(caught.value)
