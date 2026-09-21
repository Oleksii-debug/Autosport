"""Root-independent selection binding for the monotonic workspace authority.

The selected monotonic authority root is itself security-relevant state.  If a
workspace with positive history can simply point at a fresh authority root, the
new root looks pristine and anti-rollback history can be bypassed.

This module stores only a small path/identity/root receipt in the canonical
Autosport application-state area.  Critically, that receipt location ignores
AUTOSPORT_MONOTONIC_AUTHORITY_ROOT, so changing the selected authority root does
not change the place used to remember which root already owns a workspace path.

The receipt does not grant domain authority and is not created by read-only
pristine recovery.  It is established on the first real authority transition
(or when upgrading already-valid non-empty history).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .json_integrity import strict_json_loads


ROOT_SELECTION_SCHEMA: Final = "autosport.monotonic_authority.root_selection"
ROOT_SELECTION_SCHEMA_VERSION: Final = 1
ROOT_SELECTION_AUTHORITY_ID: Final = "autosport.machine.monotonic.v1"

_ROOT_SELECTION_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "authority_id",
        "workspace_locator",
        "workspace_locator_sha256",
        "workspace_instance_id",
        "authority_root_locator",
        "authority_root_locator_sha256",
        "authority_root_resolved",
        "authority_root_resolved_sha256",
        "binding_sha256",
    }
)
_SHA256_LENGTH: Final = 64


class AuthorityRootSelectionError(RuntimeError):
    """Base error for authority-root selection binding."""


class AuthorityRootSelectionConfigurationError(AuthorityRootSelectionError):
    """The stable root-selection store cannot be configured safely."""


class AuthorityRootSelectionConflictError(AuthorityRootSelectionError):
    """The selected authority root conflicts with an existing durable receipt."""


class AuthorityRootSelectionIntegrityError(AuthorityRootSelectionError):
    """The durable root-selection receipt is malformed or corrupted."""


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuthorityRootSelectionIntegrityError(
            "authority-root selection payload is outside canonical JSON domain"
        ) from exc


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _absolute_path(name: str, value: str | Path) -> Path:
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise AuthorityRootSelectionConfigurationError(
            f"{name} home expansion could not be resolved"
        ) from exc
    if not path.is_absolute():
        raise AuthorityRootSelectionConfigurationError(
            f"{name} must be an absolute path"
        )
    return path


def _lexical_locator(path: Path) -> str:
    if not path.is_absolute():
        raise AuthorityRootSelectionConfigurationError(
            "root-selection path must be absolute"
        )
    return os.path.normcase(os.path.normpath(str(path)))


def _resolved_locator(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise AuthorityRootSelectionConfigurationError(
            "cannot resolve authority-root selection path"
        ) from exc
    return os.path.normcase(os.path.normpath(str(resolved)))


def stable_root_selection_store() -> Path:
    """Return the non-overridable application-state store for root receipts.

    AUTOSPORT_MONOTONIC_AUTHORITY_ROOT is intentionally not consulted here.
    Otherwise the same environment switch that selects a fresh authority root
    would also select a fresh root-selection witness.
    """

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = _absolute_path("LOCALAPPDATA", local_app_data)
        return (
            base
            / "Autosport"
            / "application-state"
            / "monotonic-root-selection-v1"
        )

    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        base = _absolute_path("XDG_STATE_HOME", xdg_state_home)
        return base / "autosport" / "monotonic-root-selection-v1"

    try:
        home = Path.home()
    except RuntimeError as exc:
        raise AuthorityRootSelectionConfigurationError(
            "home directory could not be resolved for authority-root selection"
        ) from exc
    if not home.is_absolute():
        raise AuthorityRootSelectionConfigurationError(
            "home directory must be absolute for authority-root selection"
        )
    return home / ".local" / "state" / "autosport" / "monotonic-root-selection-v1"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AuthorityRootSelectionIntegrityError(
            "cannot open authority-root binding directory for durability"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise AuthorityRootSelectionIntegrityError(
            "cannot fsync authority-root binding directory"
        ) from exc
    finally:
        os.close(descriptor)


def _sync_existing_lineage(leaf: Path, boundary: Path) -> None:
    if os.name == "nt":
        return
    current = leaf
    while True:
        _fsync_directory(current)
        if current == boundary:
            return
        if not current.is_relative_to(boundary):
            raise AuthorityRootSelectionIntegrityError(
                "authority-root binding escaped stable application-state store"
            )
        current = current.parent


def _durable_exclusive_json_create(
    path: Path,
    payload: dict[str, object],
    *,
    lineage_boundary: Path,
) -> None:
    lineage_boundary.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sync_existing_lineage(path.parent, lineage_boundary)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
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
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except BaseException:
                pass
        raise


def _read_strict_object(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AuthorityRootSelectionIntegrityError(
            "cannot inspect authority-root binding path"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding must be a single-link regular file"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuthorityRootSelectionIntegrityError(
            "cannot read authority-root binding"
        ) from exc
    try:
        raw = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise AuthorityRootSelectionIntegrityError(
            "invalid strict JSON in authority-root binding"
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding must be a JSON object"
        )
    if frozenset(raw) != _ROOT_SELECTION_KEYS:
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding keys must match schema exactly"
        )
    return raw


def _verify_hash(raw: dict[str, object]) -> None:
    digest = raw["binding_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != _SHA256_LENGTH
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise AuthorityRootSelectionIntegrityError(
            "invalid authority-root binding digest"
        )
    unhashed = dict(raw)
    unhashed.pop("binding_sha256")
    if _payload_hash(unhashed) != digest:
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding hash mismatch"
        )


@dataclass(frozen=True, slots=True)
class AuthorityRootSelectionBinding:
    workspace: Path
    workspace_instance_id: str
    authority_root: Path
    store_root: Path
    workspace_locator: str
    workspace_locator_sha256: str
    authority_root_locator: str
    authority_root_locator_sha256: str
    authority_root_resolved: str
    authority_root_resolved_sha256: str
    binding_path: Path

    @classmethod
    def resolve(
        cls,
        *,
        workspace: Path,
        workspace_instance_id: str,
        authority_root: Path,
    ) -> "AuthorityRootSelectionBinding":
        if not isinstance(workspace_instance_id, str) or not workspace_instance_id:
            raise AuthorityRootSelectionConfigurationError(
                "workspace_instance_id is required for authority-root selection"
            )

        store_root = stable_root_selection_store()
        workspace_locator = _lexical_locator(workspace)
        workspace_locator_sha = hashlib.sha256(
            workspace_locator.encode("utf-8")
        ).hexdigest()
        root_locator = _lexical_locator(authority_root)
        root_locator_sha = hashlib.sha256(root_locator.encode("utf-8")).hexdigest()
        root_resolved = _resolved_locator(authority_root)
        root_resolved_sha = hashlib.sha256(
            root_resolved.encode("utf-8")
        ).hexdigest()

        resolved_workspace = Path(_resolved_locator(workspace))
        resolved_store = Path(_resolved_locator(store_root))
        if (
            resolved_store == resolved_workspace
            or resolved_store.is_relative_to(resolved_workspace)
            or resolved_workspace.is_relative_to(resolved_store)
        ):
            raise AuthorityRootSelectionConfigurationError(
                "authority-root selection store and protected workspace must be disjoint trees"
            )

        binding_path = (
            store_root
            / "workspace-path-bindings"
            / workspace_locator_sha[:2]
            / f"{workspace_locator_sha}.json"
        )
        binding = cls(
            workspace=workspace,
            workspace_instance_id=workspace_instance_id,
            authority_root=authority_root,
            store_root=store_root,
            workspace_locator=workspace_locator,
            workspace_locator_sha256=workspace_locator_sha,
            authority_root_locator=root_locator,
            authority_root_locator_sha256=root_locator_sha,
            authority_root_resolved=root_resolved,
            authority_root_resolved_sha256=root_resolved_sha,
            binding_path=binding_path,
        )
        binding.validate_existing()
        return binding

    def _payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": ROOT_SELECTION_SCHEMA,
            "schema_version": ROOT_SELECTION_SCHEMA_VERSION,
            "authority_id": ROOT_SELECTION_AUTHORITY_ID,
            "workspace_locator": self.workspace_locator,
            "workspace_locator_sha256": self.workspace_locator_sha256,
            "workspace_instance_id": self.workspace_instance_id,
            "authority_root_locator": self.authority_root_locator,
            "authority_root_locator_sha256": self.authority_root_locator_sha256,
            "authority_root_resolved": self.authority_root_resolved,
            "authority_root_resolved_sha256": self.authority_root_resolved_sha256,
        }
        return {**unhashed, "binding_sha256": _payload_hash(unhashed)}

    def validate_existing(self) -> bool:
        """Validate an existing receipt without creating any pristine state."""

        if not self.binding_path.exists():
            return False
        raw = _read_strict_object(self.binding_path)
        if (
            raw["schema"] != ROOT_SELECTION_SCHEMA
            or raw["schema_version"] != ROOT_SELECTION_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != ROOT_SELECTION_AUTHORITY_ID
            or raw["workspace_locator"] != self.workspace_locator
            or raw["workspace_locator_sha256"] != self.workspace_locator_sha256
        ):
            raise AuthorityRootSelectionIntegrityError(
                "authority-root binding identity/schema mismatch"
            )
        _verify_hash(raw)

        if raw["workspace_instance_id"] != self.workspace_instance_id:
            raise AuthorityRootSelectionConflictError(
                "workspace path is bound to a different immutable workspace identity"
            )
        if (
            raw["authority_root_locator"] != self.authority_root_locator
            or raw["authority_root_locator_sha256"]
            != self.authority_root_locator_sha256
            or raw["authority_root_resolved"] != self.authority_root_resolved
            or raw["authority_root_resolved_sha256"]
            != self.authority_root_resolved_sha256
        ):
            raise AuthorityRootSelectionConflictError(
                "workspace path is already bound to a different monotonic authority root"
            )
        return True

    def ensure_bound(self) -> None:
        """Durably reserve this workspace path for the selected authority root."""

        if self.validate_existing():
            return
        try:
            _durable_exclusive_json_create(
                self.binding_path,
                self._payload(),
                lineage_boundary=self.store_root,
            )
        except FileExistsError:
            self.validate_existing()
        except AuthorityRootSelectionError:
            raise
        except OSError as exc:
            raise AuthorityRootSelectionIntegrityError(
                "cannot durably persist authority-root selection binding"
            ) from exc
