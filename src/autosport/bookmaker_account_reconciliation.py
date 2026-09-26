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
from weakref import ReferenceType, ref

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
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)

_RECONCILIATION_AUTHORITY_DOMAIN = "provider.account-snapshot-reconciliation-v1"
_RECONCILIATION_TRANSITION_SCHEMA = (
    "autosport.account-reconciliation-transition-v1"
)

_CANONICAL_AUTHORITY_CLASS = MonotonicWorkspaceAuthority
_CANONICAL_AUTHORITY_METHOD_NAMES = ("read_history", "prepare", "recover")
_CANONICAL_AUTHORITY_METHODS = {
    name: getattr(_CANONICAL_AUTHORITY_CLASS, name)
    for name in _CANONICAL_AUTHORITY_METHOD_NAMES
}
_CANONICAL_AUTHORITY_METHOD_CODES = {
    name: getattr(method, "__code__", None)
    for name, method in _CANONICAL_AUTHORITY_METHODS.items()
}
_MAX_CANONICAL_DECIMAL_TEXT_LENGTH = 4096
_WINDOWS_PRODUCT_AUTHORITY_ROOT_RELATIVE = (
    Path("Autosport") / "application-state" / "monotonic-authority-v1"
)
_POSIX_PRODUCT_AUTHORITY_ROOT_RELATIVE = Path("autosport") / "monotonic-authority-v1"


def _product_account_reconciliation_authority_root() -> Path:
    """Resolve the supported product machine-state root without env overrides.

    The generic MonotonicWorkspaceAuthority deliberately supports a caller/process
    override.  The supported account-reconciliation path must not use that override
    as its default authority selector: otherwise a restart can point at a fresh root
    after deleting local state and make a previously non-pristine workspace appear
    PRISTINE.  Resolve the OS-owned user state location directly instead.
    """

    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            # CSIDL_LOCAL_APPDATA.  Query the shell rather than trusting the
            # caller-editable LOCALAPPDATA environment variable.
            result = ctypes.windll.shell32.SHGetFolderPathW(  # type: ignore[attr-defined]
                None,
                0x001C,
                None,
                0,
                buffer,
            )
        except (AttributeError, OSError, ValueError) as exc:
            raise AccountReconciliationIntegrityError(
                "cannot resolve product-owned Windows account authority root"
            ) from exc
        if result != 0 or not buffer.value:
            raise AccountReconciliationIntegrityError(
                "cannot resolve product-owned Windows account authority root"
            )
        base = Path(buffer.value)
        relative = _WINDOWS_PRODUCT_AUTHORITY_ROOT_RELATIVE
    else:
        try:
            import pwd

            home = pwd.getpwuid(os.getuid()).pw_dir
        except (ImportError, KeyError, OSError) as exc:
            raise AccountReconciliationIntegrityError(
                "cannot resolve product-owned POSIX account authority root"
            ) from exc
        base = Path(home) / ".local" / "state"
        relative = _POSIX_PRODUCT_AUTHORITY_ROOT_RELATIVE

    if not base.is_absolute():
        raise AccountReconciliationIntegrityError(
            "product-owned account authority root must be absolute"
        )
    return base / relative


