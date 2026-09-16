from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping

from autosport.data_tool_package import bind_portable_data_tool, verify_portable_data_tool
from autosport.release_package import (
    _require_git_commit_sha,
    build_windows_package,
    verify_windows_package,
)


_SHA256_HEX = frozenset("0123456789abcdef")
_COPY_CHUNK_SIZE = 1024 * 1024
_WINDOWS_MUTATION_DENY_RIGHTS = "(OI)(CI)(WD,AD,WEA,WA,DE,DC)"


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to prove source identity with git {' '.join(args)}") from exc
    value = completed.stdout.strip()
    if not value:
        raise ValueError(f"git {' '.join(args)} returned an empty identity")
    return value


def _fetch_source_commit(repo_root: Path, source_sha: str) -> None:
    try:
        subprocess.run(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", source_sha],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "source_sha is not fetchable from the authoritative origin repository"
        ) from exc


def _github_authoritative_source_sha() -> str | None:
    event_path_text = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path_text:
        return None
    event_path = Path(event_path_text)
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("GITHUB_EVENT_PATH is not readable authoritative JSON") from exc
    if not isinstance(event, dict):
        raise ValueError("GitHub event payload must be a JSON object")

    github_repository = os.environ.get("GITHUB_REPOSITORY")
    event_repository = event.get("repository")
    event_repository_name = (
        event_repository.get("full_name")
        if isinstance(event_repository, dict)
        else None
    )
    if (
        not github_repository
        or not isinstance(event_repository_name, str)
        or event_repository_name != github_repository
    ):
        raise ValueError("GitHub event repository identity does not match GITHUB_REPOSITORY")

    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if not isinstance(head, dict):
            raise ValueError("GitHub pull request event is missing head identity")
        head_repo = head.get("repo")
        head_repo_name = (
            head_repo.get("full_name")
            if isinstance(head_repo, dict)
            else None
        )
        if head_repo_name != github_repository:
            raise ValueError(
                "official package source must be a commit from the authoritative repository"
            )
        source_sha = head.get("sha")
    else:
        source_sha = os.environ.get("GITHUB_SHA")

    _require_git_commit_sha(source_sha, field="authoritative_source_sha")
    return source_sha


def _bind_source_sha_to_checkout(source_sha: str, *, repo_root: Path) -> None:
    """Bind package source identity to authoritative CI/local Git evidence.

    The offline ZIP verifier intentionally proves only syntax, equality, truth labels,
    and payload hashes. Repository membership is a build-time concern: on GitHub
    Actions we require the source SHA to equal the same-repository event head, fetch
    that exact commit from origin, and prove its tree matches the checked-out tree.
    Outside Actions, the caller must package the exact local HEAD.
    """

    _require_git_commit_sha(source_sha, field="source_sha")
    authoritative_sha = _github_authoritative_source_sha()
    if authoritative_sha is None:
        checkout_sha = _git_output(repo_root, "rev-parse", "--verify", "HEAD")
        _require_git_commit_sha(checkout_sha, field="checkout_head_sha")
        if source_sha != checkout_sha:
            raise ValueError("source_sha does not match the exact local checkout HEAD")
        source_tree = _git_output(repo_root, "rev-parse", "--verify", "HEAD^{tree}")
    else:
        if source_sha != authoritative_sha:
            raise ValueError(
                "source_sha does not match the authoritative GitHub candidate head"
            )
        _fetch_source_commit(repo_root, source_sha)
        source_tree = _git_output(
            repo_root,
            "rev-parse",
            "--verify",
            f"{source_sha}^{{tree}}",
        )

    checkout_tree = _git_output(repo_root, "rev-parse", "--verify", "HEAD^{tree}")
    if source_tree != checkout_tree:
        raise ValueError(
            "checked-out source tree does not match the authoritative source_sha tree"
        )


def _exact_git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in tuple(env):
        if name.upper().startswith("GIT_"):
            env.pop(name, None)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def _exact_git_bytes(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            env=_exact_git_environment(),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"unable to materialize exact package source with git {' '.join(args)}") from exc
    return completed.stdout


def _repo_relative_path(repo_root: Path, requested: Path, *, field: str) -> PurePosixPath:
    root = Path(os.path.abspath(repo_root))
    candidate = requested if requested.is_absolute() else root / requested
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field} must be inside the release source checkout") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{field} must name a concrete tracked source path")
    return PurePosixPath(*relative.parts)


