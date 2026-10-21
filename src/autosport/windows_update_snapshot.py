"""Bounded immutable-byte snapshot for *preflight* of Windows update packages.

This module does not verify signatures, install packages or authorize an update.
It ensures that a downstream verifier can inspect the exact bytes pinned by
an expected SHA-256 instead of following a mutable external package path.
The caller must recheck the snapshot immediately before any later use.
"""

from __future__ import annotations

import hashlib
import json
import hmac
import os
import stat
import struct
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .windows_update_local_path import UnsafeUpdatePath, require_local_update_path

_MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
# Bound decompression *before* the canonical verifier reads ZIP member bodies.
# This is an update-admission resource ceiling, not a release-format change.
_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_MEMBERS = 4096
_MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024
_ZIP_EOCD = struct.Struct("<IHHHHIIH")
_MAX_COMPRESSION_RATIO = 200


class WindowsUpdateSnapshotError(ValueError):
    """Untrusted update bytes could not be pinned to a stable local snapshot."""


def _is_redirected_file(info: os.stat_result) -> bool:
    """Reject final-file NTFS reparse objects even when st_mode is regular.

    Windows may expose a reparse-point file with a regular-file mode. The
    reparse attribute must therefore be checked separately from S_ISLNK.
    """
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _require_file_identity(info: os.stat_result) -> tuple[int, int]:
    """Refuse filesystems that cannot attest stable file-object identity.

    Some Windows/network filesystems report a zero inode. Comparing (dev, 0)
    before/after a copy cannot detect a same-content path replacement, so a
    digest-only fallback would silently defeat the anti-TOCTOU contract.
    """
    device, inode = info.st_dev, info.st_ino
    if type(device) is not int or device < 0 or type(inode) is not int or inode <= 0:
        raise WindowsUpdateSnapshotError("file identity unavailable")
    return device, inode


def _native_change_time_100ns(fd: int) -> int | None:
    """Read NT file change time, not the Windows ``st_ctime`` creation time.

    Windows Python 3.11/3.12 ``st_ctime_ns`` can mean file creation time.
    A same-byte rewrite followed by restoring last-write time can therefore
    leave the ordinary stat tuple unchanged. Windows FileBasicInfo.ChangeTime
    is an additional change witness. Unsupported filesystems/API failures are
    rejected, never downgraded to digest-only admission. On POSIX, st_ctime_ns
    already records metadata changes, so no additional witness is required.
    """
    if os.name != "nt":
        return None
    return _query_windows_change_time_100ns(fd)


