"""Negative, restart and tamper tests for update-package byte pinning."""

import hashlib
import struct
from pathlib import Path

import pytest

from autosport.windows_update_snapshot import (
    WindowsUpdateSnapshotError,
    pinned_windows_update_package,
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_snapshot_is_separate_from_source_and_not_install_authority(tmp_path):
    source = tmp_path / "оновлення з пробілами.zip"
    source.write_bytes(b"valid fixture package")
    with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
        assert snap.path != source
        assert snap.path.read_bytes() == source.read_bytes()
        assert snap.install_authorized is False
        source.write_bytes(b"replaced source")
        snap.recheck()
    assert not snap.path.exists()


def test_snapshot_recheck_rejects_post_verification_tamper(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"known package")
    with pytest.raises(WindowsUpdateSnapshotError, match="changed after verification"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            snap.path.write_bytes(b"tampered")
    assert not snap.path.exists()


def test_snapshot_recheck_rejects_same_length_tamper(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"AAAA")
    with pytest.raises(WindowsUpdateSnapshotError, match="changed after verification"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"AAAA")) as snap:
            snap.path.write_bytes(b"BBBB")


def test_source_digest_mismatch_fails(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"bad")
    with pytest.raises(WindowsUpdateSnapshotError, match="digest mismatch"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"good")):
            pytest.fail("must not yield unverified snapshot")


@pytest.mark.parametrize("bad", ["A" * 64, "abc", "0" * 63, 7, True, None])
def test_bad_expected_sha_fails_before_open(tmp_path, bad):
    with pytest.raises(WindowsUpdateSnapshotError):
        with pinned_windows_update_package(tmp_path / "missing.zip", expected_sha256=bad):
            pytest.fail("must not yield")


@pytest.mark.parametrize("bound", [0, -1, True, 2**31 + 1, "100"])
def test_bad_bounds_fail(tmp_path, bound):
    with pytest.raises(WindowsUpdateSnapshotError):
        with pinned_windows_update_package(tmp_path / "missing.zip", expected_sha256=digest(b"x"), max_bytes=bound):
            pytest.fail("must not yield")


def test_oversized_source_rejected(tmp_path):
    source = tmp_path / "big.zip"
    source.write_bytes(b"012345")
    with pytest.raises(WindowsUpdateSnapshotError, match="bounded regular"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes()), max_bytes=5):
            pytest.fail("must not yield")


def test_empty_source_rejected(tmp_path):
    source = tmp_path / "empty.zip"
    source.write_bytes(b"")
    with pytest.raises(WindowsUpdateSnapshotError, match="bounded regular"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"")):
            pytest.fail("must not yield")


def test_missing_source_rejected(tmp_path):
    with pytest.raises(WindowsUpdateSnapshotError, match="cannot open"):
        with pinned_windows_update_package(tmp_path / "missing.zip", expected_sha256=digest(b"x")):
            pytest.fail("must not yield")


def test_directory_rejected(tmp_path):
    with pytest.raises(WindowsUpdateSnapshotError):
        with pinned_windows_update_package(tmp_path, expected_sha256=digest(b"x")):
            pytest.fail("must not yield")


def test_symlink_source_rejected(tmp_path):
    source = tmp_path / "target.zip"
    source.write_bytes(b"good")
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    with pytest.raises(WindowsUpdateSnapshotError, match="symlink"):
        with pinned_windows_update_package(link, expected_sha256=digest(b"good")):
            pytest.fail("must not yield")


def test_snapshot_cleaned_after_caller_failure(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"good")
    snapshot_path = None
    with pytest.raises(RuntimeError, match="abort"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"good")) as snap:
            snapshot_path = snap.path
            raise RuntimeError("abort")
    assert snapshot_path is not None and not snapshot_path.exists()


def test_restart_repeated_pin_no_persisted_state(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"good")
    for _ in range(3):
        with pinned_windows_update_package(source, expected_sha256=digest(b"good")) as snap:
            assert snap.path.exists()
        assert not snap.path.exists()


def test_canonical_release_verifier_uses_only_pinned_bytes(tmp_path, monkeypatch, release_package):
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture bytes")
    observed = []

    def fake_verifier(path, *, expected_source_sha):
        observed.append((path, expected_source_sha))
        assert path != source
        assert path.read_bytes() == b"fixture bytes"
        return {"status": "PASS", "package_sha256": digest(b"fixture bytes"), "source_sha": expected_source_sha}

    monkeypatch.setattr(release_package, "verify_windows_package", fake_verifier, raising=False)
    # This isolated test intentionally supplies non-ZIP bytes. Other tests
    # exercise the real pinned BUILD_INFO metadata reader on actual ZIP bytes.
    from autosport import windows_update_snapshot as module
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture bytes")) as snap:
        report = verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert report["status"] == "PASS"
        assert observed == [(snap.path, "a" * 40)]


def test_canonical_verifier_cannot_report_other_bytes(tmp_path, monkeypatch, release_package):
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(release_package, "verify_windows_package", lambda *a, **k: {"status": "PASS", "package_sha256": "0" * 64}, raising=False)
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="did not confirm"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_canonical_verifier_postcall_mutation_is_rejected(tmp_path, monkeypatch, release_package):
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")

    def corrupting_verifier(path, *, expected_source_sha):
        path.write_bytes(b"tamper!")
        return {"status": "PASS", "package_sha256": digest(b"fixture"), "source_sha": expected_source_sha}

    monkeypatch.setattr(release_package, "verify_windows_package", corrupting_verifier, raising=False)
    with pytest.raises(WindowsUpdateSnapshotError, match="changed after verification"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_canonical_verifier_exception_is_fail_closed(tmp_path, monkeypatch, release_package):
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")

    def failing_verifier(*args, **kwargs):
        raise ValueError("untrusted malformed ZIP")

    monkeypatch.setattr(release_package, "verify_windows_package", failing_verifier, raising=False)
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="verification failed"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


@pytest.fixture
def release_package(monkeypatch):
    """Use real repo verifier in CI; minimal import stub for isolated ZIP tests."""
    # Legacy unit tests intentionally supply non-ZIP byte fixtures to isolate
    # the canonical verifier's post-call identity/exception contract. The
    # resource preflight is tested independently with actual ZIP structures.
    from autosport import windows_update_snapshot as snapshot_module
    monkeypatch.setattr(snapshot_module, "_require_bounded_zip_members", lambda path: None)
    import sys
    import types
    try:
        from autosport import release_package as module
    except ImportError:
        module = types.ModuleType("autosport.release_package")
        monkeypatch.setitem(sys.modules, "autosport.release_package", module)
    return module


def test_snapshot_recheck_rejects_same_byte_replacement(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"trusted package")
    with pytest.raises(WindowsUpdateSnapshotError, match="identity changed"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"trusted package")) as snap:
            replacement = snap.path.with_name("replacement.zip")
            replacement.write_bytes(b"trusted package")
            replacement.replace(snap.path)


def test_snapshot_recheck_rejects_symlink_even_to_identical_bytes(tmp_path):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"trusted package")
    with pytest.raises(WindowsUpdateSnapshotError, match="not a regular file"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"trusted package")) as snap:
            target = snap.path.with_name("target.zip")
            target.write_bytes(b"trusted package")
            snap.path.unlink()
            try:
                snap.path.symlink_to(target)
            except (OSError, NotImplementedError):
                pytest.skip("symlink creation unavailable")


def test_bad_zip_exception_is_normalized_to_snapshot_error(tmp_path, monkeypatch, release_package):
    import zipfile
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"not a ZIP")

    def reject_bad_zip(*args, **kwargs):
        raise zipfile.BadZipFile("invalid ZIP headers")

    monkeypatch.setattr(release_package, "verify_windows_package", reject_bad_zip, raising=False)
    with pinned_windows_update_package(source, expected_sha256=digest(b"not a ZIP")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="verification failed"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_canonical_verifier_cannot_report_wrong_source_commit(tmp_path, monkeypatch, release_package):
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        release_package, "verify_windows_package",
        lambda *args, **kwargs: {
            "status": "PASS",
            "package_sha256": digest(b"fixture"),
            "source_sha": "b" * 40,
        },
        raising=False,
    )
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="did not confirm"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_source_replacement_during_open_is_rejected_even_with_same_bytes(tmp_path, monkeypatch):
    """A same-content inode swap must not bypass the pre-open identity check."""
    import os
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"trusted")
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(b"trusted")
    original_open = module._open_archive_fd
    did_swap = False

    def swapped_open(path):
        nonlocal did_swap
        if Path(path) == source and not did_swap:
            did_swap = True
            replacement.replace(source)
        return original_open(path)

    monkeypatch.setattr(module, "_open_archive_fd", swapped_open)
    with pytest.raises(WindowsUpdateSnapshotError, match="identity changed during open"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"trusted")):
            pytest.fail("swapped source must never yield a snapshot")
    assert did_swap


def test_source_symlink_swap_during_open_is_rejected_without_nofollow(tmp_path, monkeypatch):
    """Reject a symlink swap through the actual no-follow leaf opener."""
    import os
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"trusted")
    target = tmp_path / "target.zip"
    target.write_bytes(b"trusted")
    original_open = module._open_archive_fd
    did_swap = False

    def swapped_open(path):
        nonlocal did_swap
        if Path(path) == source and not did_swap:
            did_swap = True
            source.unlink()
            try:
                source.symlink_to(target)
            except (OSError, NotImplementedError):
                pytest.skip("symlink creation unavailable")
        return original_open(path)

    monkeypatch.setattr(module, "_open_archive_fd", swapped_open)
    with pytest.raises(WindowsUpdateSnapshotError, match="identity changed during open|cannot open package source"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"trusted")):
            pytest.fail("symlink swap must never yield a snapshot")
    assert did_swap


