from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .integrity import atomic_write_json as _path_atomic_write_json
from .workspace_lock import WorkspaceEconomicLock, WorkspaceEconomicLockError


_SCHEMA_VERSION = 1
_KIND = "autosport-workspace-evidence-manifest"
_CHUNK_SIZE = 1024 * 1024
_FIXED_EVIDENCE_NAMES = (
    "decisions.jsonl",
    "paper_book.json",
    "run_registry.json",
    "source_health.json",
)
_MANIFEST_KEYS = {
    "schema_version",
    "kind",
    "file_count",
    "files",
    "expected_fixed_evidence_paths",
    "missing_fixed_evidence_paths",
    "fixed_evidence_set_complete",
    "run_summary_count",
    "file_contents_included",
    "market_database_included",
    "raw_historical_or_provider_bytes_included",
    "environment_or_credential_values_included",
    "arbitrary_workspace_files_included",
    "real_money_execution",
    "manifest_sha256",
}
_FILE_KEYS = {"path", "size_bytes", "sha256"}
_FALSE_TRUTH_FIELDS = (
    "file_contents_included",
    "market_database_included",
    "raw_historical_or_provider_bytes_included",
    "environment_or_credential_values_included",
    "arbitrary_workspace_files_included",
    "real_money_execution",
)
_BOUND_POSIX_OUTPUT: ContextVar[tuple[int, Path] | None] = ContextVar(
    "autosport_evidence_bound_posix_output",
    default=None,
)


def _is_canonical_run_summary_name(name: str) -> bool:
    if not name.startswith("run-") or not name.endswith(".json"):
        return False
    raw_id = name[len("run-") : -len(".json")]
    try:
        parsed = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == raw_id


def _is_canonical_evidence_name(name: str) -> bool:
    return name in _FIXED_EVIDENCE_NAMES or _is_canonical_run_summary_name(name)


def _canonical_source_names(workspace: Path) -> tuple[str, ...]:
    names: set[str] = set()
    for name in _FIXED_EVIDENCE_NAMES:
        if os.path.lexists(workspace / name):
            names.add(name)
    for entry in workspace.iterdir():
        if _is_canonical_run_summary_name(entry.name):
            names.add(entry.name)
    return tuple(sorted(names))


def _stable_stat_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _read_only_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0)


def _path_still_matches_open_file(
    path: Path,
    descriptor: int,
    expected_path_stat: os.stat_result,
) -> bool:
    """Bind one lexical regular-file path to an already-open file handle.

    Windows 3.12+ may obtain pathname stat data through a different filesystem
    API than fstat(), and that path API can omit identity fields such as st_dev.
    Comparing those two stat domains with os.path.samestat() can therefore reject
    an unchanged file. Compare path metadata only with fresh path metadata, and
    use two open handles for file identity instead.
    """

    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        return False
    if not _stable_stat_metadata(expected_path_stat, current):
        return False

    verification_descriptor = os.open(path, _read_only_open_flags())
    try:
        if not os.path.sameopenfile(descriptor, verification_descriptor):
            return False
        current_after_open = os.stat(path, follow_symlinks=False)
        return stat.S_ISREG(current_after_open.st_mode) and _stable_stat_metadata(
            expected_path_stat,
            current_after_open,
        )
    finally:
        os.close(verification_descriptor)


def _open_and_hash_regular_file(path: Path) -> tuple[int, str]:
    """Hash one stable regular-file snapshot without following path symlinks."""

    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"canonical evidence path is not a regular file: {path.name}")

    descriptor = os.open(path, _read_only_open_flags())
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not _path_still_matches_open_file(
                path,
                handle.fileno(),
                before,
            ):
                raise ValueError(f"canonical evidence path changed before snapshot: {path.name}")

            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)

            opened_after = os.fstat(handle.fileno())
            if not _stable_stat_metadata(opened, opened_after):
                raise ValueError(f"canonical evidence file mutated during snapshot: {path.name}")
            if not _path_still_matches_open_file(path, handle.fileno(), before):
                raise ValueError(f"canonical evidence path changed during snapshot: {path.name}")
            return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _manifest_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolved(path: Path, *, strict: bool) -> Path:
    try:
        return path.resolve(strict=strict)
    except OSError as exc:
        raise ValueError(f"cannot resolve path {path}: {exc}") from exc


