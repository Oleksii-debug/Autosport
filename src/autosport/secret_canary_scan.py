from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_from_bytes


_STATUS_CLEAN = "CLEAN"
_STATUS_LEAK = "LEAK"
_STATUS_INCOMPLETE = "INCOMPLETE"
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class SecretCanaryFinding:
    path_sha256: str
    encodings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecretCanaryScanError:
    path_sha256: str
    error_type: str


@dataclass(frozen=True, slots=True)
class SecretCanaryScanReport:
    status: str
    canary_sha256: str
    scanned_files: int
    excluded_files: int
    findings: tuple[SecretCanaryFinding, ...]
    errors: tuple[SecretCanaryScanError, ...]

    @property
    def exit_code(self) -> int:
        if self.status == _STATUS_CLEAN:
            return 0
        if self.status == _STATUS_LEAK:
            return 2
        return 3


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _ValidatedFixture:
    path: Path
    identity: _PathIdentity


class _ScanIntegrityError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type



def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _path_digest(root: Path, path: Path) -> str:
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except (OSError, ValueError):
        relative = "<unavailable>"
    return _digest_text(relative)



def _identity_from_stat(metadata: os.stat_result) -> _PathIdentity:
    return _PathIdentity(
        device=int(getattr(metadata, "st_dev", 0)),
        inode=int(getattr(metadata, "st_ino", 0)),
        mode=int(metadata.st_mode),
        size=int(metadata.st_size),
        mtime_ns=int(
            getattr(
                metadata,
                "st_mtime_ns",
                int(float(metadata.st_mtime) * 1_000_000_000),
            )
        ),
        ctime_ns=int(
            getattr(
                metadata,
                "st_ctime_ns",
                int(float(metadata.st_ctime) * 1_000_000_000),
            )
        ),
    )


def _same_object_identity(left: _PathIdentity, right: _PathIdentity) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and stat.S_IFMT(left.mode) == stat.S_IFMT(right.mode)
    )


def _metadata_is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT_FLAG)


def _scan_error_type(exc: BaseException) -> str:
    if isinstance(exc, _ScanIntegrityError):
        return exc.error_type
    return type(exc).__name__


def _open_readonly_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)



_PERCENT_HEX = re.compile(r"%[0-9A-F]{2}")
_PERCENT_HEX_BYTES = re.compile(rb"%[0-9A-Fa-f]{2}")
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


def _lower_percent_hex(value: str) -> str:
    return _PERCENT_HEX.sub(lambda match: match.group(0).lower(), value)


def _lower_percent_hex_bytes(value: bytes) -> bytes:
    return _PERCENT_HEX_BYTES.sub(lambda match: match.group(0).lower(), value)


def _percent_decode_bytes(value: bytes) -> bytes:
    """Decode only valid %HH triplets while preserving every literal byte exactly."""

    decoded = bytearray()
    index = 0
    while index < len(value):
        if (
            value[index] == 0x25
            and index + 2 < len(value)
            and value[index + 1] in _HEX_DIGITS
            and value[index + 2] in _HEX_DIGITS
        ):
            decoded.append(int(value[index + 1 : index + 3], 16))
            index += 3
            continue
        decoded.append(value[index])
        index += 1
    return bytes(decoded)


def _encoded_needles(canary: str) -> tuple[tuple[bytes, tuple[str, ...]], ...]:
    raw = canary.encode("utf-8")
    percent_encoded = quote_from_bytes(raw, safe="")
    json_utf8 = json.dumps(canary, ensure_ascii=False)[1:-1].encode("utf-8")
    json_ascii = json.dumps(canary, ensure_ascii=True)[1:-1].encode("ascii")
    variants = (
        ("utf-8", raw),
        ("utf-16le", canary.encode("utf-16le")),
        ("utf-16be", canary.encode("utf-16be")),
        ("url-percent-utf8-upper", percent_encoded.encode("ascii")),
        ("url-percent-utf8-lower", _lower_percent_hex(percent_encoded).encode("ascii")),
        ("json-string-utf8", json_utf8),
        ("json-string-ascii", json_ascii),
    )
    by_bytes: dict[bytes, list[str]] = {}
    for label, needle in variants:
        labels = by_bytes.setdefault(needle, [])
        labels.append(label)
    return tuple(
        (needle, tuple(labels))
        for needle, labels in sorted(by_bytes.items(), key=lambda item: item[0])
    )