def test_snapshot_recheck_rejects_midread_same_byte_path_swap(tmp_path, monkeypatch):
    """A digest of an open old inode must not validate a newly replaced path."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"same verified bytes")
    with pytest.raises(WindowsUpdateSnapshotError, match="changed during recheck"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            real_fdopen = module.os.fdopen

            class SwappingStream:
                def __init__(self, raw):
                    self.raw = raw
                    self.swapped = False

                def __enter__(self):
                    self.raw.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.raw.__exit__(*args)

                def fileno(self):
                    return self.raw.fileno()

                def read(self, amount):
                    data = self.raw.read(amount)
                    if data and not self.swapped:
                        replacement = snap.path.with_name("same-content-new-inode.zip")
                        replacement.write_bytes(data)
                        replacement.replace(snap.path)
                        self.swapped = True
                    return data

            def swap_on_read(fd, *args, **kwargs):
                return SwappingStream(real_fdopen(fd, *args, **kwargs))

            with monkeypatch.context() as context:
                context.setattr(module.os, "fdopen", swap_on_read)
                snap.recheck()


def test_snapshot_recheck_rejects_metadata_only_inplace_change(tmp_path, monkeypatch):
    """An opened file can change metadata without changing its SHA-256."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"unchanged content")
    with pytest.raises(WindowsUpdateSnapshotError, match="changed during recheck"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            real_fdopen = module.os.fdopen

            class MetadataStream:
                def __init__(self, raw):
                    self.raw = raw
                    self.changed = False

                def __enter__(self):
                    self.raw.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.raw.__exit__(*args)

                def fileno(self):
                    return self.raw.fileno()

                def read(self, amount):
                    data = self.raw.read(amount)
                    if data and not self.changed:
                        import os
                        before = os.stat(snap.path)
                        os.utime(snap.path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
                        self.changed = True
                    return data

            with monkeypatch.context() as context:
                context.setattr(module.os, "fdopen", lambda fd, *a, **k: MetadataStream(real_fdopen(fd, *a, **k)))
                snap.recheck()


def test_source_path_replacement_during_copy_rejected_even_with_same_bytes(tmp_path, monkeypatch):
    """A source path swapped after open must not silently produce a trusted pin."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"same bytes across a source path replacement")
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(source.read_bytes())
    original_fdopen = module.os.fdopen
    swapped = False

    class SwappingRead:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            self.raw.__enter__()
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def fileno(self):
            return self.raw.fileno()

        def read(self, amount):
            nonlocal swapped
            block = self.raw.read(amount)
            if block and not swapped:
                replacement.replace(source)
                swapped = True
            return block

    def wrap_source(fd, *args, **kwargs):
        raw = original_fdopen(fd, *args, **kwargs)
        return SwappingRead(raw) if not swapped else raw

    with monkeypatch.context() as context:
        context.setattr(module.os, "fdopen", wrap_source)
        with pytest.raises(WindowsUpdateSnapshotError, match="source changed while being copied"):
            with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())):
                pytest.fail("source replacement must be rejected")
    assert swapped


def test_source_metadata_mutation_during_copy_rejected(tmp_path, monkeypatch):
    """A metadata-only source mutation must fail even if the digest is unchanged."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"metadata-only mutation")
    original_fdopen = module.os.fdopen
    changed = False

    class MutatingRead:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            self.raw.__enter__()
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def fileno(self):
            return self.raw.fileno()

        def read(self, amount):
            nonlocal changed
            block = self.raw.read(amount)
            if block and not changed:
                import os
                old = os.stat(source)
                os.utime(source, ns=(old.st_atime_ns, old.st_mtime_ns + 2_000_000_000))
                changed = True
            return block

    with monkeypatch.context() as context:
        context.setattr(module.os, "fdopen", lambda fd, *a, **k: MutatingRead(original_fdopen(fd, *a, **k)))
        with pytest.raises(WindowsUpdateSnapshotError, match="source changed while being copied"):
            with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())):
                pytest.fail("metadata mutation must be rejected")
    assert changed


def test_source_with_unavailable_file_identity_fails_closed(tmp_path, monkeypatch):
    """A filesystem returning inode 0 cannot prove a same-byte path was not swapped."""
    import os
    import stat
    from types import SimpleNamespace
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"no file identity")
    original_lstat = Path.lstat
    original_stat = Path.stat
    original_fstat = os.fstat

    def without_identity(value):
        if not stat.S_ISREG(value.st_mode):
            return value
        return SimpleNamespace(
            st_mode=value.st_mode, st_dev=value.st_dev, st_ino=0,
            st_size=value.st_size, st_mtime_ns=value.st_mtime_ns,
            st_ctime_ns=value.st_ctime_ns,
        )

    with monkeypatch.context() as context:
        context.setattr(Path, "lstat", lambda self: without_identity(original_lstat(self)))
        context.setattr(Path, "stat", lambda self, **kwargs: without_identity(original_stat(self, **kwargs)))
        context.setattr(module.os, "fstat", lambda fd: without_identity(original_fstat(fd)))
        with pytest.raises(WindowsUpdateSnapshotError, match="identity unavailable"):
            with pinned_windows_update_package(source, expected_sha256=digest(b"no file identity")):
                pytest.fail("inode=0 must never establish a trusted snapshot")


def test_unbound_snapshot_without_descriptor_identity_is_not_accepted(tmp_path):
    """A digest-matching path is not proof that the caller pinned this object."""
    from autosport.windows_update_snapshot import WindowsUpdateSnapshot

    candidate = tmp_path / "candidate.zip"
    candidate.write_bytes(b"same digest, no file identity")
    snapshot = WindowsUpdateSnapshot(candidate, digest(candidate.read_bytes()), candidate.stat().st_size)
    with pytest.raises(WindowsUpdateSnapshotError, match="identity changed"):
        snapshot.recheck()


def test_recheck_rejects_unavailable_inode_after_valid_pin(tmp_path, monkeypatch):
    """A filesystem losing identity support must not silently fall back to SHA."""
    from types import SimpleNamespace

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"immutable")
    with pinned_windows_update_package(source, expected_sha256=digest(b"immutable")) as snap:
        original_lstat = Path.lstat

        def unavailable_inode(self):
            original = original_lstat(self)
            if self != snap.path:
                return original
            return SimpleNamespace(
                st_mode=original.st_mode, st_dev=original.st_dev, st_ino=0,
                st_size=original.st_size, st_mtime_ns=original.st_mtime_ns,
                st_ctime_ns=original.st_ctime_ns,
            )

        with monkeypatch.context() as context:
            context.setattr(Path, "lstat", unavailable_inode)
            with pytest.raises(WindowsUpdateSnapshotError, match="identity unavailable"):
                snap.recheck()


def test_pinned_verifier_rejects_zip_bomb_before_canonical_read(tmp_path, monkeypatch):
    """Tiny compressed input must not trigger unbounded canonical decompression."""
    import io
    import zipfile
    import sys
    import types
    from autosport.windows_update_snapshot import (
        WindowsUpdateSnapshotError, pinned_windows_update_package,
        verify_pinned_release_package,
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/huge.txt", b"x" * (2 * 1024 * 1024))
    package = tmp_path / "zip bomb.zip"
    package.write_bytes(output.getvalue())
    called = []
    fake_release = types.ModuleType("autosport.release_package")
    fake_release.verify_windows_package = lambda *a, **k: called.append(1)
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    with pinned_windows_update_package(package, expected_sha256=hashlib.sha256(package.read_bytes()).hexdigest()) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="compression ratio"):
            verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert called == []


def test_pinned_verifier_rejects_excessive_member_count_before_canonical_read(tmp_path, monkeypatch):
    """Many small members cannot consume unbounded verifier metadata/memory."""
    import zipfile
    import sys
    import types
    from autosport.windows_update_snapshot import (
        WindowsUpdateSnapshotError, pinned_windows_update_package,
        verify_pinned_release_package,
    )

    package = tmp_path / "too many.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        for index in range(4097):
            archive.writestr(f"Autosport-V1/{index:05d}.txt", b"x")
    called = []
    fake_release = types.ModuleType("autosport.release_package")
    fake_release.verify_windows_package = lambda *a, **k: called.append(1)
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    with pinned_windows_update_package(package, expected_sha256=hashlib.sha256(package.read_bytes()).hexdigest()) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="directory exceeds bound"):
            verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert called == []


def test_pinned_verifier_rejects_corrupt_zip_before_canonical_read(tmp_path, monkeypatch):
    import hashlib
    import sys
    import types
    from autosport.windows_update_snapshot import (
        WindowsUpdateSnapshotError, pinned_windows_update_package,
        verify_pinned_release_package,
    )

    package = tmp_path / "corrupt.zip"
    package.write_bytes(b"not a zip archive")
    called = []
    fake_release = types.ModuleType("autosport.release_package")
    fake_release.verify_windows_package = lambda *a, **k: called.append(1)
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    with pinned_windows_update_package(package, expected_sha256=hashlib.sha256(package.read_bytes()).hexdigest()) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="archive directory is invalid"):
            verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert called == []


def test_pinned_verifier_rejects_aggregate_expansion_before_canonical_read(tmp_path, monkeypatch):
    """Per-member safety does not imply an aggregate extraction ceiling."""
    import sys
    import types
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "many bytes.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/a.txt", b"a" * 8)
        archive.writestr("Autosport-V1/b.txt", b"b" * 8)
    monkeypatch.setattr(module, "_MAX_UNCOMPRESSED_BYTES", 15)
    called = []
    fake_release = types.ModuleType("autosport.release_package")
    fake_release.verify_windows_package = lambda *a, **k: called.append(1)
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    with module.pinned_windows_update_package(package, expected_sha256=digest(package.read_bytes())) as snapshot:
        with pytest.raises(module.WindowsUpdateSnapshotError, match="expanded size"):
            module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert called == []


def test_pinned_verifier_accepts_bounded_zip_for_canonical_validation(tmp_path, monkeypatch):
    """The new resource guard does not replace deep canonical verification."""
    import sys
    import types
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "bounded.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/a.txt", b"a" * 8)
        archive.writestr("Autosport-V1/BUILD_INFO.json", (
            '{"product":"Autosport","source_sha":"' + 'a' * 40 + '",'
            '"version":"0.1.0-v1-prehuman"}'
        ))
    called = []
    fake_release = types.ModuleType("autosport.release_package")
    def canonical(path, *, expected_source_sha):
        called.append((path, expected_source_sha))
        return {"status": "PASS", "package_sha256": digest(path.read_bytes()), "source_sha": expected_source_sha}
    fake_release.verify_windows_package = canonical
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    with module.pinned_windows_update_package(package, expected_sha256=digest(package.read_bytes())) as snapshot:
        report = module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
        assert report["status"] == "PASS"
        assert called == [(snapshot.path, "a" * 40)]

