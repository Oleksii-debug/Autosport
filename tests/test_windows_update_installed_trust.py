"""Adversarial/restart checks for installed trust authority (no installer)."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from autosport.windows_update_installed_trust import (
    InstalledWindowsTrustError,
    load_pinned_installed_update_authority,
    assess_update_with_installed_trust,
)


def _bytes(**overrides):
    data = {
        "format": "autosport-installed-update-authority-v1",
        "product": "Autosport", "version": "1.2.3", "schema": 2,
        "package_sha256": "a" * 64,
        "trusted_ed25519_public_key_hex": "01" * 32,
    }
    data.update(overrides)
    return (json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _load(path: Path, raw: bytes):
    path.write_bytes(raw)
    return load_pinned_installed_update_authority(path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())


def test_canonical_pinned_restart_and_key_identity(tmp_path):
    path = tmp_path / "встановлена версія.json"
    raw = _bytes()
    first = _load(path, raw)
    second = load_pinned_installed_update_authority(path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())
    assert first == second
    assert first.installed.version == "1.2.3"
    assert first.installed.schema == 2
    assert first.trusted_public_key == bytes.fromhex("01" * 32)
    assert first.install_authorized is False
    with pytest.raises(InstalledWindowsTrustError):
        replace(first, install_authorized=True)


@pytest.mark.parametrize("overrides", [
    {"product": "Other"}, {"format": "v2"}, {"schema": True},
    {"schema": 0}, {"version": "01.2.3"}, {"package_sha256": "A" * 64},
    {"trusted_ed25519_public_key_hex": "01" * 31},
    {"trusted_ed25519_public_key_hex": "01" * 31 + "GG"},
    {"version": []}, {"schema": "2"},
])
def test_rejects_invalid_pinned_manifest_fields(tmp_path, overrides):
    with pytest.raises(InstalledWindowsTrustError):
        _load(tmp_path / "state.json", _bytes(**overrides))


@pytest.mark.parametrize("raw", [
    b'{"format":"x","format":"y"}\n',
    b'{"product":"Autosport","schema":NaN}\n',
    b'{"product":"Autosport"}',
    _bytes().replace(b'"product":"Autosport"', b'"product": "Autosport"'),
    _bytes().rstrip(b"\n"),
    b"{" + b" " * 8192 + b"}",
    b"[" * 1500 + b"]" * 1500,
])
def test_rejects_noncanonical_or_unbounded_json(tmp_path, raw):
    with pytest.raises(InstalledWindowsTrustError):
        _load(tmp_path / "state.json", raw)


def test_tampering_or_partial_write_fails_trusted_digest(tmp_path):
    path = tmp_path / "state.json"
    original = _bytes()
    expected = hashlib.sha256(original).hexdigest()
    path.write_bytes(original)
    path.write_bytes(_bytes(version="1.2.4"))
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(path, expected_manifest_sha256=expected)
    path.write_bytes(original[:30])
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(path, expected_manifest_sha256=expected)


def test_symlink_and_same_byte_replacement_are_not_authority(tmp_path):
    target = tmp_path / "real.json"
    raw = _bytes()
    target.write_bytes(raw)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not permit symlinks")
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(link, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("digest", [None, "A" * 64, "a" * 63, 0, True])
def test_rejects_noncanonical_external_trust_digest(tmp_path, digest):
    path = tmp_path / "state.json"
    path.write_bytes(_bytes())
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(path, expected_manifest_sha256=digest)


def test_wrapper_refuses_tampered_key_before_signature_or_snapshot(tmp_path):
    path = tmp_path / "state.json"
    raw = _bytes()
    path.write_bytes(_bytes(trusted_ed25519_public_key_hex="02" * 32))
    with pytest.raises(InstalledWindowsTrustError):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=_test_snapshot(tmp_path), candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_same_byte_path_swap_during_open_rejected(tmp_path, monkeypatch):
    from autosport import windows_update_installed_trust as module
    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(raw)
    actual_open = module._snapshot._open_archive_fd
    swapped = False

    def replace_before_open(candidate):
        nonlocal swapped
        if not swapped and Path(candidate) == path:
            swapped = True
            os.replace(replacement, path)
        return actual_open(candidate)

    monkeypatch.setattr(module._snapshot, "_open_archive_fd", replace_before_open)
    with pytest.raises(InstalledWindowsTrustError, match="changed while opening"):
        load_pinned_installed_update_authority(path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())
    assert swapped


def test_installed_trust_and_real_ed25519_signed_preflight(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from autosport.windows_update_snapshot import pinned_windows_update_package
    from autosport.windows_update_signature import _signed_statement, WindowsUpdateSignatureError
    from autosport import windows_update_signature

    key = Ed25519PrivateKey.generate()  # ephemeral test-only key; never serialized
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    manifest = tmp_path / "встановлений стан.json"
    raw = _bytes(version="1.1.0", schema=1, package_sha256="a" * 64,
                 trusted_ed25519_public_key_hex=public.hex())
    manifest.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    package = tmp_path / "оновлення.zip"
    package.write_bytes(b"test-only snapshot bytes")
    package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    source_sha = "b" * 40
    statement = _signed_statement(package_sha256=package_digest, source_sha=source_sha,
                                  version="1.2.0", schema=2)
    signature = key.sign(statement)
    monkeypatch.setattr(windows_update_signature, "verify_pinned_release_package", lambda snapshot, *, expected_source_sha: {
        "status": "PASS", "package_sha256": snapshot.sha256,
        "source_sha": expected_source_sha, "update_version": "1.2.0", "update_schema": 2,
    })
    with pinned_windows_update_package(package, expected_sha256=package_digest) as snapshot:
        options = dict(
            expected_manifest_sha256=digest, snapshot=snapshot,
            candidate_version="1.2.0", candidate_schema=2,
            expected_source_sha=source_sha, signature=signature,
            supported_schema_migrations=((1, 2),),
        )
        result = assess_update_with_installed_trust(manifest, **options)
        assert result.decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
        assert result.install_authorized is False
        assert result.decision.install_authorized is False
        # Caller cannot replace the key with an arbitrary newly minted key:
        # the trust digest pins the exact original installed manifest.
        manifest.write_bytes(_bytes(version="1.1.0", schema=1,
            trusted_ed25519_public_key_hex="02" * 32))
        with pytest.raises(InstalledWindowsTrustError):
            assess_update_with_installed_trust(manifest, **options)
        manifest.write_bytes(raw)
        with pytest.raises(WindowsUpdateSignatureError):
            assess_update_with_installed_trust(manifest, **{**options, "signature": b"0" * 64})


def _test_snapshot(tmp_path):
    from autosport.windows_update_snapshot import (
        WindowsUpdateSnapshot, _native_change_time_100ns, _open_archive_fd,
        _source_parent_identity,
    )
    path = tmp_path / "pinned-candidate.zip"
    data = b"pinned test-only candidate"
    path.write_bytes(data)
    stat_info = path.stat()
    # A Windows pin must carry the native handle ChangeTime witness. The
    # synthetic fixture previously omitted it, so the real fail-closed
    # snapshot recheck rejected the fixture before each adversarial test.
    fd = _open_archive_fd(path)
    try:
        change_time = _native_change_time_100ns(fd)
    finally:
        os.close(fd)
    return WindowsUpdateSnapshot(
        path, hashlib.sha256(data).hexdigest(), len(data),
        _file_identity=(stat_info.st_dev, stat_info.st_ino),
        _file_metadata=(stat_info.st_size, stat_info.st_mtime_ns, stat_info.st_ctime_ns),
        _parent_identity=_source_parent_identity(path),
        _change_time_100ns=change_time,
    )


def test_installed_authority_rechecked_after_signature_preflight(tmp_path, monkeypatch):
    from autosport import windows_update_signature

    path = tmp_path / "state.json"
    raw = _bytes()
    path.write_bytes(raw)

    def verifier(*args, **kwargs):
        # An external actor changes the installed authority during expensive
        # signature/ZIP verification; a successful verifier is not sufficient.
        path.write_bytes(_bytes(version="1.2.4"))
        return object()

    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", verifier)
    with pytest.raises(InstalledWindowsTrustError):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=_test_snapshot(tmp_path), candidate_version="1.3.0", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_same_byte_manifest_replacement_during_signature_verification_is_rejected(tmp_path, monkeypatch):
    """A digest match cannot authorize a different installed file object."""
    from autosport import windows_update_signature

    path = tmp_path / "installed.json"
    replacement = tmp_path / "replacement.json"
    raw = _bytes()
    path.write_bytes(raw)
    replacement.write_bytes(raw)

    def verifier(*args, **kwargs):
        os.replace(replacement, path)
        return object()

    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", verifier)
    with pytest.raises(InstalledWindowsTrustError, match="changed during preflight"):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=_test_snapshot(tmp_path), candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_relative_installed_manifest_cannot_rebind_on_cwd_change(tmp_path, monkeypatch):
    """Preflight must retain the initial installed root, not follow a new cwd."""
    from autosport import windows_update_signature

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    raw = _bytes()
    (first / "installed.json").write_bytes(raw)
    (second / "installed.json").write_bytes(_bytes(version="9.9.9"))
    monkeypatch.chdir(first)

    def verifier(*args, **kwargs):
        monkeypatch.chdir(second)
        return object()

    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", verifier)
    result = assess_update_with_installed_trust(
        "installed.json", expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        snapshot=_test_snapshot(tmp_path), candidate_version="1.2.4", candidate_schema=2,
        expected_source_sha="b" * 40, signature=b"x" * 64,
        supported_schema_migrations=(),
    )
    assert type(result) is object


def test_installed_authority_records_anchored_identity_and_refuses_forgery(tmp_path):
    path = tmp_path / "довірений стан.json"
    raw = _bytes()
    authority = _load(path, raw)
    assert authority._source_path == path.absolute()
    assert authority._file_identity[1] > 0
    with pytest.raises(InstalledWindowsTrustError, match="identity is invalid"):
        replace(authority, _file_identity=(0, 0))
    with pytest.raises(InstalledWindowsTrustError, match="root is not anchored"):
        replace(authority, _source_path=Path("relative.json"))


def test_same_inode_same_bytes_rewrite_during_signature_is_rejected(tmp_path, monkeypatch):
    """An in-place rewrite is an authority change even when SHA and inode match."""
    from autosport import windows_update_signature

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    before = path.stat()

    def verifier(*args, **kwargs):
        path.write_bytes(raw)
        assert path.stat().st_ino == before.st_ino
        # Explicitly change the modification time to make the concurrent
        # rewrite deterministic on filesystems with coarse timestamps.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        return object()

    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", verifier)
    with pytest.raises(InstalledWindowsTrustError, match="changed during preflight"):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=_test_snapshot(tmp_path), candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_same_inode_tamper_then_restore_during_signature_is_rejected(tmp_path, monkeypatch):
    """Restoring trusted bytes before postcheck must not erase the write trace."""
    from autosport import windows_update_signature

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    before = path.stat()

    def verifier(*args, **kwargs):
        path.write_bytes(_bytes(version="8.8.8"))
        path.write_bytes(raw)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
        return object()

    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", verifier)
    with pytest.raises(InstalledWindowsTrustError, match="changed during preflight"):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=_test_snapshot(tmp_path), candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_candidate_snapshot_changed_during_final_installed_postcheck_is_rejected(tmp_path, monkeypatch):
    """Two trusted inputs must still be pinned when preflight returns."""
    from autosport import windows_update_installed_trust as module
    from autosport import windows_update_signature
    from autosport.windows_update_snapshot import WindowsUpdateSnapshotError

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    snapshot = _test_snapshot(tmp_path)
    original_load = module.load_pinned_installed_update_authority
    calls = 0

    def late_mutation(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_load(*args, **kwargs)
        if calls == 2:
            snapshot.path.write_bytes(b"modified after signature verification")
        return result

    monkeypatch.setattr(module, "load_pinned_installed_update_authority", late_mutation)
    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", lambda *a, **k: object())
    with pytest.raises(WindowsUpdateSnapshotError, match="grew|changed after verification"):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=snapshot, candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )
    assert calls == 2


def test_noncanonical_snapshot_rejected_before_installed_trust_io(tmp_path):
    path = tmp_path / "installed.json"
    path.write_bytes(_bytes())
    with pytest.raises(InstalledWindowsTrustError, match="canonical pinned snapshot"):
        assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            snapshot=object(), candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_installed_metadata_is_canonical_and_immutable(tmp_path):
    path = tmp_path / "installed.json"
    authority = _load(path, _bytes())
    assert authority._file_metadata == (
        path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns
    )
    for invalid in ((0, 0, 0), (1, -1, 0), (1, 2), (1, True, 3)):
        with pytest.raises(InstalledWindowsTrustError, match="metadata is invalid"):
            replace(authority, _file_metadata=invalid)


def test_native_installed_change_time_drift_during_read_fails_closed(tmp_path, monkeypatch):
    """Simulate Windows st_ctime as creation time, with separate change time."""
    from autosport import windows_update_installed_trust as module

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    calls = {"n": 0}

    def stamp(fd):
        calls["n"] += 1
        return 100 if calls["n"] == 1 else 200

    monkeypatch.setattr(module, "_native_change_time_100ns", stamp)
    with pytest.raises(InstalledWindowsTrustError, match="changed during read"):
        module.load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert calls["n"] == 2


def test_native_installed_change_time_drift_during_signature_rejected(tmp_path, monkeypatch):
    """Identical bytes and stat tuple must not erase native NT write evidence."""
    from autosport import windows_update_installed_trust as module
    from autosport import windows_update_signature

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    native = {"stamp": 100}
    monkeypatch.setattr(module, "_native_change_time_100ns", lambda fd: native["stamp"])

    def signature_check(*args, **kwargs):
        native["stamp"] = 200
        return object()

    monkeypatch.setattr(windows_update_signature, "assess_signed_windows_update", signature_check)
    with pytest.raises(InstalledWindowsTrustError, match="changed during preflight"):
        module.assess_update_with_installed_trust(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=_test_snapshot(tmp_path), candidate_version="1.2.4", candidate_schema=2,
            expected_source_sha="b" * 40, signature=b"x" * 64,
            supported_schema_migrations=(),
        )


def test_installed_native_change_time_rejects_forged_metadata(tmp_path, monkeypatch):
    from autosport import windows_update_installed_trust as module

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    monkeypatch.setattr(module, "_native_change_time_100ns", lambda fd: 100)
    pinned = module.load_pinned_installed_update_authority(
        path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert pinned._change_time_100ns == 100
    for invalid in (0, True, -1, "100"):
        with pytest.raises(InstalledWindowsTrustError, match="change-time is invalid"):
            replace(pinned, _change_time_100ns=invalid)


def test_installed_manifest_open_failure_does_not_log_private_os_error(tmp_path, monkeypatch):
    """Trust-root diagnostics must not disclose filesystem or OS error strings."""
    import traceback
    from autosport import windows_update_installed_trust as module

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    def reject_open(*args, **kwargs):
        raise OSError("PRIVATE_MARKER installed-secret-path")
    monkeypatch.setattr(module._snapshot, "_open_archive_fd", reject_open)
    with pytest.raises(InstalledWindowsTrustError, match="installed manifest could not be read") as error:
        load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


def test_installed_manifest_read_error_closes_owned_fd_once(tmp_path, monkeypatch):
    """A stream that already owns/closes fd must not be closed again by raw fd.

    On Windows a recycled integer fd could otherwise close an unrelated
    HANDLE during a concurrent preflight; a failed read is not a reason to
    invalidate descriptor ownership after fdopen succeeds.
    """
    from autosport import windows_update_installed_trust as module

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    original_fdopen = module.os.fdopen
    original_close = module.os.close
    closed_streams = []
    raw_close_attempts = []

    class FailingStream:
        def __init__(self, owned):
            self.owned = owned

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.owned.close()
            closed_streams.append(self.owned.closed)

        def fileno(self):
            return self.owned.fileno()

        def read(self, *_args):
            raise OSError("PRIVATE_MARKER secret-on-disk")

    def failing_fdopen(fd, *args, **kwargs):
        return FailingStream(original_fdopen(fd, *args, **kwargs))

    def observed_close(fd):
        raw_close_attempts.append(fd)
        return original_close(fd)

    monkeypatch.setattr(module.os, "fdopen", failing_fdopen)
    monkeypatch.setattr(module.os, "close", observed_close)
    with pytest.raises(InstalledWindowsTrustError, match="could not be read") as error:
        module.load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert closed_streams == [True]
    assert raw_close_attempts == [], "fdopen-owned descriptor was closed twice"
    import traceback
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


def test_installed_manifest_fdopen_constructor_failure_closes_raw_fd(tmp_path, monkeypatch):
    """If fdopen never accepts ownership, the original raw fd must be closed."""
    from autosport import windows_update_installed_trust as module

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    original_close = module.os.close
    closed = []

    def broken_fdopen(*_args, **_kwargs):
        raise OSError("PRIVATE_MARKER fdopen failed")

    def observed_close(fd):
        closed.append(fd)
        return original_close(fd)

    monkeypatch.setattr(module.os, "fdopen", broken_fdopen)
    monkeypatch.setattr(module.os, "close", observed_close)
    with pytest.raises(InstalledWindowsTrustError, match="could not be read") as error:
        module.load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert len(closed) == 1
    import traceback
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


def test_failed_installed_manifest_read_recovers_without_trust_rebinding(tmp_path, monkeypatch):
    """A transient read failure must not poison later trusted-manifest admission."""
    from autosport import windows_update_installed_trust as module

    path = tmp_path / "installed.json"
    raw = _bytes()
    path.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    baseline = module.load_pinned_installed_update_authority(
        path, expected_manifest_sha256=expected
    )
    original_fdopen = module.os.fdopen

    class ReadFailure:
        def __init__(self, real):
            self.real = real
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.real.close()
        def fileno(self):
            return self.real.fileno()
        def read(self, *_args):
            raise OSError("PRIVATE_MARKER transient read error")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fdopen", lambda fd, *a, **k: ReadFailure(original_fdopen(fd, *a, **k)))
        with pytest.raises(InstalledWindowsTrustError, match="could not be read"):
            module.load_pinned_installed_update_authority(
                path, expected_manifest_sha256=expected
            )
    restored = module.load_pinned_installed_update_authority(
        path, expected_manifest_sha256=expected
    )
    assert restored == baseline
    assert restored.install_authorized is False
    assert path.read_bytes() == raw

@pytest.mark.parametrize("bad_path", ["invalid-\ud800.json", "invalid-\udfff.json"])
def test_malformed_unicode_installed_authority_path_is_sanitized(bad_path):
    with pytest.raises(InstalledWindowsTrustError, match="manifest"):
        load_pinned_installed_update_authority(
            bad_path, expected_manifest_sha256="0" * 64
        )


def test_installed_lstat_unicode_failure_is_sanitized(tmp_path, monkeypatch):
    source = tmp_path / "authority.json"
    source.write_bytes(_bytes())
    original_lstat = Path.lstat
    def broken_lstat(path, *args, **kwargs):
        if path == source:
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", broken_lstat)
    with pytest.raises(InstalledWindowsTrustError, match="manifest"):
        load_pinned_installed_update_authority(
            source, expected_manifest_sha256=hashlib.sha256(_bytes()).hexdigest()
        )


def test_parent_directory_symlink_does_not_define_installed_trust(tmp_path):
    raw = _bytes()
    actual = tmp_path / "actual"
    actual.mkdir()
    (actual / "installed.json").write_bytes(raw)
    redirected = tmp_path / "redirected"
    try:
        redirected.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not permit directory symlinks")
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(
            redirected / "installed.json", expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )


def test_parent_directory_same_byte_swap_during_open_is_rejected(tmp_path, monkeypatch):
    from autosport import windows_update_installed_trust as module
    raw = _bytes()
    directory = tmp_path / "installed"
    directory.mkdir()
    path = directory / "installed.json"
    path.write_bytes(raw)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "installed.json").write_bytes(raw)
    actual_open = module._snapshot._open_archive_fd
    swapped = False

    def replace_parent_during_open(candidate):
        nonlocal swapped
        if not swapped and Path(candidate) == path:
            swapped = True
            os.rename(directory, tmp_path / "old-installed")
            os.rename(replacement, directory)
        return actual_open(candidate)

    monkeypatch.setattr(module._snapshot, "_open_archive_fd", replace_parent_during_open)
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert swapped


def test_parent_directory_identity_is_bound_across_manifest_readbacks(tmp_path):
    raw = _bytes()
    directory = tmp_path / "installed"
    directory.mkdir()
    path = directory / "installed.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    before = load_pinned_installed_update_authority(path, expected_manifest_sha256=digest)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "installed.json").write_bytes(raw)
    os.rename(directory, tmp_path / "old-installed")
    os.rename(replacement, directory)
    after = load_pinned_installed_update_authority(path, expected_manifest_sha256=digest)
    assert before != after, "same-byte parent replacement must invalidate the trust witness"


def test_parent_directory_replaced_after_read_during_json_validation_is_rejected(tmp_path, monkeypatch):
    from autosport import windows_update_installed_trust as module
    raw = _bytes()
    directory = tmp_path / "installed"
    directory.mkdir()
    path = directory / "installed.json"
    path.write_bytes(raw)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / "installed.json").write_bytes(raw)
    actual_loads = module.json.loads
    swapped = False

    def replace_after_read(*args, **kwargs):
        nonlocal swapped
        parsed = actual_loads(*args, **kwargs)
        if not swapped:
            swapped = True
            os.rename(directory, tmp_path / "old-installed")
            os.rename(replacement, directory)
        return parsed

    monkeypatch.setattr(module.json, "loads", replace_after_read)
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert swapped


def test_regular_cyrillic_parent_directory_remains_supported(tmp_path):
    directory = tmp_path / "оновлення з пробілами"
    directory.mkdir()
    path = directory / "встановлена версія.json"
    raw = _bytes()
    first = _load(path, raw)
    second = load_pinned_installed_update_authority(
        path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert first == second
    assert first.install_authorized is False


def test_postparse_native_change_time_mismatch_rejected(tmp_path, monkeypatch):
    """Windows change-time must be checked after JSON hooks, not only while reading."""
    from autosport import windows_update_installed_trust as trust

    path = tmp_path / "встановлений стан.json"
    raw = _bytes()
    path.write_bytes(raw)
    original_loads = trust.json.loads
    parsed = False
    observed = []

    def loads_and_simulate_concurrent_rewrite(*args, **kwargs):
        nonlocal parsed
        result = original_loads(*args, **kwargs)
        parsed = True
        return result

    def native_change_time(fd):
        value = 200 if parsed else 100
        observed.append(value)
        return value

    monkeypatch.setattr(trust.json, "loads", loads_and_simulate_concurrent_rewrite)
    monkeypatch.setattr(trust, "_native_change_time_100ns", native_change_time)
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(
            path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
        )
    assert parsed and 200 in observed


def test_stable_native_change_time_survives_postparse_recheck(tmp_path, monkeypatch):
    from autosport import windows_update_installed_trust as trust

    path = tmp_path / "каталог з пробілами" / "стан.json"
    path.parent.mkdir()
    raw = _bytes()
    path.write_bytes(raw)
    observed = []

    def stable_native_change_time(fd):
        observed.append(fd)
        return 100

    monkeypatch.setattr(trust, "_native_change_time_100ns", stable_native_change_time)
    authority = load_pinned_installed_update_authority(
        path, expected_manifest_sha256=hashlib.sha256(raw).hexdigest()
    )
    assert authority._change_time_100ns == 100
    assert len(observed) >= 3


@pytest.mark.parametrize("unsafe", [
    r"\\server\share\installed.json",
    "//server/share/installed.json",
    r"\\?\C:\\private\installed.json",
    r"C:drive-relative.json",
    "folder/../installed.json",
])
def test_installed_trust_rejects_nonlocal_or_ambiguous_path_before_filesystem_access(monkeypatch, unsafe):
    """A standalone trust-root reader cannot bypass the CLI path policy."""
    import autosport.windows_update_installed_trust as trust_module

    def forbidden_parent_probe(_path):
        pytest.fail("untrusted installed authority path reached filesystem inspection")

    monkeypatch.setattr(trust_module, "_source_parent_identity", forbidden_parent_probe)
    with pytest.raises(InstalledWindowsTrustError):
        load_pinned_installed_update_authority(unsafe, expected_manifest_sha256="a" * 64)


def test_installed_manifest_fdopen_and_close_failure_stays_sanitized(tmp_path, monkeypatch):
    """A secondary close-wrapper failure cannot expose installed trust paths."""
    import os
    import traceback
    from autosport import windows_update_installed_trust as module

    manifest = tmp_path / "особисті дані" / "installed.json"
    manifest.parent.mkdir()
    data = _bytes()
    manifest.write_bytes(data)
    real_open, real_fdopen, real_close = module._snapshot._open_archive_fd, os.fdopen, os.close
    owned = []

    def capture_open(path):
        fd = real_open(path)
        if os.fspath(path) == os.fspath(manifest):
            owned.append(fd)
        return fd

    def fail_fdopen(fd, *args, **kwargs):
        if owned and fd == owned[-1]:
            raise OSError("PRIVATE_MANIFEST_PATH fdopen failed")
        return real_fdopen(fd, *args, **kwargs)

    def close_then_fail(fd):
        real_close(fd)
        if owned and fd == owned[-1]:
            raise RuntimeError("PRIVATE_CLOSE_PATH close wrapper failed")

    with monkeypatch.context() as patch:
        patch.setattr(module._snapshot, "_open_archive_fd", capture_open)
        patch.setattr(module.os, "fdopen", fail_fdopen)
        patch.setattr(module.os, "close", close_then_fail)
        with pytest.raises(module.InstalledWindowsTrustError, match="could not be read") as error:
            module.load_pinned_installed_update_authority(
                manifest, expected_manifest_sha256=hashlib.sha256(data).hexdigest()
            )
    assert len(owned) == 1
    with pytest.raises(OSError):
        os.fstat(owned[0])
    formatted = "".join(traceback.format_exception(error.value))
    assert "PRIVATE_MANIFEST_PATH" not in formatted
    assert "PRIVATE_CLOSE_PATH" not in formatted
