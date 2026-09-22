from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Mapping


_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "product",
        "generation",
        "version",
        "executable_relpath",
        "executable_sha256",
        "user_scope",
    }
)
_OWNER_KEYS = frozenset(
    {
        "schema_version",
        "pid",
        "process_start_nonce",
        "user_scope",
        "generation",
        "version",
        "executable_sha256",
    }
)
_HEX = frozenset("0123456789abcdef")


class LaunchIdentityError(ValueError):
    """Raised when launch identity evidence is malformed, ambiguous, or inconsistent."""


class InstanceDecision(str, Enum):
    ACQUIRE = "ACQUIRE"
    BLOCK_ACTIVE = "BLOCK_ACTIVE"
    RECLAIM_STALE = "RECLAIM_STALE"


@dataclass(frozen=True)
class ResolvedGeneration:
    generation: int
    version: str
    executable_path: Path
    executable_sha256: str
    manifest_sha256: str
    user_scope: str


@dataclass(frozen=True)
class LaunchOwnerIdentity:
    pid: int
    process_start_nonce: str
    user_scope: str
    generation: int
    version: str
    executable_sha256: str


def _require_sha256(value: Any, *, field: str) -> str:
    if type(value) is not str or len(value) != 64 or value != value.lower():
        raise LaunchIdentityError(f"{field} must be a lowercase SHA-256 hex digest")
    if any(ch not in _HEX for ch in value):
        raise LaunchIdentityError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _require_nonempty(value: Any, *, field: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise LaunchIdentityError(f"{field} must be a non-empty trimmed string")
    return value


def _require_generation(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise LaunchIdentityError("generation must be a non-negative integer")
    return value


def _manifest_object(manifest_bytes: bytes) -> dict[str, Any]:
    if type(manifest_bytes) is not bytes:
        raise TypeError("manifest_bytes must be bytes")
    try:
        text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LaunchIdentityError("active-generation manifest must be UTF-8 JSON") from exc
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise LaunchIdentityError(
                    f"active-generation manifest contains duplicate JSON key: {key}"
                )
            payload[key] = value
        return payload

    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except LaunchIdentityError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LaunchIdentityError("active-generation manifest must be valid JSON") from exc
    if type(payload) is not dict or set(payload) != _MANIFEST_KEYS:
        raise LaunchIdentityError("active-generation manifest schema mismatch")
    return payload


def _safe_relative_windows_parts(value: Any) -> tuple[str, ...]:
    rel = _require_nonempty(value, field="executable_relpath")
    candidate = PureWindowsPath(rel)
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise LaunchIdentityError("executable_relpath must be relative to the install root")
    parts = candidate.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise LaunchIdentityError("executable_relpath contains an unsafe path component")
    if parts[-1].casefold() != "autosport.exe":
        raise LaunchIdentityError("active generation must resolve to Autosport.exe")
    return tuple(parts)


def resolve_active_generation(
    *,
    manifest_bytes: bytes,
    expected_manifest_sha256: str,
    install_root: str | Path,
    expected_user_scope: str,
) -> ResolvedGeneration:
    """Resolve and verify the exact active generation without mutating package state.

    The caller supplies an integrity-protected expected manifest digest. The manifest
    may select a versioned executable beneath ``install_root``; public launch surfaces
    remain bound to the separate stable launcher and must not point at this file.
    """

    expected_manifest_sha256 = _require_sha256(
        expected_manifest_sha256, field="expected_manifest_sha256"
    )
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise LaunchIdentityError("active-generation manifest digest mismatch")

    payload = _manifest_object(manifest_bytes)
    if payload["schema_version"] != 1 or type(payload["schema_version"]) is not int:
        raise LaunchIdentityError("unsupported active-generation manifest schema")
    if payload["product"] != "autosport" or type(payload["product"]) is not str:
        raise LaunchIdentityError("active-generation manifest product mismatch")

    generation = _require_generation(payload["generation"])
    version = _require_nonempty(payload["version"], field="version")
    user_scope = _require_nonempty(payload["user_scope"], field="user_scope")
    expected_user_scope = _require_nonempty(expected_user_scope, field="expected_user_scope")
    if user_scope != expected_user_scope:
        raise LaunchIdentityError("active-generation manifest user scope mismatch")

    executable_sha256 = _require_sha256(
        payload["executable_sha256"], field="executable_sha256"
    )
    parts = _safe_relative_windows_parts(payload["executable_relpath"])

    root = Path(install_root)
    if not root.is_absolute():
        raise LaunchIdentityError("install_root must be absolute")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise LaunchIdentityError("install_root does not resolve to an existing directory") from exc
    executable = root.joinpath(*parts)
    try:
        resolved_executable = executable.resolve(strict=True)
    except OSError as exc:
        raise LaunchIdentityError("active-generation executable is missing") from exc
    try:
        resolved_executable.relative_to(resolved_root)
    except ValueError as exc:
        raise LaunchIdentityError(
            "active-generation executable resolves outside the install root"
        ) from exc
    if not resolved_executable.is_file():
        raise LaunchIdentityError("active-generation executable is missing")

    digest = hashlib.sha256()
    with resolved_executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual_executable_sha256 = digest.hexdigest()
    if actual_executable_sha256 != executable_sha256:
        raise LaunchIdentityError("active-generation executable digest mismatch")
    executable = resolved_executable

    return ResolvedGeneration(
        generation=generation,
        version=version,
        executable_path=executable,
        executable_sha256=executable_sha256,
        manifest_sha256=actual_manifest_sha256,
        user_scope=user_scope,
    )


def validate_stable_launch_surface(
    *,
    shortcut_target: str,
    stable_launcher_path: str,
) -> None:
    """Require Start Menu/desktop surfaces to target the stable launcher exactly."""

    shortcut = PureWindowsPath(_require_nonempty(shortcut_target, field="shortcut_target"))
    launcher = PureWindowsPath(
        _require_nonempty(stable_launcher_path, field="stable_launcher_path")
    )
    if not shortcut.is_absolute() or not launcher.is_absolute():
        raise LaunchIdentityError("launch surface and stable launcher paths must be absolute")
    if str(shortcut).casefold() != str(launcher).casefold():
        raise LaunchIdentityError(
            "launch surface must target the stable launcher, not a versioned executable"
        )


def parse_owner_identity(payload: Mapping[str, Any]) -> LaunchOwnerIdentity:
    if not isinstance(payload, Mapping) or set(payload) != _OWNER_KEYS:
        raise LaunchIdentityError("single-instance owner schema mismatch")
    if payload["schema_version"] != 1 or type(payload["schema_version"]) is not int:
        raise LaunchIdentityError("unsupported single-instance owner schema")
    pid = payload["pid"]
    if type(pid) is not int or pid <= 0:
        raise LaunchIdentityError("pid must be a positive integer")
    process_start_nonce = _require_sha256(
        payload["process_start_nonce"], field="process_start_nonce"
    )
    user_scope = _require_nonempty(payload["user_scope"], field="user_scope")
    generation = _require_generation(payload["generation"])
    version = _require_nonempty(payload["version"], field="version")
    executable_sha256 = _require_sha256(
        payload["executable_sha256"], field="executable_sha256"
    )
    return LaunchOwnerIdentity(
        pid=pid,
        process_start_nonce=process_start_nonce,
        user_scope=user_scope,
        generation=generation,
        version=version,
        executable_sha256=executable_sha256,
    )


def owner_identity_for_process(
    *,
    pid: int,
    process_start_nonce: str,
    resolved: ResolvedGeneration,
) -> LaunchOwnerIdentity:
    return parse_owner_identity(
        {
            "schema_version": 1,
            "pid": pid,
            "process_start_nonce": process_start_nonce,
            "user_scope": resolved.user_scope,
            "generation": resolved.generation,
            "version": resolved.version,
            "executable_sha256": resolved.executable_sha256,
        }
    )


def decide_single_instance(
    *,
    existing_owner: LaunchOwnerIdentity | None,
    requested_user_scope: str,
    observe_process_start_nonce: Callable[[int], str | None],
) -> InstanceDecision:
    """Decide whether a per-user launcher may acquire or reclaim ownership.

    A live owner blocks launch solely by PID + process-start nonce + user scope.
    Generation mismatch is intentionally irrelevant: an old generation that is
    still running must block a second launch even after update/rollback changes
    the active-generation manifest. PID reuse is reclaimable because the nonce
    changes. Unknown/malformed observations fail closed.

    This is a pure decision helper, not an ownership acquisition primitive.
    The caller must serialize the read/decide/write owner transition with one
    exclusive per-user lock or equivalent compare-and-swap; otherwise two
    concurrent launchers could both observe no owner and both decide ACQUIRE.
    """

    requested_user_scope = _require_nonempty(
        requested_user_scope, field="requested_user_scope"
    )
    if existing_owner is None:
        return InstanceDecision.ACQUIRE
    if not isinstance(existing_owner, LaunchOwnerIdentity):
        raise LaunchIdentityError("existing_owner must be a LaunchOwnerIdentity")
    if existing_owner.user_scope != requested_user_scope:
        raise LaunchIdentityError("single-instance owner user scope mismatch")

    observed = observe_process_start_nonce(existing_owner.pid)
    if observed is None:
        return InstanceDecision.RECLAIM_STALE
    observed = _require_sha256(observed, field="observed_process_start_nonce")
    if observed == existing_owner.process_start_nonce:
        return InstanceDecision.BLOCK_ACTIVE
    return InstanceDecision.RECLAIM_STALE