def _scan_file(
    path: Path,
    needles: tuple[tuple[bytes, tuple[str, ...]], ...],
    semantic_utf8: bytes,
    *,
    chunk_size: int,
    expected_identity: _PathIdentity,
) -> tuple[str, ...]:
    pre_metadata = path.lstat()
    pre_identity = _identity_from_stat(pre_metadata)
    if pre_identity != expected_identity:
        raise _ScanIntegrityError("FileIdentityChanged")
    if stat.S_ISLNK(pre_metadata.st_mode) or _metadata_is_reparse_point(pre_metadata):
        raise _ScanIntegrityError("FileLinkLikeRace")
    if not stat.S_ISREG(pre_metadata.st_mode):
        raise _ScanIntegrityError("FileTypeChanged")

    fd = _open_readonly_no_follow(path)
    try:
        opened_metadata = os.fstat(fd)
        opened_identity = _identity_from_stat(opened_metadata)
        if opened_identity != expected_identity:
            raise _ScanIntegrityError("FileIdentityChanged")
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise _ScanIntegrityError("FileTypeChanged")

        max_needle = max(
            max(len(needle) for needle, _labels in needles),
            len(semantic_utf8) * 3,
        )
        overlap = max(0, max_needle - 1)
        required_labels = {label for _needle, labels in needles for label in labels}
        found: set[str] = set()
        tail = b""
        with os.fdopen(fd, "rb", closefd=False) as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                window = tail + chunk
                normalized_percent_window: bytes | None = None
                for needle, labels in needles:
                    if any(label in found for label in labels):
                        continue
                    haystack = window
                    if "url-percent-utf8-lower" in labels:
                        if normalized_percent_window is None:
                            normalized_percent_window = _lower_percent_hex_bytes(window)
                        haystack = normalized_percent_window
                    if needle in haystack:
                        found.update(labels)

                # URL percent encoding permits every UTF-8 byte to be represented as
                # %HH, including RFC-unreserved bytes that urllib normally leaves
                # literal. Decode only valid triplets into a private byte window and
                # compare against the exact UTF-8 secret. Literal bytes retain their
                # original case, so this does not broaden ASCII matching authority.
                if (
                    "url-percent-utf8-semantic" not in found
                    and semantic_utf8 not in window
                    and b"%" in window
                    and semantic_utf8 in _percent_decode_bytes(window)
                ):
                    found.add("url-percent-utf8-semantic")

                if required_labels.issubset(found):
                    break
                tail = window[-overlap:] if overlap else b""

        final_handle_identity = _identity_from_stat(os.fstat(fd))
        if final_handle_identity != opened_identity:
            raise _ScanIntegrityError("FileIdentityChanged")
    finally:
        os.close(fd)

    post_metadata = path.lstat()
    post_identity = _identity_from_stat(post_metadata)
    if (
        post_identity != expected_identity
        or stat.S_ISLNK(post_metadata.st_mode)
        or _metadata_is_reparse_point(post_metadata)
        or not stat.S_ISREG(post_metadata.st_mode)
    ):
        raise _ScanIntegrityError("FileIdentityChanged")
    return tuple(sorted(found))


def _lexical_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _path_is_reparse_point(path: Path) -> bool:
    """Return whether the final path entry is a Windows reparse point."""

    return _metadata_is_reparse_point(path.lstat())


def _root_link_like_error(root: Path) -> str | None:
    """Reject link-like traversal in the root itself or any absolute ancestor."""

    components = (*reversed(root.parents), root)
    for component in components:
        if component.is_symlink():
            return "RootSymlink" if component == root else "RootSymlinkAncestor"
        if _path_is_reparse_point(component):
            return (
                "RootReparsePoint"
                if component == root
                else "RootReparsePointAncestor"
            )
    return None


def _has_symlink_component(root: Path, candidate: Path) -> bool:
    relative = candidate.relative_to(root)
    current = root
    if root.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _has_reparse_component(root: Path, candidate: Path) -> bool:
    relative = candidate.relative_to(root)
    current = root
    if _path_is_reparse_point(root):
        return True
    for part in relative.parts:
        current = current / part
        if _path_is_reparse_point(current):
            return True
    return False


