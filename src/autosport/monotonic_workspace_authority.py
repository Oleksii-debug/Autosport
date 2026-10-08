"""Independent machine-state monotonic authority for workspace-local durable state.

The authority deliberately stores only opaque state digests and semantic-binding
hashes. Domain writers remain responsible for their canonical workspace state and
must follow PREPARE -> local publish -> COMMIT.

This primitive protects workspace rollback/deletion while its separate machine-state
root survives. It intentionally does not claim resistance when both trust roots are
rolled back or deleted together.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from .json_integrity import strict_json_loads
from .monotonic_authority_root_binding import (
    AuthorityRootSelectionBinding,
    AuthorityRootSelectionConfigurationError,
    AuthorityRootSelectionConflictError,
    AuthorityRootSelectionIntegrityError,
    preflight_authority_root_selection,
)
from .monotonic_workspace_binding import (
    WorkspaceBindingConflictError,
    WorkspaceBindingIntegrityError,
    WorkspaceIdentityBinding,
)
from .workspace_lock import WorkspaceEconomicLock


AUTHORITY_SCHEMA: Final = "autosport.monotonic_authority"
AUTHORITY_SCHEMA_VERSION: Final = 1
AUTHORITY_ID: Final = "autosport.machine.monotonic.v1"
WORKSPACE_ROLLBACK_RESISTANT_WHILE_MACHINE_AUTHORITY_SURVIVES: Final = True
FULL_MACHINE_ROLLBACK_RESISTANT: Final = False

_NAMESPACE_SCHEMA: Final = "autosport.monotonic_authority.namespace"
_NAMESPACE_MARKER_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "authority_id",
        "workspace_instance_id",
        "domain",
        "key",
        "namespace_sha256",
        "marker_sha256",
    }
)
_RECORD_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "authority_id",
        "workspace_instance_id",
        "domain",
        "key",
        "generation",
        "tx_id",
        "phase",
        "previous_record_sha256",
        "previous_committed_generation",
        "previous_committed_state_sha256",
        "intended_state_sha256",
        "semantic_binding_sha256",
        "record_sha256",
    }
)
_RECORD_FILE_RE: Final = re.compile(
    r"^(?P<index>[0-9]{20})-(?P<digest>[0-9a-f]{64})\.json$"
)
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class MonotonicWorkspaceAuthorityError(RuntimeError):
    """Base error for monotonic authority configuration or transitions."""


class MonotonicAuthorityConfigurationError(MonotonicWorkspaceAuthorityError):
    """The authority trust root or identity is unsafe or ambiguous."""


class MonotonicAuthorityIntegrityError(MonotonicWorkspaceAuthorityError):
    """Persisted authority history is malformed, forked, or inconsistent."""


class MonotonicAuthorityConflictError(MonotonicWorkspaceAuthorityError):
    """A transition conflicts with the durable monotonic authority tip."""


class MonotonicAuthorityRollbackError(MonotonicAuthorityConflictError):
    """Observed workspace state is stale, missing, or unproven."""


class MonotonicAuthorityRecoveryRequiredError(MonotonicAuthorityConflictError):
    """Prepared local bytes need exact semantic proof before COMMIT."""


class AuthorityPhase(str, Enum):
    PREPARE = "PREPARE"
    COMMIT = "COMMIT"
    ABORT = "ABORT"


class RecoveryDisposition(str, Enum):
    PRISTINE = "PRISTINE"
    CURRENT = "CURRENT"
    ABORTED_PREPARE = "ABORTED_PREPARE"
    COMMITTED_PREPARE = "COMMITTED_PREPARE"


@dataclass(frozen=True, slots=True)
class AuthorityRecord:
    authority_id: str
    workspace_instance_id: str
    domain: str
    key: str
    generation: int
    tx_id: str
    phase: AuthorityPhase
    previous_record_sha256: str | None
    previous_committed_generation: int
    previous_committed_state_sha256: str | None
    intended_state_sha256: str
    semantic_binding_sha256: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class AuthorityRecovery:
    disposition: RecoveryDisposition
    committed_generation: int
    committed_state_sha256: str | None
    record: AuthorityRecord | None


@dataclass(frozen=True, slots=True)
class _History:
    records: tuple[AuthorityRecord, ...]
    latest_committed_generation: int
    latest_committed_state_sha256: str | None
    pending: AuthorityRecord | None
    last_generation: int


def _absolute_path(name: str, value: str | Path) -> Path:
    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise MonotonicAuthorityConfigurationError(
            f"{name} home expansion could not be resolved"
        ) from exc
    if not path.is_absolute():
        raise MonotonicAuthorityConfigurationError(f"{name} must be an absolute path")
    return path


def default_monotonic_authority_root() -> Path:
    """Return a machine-state root outside the normal Autosport workspace tree."""

    override = os.environ.get("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT")
    if override is not None and override.strip():
        return _absolute_path("AUTOSPORT_MONOTONIC_AUTHORITY_ROOT", override)

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = _absolute_path("LOCALAPPDATA", local_app_data)
        return base / "Autosport" / "application-state" / "monotonic-authority-v1"

    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home:
        base = _absolute_path("XDG_STATE_HOME", xdg_state_home)
        return base / "autosport" / "monotonic-authority-v1"

    try:
        home = Path.home()
    except RuntimeError as exc:
        raise MonotonicAuthorityConfigurationError(
            "home directory could not be resolved for monotonic authority"
        ) from exc
    if not home.is_absolute():
        raise MonotonicAuthorityConfigurationError(
            "home directory must be absolute for monotonic authority"
        )
    return home / ".local" / "state" / "autosport" / "monotonic-authority-v1"


def resolve_monotonic_authority_root(
    workspace: str | Path,
    authority_root: str | Path | None = None,
) -> Path:
    """Prove that the machine authority and protected workspace are disjoint trees."""

    workspace_path = _absolute_path("workspace", workspace)
    root = (
        default_monotonic_authority_root()
        if authority_root is None
        else _absolute_path("authority_root", authority_root)
    )
    try:
        resolved_workspace = workspace_path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError as exc:
        raise MonotonicAuthorityConfigurationError(
            "cannot resolve workspace/authority trust roots"
        ) from exc

    if (
        resolved_root == resolved_workspace
        or resolved_root.is_relative_to(resolved_workspace)
        or resolved_workspace.is_relative_to(resolved_root)
    ):
        raise MonotonicAuthorityConfigurationError(
            "monotonic authority root and protected workspace must be disjoint trees"
        )
    if root.exists() and not root.is_dir():
        raise MonotonicAuthorityConfigurationError(
            "monotonic authority root must be a directory"
        )
    return root


def _text(name: str, value: object, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MonotonicAuthorityConfigurationError(
            f"{name} must be a non-empty canonical string"
        )
    if len(value) > max_length or "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise MonotonicAuthorityConfigurationError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MonotonicAuthorityConfigurationError(
            f"{name} contains invalid Unicode"
        ) from exc
    return value


def _digest(name: str, value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MonotonicAuthorityConflictError(
            f"{name} must be a lowercase SHA-256 hex digest"
        )
    return value


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
        raise MonotonicAuthorityIntegrityError(
            "authority record is outside canonical JSON domain"
        ) from exc


def _record_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MonotonicAuthorityIntegrityError(
            "cannot open monotonic authority directory for durability"
        ) from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise MonotonicAuthorityIntegrityError(
            "cannot fsync monotonic authority directory"
        ) from exc
    finally:
        os.close(descriptor)


def _sync_directory_lineage(authority_root: Path, leaf: Path) -> None:
    if os.name == "nt":
        return
    current = leaf
    boundary = authority_root.parent
    while True:
        _fsync_directory(current)
        if current == boundary:
            return
        if not current.is_relative_to(boundary):
            raise MonotonicAuthorityIntegrityError(
                "authority journal escaped configured machine-state root"
            )
        current = current.parent


def _durable_exclusive_json_create(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


class MonotonicWorkspaceAuthority:
    """Append-only independent freshness authority for one opaque workspace key.

    Callers acquire their canonical workspace writer lock first, then call this
    object, establishing the global workspace -> authority lock order. ``prepare``
    reserves a generation against the exact currently observed local digest.
    Callers then publish/fsync their domain state, re-read/hash it, and ``commit``.
    ``recover`` resolves crash prefixes without guessing domain semantics.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        workspace_instance_id: str | None = None,
        domain: str,
        key: str,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = _absolute_path("workspace", workspace)
        requested_workspace_instance_id = (
            None
            if workspace_instance_id is None
            else _text(
                "workspace_instance_id", workspace_instance_id, max_length=256
            )
        )
        self.domain = _text("domain", domain, max_length=256)
        self.key = _text("key", key, max_length=512)
        self.authority_root = resolve_monotonic_authority_root(
            self.workspace, authority_root
        )
        try:
            selected_workspace_instance_id = preflight_authority_root_selection(
                workspace=self.workspace,
                authority_root=self.authority_root,
                requested_workspace_instance_id=requested_workspace_instance_id,
            )
        except (
            AuthorityRootSelectionConfigurationError,
            AuthorityRootSelectionConflictError,
        ) as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except AuthorityRootSelectionIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc
        if selected_workspace_instance_id is not None:
            requested_workspace_instance_id = selected_workspace_instance_id
        try:
            self.workspace_binding = WorkspaceIdentityBinding.resolve(
                workspace=self.workspace,
                authority_root=self.authority_root,
                requested_workspace_instance_id=requested_workspace_instance_id,
            )
        except WorkspaceBindingConflictError as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except WorkspaceBindingIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc
        self.workspace_instance_id = _text(
            "workspace_instance_id",
            self.workspace_binding.workspace_instance_id,
            max_length=256,
        )
        try:
            self.authority_root_selection = AuthorityRootSelectionBinding.resolve(
                workspace=self.workspace,
                workspace_instance_id=self.workspace_instance_id,
                authority_root=self.authority_root,
            )
        except (
            AuthorityRootSelectionConfigurationError,
            AuthorityRootSelectionConflictError,
        ) as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except AuthorityRootSelectionIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc
        self.authority_root_binding_path = self.authority_root_selection.binding_path
        self.workspace_binding_path = self.workspace_binding.workspace_marker_path
        namespace_material = "\0".join(
            (AUTHORITY_ID, self.workspace_instance_id, self.domain, self.key)
        ).encode("utf-8")
        self.namespace_sha256 = hashlib.sha256(namespace_material).hexdigest()
        self.authority_root_activation_path = (
            self.authority_root_selection.namespace_activation_path(
                self.namespace_sha256
            )
        )
        self._validate_authority_root_activation()
        self.journal_dir = (
            self.authority_root
            / "journals"
            / self.namespace_sha256[:2]
            / self.namespace_sha256
        )
        self.records_dir = self.journal_dir / "records"
        self.namespace_marker_path = (
            self.authority_root
            / "namespace-bindings"
            / self.namespace_sha256[:2]
            / f"{self.namespace_sha256}.json"
        )

    def prepare(
        self,
        *,
        tx_id: str,
        observed_state_sha256: str | None,
        intended_state_sha256: str,
        semantic_binding_sha256: str,
    ) -> AuthorityRecord:
        tx_id = _text("tx_id", tx_id, max_length=256)
        observed = _digest(
            "observed_state_sha256", observed_state_sha256, allow_none=True
        )
        intended = _digest("intended_state_sha256", intended_state_sha256)
        binding = _digest("semantic_binding_sha256", semantic_binding_sha256)
        assert isinstance(intended, str)
        assert isinstance(binding, str)

        # The root selector must win before the per-root journal lock can create
        # any directory/lock state under a caller-selected fresh root.
        self._ensure_authority_root_bound()
        with WorkspaceEconomicLock(self.journal_dir):
            history = self._load_bound_history()
            prior = self._latest_record_for_tx(history, tx_id)
            if prior is not None:
                self._require_same_transaction(prior, intended, binding)
                self._validate_prepare_retry(history, prior, observed)
                return prior
            if history.pending is not None:
                raise MonotonicAuthorityConflictError(
                    f"authority has pending tx {history.pending.tx_id!r}"
                )
            if observed != history.latest_committed_state_sha256:
                if not history.records and observed is not None:
                    raise MonotonicAuthorityRollbackError(
                        "workspace has state but independent authority history is missing"
                    )
                raise MonotonicAuthorityRollbackError(
                    "observed workspace state does not match latest committed authority state"
                )
            self._ensure_workspace_bound()
            record = self._new_record(
                history,
                generation=history.last_generation + 1,
                tx_id=tx_id,
                phase=AuthorityPhase.PREPARE,
                intended=intended,
                binding=binding,
            )
            self._append_record(record, len(history.records) + 1)
            self._ensure_authority_root_activated()
            return record

    def commit(
        self,
        *,
        tx_id: str,
        observed_state_sha256: str,
        semantic_binding_sha256: str,
    ) -> AuthorityRecord:
        tx_id = _text("tx_id", tx_id, max_length=256)
        observed = _digest("observed_state_sha256", observed_state_sha256)
        binding = _digest("semantic_binding_sha256", semantic_binding_sha256)
        assert isinstance(observed, str)
        assert isinstance(binding, str)

        with WorkspaceEconomicLock(self.journal_dir):
            history = self._load_bound_history()
            tx_record = self._latest_record_for_tx(history, tx_id)
            if tx_record is None:
                raise MonotonicAuthorityConflictError("cannot commit unknown authority tx")
            self._require_same_transaction(tx_record, observed, binding)
            if tx_record.phase is AuthorityPhase.COMMIT:
                if (
                    tx_record.generation != history.latest_committed_generation
                    or tx_record.intended_state_sha256
                    != history.latest_committed_state_sha256
                ):
                    raise MonotonicAuthorityConflictError(
                        "committed tx is no longer the current authority tip"
                    )
                return tx_record
            if tx_record.phase is AuthorityPhase.ABORT:
                raise MonotonicAuthorityConflictError(
                    "cannot commit an aborted authority tx"
                )
            if history.pending is None or history.pending.tx_id != tx_id:
                raise MonotonicAuthorityIntegrityError(
                    "prepared transaction is not the durable authority tip"
                )
            terminal = self._new_terminal_record(
                history, tx_record, AuthorityPhase.COMMIT
            )
            self._append_record(terminal, len(history.records) + 1)
            return terminal

    def abort(
        self,
        *,
        tx_id: str,
        observed_state_sha256: str | None,
        semantic_binding_sha256: str,
    ) -> AuthorityRecord:
        tx_id = _text("tx_id", tx_id, max_length=256)
        observed = _digest(
            "observed_state_sha256", observed_state_sha256, allow_none=True
        )
        binding = _digest("semantic_binding_sha256", semantic_binding_sha256)
        assert isinstance(binding, str)

        with WorkspaceEconomicLock(self.journal_dir):
            history = self._load_bound_history()
            tx_record = self._latest_record_for_tx(history, tx_id)
            if tx_record is None:
                raise MonotonicAuthorityConflictError("cannot abort unknown authority tx")
            if tx_record.semantic_binding_sha256 != binding:
                raise MonotonicAuthorityConflictError(
                    "same tx_id was reused with a different semantic binding"
                )
            if tx_record.phase is AuthorityPhase.ABORT:
                if (
                    tx_record.generation != history.last_generation
                    or tx_record.previous_committed_state_sha256
                    != history.latest_committed_state_sha256
                ):
                    raise MonotonicAuthorityConflictError(
                        "aborted tx is no longer the current authority tip"
                    )
                if observed != tx_record.previous_committed_state_sha256:
                    raise MonotonicAuthorityConflictError(
                        "aborted tx retry does not observe the previous committed state"
                    )
                return tx_record
            if tx_record.phase is AuthorityPhase.COMMIT:
                raise MonotonicAuthorityConflictError(
                    "cannot abort a committed authority tx"
                )
            if history.pending is None or history.pending.tx_id != tx_id:
                raise MonotonicAuthorityIntegrityError(
                    "prepared transaction is not the durable authority tip"
                )
            if observed != tx_record.previous_committed_state_sha256:
                raise MonotonicAuthorityConflictError(
                    "cannot abort after local state diverged from previous COMMIT"
                )
            terminal = self._new_terminal_record(
                history, tx_record, AuthorityPhase.ABORT
            )
            self._append_record(terminal, len(history.records) + 1)
            return terminal

    def recover(
        self,
        *,
        observed_state_sha256: str | None,
        tx_id: str | None = None,
        semantic_binding_sha256: str | None = None,
    ) -> AuthorityRecovery:
        observed = _digest(
            "observed_state_sha256", observed_state_sha256, allow_none=True
        )
        if tx_id is not None:
            tx_id = _text("tx_id", tx_id, max_length=256)
        if semantic_binding_sha256 is not None:
            semantic_binding_sha256 = _digest(
                "semantic_binding_sha256", semantic_binding_sha256
            )

        with WorkspaceEconomicLock(self.journal_dir):
            history = self._load_bound_history()
            if not history.records:
                if observed is None:
                    return AuthorityRecovery(
                        RecoveryDisposition.PRISTINE, 0, None, None
                    )
                raise MonotonicAuthorityRollbackError(
                    "workspace has state but independent authority history is missing"
                )

            pending = history.pending
            if pending is not None:
                if observed == pending.previous_committed_state_sha256:
                    terminal = self._new_terminal_record(
                        history, pending, AuthorityPhase.ABORT
                    )
                    self._append_record(terminal, len(history.records) + 1)
                    return AuthorityRecovery(
                        RecoveryDisposition.ABORTED_PREPARE,
                        history.latest_committed_generation,
                        history.latest_committed_state_sha256,
                        terminal,
                    )
                if observed == pending.intended_state_sha256:
                    if (
                        tx_id != pending.tx_id
                        or semantic_binding_sha256 != pending.semantic_binding_sha256
                    ):
                        raise MonotonicAuthorityRecoveryRequiredError(
                            "prepared local state requires exact tx_id and semantic binding"
                        )
                    terminal = self._new_terminal_record(
                        history, pending, AuthorityPhase.COMMIT
                    )
                    self._append_record(terminal, len(history.records) + 1)
                    return AuthorityRecovery(
                        RecoveryDisposition.COMMITTED_PREPARE,
                        pending.generation,
                        pending.intended_state_sha256,
                        terminal,
                    )
                raise MonotonicAuthorityConflictError(
                    "workspace state matches neither previous COMMIT nor prepared state"
                )

            if observed != history.latest_committed_state_sha256:
                raise MonotonicAuthorityRollbackError(
                    "workspace state is missing, rolled back, or unproven against authority"
                )
            latest_commit = next(
                (
                    record
                    for record in reversed(history.records)
                    if record.phase is AuthorityPhase.COMMIT
                ),
                None,
            )
            return AuthorityRecovery(
                RecoveryDisposition.CURRENT,
                history.latest_committed_generation,
                history.latest_committed_state_sha256,
                latest_commit,
            )

    def read_history(self) -> tuple[AuthorityRecord, ...]:
        """Return one fully validated immutable history snapshot."""
        with WorkspaceEconomicLock(self.journal_dir):
            return self._load_bound_history().records

    def _validate_authority_root_selection(self) -> bool:
        try:
            return self.authority_root_selection.validate_existing()
        except (
            AuthorityRootSelectionConfigurationError,
            AuthorityRootSelectionConflictError,
        ) as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except AuthorityRootSelectionIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc

    def _ensure_authority_root_bound(self) -> None:
        try:
            self.authority_root_selection.ensure_bound()
        except (
            AuthorityRootSelectionConfigurationError,
            AuthorityRootSelectionConflictError,
        ) as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except AuthorityRootSelectionIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot durably persist monotonic authority-root selection"
            ) from exc

    def _validate_authority_root_activation(self) -> bool:
        try:
            return self.authority_root_selection.validate_namespace_activation(
                self.namespace_sha256
            )
        except (
            AuthorityRootSelectionConfigurationError,
            AuthorityRootSelectionConflictError,
        ) as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except AuthorityRootSelectionIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc

    def _ensure_authority_root_activated(self) -> None:
        try:
            self.authority_root_selection.ensure_namespace_activated(
                self.namespace_sha256
            )
        except (
            AuthorityRootSelectionConfigurationError,
            AuthorityRootSelectionConflictError,
        ) as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except AuthorityRootSelectionIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot durably persist monotonic authority namespace activation"
            ) from exc

    def _validate_workspace_binding(
        self, *, register_moved_or_copied_path: bool = True
    ) -> tuple[bool, bool]:
        try:
            return self.workspace_binding.validate_existing(
                register_moved_or_copied_path=register_moved_or_copied_path
            )
        except WorkspaceBindingConflictError as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except WorkspaceBindingIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc

    def _ensure_workspace_bound(self) -> None:
        self._ensure_authority_root_bound()
        try:
            self.workspace_binding.ensure_bound()
        except WorkspaceBindingConflictError as exc:
            raise MonotonicAuthorityConfigurationError(str(exc)) from exc
        except WorkspaceBindingIntegrityError as exc:
            raise MonotonicAuthorityIntegrityError(str(exc)) from exc
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot durably persist immutable workspace identity binding"
            ) from exc

    def _load_bound_history(self) -> _History:
        root_bound = self._validate_authority_root_selection()
        root_activated = self._validate_authority_root_activation()
        workspace_bound, path_bound = self._validate_workspace_binding(
            register_moved_or_copied_path=False
        )
        history = self._load_history()

        if root_activated and not history.records:
            raise MonotonicAuthorityIntegrityError(
                "activated monotonic authority history is missing under selected root"
            )
        if history.records and not workspace_bound:
            raise MonotonicAuthorityIntegrityError(
                "authority history exists but workspace identity binding is missing"
            )

        if not root_bound:
            if history.records or (workspace_bound and path_bound):
                # Safe upgrade from the integrated pre-root-selection format:
                # the selected machine root already proves ownership through
                # non-empty authority history, or through both durable identity
                # bindings created by a first PREPARE crash prefix.
                self._ensure_authority_root_bound()
                root_bound = True
            elif workspace_bound or path_bound:
                raise MonotonicAuthorityIntegrityError(
                    "bound workspace has no authority-root selection proof or "
                    "history under the selected machine root"
                )

        if history.records and not root_activated:
            # A record can become durable immediately before the independent
            # activation witness.  Reconstruct only from fully validated history.
            self._ensure_authority_root_activated()
            root_activated = True

        if root_bound:
            # A moved/copied workspace may have proven the root through another
            # stable path receipt for the same immutable workspace identity.
            # Persist this path's root selector before registering its per-root
            # workspace-path alias, so later deletion at the new path cannot
            # reopen a fresh machine-root universe.
            self._ensure_authority_root_bound()
            self._validate_workspace_binding(register_moved_or_copied_path=True)
        return history

    @staticmethod
    def _latest_record_for_tx(
        history: _History, tx_id: str
    ) -> AuthorityRecord | None:
        return next(
            (record for record in reversed(history.records) if record.tx_id == tx_id),
            None,
        )

    @staticmethod
    def _require_same_transaction(
        record: AuthorityRecord, intended: str, binding: str
    ) -> None:
        if (
            record.intended_state_sha256 != intended
            or record.semantic_binding_sha256 != binding
        ):
            raise MonotonicAuthorityConflictError(
                "same tx_id was reused with a different digest or semantic binding"
            )

    @staticmethod
    def _validate_prepare_retry(
        history: _History,
        prior: AuthorityRecord,
        observed: str | None,
    ) -> None:
        if prior.phase is AuthorityPhase.COMMIT:
            if (
                prior.generation != history.latest_committed_generation
                or prior.intended_state_sha256
                != history.latest_committed_state_sha256
            ):
                raise MonotonicAuthorityConflictError(
                    "committed tx is no longer the current authority tip"
                )
            if observed != prior.intended_state_sha256:
                raise MonotonicAuthorityRollbackError(
                    "committed tx retry does not observe the committed workspace state"
                )
            return
        if prior.phase is AuthorityPhase.ABORT:
            if (
                prior.generation != history.last_generation
                or prior.previous_committed_state_sha256
                != history.latest_committed_state_sha256
            ):
                raise MonotonicAuthorityConflictError(
                    "aborted tx is no longer the current authority tip"
                )
            if observed != prior.previous_committed_state_sha256:
                raise MonotonicAuthorityRollbackError(
                    "aborted tx retry does not observe the current workspace state"
                )
            return
        if history.pending is None or history.pending.tx_id != prior.tx_id:
            raise MonotonicAuthorityIntegrityError(
                "prepared transaction is not the durable authority tip"
            )
        if observed not in (
            prior.previous_committed_state_sha256,
            prior.intended_state_sha256,
        ):
            raise MonotonicAuthorityConflictError(
                "prepared tx retry observes neither previous COMMIT nor intended state"
            )

    def _new_record(
        self,
        history: _History,
        *,
        generation: int,
        tx_id: str,
        phase: AuthorityPhase,
        intended: str,
        binding: str,
    ) -> AuthorityRecord:
        previous_record = history.records[-1].record_sha256 if history.records else None
        unhashed: dict[str, object] = {
            "schema": AUTHORITY_SCHEMA,
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "authority_id": AUTHORITY_ID,
            "workspace_instance_id": self.workspace_instance_id,
            "domain": self.domain,
            "key": self.key,
            "generation": generation,
            "tx_id": tx_id,
            "phase": phase.value,
            "previous_record_sha256": previous_record,
            "previous_committed_generation": history.latest_committed_generation,
            "previous_committed_state_sha256": history.latest_committed_state_sha256,
            "intended_state_sha256": intended,
            "semantic_binding_sha256": binding,
        }
        record_sha = _record_hash(unhashed)
        return AuthorityRecord(
            AUTHORITY_ID,
            self.workspace_instance_id,
            self.domain,
            self.key,
            generation,
            tx_id,
            phase,
            previous_record,
            history.latest_committed_generation,
            history.latest_committed_state_sha256,
            intended,
            binding,
            record_sha,
        )

    def _new_terminal_record(
        self,
        history: _History,
        prepared: AuthorityRecord,
        phase: AuthorityPhase,
    ) -> AuthorityRecord:
        if phase not in (AuthorityPhase.COMMIT, AuthorityPhase.ABORT):
            raise AssertionError("terminal authority phase required")
        previous_record = history.records[-1].record_sha256
        unhashed: dict[str, object] = {
            "schema": AUTHORITY_SCHEMA,
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "authority_id": AUTHORITY_ID,
            "workspace_instance_id": self.workspace_instance_id,
            "domain": self.domain,
            "key": self.key,
            "generation": prepared.generation,
            "tx_id": prepared.tx_id,
            "phase": phase.value,
            "previous_record_sha256": previous_record,
            "previous_committed_generation": prepared.previous_committed_generation,
            "previous_committed_state_sha256": prepared.previous_committed_state_sha256,
            "intended_state_sha256": prepared.intended_state_sha256,
            "semantic_binding_sha256": prepared.semantic_binding_sha256,
        }
        record_sha = _record_hash(unhashed)
        return AuthorityRecord(
            AUTHORITY_ID,
            self.workspace_instance_id,
            self.domain,
            self.key,
            prepared.generation,
            prepared.tx_id,
            phase,
            previous_record,
            prepared.previous_committed_generation,
            prepared.previous_committed_state_sha256,
            prepared.intended_state_sha256,
            prepared.semantic_binding_sha256,
            record_sha,
        )

    @staticmethod
    def _payload(record: AuthorityRecord) -> dict[str, object]:
        return {
            "schema": AUTHORITY_SCHEMA,
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "authority_id": record.authority_id,
            "workspace_instance_id": record.workspace_instance_id,
            "domain": record.domain,
            "key": record.key,
            "generation": record.generation,
            "tx_id": record.tx_id,
            "phase": record.phase.value,
            "previous_record_sha256": record.previous_record_sha256,
            "previous_committed_generation": record.previous_committed_generation,
            "previous_committed_state_sha256": record.previous_committed_state_sha256,
            "intended_state_sha256": record.intended_state_sha256,
            "semantic_binding_sha256": record.semantic_binding_sha256,
            "record_sha256": record.record_sha256,
        }

    def _namespace_payload(self) -> dict[str, object]:
        unhashed: dict[str, object] = {
            "schema": _NAMESPACE_SCHEMA,
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "authority_id": AUTHORITY_ID,
            "workspace_instance_id": self.workspace_instance_id,
            "domain": self.domain,
            "key": self.key,
            "namespace_sha256": self.namespace_sha256,
        }
        return {**unhashed, "marker_sha256": _record_hash(unhashed)}

    def _ensure_namespace_marker(self) -> None:
        if self.namespace_marker_path.exists():
            self._validate_namespace_marker()
            return
        try:
            _durable_exclusive_json_create(
                self.namespace_marker_path, self._namespace_payload()
            )
            _sync_directory_lineage(
                self.authority_root, self.namespace_marker_path.parent
            )
        except FileExistsError:
            self._validate_namespace_marker()
        except MonotonicAuthorityIntegrityError:
            raise
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot durably persist monotonic authority namespace binding"
            ) from exc

    def _validate_namespace_marker(self) -> None:
        try:
            metadata = self.namespace_marker_path.lstat()
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot inspect monotonic authority namespace binding"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MonotonicAuthorityIntegrityError(
                "authority namespace binding must be a single-link regular file"
            )
        try:
            text = self.namespace_marker_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot read monotonic authority namespace binding"
            ) from exc
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise MonotonicAuthorityIntegrityError(
                "invalid strict JSON in monotonic authority namespace binding"
            ) from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise MonotonicAuthorityIntegrityError(
                "authority namespace binding must be a JSON object"
            )
        if frozenset(raw) != _NAMESPACE_MARKER_KEYS:
            raise MonotonicAuthorityIntegrityError(
                "authority namespace binding keys must match schema exactly"
            )
        if (
            raw["schema"] != _NAMESPACE_SCHEMA
            or raw["schema_version"] != AUTHORITY_SCHEMA_VERSION
            or isinstance(raw["schema_version"], bool)
            or raw["authority_id"] != AUTHORITY_ID
            or raw["workspace_instance_id"] != self.workspace_instance_id
            or raw["domain"] != self.domain
            or raw["key"] != self.key
            or raw["namespace_sha256"] != self.namespace_sha256
        ):
            raise MonotonicAuthorityIntegrityError(
                "authority namespace binding identity/schema mismatch"
            )
        marker_sha = raw["marker_sha256"]
        if not isinstance(marker_sha, str) or _SHA256_RE.fullmatch(marker_sha) is None:
            raise MonotonicAuthorityIntegrityError(
                "invalid authority namespace binding digest"
            )
        unhashed = dict(raw)
        unhashed.pop("marker_sha256")
        if _record_hash(unhashed) != marker_sha:
            raise MonotonicAuthorityIntegrityError(
                "authority namespace binding hash mismatch"
            )

    def _append_record(self, record: AuthorityRecord, index: int) -> None:
        self.records_dir.mkdir(parents=True, exist_ok=True)
        _sync_directory_lineage(self.authority_root, self.records_dir)
        path = self.records_dir / f"{index:020d}-{record.record_sha256}.json"
        try:
            _durable_exclusive_json_create(path, self._payload(record))
            self._ensure_namespace_marker()
        except FileExistsError as exc:
            raise MonotonicAuthorityIntegrityError(
                "authority record slot already exists; possible fork"
            ) from exc
        except MonotonicAuthorityIntegrityError:
            raise
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot durably append monotonic authority record"
            ) from exc

    def _load_history(self) -> _History:
        marker_exists = self.namespace_marker_path.exists()
        if marker_exists:
            self._validate_namespace_marker()
        if not self.records_dir.exists():
            if marker_exists:
                raise MonotonicAuthorityIntegrityError(
                    "authority namespace exists but record history is missing"
                )
            return _History((), 0, None, None, 0)
        if not self.records_dir.is_dir():
            raise MonotonicAuthorityIntegrityError(
                "authority records path must be a directory"
            )
        try:
            entries = list(self.records_dir.iterdir())
        except OSError as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot enumerate monotonic authority records"
            ) from exc

        files: list[tuple[int, str, Path]] = []
        for path in entries:
            match = _RECORD_FILE_RE.fullmatch(path.name)
            if match is None:
                raise MonotonicAuthorityIntegrityError(
                    f"unexpected authority record entry: {path.name!r}"
                )
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise MonotonicAuthorityIntegrityError(
                    "cannot inspect authority record path"
                ) from exc
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise MonotonicAuthorityIntegrityError(
                    "authority records must be single-link regular files"
                )
            files.append((int(match.group("index")), match.group("digest"), path))
        files.sort(key=lambda item: item[0])
        if not files:
            if marker_exists:
                raise MonotonicAuthorityIntegrityError(
                    "authority namespace exists but record history is empty"
                )
            return _History((), 0, None, None, 0)

        records: list[AuthorityRecord] = []
        committed_generation = 0
        committed_state: str | None = None
        pending: AuthorityRecord | None = None
        last_generation = 0
        seen_tx_ids: set[str] = set()

        for expected_index, (index, filename_digest, path) in enumerate(files, start=1):
            if index != expected_index:
                raise MonotonicAuthorityIntegrityError(
                    "authority record sequence has a gap or duplicate"
                )
            record = self._decode_record(path, filename_digest)
            expected_previous = records[-1].record_sha256 if records else None
            if record.previous_record_sha256 != expected_previous:
                raise MonotonicAuthorityIntegrityError(
                    "authority record hash-chain predecessor mismatch"
                )

            if record.phase is AuthorityPhase.PREPARE:
                if pending is not None:
                    raise MonotonicAuthorityIntegrityError(
                        "second PREPARE exists before terminal phase"
                    )
                if record.tx_id in seen_tx_ids:
                    raise MonotonicAuthorityIntegrityError(
                        "authority tx_id was reused for another generation"
                    )
                if record.generation != last_generation + 1:
                    raise MonotonicAuthorityIntegrityError(
                        "PREPARE generation is not the monotonic successor"
                    )
                if (
                    record.previous_committed_generation != committed_generation
                    or record.previous_committed_state_sha256 != committed_state
                ):
                    raise MonotonicAuthorityIntegrityError(
                        "PREPARE previous committed tip does not match history"
                    )
                pending = record
                last_generation = record.generation
                seen_tx_ids.add(record.tx_id)
            else:
                if pending is None:
                    raise MonotonicAuthorityIntegrityError(
                        "terminal authority record has no matching PREPARE"
                    )
                if (
                    record.generation != pending.generation
                    or record.tx_id != pending.tx_id
                    or record.previous_committed_generation
                    != pending.previous_committed_generation
                    or record.previous_committed_state_sha256
                    != pending.previous_committed_state_sha256
                    or record.intended_state_sha256 != pending.intended_state_sha256
                    or record.semantic_binding_sha256 != pending.semantic_binding_sha256
                ):
                    raise MonotonicAuthorityIntegrityError(
                        "terminal authority record does not exactly match PREPARE"
                    )
                if record.phase is AuthorityPhase.COMMIT:
                    committed_generation = record.generation
                    committed_state = record.intended_state_sha256
                pending = None
            records.append(record)

        if not marker_exists:
            self._ensure_namespace_marker()

        return _History(
            tuple(records),
            committed_generation,
            committed_state,
            pending,
            last_generation,
        )

    def _decode_record(self, path: Path, filename_digest: str) -> AuthorityRecord:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise MonotonicAuthorityIntegrityError(
                "cannot read monotonic authority record"
            ) from exc
        try:
            raw = strict_json_loads(text)
        except (TypeError, ValueError) as exc:
            raise MonotonicAuthorityIntegrityError(
                "invalid strict JSON in monotonic authority record"
            ) from exc
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise MonotonicAuthorityIntegrityError(
                "authority record must be a JSON object"
            )
        if frozenset(raw) != _RECORD_KEYS:
            raise MonotonicAuthorityIntegrityError(
                "authority record keys must match schema exactly"
            )
        schema_version = raw["schema_version"]
        if (
            raw["schema"] != AUTHORITY_SCHEMA
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != AUTHORITY_SCHEMA_VERSION
        ):
            raise MonotonicAuthorityIntegrityError(
                "unsupported authority record schema"
            )
        if raw["authority_id"] != AUTHORITY_ID:
            raise MonotonicAuthorityIntegrityError("authority_id mismatch")
        if raw["workspace_instance_id"] != self.workspace_instance_id:
            raise MonotonicAuthorityIntegrityError("workspace_instance_id mismatch")
        if raw["domain"] != self.domain or raw["key"] != self.key:
            raise MonotonicAuthorityIntegrityError("authority domain/key mismatch")

        generation = raw["generation"]
        previous_generation = raw["previous_committed_generation"]
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or isinstance(previous_generation, bool)
            or not isinstance(previous_generation, int)
            or previous_generation < 0
        ):
            raise MonotonicAuthorityIntegrityError(
                "authority generations must be canonical integers"
            )
        try:
            phase = AuthorityPhase(raw["phase"])
        except (TypeError, ValueError) as exc:
            raise MonotonicAuthorityIntegrityError("unsupported authority phase") from exc
        try:
            tx_id = _text("tx_id", raw["tx_id"], max_length=256)
        except MonotonicAuthorityConfigurationError as exc:
            raise MonotonicAuthorityIntegrityError("invalid persisted tx_id") from exc

        digests: dict[str, str | None] = {}
        for name, allow_none in (
            ("previous_record_sha256", True),
            ("previous_committed_state_sha256", True),
            ("intended_state_sha256", False),
            ("semantic_binding_sha256", False),
            ("record_sha256", False),
        ):
            value = raw[name]
            if value is None and allow_none:
                digests[name] = None
            elif isinstance(value, str) and _SHA256_RE.fullmatch(value):
                digests[name] = value
            else:
                raise MonotonicAuthorityIntegrityError(
                    f"invalid persisted digest field {name}"
                )

        record_sha = digests["record_sha256"]
        assert isinstance(record_sha, str)
        if record_sha != filename_digest:
            raise MonotonicAuthorityIntegrityError(
                "authority filename digest does not match record_sha256"
            )
        unhashed = dict(raw)
        unhashed.pop("record_sha256")
        if _record_hash(unhashed) != record_sha:
            raise MonotonicAuthorityIntegrityError("authority record hash mismatch")

        intended = digests["intended_state_sha256"]
        binding = digests["semantic_binding_sha256"]
        assert isinstance(intended, str)
        assert isinstance(binding, str)
        return AuthorityRecord(
            AUTHORITY_ID,
            self.workspace_instance_id,
            self.domain,
            self.key,
            generation,
            tx_id,
            phase,
            digests["previous_record_sha256"],
            previous_generation,
            digests["previous_committed_state_sha256"],
            intended,
            binding,
            record_sha,
        )
