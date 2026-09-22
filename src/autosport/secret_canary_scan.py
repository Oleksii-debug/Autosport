from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_from_bytes


_STATUS_CLEAN = "CLEAN"
_STATUS_LEAK = "LEAK"
_STATUS_INCOMPLETE = "INCOMPLETE"


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


def _encoded_needles(canary: str) -> tuple[tuple[bytes, tuple[str, ...]], ...]:
    raw = canary.encode("utf-8")
    variants = (
        ("utf-8", raw),
        ("utf-16le", canary.encode("utf-16le")),
        ("utf-16be", canary.encode("utf-16be")),
        ("url-percent-utf8-upper", quote_from_bytes(raw, safe="").encode("ascii")),
        ("url-percent-utf8-lower", quote_from_bytes(raw, safe="").lower().encode("ascii")),
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
    *,
    chunk_size: int,
) -> tuple[str, ...]:
    max_needle = max(len(needle) for needle, _labels in needles)
    overlap = max(0, max_needle - 1)
    found: set[str] = set()
    tail = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            window = tail + chunk
            for needle, labels in needles:
                if any(label in found for label in labels):
                    continue
                if needle in window:
                    found.update(labels)
            if len(found) == sum(len(labels) for _needle, labels in needles):
                break
            tail = window[-overlap:] if overlap else b""
    return tuple(sorted(found))


def _lexical_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


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

    The scan is fail-closed. Symlinks, unreadable entries, unsupported filesystem
    entries, and invalid fixture exclusions produce INCOMPLETE rather than CLEAN.
    Only an exact existing regular file below ``root`` may be excluded as the
    planted fixture input.
    """

    if not isinstance(canary, str) or not canary:
        raise ValueError("canary must be a non-empty string")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    root_path = Path(os.path.abspath(root))
    canary_digest = hashlib.sha256(canary.encode("utf-8")).hexdigest()
    findings: list[SecretCanaryFinding] = []
    errors: list[SecretCanaryScanError] = []
    scanned_files = 0
    excluded_files = 0

    try:
        if root_path.is_symlink():
            errors.append(
                SecretCanaryScanError(_path_digest(root_path, root_path), "RootSymlink")
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
                encodings = _scan_file(path, needles, chunk_size=chunk_size)
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