def _build_authority_binding_registry():
    """Keep constructor-issued authority trust outside caller-mutable store fields."""

    records: dict[
        int,
        tuple[
            ReferenceType[object],
            MonotonicWorkspaceAuthority,
            tuple[object, ...],
            Path,
            Path,
        ],
    ] = {}
    lock = RLock()

    def authority_binding(
        authority: MonotonicWorkspaceAuthority,
    ) -> tuple[object, ...]:
        return (
            authority.authority_root,
            authority.workspace,
            authority.workspace_instance_id,
            authority.domain,
            authority.key,
            authority.namespace_sha256,
            authority.journal_dir,
            authority.namespace_marker_path,
            authority.workspace_binding_path,
        )

    def register(
        store: object,
        authority: MonotonicWorkspaceAuthority,
        *,
        workspace: Path,
        path: Path,
    ) -> None:
        key = id(store)

        def release(
            dead_ref: ReferenceType[object],
            *,
            issued_key: int = key,
        ) -> None:
            with lock:
                record = records.get(issued_key)
                if record is not None and record[0] is dead_ref:
                    records.pop(issued_key, None)

        store_ref = ref(store, release)
        record = (
            store_ref,
            authority,
            authority_binding(authority),
            workspace,
            path,
        )
        with lock:
            existing = records.get(key)
            if existing is not None and existing[0]() is store:
                raise AccountReconciliationIntegrityError(
                    "account reconciliation monotonic authority issuance is already registered"
                )
            records[key] = record

    def lookup(
        store: object,
    ) -> tuple[
        MonotonicWorkspaceAuthority,
        tuple[object, ...],
        Path,
        Path,
    ] | None:
        with lock:
            record = records.get(id(store))
            if record is None or record[0]() is not store:
                return None
            return record[1], record[2], record[3], record[4]

    return register, lookup


_register_authority_binding, _lookup_authority_binding = (
    _build_authority_binding_registry()
)


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

    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    if not any(digits):
        return "0"

    # Canonicalize numeric aliases without Decimal arithmetic. Decimal.normalize()
    # is context-sensitive and may round high-precision provider values before
    # durable persistence/fingerprinting.
    while digits[-1] == 0:
        digits.pop()
        exponent += 1

    coefficient_length = len(digits)
    if exponent >= 0:
        fixed_length = coefficient_length + exponent
    else:
        point = coefficient_length + exponent
        fixed_length = (
            coefficient_length + 1
            if point > 0
            else 2 + (-point) + coefficient_length
        )
    if sign:
        fixed_length += 1
    if fixed_length > _MAX_CANONICAL_DECIMAL_TEXT_LENGTH:
        raise AccountReconciliationIntegrityError(
            "money canonical Decimal text exceeds bounded length"
        )

    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            text = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            text = f"0.{('0' * -point)}{coefficient}"

    return f"-{text}" if sign else text


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _exact_decimal_difference(left: Decimal, right: Decimal) -> Decimal:
    if (
        not isinstance(left, Decimal)
        or not isinstance(right, Decimal)
        or not left.is_finite()
        or not right.is_finite()
    ):
        raise AccountReconciliationIntegrityError(
            "balance delta operands must be finite Decimals"
        )

    def signed_coefficient(value: Decimal) -> tuple[int, int]:
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = (coefficient * 10) + digit
        if coefficient == 0:
            return 0, 0
        return (-coefficient if sign else coefficient), exponent

    left_coefficient, left_exponent = signed_coefficient(left)
    right_coefficient, right_exponent = signed_coefficient(right)
    common_exponent = min(left_exponent, right_exponent)
    left_coefficient *= 10 ** (left_exponent - common_exponent)
    right_coefficient *= 10 ** (right_exponent - common_exponent)
    coefficient = left_coefficient - right_coefficient

    if coefficient == 0:
        return Decimal(0)

    while coefficient % 10 == 0:
        coefficient //= 10
        common_exponent += 1

    sign = 1 if coefficient < 0 else 0
    digits = tuple(int(character) for character in str(abs(coefficient)))
    return Decimal((sign, digits, common_exponent))


def _authority_tx_prefix(snapshot_id: str) -> str:
    return f"account-reconciliation:{snapshot_id}:"


