"""Offline, non-mutating Windows update *diagnostic* command-line boundary.

The JSON request is operator-controlled, including its expected digests. A
passing diagnostic therefore NEVER proves an independent installed trust root.
Only a future product-owned installer can supply and recheck that root.
No command here downloads, installs, signs or updates.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import re
import stat
import sys
from pathlib import Path

from .windows_update_preflight import preflight_signed_windows_update
from .windows_update_admission import _version, WindowsUpdateAdmissionError, WindowsUpdateDecision
from .windows_update_signature import VerifiedWindowsUpdate
from .windows_update_local_path import UnsafeUpdatePath, require_local_update_path
from .windows_update_transaction_plan import preview_windows_update_transaction
from .windows_update_snapshot import _native_change_time_100ns, _source_parent_identity
from . import windows_update_snapshot as _snapshot

_MAX_REQUEST_BYTES = 16 * 1024
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_HEX_SHA1 = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_HEX_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z", re.ASCII)
_RequestWitness = tuple[tuple[tuple[int, int], ...], tuple[int, int, int, int, int], int | None]
_FIELDS = frozenset({
    "package_path", "expected_package_sha256", "installed_manifest_path",
    "expected_installed_manifest_sha256", "signature_hex", "candidate_version",
    "candidate_schema", "expected_source_sha", "supported_schema_migrations",
})


class UpdateCliError(ValueError):
    """A request was not safely admissible for a non-mutating preflight."""


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateCliError("duplicate request key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise UpdateCliError("nonstandard JSON constant")


def _require_local_path(value: str) -> None:
    """Apply the reusable, no-I/O Windows update path boundary."""
    try:
        require_local_update_path(value)
    except UnsafeUpdatePath as exc:
        raise UpdateCliError(str(exc)) from None


def _reject_redirected_path(path: Path) -> None:
    """Reject pre-existing symlink/junction components before diagnostic I/O.

    Walk from the root so inspecting a descendant does not first traverse an
    untrusted ancestor. A missing candidate file is left to the verifier;
    this lexical/metadata fence is not a race-free descriptor capability.
    """
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            break
        except OSError:
            raise UpdateCliError("cannot inspect update path") from None
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise UpdateCliError("redirected update path is not allowed")


def _request_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """Bind the operator-selected path to a stable regular file identity.

    The metadata witness detects persistent rename/replacement and in-place
    edits. It is not a Windows kernel handle-level anti-reparse guarantee: a
    hostile process that swaps and restores the pathname between checks still
    requires a higher-level trusted filesystem boundary.
    """
    if (
        not stat.S_ISREG(info.st_mode)
        or type(info.st_dev) is not int or info.st_dev < 0
        or type(info.st_ino) is not int or info.st_ino <= 0
        or not 0 < info.st_size <= _MAX_REQUEST_BYTES
    ):
        raise UpdateCliError("request is not a bounded identifiable regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _open_request_fd(path: Path) -> int:
    """No-follow opening for *every* request readback, including on Windows.

    The request is not an archive, but the shared leaf opener implements the
    required CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) semantics. Convert
    native failures to the CLI's sanitized direct-call error boundary.
    """
    try:
        return _snapshot._open_archive_fd(path)
    except _snapshot.WindowsUpdateSnapshotError:
        raise UpdateCliError("request file could not be safely opened") from None



@contextmanager
def _owned_request_fd(path: Path):
    """Own the request descriptor without masking cancellation on cleanup.

    A native/CRT close wrapper may raise a path-bearing non-OSError after
    releasing its handle. On normal exit, this invalidates the diagnostic;
    during KeyboardInterrupt/SystemExit or another primary failure, preserve
    that original exception. Never print a native exception or its context.
    """
    fd = _open_request_fd(path)
    try:
        yield fd
    except BaseException:
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    else:
        try:
            os.close(fd)
        except Exception:
            raise UpdateCliError("request descriptor close failed") from None


def _read_request(path: str | Path) -> tuple[dict[str, object], _RequestWitness]:
    # Reject path subclasses before __str__/__fspath__ can execute callbacks.
    # This is a direct-call boundary as well as the CLI parser.
    if type(path) not in (str, type(Path())):
        raise UpdateCliError("request path must be canonical")
    _require_local_path(str(path))
    path = Path(path)
    _reject_redirected_path(path)
    # Bind every ancestor directory, not just the request leaf. A directory
    # rename can preserve the leaf inode via an existing hardlink, defeating
    # a file-only before/after identity check without introducing symlinks.
    parents_before = _source_parent_identity(path)
    # Capture pathname identity BEFORE opening. A held descriptor alone does
    # not prove the user-selected path still identifies that same request.
    entry_before = _request_identity(path.lstat())
    with _owned_request_fd(path) as fd:
        before = _request_identity(os.fstat(fd))
        before_change = _native_change_time_100ns(fd)
        # On Windows the ctime/mtime views of lstat(path) and fstat(fd)
        # can differ even for one unchanged file. Cross-check stable
        # device/inode/size, not cross-API timestamp representations.
        if before[:3] != entry_before[:3]:
            raise UpdateCliError("request path changed while opening")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(_MAX_REQUEST_BYTES + 1)
        after = _request_identity(os.fstat(fd))
        after_change = _native_change_time_100ns(fd)
        # Detect a path replaced during the read even if the held file's bytes
        # remained unchanged. No success-shaped report on a stale path.
        entry_after = _request_identity(path.lstat())
        _reject_redirected_path(path)
        if (
            before != after or before_change != after_change
            or _source_parent_identity(path) != parents_before
            or entry_after != entry_before
            or len(data) != before[2]
        ):
            raise UpdateCliError("request changed while being read")
    try:
        result = json.loads(
            data.decode("utf-8", "strict"), object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise UpdateCliError("request must be strict UTF-8 JSON") from None
    if type(result) is not dict or set(result) != _FIELDS:
        raise UpdateCliError("request fields are incomplete or unknown")
    # Parsing may invoke hooks and delay validation. The descriptor has been
    # closed, so both the parent chain AND the named leaf must still match the
    # operator-selected request. A same-byte replacement changes the inode;
    # a same-inode rewrite changes the metadata witness. This is a fail-closed
    # diagnostic guard, not a kernel handle-level installer trust authority.
    _reject_redirected_path(path)
    if _source_parent_identity(path) != parents_before:
        raise UpdateCliError("request parent changed during parsing")
    if _request_identity(path.lstat()) != entry_after:
        raise UpdateCliError("request changed during parsing")
    # On Windows, st_ctime_ns can represent file creation time, not the
    # independent native ChangeTime. An in-place same-byte rewrite during
    # JSON parsing can therefore preserve the ordinary stat witness. Reopen
    # without following the final symlink and recheck the original handle's
    # ChangeTime before the parsed request can authorize even a diagnostic.
    # This remains a consistency guard, not an installer-grade atomic lock.
    with _owned_request_fd(path) as postparse_fd:
        postparse_opened = _request_identity(os.fstat(postparse_fd))
        postparse_change = _native_change_time_100ns(postparse_fd)
        postparse_entry = _request_identity(path.lstat())
        _reject_redirected_path(path)
        if (
            postparse_opened[:3] != entry_after[:3]
            or postparse_opened != after
            or postparse_entry != entry_after
            or postparse_change != after_change
            or _source_parent_identity(path) != parents_before
        ):
            raise UpdateCliError("request changed after parsing")
    # Return the final post-parse witness *with* the parsed bytes. Capturing
    # a new baseline in run() would leave a gap: the operator path could be
    # replaced between parsing and the first downstream consistency check.
    return result, (parents_before, postparse_opened, postparse_change)



def _request_witness(path: Path) -> _RequestWitness:
    """Witness the operator request across the *whole* diagnostic operation.

    The parser already checks in-read and post-parse mutations. A signature
    verifier or preview callback may take considerably longer, however. Do
    not report success for a request that was replaced or rewritten while
    those downstream operations were running. This is a consistency witness,
    not an atomic installer capability or a substitute for a trusted root.
    """
    _reject_redirected_path(path)
    parents = _source_parent_identity(path)
    entry = _request_identity(path.lstat())
    with _owned_request_fd(path) as fd:
        opened = _request_identity(os.fstat(fd))
        changed = _native_change_time_100ns(fd)
        leaf = _request_identity(path.lstat())
        _reject_redirected_path(path)
        # Across path/descriptor APIs compare identity and size only;
        # preserve each API's full metadata for same-view rechecks.
        if (entry[:3] != opened[:3] or leaf != entry
                or _source_parent_identity(path) != parents):
            raise UpdateCliError("request changed during consistency witness")
    return parents, opened, changed


def _path(value: object, parent: Path) -> Path:
    if type(value) is not str or not value or "\x00" in value:
        raise UpdateCliError("invalid request path")
    _require_local_path(value)
    path = Path(value)
    candidate = path if path.is_absolute() else parent / path
    _reject_redirected_path(candidate)
    return candidate


def _digest(value: object, pattern: re.Pattern[str], label: str) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        raise UpdateCliError(f"invalid {label}")
    return value


def _arguments(request: dict[str, object], parent: Path) -> dict[str, object]:
    migrations = request["supported_schema_migrations"]
    if type(migrations) is not list or len(migrations) > 256:
        raise UpdateCliError("invalid migration chain")
    edges: list[tuple[int, int]] = []
    for edge in migrations:
        if type(edge) is not list or len(edge) != 2 or any(type(x) is not int for x in edge):
            raise UpdateCliError("invalid migration edge")
        edges.append((edge[0], edge[1]))
    schema = request["candidate_schema"]
    if type(schema) is not int or not 1 <= schema <= 2**31 - 1:
        raise UpdateCliError("invalid candidate schema")
    version = request["candidate_version"]
    # Reject an unsupported version before touching package/manifest paths or
    # invoking the crypto backend. Share the release/installed-state bounds.
    try:
        _version(version)
    except WindowsUpdateAdmissionError:
        raise UpdateCliError("invalid candidate version") from None
    signature = _digest(request["signature_hex"], _HEX_SIGNATURE, "signature")
    return {
        "package_path": _path(request["package_path"], parent),
        "expected_package_sha256": _digest(request["expected_package_sha256"], _HEX_SHA256, "package SHA-256"),
        "installed_manifest_path": _path(request["installed_manifest_path"], parent),
        "expected_installed_manifest_sha256": _digest(
            request["expected_installed_manifest_sha256"], _HEX_SHA256, "installed SHA-256"
        ),
        "signature": bytes.fromhex(signature),
        "candidate_version": version,
        "candidate_schema": schema,
        "expected_source_sha": _digest(request["expected_source_sha"], _HEX_SHA1, "source SHA"),
        "supported_schema_migrations": tuple(edges),
    }


def _same_exact_primitives(observed: object, expected: object) -> bool:
    """Compare bounded JSON primitives without invoking attacker-defined coercions.

    The preview is a second internal boundary. It must not smuggle private
    paths, executable objects, extra fields, or an authorization-looking flag
    into a successful CLI response, even if its producer is faulty.
    """
    if type(observed) is not type(expected):
        return False
    if type(expected) is dict:
        if any(type(key) is not str for key in observed):
            return False
        if observed.keys() != expected.keys():
            return False
        return all(_same_exact_primitives(observed[key], expected[key]) for key in expected)
    if type(expected) is list:
        return len(observed) == len(expected) and all(
            _same_exact_primitives(actual, wanted)
            for actual, wanted in zip(observed, expected)
        )
    return observed == expected


def _expected_transaction_preview(
    verified: VerifiedWindowsUpdate, *,
    migrations: tuple[tuple[int, int], ...],
) -> dict[str, object]:
    """Construct a sanitized projection exclusively from validated facts."""
    decision = verified.decision
    available = decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    return {
        "status": "PREVIEW_ONLY_UNTRUSTED_OPERATOR_ROOT",
        "disposition": decision.disposition,
        "from_version": decision.from_version,
        "to_version": verified.version,
        "from_schema": decision.from_schema,
        "to_schema": verified.schema,
        "package_sha256": verified.package_sha256,
        "source_sha": verified.source_sha,
        "backup_required": available,
        "migration_steps": [
            {"from_schema": old, "to_schema": new} for old, new in migrations
        ],
        "restart_recovery_required": available,
        "independent_installer_trust_required": True,
        "install_authorized": False,
    }


def _detached_verified_receipt(value: VerifiedWindowsUpdate) -> VerifiedWindowsUpdate:
    """Copy validated primitive facts across a mutable backend boundary.

    ``frozen=True`` does not prevent ``object.__setattr__``. A downstream
    preview callback must never be able to rewrite the signed receipt or its
    nested FROM identity after the CLI has validated it. The copy is also
    validated on reconstruction, so a forged nested object cannot cross.
    """
    if type(value) is not VerifiedWindowsUpdate or type(value.decision) is not WindowsUpdateDecision:
        raise UpdateCliError("noncanonical diagnostic receipt")
    decision = value.decision
    return VerifiedWindowsUpdate(
        decision=WindowsUpdateDecision(
            disposition=decision.disposition,
            from_version=decision.from_version,
            to_version=decision.to_version,
            from_schema=decision.from_schema,
            to_schema=decision.to_schema,
            install_authorized=decision.install_authorized,
        ),
        package_sha256=value.package_sha256,
        source_sha=value.source_sha,
        version=value.version,
        schema=value.schema,
        install_authorized=value.install_authorized,
    )


def run(request_path: str | Path) -> tuple[int, dict[str, object]]:
    """Return sanitized, explicitly non-authoritative diagnostic status."""
    try:
        # Bind the request and all its relative package/manifest references to
        # one lexical working-directory snapshot. Resolving the parent again
        # after reading would silently rebase them if cwd changed meanwhile.
        # Validate the *raw* spelling before Path.absolute() normalizes dots.
        if type(request_path) not in (str, type(Path())):
            raise UpdateCliError("request path must be canonical")
        _require_local_path(str(request_path))
        anchored_request = Path(request_path).absolute()
        request, request_before = _read_request(anchored_request)
        # The witness originates inside the parser. Never accept a fresh
        # baseline after the parser returns: a concurrent writer can replace
        # the request between those two calls with valid but different bytes.
        if _request_witness(anchored_request) != request_before:
            raise UpdateCliError("request changed after parsing")
        arguments = _arguments(request, anchored_request.parent)
        result = preflight_signed_windows_update(**arguments)
        # A success-shaped backend object is not permission to serialize its
        # fields. In particular, a forged disposition may contain local paths
        # or secrets. Revalidate the exact canonical receipt and bind it to
        # the independently parsed request before emitting *any* success.
        if type(result) is not VerifiedWindowsUpdate:
            raise UpdateCliError("noncanonical diagnostic receipt")
        result.__post_init__()
        if (
            result.package_sha256 != arguments["expected_package_sha256"]
            or result.source_sha != arguments["expected_source_sha"]
            or result.version != arguments["candidate_version"]
            or result.schema != arguments["candidate_schema"]
            or result.install_authorized is not False
            or result.decision.install_authorized is not False
        ):
            raise UpdateCliError("diagnostic receipt differs from request")
        # Detach the accepted facts from the preflight backend, then hand the
        # preview a *separate* copy. A malicious/buggy preview must not mutate
        # the baseline from which we validate and emit operator-facing JSON.
        accepted = _detached_verified_receipt(result)
        disposition = accepted.decision.disposition
        if type(disposition) is not str or disposition not in (
            "NO_CHANGE", "ELIGIBLE_FOR_TRANSACTION_PREPARE"
        ):
            raise UpdateCliError("unknown diagnostic disposition")
        if disposition == "NO_CHANGE" and arguments["supported_schema_migrations"]:
            raise UpdateCliError("no-change receipt contains migration chain")
        preview_input = _detached_verified_receipt(accepted)
        preview = preview_windows_update_transaction(
            preview_input, supported_schema_migrations=arguments["supported_schema_migrations"]
        )
        expected_preview = _expected_transaction_preview(
            accepted, migrations=arguments["supported_schema_migrations"]
        )
        if (
            _detached_verified_receipt(preview_input) != accepted
            or not _same_exact_primitives(preview, expected_preview)
        ):
            raise UpdateCliError("noncanonical transaction preview")
        # Preflight/signature/preview work may outlive the initial request
        # read. Reject an inode swap, in-place rewrite or ancestor rebind
        # before reporting even non-authorizing diagnostic success.
        if _request_witness(anchored_request) != request_before:
            raise UpdateCliError("request changed during diagnostic")
        # Never serialize an object owned by a backend; return only locally
        # constructed JSON primitives after exact structural validation.
        available = disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
        # Emit only validated, locally parsed primitive values, never a
        # backend-controlled exception, pathname or arbitrary string.
        return 0, {
            "status": ("DIAGNOSTIC_PASS_UNTRUSTED_ROOT" if available
                       else "DIAGNOSTIC_NO_CHANGE_UNTRUSTED_ROOT"),
            "independent_trust_root_verified": False,
            "disposition": disposition,
            "candidate_version": arguments["candidate_version"],
            "candidate_schema": arguments["candidate_schema"],
            "package_sha256": arguments["expected_package_sha256"],
            "update_available": available,
            "transaction_preview": expected_preview,
            "install_authorized": False,
            "human_tested": False,
            "nvda_verified": False,
        }
    except (
        OSError, ValueError, TypeError, AttributeError, KeyError,
        ImportError, SyntaxError, RuntimeError, OverflowError,
    ):
        return 2, {"status": "DIAGNOSTIC_REJECTED", "install_authorized": False,
                   "independent_trust_root_verified": False,
                   "human_tested": False, "nvda_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autosport Windows update diagnostic (not installer trust authority)")
    parser.add_argument("request", type=str, help="Local UTF-8 JSON request; no secrets")
    args = parser.parse_args(argv)
    code, report = run(args.request)
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