def _resolve_output_destination(workspace: Path, output: Path) -> Path:
    workspace_root = _resolved(workspace, strict=True)
    output_path = _resolved(output, strict=False)
    try:
        output_path.relative_to(workspace_root)
    except ValueError:
        return output_path
    raise ValueError(
        "output path must be outside the Autosport workspace; "
        "must not overwrite canonical workspace evidence"
    )


def _posix_directory_open_flags() -> int:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    no_follow_flag = getattr(os, "O_NOFOLLOW", 0)
    if not directory_flag or not no_follow_flag:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks no-follow directory descriptor support for evidence export",
        )
    return (
        os.O_RDONLY
        | directory_flag
        | no_follow_flag
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_posix_directory(path: Path) -> int:
    descriptor = os.open(path, _posix_directory_open_flags())
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError(f"output path component is not a directory: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _posix_directory_is_within(candidate_descriptor: int, ancestor_descriptor: int) -> bool:
    if os.open not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks descriptor-relative directory traversal for evidence export",
        )

    current = os.dup(candidate_descriptor)
    try:
        while True:
            if os.path.sameopenfile(current, ancestor_descriptor):
                return True
            parent = os.open("..", _posix_directory_open_flags(), dir_fd=current)
            try:
                if os.path.sameopenfile(current, parent):
                    return False
            except BaseException:
                os.close(parent)
                raise
            os.close(current)
            current = parent
    finally:
        os.close(current)


def _open_or_create_posix_child_directory(parent_descriptor: int, name: str) -> int:
    if os.mkdir not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks descriptor-relative directory creation for evidence export",
        )
    try:
        return os.open(name, _posix_directory_open_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o777, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        return os.open(name, _posix_directory_open_flags(), dir_fd=parent_descriptor)


def _atomic_write_json_at_directory(
    parent_descriptor: int,
    destination_name: str,
    payload: dict[str, Any],
) -> None:
    if destination_name in {"", ".", ".."} or Path(destination_name).name != destination_name:
        raise ValueError("evidence export destination must name one file")
    if os.open not in os.supports_dir_fd or os.replace not in os.supports_dir_fd:
        raise OSError(
            errno.ENOTSUP,
            "platform lacks descriptor-relative atomic publication for evidence export",
        )

    temporary_name = f".{destination_name}.{uuid.uuid4().hex}.tmp"
    open_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor: int | None = None
    temporary_exists = False
    try:
        descriptor = os.open(
            temporary_name,
            open_flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        temporary_exists = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            destination_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_exists = False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Preserve the canonical writer API while honoring a bound POSIX parent.

    The context binding is set only by evidence export after it has proved that the
    opened directory object is outside the workspace. Keeping this name as the public
    seam also preserves existing fault-injection tests around publication.
    """

    destination = Path(path)
    binding = _BOUND_POSIX_OUTPUT.get()
    if binding is None:
        _path_atomic_write_json(destination, payload)
        return
    parent_descriptor, expected_destination = binding
    if destination != expected_destination:
        raise RuntimeError("bound evidence output destination changed before publication")
    _atomic_write_json_at_directory(parent_descriptor, destination.name, payload)


def _publish_posix_bound_output(
    workspace: Path,
    destination: Path,
    payload: dict[str, Any],
) -> None:
    workspace_descriptor = _open_posix_directory(_resolved(workspace, strict=True))
    current_descriptor: int | None = None
    token = None
    try:
        parent = destination.parent
        if not parent.is_absolute() or not parent.anchor:
            raise ValueError("evidence export destination must resolve to an absolute path")
        current_descriptor = _open_posix_directory(Path(parent.anchor))
        for component in parent.parts[1:]:
            if _posix_directory_is_within(current_descriptor, workspace_descriptor):
                raise ValueError(
                    "output path must be outside the Autosport workspace; "
                    "must not overwrite canonical workspace evidence"
                )
            next_descriptor = _open_or_create_posix_child_directory(
                current_descriptor,
                component,
            )
            os.close(current_descriptor)
            current_descriptor = next_descriptor

        if _posix_directory_is_within(current_descriptor, workspace_descriptor):
            raise ValueError(
                "output path must be outside the Autosport workspace; "
                "must not overwrite canonical workspace evidence"
            )

        token = _BOUND_POSIX_OUTPUT.set((current_descriptor, destination))
        atomic_write_json(destination, payload)
    finally:
        if token is not None:
            _BOUND_POSIX_OUTPUT.reset(token)
        if current_descriptor is not None:
            os.close(current_descriptor)
        os.close(workspace_descriptor)


def _windows_api_path(path: Path) -> str:
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def _publish_windows_bound_output(
    workspace: Path,
    destination: Path,
    payload: dict[str, Any],
) -> None:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_file_information = kernel32.GetFileInformationByHandle
    get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_file_information.restype = wintypes.BOOL
    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = (wintypes.LPCWSTR, ctypes.c_void_p)
    create_directory.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_existing = 3
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    error_file_not_found = 2
    error_path_not_found = 3
    error_already_exists = 183
    invalid_handle_value = ctypes.c_void_p(-1).value

    def open_directory(path: Path, *, deny_delete: bool) -> tuple[object, tuple[int, int, int]]:
        share_mode = file_share_read | file_share_write
        if not deny_delete:
            share_mode |= 0x00000004
        handle = create_file(
            _windows_api_path(path),
            0,
            share_mode,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        if handle == invalid_handle_value:
            error_code = ctypes.get_last_error()
            if error_code in (error_file_not_found, error_path_not_found):
                raise FileNotFoundError(error_code, "output directory path does not exist", str(path))
            raise ctypes.WinError(error_code)
        information = ByHandleFileInformation()
        if not get_file_information(handle, ctypes.byref(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            close_handle(handle)
            raise error
        if not information.dwFileAttributes & file_attribute_directory:
            close_handle(handle)
            raise ValueError(f"output path component is not a directory: {path}")
        if information.dwFileAttributes & file_attribute_reparse_point:
            close_handle(handle)
            raise ValueError(f"output directory ancestry contains a reparse point: {path}")
        identity = (
            int(information.dwVolumeSerialNumber),
            int(information.nFileIndexHigh),
            int(information.nFileIndexLow),
        )
        return handle, identity

    workspace_root = _resolved(workspace, strict=True)
    workspace_handle, workspace_identity = open_directory(workspace_root, deny_delete=True)
    handles: list[object] = [workspace_handle]
    primary_error: BaseException | None = None
    try:
        parent = destination.parent
        if not parent.is_absolute() or not parent.anchor:
            raise ValueError("evidence export destination must resolve to an absolute path")
        current_path = Path(parent.anchor)
        prefixes = [current_path]
        for component in parent.parts[1:]:
            current_path = current_path / component
            prefixes.append(current_path)

        for prefix in prefixes:
            try:
                handle, identity = open_directory(prefix, deny_delete=True)
            except FileNotFoundError:
                if not create_directory(_windows_api_path(prefix), None):
                    error_code = ctypes.get_last_error()
                    if error_code != error_already_exists:
                        raise ctypes.WinError(error_code)
                handle, identity = open_directory(prefix, deny_delete=True)
            handles.append(handle)
            if identity == workspace_identity:
                raise ValueError(
                    "output path must be outside the Autosport workspace; "
                    "must not overwrite canonical workspace evidence"
                )

        # Every parent component now has an open handle that deliberately denies
        # FILE_SHARE_DELETE, so Windows rename/delete/reparse substitution cannot
        # reinterpret the pathname while the canonical atomic writer publishes.
        checked_destination = _resolve_output_destination(workspace, destination)
        if os.path.normcase(str(checked_destination)) != os.path.normcase(str(destination)):
            raise ValueError("output path changed while binding publication ancestry")
        atomic_write_json(destination, payload)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: BaseException | None = None
        for handle in reversed(handles):
            if close_handle(handle):
                continue
            close_error = ctypes.WinError(ctypes.get_last_error())
            if primary_error is not None:
                try:
                    primary_error.add_note(
                        f"evidence output directory handle close also failed: {close_error}"
                    )
                except BaseException:
                    pass
            elif cleanup_error is None:
                cleanup_error = close_error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def _publish_bound_output(
    workspace: Path,
    requested_destination: Path,
    payload: dict[str, Any],
) -> Path:
    destination = _resolve_output_destination(workspace, requested_destination)
    if os.name == "nt":
        _publish_windows_bound_output(workspace, destination, payload)
    else:
        _publish_posix_bound_output(workspace, destination, payload)
    return destination


def _reject_duplicate_manifest_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("evidence manifest contains duplicate JSON object keys")
        result[key] = value
    return result


def _reject_manifest_constant(value: str) -> None:
    raise ValueError("evidence manifest contains non-standard JSON constants")


def _is_sha256_text(value: object) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _validate_manifest_payload(raw: object) -> dict[str, Any]:
    if type(raw) is not dict:
        raise ValueError("evidence manifest root must be a JSON object")
    if set(raw) != _MANIFEST_KEYS:
        raise ValueError("evidence manifest fields do not match schema version 1")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("evidence manifest schema_version must be exact integer 1")
    if raw["kind"] != _KIND or type(raw["kind"]) is not str:
        raise ValueError("evidence manifest kind is invalid")
    if type(raw["file_count"]) is not int or raw["file_count"] <= 0:
        raise ValueError("evidence manifest file_count must be a positive integer")
    if type(raw["run_summary_count"]) is not int or raw["run_summary_count"] < 0:
        raise ValueError("evidence manifest run_summary_count must be a non-negative integer")
    if type(raw["files"]) is not list:
        raise ValueError("evidence manifest files must be a JSON array")
    if raw["expected_fixed_evidence_paths"] != list(_FIXED_EVIDENCE_NAMES):
        raise ValueError("evidence manifest expected fixed paths are invalid")
    if type(raw["missing_fixed_evidence_paths"]) is not list:
        raise ValueError("evidence manifest missing fixed paths must be a JSON array")
    if type(raw["fixed_evidence_set_complete"]) is not bool:
        raise ValueError("evidence manifest fixed completeness flag must be boolean")
    for field in _FALSE_TRUTH_FIELDS:
        if raw[field] is not False:
            raise ValueError(f"evidence manifest truth field must be false: {field}")
    if not _is_sha256_text(raw["manifest_sha256"]):
        raise ValueError("evidence manifest manifest_sha256 is invalid")

    files = raw["files"]
    if len(files) != raw["file_count"]:
        raise ValueError("evidence manifest file_count does not match files")

    paths: list[str] = []
    for item in files:
        if type(item) is not dict or set(item) != _FILE_KEYS:
            raise ValueError("evidence manifest file record is invalid")
        path = item["path"]
        if type(path) is not str or not _is_canonical_evidence_name(path):
            raise ValueError("evidence manifest contains a noncanonical evidence path")
        if type(item["size_bytes"]) is not int or item["size_bytes"] < 0:
            raise ValueError("evidence manifest file size must be a non-negative integer")
        if not _is_sha256_text(item["sha256"]):
            raise ValueError("evidence manifest file SHA-256 is invalid")
        paths.append(path)

    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("evidence manifest file paths must be unique and sorted")

    missing_fixed = [name for name in _FIXED_EVIDENCE_NAMES if name not in paths]
    if raw["missing_fixed_evidence_paths"] != missing_fixed:
        raise ValueError("evidence manifest missing fixed paths do not match files")
    if raw["fixed_evidence_set_complete"] is not (not missing_fixed):
        raise ValueError("evidence manifest fixed completeness flag does not match files")
    run_summary_count = sum(_is_canonical_run_summary_name(name) for name in paths)
    if raw["run_summary_count"] != run_summary_count:
        raise ValueError("evidence manifest run_summary_count does not match files")

    payload_without_hash = dict(raw)
    manifest_digest = payload_without_hash.pop("manifest_sha256")
    if _manifest_sha256(payload_without_hash) != manifest_digest:
        raise ValueError("evidence manifest manifest_sha256 does not match payload")
    return raw


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_manifest_keys,
            parse_constant=_reject_manifest_constant,
        )
    except UnicodeDecodeError as exc:
        raise ValueError("evidence manifest is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("evidence manifest is not valid JSON") from exc
    except RecursionError as exc:
        raise ValueError("evidence manifest JSON nesting is too deep") from exc
    return _validate_manifest_payload(raw)


def export_evidence_manifest(workspace: str | Path, output: str | Path) -> dict[str, Any]:
    """Publish a deterministic metadata-only manifest for canonical workspace evidence.

    The export intentionally contains no workspace file contents, market database bytes,
    raw provider/history bytes, environment values, credentials, cookies, tokens or
    arbitrary workspace files. It records only canonical relative evidence names plus
    byte counts and SHA-256 digests.
    """

    root = Path(workspace)
    requested_destination = Path(output)
    if not root.exists() or not root.is_dir():
        raise ValueError("workspace must be an existing directory")
    _resolve_output_destination(root, requested_destination)

    # Refuse an empty/non-evidence directory before taking the economic lock so an
    # export attempt does not create lock metadata in an unrelated empty directory.
    if not _canonical_source_names(root):
        raise ValueError("workspace contains no canonical exportable evidence")

    # Keep the exclusive critical section limited to source discovery + hashing.
    # The manifest payload is immutable ordinary Python data after this block, so a
    # slow/failing destination write must not unnecessarily block replay/settlement.
    with WorkspaceEconomicLock(root):
        names = _canonical_source_names(root)
        if not names:
            raise ValueError("workspace canonical evidence disappeared before snapshot")

        files: list[dict[str, Any]] = []
        for name in names:
            size, digest = _open_and_hash_regular_file(root / name)
            files.append(
                {
                    "path": name,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )

        missing_fixed = [name for name in _FIXED_EVIDENCE_NAMES if name not in names]
        run_summary_count = sum(_is_canonical_run_summary_name(name) for name in names)
        payload: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "file_count": len(files),
            "files": files,
            "expected_fixed_evidence_paths": list(_FIXED_EVIDENCE_NAMES),
            "missing_fixed_evidence_paths": missing_fixed,
            "fixed_evidence_set_complete": not missing_fixed,
            "run_summary_count": run_summary_count,
            "file_contents_included": False,
            "market_database_included": False,
            "raw_historical_or_provider_bytes_included": False,
            "environment_or_credential_values_included": False,
            "arbitrary_workspace_files_included": False,
            "real_money_execution": False,
        }
        payload["manifest_sha256"] = _manifest_sha256(payload)

    # Publication remains outside WorkspaceEconomicLock, but its parent directory is
    # now bound to a stable directory object before any temp file or replace happens.
    # POSIX uses descriptor-relative mkdir/temp/replace; Windows keeps every resolved
    # parent component open without FILE_SHARE_DELETE until canonical atomic publication
    # finishes. A post-check ancestry substitution therefore cannot redirect bytes back
    # into the Autosport workspace.
    _publish_bound_output(root, requested_destination, payload)
    return payload


def verify_evidence_manifest(manifest: str | Path, workspace: str | Path) -> dict[str, Any]:
    """Fail closed unless one manifest exactly matches current canonical workspace evidence."""

    manifest_path = Path(manifest)
    root = Path(workspace)
    payload = _load_manifest(manifest_path)
    if not root.exists() or not root.is_dir():
        raise ValueError("workspace must be an existing directory")

    expected_files = {item["path"]: item for item in payload["files"]}
    expected_names = tuple(expected_files)
    with WorkspaceEconomicLock(root):
        current_names = _canonical_source_names(root)
        if current_names != expected_names:
            raise ValueError("workspace canonical evidence set does not match manifest")
        for name in current_names:
            size, digest = _open_and_hash_regular_file(root / name)
            expected = expected_files[name]
            if size != expected["size_bytes"] or digest != expected["sha256"]:
                raise ValueError(f"workspace evidence does not match manifest: {name}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-export-evidence",
        description="Export a deterministic metadata-only Autosport workspace evidence manifest",
    )
    parser.add_argument("workspace", type=Path, help="existing Autosport workspace")
    parser.add_argument("--output", type=Path, required=True, help="destination JSON manifest")
    return parser


def build_verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autosport-verify-evidence",
        description="Verify an Autosport evidence manifest against current workspace evidence",
    )
    parser.add_argument("manifest", type=Path, help="evidence manifest JSON")
    parser.add_argument("--workspace", type=Path, required=True, help="existing Autosport workspace")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = export_evidence_manifest(args.workspace, args.output)
    except (OSError, ValueError, WorkspaceEconomicLockError) as exc:
        print(f"evidence_export=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"evidence_export=PASS files={report['file_count']} "
        f"run_summaries={report['run_summary_count']} "
        f"fixed_evidence_set_complete={str(report['fixed_evidence_set_complete']).lower()} "
        f"manifest_sha256={report['manifest_sha256']}"
    )
    print(
        "file_contents_included=false market_database_included=false "
        "raw_historical_or_provider_bytes_included=false "
        "environment_or_credential_values_included=false real_money_execution=false"
    )
    print(f"output={args.output}")
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    args = build_verify_parser().parse_args(argv)
    try:
        report = verify_evidence_manifest(args.manifest, args.workspace)
    except (OSError, ValueError, WorkspaceEconomicLockError) as exc:
        print(f"evidence_verify=FAIL_CLOSED error={exc}")
        return 3

    print(
        f"evidence_verify=PASS workspace_match=true files={report['file_count']} "
        f"run_summaries={report['run_summary_count']} "
        f"fixed_evidence_set_complete={str(report['fixed_evidence_set_complete']).lower()} "
        f"manifest_sha256={report['manifest_sha256']} real_money_execution=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
