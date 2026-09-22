"""Durable workspace-instance identity binding for monotonic authority.

The immutable workspace instance id is not a caller-controlled namespace switch.
Workspace-local receipts support normal move/copy, while a stable Autosport
machine-state registry is consulted before any configurable monotonic-authority root.
That pre-selection registry prevents a workspace rollback/deletion plus root switch
from hiding surviving authority history behind a fresh physical root.
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
AUTHORITY_ROOT_BINDING_SCHEMA: Final = "autosport.monotonic_authority.authority_root_binding"
MACHINE_ROOT_BINDING_SCHEMA: Final = "autosport.monotonic_authority.machine_root_binding"
BINDING_SCHEMA_VERSION: Final = 1
BINDING_AUTHORITY_ID: Final = "autosport.machine.monotonic.v1"

_WORKSPACE_MARKER_KEYS: Final = frozenset(
    {"schema", "schema_version", "authority_id", "workspace_instance_id", "binding_sha256"}
)
_PATH_BINDING_KEYS: Final = frozenset(
    {
        "schema", "schema_version", "authority_id", "workspace_locator",
        "workspace_locator_sha256", "workspace_instance_id", "binding_sha256",
    }
)
_AUTHORITY_ROOT_BINDING_KEYS: Final = frozenset(
    {
        "schema", "schema_version", "authority_id", "workspace_instance_id",
        "authority_root_locator", "authority_root_locator_sha256", "binding_sha256",
    }
)
_MACHINE_ROOT_BINDING_KEYS: Final = frozenset(
    {
        "schema", "schema_version", "authority_id", "workspace_locator",
        "workspace_locator_sha256", "workspace_instance_id", "authority_root_locator",
        "authority_root_locator_sha256", "binding_sha256",
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
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
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
    """Return stable lexical workspace identity without following links."""
    if not workspace.is_absolute():
        raise WorkspaceBindingIntegrityError(
            "workspace path for identity binding must be absolute"
        )
    return os.path.normcase(os.path.normpath(str(workspace)))


def _authority_root_locator(authority_root: Path) -> str:
    """Return the resolved physical machine-authority root identity."""
    if not authority_root.is_absolute():
        raise WorkspaceBindingIntegrityError(
            "monotonic authority root for identity binding must be absolute"
        )
    try:
        resolved = authority_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceBindingIntegrityError(
            "cannot resolve monotonic authority root for identity binding"
        ) from exc
    return os.path.normcase(os.path.normpath(str(resolved)))


def _machine_identity_key(workspace_instance_id: str, authority_root_locator: str) -> str:
    """Scope moved-workspace identity lookup to the physical authority root.

    Workspace instance ids are immutable inside one authority-root lineage, but
    explicit ids are not globally unique across independent machine-state roots.
    Same-path root switching is fenced by the stable path anchor, while a moved
    workspace can use this root-scoped identity anchor without aliasing an unrelated
    workspace that intentionally reuses the same explicit instance id elsewhere.
    """
    material = "\0".join((workspace_instance_id, authority_root_locator)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _absolute_machine_state_base(name: str, value: str | Path) -> Path:
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise WorkspaceBindingIntegrityError(
            f"{name} home expansion could not be resolved"
        ) from exc
    if not path.is_absolute():
        raise WorkspaceBindingIntegrityError(f"{name} must be an absolute path")
    return path


def _native_machine_state_base() -> Path:
    """Resolve account-owned application state without process path configuration.

    The pre-selection registry is itself a trust anchor.  Using LOCALAPPDATA,
    XDG_STATE_HOME, HOME, or another process environment value here would let a
    supported restart select a fresh receipt namespace before surviving machine
    authority can be consulted.

    Tests may monkeypatch this private helper.  Same-interpreter monkeypatching is a
    trusted-test/extension capability and is not a supported runtime configuration
    surface.
    """

    if os.name == "nt":
        # FOLDERID_LocalAppData resolved by the Windows Known Folders API is
        # account-owned OS state, unlike the process LOCALAPPDATA environment.
        try:
            import ctypes

            class _Guid(ctypes.Structure):
                _fields_ = (
                    ("data1", ctypes.c_ulong),
                    ("data2", ctypes.c_ushort),
                    ("data3", ctypes.c_ushort),
                    ("data4", ctypes.c_ubyte * 8),
                )

            folder_id_local_app_data = _Guid(
                0xF1B32785,
                0x6FBA,
                0x4FCF,
                (ctypes.c_ubyte * 8)(
                    0x9D,
                    0x55,
                    0x7B,
                    0x8E,
                    0x7F,
                    0x15,
                    0x70,
                    0x91,
                ),
            )
            ole32 = ctypes.windll.ole32
            co_initialize = ole32.CoInitializeEx
            co_initialize.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
            co_initialize.restype = ctypes.c_long
            co_uninitialize = ole32.CoUninitialize
            co_uninitialize.argtypes = ()
            co_uninitialize.restype = None
            free_task_memory = ole32.CoTaskMemFree
            free_task_memory.argtypes = (ctypes.c_void_p,)
            free_task_memory.restype = None

            # Known Folders requires COM on the calling thread. S_OK and S_FALSE
            # both acquire a reference that must be balanced. RPC_E_CHANGED_MODE
            # means COM is already initialized with another apartment model, so
            # the thread is usable but this call must not uninitialize it.
            com_status = co_initialize(None, 0x2)  # COINIT_APARTMENTTHREADED
            com_status_u32 = com_status & 0xFFFFFFFF
            com_owned = com_status in (0, 1)
            if not com_owned and com_status_u32 != 0x80010106:
                raise WorkspaceBindingIntegrityError(
                    "COM initialization failed for Windows Known Folder lookup"
                )

            raw_path = ctypes.c_void_p()
            try:
                get_known_folder_path = ctypes.windll.shell32.SHGetKnownFolderPath
                get_known_folder_path.argtypes = (
                    ctypes.POINTER(_Guid),
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p),
                )
                get_known_folder_path.restype = ctypes.c_long
                status = get_known_folder_path(
                    ctypes.byref(folder_id_local_app_data),
                    0,
                    None,
                    ctypes.byref(raw_path),
                )
                if status != 0 or raw_path.value is None:
                    raise WorkspaceBindingIntegrityError(
                        "Windows Local AppData Known Folder lookup failed"
                    )
                native_path = ctypes.wstring_at(raw_path.value)
            finally:
                if raw_path.value is not None:
                    free_task_memory(raw_path)
                if com_owned:
                    co_uninitialize()
        except WorkspaceBindingIntegrityError:
            raise
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise WorkspaceBindingIntegrityError(
                "Windows Local AppData could not be resolved through Known Folders"
            ) from exc
        if not native_path:
            raise WorkspaceBindingIntegrityError(
                "Windows Local AppData Known Folder returned an empty path"
            )
        return _absolute_machine_state_base(
            "native Windows Local AppData",
            native_path,
        )

    try:
        import pwd

        account_home = pwd.getpwuid(os.getuid()).pw_dir
    except (AttributeError, ImportError, KeyError, OSError) as exc:
        raise WorkspaceBindingIntegrityError(
            "OS account home could not be resolved for monotonic root binding"
        ) from exc
    return _absolute_machine_state_base("OS account home", account_home)


def _machine_binding_root(*, workspace: Path, authority_root: Path) -> Path:
    """Return a non-configurable registry root consulted before authority-root selection.

    Neither AUTOSPORT_MONOTONIC_AUTHORITY_ROOT nor process path-environment
    overrides influence this locator.  Otherwise one restart could select both a
    fresh journal and a fresh pre-selection receipt namespace.
    """
    base = _native_machine_state_base()
    if os.name == "nt":
        root = base / "Autosport" / "application-state" / "monotonic-root-bindings-v1"
    else:
        root = base / ".local" / "state" / "autosport" / "monotonic-root-bindings-v1"

    try:
        resolved_root = root.resolve(strict=False)
        resolved_workspace = workspace.resolve(strict=False)
        resolved_authority = authority_root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceBindingIntegrityError(
            "cannot resolve monotonic root-binding trust boundaries"
        ) from exc
    for protected, label in (
        (resolved_workspace, "workspace"),
        (resolved_authority, "configured authority root"),
    ):
        if (
            resolved_root == protected
            or resolved_root.is_relative_to(protected)
            or protected.is_relative_to(resolved_root)
        ):
            raise WorkspaceBindingIntegrityError(
                f"stable monotonic root-binding registry must be disjoint from {label}"
            )
    if root.exists() and not root.is_dir():
        raise WorkspaceBindingIntegrityError(
            "stable monotonic root-binding registry must be a directory"
        )
    return root


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
    path: Path, payload: dict[str, object], *, lineage_boundary: Path
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
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
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
    authority_root_binding_path: Path
    machine_binding_root: Path
    machine_path_binding_path: Path
    machine_identity_binding_path: Path
    workspace_locator: str
    workspace_locator_sha256: str
    authority_root_locator: str
    authority_root_locator_sha256: str

    @classmethod
    def resolve(
        cls,
        *,
        workspace: Path,
        authority_root: Path,
        requested_workspace_instance_id: str | None,
    ) -> "WorkspaceIdentityBinding":
        requested = (
            None
            if requested_workspace_instance_id is None
            else _canonical_instance_id(requested_workspace_instance_id)
        )
        locator = _workspace_locator(workspace)
        locator_sha = hashlib.sha256(locator.encode("utf-8")).hexdigest()
        root_locator = _authority_root_locator(authority_root)
        root_locator_sha = hashlib.sha256(root_locator.encode("utf-8")).hexdigest()
        machine_root = _machine_binding_root(workspace=workspace, authority_root=authority_root)
        workspace_marker = workspace / ".autosport" / "monotonic-workspace-binding.json"
        authority_root_binding = workspace / ".autosport" / "monotonic-authority-root-binding.json"
        path_binding = authority_root / "workspace-bindings" / locator_sha[:2] / f"{locator_sha}.json"
        machine_path_binding = (
            machine_root / "workspace-path-bindings" / locator_sha[:2] / f"{locator_sha}.json"
        )

        workspace_id = cls._read_workspace_marker_id(workspace_marker)
        identity_probe = workspace_id or requested
        machine_path_id = cls._read_machine_binding_id(
            machine_path_binding,
            expected_workspace_locator=locator,
            expected_workspace_locator_sha=locator_sha,
            expected_workspace_instance_id=None,
            expected_authority_root_locator=root_locator,
            expected_authority_root_locator_sha=root_locator_sha,
        )
        if identity_probe is None and machine_path_id is not None:
            identity_probe = machine_path_id
        identity_probe_sha = (
            None
            if identity_probe is None
            else _machine_identity_key(identity_probe, root_locator)
        )
        machine_identity_binding = (
            machine_root
            / "workspace-identity-bindings"
            / identity_probe_sha[:2]
            / f"{identity_probe_sha}.json"
            if identity_probe_sha is not None
            else machine_root / "workspace-identity-bindings" / "unbound"
        )
        machine_identity_id = (
            None
            if identity_probe is None
            else cls._read_machine_binding_id(
                machine_identity_binding,
                expected_workspace_locator=None,
                expected_workspace_locator_sha=None,
                expected_workspace_instance_id=identity_probe,
                expected_authority_root_locator=root_locator,
                expected_authority_root_locator_sha=root_locator_sha,
            )
        )
        path_id = cls._read_path_binding_id(
            path_binding, expected_locator=locator, expected_locator_sha=locator_sha
        )
        root_id = cls._read_authority_root_binding_id(
            authority_root_binding,
            expected_locator=root_locator,
            expected_locator_sha=root_locator_sha,
        )
        if workspace_id is not None and path_id is None and root_id is None and machine_identity_id is None:
            raise WorkspaceBindingConflictError(
                "bound workspace is missing authority-root proof for the selected monotonic authority root"
            )
        durable_ids = {
            value
            for value in (
                workspace_id,
                path_id,
                root_id,
                machine_path_id,
                machine_identity_id,
            )
            if value is not None
        }
        if len(durable_ids) > 1:
            raise WorkspaceBindingConflictError(
                "workspace and machine bindings disagree on instance identity"
            )
        durable_id = next(iter(durable_ids), None)
        if requested is not None and durable_id is not None and requested != durable_id:
            raise WorkspaceBindingConflictError(
                "requested workspace_instance_id conflicts with durable workspace binding"
            )
        resolved_id = durable_id or requested or uuid.uuid4().hex
        identity_sha = _machine_identity_key(resolved_id, root_locator)
        machine_identity_binding = (
            machine_root / "workspace-identity-bindings" / identity_sha[:2] / f"{identity_sha}.json"
        )
        return cls(
            workspace=workspace,
            authority_root=authority_root,
            workspace_instance_id=resolved_id,
            workspace_marker_path=workspace_marker,
            path_binding_path=path_binding,
            authority_root_binding_path=authority_root_binding,
            machine_binding_root=machine_root,
            machine_path_binding_path=machine_path_binding,
            machine_identity_binding_path=machine_identity_binding,
            workspace_locator=locator,
            workspace_locator_sha256=locator_sha,
            authority_root_locator=root_locator,
            authority_root_locator_sha256=root_locator_sha,
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
        path: Path, *, expected_locator: str, expected_locator_sha: str
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

    @staticmethod
    def _read_authority_root_binding_id(
        path: Path, *, expected_locator: str, expected_locator_sha: str
    ) -> str | None:
        if not path.exists():
            return None
        raw = _read_strict_object(path, _AUTHORITY_ROOT_BINDING_KEYS)
        if (
            raw["schema"] != AUTHORITY_ROOT_BINDING_SCHEMA
            or raw["schema_version"] != BINDING_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != BINDING_AUTHORITY_ID
            or type(raw["authority_root_locator"]) is not str
            or type(raw["authority_root_locator_sha256"]) is not str
        ):
            raise WorkspaceBindingIntegrityError(
                "workspace authority-root binding identity/schema mismatch"
            )
        _verify_binding_hash(raw)
        locator = raw["authority_root_locator"]
        locator_sha = raw["authority_root_locator_sha256"]
        if hashlib.sha256(locator.encode("utf-8")).hexdigest() != locator_sha:
            raise WorkspaceBindingIntegrityError(
                "workspace authority-root locator digest mismatch"
            )
        if locator != expected_locator or locator_sha != expected_locator_sha:
            raise WorkspaceBindingConflictError(
                "configured monotonic authority root conflicts with immutable workspace authority-root binding"
            )
        return _canonical_instance_id(raw["workspace_instance_id"])

    @staticmethod
    def _read_machine_binding_id(
        path: Path,
        *,
        expected_workspace_locator: str | None,
        expected_workspace_locator_sha: str | None,
        expected_workspace_instance_id: str | None,
        expected_authority_root_locator: str,
        expected_authority_root_locator_sha: str,
    ) -> str | None:
        if not path.exists():
            return None
        raw = _read_strict_object(path, _MACHINE_ROOT_BINDING_KEYS)
        if (
            raw["schema"] != MACHINE_ROOT_BINDING_SCHEMA
            or raw["schema_version"] != BINDING_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != BINDING_AUTHORITY_ID
            or type(raw["workspace_locator"]) is not str
            or type(raw["workspace_locator_sha256"]) is not str
            or type(raw["authority_root_locator"]) is not str
            or type(raw["authority_root_locator_sha256"]) is not str
        ):
            raise WorkspaceBindingIntegrityError(
                "stable machine root binding identity/schema mismatch"
            )
        _verify_binding_hash(raw)
        locator = raw["workspace_locator"]
        locator_sha = raw["workspace_locator_sha256"]
        root_locator = raw["authority_root_locator"]
        root_sha = raw["authority_root_locator_sha256"]
        if hashlib.sha256(locator.encode("utf-8")).hexdigest() != locator_sha:
            raise WorkspaceBindingIntegrityError(
                "stable machine workspace locator digest mismatch"
            )
        if hashlib.sha256(root_locator.encode("utf-8")).hexdigest() != root_sha:
            raise WorkspaceBindingIntegrityError(
                "stable machine authority-root locator digest mismatch"
            )
        instance_id = _canonical_instance_id(raw["workspace_instance_id"])
        if (
            expected_workspace_locator is not None
            and (locator != expected_workspace_locator or locator_sha != expected_workspace_locator_sha)
        ):
            raise WorkspaceBindingIntegrityError(
                "stable machine path binding locator mismatch"
            )
        if expected_workspace_instance_id is not None and instance_id != expected_workspace_instance_id:
            raise WorkspaceBindingConflictError(
                "stable machine identity binding belongs to another workspace instance"
            )
        if (
            root_locator != expected_authority_root_locator
            or root_sha != expected_authority_root_locator_sha
        ):
            raise WorkspaceBindingConflictError(
                "configured monotonic authority root conflicts with stable machine root binding"
            )
        return instance_id

    def validate_existing(self, *, register_moved_or_copied_path: bool = True) -> tuple[bool, bool]:
        workspace_id = self._read_workspace_marker_id(self.workspace_marker_path)
        path_id = self._read_path_binding_id(
            self.path_binding_path,
            expected_locator=self.workspace_locator,
            expected_locator_sha=self.workspace_locator_sha256,
        )
        root_id = self._read_authority_root_binding_id(
            self.authority_root_binding_path,
            expected_locator=self.authority_root_locator,
            expected_locator_sha=self.authority_root_locator_sha256,
        )
        machine_path_id = self._read_machine_binding_id(
            self.machine_path_binding_path,
            expected_workspace_locator=self.workspace_locator,
            expected_workspace_locator_sha=self.workspace_locator_sha256,
            expected_workspace_instance_id=None,
            expected_authority_root_locator=self.authority_root_locator,
            expected_authority_root_locator_sha=self.authority_root_locator_sha256,
        )
        machine_identity_id = self._read_machine_binding_id(
            self.machine_identity_binding_path,
            expected_workspace_locator=None,
            expected_workspace_locator_sha=None,
            expected_workspace_instance_id=self.workspace_instance_id,
            expected_authority_root_locator=self.authority_root_locator,
            expected_authority_root_locator_sha=self.authority_root_locator_sha256,
        )
        for value, message in (
            (workspace_id, "workspace marker changed immutable workspace instance identity"),
            (path_id, "machine path binding reserves this workspace path for another identity"),
            (root_id, "workspace authority-root binding belongs to another instance identity"),
            (machine_path_id, "stable machine path binding belongs to another instance identity"),
            (machine_identity_id, "stable machine identity binding belongs to another instance identity"),
        ):
            if value is not None and value != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(message)

        if machine_path_id is None and machine_identity_id is None and path_id == self.workspace_instance_id:
            self._ensure_machine_path_binding()
            self._ensure_machine_identity_binding()
            machine_path_id = machine_identity_id = self.workspace_instance_id
        elif machine_identity_id == self.workspace_instance_id and machine_path_id is None:
            if workspace_id != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "unregistered workspace path lacks immutable identity proof"
                )
            self._ensure_machine_path_binding()
            machine_path_id = self.workspace_instance_id
        elif machine_path_id == self.workspace_instance_id and machine_identity_id is None:
            self._ensure_machine_identity_binding()
            machine_identity_id = self.workspace_instance_id

        if root_id is None:
            if (
                workspace_id == self.workspace_instance_id
                and path_id == self.workspace_instance_id
                and machine_path_id == self.workspace_instance_id
                and machine_identity_id == self.workspace_instance_id
            ):
                self._ensure_authority_root_binding()
                root_id = self.workspace_instance_id
            elif workspace_id == self.workspace_instance_id and path_id is None:
                raise WorkspaceBindingConflictError(
                    "bound workspace cannot attach to an unproven monotonic authority root"
                )
        if (
            register_moved_or_copied_path
            and workspace_id == self.workspace_instance_id
            and root_id == self.workspace_instance_id
            and machine_identity_id == self.workspace_instance_id
            and path_id is None
        ):
            self._ensure_machine_path_binding()
            self._ensure_path_binding()
            path_id = self.workspace_instance_id
        return workspace_id is not None, path_id is not None

    def ensure_bound(self) -> None:
        """Durably bind identity/root before publishing workspace-local receipts."""
        self._ensure_machine_path_binding()
        self._ensure_machine_identity_binding()
        self._ensure_path_binding()
        self._ensure_workspace_marker()
        self._ensure_authority_root_binding()

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

    def _authority_root_payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": AUTHORITY_ROOT_BINDING_SCHEMA,
            "schema_version": BINDING_SCHEMA_VERSION,
            "authority_id": BINDING_AUTHORITY_ID,
            "workspace_instance_id": self.workspace_instance_id,
            "authority_root_locator": self.authority_root_locator,
            "authority_root_locator_sha256": self.authority_root_locator_sha256,
        }
        return {**unhashed, "binding_sha256": _payload_hash(unhashed)}

    def _machine_payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": MACHINE_ROOT_BINDING_SCHEMA,
            "schema_version": BINDING_SCHEMA_VERSION,
            "authority_id": BINDING_AUTHORITY_ID,
            "workspace_locator": self.workspace_locator,
            "workspace_locator_sha256": self.workspace_locator_sha256,
            "workspace_instance_id": self.workspace_instance_id,
            "authority_root_locator": self.authority_root_locator,
            "authority_root_locator_sha256": self.authority_root_locator_sha256,
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
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            _durable_exclusive_json_create(
                self.workspace_marker_path, self._workspace_payload(), lineage_boundary=self.workspace
            )
        except FileExistsError:
            existing = self._read_workspace_marker_id(self.workspace_marker_path)
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace marker concurrently bound to another identity"
                )

    def _ensure_authority_root_binding(self) -> None:
        existing = self._read_authority_root_binding_id(
            self.authority_root_binding_path,
            expected_locator=self.authority_root_locator,
            expected_locator_sha=self.authority_root_locator_sha256,
        )
        if existing is not None:
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace authority-root binding conflicts with immutable instance identity"
                )
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            _durable_exclusive_json_create(
                self.authority_root_binding_path,
                self._authority_root_payload(),
                lineage_boundary=self.workspace,
            )
        except FileExistsError:
            existing = self._read_authority_root_binding_id(
                self.authority_root_binding_path,
                expected_locator=self.authority_root_locator,
                expected_locator_sha=self.authority_root_locator_sha256,
            )
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "workspace authority-root binding was concurrently changed"
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
                self.path_binding_path, self._path_payload(), lineage_boundary=self.authority_root
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

    def _ensure_machine_path_binding(self) -> None:
        existing = self._read_machine_binding_id(
            self.machine_path_binding_path,
            expected_workspace_locator=self.workspace_locator,
            expected_workspace_locator_sha=self.workspace_locator_sha256,
            expected_workspace_instance_id=None,
            expected_authority_root_locator=self.authority_root_locator,
            expected_authority_root_locator_sha=self.authority_root_locator_sha256,
        )
        if existing is not None:
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "stable machine path is already bound to another instance identity"
                )
            return
        self.machine_binding_root.mkdir(parents=True, exist_ok=True)
        try:
            _durable_exclusive_json_create(
                self.machine_path_binding_path,
                self._machine_payload(),
                lineage_boundary=self.machine_binding_root,
            )
        except FileExistsError:
            existing = self._read_machine_binding_id(
                self.machine_path_binding_path,
                expected_workspace_locator=self.workspace_locator,
                expected_workspace_locator_sha=self.workspace_locator_sha256,
                expected_workspace_instance_id=None,
                expected_authority_root_locator=self.authority_root_locator,
                expected_authority_root_locator_sha=self.authority_root_locator_sha256,
            )
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "stable machine path was concurrently bound to another identity"
                )

    def _ensure_machine_identity_binding(self) -> None:
        existing = self._read_machine_binding_id(
            self.machine_identity_binding_path,
            expected_workspace_locator=None,
            expected_workspace_locator_sha=None,
            expected_workspace_instance_id=self.workspace_instance_id,
            expected_authority_root_locator=self.authority_root_locator,
            expected_authority_root_locator_sha=self.authority_root_locator_sha256,
        )
        if existing is not None:
            return
        self.machine_binding_root.mkdir(parents=True, exist_ok=True)
        try:
            _durable_exclusive_json_create(
                self.machine_identity_binding_path,
                self._machine_payload(),
                lineage_boundary=self.machine_binding_root,
            )
        except FileExistsError:
            existing = self._read_machine_binding_id(
                self.machine_identity_binding_path,
                expected_workspace_locator=None,
                expected_workspace_locator_sha=None,
                expected_workspace_instance_id=self.workspace_instance_id,
                expected_authority_root_locator=self.authority_root_locator,
                expected_authority_root_locator_sha=self.authority_root_locator_sha256,
            )
            if existing != self.workspace_instance_id:
                raise WorkspaceBindingConflictError(
                    "stable machine identity was concurrently bound to another identity"
                )