@pytest.mark.parametrize(
    ("field_offset", "field_value", "reason"),
    [
        (8, 1, "unsupported ZIP flags"),
        (10, 99, "unsupported ZIP compression"),
    ],
)
def test_zip_preflight_rejects_unsupported_features_before_decompression(
    tmp_path, field_offset, field_value, reason
):
    """Encrypted/unknown-codec ZIPs must never reach archive.read()."""
    import struct
    import zipfile
    from autosport.windows_update_snapshot import _require_bounded_zip_members

    package = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    payload = bytearray(package.read_bytes())
    central = payload.index(b"PK\x01\x02")
    struct.pack_into("<H", payload, central + field_offset, field_value)
    package.write_bytes(payload)
    with pytest.raises(WindowsUpdateSnapshotError, match=reason):
        _require_bounded_zip_members(package)

@pytest.mark.parametrize(("field_offset", "field_value"), [(8, 1), (10, 99)])
def test_unsupported_zip_never_invokes_canonical_verifier(
    tmp_path, monkeypatch, field_offset, field_value
):
    """Full pinned flow refuses hostile ZIP flags/codecs before deep reading."""
    import struct
    import sys
    import types
    import zipfile
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    payload = bytearray(source.read_bytes())
    central = payload.index(b"PK\x01\x02")
    struct.pack_into("<H", payload, central + field_offset, field_value)
    source.write_bytes(payload)

    verifier = types.ModuleType("autosport.release_package")

    def forbidden(*args, **kwargs):
        pytest.fail("canonical verifier must not decompress an unsupported ZIP")

    verifier.verify_windows_package = forbidden
    monkeypatch.setitem(sys.modules, "autosport.release_package", verifier)
    with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="unsupported ZIP"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)

# Plan-6 6Б: an Ed25519 envelope must not borrow a claimed version/schema from
# an unverified caller report. The update identity comes from the pinned ZIP's
# canonical BUILD_INFO.json, whose bytes the release verifier has already hashed.
@pytest.mark.parametrize("build_info, expected", [
    ({"product": "Autosport", "source_sha": "a" * 40,
      "version": "1.2.0", "update_version": "1.2.0", "update_schema": 2},
     {"update_version": "1.2.0", "update_schema": 2}),
    ({"product": "Autosport", "source_sha": "a" * 40,
      "version": "0.1.0-v1-prehuman"}, {}),
])
def test_update_identity_is_extracted_only_from_verified_pinned_build_info(
    tmp_path, monkeypatch, release_package, build_info, expected,
):
    import json
    import zipfile
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "кандидат оновлення.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(build_info))
    payload_digest = digest(source.read_bytes())
    monkeypatch.setattr(release_package, "verify_windows_package", lambda path, *, expected_source_sha: {
        "status": "PASS", "package_sha256": payload_digest, "source_sha": expected_source_sha,
        # A caller-controlled deep-verifier report cannot mint an update identity.
        "update_version": "99.0.0", "update_schema": 99,
    }, raising=False)
    with pinned_windows_update_package(source, expected_sha256=payload_digest) as snap:
        report = verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert {k: report[k] for k in ("update_version", "update_schema") if k in report} == expected


@pytest.mark.parametrize("metadata", [
    {"update_version": "1.2.0"},
    {"update_schema": 2},
    {"update_version": "1.2.0", "update_schema": True},
    {"update_version": "01.2.0", "update_schema": 2},
    {"update_version": "1.2.0", "update_schema": 2, "version": "1.1.0"},
    {"update_version": "1.2.0", "update_schema": 2, "source_sha": "b" * 40},
])
def test_malformed_embedded_update_identity_is_rejected(tmp_path, monkeypatch, release_package, metadata):
    import json
    import zipfile
    from autosport.windows_update_snapshot import verify_pinned_release_package

    build_info = {"product": "Autosport", "source_sha": "a" * 40, "version": "1.2.0"}
    build_info.update(metadata)
    source = tmp_path / "candidate.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(build_info))
    package_digest = digest(source.read_bytes())
    monkeypatch.setattr(release_package, "verify_windows_package", lambda path, *, expected_source_sha: {
        "status": "PASS", "package_sha256": package_digest, "source_sha": expected_source_sha,
    }, raising=False)
    with pinned_windows_update_package(source, expected_sha256=package_digest) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="embedded update"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_duplicate_json_update_identity_cannot_be_last_wins(tmp_path, monkeypatch, release_package):
    import zipfile
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    raw = ('{"product":"Autosport","source_sha":"' + 'a' * 40 + '",'
           '"version":"1.2.0","update_version":"1.2.0",'
           '"update_schema":1,"update_schema":2}').encode("utf-8")
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", raw)
    package_digest = digest(source.read_bytes())
    monkeypatch.setattr(release_package, "verify_windows_package", lambda path, *, expected_source_sha: {
        "status": "PASS", "package_sha256": package_digest, "source_sha": expected_source_sha,
    }, raising=False)
    with pinned_windows_update_package(source, expected_sha256=package_digest) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="embedded update"):
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_oversized_embedded_build_info_rejected_before_read(tmp_path, monkeypatch, release_package):
    """A verified archive cannot force unbounded metadata decompression."""
    import zipfile
    from autosport import windows_update_snapshot as module

    source = tmp_path / "large-metadata.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", b" " * (64 * 1024 + 1))
    package_digest = digest(source.read_bytes())
    monkeypatch.setattr(release_package, "verify_windows_package", lambda path, *, expected_source_sha: {
        "status": "PASS", "package_sha256": package_digest, "source_sha": expected_source_sha,
    }, raising=False)
    with module.pinned_windows_update_package(source, expected_sha256=package_digest) as snap:
        with pytest.raises(module.WindowsUpdateSnapshotError, match="embedded update"):
            module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_private_snapshot_same_bytes_rewrite_between_checks_is_rejected(tmp_path):
    """A signed snapshot cannot regain authority after an in-place same-byte rewrite."""
    import os

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"trusted archive bytes")
    with pytest.raises(WindowsUpdateSnapshotError, match="changed"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            before = snap.path.stat()
            snap.path.write_bytes(b"trusted archive bytes")
            os.utime(snap.path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            snap.recheck()


def test_snapshot_recheck_detects_metadata_change_after_last_fd_stat(tmp_path, monkeypatch):
    """A write between final descriptor fstat and path lstat must fail closed."""
    import os
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"unchanged")
    with pytest.raises(WindowsUpdateSnapshotError, match="changed"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            original_lstat = Path.lstat
            calls = 0

            def changed_lstat(path, *args, **kwargs):
                nonlocal calls
                if path == snap.path:
                    calls += 1
                    if calls == 2:
                        current = os.stat(snap.path)
                        os.utime(snap.path, ns=(current.st_atime_ns, current.st_mtime_ns + 2_000_000_000))
                return original_lstat(path, *args, **kwargs)

            with monkeypatch.context() as context:
                context.setattr(module.Path, "lstat", changed_lstat)
                snap.recheck()
    assert calls == 2


def test_snapshot_recheck_rejects_removed_pinned_metadata(tmp_path):
    """Mutation of frozen dataclass internals must never weaken pinning."""
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"trusted")
    with pytest.raises(WindowsUpdateSnapshotError, match="snapshot authority is invalid"):
        with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            object.__setattr__(snap, "_file_metadata", None)
            snap.recheck()


def test_native_change_time_drift_rejects_same_digest_and_metadata(tmp_path, monkeypatch):
    """Simulate NT change-time drift invisible to Windows creation-time stat."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "package.zip"
    source.write_bytes(b"same exact bytes")
    native = {"stamp": 100}
    monkeypatch.setattr(module, "_native_change_time_100ns", lambda fd: native["stamp"])
    with pytest.raises(WindowsUpdateSnapshotError, match="changed"):
        with module.pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
            assert snap._change_time_100ns == 100
            # Digest, inode and all three Python stat values remain unchanged;
            # Windows ChangeTime is the only changed witness in this test.
            native["stamp"] = 200
            snap.recheck()


def test_native_change_time_drift_during_source_copy_rejected(tmp_path, monkeypatch):
    """A same-content source rewrite during copy cannot inherit an old pin."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "package.zip"
    source.write_bytes(b"same exact bytes")
    native = {"reads": 0}

    def changing_stamp(fd):
        native["reads"] += 1
        return 100 if native["reads"] == 1 else 200

    monkeypatch.setattr(module, "_native_change_time_100ns", changing_stamp)
    with pytest.raises(WindowsUpdateSnapshotError, match="package changed while being copied"):
        with module.pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())):
            pytest.fail("changed native source identity must never yield a pinned package")
    assert native["reads"] == 2


def test_invalid_native_stamp_fails_closed_even_with_correct_digest(tmp_path, monkeypatch):
    from autosport import windows_update_snapshot as module

    source = tmp_path / "package.zip"
    source.write_bytes(b"bytes")
    monkeypatch.setattr(module, "_native_change_time_100ns", lambda fd: 0)
    with pytest.raises(WindowsUpdateSnapshotError, match="snapshot authority is invalid"):
        with module.pinned_windows_update_package(source, expected_sha256=digest(b"bytes")):
            pytest.fail("zero native change-time cannot establish trust")


def test_win32_file_basic_info_change_time_query_contract(monkeypatch):
    """Cross-platform ABI mock: actual Windows invocation still needs Windows CI."""
    import ctypes
    import sys
    from types import SimpleNamespace
    from autosport import windows_update_snapshot as module

    calls = []

    class Query:
        argtypes = None
        restype = None

        def __call__(self, handle, info_class, output, size):
            calls.append((handle, info_class, size))
            # FILE_BASIC_INFO has four signed 64-bit timestamps. ChangeTime
            # is the fourth, distinct from CreationTime (Windows st_ctime).
            fields = ctypes.cast(output, ctypes.POINTER(ctypes.c_longlong * 4))
            fields.contents[0] = 10
            fields.contents[3] = 987654321
            return True

    query = Query()
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 12345))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error: SimpleNamespace(
        GetFileInformationByHandleEx=query,
    ), raising=False)
    assert module._query_windows_change_time_100ns(5) == 987654321
    assert calls == [(12345, 0, 40)]
    assert len(query.argtypes) == 4
    assert query.restype is not None


def test_win32_file_basic_info_query_error_fails_closed(monkeypatch):
    import ctypes
    import sys
    from types import SimpleNamespace
    from autosport import windows_update_snapshot as module

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 12345))

    def no_info(*args):
        return False

    monkeypatch.setattr(ctypes, "WinDLL", lambda name, use_last_error: SimpleNamespace(
        GetFileInformationByHandleEx=no_info,
    ), raising=False)
    with pytest.raises(WindowsUpdateSnapshotError, match="change-time unavailable"):
        module._query_windows_change_time_100ns(5)