def _query_windows_change_time_100ns(fd: int) -> int:
    """Query FileBasicInfo on an open Windows handle; fail closed on errors."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class FileBasicInfo(ctypes.Structure):
            _fields_ = [
                ("CreationTime", ctypes.c_longlong),
                ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong),
                ("ChangeTime", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel32.GetFileInformationByHandleEx
        query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        query.restype = wintypes.BOOL
        info = FileBasicInfo()
        handle = msvcrt.get_osfhandle(fd)
        if handle == -1 or not query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise WindowsUpdateSnapshotError("Windows file change-time unavailable")
        if type(info.ChangeTime) is not int or info.ChangeTime <= 0:
            raise WindowsUpdateSnapshotError("Windows file change-time unavailable")
        # The handle, not the pathname, is the authoritative reparse witness.
        # CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) can open the link object
        # itself; it must never be accepted as an ordinary update archive.
        if info.FileAttributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise WindowsUpdateSnapshotError("Windows reparse update handle rejected")
        return info.ChangeTime
    except WindowsUpdateSnapshotError:
        raise
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError, OverflowError):
        # Native API wrappers can raise non-OSError failures as well.  Never
        # leak an OS/CRT diagnostic containing a private path or accept an
        # archive without its descriptor-bound change-time witness.
        raise WindowsUpdateSnapshotError("Windows file change-time unavailable") from None


def _open_windows_archive_fd(path: str | Path) -> int:
    """Open a ZIP leaf as a Windows *reparse object*, never its target.

    Python's Windows ``os.open`` does not expose POSIX O_NOFOLLOW.  Use
    CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) and transfer the native handle
    to the CRT exactly once.  The subsequent descriptor-bound FileBasicInfo
    check rejects reparse objects before ZIP parsing.  No fallback to a
    path-following open is allowed if the native API is unavailable.
    """
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create.restype = wintypes.HANDLE
        close = kernel32.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        handle = create(
            str(path), 0x80000000, 0x7, None, 3,
            0x00200000, None,  # FILE_FLAG_OPEN_REPARSE_POINT
        )
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            raise WindowsUpdateSnapshotError("Windows update handle unavailable")
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            if type(fd) is not int or fd < 0:
                raise OSError("CRT handle adoption failed")
        except BaseException as exc:
            # Adoption did not complete: the native HANDLE remains ours even
            # on KeyboardInterrupt/SystemExit.  Release it before propagating
            # process cancellation; never turn cancellation into an update
            # rejection that a caller could silently retry.
            try:
                close(handle)
            except Exception:
                # Cleanup failure must not replace the original cancellation
                # or leak a native wrapper diagnostic containing a private path.
                pass
            if not isinstance(exc, Exception):
                raise
            raise WindowsUpdateSnapshotError("Windows update handle unavailable") from None
        return fd
    except WindowsUpdateSnapshotError:
        raise
    except (ImportError, OSError, AttributeError, ValueError, TypeError, RuntimeError, OverflowError):
        raise WindowsUpdateSnapshotError("Windows update handle unavailable") from None


def _open_archive_fd(path: Path, *, _platform: str | None = None) -> int:
    """Single ZIP leaf opener; Windows must never use path-following CRT open.

    The private platform override exists solely for portable dispatch tests.
    Production calls omit it and always use the actual runtime platform.
    """
    platform = os.name if _platform is None else _platform
    if platform == "nt":
        return _open_windows_archive_fd(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


@contextmanager
def _owned_fd_stream(fd: int) -> Iterator[object]:
    """Close a raw descriptor even when constructing its file object fails.

    ``os.fdopen`` takes ownership only after it returns successfully.  The
    caller must not leak a raw Windows HANDLE on constructor failure, since
    update preflight may be retried many times in one process.
    """
    try:
        stream = os.fdopen(fd, "rb", closefd=True)
    except BaseException:
        try:
            os.close(fd)
        except Exception:
            # fdopen did not take ownership. A secondary descriptor-close
            # failure (including native wrapper RuntimeError/ValueError)
            # must not mask the primary error or process cancellation.
            pass
        raise
    with stream:
        yield stream


@dataclass(frozen=True, slots=True)
class WindowsUpdateSnapshot:
    path: Path
    sha256: str
    byte_count: int
    install_authorized: bool = False
    _file_identity: tuple[int, int] | None = field(default=None, repr=False, compare=False)
    _file_metadata: tuple[int, int, int] | None = field(default=None, repr=False, compare=False)
    _change_time_100ns: int | None = field(default=None, repr=False, compare=False)
    _parent_identity: tuple[tuple[int, int], ...] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.install_authorized is not False or type(self.install_authorized) is not bool:
            raise WindowsUpdateSnapshotError("pinned snapshot cannot authorize installation")

    def recheck(self) -> None:
        """Fail closed if the private snapshot changed since its creation."""
        # Frozen dataclasses can still be forged with object.__setattr__.
        self.__post_init__()
        # Revalidate the frozen object's exact pinned metadata on every use:
        # object.__setattr__ can mutate even a frozen dataclass.
        if (
            type(self._file_identity) is not tuple
            or len(self._file_identity) != 2
            or any(type(x) is not int or x < 0 for x in self._file_identity)
            or self._file_identity[1] == 0
        ):
            raise WindowsUpdateSnapshotError("pinned package file identity changed")
        if (
            type(self.path) is not type(Path()) or not self.path.is_absolute()
            or type(self.sha256) is not str or len(self.sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.sha256)
            or type(self.byte_count) is not int
            or not 0 < self.byte_count <= _MAX_PACKAGE_BYTES
            or type(self._file_metadata) is not tuple
            or len(self._file_metadata) != 3
            or any(type(x) is not int or x < 0 for x in self._file_metadata)
            or self._file_metadata[0] != self.byte_count
            or (self._change_time_100ns is not None and
                (type(self._change_time_100ns) is not int or self._change_time_100ns <= 0))
            or (os.name == "nt" and self._change_time_100ns is None)
        ):
            raise WindowsUpdateSnapshotError("pinned package snapshot authority is invalid")
        if (
            type(self._parent_identity) is not tuple
            or len(self._parent_identity) != len(self.path.parents)
            or any(
                type(pair) is not tuple or len(pair) != 2
                or type(pair[0]) is not int or pair[0] < 0
                or type(pair[1]) is not int or pair[1] <= 0
                for pair in self._parent_identity
            )
        ):
            raise WindowsUpdateSnapshotError("pinned package ancestor authority is invalid")
        digest = hashlib.sha256()
        total = 0
        try:
            # The private snapshot directory is not exempt from redirected
            # ancestors. Preserve the entire ancestor identity from creation,
            # including during the descriptor read, so a renamed directory
            # cannot be replaced by a symlink to the original inode.
            if _source_parent_identity(self.path) != self._parent_identity:
                raise WindowsUpdateSnapshotError("pinned package ancestor changed")
            # Refuse a replacement path (including a same-byte symlink) rather
            # than silently accepting a different filesystem object.
            entry = self.path.lstat()
            entry_identity = _require_file_identity(entry)
            if not stat.S_ISREG(entry.st_mode) or _is_redirected_file(entry):
                raise WindowsUpdateSnapshotError("pinned package is not a regular file")
            if entry_identity != self._file_identity:
                raise WindowsUpdateSnapshotError("pinned package file identity changed")
            if (entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns) != self._file_metadata:
                raise WindowsUpdateSnapshotError("pinned package changed after verification")
            # Use the same no-follow leaf opener as ZIP preflight. Windows
            # CRT os.open cannot prevent a reparse-point swap after lstat.
            fd = _open_archive_fd(self.path)
            with _owned_fd_stream(fd) as stream:
                opened = os.fstat(stream.fileno())
                opened_change_time = _native_change_time_100ns(stream.fileno())
                opened_identity = _require_file_identity(opened)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or type(self._file_identity) is not tuple
                    or len(self._file_identity) != 2
                    or self._file_identity != opened_identity
                    or opened_identity != entry_identity
                    or opened_change_time != self._change_time_100ns
                    or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != self._file_metadata
                ):
                    raise WindowsUpdateSnapshotError("pinned package file identity changed")
                while block := stream.read(_CHUNK_BYTES):
                    total += len(block)
                    if total > self.byte_count:
                        raise WindowsUpdateSnapshotError("pinned package grew")
                    digest.update(block)
                # A path may be replaced *during* the read, even with the
                # identical bytes. A digest of the old opened inode would
                # otherwise approve a new, unverified object at this path.
                completed = os.fstat(stream.fileno())
                completed_change_time = _native_change_time_100ns(stream.fileno())
                after_entry = self.path.lstat()
                if _source_parent_identity(self.path) != self._parent_identity:
                    raise WindowsUpdateSnapshotError("pinned package ancestor changed during recheck")
                if (
                    not stat.S_ISREG(after_entry.st_mode) or _is_redirected_file(after_entry)
                    or _require_file_identity(after_entry) != opened_identity
                    or _require_file_identity(completed) != opened_identity
                    or completed_change_time != self._change_time_100ns
                    or (completed.st_size, completed.st_mtime_ns, completed.st_ctime_ns)
                    != self._file_metadata
                    or (after_entry.st_size, after_entry.st_mtime_ns, after_entry.st_ctime_ns)
                    != self._file_metadata
                ):
                    raise WindowsUpdateSnapshotError("pinned package changed during recheck")
        except (OSError, UnicodeError):
            # Path/codec failures can arise from Windows filesystem APIs even
            # after the source was safely pinned. Normalize them at the
            # recheck boundary rather than leaking a path-bearing exception.
            raise WindowsUpdateSnapshotError("pinned package unreadable") from None
        if total != self.byte_count or not hmac.compare_digest(digest.hexdigest(), self.sha256):
            raise WindowsUpdateSnapshotError("pinned package changed after verification")


def _source_parent_identity(path: Path) -> tuple[tuple[int, int], ...]:
    """Reject redirected ancestors and bind each parent to one file object.

    Checking only the final ZIP with lstat/O_NOFOLLOW is insufficient: a
    regular ZIP beneath a symlinked directory still follows that directory.
    Inspect from the filesystem root outward so a redirect is rejected before
    visiting any descendant. This is a preflight witness, not a substitute for
    a kernel handle-relative trusted installer transaction.
    """
    identities: list[tuple[int, int]] = []
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise WindowsUpdateSnapshotError("redirected package ancestor forbidden")
        identities.append(_require_file_identity(info))
    return tuple(identities)


@contextmanager
def pinned_windows_update_package(
    source: str | Path,
    *,
    expected_sha256: str,
    max_bytes: int = _MAX_PACKAGE_BYTES,
) -> Iterator[WindowsUpdateSnapshot]:
    """Pin one local regular package to an ephemeral private snapshot.

    The caller must perform *independent* signature and package-structure
    verification on snapshot.path, not on source, inside this context. The
    returned object is not an installation capability. Do not persist its path.
    """
    if type(expected_sha256) is not str or len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise WindowsUpdateSnapshotError("expected SHA-256 must be canonical")
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_PACKAGE_BYTES:
        raise WindowsUpdateSnapshotError("invalid package size bound")
    # Reject custom str/Path subclasses before their path protocol or string
    # conversion can run arbitrary callbacks. Installed trust uses the same
    # exact-type boundary; both direct-call update inputs must agree.
    if type(source) not in (str, type(Path())):
        raise WindowsUpdateSnapshotError("package path must be a local path")
    # The snapshot helper is also a public direct-call boundary. Do not rely
    # on the CLI alone to reject UNC/device/drive-relative aliases: a future
    # installer or verifier can call this helper without the CLI. Validate the
    # original spelling before abspath normalizes it or any filesystem probe.
    try:
        require_local_update_path(os.fspath(source))
    except (UnsafeUpdatePath, TypeError, ValueError):
        raise WindowsUpdateSnapshotError("package source path is not local and canonical") from None
    # Bind a relative caller path to one absolute pathname before the first
    # filesystem operation.  A different thread or callback may change the
    # process cwd between lstat/open/copy; without this anchor, a same-named
    # file under the new cwd could be read or the original source spuriously
    # rejected.  abspath is lexical: it does not resolve or follow symlinks.
    try:
        path = Path(os.path.abspath(os.fspath(source)))
    except (OSError, UnicodeError, ValueError, TypeError):
        raise WindowsUpdateSnapshotError("invalid package source path") from None
    try:
        # Check the filesystem object before and after opening it. O_NOFOLLOW
        # is not available on every Windows filesystem, and a separate
        # is_symlink() precheck alone leaves a replace-with-symlink race.
        parent_identity = _source_parent_identity(path)
        source_entry = path.lstat()
        source_identity = _require_file_identity(source_entry)
        if _is_redirected_file(source_entry):
            raise WindowsUpdateSnapshotError("symlink package source forbidden")
        if not stat.S_ISREG(source_entry.st_mode):
            raise WindowsUpdateSnapshotError("source is not a regular package")
        # Source pinning and embedded metadata both cross a trust boundary.
        # Never reopen a Windows ZIP via path-following CRT os.open.
        fd = _open_archive_fd(path)
    except WindowsUpdateSnapshotError:
        raise
    except (OSError, UnicodeError):
        raise WindowsUpdateSnapshotError("cannot open package source") from None

    try:
        with _owned_fd_stream(fd) as original:
            before = os.fstat(original.fileno())
            before_change_time = _native_change_time_100ns(original.fileno())
            opened_entry = path.lstat()
            if _source_parent_identity(path) != parent_identity:
                raise WindowsUpdateSnapshotError("package ancestor changed during open")
            before_identity = _require_file_identity(before)
            if (
                not stat.S_ISREG(opened_entry.st_mode) or _is_redirected_file(opened_entry)
                or before_identity != source_identity
                or before_identity != _require_file_identity(opened_entry)
            ):
                raise WindowsUpdateSnapshotError("package source identity changed during open")
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
                raise WindowsUpdateSnapshotError("source is not a bounded regular package")
            with tempfile.TemporaryDirectory(prefix="autosport-update-snapshot-") as temp:
                destination = Path(temp) / "candidate.zip"
                digest = hashlib.sha256()
                copied = 0
                # Exclusive creation prevents a pre-existing file from being followed.
                with destination.open("xb") as pinned:
                    while block := original.read(_CHUNK_BYTES):
                        copied += len(block)
                        if copied > max_bytes:
                            raise WindowsUpdateSnapshotError("package exceeds size bound")
                        digest.update(block)
                        pinned.write(block)
                    pinned.flush()
                    os.fsync(pinned.fileno())
                after = os.fstat(original.fileno())
                after_change_time = _native_change_time_100ns(original.fileno())
                # The descriptor can still refer to the original bytes after
                # an attacker replaces the *source path* with another inode.
                # Compare both the opened object and the path after copying;
                # digest equality alone is not a filesystem identity proof.
                source_after = path.lstat()
                if _source_parent_identity(path) != parent_identity:
                    raise WindowsUpdateSnapshotError("package ancestor changed during copy")
                if (
                    not stat.S_ISREG(source_after.st_mode) or _is_redirected_file(source_after)
                    or _require_file_identity(source_after) != before_identity
                    or (source_after.st_size, source_after.st_mtime_ns, source_after.st_ctime_ns)
                    != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                ):
                    raise WindowsUpdateSnapshotError("source changed while being copied")
                if (
                    _require_file_identity(after) != before_identity
                    or after_change_time != before_change_time
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or copied != before.st_size
                ):
                    raise WindowsUpdateSnapshotError("package changed while being copied")
                actual = digest.hexdigest()
                if not hmac.compare_digest(actual, expected_sha256):
                    raise WindowsUpdateSnapshotError("package digest mismatch")
                pinned_stat = destination.stat()
                # Open the pinned object for a descriptor-bound NT change-time
                # witness. The subsequent recheck confirms identity and stamp.
                with destination.open("rb") as pinned_reader:
                    pinned_change_time = _native_change_time_100ns(pinned_reader.fileno())
                snapshot = WindowsUpdateSnapshot(
                    destination, actual, copied,
                    _file_identity=_require_file_identity(pinned_stat),
                    _file_metadata=(
                        pinned_stat.st_size, pinned_stat.st_mtime_ns, pinned_stat.st_ctime_ns
                    ),
                    _change_time_100ns=pinned_change_time,
                    _parent_identity=_source_parent_identity(destination),
                )
                snapshot.recheck()
                yield snapshot
                snapshot.recheck()
    except (OSError, UnicodeError):
        raise WindowsUpdateSnapshotError("package snapshot I/O failure") from None


def _require_bounded_zip_members(path: Path) -> None:
    """Fail closed on archive expansion before canonical payload materialization."""
    # The canonical package verifier materializes each decompressed member. A
    # 2-GiB compressed-file ceiling does not prevent a tiny ZIP bomb from
    # exhausting memory before the verifier can reject its manifest. Validate
    # central-directory resource claims first; the canonical verifier still
    # validates headers, CRCs, manifests, paths and all actual member bytes.
    try:
        # The caller normally owns a private pinned snapshot, but this
        # resource guard can also be invoked directly. A renamed parent
        # replaced by a symlink to the original directory leaves the ZIP
        # inode and all leaf metadata unchanged. Bind the full ancestor chain
        # across the EOCD and central-directory parse, not only the leaf.
        parent_identity = _source_parent_identity(path)
        # A direct caller may supply a symlinked ZIP leaf even though the
        # normal snapshot path is private. Reject it before opening any bytes;
        # the post-parse check alone is too late to prevent a redirected read.
        entry_before = path.lstat()
        if not stat.S_ISREG(entry_before.st_mode) or _is_redirected_file(entry_before):
            raise WindowsUpdateSnapshotError("redirected or nonregular update archive")
        entry_identity = _require_file_identity(entry_before)
        # ZipFile eagerly loads the central directory, so bound its size and
        # advertised entry count *before* constructing ZipFile. The canonical
        # release format has one comment-free, single-disk, non-ZIP64 EOCD.
        # Open the leaf without following a symlink on platforms exposing
        # O_NOFOLLOW.  A path.open() after lstat can follow a replacement link
        # even when a subsequent inode check correctly rejects the receipt:
        # the untrusted target was already opened.  Keep one owned descriptor
        # for both the EOCD bound and ZipFile's central-directory parser.
        fd = _open_archive_fd(path)
        with _owned_fd_stream(fd) as raw:
            if _source_parent_identity(path) != parent_identity:
                raise WindowsUpdateSnapshotError("update archive ancestor changed during open")
            # Hold one descriptor across EOCD validation and ZipFile parsing.
            # Reopening by path here would let a replacement archive bypass
            # the resource limits checked on the original EOCD.
            initial = os.fstat(raw.fileno())
            initial_change = _native_change_time_100ns(raw.fileno())
            entry_opened = path.lstat()
            if (
                not stat.S_ISREG(initial.st_mode)
                or _is_redirected_file(initial)
                or not stat.S_ISREG(entry_opened.st_mode)
                or _is_redirected_file(entry_opened)
                or _require_file_identity(initial) != entry_identity
                or _require_file_identity(entry_opened) != entry_identity
                or (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
                != (entry_before.st_size, entry_before.st_mtime_ns, entry_before.st_ctime_ns)
                or (entry_opened.st_size, entry_opened.st_mtime_ns, entry_opened.st_ctime_ns)
                != (entry_before.st_size, entry_before.st_mtime_ns, entry_before.st_ctime_ns)
            ):
                raise WindowsUpdateSnapshotError("update archive identity changed during open")
            raw.seek(0, os.SEEK_END)
            length = raw.tell()
            if length < _ZIP_EOCD.size:
                raise WindowsUpdateSnapshotError("update archive directory is invalid")
            raw.seek(-_ZIP_EOCD.size, os.SEEK_END)
            (
                signature, disk, directory_disk, entries_on_disk, entries,
                directory_size, directory_offset, comment_length,
            ) = _ZIP_EOCD.unpack(raw.read(_ZIP_EOCD.size))
            if (
                signature != 0x06054B50 or disk != 0 or directory_disk != 0
                or entries_on_disk != entries or entries < 1
                or entries > _MAX_MEMBERS or comment_length != 0
                or directory_size > _MAX_CENTRAL_DIRECTORY_BYTES
                or directory_offset + directory_size != length - _ZIP_EOCD.size
            ):
                raise WindowsUpdateSnapshotError("update archive directory exceeds bound or is noncanonical")
            with zipfile.ZipFile(raw, "r") as archive:
                members = archive.infolist()
                if not members or len(members) > _MAX_MEMBERS:
                    raise WindowsUpdateSnapshotError("update archive member count exceeds bound")
                # ZipFile parses all central-directory records even when the
                # EOCD under-reports the count. Require agreement with the
                # pre-parsing resource witness instead of accepting a second,
                # larger member set under a smaller advertised count.
                if len(members) != entries:
                    raise WindowsUpdateSnapshotError("update archive member count disagrees with directory")
                total_uncompressed = 0
                for member in members:
                    # Canonical release ZIPs use only the optional UTF-8 name flag.
                    # Reject encryption, data descriptors and other features before
                    # archive.read() can raise an uncontrolled codec/password error.
                    if member.flag_bits & ~0x800:
                        raise WindowsUpdateSnapshotError("unsupported ZIP flags")
                    if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise WindowsUpdateSnapshotError("unsupported ZIP compression")
                    if (
                        member.file_size < 0
                        or member.file_size > _MAX_MEMBER_BYTES
                        or member.compress_size < 0
                    ):
                        raise WindowsUpdateSnapshotError("update archive member size exceeds bound")
                    total_uncompressed += member.file_size
                    if total_uncompressed > _MAX_UNCOMPRESSED_BYTES:
                        raise WindowsUpdateSnapshotError("update archive expanded size exceeds bound")
                    if member.file_size > _MAX_COMPRESSION_RATIO * max(member.compress_size, 1):
                        raise WindowsUpdateSnapshotError("update archive compression ratio exceeds bound")
            completed = os.fstat(raw.fileno())
            completed_change = _native_change_time_100ns(raw.fileno())
            if _source_parent_identity(path) != parent_identity:
                raise WindowsUpdateSnapshotError("update archive ancestor changed during parse")
            current_entry = path.lstat()
            if (
                not stat.S_ISREG(current_entry.st_mode) or _is_redirected_file(current_entry)
                or _is_redirected_file(completed)
                or _require_file_identity(initial) != _require_file_identity(completed)
                or _require_file_identity(initial) != _require_file_identity(current_entry)
                or initial_change != completed_change
                or (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
                != (completed.st_size, completed.st_mtime_ns, completed.st_ctime_ns)
                or (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
                != (current_entry.st_size, current_entry.st_mtime_ns, current_entry.st_ctime_ns)
            ):
                raise WindowsUpdateSnapshotError("update archive changed during preflight")
    except WindowsUpdateSnapshotError:
        # Preserve specific bounded-resource and policy failures.
        raise
    except (OSError, EOFError, RuntimeError, UnicodeError, ValueError,
            struct.error, zipfile.BadZipFile, zipfile.LargeZipFile):
        # Untrusted ZIP exceptions may embed paths or attacker-controlled names.
        # Suppress chained tracebacks in ordinary diagnostic logs.
        raise WindowsUpdateSnapshotError("update archive preflight failed") from None


_MAX_BUILD_INFO_BYTES = 64 * 1024


def _verified_embedded_update_identity(snapshot: WindowsUpdateSnapshot, *, expected_source_sha: str) -> dict[str, object]:
    """Read only identity fields from already deep-verified pinned BUILD_INFO bytes.

    A deep-verifier report is *not* authority for optional update metadata:
    version/schema must be re-derived from the hashed archive payload. Older
    packages without these fields remain valid for ordinary release inspection,
    but are ineligible for signed update admission.
    """
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError("noncanonical JSON constant")

    try:
        # The deep verifier has already authenticated the snapshot. Do not
        # reopen its *pathname* for optional update identity: a transient
        # replacement between two rechecks can supply an unverified BUILD_INFO
        # and restore the original path before the final recheck. Bind this
        # read to the same pinned inode and metadata using a held descriptor.
        if type(snapshot) is not WindowsUpdateSnapshot:
            raise WindowsUpdateSnapshotError("invalid pinned update snapshot")
        snapshot.recheck()
        path = snapshot.path
        # The inode alone is insufficient: a renamed private directory can be
        # replaced with a symlink to the *same* inode while BUILD_INFO is read.
        # Bind the original ancestor chain before and throughout this read.
        if _source_parent_identity(path) != snapshot._parent_identity:
            raise WindowsUpdateSnapshotError("embedded update ancestor changed before read")
        # Source pinning and embedded metadata both cross a trust boundary.
        # Never reopen a Windows ZIP via path-following CRT os.open.
        fd = _open_archive_fd(path)
        with _owned_fd_stream(fd) as stream:
            if _source_parent_identity(path) != snapshot._parent_identity:
                raise WindowsUpdateSnapshotError("embedded update ancestor changed during open")
            before = os.fstat(stream.fileno())
            before_change = _native_change_time_100ns(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or _require_file_identity(before) != snapshot._file_identity
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != snapshot._file_metadata
                or before_change != snapshot._change_time_100ns
            ):
                raise WindowsUpdateSnapshotError("embedded update source identity changed")
            with zipfile.ZipFile(stream, "r") as archive:
                member = archive.getinfo("Autosport-V1/BUILD_INFO.json")
                if member.file_size > _MAX_BUILD_INFO_BYTES or member.file_size < 2:
                    raise ValueError("BUILD_INFO size out of bounds")
                raw = archive.read(member)
            after = os.fstat(stream.fileno())
            after_change = _native_change_time_100ns(stream.fileno())
            if _source_parent_identity(path) != snapshot._parent_identity:
                raise WindowsUpdateSnapshotError("embedded update ancestor changed during read")
            entry = path.lstat()
            if (
                not stat.S_ISREG(entry.st_mode) or _is_redirected_file(entry)
                or _require_file_identity(after) != snapshot._file_identity
                or _require_file_identity(entry) != snapshot._file_identity
                or after_change != snapshot._change_time_100ns
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != snapshot._file_metadata
                or (entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns)
                != snapshot._file_metadata
            ):
                raise WindowsUpdateSnapshotError("embedded update source changed during read")
        if len(raw) > _MAX_BUILD_INFO_BYTES:
            raise ValueError("BUILD_INFO actual size out of bounds")
        build_info = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if type(build_info) is not dict or build_info.get("product") != "Autosport":
            raise ValueError("incorrect product identity")
        if build_info.get("source_sha") != expected_source_sha:
            raise ValueError("incorrect embedded source identity")
        has_version = "update_version" in build_info
        has_schema = "update_schema" in build_info
        if has_version != has_schema:
            raise ValueError("incomplete embedded update identity")
        if not has_version:
            return {}
        from .windows_update_admission import _schema, _version
        version = build_info["update_version"]
        schema = build_info["update_schema"]
        _version(version)
        _schema(schema)
        if type(build_info.get("version")) is not str or build_info["version"] != version:
            raise ValueError("package version disagrees with update identity")
        return {"update_version": version, "update_schema": schema}
    except (OSError, EOFError, ImportError, KeyError, ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError, struct.error):
        # BUILD_INFO and ZIP parser diagnostics can contain private paths.
        raise WindowsUpdateSnapshotError("embedded update identity is invalid") from None



# A deep package verifier is structural evidence, never an authority issuer.
# Keep its report schema bounded so a future/malicious verifier cannot append
# install/transaction/real-money/NVDA permissions to an otherwise PASS result.
_CANONICAL_VERIFIER_FIELDS = frozenset({
    "status", "source_sha", "package_sha256", "autosport_exe_sha256",
    "file_count", "real_money_execution", "human_tested", "nvda_verified",
    # These are re-derived from BUILD_INFO after deep verification, not trusted
    # from the report itself. Older verifiers may omit them.
    "update_version", "update_schema",
})


def _require_non_authorizing_verifier_report(report: object) -> dict[str, object]:
    if (
        type(report) is not dict
        or any(type(key) is not str for key in report)
        or not set(report).issubset(_CANONICAL_VERIFIER_FIELDS)
    ):
        raise WindowsUpdateSnapshotError("canonical verifier report contains unknown authority")
    # Never dispatch custom __eq__/__ne__ from an untrusted verifier result.
    # Only exact primitive values may attest PASS or match pinned digests.
    if (
        type(report.get("status")) is not str
        or report["status"] != "PASS"
        or ("source_sha" in report and (
            type(report["source_sha"]) is not str
            or len(report["source_sha"]) != 40
            or any(c not in "0123456789abcdef" for c in report["source_sha"])
        ))
        or ("package_sha256" in report and (
            type(report["package_sha256"]) is not str
            or len(report["package_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in report["package_sha256"])
        ))
    ):
        raise WindowsUpdateSnapshotError("canonical verifier report has invalid identity fields")
    for flag in ("real_money_execution", "human_tested", "nvda_verified"):
        if flag in report and report[flag] is not False:
            raise WindowsUpdateSnapshotError("canonical verifier report contains positive authority")
    if "file_count" in report and (
        type(report["file_count"]) is not int or report["file_count"] < 1
        or report["file_count"] > _MAX_MEMBERS
    ):
        raise WindowsUpdateSnapshotError("canonical verifier report has invalid member count")
    if "autosport_exe_sha256" in report and (
        type(report["autosport_exe_sha256"]) is not str
        or len(report["autosport_exe_sha256"]) != 64
        or any(c not in "0123456789abcdef" for c in report["autosport_exe_sha256"])
    ):
        raise WindowsUpdateSnapshotError("canonical verifier report has invalid executable digest")
    return report


def verify_pinned_release_package(
    snapshot: WindowsUpdateSnapshot,
    *,
    expected_source_sha: str,
) -> dict[str, object]:
    """Reuse the canonical deep ZIP verifier against the pinned, rechecked bytes.

    The returned report is only structural/digest evidence. In particular, a
    PASS here is not an Authenticode or detached-signature verification and
    never grants installation authority.
    """
    if type(snapshot) is not WindowsUpdateSnapshot:
        raise WindowsUpdateSnapshotError("invalid pinned package authority")
    # The caller-supplied source identity is itself an admission input.  A
    # subclass can override reflected equality and impersonate the embedded
    # source SHA even when the verifier report contains only plain strings.
    if (
        type(expected_source_sha) is not str
        or len(expected_source_sha) != 40
        or any(ch not in "0123456789abcdef" for ch in expected_source_sha)
    ):
        raise WindowsUpdateSnapshotError("expected source SHA must be canonical")
    snapshot.recheck()
    _require_bounded_zip_members(snapshot.path)
    snapshot.recheck()
    # A partially installed/corrupted Windows build may be missing the
    # canonical release verifier or its public entry point. Import failures
    # must not escape with local installation paths or become a PASS-shaped
    # update decision. Never fall back to digest-only package verification.
    try:
        from .release_package import verify_windows_package
    except (ImportError, SyntaxError, UnicodeError):
        raise WindowsUpdateSnapshotError(
            "canonical package verification backend unavailable"
        ) from None

    try:
        report = verify_windows_package(snapshot.path, expected_source_sha=expected_source_sha)
    except (ValueError, OSError, EOFError, ImportError, SyntaxError, KeyError, TypeError, RuntimeError, struct.error, zipfile.BadZipFile):
        # Never chain verifier internals into a user-visible traceback.
        raise WindowsUpdateSnapshotError("canonical package verification failed") from None
    report = _require_non_authorizing_verifier_report(report)
    # Freeze the *validated* receipt before any subsequent filesystem check or
    # metadata-reader callback can mutate the verifier's original dictionary.
    # This is a value copy: all retained fields have exact primitive types.
    attested = {key: value for key, value in report.items()
                if key not in ("update_version", "update_schema")}
    if (
        attested.get("status") != "PASS"
        or attested.get("package_sha256") != snapshot.sha256
        or attested.get("source_sha") != expected_source_sha
    ):
        raise WindowsUpdateSnapshotError("canonical verifier did not confirm pinned bytes")
    # Deep verification has already attested every BUILD_INFO byte through
    # PACKAGE_MANIFEST and SHA256SUMS. Never trust optional update fields from
    # the verifier's report object; derive them again from those exact bytes.
    snapshot.recheck()
    embedded = _verified_embedded_update_identity(
        snapshot, expected_source_sha=expected_source_sha,
    )
    snapshot.recheck()
    attested.update(embedded)
    return attested
