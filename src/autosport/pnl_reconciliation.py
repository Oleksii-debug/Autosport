from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from decimal import Context, Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_CEILING, Underflow, localcontext
from pathlib import Path

from .real_execution_ledger import RealExecutionLedger
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
)

_SCHEMA = 1
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 4096
_MAX_DECIMAL_TEXT = 96
_MAX_DECIMAL_DIGITS = 50
_MIN_EXP = -18
_MAX_EXP = 18
_SIDES = frozenset({"BACK", "LAY"})
_CTX = Context(prec=160, Emin=-999999, Emax=999999)
for _signal in (InvalidOperation, DivisionByZero, Overflow, Underflow):
    _CTX.traps[_signal] = True


@dataclass(frozen=True, slots=True)
class PnLReconciliationSnapshot:
    currency: str
    accepted_order_count: int
    settlement_revision_count: int
    realized_pnl: Decimal
    open_back_stake: Decimal
    open_lay_liability: Decimal
    mark_to_market_unrealized_pnl: None = None
    execution_provenance_bound: bool = False
    execution_evidence_verified: bool = field(default=False, init=False)
    positive_authority_verified: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class _Order:
    source: str
    order_id: str
    side: str
    stake: Decimal
    odds: Decimal
    execution_provenance_bound: bool = False
    revision: int = 0
    settled_stake: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")


