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
_STATE_SCHEMA_VERSION: Final = 1
_AUTHORITY_DOMAIN: Final = "portfolio.risk.economic-session"
_STATE_FILE_NAME: Final = "economic_session.json"
_MAX_STATE_BYTES: Final = 64 * 1024
_MAX_PAPERBOOK_BYTES: Final = 64 * 1024 * 1024
_HEX: Final = frozenset("0123456789abcdef")
_PRODUCT_TIME_NS: Final = time.time_ns
_DATETIME_FROMTIMESTAMP: Final = datetime.fromtimestamp
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
    }
)


class EconomicSessionError(RuntimeError):
    pass


class EconomicSessionIntegrityError(EconomicSessionError):
    pass


class EconomicSessionMismatchError(EconomicSessionError):
    pass


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

    def __post_init__(self) -> None:
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
            if not _is_sha256(getattr(self, field)):
                raise EconomicSessionIntegrityError(f"{field} must be canonical SHA-256")
        _parse_instant(self.started_at)

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


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _is_transition_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 32
        and all(character in _HEX for character in value)
    )


def _parse_instant(value: object) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise EconomicSessionIntegrityError("started_at must be canonical text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EconomicSessionIntegrityError("started_at must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EconomicSessionIntegrityError("started_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _clock_instant(clock: Callable[[], int]) -> str:
    epoch_ns = clock()
    if type(epoch_ns) is not int or epoch_ns < 0:
        raise EconomicSessionIntegrityError("economic-session clock is invalid")
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    try:
        instant = _DATETIME_FROMTIMESTAMP(seconds, timezone.utc).replace(
            microsecond=nanoseconds // 1000
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "economic-session clock is outside supported range"
        ) from exc
    return instant.isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    try:
        return (
            json.dumps(
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


def _state_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _semantic_binding(payload: dict[str, object]) -> str:
    material = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(
        _AUTHORITY_DOMAIN.encode("utf-8") + b"\0" + material
    ).hexdigest()


def _tx_id(payload: dict[str, object]) -> str:
    value = payload["transition_id"]
    if not _is_transition_id(value):
        raise EconomicSessionIntegrityError("transition_id is invalid")
    return f"economic-session-{value}"


def _read_regular_bytes(path: Path, *, limit: int, label: str) -> bytes:
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise EconomicSessionIntegrityError(f"cannot inspect {label}") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise EconomicSessionIntegrityError(f"{label} must be a single-link regular file")
    if before.st_size < 0 or before.st_size > limit:
        raise EconomicSessionIntegrityError(f"{label} exceeds bounded size")
    try:
        descriptor = _open_read_only_descriptor(path)
    except OSError as exc:
        raise EconomicSessionIntegrityError(f"cannot safely open {label}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
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
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise EconomicSessionIntegrityError(f"{label} exceeds bounded size")
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (
            opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
            or after.st_dev != path_after.st_dev
            or after.st_ino != path_after.st_ino
            or not stat.S_ISREG(path_after.st_mode)
            or path_after.st_nlink != 1
        ):
            raise EconomicSessionIntegrityError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _opening_paperbook_sha256(path: Path) -> str:
    before = _read_regular_bytes(path, limit=_MAX_PAPERBOOK_BYTES, label="canonical PaperBook")
    try:
        book = PaperBook.load(path)
        PaperBook._validate_loaded_state(book)
    except (OSError, TypeError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "canonical PaperBook cannot establish economic session"
        ) from exc
    after = _read_regular_bytes(path, limit=_MAX_PAPERBOOK_BYTES, label="canonical PaperBook")
    if before != after:
        raise EconomicSessionIntegrityError(
            "canonical PaperBook changed during economic-session issuance"
        )
    return hashlib.sha256(after).hexdigest()


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
) -> dict[str, object]:
    return {
        "schema": _STATE_SCHEMA,
        "schema_version": _STATE_SCHEMA_VERSION,
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
    }


def _decode_state(raw: bytes, *, workspace_instance_id: str) -> dict[str, object]:
    try:
        parsed = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeError, TypeError, ValueError) as exc:
        raise EconomicSessionIntegrityError(
            "economic-session state is not strict UTF-8 JSON"
        ) from exc
    if type(parsed) is not dict or frozenset(parsed) != _STATE_KEYS:
        raise EconomicSessionIntegrityError("economic-session state schema keys mismatch")
    if (
        parsed["schema"] != _STATE_SCHEMA
        or parsed["schema_version"] != _STATE_SCHEMA_VERSION
        or parsed["workspace_instance_id"] != workspace_instance_id
        or not _is_transition_id(parsed["transition_id"])
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
        or not _is_sha256(parsed["goal_contract_sha256"])
        or not _is_sha256(parsed["opening_paperbook_sha256"])
        or type(parsed["product_clock_authoritative"]) is not bool
    ):
        raise EconomicSessionIntegrityError("economic-session state identity is invalid")
    _parse_instant(parsed["started_at"])
    canonical = _state_payload(
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
    )
    if parsed != canonical or raw != _canonical_json_bytes(canonical):
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

    def current(self) -> ProductEconomicSession:
        with WorkspaceEconomicLock(self.workspace):
            goal = self.goal_store.load()
            provenance = provenance_for(goal)
            if os.path.lexists(self.state_path):
                raw = _read_regular_bytes(
                    self.state_path, limit=_MAX_STATE_BYTES, label="economic-session state"
                )
                payload = _decode_state(
                    raw,
                    workspace_instance_id=self._authority.workspace_instance_id,
                )
                observed = hashlib.sha256(raw).hexdigest()
                recovery = self._authority.recover(
                    observed_state_sha256=observed,
                    tx_id=_tx_id(payload),
                    semantic_binding_sha256=_semantic_binding(payload),
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

            recovery = self._authority.recover(observed_state_sha256=None)
            if recovery.disposition not in {
                RecoveryDisposition.PRISTINE,
                RecoveryDisposition.ABORTED_PREPARE,
            }:
                raise MonotonicAuthorityRollbackError(
                    "economic-session state is missing after authority establishment"
                )
            if not self.paperbook_path.exists():
                raise EconomicSessionIntegrityError(
                    "canonical paper_book.json is required before economic-session issuance"
                )
            return self._publish_new(goal, provenance.contract_sha256)

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
        opening_sha256 = _opening_paperbook_sha256(self.paperbook_path)
        payload = _state_payload(
            workspace_instance_id=self._authority.workspace_instance_id,
            session_id=uuid.uuid4().hex,
            goal_id=goal.goal_id,
            goal_revision=goal.revision,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            goal_contract_sha256=goal_contract_sha256,
            started_at=_clock_instant(self._clock),
            opening_paperbook_sha256=opening_sha256,
            product_clock_authoritative=self._product_clock,
            transition_id=uuid.uuid4().hex,
        )
        intended = _state_sha256(payload)
        binding = _semantic_binding(payload)
        tx_id = _tx_id(payload)
        self._authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=intended,
            semantic_binding_sha256=binding,
        )
        atomic_write_json(self.state_path, payload)
        raw = _read_regular_bytes(
            self.state_path, limit=_MAX_STATE_BYTES, label="economic-session state"
        )
        observed = hashlib.sha256(raw).hexdigest()
        if observed != intended:
            raise EconomicSessionIntegrityError(
                "published economic-session state does not match prepared digest"
            )
        committed = self._authority.commit(
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
        )


__all__ = [
    "EconomicSessionError",
    "EconomicSessionIntegrityError",
    "EconomicSessionMismatchError",
    "ProductEconomicSession",
    "ProductEconomicSessionStore",
]
