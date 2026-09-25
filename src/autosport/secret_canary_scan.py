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


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _path_digest(root: Path, path: Path) -> str:
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except (OSError, ValueError):
        relative = "<unavailable>"
    return _digest_text(relative)


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
) -> tuple[str, ...]:
    max_needle = max(
        max(len(needle) for needle, _labels in needles),
        len(semantic_utf8) * 3,
    )
    overlap = max(0, max_needle - 1)
    required_labels = {label for _needle, labels in needles for label in labels}
    found: set[str] = set()
    tail = b""
    with path.open("rb") as handle:
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
    return tuple(sorted(found))


def _lexical_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _path_is_reparse_point(path: Path) -> bool:
    """Return whether the final path entry is a Windows reparse point.

    ``Path.is_symlink()`` does not cover every Windows link-like object (notably
    directory junctions). Python 3.11 exposes the file-attribute bit through
    ``lstat`` on Windows, while other platforms simply lack that attribute.
    """

    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT_FLAG)


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


def _validate_fixture_input(
    root: Path,
    fixture_input: Path | None,
) -> tuple[Path | None, str | None]:
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
        if not candidate.is_file():
            return None, "FixtureNotRegularFile"
    except OSError as exc:
        return None, type(exc).__name__
    return candidate, None


def scan_secret_canary(
    root: str | os.PathLike[str],
    canary: str,
    *,
    fixture_input: str | os.PathLike[str] | None = None,
    chunk_size: int = 64 * 1024,
) -> SecretCanaryScanReport:
    """Scan a writable artifact tree for a planted secret without echoing the secret.

    The scan is fail-closed. Symlinks, reparse points, unreadable entries,
    unsupported filesystem entries, and invalid fixture exclusions produce
    INCOMPLETE rather than CLEAN. Only an exact existing regular file below
    ``root`` may be excluded as the planted fixture input.
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

    try:
        if root_path.is_symlink():
            errors.append(
                SecretCanaryScanError(_path_digest(root_path, root_path), "RootSymlink")
            )
        elif _path_is_reparse_point(root_path):
            errors.append(
                SecretCanaryScanError(
                    _path_digest(root_path, root_path),
                    "RootReparsePoint",
                )
            )
        elif not root_path.is_dir():
            errors.append(
                SecretCanaryScanError(_path_digest(root_path, root_path), "RootNotDirectory")
            )
    except OSError as exc:
        errors.append(
            SecretCanaryScanError(_path_digest(root_path, root_path), type(exc).__name__)
        )

    if errors:
        return SecretCanaryScanReport(
            _STATUS_INCOMPLETE,
            canary_digest,
            scanned_files,
            excluded_files,
            tuple(findings),
            tuple(errors),
        )

    fixture_path, fixture_error = _validate_fixture_input(
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
    stack = [root_path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda item: item.name)
        except OSError as exc:
            errors.append(
                SecretCanaryScanError(
                    _path_digest(root_path, directory),
                    type(exc).__name__,
                )
            )
            continue

        child_directories: list[Path] = []
        for entry in ordered:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "SymlinkRejected",
                        )
                    )
                    continue
                if _path_is_reparse_point(path):
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "ReparsePointRejected",
                        )
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_directories.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    errors.append(
                        SecretCanaryScanError(
                            _path_digest(root_path, path),
                            "UnsupportedEntry",
                        )
                    )
                    continue
                if fixture_path is not None and path == fixture_path:
                    excluded_files += 1
                    continue
                scanned_files += 1
                encodings = _scan_file(
                    path,
                    needles,
                    semantic_utf8,
                    chunk_size=chunk_size,
                )
                if encodings:
                    findings.append(
                        SecretCanaryFinding(
                            path_sha256=_path_digest(root_path, path),
                            encodings=encodings,
                        )
                    )
            except OSError as exc:
                errors.append(
                    SecretCanaryScanError(
                        _path_digest(root_path, path),
                        type(exc).__name__,
                    )
                )
        stack.extend(reversed(child_directories))

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
