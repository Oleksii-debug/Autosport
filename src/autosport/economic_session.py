"""Rollback-resistant owner economic-session boundary for PAPER risk accounting.

This module defines only the durable financial-session identity required by
session-scoped EconomicGoal limits. It does not calculate turnover/loss, mutate
PaperBook, define UTC-day semantics, or authorize execution. The first slice is
conservative: one active session is established and survives restart; rollover
is deliberately unsupported rather than inferred from unrelated session IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    MonotonicAuthorityRollbackError,
    MonotonicWorkspaceAuthority,
    RecoveryDisposition,
)
from .paper import PaperBook
from .workspace_lock import WorkspaceEconomicLock, _open_read_only_descriptor


_STATE_SCHEMA: Final = "autosport.risk.economic-session"
_STATE_SCHEMA_VERSION: Final = 2
_AUTHORITY_DOMAIN: Final = "portfolio.risk.economic-session"
_STATE_FILE_NAME: Final = "economic_session.json"
_MAX_STATE_BYTES: Final = 64 * 1024
_MAX_PAPERBOOK_BYTES: Final = 64 * 1024 * 1024
_HEX: Final = frozenset("0123456789abcdef")
_PRODUCT_TIME_NS: Final = time.time_ns
_DATETIME_FROMTIMESTAMP: Final = datetime.fromtimestamp
_UUID4 = uuid.uuid4
_WORKSPACE_LOCK_TYPE = WorkspaceEconomicLock
_PAPERBOOK_LOAD = PaperBook.load
_PAPERBOOK_VALIDATE_LOADED_STATE = PaperBook._validate_loaded_state
_ECONOMIC_GOAL_LOAD = EconomicGoalStore.load
_AUTHORITY_RECOVER = MonotonicWorkspaceAuthority.recover
_AUTHORITY_PREPARE = MonotonicWorkspaceAuthority.prepare
_AUTHORITY_COMMIT = MonotonicWorkspaceAuthority.commit

_STATE_KEYS: Final = frozenset(
    {
        "schema",
        "schema_version",
        "workspace_instance_id",
        "transition_id",
        "session_id",
        "goal_id",
        "goal_revision",
        "bankroll_id",
        "currency",
        "goal_contract_sha256",
        "started_at",
        "opening_paperbook_sha256",
        "product_clock_authoritative",
        "predecessor_session_id",
        "predecessor_state_sha256",
        "predecessor_ended_at",
    }
)


class EconomicSessionError(RuntimeError):
    pass


class EconomicSessionIntegrityError(EconomicSessionError):
    pass


class EconomicSessionMismatchError(EconomicSessionError):
    pass



def _is_sha256(value: object, *, _hex=_HEX) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _hex for character in value)
    )



def _parse_instant(
    value: object,
    *,
    _fromisoformat=datetime.fromisoformat,
    _utc=timezone.utc,
) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise EconomicSessionIntegrityError("started_at must be canonical text")
    try:
        parsed = _fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EconomicSessionIntegrityError("started_at must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EconomicSessionIntegrityError("started_at must be timezone-aware")
    return parsed.astimezone(_utc)


@dataclass(frozen=True, slots=True)
class ProductEconomicSession:
    workspace_instance_id: str
    session_id: str
    goal_id: str
    goal_revision: int
    bankroll_id: str
    currency: str
    goal_contract_sha256: str
    started_at: str
    opening_paperbook_sha256: str
    state_sha256: str
    authority_generation: int
    product_clock_authoritative: bool
    predecessor_session_id: str | None
    predecessor_state_sha256: str | None
    predecessor_ended_at: str | None

    def __post_init__(
        self,
        _sha_validator=_is_sha256,
        _instant_parser=_parse_instant,
    ) -> None:
        for field in (
            "workspace_instance_id",
            "session_id",
            "goal_id",
            "bankroll_id",
            "currency",
            "goal_contract_sha256",
            "started_at",
            "opening_paperbook_sha256",
            "state_sha256",
        ):
            value = getattr(self, field)
            if type(value) is not str or not value or value != value.strip():
                raise EconomicSessionIntegrityError(
                    f"{field} must be exact non-empty canonical text"
                )
        if type(self.goal_revision) is not int or self.goal_revision < 1:
            raise EconomicSessionIntegrityError("goal_revision must be a positive exact integer")
        if type(self.authority_generation) is not int or self.authority_generation < 1:
            raise EconomicSessionIntegrityError(
                "authority_generation must be a positive exact integer"
            )
        if type(self.product_clock_authoritative) is not bool:
            raise EconomicSessionIntegrityError(
                "product_clock_authoritative must be an exact boolean"
            )
        for field in ("goal_contract_sha256", "opening_paperbook_sha256", "state_sha256"):
            if not _sha_validator(getattr(self, field)):
                raise EconomicSessionIntegrityError(f"{field} must be canonical SHA-256")
        _instant_parser(self.started_at)
        predecessor_values = (
            self.predecessor_session_id,
            self.predecessor_state_sha256,
            self.predecessor_ended_at,
        )
        if predecessor_values == (None, None, None):
            pass
        elif any(value is None for value in predecessor_values):
            raise EconomicSessionIntegrityError(
                "predecessor session identity must be complete or absent"
            )
        else:
            assert self.predecessor_session_id is not None
            assert self.predecessor_state_sha256 is not None
            assert self.predecessor_ended_at is not None
            if (
                type(self.predecessor_session_id) is not str
                or not self.predecessor_session_id
                or self.predecessor_session_id != self.predecessor_session_id.strip()
            ):
                raise EconomicSessionIntegrityError(
                    "predecessor_session_id must be canonical text"
                )
            if not _sha_validator(self.predecessor_state_sha256):
                raise EconomicSessionIntegrityError(
                    "predecessor_state_sha256 must be canonical SHA-256"
                )
            ended = _instant_parser(self.predecessor_ended_at)
            started = _instant_parser(self.started_at)
            if ended != started:
                raise EconomicSessionIntegrityError(
                    "successor must start exactly at predecessor terminal boundary"
                )

    @property
    def session_turnover_authoritative(self) -> bool:
        return False

    @property
    def session_loss_authoritative(self) -> bool:
        return False

    @property
    def automatic_rollover_supported(self) -> bool:
        return False

    @property
    def real_money_execution_authorized(self) -> bool:
        return False


def _is_transition_id(value: object, *, _hex=_HEX) -> bool:
    return (
        type(value) is str
        and len(value) == 32
        and all(character in _hex for character in value)
    )



def _clock_instant(
    clock: Callable[[], int],
    *,
    _fromtimestamp=_DATETIME_FROMTIMESTAMP,
    _utc=timezone.utc,
) -> str:
    epoch_ns = clock()
    if type(epoch_ns) is not int or epoch_ns < 0:
        raise EconomicSessionIntegrityError("economic-session clock is invalid")
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    try:
        instant = _fromtimestamp(seconds, _utc).replace(
            microsecond=nanoseconds // 1000
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "economic-session clock is outside supported range"
        ) from exc
    return instant.isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(payload: dict[str, object], *, _dumps=json.dumps) -> bytes:
    try:
        return (
            _dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "economic-session state is outside canonical JSON domain"
        ) from exc


def _state_sha256(
    payload: dict[str, object],
    *,
    _sha256=hashlib.sha256,
    _canonical=_canonical_json_bytes,
) -> str:
    return _sha256(_canonical(payload)).hexdigest()


def _semantic_binding(
    payload: dict[str, object],
    *,
    _dumps=json.dumps,
    _sha256=hashlib.sha256,
    _domain=_AUTHORITY_DOMAIN,
) -> str:
    material = _dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(
        _domain.encode("utf-8") + b"\0" + material
    ).hexdigest()


def _tx_id(payload: dict[str, object], *, _validator=_is_transition_id) -> str:
    value = payload["transition_id"]
    if not _validator(value):
        raise EconomicSessionIntegrityError("transition_id is invalid")
    return f"economic-session-{value}"


def _read_regular_bytes(
    path: Path,
    *,
    limit: int,
    label: str,
    _stat=os.stat,
    _fstat=os.fstat,
    _read=os.read,
    _close=os.close,
    _open=_open_read_only_descriptor,
    _is_regular=stat.S_ISREG,
) -> bytes:
    try:
        before = _stat(path, follow_symlinks=False)
    except OSError as exc:
        raise EconomicSessionIntegrityError(f"cannot inspect {label}") from exc
    if not _is_regular(before.st_mode) or before.st_nlink != 1:
        raise EconomicSessionIntegrityError(f"{label} must be a single-link regular file")
    if before.st_size < 0 or before.st_size > limit:
        raise EconomicSessionIntegrityError(f"{label} exceeds bounded size")
    try:
        descriptor = _open(path)
    except OSError as exc:
        raise EconomicSessionIntegrityError(f"cannot safely open {label}") from exc
    try:
        opened = _fstat(descriptor)
        if (
            not _is_regular(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size < 0
            or opened.st_size > limit
        ):
            raise EconomicSessionIntegrityError(f"{label} opened identity is invalid")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = limit + 1 - total
            if remaining <= 0:
                raise EconomicSessionIntegrityError(f"{label} exceeds bounded size")
            chunk = _read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise EconomicSessionIntegrityError(f"{label} exceeds bounded size")
        after = _fstat(descriptor)
        path_after = _stat(path, follow_symlinks=False)
        if (
            opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
            or after.st_dev != path_after.st_dev
            or after.st_ino != path_after.st_ino
            or not _is_regular(path_after.st_mode)
            or path_after.st_nlink != 1
        ):
            raise EconomicSessionIntegrityError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        _close(descriptor)


def _opening_paperbook_sha256(
    path: Path,
    *,
    _read=_read_regular_bytes,
    _load=_PAPERBOOK_LOAD,
    _validate=_PAPERBOOK_VALIDATE_LOADED_STATE,
    _sha256=hashlib.sha256,
    _limit=_MAX_PAPERBOOK_BYTES,
) -> str:
    before = _read(path, limit=_limit, label="canonical PaperBook")
    try:
        book = _load(path)
        _validate(book)
    except (OSError, TypeError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "canonical PaperBook cannot establish economic session"
        ) from exc
    after = _read(path, limit=_limit, label="canonical PaperBook")
    if before != after:
        raise EconomicSessionIntegrityError(
            "canonical PaperBook changed during economic-session issuance"
        )
    return _sha256(after).hexdigest()


def _state_payload(
    *,
    workspace_instance_id: str,
    session_id: str,
    goal_id: str,
    goal_revision: int,
    bankroll_id: str,
    currency: str,
    goal_contract_sha256: str,
    started_at: str,
    opening_paperbook_sha256: str,
    product_clock_authoritative: bool,
    transition_id: str,
    predecessor_session_id: str | None = None,
    predecessor_state_sha256: str | None = None,
    predecessor_ended_at: str | None = None,
    _schema=_STATE_SCHEMA,
    _version=_STATE_SCHEMA_VERSION,
) -> dict[str, object]:
    return {
        "schema": _schema,
        "schema_version": _version,
        "workspace_instance_id": workspace_instance_id,
        "transition_id": transition_id,
        "session_id": session_id,
        "goal_id": goal_id,
        "goal_revision": goal_revision,
        "bankroll_id": bankroll_id,
        "currency": currency,
        "goal_contract_sha256": goal_contract_sha256,
        "started_at": started_at,
        "opening_paperbook_sha256": opening_paperbook_sha256,
        "product_clock_authoritative": product_clock_authoritative,
        "predecessor_session_id": predecessor_session_id,
        "predecessor_state_sha256": predecessor_state_sha256,
        "predecessor_ended_at": predecessor_ended_at,
    }


def _decode_state(
    raw: bytes,
    *,
    workspace_instance_id: str,
    _load_json=strict_json_loads,
    _keys=_STATE_KEYS,
    _schema=_STATE_SCHEMA,
    _version=_STATE_SCHEMA_VERSION,
    _transition_validator=_is_transition_id,
    _sha_validator=_is_sha256,
    _parse=_parse_instant,
    _payload=_state_payload,
    _canonical=_canonical_json_bytes,
) -> dict[str, object]:
    try:
        parsed = _load_json(raw.decode("utf-8"))
    except (UnicodeError, TypeError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "economic-session state is not strict UTF-8 JSON"
        ) from exc
    if type(parsed) is not dict or frozenset(parsed) != _keys:
        raise EconomicSessionIntegrityError("economic-session state schema keys mismatch")
    if (
        parsed["schema"] != _schema
        or parsed["schema_version"] != _version
        or parsed["workspace_instance_id"] != workspace_instance_id
        or not _transition_validator(parsed["transition_id"])
        or type(parsed["session_id"]) is not str
        or not parsed["session_id"]
        or type(parsed["goal_id"]) is not str
        or not parsed["goal_id"]
        or type(parsed["goal_revision"]) is not int
        or parsed["goal_revision"] < 1
        or type(parsed["bankroll_id"]) is not str
        or not parsed["bankroll_id"]
        or type(parsed["currency"]) is not str
        or not parsed["currency"]
        or not _sha_validator(parsed["goal_contract_sha256"])
        or not _sha_validator(parsed["opening_paperbook_sha256"])
        or type(parsed["product_clock_authoritative"]) is not bool
        or (
            (
                parsed["predecessor_session_id"],
                parsed["predecessor_state_sha256"],
                parsed["predecessor_ended_at"],
            )
            != (None, None, None)
            and (
                type(parsed["predecessor_session_id"]) is not str
                or not parsed["predecessor_session_id"]
                or not _sha_validator(parsed["predecessor_state_sha256"])
                or type(parsed["predecessor_ended_at"]) is not str
                or not parsed["predecessor_ended_at"]
            )
        )
    ):
        raise EconomicSessionIntegrityError("economic-session state identity is invalid")
    started_at = _parse(parsed["started_at"])
    if parsed["predecessor_ended_at"] is not None:
        predecessor_ended_at = _parse(parsed["predecessor_ended_at"])
        if predecessor_ended_at != started_at:
            raise EconomicSessionIntegrityError(
                "successor must start exactly at predecessor terminal boundary"
            )
    canonical = _payload(
        workspace_instance_id=workspace_instance_id,
        session_id=str(parsed["session_id"]),
        goal_id=str(parsed["goal_id"]),
        goal_revision=int(parsed["goal_revision"]),
        bankroll_id=str(parsed["bankroll_id"]),
        currency=str(parsed["currency"]),
        goal_contract_sha256=str(parsed["goal_contract_sha256"]),
        started_at=str(parsed["started_at"]),
        opening_paperbook_sha256=str(parsed["opening_paperbook_sha256"]),
        product_clock_authoritative=bool(parsed["product_clock_authoritative"]),
        transition_id=str(parsed["transition_id"]),
        predecessor_session_id=(
            None
            if parsed["predecessor_session_id"] is None
            else str(parsed["predecessor_session_id"])
        ),
        predecessor_state_sha256=(
            None
            if parsed["predecessor_state_sha256"] is None
            else str(parsed["predecessor_state_sha256"])
        ),
        predecessor_ended_at=(
            None
            if parsed["predecessor_ended_at"] is None
            else str(parsed["predecessor_ended_at"])
        ),
    )
    if parsed != canonical or raw != _canonical(canonical):
        raise EconomicSessionIntegrityError("economic-session state is not canonical")
    return canonical


class ProductEconomicSessionStore:
    """Issue/re-resolve one active economic session for one workspace."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
        _test_clock: Callable[[], int] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve(strict=False)
        if not self.workspace.is_absolute():
            raise EconomicSessionIntegrityError("workspace must resolve to an absolute path")
        self.state_path = self.workspace / ".autosport" / _STATE_FILE_NAME
        self.paperbook_path = self.workspace / "paper_book.json"
        self.goal_store = EconomicGoalStore(self.workspace)
        self._clock = _PRODUCT_TIME_NS if _test_clock is None else _test_clock
        if not callable(self._clock):
            raise EconomicSessionIntegrityError("_test_clock must be callable")
        self._product_clock = _test_clock is None
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=_AUTHORITY_DOMAIN,
            key=str(Path(".autosport") / _STATE_FILE_NAME),
            authority_root=authority_root,
        )
        # Capture the exact composition that owns this durable session boundary.
        self._workspace_witness = self.workspace
        self._state_path_witness = self.state_path
        self._paperbook_path_witness = self.paperbook_path
        self._goal_store_witness = self.goal_store
        self._goal_store_path_witness = self.goal_store.path
        self._authority_witness = self._authority
        self._authority_workspace_witness = self._authority.workspace
        self._authority_domain_witness = self._authority.domain
        self._authority_key_witness = self._authority.key
        self._authority_root_witness = self._authority.authority_root
        self._authority_workspace_binding_witness = self._authority.workspace_binding
        self._authority_root_selection_witness = self._authority.authority_root_selection
        self._authority_root_binding_path_witness = self._authority.authority_root_binding_path
        self._authority_workspace_binding_path_witness = self._authority.workspace_binding_path
        self._authority_activation_path_witness = self._authority.authority_root_activation_path
        self._authority_journal_dir_witness = self._authority.journal_dir
        self._authority_records_dir_witness = self._authority.records_dir
        self._authority_namespace_marker_path_witness = self._authority.namespace_marker_path
        self._clock_witness = self._clock
        self._product_clock_witness = self._product_clock
        self._uuid4_witness = _UUID4
        self._opening_paperbook_sha256_witness = _opening_paperbook_sha256
        self._workspace_lock_type_witness = _WORKSPACE_LOCK_TYPE
        self._paperbook_load_witness = _PAPERBOOK_LOAD
        self._paperbook_validate_witness = _PAPERBOOK_VALIDATE_LOADED_STATE
        self._economic_goal_load_witness = _ECONOMIC_GOAL_LOAD
        self._authority_recover_witness = _AUTHORITY_RECOVER
        self._authority_prepare_witness = _AUTHORITY_PREPARE
        self._authority_commit_witness = _AUTHORITY_COMMIT
        self._clock_instant_witness = _clock_instant
        self._state_sha256_witness = _state_sha256
        self._semantic_binding_witness = _semantic_binding
        self._tx_id_witness = _tx_id
        self._canonical_json_bytes_witness = _canonical_json_bytes
        self._read_regular_bytes_witness = _read_regular_bytes
        self._decode_state_witness = _decode_state
        self._state_payload_witness = _state_payload
        self._sha256_witness = hashlib.sha256
        self._lexists_witness = os.path.lexists
        self._provenance_for_witness = provenance_for
        self._atomic_write_json_witness = atomic_write_json
        self._authority_schema_witness = (
            _STATE_SCHEMA,
            _STATE_SCHEMA_VERSION,
            _AUTHORITY_DOMAIN,
            _STATE_FILE_NAME,
            _MAX_STATE_BYTES,
            _MAX_PAPERBOOK_BYTES,
            _STATE_KEYS,
        )

    def _require_configuration_authority(self) -> None:
        if type(self) is not ProductEconomicSessionStore:
            raise EconomicSessionIntegrityError(
                "economic-session store must retain exact ProductEconomicSessionStore authority"
            )
        if (
            self.workspace is not self._workspace_witness
            or self.state_path is not self._state_path_witness
            or self.paperbook_path is not self._paperbook_path_witness
            or self.goal_store is not self._goal_store_witness
            or self.goal_store.path is not self._goal_store_path_witness
            or self._authority is not self._authority_witness
            or self._authority.workspace is not self._authority_workspace_witness
            or self._authority.domain is not self._authority_domain_witness
            or self._authority.key is not self._authority_key_witness
            or self._authority.authority_root is not self._authority_root_witness
            or self._authority.workspace_binding is not self._authority_workspace_binding_witness
            or self._authority.authority_root_selection is not self._authority_root_selection_witness
            or self._authority.authority_root_binding_path is not self._authority_root_binding_path_witness
            or self._authority.workspace_binding_path is not self._authority_workspace_binding_path_witness
            or self._authority.authority_root_activation_path is not self._authority_activation_path_witness
            or self._authority.journal_dir is not self._authority_journal_dir_witness
            or self._authority.records_dir is not self._authority_records_dir_witness
            or self._authority.namespace_marker_path is not self._authority_namespace_marker_path_witness
            or self._clock is not self._clock_witness
            or self._product_clock is not self._product_clock_witness
            or _ECONOMIC_GOAL_LOAD is not self._economic_goal_load_witness
            or _AUTHORITY_RECOVER is not self._authority_recover_witness
            or _AUTHORITY_PREPARE is not self._authority_prepare_witness
            or _AUTHORITY_COMMIT is not self._authority_commit_witness
            or _PAPERBOOK_LOAD is not self._paperbook_load_witness
            or _PAPERBOOK_VALIDATE_LOADED_STATE is not self._paperbook_validate_witness
            or _WORKSPACE_LOCK_TYPE is not self._workspace_lock_type_witness
            or _clock_instant is not self._clock_instant_witness
            or _state_sha256 is not self._state_sha256_witness
            or _semantic_binding is not self._semantic_binding_witness
            or _tx_id is not self._tx_id_witness
            or _canonical_json_bytes is not self._canonical_json_bytes_witness
            or _read_regular_bytes is not self._read_regular_bytes_witness
            or _decode_state is not self._decode_state_witness
            or _state_payload is not self._state_payload_witness
            or hashlib.sha256 is not self._sha256_witness
            or os.path.lexists is not self._lexists_witness
            or provenance_for is not self._provenance_for_witness
            or atomic_write_json is not self._atomic_write_json_witness
            or _UUID4 is not self._uuid4_witness
            or _opening_paperbook_sha256 is not self._opening_paperbook_sha256_witness
            or EconomicGoalStore.load is not self._economic_goal_load_witness
            or MonotonicWorkspaceAuthority.recover is not self._authority_recover_witness
            or MonotonicWorkspaceAuthority.prepare is not self._authority_prepare_witness
            or MonotonicWorkspaceAuthority.commit is not self._authority_commit_witness
            or PaperBook.load is not self._paperbook_load_witness
            or PaperBook._validate_loaded_state is not self._paperbook_validate_witness
            or WorkspaceEconomicLock is not self._workspace_lock_type_witness
            or uuid.uuid4 is not self._uuid4_witness
            or (
                _STATE_SCHEMA,
                _STATE_SCHEMA_VERSION,
                _AUTHORITY_DOMAIN,
                _STATE_FILE_NAME,
                _MAX_STATE_BYTES,
                _MAX_PAPERBOOK_BYTES,
                _STATE_KEYS,
            ) != self._authority_schema_witness
        ):
            raise EconomicSessionIntegrityError(
                "economic-session authority composition changed after construction"
            )

    def current(self) -> ProductEconomicSession:
        self._require_configuration_authority()
        with _WORKSPACE_LOCK_TYPE(self._workspace_witness):
            self._require_configuration_authority()
            goal = _ECONOMIC_GOAL_LOAD(self._goal_store_witness)
            provenance = self._provenance_for_witness(goal)
            if self._lexists_witness(self._state_path_witness):
                raw = self._read_regular_bytes_witness(
                    self._state_path_witness, limit=_MAX_STATE_BYTES, label="economic-session state"
                )
                payload = self._decode_state_witness(
                    raw,
                    workspace_instance_id=self._authority_witness.workspace_instance_id,
                )
                observed = self._sha256_witness(raw).hexdigest()
                self._require_configuration_authority()
                recovery = _AUTHORITY_RECOVER(
                    self._authority_witness,
                    observed_state_sha256=observed,
                    tx_id=self._tx_id_witness(payload),
                    semantic_binding_sha256=self._semantic_binding_witness(payload),
                )
                if recovery.disposition not in {
                    RecoveryDisposition.CURRENT,
                    RecoveryDisposition.ABORTED_PREPARE,
                    RecoveryDisposition.COMMITTED_PREPARE,
                }:
                    raise EconomicSessionIntegrityError(
                        "economic-session state is not a recoverable authority tip"
                    )
                if (
                    payload["goal_id"] != goal.goal_id
                    or payload["goal_revision"] != goal.revision
                    or payload["bankroll_id"] != goal.bankroll_id
                    or payload["currency"] != goal.currency
                    or payload["goal_contract_sha256"] != provenance.contract_sha256
                ):
                    raise EconomicSessionMismatchError(
                        "owner EconomicGoal changed without explicit economic-session transition"
                    )
                return self._evidence(payload, observed, recovery.committed_generation)

            self._require_configuration_authority()
            recovery = _AUTHORITY_RECOVER(
                self._authority_witness,
                observed_state_sha256=None,
            )
            if recovery.disposition not in {
                RecoveryDisposition.PRISTINE,
                RecoveryDisposition.ABORTED_PREPARE,
            }:
                raise MonotonicAuthorityRollbackError(
                    "economic-session state is missing after authority establishment"
                )
            if not self._paperbook_path_witness.exists():
                raise EconomicSessionIntegrityError(
                    "canonical paper_book.json is required before economic-session issuance"
                )
            return self._publish_new(goal, provenance.contract_sha256)

    def transition_to_current_goal(
        self,
        previous: ProductEconomicSession,
    ) -> ProductEconomicSession:
        """Explicitly terminate one session and publish its owner-authorized successor."""
        if type(previous) is not ProductEconomicSession:
            raise EconomicSessionMismatchError(
                "previous must be exact ProductEconomicSession evidence"
            )
        self._require_configuration_authority()
        with _WORKSPACE_LOCK_TYPE(self._workspace_witness):
            self._require_configuration_authority()
            if not self._lexists_witness(self._state_path_witness):
                raise EconomicSessionIntegrityError(
                    "economic-session state is missing; transition cannot mint a predecessor"
                )
            raw = self._read_regular_bytes_witness(
                self._state_path_witness,
                limit=_MAX_STATE_BYTES,
                label="economic-session state",
            )
            payload = self._decode_state_witness(
                raw,
                workspace_instance_id=self._authority_witness.workspace_instance_id,
            )
            observed = self._sha256_witness(raw).hexdigest()
            recovery = _AUTHORITY_RECOVER(
                self._authority_witness,
                observed_state_sha256=observed,
                tx_id=self._tx_id_witness(payload),
                semantic_binding_sha256=self._semantic_binding_witness(payload),
            )
            if recovery.disposition not in {
                RecoveryDisposition.CURRENT,
                RecoveryDisposition.ABORTED_PREPARE,
                RecoveryDisposition.COMMITTED_PREPARE,
            }:
                raise EconomicSessionIntegrityError(
                    "economic-session predecessor is not a recoverable authority tip"
                )
            current_evidence = self._evidence(
                payload, observed, recovery.committed_generation
            )
            if previous != current_evidence:
                raise EconomicSessionMismatchError(
                    "explicit transition predecessor does not match current durable session"
                )

            goal = _ECONOMIC_GOAL_LOAD(self._goal_store_witness)
            provenance = self._provenance_for_witness(goal)
            if (
                payload["goal_id"] == goal.goal_id
                and payload["goal_revision"] == goal.revision
                and payload["bankroll_id"] == goal.bankroll_id
                and payload["currency"] == goal.currency
                and payload["goal_contract_sha256"] == provenance.contract_sha256
            ):
                raise EconomicSessionMismatchError(
                    "owner EconomicGoal is unchanged; explicit transition is not authorized"
                )
            if not self._paperbook_path_witness.exists():
                raise EconomicSessionIntegrityError(
                    "canonical paper_book.json is required before economic-session transition"
                )
            terminal_at = self._clock_instant_witness(self._clock)
            if _parse_instant(terminal_at) < _parse_instant(previous.started_at):
                raise EconomicSessionIntegrityError(
                    "economic-session transition clock precedes predecessor start"
                )
            opening_sha256 = self._opening_paperbook_sha256_witness(
                self._paperbook_path_witness
            )
            successor = self._state_payload_witness(
                workspace_instance_id=self._authority_witness.workspace_instance_id,
                session_id=_UUID4().hex,
                goal_id=goal.goal_id,
                goal_revision=goal.revision,
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                goal_contract_sha256=provenance.contract_sha256,
                started_at=terminal_at,
                opening_paperbook_sha256=opening_sha256,
                product_clock_authoritative=self._product_clock,
                transition_id=_UUID4().hex,
                predecessor_session_id=previous.session_id,
                predecessor_state_sha256=previous.state_sha256,
                predecessor_ended_at=terminal_at,
            )
            intended = self._state_sha256_witness(successor)
            binding = self._semantic_binding_witness(successor)
            tx_id = self._tx_id_witness(successor)
            self._require_configuration_authority()
            _AUTHORITY_PREPARE(
                self._authority_witness,
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            self._atomic_write_json_witness(self._state_path_witness, successor)
            successor_raw = self._read_regular_bytes_witness(
                self._state_path_witness,
                limit=_MAX_STATE_BYTES,
                label="economic-session state",
            )
            successor_observed = self._sha256_witness(successor_raw).hexdigest()
            if successor_observed != intended:
                raise EconomicSessionIntegrityError(
                    "published successor session does not match prepared digest"
                )
            self._require_configuration_authority()
            committed = _AUTHORITY_COMMIT(
                self._authority_witness,
                tx_id=tx_id,
                observed_state_sha256=successor_observed,
                semantic_binding_sha256=binding,
            )
            return self._evidence(
                successor,
                successor_observed,
                committed.generation,
            )

    def require_current(self, candidate: ProductEconomicSession) -> ProductEconomicSession:
        if type(candidate) is not ProductEconomicSession:
            raise EconomicSessionMismatchError(
                "candidate must be exact ProductEconomicSession evidence"
            )
        current = self.current()
        if not current.product_clock_authoritative:
            raise EconomicSessionIntegrityError(
                "synthetic clock cannot mint positive economic-session authority"
            )
        if candidate != current:
            raise EconomicSessionMismatchError(
                "economic-session evidence does not match current durable authority"
            )
        return current

    def _publish_new(self, goal, goal_contract_sha256: str) -> ProductEconomicSession:
        self._require_configuration_authority()
        opening_sha256 = self._opening_paperbook_sha256_witness(self._paperbook_path_witness)
        self._require_configuration_authority()
        payload = self._state_payload_witness(
            workspace_instance_id=self._authority_witness.workspace_instance_id,
            session_id=_UUID4().hex,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            goal_contract_sha256=goal_contract_sha256,
            started_at=self._clock_instant_witness(self._clock),
            opening_paperbook_sha256=opening_sha256,
            product_clock_authoritative=self._product_clock,
            transition_id=_UUID4().hex,
        )
        intended = self._state_sha256_witness(payload)
        binding = self._semantic_binding_witness(payload)
        tx_id = self._tx_id_witness(payload)
        self._require_configuration_authority()
        _AUTHORITY_PREPARE(
            self._authority_witness,
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        self._atomic_write_json_witness(self._state_path_witness, payload)
        raw = self._read_regular_bytes_witness(
            self._state_path_witness, limit=_MAX_STATE_BYTES, label="economic-session state"
        )
        observed = self._sha256_witness(raw).hexdigest()
        if observed != intended:
            raise EconomicSessionIntegrityError(
                "published economic-session state does not match prepared digest"
            )
        self._require_configuration_authority()
        committed = _AUTHORITY_COMMIT(
            self._authority_witness,
            tx_id=tx_id,
            observed_state_sha256=observed,
            semantic_binding_sha256=binding,
        )
        return self._evidence(payload, observed, committed.generation)

    @staticmethod
    def _evidence(
        payload: dict[str, object],
        state_sha256: str,
        generation: int,
    ) -> ProductEconomicSession:
        return ProductEconomicSession(
            workspace_instance_id=str(payload["workspace_instance_id"]),
            session_id=str(payload["session_id"]),
            goal_id=str(payload["goal_id"]),
            goal_revision=int(payload["goal_revision"]),
            bankroll_id=str(payload["bankroll_id"]),
            currency=str(payload["currency"]),
            goal_contract_sha256=str(payload["goal_contract_sha256"]),
            started_at=str(payload["started_at"]),
            opening_paperbook_sha256=str(payload["opening_paperbook_sha256"]),
            state_sha256=state_sha256,
            authority_generation=generation,
            product_clock_authoritative=bool(payload["product_clock_authoritative"]),
            predecessor_session_id=(
                None
                if payload["predecessor_session_id"] is None
                else str(payload["predecessor_session_id"])
            ),
            predecessor_state_sha256=(
                None
                if payload["predecessor_state_sha256"] is None
                else str(payload["predecessor_state_sha256"])
            ),
            predecessor_ended_at=(
                None
                if payload["predecessor_ended_at"] is None
                else str(payload["predecessor_ended_at"])
            ),
        )


__all__ = [
    "EconomicSessionError",
    "EconomicSessionIntegrityError",
    "EconomicSessionMismatchError",
    "ProductEconomicSession",
    "ProductEconomicSessionStore",
]