def _scan_directory(
    path: Path,
    expected_identity: _PathIdentity,
) -> list[os.DirEntry]:
    pre_metadata = path.lstat()
    pre_identity = _identity_from_stat(pre_metadata)
    if (
        not _same_object_identity(pre_identity, expected_identity)
        or stat.S_ISLNK(pre_metadata.st_mode)
        or _metadata_is_reparse_point(pre_metadata)
        or not stat.S_ISDIR(pre_metadata.st_mode)
    ):
        raise _ScanIntegrityError("DirectoryIdentityChanged")

    with os.scandir(path) as entries:
        ordered = sorted(entries, key=lambda item: item.name)

    post_metadata = path.lstat()
    post_identity = _identity_from_stat(post_metadata)
    if (
        not _same_object_identity(post_identity, expected_identity)
        or stat.S_ISLNK(post_metadata.st_mode)
        or _metadata_is_reparse_point(post_metadata)
        or not stat.S_ISDIR(post_metadata.st_mode)
    ):
        raise _ScanIntegrityError("DirectoryIdentityChanged")
    return ordered


def _validate_fixture_input(
    root: Path,
    fixture_input: Path | None,
) -> tuple[_ValidatedFixture | None, str | None]:
    if fixture_input is None:
        return None, None
    candidate = fixture_input if fixture_input.is_absolute() else root / fixture_input
    candidate = Path(os.path.abspath(candidate))
    if not _lexical_under(root, candidate):
        return None, "FixtureOutsideRoot"
    try:
        if _has_symlink_component(root, candidate):
            return None, "FixtureSymlink"
        if _has_reparse_component(root, candidate):
            return None, "FixtureReparsePoint"
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return None, "FixtureSymlink"
        if _metadata_is_reparse_point(metadata):
            return None, "FixtureReparsePoint"
        if not stat.S_ISREG(metadata.st_mode):
            return None, "FixtureNotRegularFile"
    except OSError as exc:
        return None, type(exc).__name__
    return _ValidatedFixture(candidate, _identity_from_stat(metadata)), None