def _exact_tree_entries(
    repo_root: Path,
    source_sha: str,
    pathspec: PurePosixPath,
) -> tuple[tuple[PurePosixPath, str], ...]:
    _require_git_commit_sha(source_sha, field="source_sha")
    raw = _exact_git_bytes(
        repo_root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        source_sha,
        "--",
        pathspec.as_posix(),
    )
    entries: list[tuple[PurePosixPath, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_sha = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ValueError("unable to parse exact package source tree") from exc
        path = PurePosixPath(path_bytes.decode("utf-8", errors="strict"))
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError(
                f"unsupported tracked package payload entry: {path.as_posix()}"
            )
        object_sha_text = object_sha.decode("ascii")
        if len(object_sha_text) != 40 or any(
            character not in "0123456789abcdef" for character in object_sha_text
        ):
            raise ValueError("exact package source tree returned a noncanonical blob identity")
        entries.append((path, object_sha_text))
    return tuple(entries)


def _read_exact_git_blob(repo_root: Path, object_sha: str) -> bytes:
    data = _exact_git_bytes(repo_root, "cat-file", "blob", object_sha)
    blob_header = f"blob {len(data)}\0".encode("ascii")
    actual_object_sha = hashlib.sha1(
        blob_header + data,
        usedforsecurity=False,
    ).hexdigest()
    if actual_object_sha != object_sha:
        raise ValueError(
            "exact package source Git blob identity mismatch: "
            f"expected {object_sha}, got {actual_object_sha}"
        )
    return data


def _write_exact_git_blob(
    repo_root: Path,
    object_sha: str,
    destination: Path,
) -> str:
    data = _read_exact_git_blob(repo_root, object_sha)
    expected_sha256 = hashlib.sha256(data).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return expected_sha256


def _materialize_exact_static_payload(
    *,
    repo_root: Path,
    source_sha: str,
    start_file: Path,
    example_dir: Path,
    snapshot_dir: Path,
    expected_sha256: dict[Path, str] | None = None,
) -> tuple[Path, Path]:
    """Materialize tracked static package inputs from the exact source Git tree.

    The caller-provided paths select repository paths only. Their live working-tree
    bytes and live directory membership are intentionally never consumed here.
    """

    start_relative = _repo_relative_path(repo_root, start_file, field="start_file")
    example_relative = _repo_relative_path(repo_root, example_dir, field="example_dir")

    start_entries = _exact_tree_entries(repo_root, source_sha, start_relative)
    if len(start_entries) != 1 or start_entries[0][0] != start_relative:
        raise ValueError("start_file is not exactly one tracked regular file in source_sha")

    example_entries = _exact_tree_entries(repo_root, source_sha, example_relative)
    prefix = example_relative.as_posix().rstrip("/") + "/"
    if not example_entries:
        raise ValueError("example_dir has no tracked regular files in source_sha")
    if any(not path.as_posix().startswith(prefix) for path, _object_sha in example_entries):
        raise ValueError("example_dir exact source tree escaped its requested prefix")

    static_root = snapshot_dir / "exact-source-static"
    trusted_start = static_root.joinpath(*start_relative.parts)
    trusted_example_dir = static_root.joinpath(*example_relative.parts)
    start_digest = _write_exact_git_blob(repo_root, start_entries[0][1], trusted_start)
    if expected_sha256 is not None:
        expected_sha256[trusted_start] = start_digest
    for path, object_sha in example_entries:
        destination = static_root.joinpath(*path.parts)
        digest = _write_exact_git_blob(repo_root, object_sha, destination)
        if expected_sha256 is not None:
            expected_sha256[destination] = digest

    materialized = tuple(
        PurePosixPath(path.relative_to(static_root).as_posix())
        for path in trusted_example_dir.rglob("*")
        if path.is_file()
    )
    expected = tuple(path for path, _object_sha in example_entries)
    if tuple(sorted(materialized, key=lambda item: item.as_posix())) != tuple(
        sorted(expected, key=lambda item: item.as_posix())
    ):
        raise ValueError("exact example payload materialization membership mismatch")
    return trusted_start, trusted_example_dir


def _require_sha256(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in _SHA256_HEX for character in normalized):
        raise ValueError(f"{field} must be exactly 64 lowercase hexadecimal characters")
    return normalized


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _capture_verified_file(
    source: Path,
    expected_sha256: str,
    *,
    snapshot_dir: Path,
    snapshot_name: str,
    handoff_kind: str,
) -> Path:
    """Capture one hash-bound regular file into a process-private package snapshot."""

    expected = _require_sha256(expected_sha256, field=f"{snapshot_name}_sha256")
    source = source.absolute()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    destination = snapshot_dir / snapshot_name
    temporary_path: Path | None = None

    try:
        before_path = source.lstat()
    except OSError as exc:
        raise ValueError(f"{snapshot_name} {handoff_kind} handoff is not readable: {source}") from exc
    if stat.S_ISLNK(before_path.st_mode) or not stat.S_ISREG(before_path.st_mode):
        raise ValueError(
            f"{snapshot_name} {handoff_kind} handoff must be a regular non-symlink file"
        )

    digest = hashlib.sha256()
    try:
        with source.open("rb") as source_handle:
            opened = os.fstat(source_handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(
                    f"{snapshot_name} opened {handoff_kind} handoff is not a regular file"
                )
            if _file_identity(opened) != _file_identity(before_path):
                raise ValueError(f"{snapshot_name} {handoff_kind} handoff changed before capture")

            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{snapshot_name}.",
                suffix=".tmp",
                dir=snapshot_dir,
                delete=False,
            ) as destination_handle:
                temporary_path = Path(destination_handle.name)
                while True:
                    chunk = source_handle.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    destination_handle.write(chunk)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())

            after_handle = os.fstat(source_handle.fileno())
            if _file_identity(after_handle) != _file_identity(opened):
                raise ValueError(f"{snapshot_name} {handoff_kind} handoff changed during capture")

        try:
            after_path = source.lstat()
        except OSError as exc:
            raise ValueError(
                f"{snapshot_name} {handoff_kind} handoff disappeared during capture"
            ) from exc
        if stat.S_ISLNK(after_path.st_mode) or not stat.S_ISREG(after_path.st_mode):
            raise ValueError(f"{snapshot_name} {handoff_kind} handoff was replaced during capture")
        if _file_identity(after_path) != _file_identity(before_path):
            raise ValueError(f"{snapshot_name} {handoff_kind} handoff was replaced during capture")

        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(
                f"{snapshot_name} {handoff_kind} handoff SHA-256 mismatch: "
                f"expected {expected}, got {actual}"
            )

        os.replace(temporary_path, destination)
        temporary_path = None
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _capture_verified_executable(
    source: Path,
    expected_sha256: str,
    *,
    snapshot_dir: Path,
    snapshot_name: str,
) -> Path:
    return _capture_verified_file(
        source,
        expected_sha256,
        snapshot_dir=snapshot_dir,
        snapshot_name=snapshot_name,
        handoff_kind="executable",
    )


def _capture_verified_evidence(
    source: Path,
    expected_sha256: str,
    *,
    snapshot_dir: Path,
    snapshot_name: str,
) -> Path:
    return _capture_verified_file(
        source,
        expected_sha256,
        snapshot_dir=snapshot_dir,
        snapshot_name=snapshot_name,
        handoff_kind="evidence",
    )


def _contains_windows_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = int(getattr(path_stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _normalize_snapshot_manifest(
    snapshot_dir: Path,
    expected_sha256: Mapping[Path, str],
) -> tuple[Path, dict[Path, str]]:
    root = Path(os.path.abspath(snapshot_dir))
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ValueError("private package snapshot root is not readable") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or _contains_windows_reparse_point(root_stat)
    ):
        raise ValueError("private package snapshot root must be a real directory")
    if not expected_sha256:
        raise ValueError("private package snapshot manifest is empty")

    root_prefix = str(root) + os.sep
    normalized: dict[Path, str] = {}
    for requested, digest in expected_sha256.items():
        candidate = Path(os.path.abspath(requested))
        if str(candidate) == str(root) or not str(candidate).startswith(root_prefix):
            raise ValueError("private package snapshot manifest escaped its root")
        normalized[candidate] = _require_sha256(
            digest,
            field=f"snapshot_sha256:{candidate.name}",
        )
    return root, normalized


def _expected_snapshot_directories(root: Path, files: Mapping[Path, str]) -> set[Path]:
    directories = {root}
    for path in files:
        parent = path.parent
        while True:
            directories.add(parent)
            if parent == root:
                break
            try:
                parent.relative_to(root)
            except ValueError as exc:
                raise ValueError("private package snapshot parent escaped its root") from exc
            parent = parent.parent
    return directories


def _verify_snapshot_manifest(root: Path, expected_sha256: Mapping[Path, str]) -> None:
    expected_directories = _expected_snapshot_directories(root, expected_sha256)
    actual_files: set[Path] = set()
    actual_directories = {root}

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            directory_stat = directory.lstat()
        except OSError as exc:
            raise ValueError(f"private package snapshot directory disappeared: {directory}") from exc
        if (
            stat.S_ISLNK(directory_stat.st_mode)
            or not stat.S_ISDIR(directory_stat.st_mode)
            or _contains_windows_reparse_point(directory_stat)
        ):
            raise ValueError(f"private package snapshot contains unsafe directory: {directory}")
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise ValueError(f"private package snapshot directory is unreadable: {directory}") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_stat = path.lstat()
            except OSError as exc:
                raise ValueError(f"private package snapshot entry disappeared: {path}") from exc
            if stat.S_ISLNK(entry_stat.st_mode) or _contains_windows_reparse_point(entry_stat):
                raise ValueError(f"private package snapshot contains a reparse/symlink entry: {path}")
            if stat.S_ISDIR(entry_stat.st_mode):
                actual_directories.add(path)
                pending.append(path)
            elif stat.S_ISREG(entry_stat.st_mode):
                actual_files.add(path)
            else:
                raise ValueError(f"private package snapshot contains a non-regular entry: {path}")

    if actual_files != set(expected_sha256):
        missing = sorted(str(path.relative_to(root)) for path in set(expected_sha256) - actual_files)
        extra = sorted(str(path.relative_to(root)) for path in actual_files - set(expected_sha256))
        raise ValueError(
            f"private package snapshot file membership mismatch: missing={missing}, extra={extra}"
        )
    if actual_directories != expected_directories:
        missing = sorted(str(path.relative_to(root)) for path in expected_directories - actual_directories)
        extra = sorted(str(path.relative_to(root)) for path in actual_directories - expected_directories)
        raise ValueError(
            f"private package snapshot directory membership mismatch: missing={missing}, extra={extra}"
        )

    for path, expected in sorted(expected_sha256.items(), key=lambda item: str(item[0])):
        digest = hashlib.sha256()
        try:
            before = path.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or _contains_windows_reparse_point(before)
            ):
                raise ValueError(f"private package snapshot path is not a regular file: {path}")
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(before):
                    raise ValueError(f"private package snapshot changed before verification: {path}")
                while True:
                    chunk = handle.read(_COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                after_handle = os.fstat(handle.fileno())
                if _file_identity(after_handle) != _file_identity(opened):
                    raise ValueError(f"private package snapshot changed during verification: {path}")
            after_path = path.lstat()
        except OSError as exc:
            raise ValueError(f"private package snapshot file is unreadable: {path}") from exc
        if (
            stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _file_identity(after_path) != _file_identity(before)
        ):
            raise ValueError(f"private package snapshot was replaced during verification: {path}")
        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(
                f"private package snapshot SHA-256 mismatch for {path.name}: "
                f"expected {expected}, got {actual}"
            )


def _windows_system_executable(name: str) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_system_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(get_system_directory(buffer, len(buffer)))
    if length == 0 or length >= len(buffer):
        error = ctypes.get_last_error()
        raise OSError(error, "unable to resolve the Windows system directory")
    executable = Path(buffer.value) / name
    try:
        executable_stat = executable.lstat()
    except OSError as exc:
        raise ValueError(f"Windows system executable is unavailable: {name}") from exc
    if (
        stat.S_ISLNK(executable_stat.st_mode)
        or not stat.S_ISREG(executable_stat.st_mode)
        or _contains_windows_reparse_point(executable_stat)
    ):
        raise ValueError(f"Windows system executable is not a regular file: {name}")
    return str(executable)


def _windows_current_sid() -> str:
    try:
        completed = subprocess.run(
            [_windows_system_executable("whoami.exe"), "/user", "/fo", "csv", "/nh"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("unable to resolve current Windows SID for package snapshot fence") from exc
    match = re.search(rb"S-\d+(?:-\d+)+", completed.stdout)
    if match is None:
        raise ValueError("whoami did not return a canonical Windows SID")
    return match.group(0).decode("ascii")


def _run_icacls(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            [_windows_system_executable("icacls.exe"), *arguments],
            capture_output=True,
        )
    except OSError as exc:
        raise ValueError("unable to execute icacls for package snapshot fence") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ValueError(
            f"package snapshot ACL fence failed with exit {completed.returncode}: {detail}"
        )
    return completed


def _windows_open_read_fence(path: Path, *, directory: bool) -> int:
    import ctypes
    from ctypes import wintypes

    file_list_directory = 0x00000001
    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_backup_semantics = 0x02000000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    desired_access = file_list_directory if directory else generic_read
    flags = file_flag_backup_semantics if directory else file_attribute_normal
    handle = create_file(
        str(path),
        desired_access,
        file_share_read,
        None,
        open_existing,
        flags,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    handle_value = ctypes.cast(handle, ctypes.c_void_p).value
    if handle_value in {None, invalid_handle}:
        error = ctypes.get_last_error()
        kind = "directory namespace" if directory else "file read"
        raise OSError(error, f"unable to acquire private package snapshot {kind} fence: {path}")
    return int(handle_value)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        error = ctypes.get_last_error()
        raise OSError(error, "unable to close private package snapshot fence handle")


@contextmanager
def _package_input_write_fence(
    snapshot_dir: Path,
    expected_sha256: Mapping[Path, str],
) -> Iterator[None]:
    """Hold exact package inputs immutable across every final path consumer.

    The release package is built on Windows. A granular deny ACE blocks new file or
    namespace mutations without denying reads. Read-share-only handles then fail
    closed if a writer/delete-capable handle was already open and prevent new
    conflicting opens. Exact membership and hashes are verified only after those
    handles are held, closing the capture-to-consumption pathname TOCTOU.
    """

    root, manifest = _normalize_snapshot_manifest(snapshot_dir, expected_sha256)
    if os.name != "nt":
        _verify_snapshot_manifest(root, manifest)
        yield
        _verify_snapshot_manifest(root, manifest)
        return

    sid = _windows_current_sid()
    principal = f"*{sid}"
    deny_applied = False
    handles: list[int] = []
    try:
        try:
            _run_icacls(
                str(root),
                "/deny",
                f"{principal}:{_WINDOWS_MUTATION_DENY_RIGHTS}",
                "/T",
                "/C",
            )
            deny_applied = True
        except BaseException:
            _run_icacls(str(root), "/remove:d", principal, "/T", "/C", check=False)
            raise

        directories = _expected_snapshot_directories(root, manifest)
        for directory in sorted(directories, key=lambda value: (len(value.parts), str(value))):
            handles.append(_windows_open_read_fence(directory, directory=True))
        for path in sorted(manifest, key=str):
            handles.append(_windows_open_read_fence(path, directory=False))

        _verify_snapshot_manifest(root, manifest)
        yield
        _verify_snapshot_manifest(root, manifest)
    finally:
        close_error: BaseException | None = None
        for handle in reversed(handles):
            try:
                _windows_close_handle(handle)
            except BaseException as exc:  # pragma: no cover - exceptional OS cleanup path
                if close_error is None:
                    close_error = exc
        acl_error: BaseException | None = None
        if deny_applied:
            try:
                _run_icacls(str(root), "/remove:d", principal, "/T", "/C")
            except BaseException as exc:  # pragma: no cover - exceptional OS cleanup path
                acl_error = exc
        if close_error is not None:
            raise close_error
        if acl_error is not None:
            raise acl_error


def _verify_bound_final_package(
    package: Path,
    expected_package_sha256: str,
    *,
    expected_source_sha: str,
) -> dict[str, object]:
    """Verify semantic evidence from the exact producer-bound final ZIP bytes."""

    expected = _require_sha256(
        expected_package_sha256,
        field="final_package_sha256",
    )
    with tempfile.TemporaryDirectory(prefix="autosport-package-verification-") as snapshot_root:
        snapshot_dir = Path(snapshot_root)
        trusted_package = _capture_verified_file(
            package,
            expected,
            snapshot_dir=snapshot_dir,
            snapshot_name=package.name,
            handoff_kind="release package",
        )
        manifest = {trusted_package: expected}
        with _package_input_write_fence(snapshot_dir, manifest):
            verification: dict[str, object] = verify_windows_package(
                trusted_package,
                expected_source_sha=expected_source_sha,
            )
            data_verification = verify_portable_data_tool(trusted_package)
            verification.update(data_verification)
            verification["package_sha256"] = expected
            return verification


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--exe-sha256", required=True)
    parser.add_argument("--data-exe", type=Path, required=True)
    parser.add_argument("--data-exe-sha256", required=True)
    parser.add_argument("--start-file", type=Path, required=True)
    parser.add_argument("--example-dir", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, required=True)
    parser.add_argument("--diagnostic-sha256", required=True)
    parser.add_argument("--accessibility-audit", type=Path, required=True)
    parser.add_argument("--accessibility-audit-sha256", required=True)
    parser.add_argument("--keyboard-audit", type=Path, required=True)
    parser.add_argument("--keyboard-audit-sha256", required=True)
    parser.add_argument("--restart-recovery-audit", type=Path, required=True)
    parser.add_argument("--restart-recovery-audit-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--verification-output", type=Path)
    args = parser.parse_args()

    repo_root = Path.cwd()
    _bind_source_sha_to_checkout(args.source_sha, repo_root=repo_root)

    with tempfile.TemporaryDirectory(prefix="autosport-package-inputs-") as snapshot_root:
        snapshot_dir = Path(snapshot_root)
        expected_snapshot_sha256: dict[Path, str] = {}
        trusted_start_file, trusted_example_dir = _materialize_exact_static_payload(
            repo_root=repo_root,
            source_sha=args.source_sha,
            start_file=args.start_file,
            example_dir=args.example_dir,
            snapshot_dir=snapshot_dir,
            expected_sha256=expected_snapshot_sha256,
        )
        trusted_exe = _capture_verified_executable(
            args.exe,
            args.exe_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="Autosport.exe",
        )
        expected_snapshot_sha256[trusted_exe] = _require_sha256(
            args.exe_sha256,
            field="Autosport.exe_sha256",
        )
        trusted_data_exe = _capture_verified_executable(
            args.data_exe,
            args.data_exe_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="Autosport-Data.exe",
        )
        expected_snapshot_sha256[trusted_data_exe] = _require_sha256(
            args.data_exe_sha256,
            field="Autosport-Data.exe_sha256",
        )
        trusted_diagnostic = _capture_verified_evidence(
            args.diagnostic,
            args.diagnostic_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="packaged-diagnostic.json",
        )
        expected_snapshot_sha256[trusted_diagnostic] = _require_sha256(
            args.diagnostic_sha256,
            field="packaged-diagnostic.json_sha256",
        )
        trusted_accessibility = _capture_verified_evidence(
            args.accessibility_audit,
            args.accessibility_audit_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="accessibility-audit.json",
        )
        expected_snapshot_sha256[trusted_accessibility] = _require_sha256(
            args.accessibility_audit_sha256,
            field="accessibility-audit.json_sha256",
        )
        trusted_keyboard = _capture_verified_evidence(
            args.keyboard_audit,
            args.keyboard_audit_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="keyboard-audit.json",
        )
        expected_snapshot_sha256[trusted_keyboard] = _require_sha256(
            args.keyboard_audit_sha256,
            field="keyboard-audit.json_sha256",
        )
        trusted_restart_recovery = _capture_verified_evidence(
            args.restart_recovery_audit,
            args.restart_recovery_audit_sha256,
            snapshot_dir=snapshot_dir,
            snapshot_name="restart-recovery-audit.json",
        )
        expected_snapshot_sha256[trusted_restart_recovery] = _require_sha256(
            args.restart_recovery_audit_sha256,
            field="restart-recovery-audit.json_sha256",
        )

        with _package_input_write_fence(snapshot_dir, expected_snapshot_sha256):
            output, _base_digest = build_windows_package(
                trusted_exe,
                trusted_start_file,
                trusted_example_dir,
                trusted_diagnostic,
                trusted_accessibility,
                trusted_keyboard,
                trusted_restart_recovery,
                args.output,
                args.source_sha,
            )
            binding = bind_portable_data_tool(
                output,
                trusted_data_exe,
                expected_base_package_sha256=_base_digest,
                expected_autosport_exe_sha256=expected_snapshot_sha256[trusted_exe],
            )

    verification = _verify_bound_final_package(
        output,
        binding["package_sha256"],
        expected_source_sha=args.source_sha,
    )
    if args.verification_output is not None:
        args.verification_output.parent.mkdir(parents=True, exist_ok=True)
        args.verification_output.write_text(
            json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"PACKAGE={output}")
    print(f"SHA256={binding['package_sha256']}")
    print("PACKAGE_VERIFICATION=PASS")
    print("PORTABLE_DATA_TOOLS=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