def test_source_fdopen_failure_releases_raw_descriptor(tmp_path, monkeypatch):
    """An fdopen constructor error must not leak the source handle."""
    import errno
    import hashlib
    import os
    from autosport import windows_update_snapshot as module

    source = tmp_path / "source.zip"
    source.write_bytes(b"source payload")
    raw_open = module._open_archive_fd
    raw_fdopen = os.fdopen
    observed = []

    def recording_open(path):
        fd = raw_open(path)
        if os.fspath(path) == os.fspath(source):
            observed.append(fd)
        return fd
    def failing_fdopen(fd, *args, **kwargs):
        if observed and fd == observed[-1]:
            raise OSError(errno.EMFILE, "simulated fdopen allocation failure")
        return raw_fdopen(fd, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_archive_fd", recording_open)
        patch.setattr(module.os, "fdopen", failing_fdopen)
        with pytest.raises(WindowsUpdateSnapshotError, match="snapshot I/O failure"):
            with pinned_windows_update_package(source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()):
                pass
    assert len(observed) == 1
    with pytest.raises(OSError):
        os.fstat(observed[0])


def test_recheck_fdopen_failure_releases_raw_descriptor(tmp_path, monkeypatch):
    """Repeated rejected rechecks cannot exhaust process file descriptors."""
    import errno
    import hashlib
    import os
    from autosport import windows_update_snapshot as module

    source = tmp_path / "source.zip"
    source.write_bytes(b"source payload")
    with pinned_windows_update_package(source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()) as snap:
        raw_open = module._open_archive_fd
        raw_fdopen = os.fdopen
        observed = []

        def recording_open(path):
            fd = raw_open(path)
            if os.fspath(path) == os.fspath(snap.path):
                observed.append(fd)
            return fd

        def failing_fdopen(fd, *args, **kwargs):
            if observed and fd == observed[-1]:
                raise OSError(errno.EMFILE, "simulated fdopen allocation failure")
            return raw_fdopen(fd, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(module, "_open_archive_fd", recording_open)
            patch.setattr(module.os, "fdopen", failing_fdopen)
            with pytest.raises(WindowsUpdateSnapshotError, match="pinned package unreadable"):
                snap.recheck()
        assert len(observed) == 1
        with pytest.raises(OSError):
            os.fstat(observed[0])


def test_zip_preflight_rejects_path_swap_before_central_directory_parse(tmp_path, monkeypatch):
    """EOCD and central directory must come from the same held file object.

    The attacker replaces the pathname with another valid archive exactly as
    ZipFile is constructed. Reopening the pathname would parse unbounded
    metadata that was never admitted by the preceding EOCD resource guard.
    """
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "candidate.zip"
    replacement = tmp_path / "replacement.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    with zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
        archive.writestr("Autosport-V1/extra.txt", "extra")
    original_inode = package.stat().st_ino
    actual_zipfile = zipfile.ZipFile
    seen = []

    def swap_before_zip_parse(file, *args, **kwargs):
        replacement.replace(package)
        seen.append(file)
        return actual_zipfile(file, *args, **kwargs)

    monkeypatch.setattr(module.zipfile, "ZipFile", swap_before_zip_parse)
    with pytest.raises(module.WindowsUpdateSnapshotError, match="changed during preflight"):
        module._require_bounded_zip_members(package)
    assert len(seen) == 1
    assert hasattr(seen[0], "fileno"), "ZIP parser reopened a mutable pathname"
    assert package.stat().st_ino != original_inode


def test_zip_preflight_rejects_in_place_rewrite_after_eocd(tmp_path, monkeypatch):
    """A held descriptor alone is insufficient when its bytes are rewritten."""
    import os
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    previous = package.stat()
    original_zipfile = zipfile.ZipFile
    called = []

    def mutate_open_file(file, *args, **kwargs):
        called.append(1)
        # Rewrite one stored payload byte, preserving the length and mtime.
        with package.open("r+b") as writable:
            payload = writable.read()
            offset = payload.index(b"{}")
            writable.seek(offset)
            writable.write(b"[]")
            writable.flush()
            os.fsync(writable.fileno())
        os.utime(package, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        return original_zipfile(file, *args, **kwargs)

    monkeypatch.setattr(module.zipfile, "ZipFile", mutate_open_file)
    with pytest.raises(module.WindowsUpdateSnapshotError, match="changed during preflight"):
        module._require_bounded_zip_members(package)
    assert called == [1]

@pytest.mark.parametrize("forged_report_field", [
    {"install_authorized": True},
    {"install_authorized": False},
    {"real_money_execution": True},
    {"human_tested": True},
    {"nvda_verified": True},
    {"transaction_prepared": True},
    {"file_count": True},
    {"autosport_exe_sha256": "not-a-digest"},
])
def test_deep_verifier_report_cannot_mint_update_or_product_authority(
    tmp_path, monkeypatch, release_package, forged_report_field,
):
    """A future verifier/report extension cannot grant positive authority.

    Update admission consumes this report, so fail closed on unexpected
    authority-bearing fields, not just the verified archive SHA.
    """
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    report = {
        "status": "PASS",
        "source_sha": "a" * 40,
        "package_sha256": digest(b"fixture"),
        **forged_report_field,
    }
    monkeypatch.setattr(
        release_package, "verify_windows_package", lambda *a, **k: report,
        raising=False,
    )
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="verifier report"):
            module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)


def test_deep_verifier_report_allows_exact_negative_truth_labels(
    tmp_path, monkeypatch, release_package,
):
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        release_package, "verify_windows_package",
        lambda *a, **k: {
            "status": "PASS", "source_sha": "a" * 40,
            "package_sha256": digest(b"fixture"),
            "real_money_execution": False, "human_tested": False,
            "nvda_verified": False, "file_count": 3,
            "autosport_exe_sha256": "b" * 64,
        }, raising=False,
    )
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        result = module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert result["real_money_execution"] is False
        assert result["file_count"] == 3


class _AdversarialReportEquality:
    """A verifier result is not allowed to supply user-defined equality code."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


@pytest.mark.parametrize("field", ["status", "source_sha", "package_sha256"])
def test_verifier_report_rejects_custom_equality_values(
    tmp_path, monkeypatch, release_package, field,
):
    """A hostile report object must not impersonate PASS or a pinned digest."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    report = {
        "status": "PASS", "source_sha": "a" * 40,
        "package_sha256": digest(b"fixture"),
    }
    report[field] = _AdversarialReportEquality()
    monkeypatch.setattr(release_package, "verify_windows_package", lambda *a, **k: report, raising=False)
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="verifier report"):
            module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)


@pytest.mark.parametrize("field", ["status", "source_sha", "package_sha256"])
def test_verifier_report_rejects_str_subclasses(
    tmp_path, monkeypatch, release_package, field,
):
    from autosport import windows_update_snapshot as module

    class ForgedStr(str):
        pass

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    report = {
        "status": "PASS", "source_sha": "a" * 40,
        "package_sha256": digest(b"fixture"),
    }
    report[field] = ForgedStr(report[field])
    monkeypatch.setattr(release_package, "verify_windows_package", lambda *a, **k: report, raising=False)
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="verifier report"):
            module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)


def test_verifier_report_rejects_subclassed_field_name_before_equality(
    tmp_path, monkeypatch, release_package,
):
    from autosport import windows_update_snapshot as module

    class HostileKey(str):
        def __eq__(self, other):
            raise AssertionError("untrusted key equality executed")

        __hash__ = str.__hash__

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    report = {
        "status": "PASS", "source_sha": "a" * 40,
        "package_sha256": digest(b"fixture"),
        HostileKey("install_authorized"): False,
    }
    monkeypatch.setattr(release_package, "verify_windows_package", lambda *a, **k: report, raising=False)
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="unknown authority"):
            module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)

class _ForgedExpectedSourceSha(str):
    """A caller must not control equality during source-identity admission."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


@pytest.mark.parametrize("bad_source_sha", [
    _ForgedExpectedSourceSha("b" * 40),
    _ForgedExpectedSourceSha("a" * 40),
    _AdversarialReportEquality(),
    "B" * 40,
    "a" * 39,
    True,
    None,
])
def test_expected_source_sha_rejected_before_deep_verifier(
    tmp_path, monkeypatch, release_package, bad_source_sha,
):
    """Reject noncanonical caller identity before any verifier call or custom equality."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    calls = []

    def deep_verifier(path, *, expected_source_sha):
        calls.append(expected_source_sha)
        return {
            "status": "PASS", "source_sha": "a" * 40,
            "package_sha256": digest(b"fixture"),
        }

    monkeypatch.setattr(release_package, "verify_windows_package", deep_verifier, raising=False)
    monkeypatch.setattr(module, "_require_bounded_zip_members", lambda *a: None)
    monkeypatch.setattr(module, "_verified_embedded_update_identity", lambda *a, **k: {})
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="source SHA"):
            module.verify_pinned_release_package(snapshot, expected_source_sha=bad_source_sha)
    assert calls == []


