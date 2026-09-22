"""Betfair BET-level settlement revision authority.

Consumes existing provider-issued readback and durable execution-ledger authority.
It never creates execution rejection, legal outcome-space authority, or canonical
campaign-cost authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from .betfair_account_readonly import (
    ADAPTER_ID as BETFAIR_ADAPTER_ID,
    ADAPTER_VERSION as BETFAIR_ADAPTER_VERSION,
    BetfairClearedOrderObservation,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionLedgerError,
    ExternalReceiptIdentity,
    RealExecutionLedger,
)

_SCHEMA = "autosport.betfair_settlement_revision"
_SCHEMA_VERSION = 1
_ALLOWED_STATUSES = frozenset({"SETTLED", "VOIDED", "LAPSED", "CANCELLED"})
_MONOTONIC_DOMAIN = "betfair-settlement-revisions"
_MONOTONIC_BINDING_SCHEMA = "autosport.betfair_settlement_revision.monotonic_binding"
_MONOTONIC_BINDING_VERSION = 1
_MONOTONIC_STATE_SCHEMA = "autosport.betfair_settlement_revision.monotonic_state"
_MONOTONIC_STATE_VERSION = 1


class BetfairSettlementRevisionError(RuntimeError):
    pass


class BetfairSettlementNotObserved(BetfairSettlementRevisionError):
    pass


class BetfairSettlementBusyError(BetfairSettlementRevisionError):
    pass


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairSettlementRevisionError(f"{field} must be non-empty canonical text")
    return value


def _time(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairSettlementRevisionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairSettlementRevisionError(f"{field} must be timezone-aware")
    return parsed


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
        raise BetfairSettlementRevisionError(f"{field} must be lowercase SHA-256")
    return raw


def _dec(value: object, field: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value) if type(value) is str else None
    except InvalidOperation as exc:
        raise BetfairSettlementRevisionError(f"{field} must be finite Decimal") from exc
    if parsed is None or not parsed.is_finite():
        raise BetfairSettlementRevisionError(f"{field} must be finite Decimal")
    return parsed


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairSettlementRevisionError("settlement evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BetfairSettlementRevisionError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise BetfairSettlementRevisionError(f"non-finite JSON value {value!r}")


@dataclass(frozen=True, slots=True)
class BetfairSettlementRevision:
    revision_id: str
    previous_revision_id: str | None
    revision_number: int
    bookmaker_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    plan_id: str
    action_id: str
    attempt_id: str
    external_bet_id: str
    event_id: str
    market_id: str
    selection_id: str
    side: str
    provider_status: str
    placed_date: str
    settled_date: str
    price_requested: Decimal
    price_matched: Decimal
    size_settled: Decimal
    provider_profit: Decimal
    available_at: str
    source_payload_sha256: str
    capture_evidence_sha256: str
    content_sha256: str

    def __post_init__(self) -> None:
        _sha(self.revision_id, "revision_id")
        if self.previous_revision_id is not None:
            _sha(self.previous_revision_id, "previous_revision_id")
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise BetfairSettlementRevisionError("revision_number must be positive int")
        for field in (
            "bookmaker_id", "account_id", "adapter_id", "adapter_version", "plan_id",
            "action_id", "attempt_id", "external_bet_id", "event_id", "market_id",
            "selection_id", "side",
        ):
            _text(getattr(self, field), field)
        if self.adapter_id != BETFAIR_ADAPTER_ID or self.adapter_version != BETFAIR_ADAPTER_VERSION:
            raise BetfairSettlementRevisionError("settlement adapter identity mismatch")
        if self.provider_status not in _ALLOWED_STATUSES:
            raise BetfairSettlementRevisionError("provider_status is not a BET cleared status")
        if _time(self.available_at, "available_at") < _time(self.settled_date, "settled_date"):
            raise BetfairSettlementRevisionError("settlement cannot be available before settled_date")
        _time(self.placed_date, "placed_date")
        for field in ("price_requested", "price_matched", "size_settled", "provider_profit"):
            _dec(getattr(self, field), field)
        _sha(self.source_payload_sha256, "source_payload_sha256")
        _sha(self.capture_evidence_sha256, "capture_evidence_sha256")
        _sha(self.content_sha256, "content_sha256")
        if self.content_sha256 != _digest(self.semantic_payload()):
            raise BetfairSettlementRevisionError("settlement content digest mismatch")
        expected = _digest({
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "previous_revision_id": self.previous_revision_id,
            "revision_number": self.revision_number,
            "content_sha256": self.content_sha256,
        })
        if self.revision_id != expected:
            raise BetfairSettlementRevisionError("settlement revision identity mismatch")

    @property
    def permanent_final(self) -> bool:
        return False

    @property
    def terminal_space_exact(self) -> bool:
        return False

    def semantic_payload(self) -> dict[str, str]:
        return {
            "bookmaker_id": self.bookmaker_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "plan_id": self.plan_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "external_bet_id": self.external_bet_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "side": self.side,
            "provider_status": self.provider_status,
            "placed_date": self.placed_date,
            "settled_date": self.settled_date,
            "price_requested": format(self.price_requested, "f"),
            "price_matched": format(self.price_matched, "f"),
            "size_settled": format(self.size_settled, "f"),
            "provider_profit": format(self.provider_profit, "f"),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "previous_revision_id": self.previous_revision_id,
            "revision_number": self.revision_number,
            **self.semantic_payload(),
            "available_at": self.available_at,
            "source_payload_sha256": self.source_payload_sha256,
            "capture_evidence_sha256": self.capture_evidence_sha256,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "BetfairSettlementRevision":
        if type(raw) is not dict:
            raise BetfairSettlementRevisionError("revision must be JSON object")
        required = {
            "revision_id", "previous_revision_id", "revision_number", "bookmaker_id",
            "account_id", "adapter_id", "adapter_version", "plan_id", "action_id",
            "attempt_id", "external_bet_id", "event_id", "market_id", "selection_id",
            "side", "provider_status", "placed_date", "settled_date", "price_requested",
            "price_matched", "size_settled", "provider_profit", "available_at",
            "source_payload_sha256", "capture_evidence_sha256", "content_sha256",
        }
        if set(raw) != required:
            raise BetfairSettlementRevisionError("revision fields are not canonical")
        values = dict(raw)
        for field in ("price_requested", "price_matched", "size_settled", "provider_profit"):
            values[field] = _dec(values[field], field)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class SettlementIngestResult:
    revision: BetfairSettlementRevision
    created: bool


class BetfairSettlementRevisionStore:
    """Restart-safe append-only projection; one writer at a time, fail closed."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._writer_lock_path = self.path.with_name(self.path.name + ".writer.lock")
        self._thread_lock = RLock()
        self._revisions: list[BetfairSettlementRevision] = []
        self._by_bet: dict[tuple[str, str, str], list[BetfairSettlementRevision]] = {}
        self._last_record_sha256: str | None = None
        # Startup reload may resolve a pending monotonic PREPARE to ABORT/COMMIT.
        # Serialize that recovery with the same OS writer lock used by ingest so
        # a concurrent opener cannot terminate another process's active append.
        with self._writer_lock():
            self._reload()

    @staticmethod
    def _monotonic_key(path: Path) -> str:
        name = os.path.normcase(path.name)
        return "settlement-" + sha256(name.encode("utf-8")).hexdigest()

    def _monotonic_authority(self) -> MonotonicWorkspaceAuthority:
        absolute = Path(os.path.abspath(os.fspath(self.path)))
        return MonotonicWorkspaceAuthority(
            workspace=absolute.parent,
            domain=_MONOTONIC_DOMAIN,
            key=self._monotonic_key(absolute),
        )

    def _monotonic_binding(self) -> str:
        return _digest({
            "schema": _MONOTONIC_BINDING_SCHEMA,
            "schema_version": _MONOTONIC_BINDING_VERSION,
            "journal_schema": _SCHEMA,
            "journal_schema_version": _SCHEMA_VERSION,
            "journal_key": self._monotonic_key(
                Path(os.path.abspath(os.fspath(self.path)))
            ),
        })

    def _monotonic_state_digest_for(
        self,
        *,
        record_count: int,
        tail_record_sha256: str | None,
        tail_revision_id: str | None,
    ) -> str | None:
        if record_count == 0:
            if tail_record_sha256 is not None or tail_revision_id is not None:
                raise BetfairSettlementRevisionError(
                    "empty settlement journal cannot have a monotonic tail"
                )
            return None
        if (
            type(record_count) is not int
            or record_count < 1
            or tail_record_sha256 is None
            or tail_revision_id is None
        ):
            raise BetfairSettlementRevisionError(
                "settlement monotonic state is internally inconsistent"
            )
        return _digest({
            "schema": _MONOTONIC_STATE_SCHEMA,
            "schema_version": _MONOTONIC_STATE_VERSION,
            "journal_key": self._monotonic_key(
                Path(os.path.abspath(os.fspath(self.path)))
            ),
            "record_count": record_count,
            "tail_record_sha256": _sha(
                tail_record_sha256, "tail_record_sha256"
            ),
            "tail_revision_id": _sha(tail_revision_id, "tail_revision_id"),
        })

    def _monotonic_state_digest(self) -> str | None:
        tail_revision_id = (
            None if not self._revisions else self._revisions[-1].revision_id
        )
        return self._monotonic_state_digest_for(
            record_count=len(self._revisions),
            tail_record_sha256=self._last_record_sha256,
            tail_revision_id=tail_revision_id,
        )

    def _ensure_monotonic_current(self, *, adopt_if_missing: bool) -> None:
        observed = self._monotonic_state_digest()
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            if not history:
                if observed is None:
                    return
                if not adopt_if_missing:
                    raise BetfairSettlementRevisionError(
                        "settlement journal lacks independent monotonic authority"
                    )
                tx_id = _digest({
                    "operation": "ADOPT_VALIDATED_SETTLEMENT_BASELINE",
                    "state_sha256": observed,
                    "semantic_binding_sha256": binding,
                })
                authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                return

            latest = history[-1]
            if (
                latest.phase is AuthorityPhase.PREPARE
                and observed == latest.intended_state_sha256
            ):
                authority.recover(
                    observed_state_sha256=observed,
                    tx_id=latest.tx_id,
                    semantic_binding_sha256=binding,
                )
            else:
                authority.recover(observed_state_sha256=observed)
        except MonotonicWorkspaceAuthorityError as exc:
            raise BetfairSettlementRevisionError(
                "settlement monotonic authority rejected journal state"
            ) from exc

    def _record_unsigned(
        self,
        revision: BetfairSettlementRevision,
    ) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "previous_record_sha256": self._last_record_sha256,
            "revision": revision.to_dict(),
        }

    def _prepare_monotonic_append(
        self,
        revision: BetfairSettlementRevision,
    ) -> None:
        unsigned = self._record_unsigned(revision)
        record_hash = _digest(unsigned)
        observed = self._monotonic_state_digest()
        intended = self._monotonic_state_digest_for(
            record_count=len(self._revisions) + 1,
            tail_record_sha256=record_hash,
            tail_revision_id=revision.revision_id,
        )
        assert intended is not None
        binding = self._monotonic_binding()
        try:
            authority = self._monotonic_authority()
            history = authority.read_history()
            authority_tip_sha256 = (
                None if not history else history[-1].record_sha256
            )
            tx_id = _digest({
                "operation": "APPEND_SETTLEMENT_REVISION",
                "observed_state_sha256": observed,
                "intended_state_sha256": intended,
                "revision_id": revision.revision_id,
                "semantic_binding_sha256": binding,
                "authority_tip_sha256": authority_tip_sha256,
            })
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise BetfairSettlementRevisionError(
                "settlement monotonic authority rejected append"
            ) from exc

    @property
    def revisions(self) -> tuple[BetfairSettlementRevision, ...]:
        with self._thread_lock:
            return tuple(self._revisions)

    def current(self, bookmaker_id: str, account_id: str, external_bet_id: str) -> BetfairSettlementRevision | None:
        key = (_text(bookmaker_id, "bookmaker_id"), _text(account_id, "account_id"), _text(external_bet_id, "external_bet_id"))
        with self._thread_lock:
            chain = self._by_bet.get(key, ())
            return chain[-1] if chain else None

    def as_of(self, bookmaker_id: str, account_id: str, external_bet_id: str, cutoff: str) -> BetfairSettlementRevision | None:
        key = (_text(bookmaker_id, "bookmaker_id"), _text(account_id, "account_id"), _text(external_bet_id, "external_bet_id"))
        limit = _time(cutoff, "cutoff")
        with self._thread_lock:
            visible = [item for item in self._by_bet.get(key, ()) if _time(item.available_at, "available_at") <= limit]
            return visible[-1] if visible else None

    def ingest(
        self,
        ledger: RealExecutionLedger,
        *,
        plan_id: str,
        attempt_id: str,
        action: ExecutionAction,
        capture: BetfairExecutionReadbackEnvelope,
    ) -> SettlementIngestResult:
        if not isinstance(ledger, RealExecutionLedger):
            raise BetfairSettlementRevisionError("ledger must be RealExecutionLedger")
        plan = _text(plan_id, "plan_id")
        attempt = _text(attempt_id, "attempt_id")
        if not isinstance(action, ExecutionAction):
            raise BetfairSettlementRevisionError("action must be ExecutionAction")
        if not isinstance(capture, BetfairExecutionReadbackEnvelope):
            raise BetfairSettlementRevisionError("capture must be BetfairExecutionReadbackEnvelope")
        try:
            capture.assert_authoritative()
        except BetfairReadOnlyError as exc:
            raise BetfairSettlementRevisionError("settlement capture is not canonical adapter-issued evidence") from exc
        order = _match_order(action, capture)
        _require_attempt_receipt_owner(
            ledger, plan_id=plan, attempt_id=attempt, action=action,
            capture=capture, external_bet_id=order.bet_id,
        )
        payload = _semantic_payload(action, capture, order, plan, attempt)
        content_sha = _digest(payload)
        key = (action.bookmaker_id, action.account_id, order.bet_id)

        with self._thread_lock, self._writer_lock():
            self._reload()
            chain = self._by_bet.get(key, [])
            previous = chain[-1] if chain else None
            if previous is not None:
                if (previous.plan_id, previous.action_id, previous.attempt_id) != (plan, action.action_id, attempt):
                    raise BetfairSettlementRevisionError("external bet is bound to a different execution attempt")
                if previous.content_sha256 == content_sha:
                    return SettlementIngestResult(previous, False)
                if any(item.content_sha256 == content_sha for item in chain[:-1]):
                    raise BetfairSettlementRevisionError(
                        "settlement evidence regressed to a superseded semantic revision"
                    )
                if _time(order.settled_date, "settled_date") < _time(
                    previous.settled_date,
                    "previous settled_date",
                ):
                    raise BetfairSettlementRevisionError(
                        "changed settlement content regressed provider settled_date"
                    )
                if _time(capture.observed_at, "observed_at") <= _time(previous.available_at, "available_at"):
                    raise BetfairSettlementRevisionError("changed settlement evidence is not causally later")
            revision_number = 1 if previous is None else previous.revision_number + 1
            previous_id = None if previous is None else previous.revision_id
            revision_id = _digest({
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "previous_revision_id": previous_id,
                "revision_number": revision_number,
                "content_sha256": content_sha,
            })
            revision = BetfairSettlementRevision(
                revision_id=revision_id,
                previous_revision_id=previous_id,
                revision_number=revision_number,
                bookmaker_id=action.bookmaker_id,
                account_id=action.account_id,
                adapter_id=capture.adapter_id,
                adapter_version=capture.adapter_version,
                plan_id=plan,
                action_id=action.action_id,
                attempt_id=attempt,
                external_bet_id=order.bet_id,
                event_id=action.event_id,
                market_id=action.market_id,
                selection_id=action.selection_id,
                side=action.side,
                provider_status=order.bet_status,
                placed_date=order.placed_date,
                settled_date=order.settled_date,
                price_requested=order.price_requested,
                price_matched=order.price_matched,
                size_settled=order.size_settled,
                provider_profit=order.profit,
                available_at=capture.observed_at,
                source_payload_sha256=order.evidence.source_payload_sha256,
                capture_evidence_sha256=capture.evidence_sha256,
                content_sha256=content_sha,
            )
            self._prepare_monotonic_append(revision)
            self._append(revision)
            # Re-read the durable journal before exposing the new revision. This
            # also resolves PREPARE -> COMMIT only when the exact intended local
            # state is present, so crashes on either side of the append remain
            # deterministic and stale complete prefixes fail closed.
            self._reload()
            persisted = self._by_bet.get(key, [])[-1]
            if persisted.revision_id != revision.revision_id:
                raise BetfairSettlementRevisionError(
                    "persisted settlement revision differs from prepared append"
                )
            return SettlementIngestResult(persisted, True)

    @contextmanager
    def _writer_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._writer_lock_path.open("a+b")
        try:
            # Keep a stable lock file across restarts. The byte is only a lock
            # target on Windows; ownership lives in the OS advisory lock, so a
            # process crash cannot leave a permanently "owned" sentinel.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
            except OSError as exc:
                raise BetfairSettlementBusyError(
                    "settlement writer lock is held by another process"
                ) from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _reload(self) -> None:
        previous_revisions = self._revisions
        previous_by_bet = self._by_bet
        previous_last_record_sha256 = self._last_record_sha256
        self._revisions = []
        self._by_bet = {}
        self._last_record_sha256 = None
        try:
            previous_hash: str | None = None
            if self.path.exists():
                try:
                    with self.path.open("r", encoding="utf-8") as handle:
                        for line_no, line in enumerate(handle, 1):
                            if not line.endswith("\n"):
                                raise BetfairSettlementRevisionError(
                                    "settlement log has partial final record"
                                )
                            record = json.loads(
                                line,
                                object_pairs_hook=_pairs,
                                parse_constant=_nonfinite,
                            )
                            if type(record) is not dict or set(record) != {
                                "schema",
                                "schema_version",
                                "previous_record_sha256",
                                "revision",
                                "record_sha256",
                            }:
                                raise BetfairSettlementRevisionError(
                                    f"settlement record {line_no} schema invalid"
                                )
                            if (
                                record["schema"] != _SCHEMA
                                or record["schema_version"] != _SCHEMA_VERSION
                            ):
                                raise BetfairSettlementRevisionError(
                                    f"settlement record {line_no} schema unsupported"
                                )
                            if record["previous_record_sha256"] != previous_hash:
                                raise BetfairSettlementRevisionError(
                                    f"settlement record {line_no} hash chain broken"
                                )
                            supplied = _sha(
                                record["record_sha256"], "record_sha256"
                            )
                            unsigned = dict(record)
                            unsigned.pop("record_sha256")
                            if supplied != _digest(unsigned):
                                raise BetfairSettlementRevisionError(
                                    f"settlement record {line_no} digest mismatch"
                                )
                            self._accept(
                                BetfairSettlementRevision.from_dict(
                                    record["revision"]
                                )
                            )
                            previous_hash = supplied
                except json.JSONDecodeError as exc:
                    raise BetfairSettlementRevisionError(
                        "settlement log is invalid JSON"
                    ) from exc
            self._last_record_sha256 = previous_hash
            self._ensure_monotonic_current(adopt_if_missing=True)
        except Exception:
            # A failed reload must never publish a validated prefix as current truth.
            # Restore the previously complete in-memory authority atomically.
            self._revisions = previous_revisions
            self._by_bet = previous_by_bet
            self._last_record_sha256 = previous_last_record_sha256
            raise

    def _accept(self, revision: BetfairSettlementRevision) -> None:
        key = (revision.bookmaker_id, revision.account_id, revision.external_bet_id)
        chain = self._by_bet.setdefault(key, [])
        if chain:
            prior = chain[-1]
            if (revision.plan_id, revision.action_id, revision.attempt_id) != (prior.plan_id, prior.action_id, prior.attempt_id):
                raise BetfairSettlementRevisionError("settlement revision changes execution attempt")
            if revision.previous_revision_id != prior.revision_id or revision.revision_number != prior.revision_number + 1:
                raise BetfairSettlementRevisionError("settlement revision lineage is not contiguous")
            if _time(revision.available_at, "available_at") <= _time(prior.available_at, "available_at"):
                raise BetfairSettlementRevisionError("settlement revision time is not increasing")
            if revision.content_sha256 == prior.content_sha256:
                raise BetfairSettlementRevisionError("duplicate semantic settlement revision persisted")
        elif revision.previous_revision_id is not None or revision.revision_number != 1:
            raise BetfairSettlementRevisionError("first settlement revision has invalid predecessor")
        chain.append(revision)
        self._revisions.append(revision)

    def _append(self, revision: BetfairSettlementRevision) -> None:
        unsigned = self._record_unsigned(revision)
        record_hash = _digest(unsigned)
        line = (_canonical({**unsigned, "record_sha256": record_hash}) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            fd = os.open(self.path.parent, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        # Do not publish the new tail in memory before an exact durable re-read
        # has reconciled the independent monotonic PREPARE.
        return None


def _match_order(action: ExecutionAction, capture: BetfairExecutionReadbackEnvelope) -> BetfairClearedOrderObservation:
    if capture.adapter_id != BETFAIR_ADAPTER_ID or capture.adapter_version != BETFAIR_ADAPTER_VERSION:
        raise BetfairSettlementRevisionError("settlement capture adapter mismatch")
    if (capture.venue_id, capture.account_id, capture.action_id, capture.market_id) != (
        action.bookmaker_id, action.account_id, action.action_id, action.market_id
    ):
        raise BetfairSettlementRevisionError("settlement capture execution identity mismatch")
    if capture.market_event.event_id != action.event_id:
        raise BetfairSettlementRevisionError("settlement capture event mismatch")
    expected_ref = capture.provider_order_ref or action.action_id
    matches: list[BetfairClearedOrderObservation] = []
    for status, pages in capture.cleared_pages_by_status:
        if status not in _ALLOWED_STATUSES:
            raise BetfairSettlementRevisionError("unsupported cleared status")
        for page in pages:
            for order in page.orders:
                if order.bet_status != status:
                    raise BetfairSettlementRevisionError("cleared row status partition mismatch")
                if order.customer_order_ref == expected_ref:
                    if (
                        order.market_id != action.market_id
                        or str(order.selection_id) != action.selection_id
                        or order.side != action.side
                    ):
                        raise BetfairSettlementRevisionError(
                            "cleared row provider order reference identity mismatch"
                        )
                    if order.event_id is not None and order.event_id != action.event_id:
                        raise BetfairSettlementRevisionError("cleared row event mismatch")
                    matches.append(order)
                    continue
                if order.market_id != action.market_id or str(order.selection_id) != action.selection_id or order.side != action.side:
                    continue
                if order.event_id is not None and order.event_id != action.event_id:
                    raise BetfairSettlementRevisionError("cleared row event mismatch")
                if order.customer_order_ref is not None and order.customer_order_ref != expected_ref:
                    raise BetfairSettlementRevisionError("cleared row customer_order_ref mismatch")
                matches.append(order)
    if not matches:
        raise BetfairSettlementNotObserved("no matching BET-level cleared settlement")
    if len(matches) != 1:
        raise BetfairSettlementRevisionError("conflicting BET-level cleared settlement rows")
    return matches[0]


def _require_attempt_receipt_owner(
    ledger: RealExecutionLedger,
    *,
    plan_id: str,
    attempt_id: str,
    action: ExecutionAction,
    capture: BetfairExecutionReadbackEnvelope,
    external_bet_id: str,
) -> None:
    try:
        events = ledger._events()
        _, durable_action = ledger._action_payload(
            events, plan_id, action.action_id
        )
        if durable_action != action.to_dict():
            raise BetfairSettlementRevisionError(
                "settlement action differs from durable execution plan"
            )
        saga = ledger.saga(plan_id)
        state = saga.attempts.get(attempt_id)
        if state not in {AttemptState.ACCEPTED, AttemptState.PARTIAL}:
            raise BetfairSettlementRevisionError("settlement requires durable ACCEPTED/PARTIAL attempt")
        if saga.attempt_action_ids.get(attempt_id) != action.action_id:
            raise BetfairSettlementRevisionError("settlement attempt does not own action")
        receipt = ExternalReceiptIdentity(action.bookmaker_id, action.account_id, external_bet_id)
        if saga.receipts.get(receipt) != attempt_id:
            raise BetfairSettlementRevisionError("external bet is not durable receipt owner for attempt")
        provider_ref = ledger.provider_order_reference(attempt_id=attempt_id, provider_id=action.bookmaker_id)
    except (KeyError, ExecutionLedgerError) as exc:
        raise BetfairSettlementRevisionError("execution-attempt authority unavailable") from exc
    if provider_ref is None or capture.provider_order_ref != provider_ref:
        raise BetfairSettlementRevisionError("capture does not bind durable provider order reference")


def _semantic_payload(
    action: ExecutionAction,
    capture: BetfairExecutionReadbackEnvelope,
    order: BetfairClearedOrderObservation,
    plan_id: str,
    attempt_id: str,
) -> dict[str, str]:
    return {
        "bookmaker_id": action.bookmaker_id,
        "account_id": action.account_id,
        "adapter_id": capture.adapter_id,
        "adapter_version": capture.adapter_version,
        "plan_id": plan_id,
        "action_id": action.action_id,
        "attempt_id": attempt_id,
        "external_bet_id": order.bet_id,
        "event_id": action.event_id,
        "market_id": action.market_id,
        "selection_id": action.selection_id,
        "side": action.side,
        "provider_status": order.bet_status,
        "placed_date": order.placed_date,
        "settled_date": order.settled_date,
        "price_requested": format(order.price_requested, "f"),
        "price_matched": format(order.price_matched, "f"),
        "size_settled": format(order.size_settled, "f"),
        "provider_profit": format(order.profit, "f"),
    }