class PnLReconciliationJournal:
    """Durable derived P&L projection; never executes or settles provider orders.

    Accepted execution economics may be re-resolved from canonical RealExecutionLedger
    truth. Caller-supplied scalar records remain derived/unverified. Settlement revisions
    are still caller-derived in this lineage, so snapshots can never claim positive
    risk/readiness authority. Exact event-id replay is idempotent. Mark-to-market P&L
    is withheld because this journal has no live exit-price authority.
    """

    def __init__(self, path: str | Path, *, currency: str, liability_quantum: str = "0.01") -> None:
        self.path = Path(path)
        self.currency = _currency(currency)
        self._quantum_text = _decimal_text(liability_quantum, "liability_quantum")
        self._quantum = _decimal(self._quantum_text, "liability_quantum")
        if self._quantum <= 0 or self._quantum.as_tuple().digits != (1,) or self._quantum.as_tuple().exponent > 0:
            raise ValueError("liability_quantum must be a positive power of ten")
        self._orders: dict[tuple[str, str], _Order] = {}
        self._events: dict[str, bytes] = {}
        self._revision_count = 0
        self._faulted = False
        self._loaded_file_identity: tuple[int, int, int, int] | None = None
        with WorkspaceEconomicLock(self.path.parent):
            self._reload()

    @property
    def faulted(self) -> bool:
        return self._faulted

    def record_accepted_order(
        self, *, event_id: str, provider_source_id: str, provider_order_id: str,
        side: str, accepted_stake: str, accepted_odds: str,
    ) -> PnLReconciliationSnapshot:
        """Persist caller-supplied derived economics without positive authority."""

        event = {
            "schema_version": _SCHEMA, "event_type": "accepted_order",
            "event_id": _text(event_id, "event_id"), "currency": self.currency,
            "liability_quantum": self._quantum_text,
            "provider_source_id": _text(provider_source_id, "provider_source_id"),
            "provider_order_id": _text(provider_order_id, "provider_order_id"),
            "side": _side(side),
            "accepted_stake": _decimal_text(accepted_stake, "accepted_stake"),
            "accepted_odds": _decimal_text(accepted_odds, "accepted_odds"),
        }
        return self._record(event)

    def record_accepted_execution(
        self,
        *,
        event_id: str,
        execution_ledger: RealExecutionLedger,
        bookmaker_id: str,
        account_id: str,
        external_receipt_id: str,
    ) -> PnLReconciliationSnapshot:
        """Persist accepted economics re-resolved from canonical execution truth."""

        if type(execution_ledger) is not RealExecutionLedger:
            raise TypeError("execution_ledger must be canonical RealExecutionLedger")
        verified = RealExecutionLedger.accepted_execution_by_receipt(
            execution_ledger,
            bookmaker_id=bookmaker_id,
            account_id=account_id,
            external_receipt_id=external_receipt_id,
        )
        event = {
            "schema_version": _SCHEMA,
            "event_type": "accepted_execution",
            "event_id": _text(event_id, "event_id"),
            "currency": self.currency,
            "liability_quantum": self._quantum_text,
            "provider_source_id": verified.bookmaker_id,
            "provider_order_id": verified.external_receipt_id,
            "side": verified.side,
            "accepted_stake": _decimal_text(
                verified.accepted_stake, "accepted_stake"
            ),
            "accepted_odds": _decimal_text(
                verified.accepted_odds, "accepted_odds"
            ),
            "execution_account_id": verified.account_id,
            "execution_action_id": verified.action_id,
            "execution_attempt_id": verified.attempt_id,
            "execution_ledger_sha256": _sha256_text(
                verified.ledger_sha256, "execution_ledger_sha256"
            ),
        }
        return self._record(event)

    def record_settlement_revision(
        self, *, event_id: str, provider_source_id: str, provider_order_id: str,
        revision_seq: int, cumulative_settled_stake: str, cumulative_realized_pnl: str,
    ) -> PnLReconciliationSnapshot:
        if type(revision_seq) is not int or revision_seq < 1:
            raise ValueError("revision_seq must be a positive integer")
        event = {
            "schema_version": _SCHEMA, "event_type": "settlement_revision",
            "event_id": _text(event_id, "event_id"), "currency": self.currency,
            "liability_quantum": self._quantum_text,
            "provider_source_id": _text(provider_source_id, "provider_source_id"),
            "provider_order_id": _text(provider_order_id, "provider_order_id"),
            "revision_seq": revision_seq,
            "cumulative_settled_stake": _decimal_text(cumulative_settled_stake, "cumulative_settled_stake"),
            "cumulative_realized_pnl": _decimal_text(cumulative_realized_pnl, "cumulative_realized_pnl"),
        }
        return self._record(event)

    def _record(self, event: dict[str, object]) -> PnLReconciliationSnapshot:
        if self._faulted:
            raise RuntimeError("reconciliation journal is faulted; reopen before retry")
        encoded = _encode(event)
        lock = WorkspaceEconomicLock(self.path.parent)
        try:
            with lock:
                self._reload()
                event_id = event["event_id"]
                previous = self._events.get(event_id)
                if previous is not None:
                    if previous != encoded:
                        raise ValueError("event_id already exists with different reconciliation payload")
                    return self._snapshot_loaded()
                self._validate(event)
                self._append(encoded)
                self._apply(event, encoded)
        except ValueError:
            raise
        except WorkspaceEconomicLockBusyError:
            raise
        except WorkspaceEconomicLockError:
            self._faulted = True
            raise
        except BaseException:
            self._faulted = True
            raise
        return self._snapshot_loaded()

    def snapshot(self) -> PnLReconciliationSnapshot:
        # A faulted writer exposes only its last proven in-memory publication.
        # Reopen is the recovery boundary for a possibly durable ambiguous tail.
        if self._faulted:
            return self._snapshot_loaded()
        with WorkspaceEconomicLock(self.path.parent):
            self._reload()
            return self._snapshot_loaded()

    def _snapshot_loaded(self) -> PnLReconciliationSnapshot:
        with localcontext(_CTX):
            realized = Decimal("0")
            back = Decimal("0")
            lay = Decimal("0")
            for order in self._orders.values():
                realized += order.realized_pnl
                remaining = order.stake - order.settled_stake
                if remaining <= 0:
                    continue
                if order.side == "BACK":
                    back += remaining
                else:
                    lay += (remaining * (order.odds - Decimal("1"))).quantize(
                        self._quantum, rounding=ROUND_CEILING
                    )
        execution_provenance_bound = bool(self._orders) and all(
            order.execution_provenance_bound for order in self._orders.values()
        )
        return PnLReconciliationSnapshot(
            self.currency,
            len(self._orders),
            self._revision_count,
            realized,
            back,
            lay,
            None,
            execution_provenance_bound,
        )

    def _reload(self) -> None:
        self._orders = {}
        self._events = {}
        self._revision_count = 0
        raw, self._loaded_file_identity = _read_regular_journal(self.path)
        if raw is None or not raw:
            return
        if not raw.endswith(b"\n"):
            raise ValueError("reconciliation journal has a truncated final record")
        for number, line in enumerate(raw.splitlines(), 1):
            if not line or len(line) > _MAX_LINE_BYTES:
                raise ValueError(f"reconciliation journal line {number} has invalid bounded length")
            try:
                event = json.loads(line.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ValueError(f"reconciliation journal line {number} is invalid") from exc
            if type(event) is not dict:
                raise ValueError(f"reconciliation journal line {number} must be an object")
            encoded = _encode(event)
            if encoded != line:
                raise ValueError(f"reconciliation journal line {number} is not canonical JSON")
            event_id = event.get("event_id")
            if type(event_id) is str and event_id in self._events:
                if self._events[event_id] != encoded:
                    raise ValueError("reconciliation journal reuses event_id with different payload")
                continue
            self._validate(event)
            self._apply(event, encoded)

    def _validate(self, event: dict[str, object]) -> None:
        common = {"schema_version", "event_type", "event_id", "currency", "liability_quantum", "provider_source_id", "provider_order_id"}
        kind = event.get("event_type")
        if kind == "accepted_order":
            required = common | {"side", "accepted_stake", "accepted_odds"}
        elif kind == "accepted_execution":
            required = common | {
                "side",
                "accepted_stake",
                "accepted_odds",
                "execution_account_id",
                "execution_action_id",
                "execution_attempt_id",
                "execution_ledger_sha256",
            }
        elif kind == "settlement_revision":
            required = common | {"revision_seq", "cumulative_settled_stake", "cumulative_realized_pnl"}
        else:
            raise ValueError("unsupported reconciliation journal event_type")
        if set(event) != required or event.get("schema_version") != _SCHEMA:
            raise ValueError("reconciliation journal event fields/schema are not canonical")
        _text(event["event_id"], "event_id")
        source = _text(event["provider_source_id"], "provider_source_id")
        order_id = _text(event["provider_order_id"], "provider_order_id")
        if event["currency"] != self.currency:
            raise ValueError("reconciliation journal currency does not match ledger scope")
        if event["liability_quantum"] != self._quantum_text:
            raise ValueError("reconciliation journal liability_quantum does not match ledger scope")
        key = (source, order_id)
        if kind in {"accepted_order", "accepted_execution"}:
            stake = _decimal(event["accepted_stake"], "accepted_stake")
            odds = _decimal(event["accepted_odds"], "accepted_odds")
            _side(event["side"])
            if stake <= 0 or odds <= 1:
                raise ValueError("accepted order requires positive stake and odds greater than one")
            if kind == "accepted_execution":
                _text(event["execution_account_id"], "execution_account_id")
                _text(event["execution_action_id"], "execution_action_id")
                _text(event["execution_attempt_id"], "execution_attempt_id")
                _sha256_text(
                    event["execution_ledger_sha256"],
                    "execution_ledger_sha256",
                )
            if key in self._orders:
                raise ValueError("provider order already has accepted-order evidence")
            return
        seq = event["revision_seq"]
        if type(seq) is not int or seq < 1:
            raise ValueError("revision_seq must be a positive integer")
        settled = _decimal(event["cumulative_settled_stake"], "cumulative_settled_stake")
        _decimal(event["cumulative_realized_pnl"], "cumulative_realized_pnl")
        order = self._orders.get(key)
        if order is None:
            raise ValueError("settlement revision references unknown provider order")
        if seq != order.revision + 1:
            raise ValueError("settlement revision sequence must advance by exactly one")
        if settled < order.settled_stake:
            raise ValueError("cumulative_settled_stake must not move backward")
        if settled > order.stake:
            raise ValueError("cumulative_settled_stake exceeds accepted_stake")

    def _apply(self, event: dict[str, object], encoded: bytes) -> None:
        key = (event["provider_source_id"], event["provider_order_id"])
        if event["event_type"] in {"accepted_order", "accepted_execution"}:
            self._orders[key] = _Order(
                key[0],
                key[1],
                event["side"],
                _decimal(event["accepted_stake"], "accepted_stake"),
                _decimal(event["accepted_odds"], "accepted_odds"),
                event["event_type"] == "accepted_execution",
            )
        else:
            current = self._orders[key]
            self._orders[key] = replace(
                current, revision=event["revision_seq"],
                settled_stake=_decimal(event["cumulative_settled_stake"], "cumulative_settled_stake"),
                realized_pnl=_decimal(event["cumulative_realized_pnl"], "cumulative_realized_pnl"),
            )
            self._revision_count += 1
        self._events[event["event_id"]] = encoded

    def _append(self, encoded: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        base_flags = (
            os.O_WRONLY
            | os.O_APPEND
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        created = False
        expected_identity = self._loaded_file_identity
        try:
            if expected_identity is None:
                try:
                    fd = os.open(
                        self.path,
                        base_flags | no_follow | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError as exc:
                    raise ValueError(
                        "reconciliation journal path appeared after replay"
                    ) from exc
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("reconciliation journal must be a regular file")
                created = True
            else:
                try:
                    fd = _open_existing_regular_journal(
                        self.path,
                        base_flags,
                        expected_identity=expected_identity,
                    )
                except FileNotFoundError as exc:
                    raise ValueError(
                        "reconciliation journal path disappeared after replay"
                    ) from exc
            payload = encoded + b"\n"
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                if written <= 0:
                    raise OSError("reconciliation journal append made no progress")
                offset += written
            os.fsync(fd)
            if created:
                _fsync_parent_directory(self.path.parent)
            _require_path_references_open_journal(self.path, fd)
        except BaseException:
            self._faulted = True
            raise
        finally:
            if fd is not None:
                os.close(fd)


def _lstat_regular_journal(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("reconciliation journal path must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("reconciliation journal path must be a regular file")
    return info


def _journal_file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _require_path_references_open_journal(path: Path, fd: int) -> None:
    current = _lstat_regular_journal(path)
    if current is None:
        raise ValueError("reconciliation journal path disappeared during append")
    opened = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError("reconciliation journal path changed during append")


def _open_existing_regular_journal(
    path: Path,
    flags: int,
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> int:
    before = _lstat_regular_journal(path)
    if before is None:
        raise FileNotFoundError(path)
    before_identity = _journal_file_identity(before)
    if expected_identity is not None and before_identity != expected_identity:
        raise ValueError("reconciliation journal changed after replay")
    fd = os.open(
        path,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("reconciliation journal path must be a regular file")
        opened_identity = _journal_file_identity(opened)
        if before_identity != opened_identity:
            raise ValueError("reconciliation journal path changed during open")
        if expected_identity is not None and opened_identity != expected_identity:
            raise ValueError("reconciliation journal changed after replay")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_regular_journal(
    path: Path,
) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    try:
        fd = _open_existing_regular_journal(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except FileNotFoundError:
        return None, None
    try:
        before = os.fstat(fd)
        if before.st_size > _MAX_FILE_BYTES:
            raise ValueError("reconciliation journal exceeds bounded replay size")
        raw = bytearray()
        while True:
            remaining = _MAX_FILE_BYTES + 1 - len(raw)
            if remaining <= 0:
                raise ValueError("reconciliation journal exceeds bounded replay size")
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > _MAX_FILE_BYTES:
                raise ValueError("reconciliation journal exceeds bounded replay size")
        after = os.fstat(fd)
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or len(raw) != after.st_size
        ):
            raise ValueError("reconciliation journal changed during bounded replay")
        return bytes(raw), _journal_file_identity(after)
    finally:
        os.close(fd)


def _fsync_parent_directory(path: Path) -> None:
    """Persist a newly-created journal pathname on POSIX before publication."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _sha256_text(value: object, label: str) -> str:
    value = _text(value, label)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be lowercase SHA-256 hex")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty trimmed string")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    return value


def _currency(value: object) -> str:
    value = _text(value, "currency")
    if len(value) != 3 or not value.isascii() or not value.isalpha() or value != value.upper():
        raise ValueError("currency must be a three-letter uppercase ASCII code")
    return value


def _side(value: object) -> str:
    if type(value) is not str or value not in _SIDES:
        raise ValueError("side must be BACK or LAY")
    return value


def _decimal_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{label} must be serialized as a string")
    if not value or value.strip() != value or len(value) > _MAX_DECIMAL_TEXT:
        raise ValueError(f"{label} must be a bounded trimmed decimal string")
    _decimal(value, label)
    return value


def _decimal(value: object, label: str) -> Decimal:
    if type(value) is not str:
        raise ValueError(f"{label} must be serialized as a string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal string")
    parts = parsed.as_tuple()
    if len(parts.digits) > _MAX_DECIMAL_DIGITS or not (_MIN_EXP <= int(parts.exponent) <= _MAX_EXP):
        raise ValueError(f"{label} decimal representation exceeds supported bounds")
    return parsed


def _encode(event: dict[str, object]) -> bytes:
    try:
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("reconciliation journal event is not canonical JSON") from exc
    if len(encoded) > _MAX_LINE_BYTES:
        raise ValueError("reconciliation journal event exceeds bounded line size")
    return encoded


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate reconciliation journal JSON key: {key}")
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise ValueError(f"non-finite reconciliation journal JSON constant: {value}")