@pytest.mark.parametrize("exception", [
    EOFError("private-path /Users/secret truncated archive"),
    RuntimeError("private-path /Users/secret decompressor failed"),
    struct.error("private-path /Users/secret malformed header"),
])
def test_canonical_verifier_low_level_archive_failure_is_sanitized_and_cleaned(
    tmp_path, monkeypatch, release_package, exception,
):
    """Malformed archive internals must not escape as raw error/secret paths."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "оновлення з пробілами.zip"
    source.write_bytes(b"untrusted archive bytes")
    paths = []

    def failing_verifier(path, *, expected_source_sha):
        paths.append(path)
        assert path.exists()
        raise exception

    monkeypatch.setattr(release_package, "verify_windows_package", failing_verifier, raising=False)
    with pytest.raises(module.WindowsUpdateSnapshotError, match="^canonical package verification failed$") as error:
        with module.pinned_windows_update_package(
            source, expected_sha256=digest(source.read_bytes())
        ) as snap:
            module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)
    assert "private-path" not in str(error.value)
    import traceback
    assert "private-path" not in "".join(traceback.format_exception(error.value))
    assert len(paths) == 1
    assert not paths[0].exists(), "failed admission must delete ephemeral snapshot"


@pytest.mark.parametrize("exception", [
    EOFError("secret archive directory truncated"),
    RuntimeError("secret ZIP decompressor error"),
    struct.error("secret central directory unpack failed"),
])
def test_zip_preflight_low_level_failure_is_sanitized(tmp_path, monkeypatch, exception):
    """ZIP reader's low-level parsing failures are rejected at the boundary."""
    from autosport import windows_update_snapshot as module
    import zipfile

    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")

    def failed_reader(*args, **kwargs):
        raise exception

    with monkeypatch.context() as patch:
        patch.setattr(module.zipfile, "ZipFile", failed_reader)
        with pytest.raises(module.WindowsUpdateSnapshotError, match="^update archive preflight failed$") as error:
            module._require_bounded_zip_members(package)
    assert "secret" not in str(error.value)
    import traceback
    assert "secret" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("exception", [
    EOFError("secret embedded BUILD_INFO truncated"),
    RuntimeError("secret decompression error"),
    struct.error("secret malformed metadata header"),
])
def test_embedded_update_identity_low_level_failure_is_sanitized(
    tmp_path, monkeypatch, exception,
):
    """Metadata parsing cannot leak raw ZIP reader exceptions to callers."""
    from autosport import windows_update_snapshot as module

    def failed_reader(*args, **kwargs):
        raise exception

    import zipfile
    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    with module.pinned_windows_update_package(package, expected_sha256=digest(package.read_bytes())) as snap:
        with monkeypatch.context() as patch:
            patch.setattr(module.zipfile, "ZipFile", failed_reader)
            with pytest.raises(module.WindowsUpdateSnapshotError, match="^embedded update identity is invalid$") as error:
                module._verified_embedded_update_identity(
                    snap, expected_source_sha="a" * 40
                )
    assert "secret" not in str(error.value)
    import traceback
    assert "secret" not in "".join(traceback.format_exception(error.value))