def _authority_transition_binding(
    *,
    previous_state_sha256: str | None,
    snapshot_id: str,
    tx_id: str,
) -> str:
    payload = {
        "schema": _RECONCILIATION_TRANSITION_SCHEMA,
        "schema_version": 1,
        "previous_state_sha256": previous_state_sha256,
        "snapshot_id": snapshot_id,
        "tx_id": tx_id,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


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

    def __init__(
        self,
        path: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self._workspace = self.path.parent.resolve(strict=False)
        if MonotonicWorkspaceAuthority is not _CANONICAL_AUTHORITY_CLASS:
            raise AccountReconciliationIntegrityError(
                "account reconciliation monotonic authority class identity changed"
            )
        selected_authority_root = (
            _product_account_reconciliation_authority_root()
            if authority_root is None
            else authority_root
        )
        authority = _CANONICAL_AUTHORITY_CLASS(
            workspace=self._workspace,
            domain=_RECONCILIATION_AUTHORITY_DOMAIN,
            key=f"account-reconciliation:{self.path.name}",
            authority_root=selected_authority_root,
        )
        self._authority = authority
        _register_authority_binding(
            self,
            authority,
            workspace=self._workspace,
            path=self.path,
        )

    def _require_canonical_authority(
        self,
        _registry_lookup=_lookup_authority_binding,
    ) -> MonotonicWorkspaceAuthority:
        if MonotonicWorkspaceAuthority is not _CANONICAL_AUTHORITY_CLASS:
            raise AccountReconciliationIntegrityError(
                "account reconciliation monotonic authority class identity changed"
            )
        authority = self._authority
        registered = _registry_lookup(self)
        if registered is None:
            raise AccountReconciliationIntegrityError(
                "account reconciliation monotonic authority issuance is missing"
            )
        (
            expected_authority,
            expected_binding,
            expected_workspace,
            expected_path,
        ) = registered
        current_binding = (
            getattr(authority, "authority_root", None),
            getattr(authority, "workspace", None),
            getattr(authority, "workspace_instance_id", None),
            getattr(authority, "domain", None),
            getattr(authority, "key", None),
            getattr(authority, "namespace_sha256", None),
            getattr(authority, "journal_dir", None),
            getattr(authority, "namespace_marker_path", None),
            getattr(authority, "workspace_binding_path", None),
        )
        if (
            type(authority) is not _CANONICAL_AUTHORITY_CLASS
            or authority is not expected_authority
            or current_binding != expected_binding
            or self._workspace != expected_workspace
            or self.path != expected_path
            or authority.workspace != expected_workspace
            or authority.domain != _RECONCILIATION_AUTHORITY_DOMAIN
            or authority.key != f"account-reconciliation:{expected_path.name}"
        ):
            raise AccountReconciliationIntegrityError(
                "account reconciliation monotonic authority identity or binding changed"
            )

        for method_name in _CANONICAL_AUTHORITY_METHOD_NAMES:
            expected_method = _CANONICAL_AUTHORITY_METHODS[method_name]
            current_class_method = getattr(
                _CANONICAL_AUTHORITY_CLASS,
                method_name,
                None,
            )
            bound_method = getattr(authority, method_name, None)
            if (
                current_class_method is not expected_method
                or getattr(current_class_method, "__code__", None)
                is not _CANONICAL_AUTHORITY_METHOD_CODES[method_name]
                or getattr(bound_method, "__self__", None) is not authority
                or getattr(bound_method, "__func__", None) is not expected_method
            ):
                raise AccountReconciliationIntegrityError(
                    "account reconciliation monotonic authority dispatch changed"
                )
        return authority

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
        with _write_lock(self.path):
            return tuple(self._load_history())

    def latest_snapshot(self) -> BookmakerAccountSnapshot | None:
        with _write_lock(self.path):
            history = self._load_history()
            return history[-1] if history else None

    def latest_state(self) -> ReconciledAccountState | None:
        with _write_lock(self.path):
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

    @staticmethod
    def _require_nested_evidence_after(
        snapshot: BookmakerAccountSnapshot,
        previous_snapshot_at: datetime,
        *,
        balance_observations: dict[str, dict[str, object]],
        position_observations: dict[str, dict[str, object]],
    ) -> None:
        """Require causal freshness only when a nested observation identity is new.

        A later account snapshot may legitimately carry the exact same provider
        observation again.  Previously seen identities therefore pass through to
        the canonical payload conflict/dedup checks below; changed content under an
        existing identity still fails closed there.  Only previously unseen nested
        evidence can advance reconciliation truth, so only that evidence must be
        newer than the preceding account snapshot.
        """

        evidence: list[tuple[str, str]] = []
        if (
            snapshot.balance is not None
            and snapshot.balance.observation_id not in balance_observations
        ):
            evidence.append(("balance", snapshot.balance.observed_at))
        evidence.extend(
            ("open position", observation.observed_at)
            for observation in snapshot.open_positions
            if observation.observation_id not in position_observations
        )
        evidence.extend(
            ("settled position", observation.observed_at)
            for observation in snapshot.settled_positions
            if observation.observation_id not in position_observations
        )
        for label, observed_at in evidence:
            if _time(observed_at, f"{label}.observed_at") <= previous_snapshot_at:
                raise AccountReconciliationIntegrityError(
                    f"{label} evidence must be newer than the previous account snapshot"
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
            if previous_at is not None:
                if current_at <= previous_at:
                    raise AccountReconciliationIntegrityError(
                        "persisted account snapshot history must be strictly chronological"
                    )
                cls._require_nested_evidence_after(
                    snapshot,
                    previous_at,
                    balance_observations=balance_observations,
                    position_observations=position_observations,
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
                is_new_balance_observation = prior_balance_payload is None
                balance_observations[
                    snapshot.balance.observation_id
                ] = balance_payload
                if is_new_balance_observation:
                    if latest_balance is not None:
                        if latest_balance.currency != snapshot.balance.currency:
                            raise AccountReconciliationIntegrityError(
                                "available-balance currency changed within one account history"
                            )
                        balance_delta = UnexplainedBalanceDelta(
                            currency=snapshot.balance.currency,
                            amount=_exact_decimal_difference(
                                snapshot.balance.available_balance,
                                latest_balance.available_balance,
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

    def _recover_authority(
        self,
        observed_state_sha256: str | None,
        *,
        history: list[BookmakerAccountSnapshot] | None = None,
        _authority_guard=_require_canonical_authority,
    ) -> None:
        # Capture the product-owned validator at class-definition time.  Resolving
        # self._require_canonical_authority here would let an exact store instance
        # shadow the guard after construction and route durability to another root.
        authority = _authority_guard(self)
        try:
            records = authority.read_history()
            pending = (
                records[-1]
                if records and records[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if (
                pending is not None
                and observed_state_sha256 == pending.intended_state_sha256
            ):
                if not history:
                    raise AccountReconciliationIntegrityError(
                        "prepared account reconciliation state has no snapshot"
                    )
                snapshot_id = snapshot_fingerprint(history[-1])
                prefix = _authority_tx_prefix(snapshot_id)
                if not pending.tx_id.startswith(prefix):
                    raise AccountReconciliationIntegrityError(
                        "account reconciliation authority tx does not bind latest snapshot"
                    )
                suffix = pending.tx_id[len(prefix) :]
                if not suffix.isascii() or not suffix.isdigit() or int(suffix) <= 0:
                    raise AccountReconciliationIntegrityError(
                        "account reconciliation authority tx attempt is invalid"
                    )
                expected_binding = _authority_transition_binding(
                    previous_state_sha256=pending.previous_committed_state_sha256,
                    snapshot_id=snapshot_id,
                    tx_id=pending.tx_id,
                )
                if pending.semantic_binding_sha256 != expected_binding:
                    raise AccountReconciliationIntegrityError(
                        "account reconciliation authority semantic binding mismatch"
                    )
                authority.recover(
                    observed_state_sha256=observed_state_sha256,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=expected_binding,
                )
            else:
                authority.recover(
                    observed_state_sha256=observed_state_sha256,
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise AccountReconciliationIntegrityError(
                "account reconciliation failed independent monotonic authority validation"
            ) from exc

    def _next_authority_tx_id(
        self,
        snapshot_id: str,
        *,
        _authority_guard=_require_canonical_authority,
    ) -> str:
        prefix = _authority_tx_prefix(snapshot_id)
        authority = _authority_guard(self)
        try:
            records = authority.read_history()
        except MonotonicWorkspaceAuthorityError as exc:
            raise AccountReconciliationIntegrityError(
                "account reconciliation monotonic authority history is unreadable"
            ) from exc
        attempts: list[int] = []
        for record in records:
            if not record.tx_id.startswith(prefix):
                continue
            suffix = record.tx_id[len(prefix) :]
            if suffix.isascii() and suffix.isdigit() and int(suffix) > 0:
                attempts.append(int(suffix))
        return f"{prefix}{max(attempts, default=0) + 1}"

    def _load_history(self) -> list[BookmakerAccountSnapshot]:
        if not self.path.exists():
            self._recover_authority(None)
            return []
        try:
            raw_bytes = self.path.read_bytes()
            raw = raw_bytes.decode("utf-8")
            document = strict_json_loads(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            raise AccountReconciliationIntegrityError(
                "account reconciliation store is unreadable or corrupt"
            ) from exc
        payload = _exact_keys(document, {"schema_version", "snapshots"}, "store")
        schema_version = payload["schema_version"]
        if type(schema_version) is not int or schema_version != self.SCHEMA_VERSION:
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
        state_sha256 = sha256(raw_bytes).hexdigest()
        self._recover_authority(state_sha256, history=history)
        return history

    @classmethod
    def _encode_history(
        cls,
        history: tuple[BookmakerAccountSnapshot, ...] | list[BookmakerAccountSnapshot],
    ) -> bytes:
        if not history:
            raise AccountReconciliationIntegrityError(
                "cannot persist empty account reconciliation history"
            )
        document = {
            "schema_version": cls.SCHEMA_VERSION,
            "snapshots": [
                {
                    "snapshot_id": snapshot_fingerprint(snapshot),
                    "snapshot": snapshot_to_canonical_dict(snapshot),
                }
                for snapshot in history
            ],
        }
        return (
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + "\n"
        ).encode("utf-8")

    def _publish_history_bytes(self, encoded: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
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

    def _write_history(
        self,
        history: tuple[BookmakerAccountSnapshot, ...] | list[BookmakerAccountSnapshot],
        *,
        _authority_guard=_require_canonical_authority,
    ) -> None:
        authority = _authority_guard(self)
        encoded = self._encode_history(history)
        intended_state_sha256 = sha256(encoded).hexdigest()
        previous_state_sha256: str | None = None
        if self.path.exists():
            try:
                previous_state_sha256 = sha256(self.path.read_bytes()).hexdigest()
            except OSError as exc:
                raise AccountReconciliationIntegrityError(
                    "cannot read current account reconciliation state before publication"
                ) from exc

        latest_snapshot_id = snapshot_fingerprint(history[-1])
        tx_id = self._next_authority_tx_id(latest_snapshot_id)
        semantic_binding = _authority_transition_binding(
            previous_state_sha256=previous_state_sha256,
            snapshot_id=latest_snapshot_id,
            tx_id=tx_id,
        )
        try:
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=previous_state_sha256,
                intended_state_sha256=intended_state_sha256,
                semantic_binding_sha256=semantic_binding,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise AccountReconciliationIntegrityError(
                "account reconciliation monotonic transition was rejected"
            ) from exc

        self._publish_history_bytes(encoded)

        # Re-read exact durable bytes. If the process crashed after local publication,
        # the same path on restart performs this recovery and commits the PREPARE only
        # after reconstructing its semantic binding from the persisted latest snapshot.
        persisted = self._load_history()
        intended_ids = tuple(snapshot_fingerprint(item) for item in history)
        persisted_ids = tuple(snapshot_fingerprint(item) for item in persisted)
        if persisted_ids != intended_ids:
            raise AccountReconciliationIntegrityError(
                "account reconciliation publication did not preserve intended history"
            )