def scan_secret_canary(
    root: str | os.PathLike[str],
    canary: str,
    *,
    fixture_input: str | os.PathLike[str] | None = None,
    chunk_size: int = 64 * 1024,
) -> SecretCanaryScanReport:
    """Scan a writable artifact tree for a planted secret without echoing the secret.

    Qualification is fail-closed under link/reparse and filesystem-identity races.
    Every regular-file read is bound to the exact no-follow snapshot observed by
    traversal, and optional fixture exclusion is bound to the exact validated file
    snapshot rather than only its pathname.
    """

    if not isinstance(canary, str) or not canary:
        raise ValueError("canary must be a non-empty string")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    root_path = Path(os.path.abspath(root))
    semantic_utf8 = canary.encode("utf-8")
    canary_digest = hashlib.sha256(semantic_utf8).hexdigest()
    findings: list[SecretCanaryFinding] = []
    errors: list[SecretCanaryScanError] = []
    scanned_files = 0
    excluded_files = 0
    root_identity: _PathIdentity | None = None

    try:
        root_link_error = _root_link_like_error(root_path)
        if root_link_error is not None:
            errors.append(
                SecretCanaryScanError(
                    _path_digest(root_path, root_path),
                    root_link_error,
                )
            )
        else:
            root_metadata = root_path.lstat()
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or _metadata_is_reparse_point(root_metadata)
                or not stat.S_ISDIR(root_metadata.st_mode)
            ):
                errors.append(
                    SecretCanaryScanError(
                        _path_digest(root_path, root_path),
                        "RootNotDirectory",
                    )
                )
            else:
                root_identity = _identity_from_stat(root_metadata)
    except OSError as exc:
        errors.append(
            SecretCanaryScanError(_path_digest(root_path, root_path), type(exc).__name__)
        )

    if errors or root_identity is None:
        return SecretCanaryScanReport(
            _STATUS_INCOMPLETE,
            canary_digest,
            scanned_files,
            excluded_files,
            tuple(findings),
            tuple(errors),
        )

    fixture, fixture_error = _validate_fixture_input(
        root_path,
        None if fixture_input is None else Path(fixture_input),
    )
    if fixture_error is not None:
        errors.append(
            SecretCanaryScanError(
                _path_digest(
                    root_path,
                    root_path if fixture_input is None else Path(fixture_input),
                ),
                fixture_error,
            )
        )
        return SecretCanaryScanReport(
            _STATUS_INCOMPLETE,
            canary_digest,
            scanned_files,
            excluded_files,
            tuple(findings),
            tuple(errors),
        )

    needles = _encoded_needles(canary)
    stack: list[tuple[Path, _PathIdentity]] = [(root_path, root_identity)]
    fixture_excluded = False
    while stack:
        directory, directory_identity = stack.pop()
        try:
            ordered = _scan_directory(directory, directory_identity)
        except (OSError, _ScanIntegrityError) as exc:
            errors.append(
                SecretCanaryScanError(
                    _path_digest(root_path, directory),
                    _scan_error_type(exc),
                )
            )
            continue

        child_directories: list[tuple[Path, _PathIdentity]] = []
        for entry in ordered:
            path = Path(entry.path)
            try:
                metadata = entry.stat(follow_symlinks=False)
                entry_identity = _identity_from_stat(metadata)
                if stat.S_ISLNK(metadata.st_mode):
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "SymlinkRejected",
                        )
                    )
                    continue
                if _metadata_is_reparse_point(metadata):
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "ReparsePointRejected",
                        )
                    )
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    child_directories.append((path, entry_identity))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "UnsupportedEntry",
                        )
                    )
                    continue

                scan_identity = entry_identity
                if fixture is not None and path == fixture.path:
                    current_metadata = path.lstat()
                    current_identity = _identity_from_stat(current_metadata)
                    fixture_is_same = (
                        current_identity == fixture.identity
                        and stat.S_ISREG(current_metadata.st_mode)
                        and not stat.S_ISLNK(current_metadata.st_mode)
                        and not _metadata_is_reparse_point(current_metadata)
                    )
                    if fixture_is_same:
                        excluded_files += 1
                        fixture_excluded = True
                        continue
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "FixtureIdentityChanged",
                        )
                    )
                    if (
                        not stat.S_ISREG(current_metadata.st_mode)
                        or stat.S_ISLNK(current_metadata.st_mode)
                        or _metadata_is_reparse_point(current_metadata)
                    ):
                        continue
                    scan_identity = current_identity

                scanned_files += 1
                encodings = _scan_file(
                    path,
                    needles,
                    semantic_utf8,
                    chunk_size=chunk_size,
                    expected_identity=scan_identity,
                )
                if encodings:
                    findings.append(
                        SecretCanaryFinding(
                            path_sha256=_path_digest(root_path, path),
                            encodings=encodings,
                        )
                    )
            except (OSError, _ScanIntegrityError) as exc:
                errors.append(
                    SecretCanaryScanError(
                        _path_digest(root_path, path),
                        _scan_error_type(exc),
                    )
                )
        stack.extend(reversed(child_directories))

    if fixture is not None and fixture_excluded:
        try:
            final_fixture_metadata = fixture.path.lstat()
            final_fixture_identity = _identity_from_stat(final_fixture_metadata)
            if (
                final_fixture_identity != fixture.identity
                or not stat.S_ISREG(final_fixture_metadata.st_mode)
                or stat.S_ISLNK(final_fixture_metadata.st_mode)
                or _metadata_is_reparse_point(final_fixture_metadata)
            ):
                raise _ScanIntegrityError("FixtureIdentityChanged")
        except (OSError, _ScanIntegrityError):
            errors.append(
                SecretCanaryScanError(
                    _path_digest(root_path, fixture.path),
                    "FixtureIdentityChanged",
                )
            )

    if errors:
        status = _STATUS_INCOMPLETE
    elif findings:
        status = _STATUS_LEAK
    else:
        status = _STATUS_CLEAN
    return SecretCanaryScanReport(
        status=status,
        canary_sha256=canary_digest,
        scanned_files=scanned_files,
        excluded_files=excluded_files,
        findings=tuple(findings),
        errors=tuple(errors),
    )