def test_canonical_verifier_keyboard_interrupt_not_swallowed(tmp_path, monkeypatch, release_package):
    """Program interruption must remain interruptible, never falsely PASS."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(release_package, "verify_windows_package", interrupted, raising=False)
    with module.pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(KeyboardInterrupt):
            module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)



def test_source_open_failure_does_not_log_private_os_error(tmp_path, monkeypatch):
    """Normal exception tracebacks must not expose OS-provided private paths."""
    import traceback
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"candidate")
    def reject_open(*args, **kwargs):
        raise OSError("PRIVATE_MARKER password-folder source access denied")
    monkeypatch.setattr(module, "_open_archive_fd", reject_open)
    with pytest.raises(module.WindowsUpdateSnapshotError, match="cannot open package source") as error:
        with module.pinned_windows_update_package(
            source, expected_sha256=digest(source.read_bytes())
        ):
            pytest.fail("unreadable source must not be accepted")
    assert "PRIVATE_MARKER" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("source_type", [str, Path])
def test_relative_source_is_anchored_before_concurrent_cwd_change(tmp_path, monkeypatch, source_type):
    """Changing process cwd between lstat/open must not retarget the source.

    A service may have legacy code that changes cwd while an update is being
    pinned.  The pinned source must remain the caller's original filesystem
    object rather than the same relative name under the new cwd.
    """
    import os
    from autosport import windows_update_snapshot as module

    original = tmp_path / "перший каталог"
    alternate = tmp_path / "другий каталог"
    original.mkdir()
    alternate.mkdir()
    expected = b"trusted original update bytes"
    (original / "candidate.zip").write_bytes(expected)
    (alternate / "candidate.zip").write_bytes(b"untrusted alternate bytes")
    old_cwd = Path.cwd()
    raw_open = module._open_archive_fd
    observed = []

    def switch_cwd_on_open(path, *args, **kwargs):
        if not observed:
            observed.append(Path(os.fspath(path)))
            os.chdir(alternate)
        return raw_open(path, *args, **kwargs)

    try:
        os.chdir(original)
        with monkeypatch.context() as patch:
            patch.setattr(module, "_open_archive_fd", switch_cwd_on_open)
            with pinned_windows_update_package(
                source_type("candidate.zip"), expected_sha256=digest(expected)
            ) as snapshot:
                assert snapshot.path.read_bytes() == expected
                snapshot.recheck()
        assert len(observed) == 1
        assert observed[0] == original / "candidate.zip"
    finally:
        os.chdir(old_cwd)


def test_relative_source_remains_anchored_after_copy_callback_changes_cwd(tmp_path, monkeypatch):
    """The post-copy path witness must not follow a changed process cwd."""
    import os
    from autosport import windows_update_snapshot as module

    original = tmp_path / "початковий шлях"
    alternate = tmp_path / "інший шлях"
    original.mkdir()
    alternate.mkdir()
    payload = b"stable original bytes"
    (original / "candidate.zip").write_bytes(payload)
    (alternate / "candidate.zip").write_bytes(b"different alternate bytes")
    old_cwd = Path.cwd()
    raw_fdopen = os.fdopen
    changed = []

    class CwdChangingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, *args):
            if not changed:
                changed.append(True)
                os.chdir(alternate)
            return self.stream.read(*args)

    def fdopen_with_cwd_change(fd, *args, **kwargs):
        stream = raw_fdopen(fd, *args, **kwargs)
        # The first fdopen is the source. Later snapshot.recheck calls must
        # retain the standard stream semantics.
        if not changed:
            return CwdChangingReader(stream)
        return stream

    try:
        os.chdir(original)
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "fdopen", fdopen_with_cwd_change)
            with pinned_windows_update_package(
                Path("candidate.zip"), expected_sha256=digest(payload)
            ) as snapshot:
                assert snapshot.path.read_bytes() == payload
                snapshot.recheck()
        assert changed == [True]
    finally:
        os.chdir(old_cwd)


def test_zip_preflight_invalid_utf8_filename_is_sanitized_before_verifier(tmp_path, monkeypatch):
    """Untrusted ZIP filename decoding must not escape as raw UnicodeDecodeError.

    The caller's diagnostics may be persisted in Windows logs; raw codec
    exceptions can contain attacker-controlled archive bytes.  The canonical
    verifier must not be called on a malformed central-directory filename.
    """
    import struct
    import sys
    import types
    import zipfile

    from autosport.windows_update_snapshot import (
        WindowsUpdateSnapshotError, pinned_windows_update_package,
        verify_pinned_release_package,
    )

    package = tmp_path / "кандидат з некоректним UTF-8.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("x.txt", b"safe")
    data = bytearray(package.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    for offset in (local + 6, central + 8):
        flags = struct.unpack_from("<H", data, offset)[0]
        struct.pack_into("<H", data, offset, flags | 0x800)
    data[local + 30] = 0xff
    data[central + 46] = 0xff
    package.write_bytes(data)

    called = []
    fake_release = types.ModuleType("autosport.release_package")
    fake_release.verify_windows_package = lambda *a, **kw: called.append(True)
    monkeypatch.setitem(sys.modules, "autosport.release_package", fake_release)
    with pinned_windows_update_package(
        package, expected_sha256=hashlib.sha256(data).hexdigest()
    ) as snapshot:
        with pytest.raises(WindowsUpdateSnapshotError, match="update archive preflight failed") as error:
            verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert error.value.__cause__ is None
    assert called == []

@pytest.mark.parametrize("bad_path", ["invalid-\ud800.zip", "invalid-\udfff.zip"])
def test_malformed_unicode_source_path_is_sanitized(bad_path):
    # Some Windows/POSIX filesystem encoders raise UnicodeError rather than
    # OSError.  The update admission API must never expose the codec traceback.
    with pytest.raises(WindowsUpdateSnapshotError, match="package source"):
        with pinned_windows_update_package(bad_path, expected_sha256="0" * 64):
            pytest.fail("invalid path must not yield a snapshot")


def test_source_lstat_unicode_failure_is_sanitized(tmp_path, monkeypatch):
    source = tmp_path / "candidate.zip"
    source.write_bytes(b"candidate")
    original_lstat = Path.lstat
    def broken_lstat(path, *args, **kwargs):
        if path == source:
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogate")
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", broken_lstat)
    with pytest.raises(WindowsUpdateSnapshotError, match="package source"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"candidate")):
            pytest.fail("lstat codec failure must not yield a snapshot")


def test_embedded_update_identity_cannot_read_replaced_zip_between_rechecks(
    tmp_path, monkeypatch, release_package,
):
    """A transient path swap cannot substitute a different signed-update identity.

    The deep verifier has already attested the original package bytes.  The
    attacker redirects only the subsequent ZIP reopen, then restores the path
    before the final snapshot.recheck().  The old implementation accepted the
    substituted BUILD_INFO even though both rechecks passed.
    """
    import json
    import zipfile
    from autosport import windows_update_snapshot as module

    original = tmp_path / "original.zip"
    attacker = tmp_path / "attacker.zip"
    for path, version in ((original, "1.2.0"), (attacker, "99.0.0")):
        info = {
            "product": "Autosport", "source_sha": "a" * 40,
            "version": version, "update_version": version, "update_schema": 2,
        }
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(info))
    original_digest = digest(original.read_bytes())
    monkeypatch.setattr(
        release_package, "verify_windows_package",
        lambda path, *, expected_source_sha: {
            "status": "PASS", "package_sha256": original_digest,
            "source_sha": expected_source_sha,
        },
        raising=False,
    )
    real_zip = zipfile.ZipFile
    redirect_count = 0

    def transient_replacement(file, *args, **kwargs):
        nonlocal redirect_count
        if isinstance(file, (str, Path)) and Path(file).name == "candidate.zip":
            redirect_count += 1
            return real_zip(attacker, *args, **kwargs)
        return real_zip(file, *args, **kwargs)

    with pinned_windows_update_package(original, expected_sha256=original_digest) as snap:
        with monkeypatch.context() as patch:
            patch.setattr(module.zipfile, "ZipFile", transient_replacement)
            report = module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert report["update_version"] == "1.2.0", (
            "transient pathname substitution must not inject a different update version"
        )
    # The regression only matters when the old vulnerable path-based ZIP
    # reopen is actually exercised; after the fix no pathname reopen is used.
    assert redirect_count == 0


def test_embedded_identity_rejects_wrong_inode_even_if_zip_payload_is_valid(
    tmp_path, monkeypatch,
):
    """A substituted open descriptor cannot supply valid-looking update metadata."""
    import json
    import os
    import zipfile
    from autosport import windows_update_snapshot as module

    original = tmp_path / "source.zip"
    decoy = tmp_path / "decoy.zip"
    for path, version in ((original, "1.0.0"), (decoy, "8.0.0")):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps({
                "product": "Autosport", "source_sha": "a" * 40,
                "version": version, "update_version": version, "update_schema": 1,
            }))
    real_open = module._open_archive_fd
    with module.pinned_windows_update_package(original, expected_sha256=digest(original.read_bytes())) as snap:
        def redirected_open(path):
            if Path(path) == snap.path:
                return real_open(decoy)
            return real_open(path)

        with monkeypatch.context() as patch:
            patch.setattr(module, "_open_archive_fd", redirected_open)
            with pytest.raises(module.WindowsUpdateSnapshotError, match="^embedded update identity is invalid$"):
                module._verified_embedded_update_identity(snap, expected_source_sha="a" * 40)
        snap.recheck()

@pytest.mark.parametrize("backend_failure", ["module_missing", "verifier_symbol_missing"])
def test_missing_canonical_verifier_backend_fails_closed_without_private_details(
    tmp_path, monkeypatch, release_package, backend_failure
):
    """A damaged deployment must not leak an ImportError or imply package PASS."""
    import builtins
    import types
    import sys
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "private user path candidate.zip"
    source.write_bytes(b"fixture")
    if backend_failure == "module_missing":
        original_import = builtins.__import__

        def unavailable(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "release_package" and level == 1:
                raise ModuleNotFoundError("private user path / credentials / missing backend")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", unavailable)
    else:
        missing = types.ModuleType("autosport.release_package")
        monkeypatch.setitem(sys.modules, "autosport.release_package", missing)
        monkeypatch.setattr(
            __import__("autosport", fromlist=["release_package"]),
            "release_package", missing, raising=False,
        )

    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError, match="canonical package verification backend unavailable") as exc:
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert "private user path" not in str(exc.value)
        assert "credentials" not in str(exc.value)
    assert not snap.path.exists()


def test_snapshot_recheck_unicode_filesystem_failure_is_sanitized_and_cleanup_survives(
    tmp_path, monkeypatch
):
    """Windows path codec failures at recheck are integrity failures, not trace leaks."""
    source = tmp_path / "кандидат.zip"
    source.write_bytes(b"trusted")
    original_lstat = Path.lstat
    armed = False
    snap_path = None

    def fail_once(self, *args, **kwargs):
        nonlocal armed
        if armed and self == snap_path:
            armed = False
            raise UnicodeEncodeError("utf-8", "private path: \ud800", 14, 15, "invalid")
        return original_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fail_once)
    with pinned_windows_update_package(source, expected_sha256=digest(b"trusted")) as snap:
        snap_path = snap.path
        armed = True
        with pytest.raises(WindowsUpdateSnapshotError, match="pinned package unreadable") as exc:
            snap.recheck()
        assert "private path" not in str(exc.value)
    assert snap_path is not None and not snap_path.exists()

@pytest.mark.parametrize("declared_count", [1, 2])
def test_zip_preflight_rejects_eocd_member_count_disagreement(tmp_path, declared_count):
    """The EOCD count must agree with the directory actually parsed.

    Python's ZipFile reads all central entries even when the EOCD declares
    fewer. Accepting this discrepancy weakens the bounded member-count
    witness before the canonical release verifier materializes members.
    """
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "inconsistent eocd.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ("a", "b", "c"):
            archive.writestr(f"Autosport-V1/{name}.txt", name.encode("ascii"))
    payload = bytearray(package.read_bytes())
    eocd_offset = len(payload) - module._ZIP_EOCD.size
    signature, disk, directory_disk, count_on_disk, count, size, offset, comment = (
        module._ZIP_EOCD.unpack_from(payload, eocd_offset)
    )
    assert signature == 0x06054B50 and count == count_on_disk == 3
    module._ZIP_EOCD.pack_into(
        payload, eocd_offset, signature, disk, directory_disk,
        declared_count, declared_count, size, offset, comment,
    )
    package.write_bytes(payload)
    # Verify this is the intended discrepancy, not a malformed central dir.
    with zipfile.ZipFile(package, "r") as archive:
        assert len(archive.infolist()) == 3
    with pytest.raises(module.WindowsUpdateSnapshotError, match="member count disagrees"):
        module._require_bounded_zip_members(package)


def test_zip_preflight_accepts_matching_eocd_member_count(tmp_path):
    """Count enforcement must preserve ordinary canonical ZIP inputs."""
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "matching eocd.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/a.txt", b"a")
        archive.writestr("Autosport-V1/b.txt", b"b")
    module._require_bounded_zip_members(package)

@pytest.mark.parametrize("declared_count", [1, 2])
def test_eocd_member_count_mismatch_cannot_reach_deep_release_verifier(
    tmp_path, monkeypatch, declared_count
):
    """Reject under-declared central entries before the deep verifier runs."""
    import sys
    import types
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "false-count.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in ("BUILD_INFO.json", "a.txt", "b.txt"):
            archive.writestr("Autosport-V1/" + name, b"{}")
    payload = bytearray(package.read_bytes())
    position = len(payload) - module._ZIP_EOCD.size
    fields = list(module._ZIP_EOCD.unpack_from(payload, position))
    assert fields[3:5] == [3, 3]
    fields[3:5] = [declared_count, declared_count]
    module._ZIP_EOCD.pack_into(payload, position, *fields)
    package.write_bytes(payload)

    calls = []
    backend = types.ModuleType("autosport.release_package")
    backend.verify_windows_package = lambda *a, **kw: calls.append(1)
    monkeypatch.setitem(sys.modules, "autosport.release_package", backend)
    with module.pinned_windows_update_package(
        package, expected_sha256=digest(package.read_bytes())
    ) as snapshot:
        with pytest.raises(module.WindowsUpdateSnapshotError, match="member count disagrees"):
            module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert calls == [], "untrusted ZIP metadata reached the deep verifier"

@pytest.mark.parametrize("failure", [
    UnicodeDecodeError("utf-8", b"\xff", 0, 1, "C:\\private\\operator\\update.zip"),
    UnicodeEncodeError("ascii", "ї", 0, 1, "C:\\private\\operator\\update.zip"),
])
def test_deep_verifier_unicode_failure_is_redacted_and_fail_closed(
    tmp_path, monkeypatch, release_package, failure,
):
    """Malformed ZIP/BUILD_INFO text must never expose a local update path."""
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")

    def failing_verifier(*args, **kwargs):
        raise failure

    monkeypatch.setattr(release_package, "verify_windows_package", failing_verifier, raising=False)
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError) as error:
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert "C:\\private" not in str(error.value)
        assert error.value.__suppress_context__ is True

@pytest.mark.parametrize("failure", [
    ImportError("backend dependency at C:\\private\\operator\\release_package.py"),
    ModuleNotFoundError("No module named 'C:\\private\\operator\\release_package'"),
])
def test_deep_verifier_runtime_import_failure_is_redacted_and_fail_closed(
    tmp_path, monkeypatch, release_package, failure,
):
    """A lazy verifier dependency may fail after the public entry was imported."""
    from autosport.windows_update_snapshot import verify_pinned_release_package

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")

    def failing_verifier(*args, **kwargs):
        raise failure

    monkeypatch.setattr(release_package, "verify_windows_package", failing_verifier, raising=False)
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        with pytest.raises(WindowsUpdateSnapshotError) as error:
            verify_pinned_release_package(snap, expected_source_sha="a" * 40)
        assert "C:\\private" not in str(error.value)
        assert error.value.__suppress_context__ is True

@pytest.mark.parametrize("error_type", [ImportError, ModuleNotFoundError])
def test_embedded_update_metadata_dependency_import_failure_is_redacted(
    tmp_path, monkeypatch, error_type,
):
    """A damaged update-admission module cannot leak installed Windows paths.

    This is the post-verifier metadata stage, not the already guarded import
    of the canonical release verifier itself.
    """
    import builtins
    import json
    import traceback
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "candidate.zip"
    build_info = {
        "product": "Autosport", "source_sha": "a" * 40,
        "version": "1.2.3", "update_version": "1.2.3", "update_schema": 2,
    }
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps(build_info))
    original_import = builtins.__import__

    def damaged_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "windows_update_admission" and level == 1:
            raise error_type("C:\\private\\operator\\autosport\\windows_update_admission.py")
        return original_import(name, globals, locals, fromlist, level)

    with module.pinned_windows_update_package(
        package, expected_sha256=digest(package.read_bytes())
    ) as snapshot:
        with monkeypatch.context() as patch:
            patch.setattr(builtins, "__import__", damaged_import)
            with pytest.raises(module.WindowsUpdateSnapshotError) as raised:
                module._verified_embedded_update_identity(
                    snapshot, expected_source_sha="a" * 40
                )
    assert str(raised.value) == "embedded update identity is invalid"
    assert raised.value.__suppress_context__ is True
    assert "C:\\private" not in "".join(traceback.format_exception(raised.value))

@pytest.mark.parametrize("stage", ["backend_import", "verifier_call"])
def test_canonical_verifier_syntax_failure_does_not_expose_private_install_path(
    tmp_path, monkeypatch, release_package, stage,
):
    """A damaged installed verifier or lazy dependency must fail closed."""
    import builtins
    import traceback
    from autosport import windows_update_snapshot as module

    package = tmp_path / "candidate.zip"
    package.write_bytes(b"fixture")
    secret_path = "C:\\private\\operator\\release_package.py"
    failure = SyntaxError("invalid syntax", (secret_path, 3, 2, "password = secret", 3, 9))
    original_import = builtins.__import__

    def damaged_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "release_package" and level == 1:
            raise failure
        return original_import(name, globals, locals, fromlist, level)

    with module.pinned_windows_update_package(
        package, expected_sha256=digest(package.read_bytes())
    ) as snapshot:
        with monkeypatch.context() as patch:
            if stage == "backend_import":
                patch.setattr(builtins, "__import__", damaged_import)
            else:
                def damaged_verifier(*args, **kwargs):
                    raise failure
                patch.setattr(release_package, "verify_windows_package", damaged_verifier, raising=False)
            with pytest.raises(module.WindowsUpdateSnapshotError) as raised:
                module.verify_pinned_release_package(snapshot, expected_source_sha="a" * 40)
    assert raised.value.__suppress_context__ is True
    assert "C:\\private" not in "".join(traceback.format_exception(raised.value))
    assert "password = secret" not in str(raised.value)


def test_package_source_rejects_symlinked_parent_directory(tmp_path):
    """A regular ZIP reached through a redirected ancestor is not a local pin."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "candidate.zip").write_bytes(b"signed-candidate-fixture")
    alias = tmp_path / "redirected"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(WindowsUpdateSnapshotError, match="redirected|symlink"):
        with pinned_windows_update_package(
            alias / "candidate.zip", expected_sha256=digest(b"signed-candidate-fixture")
        ):
            pytest.fail("a redirected ancestor must not yield a package snapshot")


