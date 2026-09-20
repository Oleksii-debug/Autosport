"""Durable workspace-instance identity binding for monotonic authority.

The immutable workspace instance id is not a caller-controlled namespace switch.
A workspace-local marker survives normal move/copy, while an Autosport machine-state
path alias prevents silent identity remint after local marker/workspace deletion at a
previously bound path. Paths are metadata aliases only; authority history remains
keyed by the immutable workspace instance id.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .json_integrity import strict_json_loads


WORKSPACE_BINDING_SCHEMA: Final = "autosport.monotonic_authority.workspace_binding"
PATH_BINDING_SCHEMA: Final = "autosport.monotonic_authority.workspace_path_binding"
BINDING_SCHEMA_VERSION: Final = 1
BINDING_AUTHORITY_ID: Final = "autosport.machine.monotonic.v1"

_WORKSPACE_MARKER_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "authority_id",
        "workspace_instance_id",
        "binding_sha256",
    }
)
_PATH_BINDING_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "authority_id",
        "workspace_locator",
        "workspace_locator_sha256",
        "workspace_instance_id",
        "binding_sha256",
    }
)
_SHA256_LENGTH: Final = 64


class WorkspaceBindingError(RuntimeError):
    """Base workspace identity binding error."""


class WorkspaceBindingConflictError(WorkspaceBindingError):
    """The requested identity conflicts with durable workspace binding evidence."""


class WorkspaceBindingIntegrityError(WorkspaceBindingError):
    """Durable workspace binding evidence is malformed or corrupted."""


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
        raise WorkspaceBindingIntegrityError(
            "workspace binding payload is outside canonical JSON domain"
        ) from exc


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _canonical_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkspaceBindingConflictError(
            "workspace_instance_id must be a non-empty canonical string"
        )
    if len(value) > 256 or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise WorkspaceBindingConflictError(
            "workspace_instance_id contains unsupported characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise WorkspaceBindingConflictError(
            "workspace_instance_id contains invalid Unicode"
        ) from exc
    return value


def _workspace_locator(workspace: Path) -> str:
    """Return the stable lexical path identity without following reparses/symlinks.

    The machine path receipt exists specifically to reserve a previously used
    workspace location even when the local workspace is later deleted. Resolving
    the path through the filesystem would let the same lexical location select a
    new receipt merely by recreating it as a symlink/junction to another target.
    Trust-root disjointness is still checked separately against resolved paths by
    ``resolve_monotonic_authority_root``.
    """

    if not workspace.is_absolute():
        raise WorkspaceBindingIntegrityError(
            "workspace path for identity binding must be absolute"
        )
    return os.path.normcase(os.path.normpath(str(workspace)))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise WorkspaceBindingIntegrityError(
            "cannot open workspace binding directory for durability"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise WorkspaceBindingIntegrityError(
            "cannot fsync workspace binding directory"
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
            raise WorkspaceBindingIntegrityError(
                "workspace binding directory escaped durability boundary"
            )
        current = current.parent


def _durable_exclusive_json_create(
    path: Path,
    payload: dict[str, object],
    *,
    lineage_boundary: Path,
) -> None:
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


def _read_strict_object(path: Path, expected_keys: frozenset[str]) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise WorkspaceBindingIntegrityError(
            "cannot inspect workspace binding path"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise WorkspaceBindingIntegrityError(
            "workspace binding must be a single-link regular file"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkspaceBindingIntegrityError("cannot read workspace binding") from exc
    try:
        raw = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise WorkspaceBindingIntegrityError(
            "invalid strict JSON in workspace binding"
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise WorkspaceBindingIntegrityError(
            "workspace binding must be a JSON object"
        )
    if frozenset(raw) != expected_keys:
        raise WorkspaceBindingIntegrityError(
            "workspace binding keys must match schema exactly"
        )
    return raw


def _verify_binding_hash(raw: dict[str, object]) -> None:
    digest = raw["binding_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != _SHA256_LENGTH
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise WorkspaceBindingIntegrityError("invalid workspace binding digest")
    unhashed = dict(raw)
    unhashed.pop("binding_sha256")
    if _payload_hash(unhashed) != digest:
        raise WorkspaceBindingIntegrityError("workspace binding hash mismatch")


@dataclass(frozen=True, slots=True)
class WorkspaceIdentityBinding:
    workspace: Path
    authority_root: Path
    workspace_instance_id: str
    workspace_marker_path: Path
    path_binding_path: Path
    workspace_locator: str
    workspace_locator_sha256: str

    @classmethod
    def resolve(
        cls,
        *,
        workspace: Path,
        authority_root: Path,
        requested_workspace_instance_id: str | None,
    ) -> WorkspaceIdentityBinding:
        requested = (
            None
            if requested_workspace_instance_id is None
            else _canonical_instance_id(requested_workspace_instance_id)
        )
        locator = _workspace_locator(workspace)
        locator_sha = hashlib.sha256(locator.encode("utf-8")).hexdigest()
        workspace_marker = workspace / ".autosport" / "monotonic-workspace-binding.json"
        path_binding = (
            authority_root
            / "workspace-bindings"
            / locator_sha[:2]
            / f"{locator_sha}.json"
        )

        workspace_id = cls._read_workspace_marker_id(workspace_marker)
        path_id = cls._read_path_binding_id(
            path_binding,
            expected_locator=locator,
            expected_locator_sha=locator_sha,
        )
        durable_ids = {value for value in (workspace_id, path_id) if value is not None}
        if len(durable_ids) > 1:
            raise WorkspaceBindingConflictError(
                "workspace marker and machine path binding disagree on instance identity"
            )
        durable_id = next(iter(durable_ids), None)
        if requested is not None and durable_id is not None and requested != durable_id:
            raise WorkspaceBindingConflictError(
                "requested workspace_instance_id conflicts with durable workspace binding"
            )
        resolved_id = durable_id or requested or uuid.uuid4().hex
        return cls(
            workspace=workspace,
            authority_root=authority_root,
            workspace_instance_id=resolved_id,
            workspace_marker_path=workspace_marker,
            path_binding_path=path_binding,
            workspace_locator=locator,
            workspace_locator_sha256=locator_sha,
        )

    @staticmethod
    def _read_workspace_marker_id(path: Path) -> str | None:
        if not path.exists():
            return None
        raw = _read_strict_object(path, _WORKSPACE_MARKER_KEYS)
        if (
            raw["schema"] != WORKSPACE_BINDING_SCHEMA
            or raw["schema_version"] != BINDING_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != BINDING_AUTHORITY_ID
        ):
            raise WorkspaceBindingIntegrityError(
                "unsupported workspace identity binding schema"
            )
        _verify_binding_hash(raw)
        return _canonical_instance_id(raw["workspace_instance_id"])

    @staticmethod
    def _read_path_binding_id(
        path: Path,
        *,
        expected_locator: str,
        expected_locator_sha: str,
    ) -> str | None:
        if not path.exists():
            return None
        raw = _read_strict_object(path, _PATH_BINDING_KEYS)
        if (
            raw["schema"] != PATH_BINDING_SCHEMA
            or raw["schema_version"] != BINDING_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != BINDING_AUTHORITY_ID
            or raw["workspace_locator"] != expected_locator
            or raw["workspace_locator_sha256"] != expected_locator_sha
        ):
            raise WorkspaceBindingIntegrityError(
                "workspace path binding identity/schema mismatch"
            )
        _verify_binding_hash(raw)
        return _canonical_instance_id(raw["workspace_instance_id"])

    def validate_existing(self, *, register_moved_or_copied_path: bool = True) -> tuple[bool, bool]:
        workspace_id = self._read_workspace_marker_id(self.workspace_marker_path)
        path_id = self._read_path_binding_id(
            self.path_binding_path,
            expected_locator=self.workspace_locator,
            expected_locator_sha=self.workspace_locator_sha256,
        )
        if workspace_id is not None and workspace_id != self.workspace_instance_id:
            raise WorkspaceBindingConflictError(
                "workspace marker changed immutable workspace instance identity"
            )
        if path_id is not None and path_id != self.workspace_instance_id:
            raise WorkspaceBindingConflictError(
                "machine path binding reserves this workspace path for another identity"
            )
        if (
            register_moved_or_copied_path
            and workspace_id == self.workspace_instance_id
            and path_id is None
        ):
            self._ensure_path_binding()
            path_id = self.workspace_instance_id
        return workspace_id is not None, path_id is not None

    def ensure_bound(self) -> None:
        """Durably bind this pristine workspace path and local marker to one identity."""
        self._ensure_path_binding()
        self._ensure_workspace_marker()

    def _workspace_payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": WORKSPACE_BINDING_SCHEMA,
            "schema_version": BINDING_SCHEMA_VERSION,
            "authority_id": BINDING_AUTHORITY_ID,
            "workspace_instance_id": self.workspace_instance_id,
        }
        return {**unhashed, "binding_sha256": _payload_hash(unhashed)}

    def _path_payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": PATH_BINDING_SCHEMA,
            "schema_version": BINDING_SCHEMA_VERSION,
            "authority_id": BINDING_AUTHORITY_ID,
            "workspace_locator": self.workspace_locator,
            "workspace_locator_sha256": self.workspace_locator_sha256,
            "workspace_instance_id": self.workspace_instance_id,
        }
        return {**unhashed, "binding_sha256": _payload_hash(unhashed)}

    def _ensure_workspace_marker(self) -> None:
        existing = self._read_workspace_marker_id(self.workspace_marker_path)
        if existing is not None:
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace marker conflicts with immutable instance identity"
                )
            return
        boundary = self.workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            _durable_exclusive_json_create(
                self.workspace_marker_path,
                self._workspace_payload(),
                lineage_boundary=boundary,
            )
        except FileExistsError:
            existing = self._read_workspace_marker_id(self.workspace_marker_path)
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace marker concurrently bound to another identity"
                )

    def _ensure_path_binding(self) -> None:
        existing = self._read_path_binding_id(
            self.path_binding_path,
            expected_locator=self.workspace_locator,
            expected_locator_sha=self.workspace_locator_sha256,
        )
        if existing is not None:
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace path is already bound to another instance identity"
                )
            return
        self.authority_root.mkdir(parents=True, exist_ok=True)
        try:
            _durable_exclusive_json_create(
                self.path_binding_path,
                self._path_payload(),
                lineage_boundary=self.authority_root,
            )
        except FileExistsError:
            existing = self._read_path_binding_id(
                self.path_binding_path,
                expected_locator=self.workspace_locator,
                expected_locator_sha=self.workspace_locator_sha256,
            )
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace path was concurrently bound to another identity"
                )
