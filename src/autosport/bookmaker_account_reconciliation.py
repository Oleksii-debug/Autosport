"""Durable read-only bookmaker account snapshot reconciliation.

This module journals complete point-in-time BookmakerAccountSnapshot observations and derives
conservative cross-snapshot state. It never performs provider I/O, moves money, settles bets,
or infers economic causes from balance arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from threading import RLock

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)
from .json_integrity import strict_json_loads


class AccountReconciliationError(RuntimeError):
    """Base error for durable account reconciliation."""


class AccountReconciliationIntegrityError(AccountReconciliationError):
    """Raised when persisted or incoming account history is contradictory/corrupt."""


class AccountSnapshotStaleError(AccountReconciliationError):
    """Raised when an older non-idempotent snapshot tries to supersede a checkpoint."""


class ReconciledPositionState(str, Enum):
    OPEN = "OPEN"
    SETTLED = "SETTLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ReconciledPosition:
    external_position_id: str
    state: ReconciledPositionState
    last_observation_id: str
    last_observed_at: str


@dataclass(frozen=True, slots=True)
class UnexplainedBalanceDelta:
    """Provider-native available-balance movement with deliberately unknown cause."""

    currency: str
    amount: Decimal
    previous_observation_id: str
    current_observation_id: str
    previous_observed_at: str
    current_observed_at: str


@dataclass(frozen=True, slots=True)
class ReconciledAccountState:
    snapshot_id: str
    venue_id: str
    account_id: str
    adapter_id: str
    profile_id: str
    observed_at: str
    observed_capabilities: frozenset[BookmakerCapability]
    positions: tuple[ReconciledPosition, ...]
    latest_balance_observation: BookmakerBalanceObservation | None
    unexplained_balance_delta: UnexplainedBalanceDelta | None

    def position_state(self, external_position_id: str) -> ReconciledPositionState | None:
        for position in self.positions:
            if position.external_position_id == external_position_id:
                return position.state
        return None


_LOCAL_WRITE_LOCK = RLock()


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCAL_WRITE_LOCK:
        try:
            with lock_path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AccountReconciliationIntegrityError(
                "unable to acquire or release account reconciliation write lock"
            ) from exc


def _time(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AccountReconciliationIntegrityError(
            f"{field} must be non-empty trimmed ISO-8601 text"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AccountReconciliationIntegrityError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AccountReconciliationIntegrityError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountReconciliationIntegrityError("money must be a finite Decimal")
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _balance_to_dict(value: BookmakerBalanceObservation) -> dict[str, object]:
    return {
        "venue_id": value.venue_id,
        "account_id": value.account_id,
        "adapter_id": value.adapter_id,
        "observation_id": value.observation_id,
        "currency": value.currency,
        "available_balance": _decimal_text(value.available_balance),
        "observed_at": value.observed_at,
        "source_payload_sha256": value.source_payload_sha256,
        "total_balance": _optional_decimal_text(value.total_balance),
        "total_balance_source_ref": value.total_balance_source_ref,
        "exposure": _optional_decimal_text(value.exposure),
        "retained_commission": _optional_decimal_text(value.retained_commission),
        "exposure_limit": _optional_decimal_text(value.exposure_limit),
    }


def _position_to_dict(value: BookmakerPositionObservation) -> dict[str, object]:
    return {
        "venue_id": value.venue_id,
        "account_id": value.account_id,
        "adapter_id": value.adapter_id,
        "observation_id": value.observation_id,
        "external_position_id": value.external_position_id,
        "state": value.state.value,
        "currency": value.currency,
        "observed_at": value.observed_at,
        "source_payload_sha256": value.source_payload_sha256,
        "provider_amount": _decimal_text(value.provider_amount),
        "provider_amount_semantics": value.provider_amount_semantics,
        "provider_side": value.provider_side,
        "decimal_odds": _optional_decimal_text(value.decimal_odds),
        "gross_return": _optional_decimal_text(value.gross_return),
        "external_receipt_id": value.external_receipt_id,
    }


def snapshot_to_canonical_dict(snapshot: BookmakerAccountSnapshot) -> dict[str, object]:
    if not isinstance(snapshot, BookmakerAccountSnapshot):
        raise AccountReconciliationIntegrityError(
            "snapshot must be a BookmakerAccountSnapshot"
        )
    return {
        "profile": snapshot.profile.to_canonical_dict(),
        "observed_capabilities": sorted(
            capability.value for capability in snapshot.observed_capabilities
        ),
        "observed_at": snapshot.observed_at,
        "balance": None if snapshot.balance is None else _balance_to_dict(snapshot.balance),
        "open_positions": [
            _position_to_dict(item)
            for item in sorted(
                snapshot.open_positions,
                key=lambda item: (item.external_position_id, item.observation_id),
            )
        ],
        "settled_positions": [
            _position_to_dict(item)
            for item in sorted(
                snapshot.settled_positions,
                key=lambda item: (item.external_position_id, item.observation_id),
            )
        ],
    }


def snapshot_fingerprint(snapshot: BookmakerAccountSnapshot) -> str:
    encoded = json.dumps(
        snapshot_to_canonical_dict(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _exact_keys(raw: object, expected: set[str], field: str) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != expected:
        raise AccountReconciliationIntegrityError(f"{field} schema is invalid")
    return raw


def _optional_decimal(raw: object, field: str) -> Decimal | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise AccountReconciliationIntegrityError(f"{field} must be Decimal text or null")
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise AccountReconciliationIntegrityError(
            f"{field} must be Decimal text"
        ) from exc
    if not value.is_finite():
        raise AccountReconciliationIntegrityError(f"{field} must be finite")
    return value


def _decode_profile(raw: object) -> BookmakerCapabilityProfile:
    payload = _exact_keys(
        raw,
        {
            "account_id",
            "adapter_id",
            "adapter_version",
            "facts",
            "observed_at",
            "profile_version",
            "source_payload_sha256",
            "source_ref",
            "venue_id",
        },
        "profile",
    )
    facts_raw = payload["facts"]
    if not isinstance(facts_raw, list):
        raise AccountReconciliationIntegrityError("profile.facts must be a list")
    facts: list[BookmakerCapabilityFact] = []
    try:
        for item in facts_raw:
            fact = _exact_keys(item, {"capability", "state"}, "profile.fact")
            facts.append(
                BookmakerCapabilityFact(
                    capability=BookmakerCapability(fact["capability"]),
                    state=BookmakerCapabilityState(fact["state"]),
                )
            )
        return BookmakerCapabilityProfile(
            venue_id=payload["venue_id"],
            account_id=payload["account_id"],
            adapter_id=payload["adapter_id"],
            adapter_version=payload["adapter_version"],
            profile_version=payload["profile_version"],
            facts=tuple(facts),
            observed_at=payload["observed_at"],
            source_ref=payload["source_ref"],
            source_payload_sha256=payload["source_payload_sha256"],
        )
    except (TypeError, ValueError) as exc:
        raise AccountReconciliationIntegrityError(
            "profile payload is invalid"
        ) from exc


def _decode_balance(raw: object) -> BookmakerBalanceObservation | None:
    if raw is None:
        return None
    payload = _exact_keys(
        raw,
        {
            "venue_id",
            "account_id",
            "adapter_id",
            "observation_id",
            "currency",
            "available_balance",
            "observed_at",
            "source_payload_sha256",
            "total_balance",
            "total_balance_source_ref",
            "exposure",
            "retained_commission",
            "exposure_limit",
        },
        "balance",
    )
    try:
        available = _optional_decimal(payload["available_balance"], "available_balance")
        if available is None:
            raise AccountReconciliationIntegrityError("available_balance cannot be null")
        return BookmakerBalanceObservation(
            venue_id=payload["venue_id"],
            account_id=payload["account_id"],
            adapter_id=payload["adapter_id"],
            observation_id=payload["observation_id"],
            currency=payload["currency"],
            available_balance=available,
            observed_at=payload["observed_at"],
            source_payload_sha256=payload["source_payload_sha256"],
            total_balance=_optional_decimal(payload["total_balance"], "total_balance"),
            total_balance_source_ref=payload["total_balance_source_ref"],
            exposure=_optional_decimal(payload["exposure"], "exposure"),
            retained_commission=_optional_decimal(
                payload["retained_commission"], "retained_commission"
            ),
            exposure_limit=_optional_decimal(
                payload["exposure_limit"], "exposure_limit"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise AccountReconciliationIntegrityError(
            "balance payload is invalid"
        ) from exc


def _decode_position(raw: object) -> BookmakerPositionObservation:
    payload = _exact_keys(
        raw,
        {
            "venue_id",
            "account_id",
            "adapter_id",
            "observation_id",
            "external_position_id",
            "state",
            "currency",
            "observed_at",
            "source_payload_sha256",
            "provider_amount",
            "provider_amount_semantics",
            "provider_side",
            "decimal_odds",
            "gross_return",
            "external_receipt_id",
        },
        "position",
    )
    try:
        amount = _optional_decimal(payload["provider_amount"], "provider_amount")
        if amount is None:
            raise AccountReconciliationIntegrityError("provider_amount cannot be null")
        return BookmakerPositionObservation(
            venue_id=payload["venue_id"],
            account_id=payload["account_id"],
            adapter_id=payload["adapter_id"],
            observation_id=payload["observation_id"],
            external_position_id=payload["external_position_id"],
            state=BookmakerPositionState(payload["state"]),
            currency=payload["currency"],
            observed_at=payload["observed_at"],
            source_payload_sha256=payload["source_payload_sha256"],
            provider_amount=amount,
            provider_amount_semantics=payload["provider_amount_semantics"],
            provider_side=payload["provider_side"],
            decimal_odds=_optional_decimal(payload["decimal_odds"], "decimal_odds"),
            gross_return=_optional_decimal(payload["gross_return"], "gross_return"),
            external_receipt_id=payload["external_receipt_id"],
        )
    except (TypeError, ValueError) as exc:
        raise AccountReconciliationIntegrityError(
            "position payload is invalid"
        ) from exc


def _decode_snapshot(raw: object) -> BookmakerAccountSnapshot:
    payload = _exact_keys(
        raw,
        {
            "profile",
            "observed_capabilities",
            "observed_at",
            "balance",
            "open_positions",
            "settled_positions",
        },
        "snapshot",
    )
    capabilities_raw = payload["observed_capabilities"]
    open_raw = payload["open_positions"]
    settled_raw = payload["settled_positions"]
    if not isinstance(capabilities_raw, list):
        raise AccountReconciliationIntegrityError(
            "observed_capabilities must be a list"
        )
    if not isinstance(open_raw, list) or not isinstance(settled_raw, list):
        raise AccountReconciliationIntegrityError("position collections must be lists")
    try:
        snapshot = BookmakerAccountSnapshot(
            profile=_decode_profile(payload["profile"]),
            observed_capabilities=frozenset(
                BookmakerCapability(item) for item in capabilities_raw
            ),
            observed_at=payload["observed_at"],
            balance=_decode_balance(payload["balance"]),
            open_positions=tuple(_decode_position(item) for item in open_raw),
            settled_positions=tuple(_decode_position(item) for item in settled_raw),
        )
    except (TypeError, ValueError) as exc:
        raise AccountReconciliationIntegrityError(
            "snapshot payload is invalid"
        ) from exc
    if snapshot_to_canonical_dict(snapshot) != payload:
        raise AccountReconciliationIntegrityError(
            "snapshot payload is not canonical or contains semantic aliases"
        )
    return snapshot


class BookmakerAccountReconciliationStore:
    """Durable whole-account observation history and conservative derived state."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append_snapshot(self, snapshot: BookmakerAccountSnapshot) -> bool:
        if not isinstance(snapshot, BookmakerAccountSnapshot):
            raise AccountReconciliationIntegrityError(
                "snapshot must be a BookmakerAccountSnapshot"
            )
        with _write_lock(self.path):
            history = self._load_history()
            incoming_id = snapshot_fingerprint(snapshot)
            if any(snapshot_fingerprint(existing) == incoming_id for existing in history):
                return False
            if history:
                latest = history[-1]
                self._require_same_account(latest, snapshot)
                incoming_at = _time(snapshot.observed_at, "snapshot.observed_at")
                latest_at = _time(latest.observed_at, "checkpoint.observed_at")
                if incoming_at < latest_at:
                    raise AccountSnapshotStaleError(
                        "older account snapshot cannot supersede the durable checkpoint"
                    )
                if incoming_at == latest_at:
                    raise AccountReconciliationIntegrityError(
                        "conflicting account snapshot content at the same observed_at"
                    )
            candidate = (*history, snapshot)
            self._reconcile(candidate)
            self._write_history(candidate)
            return True

    def history(self) -> tuple[BookmakerAccountSnapshot, ...]:
        return tuple(self._load_history())

    def latest_snapshot(self) -> BookmakerAccountSnapshot | None:
        history = self._load_history()
        return history[-1] if history else None

    def latest_state(self) -> ReconciledAccountState | None:
        history = self._load_history()
        return self._reconcile(history) if history else None

    @staticmethod
    def _require_same_account(
        first: BookmakerAccountSnapshot,
        second: BookmakerAccountSnapshot,
    ) -> None:
        left = (first.profile.venue_id, first.profile.account_id, first.profile.adapter_id)
        right = (
            second.profile.venue_id,
            second.profile.account_id,
            second.profile.adapter_id,
        )
        if left != right:
            raise AccountReconciliationIntegrityError(
                "account reconciliation store cannot mix venue/account/adapter identity"
            )

    @classmethod
    def _reconcile(
        cls, history: tuple[BookmakerAccountSnapshot, ...] | list[BookmakerAccountSnapshot]
    ) -> ReconciledAccountState:
        if not history:
            raise AccountReconciliationIntegrityError(
                "cannot reconcile empty account history"
            )
        first = history[0]
        previous_at: datetime | None = None
        positions: dict[str, ReconciledPosition] = {}
        latest_balance: BookmakerBalanceObservation | None = None
        balance_delta: UnexplainedBalanceDelta | None = None
        balance_observations: dict[str, dict[str, object]] = {}
        position_observations: dict[str, dict[str, object]] = {}

        for snapshot in history:
            cls._require_same_account(first, snapshot)
            current_at = _time(snapshot.observed_at, "snapshot.observed_at")
            if previous_at is not None and current_at <= previous_at:
                raise AccountReconciliationIntegrityError(
                    "persisted account snapshot history must be strictly chronological"
                )
            previous_at = current_at
            balance_delta = None

            explicitly_seen: set[str] = set()
            for observation in snapshot.open_positions:
                observation_payload = _position_to_dict(observation)
                prior_payload = position_observations.get(observation.observation_id)
                if prior_payload is not None and prior_payload != observation_payload:
                    raise AccountReconciliationIntegrityError(
                        "position observation_id was reused with conflicting content"
                    )
                position_observations[observation.observation_id] = observation_payload
                external_id = observation.external_position_id
                prior = positions.get(external_id)
                if prior is not None and prior.state is ReconciledPositionState.SETTLED:
                    raise AccountReconciliationIntegrityError(
                        "explicitly settled provider position cannot regress to OPEN"
                    )
                positions[external_id] = ReconciledPosition(
                    external_position_id=external_id,
                    state=ReconciledPositionState.OPEN,
                    last_observation_id=observation.observation_id,
                    last_observed_at=observation.observed_at,
                )
                explicitly_seen.add(external_id)

            for observation in snapshot.settled_positions:
                observation_payload = _position_to_dict(observation)
                prior_payload = position_observations.get(observation.observation_id)
                if prior_payload is not None and prior_payload != observation_payload:
                    raise AccountReconciliationIntegrityError(
                        "position observation_id was reused with conflicting content"
                    )
                position_observations[observation.observation_id] = observation_payload
                external_id = observation.external_position_id
                positions[external_id] = ReconciledPosition(
                    external_position_id=external_id,
                    state=ReconciledPositionState.SETTLED,
                    last_observation_id=observation.observation_id,
                    last_observed_at=observation.observed_at,
                )
                explicitly_seen.add(external_id)

            for external_id, prior in tuple(positions.items()):
                if external_id in explicitly_seen:
                    continue
                if prior.state is ReconciledPositionState.SETTLED:
                    continue
                positions[external_id] = ReconciledPosition(
                    external_position_id=external_id,
                    state=ReconciledPositionState.UNKNOWN,
                    last_observation_id=prior.last_observation_id,
                    last_observed_at=prior.last_observed_at,
                )

            if snapshot.balance is not None:
                balance_payload = _balance_to_dict(snapshot.balance)
                prior_balance_payload = balance_observations.get(
                    snapshot.balance.observation_id
                )
                if (
                    prior_balance_payload is not None
                    and prior_balance_payload != balance_payload
                ):
                    raise AccountReconciliationIntegrityError(
                        "balance observation_id was reused with conflicting content"
                    )
                balance_observations[
                    snapshot.balance.observation_id
                ] = balance_payload
                if latest_balance is not None:
                    if latest_balance.currency != snapshot.balance.currency:
                        raise AccountReconciliationIntegrityError(
                            "available-balance currency changed within one account history"
                        )
                    balance_delta = UnexplainedBalanceDelta(
                        currency=snapshot.balance.currency,
                        amount=(
                            snapshot.balance.available_balance
                            - latest_balance.available_balance
                        ),
                        previous_observation_id=latest_balance.observation_id,
                        current_observation_id=snapshot.balance.observation_id,
                        previous_observed_at=latest_balance.observed_at,
                        current_observed_at=snapshot.balance.observed_at,
                    )
                latest_balance = snapshot.balance

        latest = history[-1]
        return ReconciledAccountState(
            snapshot_id=snapshot_fingerprint(latest),
            venue_id=latest.profile.venue_id,
            account_id=latest.profile.account_id,
            adapter_id=latest.profile.adapter_id,
            profile_id=latest.profile.profile_id,
            observed_at=latest.observed_at,
            observed_capabilities=latest.observed_capabilities,
            positions=tuple(
                sorted(positions.values(), key=lambda item: item.external_position_id)
            ),
            latest_balance_observation=latest_balance,
            unexplained_balance_delta=balance_delta,
        )

    def _load_history(self) -> list[BookmakerAccountSnapshot]:
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
            document = strict_json_loads(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            raise AccountReconciliationIntegrityError(
                "account reconciliation store is unreadable or corrupt"
            ) from exc
        payload = _exact_keys(document, {"schema_version", "snapshots"}, "store")
        if payload["schema_version"] != self.SCHEMA_VERSION:
            raise AccountReconciliationIntegrityError(
                "unsupported account reconciliation schema_version"
            )
        entries = payload["snapshots"]
        if not isinstance(entries, list):
            raise AccountReconciliationIntegrityError("snapshots must be a list")
        history: list[BookmakerAccountSnapshot] = []
        seen_ids: set[str] = set()
        for entry in entries:
            item = _exact_keys(entry, {"snapshot_id", "snapshot"}, "snapshot entry")
            snapshot = _decode_snapshot(item["snapshot"])
            expected_id = snapshot_fingerprint(snapshot)
            if item["snapshot_id"] != expected_id:
                raise AccountReconciliationIntegrityError(
                    "stored snapshot_id does not match canonical snapshot fingerprint"
                )
            if expected_id in seen_ids:
                raise AccountReconciliationIntegrityError(
                    "duplicate snapshot_id in account reconciliation store"
                )
            seen_ids.add(expected_id)
            history.append(snapshot)
        if history:
            self._reconcile(history)
        return history

    def _write_history(
        self, history: tuple[BookmakerAccountSnapshot, ...] | list[BookmakerAccountSnapshot]
    ) -> None:
        document = {
            "schema_version": self.SCHEMA_VERSION,
            "snapshots": [
                {
                    "snapshot_id": snapshot_fingerprint(snapshot),
                    "snapshot": snapshot_to_canonical_dict(snapshot),
                }
                for snapshot in history
            ],
        }
        encoded = (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            temp_name = None
            if os.name != "nt":
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(self.path.parent, flags)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise AccountReconciliationIntegrityError(
                "failed to durably publish account reconciliation store"
            ) from exc
        finally:
            if temp_name is not None:
                try:
                    Path(temp_name).unlink()
                except FileNotFoundError:
                    pass
