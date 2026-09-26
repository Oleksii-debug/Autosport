"""Root-independent selection binding for monotonic workspace authority.

A caller-selectable monotonic authority root is not itself a trust anchor: after
positive history exists under root A, choosing a fresh root B can otherwise make
the same workspace/namespace look pristine.  This module records the root choice
in Autosport's stable application-state area, deliberately outside
AUTOSPORT_MONOTONIC_AUTHORITY_ROOT and outside the protected workspace.

The path selector is written before the first authority-bearing write in a
selected root.  A namespace activation witness is written after the first
authority record, letting recovery distinguish "selector won but first PREPARE
never became durable" from "this namespace was used and its selected-root
history later disappeared".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .json_integrity import strict_json_loads
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


ROOT_SELECTION_SCHEMA: Final = "autosport.monotonic_authority.root_selection"
ROOT_SELECTION_SCHEMA_VERSION: Final = 1
ROOT_SELECTION_AUTHORITY_ID: Final = "autosport.machine.monotonic.v1"
NAMESPACE_ACTIVATION_SCHEMA: Final = (
    "autosport.monotonic_authority.root_selection.namespace_activation"
)

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
_NAMESPACE_ACTIVATION_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "authority_id",
        "workspace_instance_id",
        "namespace_sha256",
        "authority_root_resolved",
        "authority_root_resolved_sha256",
        "activation_sha256",
    }
)
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES: Final = 64 * 1024


class AuthorityRootSelectionError(RuntimeError):
    """Base error for authority-root selection binding."""


class AuthorityRootSelectionConfigurationError(AuthorityRootSelectionError):
    """The stable root-selection store cannot be configured safely."""


class AuthorityRootSelectionConflictError(AuthorityRootSelectionError):
    """The selected authority root conflicts with a durable root receipt."""


class AuthorityRootSelectionIntegrityError(AuthorityRootSelectionError):
    """A durable root-selection receipt is malformed or corrupted."""


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


def _canonical_instance_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthorityRootSelectionConfigurationError(
            "workspace_instance_id must be a non-empty canonical string"
        )
    if len(value) > 256 or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise AuthorityRootSelectionConfigurationError(
            "workspace_instance_id contains unsupported characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthorityRootSelectionConfigurationError(
            "workspace_instance_id contains invalid Unicode"
        ) from exc
    return value


def _digest(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AuthorityRootSelectionIntegrityError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


def stable_root_selection_store() -> Path:
    """Return the product-owned store for root-selection receipts.

    This witness must outlive and remain independent from both the caller-selectable
    monotonic authority root and caller-editable HOME/XDG/LOCALAPPDATA environment
    variables.  Resolve the OS-owned per-user state location directly, matching the
    already-hardened STOP/account-reconciliation product authority pattern.
    """

    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            result = ctypes.windll.shell32.SHGetFolderPathW(  # type: ignore[attr-defined]
                None,
                0x001C,  # CSIDL_LOCAL_APPDATA
                None,
                0,
                buffer,
            )
        except (AttributeError, OSError, ValueError) as exc:
            raise AuthorityRootSelectionConfigurationError(
                "cannot resolve product-owned Windows root-selection store"
            ) from exc
        if result != 0 or not buffer.value:
            raise AuthorityRootSelectionConfigurationError(
                "cannot resolve product-owned Windows root-selection store"
            )
        base = Path(buffer.value)
        relative = (
            Path("Autosport")
            / "application-state"
            / "monotonic-root-selection-v1"
        )
    else:
        try:
            import pwd

            home = pwd.getpwuid(os.getuid()).pw_dir
        except (AttributeError, ImportError, KeyError, OSError) as exc:
            raise AuthorityRootSelectionConfigurationError(
                "cannot resolve product-owned POSIX root-selection store"
            ) from exc
        base = Path(home) / ".local" / "state"
        relative = Path("autosport") / "monotonic-root-selection-v1"

    if not base.is_absolute():
        raise AuthorityRootSelectionConfigurationError(
            "product-owned root-selection store must be absolute"
        )
    return base / relative


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


def _path_exists_or_is_link(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _read_strict_object(
    path: Path,
    *,
    expected_keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    try:
        path_before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise AuthorityRootSelectionIntegrityError(
            f"cannot inspect {label} path"
        ) from exc
    if not stat.S_ISREG(path_before.st_mode) or path_before.st_nlink != 1:
        raise AuthorityRootSelectionIntegrityError(
            f"{label} must be a single-link regular file"
        )
    if path_before.st_size < 0 or path_before.st_size > _MAX_RECEIPT_BYTES:
        raise AuthorityRootSelectionIntegrityError(
            f"{label} exceeds bounded root-selection receipt size"
        )

    try:
        descriptor = _open_read_only_descriptor(path)
    except OSError as exc:
        raise AuthorityRootSelectionIntegrityError(
            f"cannot open {label} safely"
        ) from exc

    primary_error: BaseException | None = None
    try:
        opened_before = os.fstat(descriptor)
        verification = _open_read_only_descriptor(path)
        try:
            same_file = os.path.sameopenfile(descriptor, verification)
            verified_stat = os.fstat(verification)
        finally:
            os.close(verification)

        if (
            opened_before.st_size < 0
            or opened_before.st_size > _MAX_RECEIPT_BYTES
            or verified_stat.st_size < 0
            or verified_stat.st_size > _MAX_RECEIPT_BYTES
        ):
            raise AuthorityRootSelectionIntegrityError(
                f"{label} exceeds bounded root-selection receipt size"
            )
        if (
            not same_file
            or not os.path.samestat(path_before, opened_before)
            or not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or not stat.S_ISREG(verified_stat.st_mode)
            or verified_stat.st_nlink != 1
        ):
            raise AuthorityRootSelectionIntegrityError(
                f"{label} path changed during verification"
            )

        chunks: list[bytes] = []
        total_bytes = 0
        while True:
            remaining = (_MAX_RECEIPT_BYTES + 1) - total_bytes
            if remaining <= 0:
                raise AuthorityRootSelectionIntegrityError(
                    f"{label} exceeds bounded root-selection receipt size"
                )
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > _MAX_RECEIPT_BYTES:
                raise AuthorityRootSelectionIntegrityError(
                    f"{label} exceeds bounded root-selection receipt size"
                )

        opened_after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (
            opened_after.st_size < 0
            or opened_after.st_size > _MAX_RECEIPT_BYTES
            or path_after.st_size < 0
            or path_after.st_size > _MAX_RECEIPT_BYTES
        ):
            raise AuthorityRootSelectionIntegrityError(
                f"{label} exceeds bounded root-selection receipt size"
            )

        final_verification = _open_read_only_descriptor(path)
        try:
            same_final_file = os.path.sameopenfile(descriptor, final_verification)
        finally:
            os.close(final_verification)

        if (
            not same_final_file
            or not os.path.samestat(opened_after, path_after)
            or not stat.S_ISREG(opened_after.st_mode)
            or opened_after.st_nlink != 1
            or not stat.S_ISREG(path_after.st_mode)
            or path_after.st_nlink != 1
            or opened_before.st_mode != opened_after.st_mode
            or opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns
            or opened_before.st_ctime_ns != opened_after.st_ctime_ns
        ):
            raise AuthorityRootSelectionIntegrityError(
                f"{label} changed while it was being read"
            )

        try:
            text = b"".join(chunks).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AuthorityRootSelectionIntegrityError(
                f"cannot read {label}"
            ) from exc
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            os.close(descriptor)
        except OSError as close_error:
            if primary_error is None:
                raise AuthorityRootSelectionIntegrityError(
                    f"cannot close {label} descriptor"
                ) from close_error
            try:
                primary_error.add_note(f"{label} descriptor close also failed")
            except BaseException:
                pass

    try:
        raw = strict_json_loads(text)
    except (TypeError, ValueError) as exc:
        raise AuthorityRootSelectionIntegrityError(
            f"invalid strict JSON in {label}"
        ) from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise AuthorityRootSelectionIntegrityError(
            f"{label} must be a JSON object"
        )
    if frozenset(raw) != expected_keys:
        raise AuthorityRootSelectionIntegrityError(
            f"{label} keys must match schema exactly"
        )
    return raw


def _verify_hash(
    raw: dict[str, object],
    *,
    digest_key: str,
    label: str,
) -> str:
    digest = _digest(digest_key, raw[digest_key])
    unhashed = dict(raw)
    unhashed.pop(digest_key)
    if _payload_hash(unhashed) != digest:
        raise AuthorityRootSelectionIntegrityError(f"{label} hash mismatch")
    return digest


@dataclass(frozen=True, slots=True)
class _SelectionContext:
    workspace_locator: str
    workspace_locator_sha256: str
    authority_root_locator: str
    authority_root_locator_sha256: str
    authority_root_resolved: str
    authority_root_resolved_sha256: str
    store_root: Path
    binding_path: Path


def _selection_context(
    *,
    workspace: Path,
    authority_root: Path,
) -> _SelectionContext:
    store_root = stable_root_selection_store()
    workspace_locator = _lexical_locator(workspace)
    workspace_locator_sha = hashlib.sha256(
        workspace_locator.encode("utf-8")
    ).hexdigest()
    root_locator = _lexical_locator(authority_root)
    root_locator_sha = hashlib.sha256(root_locator.encode("utf-8")).hexdigest()
    root_resolved = _resolved_locator(authority_root)
    root_resolved_sha = hashlib.sha256(root_resolved.encode("utf-8")).hexdigest()

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

    return _SelectionContext(
        workspace_locator=workspace_locator,
        workspace_locator_sha256=workspace_locator_sha,
        authority_root_locator=root_locator,
        authority_root_locator_sha256=root_locator_sha,
        authority_root_resolved=root_resolved,
        authority_root_resolved_sha256=root_resolved_sha,
        store_root=store_root,
        binding_path=(
            store_root
            / "workspace-path-bindings"
            / workspace_locator_sha[:2]
            / f"{workspace_locator_sha}.json"
        ),
    )


def _validate_selection_raw(
    raw: dict[str, object],
    *,
    expected_workspace_locator: str | None,
    expected_workspace_locator_sha256: str | None,
    selected_authority_root_resolved: str | None,
    selected_authority_root_resolved_sha256: str | None,
) -> str:
    if (
        raw["schema"] != ROOT_SELECTION_SCHEMA
        or raw["schema_version"] != ROOT_SELECTION_SCHEMA_VERSION
        or isinstance(raw["schema_version"], bool)
        or raw["authority_id"] != ROOT_SELECTION_AUTHORITY_ID
    ):
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding identity/schema mismatch"
        )
    _verify_hash(raw, digest_key="binding_sha256", label="authority-root binding")

    workspace_locator = raw["workspace_locator"]
    workspace_locator_sha = raw["workspace_locator_sha256"]
    if not isinstance(workspace_locator, str) or not workspace_locator:
        raise AuthorityRootSelectionIntegrityError(
            "invalid workspace locator in authority-root binding"
        )
    if _digest("workspace_locator_sha256", workspace_locator_sha) != hashlib.sha256(
        workspace_locator.encode("utf-8")
    ).hexdigest():
        raise AuthorityRootSelectionIntegrityError(
            "workspace locator digest mismatch in authority-root binding"
        )
    if (
        expected_workspace_locator is not None
        and workspace_locator != expected_workspace_locator
    ):
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding workspace locator mismatch"
        )
    if (
        expected_workspace_locator_sha256 is not None
        and workspace_locator_sha != expected_workspace_locator_sha256
    ):
        raise AuthorityRootSelectionIntegrityError(
            "authority-root binding workspace locator identity mismatch"
        )

    root_locator = raw["authority_root_locator"]
    root_locator_sha = raw["authority_root_locator_sha256"]
    if not isinstance(root_locator, str) or not root_locator:
        raise AuthorityRootSelectionIntegrityError(
            "invalid lexical authority root in root-selection binding"
        )
    if _digest("authority_root_locator_sha256", root_locator_sha) != hashlib.sha256(
        root_locator.encode("utf-8")
    ).hexdigest():
        raise AuthorityRootSelectionIntegrityError(
            "lexical authority-root digest mismatch"
        )

    root_resolved = raw["authority_root_resolved"]
    root_resolved_sha = raw["authority_root_resolved_sha256"]
    if not isinstance(root_resolved, str) or not root_resolved:
        raise AuthorityRootSelectionIntegrityError(
            "invalid resolved authority root in root-selection binding"
        )
    if _digest("authority_root_resolved_sha256", root_resolved_sha) != hashlib.sha256(
        root_resolved.encode("utf-8")
    ).hexdigest():
        raise AuthorityRootSelectionIntegrityError(
            "resolved authority-root digest mismatch"
        )
    if selected_authority_root_resolved is not None:
        if selected_authority_root_resolved_sha256 is None:
            raise AssertionError("selected authority-root digest is required")
        if (
            root_resolved != selected_authority_root_resolved
            or root_resolved_sha != selected_authority_root_resolved_sha256
        ):
            raise AuthorityRootSelectionConflictError(
                "workspace is already bound to a different monotonic authority root"
            )
    return _canonical_instance_id(raw["workspace_instance_id"])


def preflight_authority_root_selection(
    *,
    workspace: Path,
    authority_root: Path,
    requested_workspace_instance_id: str | None,
) -> str | None:
    """Resolve an existing same-path selector before workspace-id allocation.

    This function is read-only.  If a durable selector exists, its immutable
    workspace instance id becomes the requested id for the selected root.
    """

    context = _selection_context(workspace=workspace, authority_root=authority_root)
    if not _path_exists_or_is_link(context.binding_path):
        return None
    raw = _read_strict_object(
        context.binding_path,
        expected_keys=_ROOT_SELECTION_KEYS,
        label="authority-root binding",
    )
    bound_id = _validate_selection_raw(
        raw,
        expected_workspace_locator=context.workspace_locator,
        expected_workspace_locator_sha256=context.workspace_locator_sha256,
        selected_authority_root_resolved=context.authority_root_resolved,
        selected_authority_root_resolved_sha256=context.authority_root_resolved_sha256,
    )
    if (
        requested_workspace_instance_id is not None
        and _canonical_instance_id(requested_workspace_instance_id) != bound_id
    ):
        raise AuthorityRootSelectionConflictError(
            "requested workspace_instance_id conflicts with authority-root binding"
        )
    return bound_id


@dataclass(frozen=True, slots=True)
class AuthorityRootSelectionBinding:
    workspace: Path
    workspace_instance_id: str
    authority_root: Path
    context: _SelectionContext

    @classmethod
    def resolve(
        cls,
        *,
        workspace: Path,
        workspace_instance_id: str,
        authority_root: Path,
    ) -> "AuthorityRootSelectionBinding":
        binding = cls(
            workspace=workspace,
            workspace_instance_id=_canonical_instance_id(workspace_instance_id),
            authority_root=authority_root,
            context=_selection_context(
                workspace=workspace,
                authority_root=authority_root,
            ),
        )
        binding.validate_existing()
        return binding

    @property
    def store_root(self) -> Path:
        return self.context.store_root

    @property
    def binding_path(self) -> Path:
        return self.context.binding_path

    @property
    def instance_registration_lock_root(self) -> Path:
        instance_sha = hashlib.sha256(self.workspace_instance_id.encode("utf-8")).hexdigest()
        return (
            self.store_root
            / "workspace-instance-registration-locks"
            / instance_sha[:2]
            / instance_sha
        )

    def namespace_activation_path(self, namespace_sha256: str) -> Path:
        namespace_sha = _digest("namespace_sha256", namespace_sha256)
        return (
            self.store_root
            / "namespace-activations"
            / namespace_sha[:2]
            / f"{namespace_sha}.json"
        )

    def _selection_payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": ROOT_SELECTION_SCHEMA,
            "schema_version": ROOT_SELECTION_SCHEMA_VERSION,
            "authority_id": ROOT_SELECTION_AUTHORITY_ID,
            "workspace_locator": self.context.workspace_locator,
            "workspace_locator_sha256": self.context.workspace_locator_sha256,
            "workspace_instance_id": self.workspace_instance_id,
            "authority_root_locator": self.context.authority_root_locator,
            "authority_root_locator_sha256": self.context.authority_root_locator_sha256,
            "authority_root_resolved": self.context.authority_root_resolved,
            "authority_root_resolved_sha256": self.context.authority_root_resolved_sha256,
        }
        return {**unhashed, "binding_sha256": _payload_hash(unhashed)}

    def _iter_selection_records(self):
        root = self.store_root / "workspace-path-bindings"
        if not root.exists():
            return
        try:
            paths = sorted(root.glob("*/*.json"))
        except OSError as exc:
            raise AuthorityRootSelectionIntegrityError(
                "cannot enumerate authority-root bindings"
            ) from exc
        for path in paths:
            yield path

    def _validate_path(self, path: Path, *, current_path: bool) -> str:
        raw = _read_strict_object(
            path,
            expected_keys=_ROOT_SELECTION_KEYS,
            label="authority-root binding",
        )
        bound_id = _validate_selection_raw(
            raw,
            expected_workspace_locator=(
                self.context.workspace_locator if current_path else None
            ),
            expected_workspace_locator_sha256=(
                self.context.workspace_locator_sha256 if current_path else None
            ),
            selected_authority_root_resolved=(
                self.context.authority_root_resolved if current_path else None
            ),
            selected_authority_root_resolved_sha256=(
                self.context.authority_root_resolved_sha256
                if current_path
                else None
            ),
        )
        if not current_path and bound_id == self.workspace_instance_id:
            _validate_selection_raw(
                raw,
                expected_workspace_locator=None,
                expected_workspace_locator_sha256=None,
                selected_authority_root_resolved=self.context.authority_root_resolved,
                selected_authority_root_resolved_sha256=(
                    self.context.authority_root_resolved_sha256
                ),
            )
        return bound_id

    def validate_existing(self) -> bool:
        """Validate direct or moved/copied root selection without writing."""

        found_same_identity = False
        if _path_exists_or_is_link(self.binding_path):
            bound_id = self._validate_path(self.binding_path, current_path=True)
            if bound_id != self.workspace_instance_id:
                raise AuthorityRootSelectionConflictError(
                    "workspace path is bound to a different immutable workspace identity"
                )
            found_same_identity = True

        # Never stop at the direct path.  A racing first registration can leave a
        # second path receipt for the same immutable instance.  Cross-scan all
        # durable selectors so an already-materialized split fails closed rather
        # than allowing each path to validate in isolation.
        for path in self._iter_selection_records() or ():
            if path == self.binding_path:
                continue
            bound_id = self._validate_path(path, current_path=False)
            if bound_id == self.workspace_instance_id:
                found_same_identity = True
        return found_same_identity

    def ensure_bound(self) -> None:
        """Durably reserve this workspace path for the selected authority root."""

        # Distinct lexical workspace paths hash to distinct receipt files, so O_EXCL
        # alone cannot serialize first registration for one immutable instance.
        # Reuse Autosport's existing cross-process lock primitive under a stable,
        # instance-derived store path, then revalidate the complete identity set
        # before and after publication.
        with WorkspaceEconomicLock(self.instance_registration_lock_root):
            if _path_exists_or_is_link(self.binding_path):
                if not self.validate_existing():
                    raise AuthorityRootSelectionIntegrityError(
                        "authority-root binding disappeared during validation"
                    )
                return

            # An existing receipt for this immutable workspace identity, even at a
            # moved/copied path, must agree on the physical root before a new alias is
            # registered.  validate_existing() performs that check.
            self.validate_existing()
            try:
                _durable_exclusive_json_create(
                    self.binding_path,
                    self._selection_payload(),
                    lineage_boundary=self.store_root,
                )
            except FileExistsError:
                if not self.validate_existing():
                    raise AuthorityRootSelectionIntegrityError(
                        "authority-root binding concurrently disappeared"
                    )
            except AuthorityRootSelectionError:
                raise
            except OSError as exc:
                raise AuthorityRootSelectionIntegrityError(
                    "cannot durably persist authority-root selection binding"
                ) from exc
            else:
                if not self.validate_existing():
                    raise AuthorityRootSelectionIntegrityError(
                        "authority-root binding disappeared after publication"
                    )

    def _activation_payload(self, namespace_sha256: str) -> dict[str, object]:
        namespace_sha = _digest("namespace_sha256", namespace_sha256)
        unhashed: dict[str, object] = {
            "schema": NAMESPACE_ACTIVATION_SCHEMA,
            "schema_version": ROOT_SELECTION_SCHEMA_VERSION,
            "authority_id": ROOT_SELECTION_AUTHORITY_ID,
            "workspace_instance_id": self.workspace_instance_id,
            "namespace_sha256": namespace_sha,
            "authority_root_resolved": self.context.authority_root_resolved,
            "authority_root_resolved_sha256": self.context.authority_root_resolved_sha256,
        }
        return {**unhashed, "activation_sha256": _payload_hash(unhashed)}

    def validate_namespace_activation(self, namespace_sha256: str) -> bool:
        namespace_sha = _digest("namespace_sha256", namespace_sha256)
        path = self.namespace_activation_path(namespace_sha)
        if not _path_exists_or_is_link(path):
            return False
        raw = _read_strict_object(
            path,
            expected_keys=_NAMESPACE_ACTIVATION_KEYS,
            label="authority-root namespace activation",
        )
        if (
            raw["schema"] != NAMESPACE_ACTIVATION_SCHEMA
            or raw["schema_version"] != ROOT_SELECTION_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != ROOT_SELECTION_AUTHORITY_ID
            or raw["workspace_instance_id"] != self.workspace_instance_id
            or raw["namespace_sha256"] != namespace_sha
        ):
            raise AuthorityRootSelectionIntegrityError(
                "authority-root namespace activation identity/schema mismatch"
            )
        _verify_hash(
            raw,
            digest_key="activation_sha256",
            label="authority-root namespace activation",
        )
        resolved = raw["authority_root_resolved"]
        resolved_sha = raw["authority_root_resolved_sha256"]
        if (
            not isinstance(resolved, str)
            or _digest("authority_root_resolved_sha256", resolved_sha)
            != hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        ):
            raise AuthorityRootSelectionIntegrityError(
                "authority-root namespace activation root digest mismatch"
            )
        if (
            resolved != self.context.authority_root_resolved
            or resolved_sha != self.context.authority_root_resolved_sha256
        ):
            raise AuthorityRootSelectionConflictError(
                "authority namespace is activated under a different monotonic root"
            )
        return True

    def ensure_namespace_activated(self, namespace_sha256: str) -> None:
        self.ensure_bound()
        path = self.namespace_activation_path(namespace_sha256)
        if self.validate_namespace_activation(namespace_sha256):
            return
        try:
            _durable_exclusive_json_create(
                path,
                self._activation_payload(namespace_sha256),
                lineage_boundary=self.store_root,
            )
        except FileExistsError:
            if not self.validate_namespace_activation(namespace_sha256):
                raise AuthorityRootSelectionIntegrityError(
                    "authority-root namespace activation concurrently disappeared"
                )
        except AuthorityRootSelectionError:
            raise
        except OSError as exc:
            raise AuthorityRootSelectionIntegrityError(
                "cannot durably persist monotonic authority namespace activation"
            ) from exc