def test_package_source_rejects_symlinked_grandparent_directory(tmp_path):
    real = tmp_path / "real"
    (real / "nested").mkdir(parents=True)
    (real / "nested" / "candidate.zip").write_bytes(b"signed-candidate-fixture")
    alias = tmp_path / "redirected"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(WindowsUpdateSnapshotError, match="redirected|symlink"):
        with pinned_windows_update_package(
            alias / "nested" / "candidate.zip", expected_sha256=digest(b"signed-candidate-fixture")
        ):
            pytest.fail("a redirected grandparent must not yield a package snapshot")


def test_package_source_rejects_parent_replacement_during_open(tmp_path, monkeypatch):
    """A same-byte ZIP in a different parent inode is not the selected ZIP."""
    import os
    from autosport import windows_update_snapshot as module

    active = tmp_path / "active"
    other = tmp_path / "other"
    active.mkdir()
    other.mkdir()
    source = active / "candidate.zip"
    source.write_bytes(b"same signed bytes")
    (other / "candidate.zip").write_bytes(b"same signed bytes")
    original_open = module._open_archive_fd
    swapped = False

    def replace_parent(path):
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            active.rename(tmp_path / "original")
            other.rename(active)
        return original_open(path)

    monkeypatch.setattr(module, "_open_archive_fd", replace_parent)
    with pytest.raises(WindowsUpdateSnapshotError, match="ancestor changed during open"):
        with pinned_windows_update_package(source, expected_sha256=digest(b"same signed bytes")):            pytest.fail("same-byte parent replacement must not yield snapshot")
    assert swapped


def test_nested_unicode_package_without_redirection_is_accepted(tmp_path):
    directory = tmp_path / "Пакети з пробілами" / "Оновлення"
    directory.mkdir(parents=True)
    source = directory / "новий пакет.zip"
    source.write_bytes(b"verified fixture")
    with pinned_windows_update_package(source, expected_sha256=digest(b"verified fixture")) as snap:
        assert snap.sha256 == digest(b"verified fixture")
        assert snap.install_authorized is False


@pytest.mark.parametrize("unsafe", [
    r"\\server\share\candidate.zip",
    "//server/share/candidate.zip",
    r"\\?\C:\\private\candidate.zip",
    r"C:drive-relative.zip",
    "folder/../candidate.zip",
])
def test_snapshot_rejects_nonlocal_or_ambiguous_path_before_any_filesystem_access(monkeypatch, unsafe):
    """Direct snapshot callers must not bypass the CLI's local-only path gate."""
    import autosport.windows_update_snapshot as snapshot_module

    def forbidden_parent_probe(_path):
        pytest.fail("untrusted update path reached filesystem inspection")

    monkeypatch.setattr(snapshot_module, "_source_parent_identity", forbidden_parent_probe)
    with pytest.raises(WindowsUpdateSnapshotError):
        with pinned_windows_update_package(unsafe, expected_sha256="a" * 64):
            pytest.fail("untrusted update path was admitted")


def test_snapshot_rejects_path_subclass_before_untrusted_fspath_callback():
    """A direct caller cannot inject a mutable/custom path protocol into admission."""
    class HostilePath(Path):
        def __fspath__(self):
            raise AssertionError("untrusted path callback executed")

    with pytest.raises(WindowsUpdateSnapshotError, match="package path must be a local path"):
        with pinned_windows_update_package(HostilePath("candidate.zip"), expected_sha256="a" * 64):
            pytest.fail("subclass was admitted")


def test_snapshot_rejects_string_subclass_before_pathname_probe(monkeypatch):
    """Reject noncanonical source types consistently with installed trust."""
    class HostileString(str):
        pass

    import autosport.windows_update_snapshot as snapshot_module
    def forbidden_parent_probe(_path):
        pytest.fail("subclass source reached filesystem inspection")

    monkeypatch.setattr(snapshot_module, "_source_parent_identity", forbidden_parent_probe)
    with pytest.raises(WindowsUpdateSnapshotError, match="package path must be a local path"):
        with pinned_windows_update_package(HostileString("candidate.zip"), expected_sha256="a" * 64):
            pytest.fail("subclass was admitted")


def test_private_snapshot_recheck_rejects_redirected_parent_after_pin(tmp_path):
    """A renamed private directory plus symlink must not preserve pin authority.

    The ZIP bytes and inode remain unchanged; only an ancestor becomes a
    redirection after the initial pin. Restore the directory in finally so
    the TemporaryDirectory owner can still clean up its own files.
    """
    import os

    source = tmp_path / "оновлення.zip"
    source.write_bytes(b"signed-update-fixture")
    with pinned_windows_update_package(
        source, expected_sha256=digest(source.read_bytes())
    ) as snapshot:
        parent = snapshot.path.parent
        relocated = parent.with_name(parent.name + "-moved")
        parent.rename(relocated)
        try:
            try:
                parent.symlink_to(relocated, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("directory symlinks unavailable")
            with pytest.raises(WindowsUpdateSnapshotError, match="ancestor|redirect"):
                snapshot.recheck()
        finally:
            if parent.is_symlink():
                parent.unlink()
            relocated.rename(parent)
        snapshot.recheck()


def test_private_snapshot_recheck_rejects_ancestor_swap_during_descriptor_open(tmp_path, monkeypatch):
    """A parent redirect inserted after the first witness must fail closed."""
    import os
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"same signed bytes")
    with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snapshot:
        parent = snapshot.path.parent
        moved = parent.with_name(parent.name + "-original")
        original_open = module._open_archive_fd
        swapped = False

        def swap_at_open(path):
            nonlocal swapped
            if Path(path) == snapshot.path and not swapped:
                parent.rename(moved)
                try:
                    parent.symlink_to(moved, target_is_directory=True)
                except BaseException:
                    moved.rename(parent)
                    raise
                swapped = True
            return original_open(path)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(module, "_open_archive_fd", swap_at_open)
                with pytest.raises(WindowsUpdateSnapshotError, match="ancestor|redirect|unreadable"):
                    snapshot.recheck()
        finally:
            if parent.is_symlink():
                parent.unlink()
            if moved.exists():
                moved.rename(parent)
        assert swapped
        snapshot.recheck()


def test_zip_resource_preflight_rejects_ancestor_redirect_during_parse(tmp_path, monkeypatch):
    """A stable ZIP inode cannot excuse a renamed, symlinked ancestor.

    A ZIP descriptor opened before a directory swap still parses the original
    bytes; the pathname afterwards also resolves to the same inode. Without
    an ancestor witness the direct resource guard silently returns success.
    """
    import zipfile
    from autosport import windows_update_snapshot as module

    parent = tmp_path / "оригінальний каталог"
    parent.mkdir()
    package = parent / "candidate.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    relocated = tmp_path / "переміщений каталог"
    original_zipfile = zipfile.ZipFile
    swapped = []

    def redirect_ancestor(file, *args, **kwargs):
        if not swapped:
            parent.rename(relocated)
            try:
                parent.symlink_to(relocated, target_is_directory=True)
            except (OSError, NotImplementedError):
                relocated.rename(parent)
                pytest.skip("directory symlinks unavailable")
            swapped.append(True)
        return original_zipfile(file, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.zipfile, "ZipFile", redirect_ancestor)
            with pytest.raises(WindowsUpdateSnapshotError, match="ancestor|redirect"):
                module._require_bounded_zip_members(package)
        assert swapped
    finally:
        if parent.is_symlink():
            parent.unlink()
        if relocated.exists():
            relocated.rename(parent)
    # A restored original tree remains a valid bounded archive.
    module._require_bounded_zip_members(package)


def test_zip_resource_guard_rejects_symlink_leaf_before_open(tmp_path, monkeypatch):
    """Direct resource guard must not open a redirected leaf even briefly."""
    import zipfile
    from autosport import windows_update_snapshot as module

    target = tmp_path / "target.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    link = tmp_path / "alias.zip"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable")

    original_open = module.Path.open
    observed = []

    def guarded_open(path, *args, **kwargs):
        if path == link:
            observed.append(True)
            raise AssertionError("redirected leaf was opened")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.Path, "open", guarded_open)
        with pytest.raises(WindowsUpdateSnapshotError, match="redirect|regular"):
            module._require_bounded_zip_members(link)
    assert not observed
    module._require_bounded_zip_members(target)


def test_zip_resource_guard_rejects_leaf_swap_at_open_before_eocd(tmp_path, monkeypatch):
    """The direct ZIP resource guard must never follow a swapped leaf."""
    import os
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "candidate.zip"
    replacement = tmp_path / "replacement.zip"
    for path in (package, replacement):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    original_open = os.open
    swapped = []
    opened_flags = []

    def replace_during_open(path, flags, *args, **kwargs):
        if path == package:
            opened_flags.append(flags)
            if not swapped:
                package.unlink()
                try:
                    package.symlink_to(replacement)
                except (OSError, NotImplementedError):
                    with zipfile.ZipFile(package, "w") as archive:
                        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
                    pytest.skip("file symlinks unavailable")
                swapped.append(True)
        return original_open(path, flags, *args, **kwargs)

    def unexpected_parse(*args, **kwargs):
        raise AssertionError("redirected leaf reached ZIP parser")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "open", replace_during_open)
            patch.setattr(module.zipfile, "ZipFile", unexpected_parse)
            with pytest.raises(WindowsUpdateSnapshotError, match="preflight|redirect|identity|regular"):
                module._require_bounded_zip_members(package)
        assert swapped, "the ZIP guard did not use descriptor-bound os.open"
        assert opened_flags
        if getattr(os, "O_NOFOLLOW", 0):
            assert all(flags & os.O_NOFOLLOW for flags in opened_flags)
    finally:
        if package.is_symlink():
            package.unlink()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    module._require_bounded_zip_members(package)


def test_zip_resource_guard_opens_stable_leaf_via_descriptor(tmp_path, monkeypatch):
    """A valid ZIP must use the same non-follow descriptor path as rejection."""
    import os
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "оновлення з пробілами.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    original_open = os.open
    opened = []

    def observed_open(path, flags, *args, **kwargs):
        if path == package:
            opened.append(flags)
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "open", observed_open)
        module._require_bounded_zip_members(package)
    assert len(opened) == 1, "ZIP guard must open the source exactly once"
    if getattr(os, "O_NOFOLLOW", 0):
        assert opened[0] & os.O_NOFOLLOW



def test_zip_resource_guard_fdopen_failure_releases_raw_descriptor(tmp_path, monkeypatch):
    """An exhausted file-object allocator must not leak the ZIP raw handle."""
    import errno
    import os
    import zipfile
    from autosport import windows_update_snapshot as module

    package = tmp_path / "candidate.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    original_open = module._open_archive_fd
    original_fdopen = os.fdopen
    owned = []

    def record_open(path):
        fd = original_open(path)
        if path == package:
            owned.append(fd)
        return fd

    def fail_fdopen(fd, *args, **kwargs):
        if fd in owned:
            raise OSError(errno.EMFILE, "simulated descriptor exhaustion")
        return original_fdopen(fd, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_archive_fd", record_open)
        patch.setattr(module.os, "fdopen", fail_fdopen)
        with pytest.raises(WindowsUpdateSnapshotError, match="preflight"):
            module._require_bounded_zip_members(package)
    assert len(owned) == 1
    with pytest.raises(OSError):
        os.fstat(owned[0])
    module._require_bounded_zip_members(package)


def test_zip_resource_guard_malformed_zip_closes_raw_descriptor(tmp_path, monkeypatch):
    """Malformed EOCD must not leave an open file descriptor after rejection."""
    import os
    from autosport import windows_update_snapshot as module

    package = tmp_path / "broken.zip"
    package.write_bytes(b"not a zip file with a valid end record")
    original_open = module._open_archive_fd
    owned = []

    def record_open(path):
        fd = original_open(path)
        if path == package:
            owned.append(fd)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_archive_fd", record_open)
        with pytest.raises(WindowsUpdateSnapshotError):
            module._require_bounded_zip_members(package)
    assert len(owned) == 1
    with pytest.raises(OSError):
        os.fstat(owned[0])

@pytest.mark.parametrize("mutated_field,mutated_value", [
    ("status", "FAIL"),
    ("real_money_execution", True),
    ("autosport_exe_sha256", "b" * 64),
])
def test_verified_report_cannot_be_mutated_by_embedded_identity_reader(
    tmp_path, monkeypatch, release_package, mutated_field, mutated_value,
):
    """A later callback must not rewrite an already validated verifier receipt."""
    from autosport import windows_update_snapshot as module

    source = tmp_path / "candidate.zip"
    source.write_bytes(b"fixture")
    report = {
        "status": "PASS", "source_sha": "a" * 40,
        "package_sha256": digest(b"fixture"),
        "real_money_execution": False,
        "autosport_exe_sha256": "a" * 64,
    }
    original = report.copy()
    monkeypatch.setattr(
        release_package, "verify_windows_package", lambda *a, **k: report,
        raising=False,
    )

    def mutate_after_receipt_validation(*args, **kwargs):
        report[mutated_field] = mutated_value
        return {}

    monkeypatch.setattr(module, "_verified_embedded_update_identity", mutate_after_receipt_validation)
    with pinned_windows_update_package(source, expected_sha256=digest(b"fixture")) as snap:
        attested = module.verify_pinned_release_package(snap, expected_source_sha="a" * 40)
    for field, value in original.items():
        assert attested[field] == value, "the attestation must be a validated immutable-in-time copy"


@pytest.mark.parametrize("swap_stage", ["open", "parse"])
def test_embedded_identity_rejects_ancestor_redirect_during_read(
    tmp_path, monkeypatch, swap_stage,
):
    """The BUILD_INFO reader must bind ancestors even if ZIP bytes/inode agree."""
    import json
    import zipfile
    from autosport import windows_update_snapshot as module

    source = tmp_path / "original.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", json.dumps({
            "product": "Autosport", "source_sha": "a" * 40,
            "version": "1.2.3", "update_version": "1.2.3", "update_schema": 1,
        }))
    original_open = module.os.open
    original_zipfile = zipfile.ZipFile
    with pinned_windows_update_package(source, expected_sha256=digest(source.read_bytes())) as snap:
        parent = snap.path.parent
        relocated = parent.with_name(parent.name + "-relocated")
        swapped = []
        opens = []

        def swap_parent():
            parent.rename(relocated)
            try:
                parent.symlink_to(relocated, target_is_directory=True)
            except (OSError, NotImplementedError):
                relocated.rename(parent)
                pytest.skip("directory symlinks unavailable")
            swapped.append(True)

        def redirect_during_open(path, flags, *args, **kwargs):
            if Path(path) == snap.path:
                opens.append(True)
                # The first open is snapshot.recheck(), the second is BUILD_INFO.
                if len(opens) == 2:
                    swap_parent()
            return original_open(path, flags, *args, **kwargs)

        def redirect_during_parse(file, *args, **kwargs):
            if not swapped:
                swap_parent()
            return original_zipfile(file, *args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                if swap_stage == "open":
                    patch.setattr(module.os, "open", redirect_during_open)
                else:
                    patch.setattr(module.zipfile, "ZipFile", redirect_during_parse)
                with pytest.raises(module.WindowsUpdateSnapshotError, match="^embedded update identity is invalid$"):
                    module._verified_embedded_update_identity(snap, expected_source_sha="a" * 40)
            assert swapped, "the adversarial redirect must actually execute"
        finally:
            if parent.is_symlink():
                parent.unlink()
            if relocated.exists():
                relocated.rename(parent)
        assert module._verified_embedded_update_identity(
            snap, expected_source_sha="a" * 40,
        ) == {"update_version": "1.2.3", "update_schema": 1}


@pytest.mark.parametrize("phase", ["opened", "completed"])
def test_zip_resource_guard_rejects_descriptor_reparse_attribute(tmp_path, monkeypatch, phase):
    """An NTFS reparse flag on the held descriptor must reject the ZIP.

    Windows can expose an opened reparse-point object with regular-file
    st_mode. A clean pathname lstat is not sufficient when the opened handle
    itself carries FILE_ATTRIBUTE_REPARSE_POINT. Simulate that native stat
    witness without requiring a Windows host for this negative test.
    """
    import os
    import stat
    import zipfile
    from types import SimpleNamespace
    from autosport import windows_update_snapshot as module

    package = tmp_path / "кандидат оновлення.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Autosport-V1/BUILD_INFO.json", "{}")
    real_open = os.open
    real_fstat = os.fstat
    opened = []
    calls = []

    def observe_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == package:
            opened.append(fd)
        return fd

    def marked_fstat(fd):
        result = real_fstat(fd)
        if fd not in opened:
            return result
        calls.append(fd)
        if len(calls) != (1 if phase == "opened" else 2):
            return result
        return SimpleNamespace(
            st_mode=result.st_mode,
            st_dev=result.st_dev,
            st_ino=result.st_ino,
            st_size=result.st_size,
            st_mtime_ns=result.st_mtime_ns,
            st_ctime_ns=result.st_ctime_ns,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "open", observe_open)
        patch.setattr(module.os, "fstat", marked_fstat)
        with pytest.raises(WindowsUpdateSnapshotError, match="redirect|identity|preflight"):
            module._require_bounded_zip_members(package)
    assert opened and len(calls) >= (1 if phase == "opened" else 2)
    # The rejected handle must be closed and a normal ZIP must remain usable.
    with pytest.raises(OSError):
        os.fstat(opened[0])
    module._require_bounded_zip_members(package)

@pytest.mark.parametrize("cancel", [KeyboardInterrupt, SystemExit])
def test_fdopen_constructor_cleanup_failure_preserves_cancellation(monkeypatch, cancel):
    """A secondary CRT descriptor-close failure must not swallow cancellation."""
    from autosport import windows_update_snapshot as module

    closed = []

    def interrupted_fdopen(*_args, **_kwargs):
        raise cancel("primary cancellation")

    def failing_close(fd):
        closed.append(fd)
        raise RuntimeError("PRIVATE_CLEANUP_PATH must not replace cancellation")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fdopen", interrupted_fdopen)
        patch.setattr(module.os, "close", failing_close)
        with pytest.raises(cancel, match="primary cancellation"):
            with module._owned_fd_stream(123456):
                pytest.fail("no stream may be yielded")
    assert closed == [123456]


def test_source_fdopen_cleanup_failure_does_not_leak_private_path(tmp_path, monkeypatch):
    """Two simultaneous descriptor errors must remain one sanitized rejection."""
    import hashlib
    import os
    import traceback
    from autosport import windows_update_snapshot as module

    source = tmp_path / "приватний шлях" / "кандидат.zip"
    source.parent.mkdir()
    source.write_bytes(b"fixture-only ZIP bytes")
    real_open = module._open_archive_fd
    real_fdopen = os.fdopen
    real_close = os.close
    source_fd = []

    def record_open(path):
        fd = real_open(path)
        if os.fspath(path) == os.fspath(source):
            source_fd.append(fd)
        return fd

    def fail_fdopen(fd, *args, **kwargs):
        if source_fd and fd == source_fd[-1]:
            raise OSError("PRIVATE_SOURCE_PATH fdopen allocation failed")
        return real_fdopen(fd, *args, **kwargs)

    def close_then_fail(fd):
        real_close(fd)
        if source_fd and fd == source_fd[-1]:
            raise RuntimeError("PRIVATE_CLEANUP_PATH descriptor close failed")

    with monkeypatch.context() as patch:
        patch.setattr(module, "_open_archive_fd", record_open)
        patch.setattr(module.os, "fdopen", fail_fdopen)
        patch.setattr(module.os, "close", close_then_fail)
        with pytest.raises(module.WindowsUpdateSnapshotError, match="snapshot I/O failure") as error:
            with module.pinned_windows_update_package(
                source, expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()
            ):
                pytest.fail("snapshot must not be yielded")
    assert len(source_fd) == 1
    with pytest.raises(OSError):
        os.fstat(source_fd[0])
    diagnostic = "".join(traceback.format_exception(error.value))
    assert "PRIVATE_SOURCE_PATH" not in diagnostic
    assert "PRIVATE_CLEANUP_PATH" not in diagnostic
