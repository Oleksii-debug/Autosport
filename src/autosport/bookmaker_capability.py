"""Fail-closed read-only bookmaker capability and account observation contracts.

This module is deliberately policy/data only.  It defines what an adapter says it can
read and the immutable observations returned from those reads.  It does not contain
credentials, network access, bet placement/cancellation, or canonical execution-ledger
truth.  Product-level execution and settlement authority remain owned by the supervised
execution program (#353).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable


class BookmakerCapabilityError(ValueError):
    """Raised when bookmaker capability/account evidence violates the contract."""


class UnsupportedBookmakerCapability(BookmakerCapabilityError):
    """Raised when a read is not explicitly supported by the current profile."""


class BookmakerCapability(str, Enum):
    """Read-only capabilities that an adapter may explicitly advertise."""

    BALANCE_READ = "balance_read"
    OPEN_POSITIONS_READ = "open_positions_read"
    SETTLED_POSITIONS_READ = "settled_positions_read"


class BookmakerPositionState(str, Enum):
    OPEN = "open"
    SETTLED = "settled"


def _text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise BookmakerCapabilityError(f"{field} must be a string")
    if not value or value != value.strip():
        raise BookmakerCapabilityError(f"{field} must be non-empty and trimmed")
    return value


def _timestamp(value: str, field: str) -> datetime:
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BookmakerCapabilityError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BookmakerCapabilityError(f"{field} must include a timezone offset")
    return parsed


def _money(value: Decimal, field: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BookmakerCapabilityError(f"{field} must be a finite Decimal")
    if positive:
        if value <= 0:
            raise BookmakerCapabilityError(f"{field} must be positive")
    elif value < 0:
        raise BookmakerCapabilityError(f"{field} must be non-negative")
    return value


def _sha256(value: str, field: str) -> str:
    _text(value, field)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise BookmakerCapabilityError(
            f"{field} must be a lowercase 64-character SHA-256 hex digest"
        )
    return value


def _currency(value: str) -> str:
    _text(value, "currency")
    if value != value.upper() or not value.isascii() or not value.isalnum():
        raise BookmakerCapabilityError(
            "currency must be an uppercase ASCII alphanumeric provider currency code"
        )
    return value


@dataclass(frozen=True, slots=True)
class BookmakerCapabilityProfile:
    """Immutable statement of what one adapter can read for one bookmaker account."""

    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    capabilities: frozenset[BookmakerCapability]
    observed_at: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.adapter_version, "adapter_version")
        _timestamp(self.observed_at, "observed_at")
        if not isinstance(self.capabilities, frozenset):
            raise BookmakerCapabilityError("capabilities must be a frozenset")
        invalid = [
            item
            for item in self.capabilities
            if not isinstance(item, BookmakerCapability)
        ]
        if invalid:
            raise BookmakerCapabilityError(
                "capabilities must contain only BookmakerCapability values"
            )

    def supports(self, capability: BookmakerCapability) -> bool:
        if not isinstance(capability, BookmakerCapability):
            raise BookmakerCapabilityError(
                "capability must be a BookmakerCapability value"
            )
        return capability in self.capabilities

    def require(self, capability: BookmakerCapability) -> None:
        if not self.supports(capability):
            raise UnsupportedBookmakerCapability(
                f"{self.venue_id}/{self.account_id} does not advertise "
                f"{capability.value}"
            )


@dataclass(frozen=True, slots=True)
class BookmakerBalanceObservation:
    """Provider-derived balance evidence; not a canonical execution ledger."""

    venue_id: str
    account_id: str
    adapter_id: str
    observation_id: str
    currency: str
    available_balance: Decimal
    total_balance: Decimal
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.observation_id, "observation_id")
        _currency(self.currency)
        _money(self.available_balance, "available_balance")
        _money(self.total_balance, "total_balance")
        if self.available_balance > self.total_balance:
            raise BookmakerCapabilityError(
                "available_balance cannot exceed total_balance"
            )
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.source_payload_sha256, "source_payload_sha256")


@dataclass(frozen=True, slots=True)
class BookmakerPositionObservation:
    """Provider-native open/settled position evidence.

    Monetary fields are observations from the provider and intentionally do not
    establish canonical settlement, profit, or bankroll truth.
    """

    venue_id: str
    account_id: str
    adapter_id: str
    observation_id: str
    external_position_id: str
    state: BookmakerPositionState
    stake: Decimal
    currency: str
    observed_at: str
    source_payload_sha256: str
    decimal_odds: Decimal | None = None
    gross_return: Decimal | None = None
    external_receipt_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.observation_id, "observation_id")
        _text(self.external_position_id, "external_position_id")
        if not isinstance(self.state, BookmakerPositionState):
            raise BookmakerCapabilityError(
                "state must be a BookmakerPositionState value"
            )
        _money(self.stake, "stake")
        _currency(self.currency)
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        if self.decimal_odds is not None:
            _money(self.decimal_odds, "decimal_odds", positive=True)
        if self.gross_return is not None:
            _money(self.gross_return, "gross_return")
        if self.external_receipt_id is not None:
            _text(self.external_receipt_id, "external_receipt_id")


@dataclass(frozen=True, slots=True)
class BookmakerAccountSnapshot:
    """One read-only account observation bound to one capability profile."""

    profile: BookmakerCapabilityProfile
    observed_capabilities: frozenset[BookmakerCapability]
    observed_at: str
    balance: BookmakerBalanceObservation | None = None
    open_positions: tuple[BookmakerPositionObservation, ...] = ()
    settled_positions: tuple[BookmakerPositionObservation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.profile, BookmakerCapabilityProfile):
            raise BookmakerCapabilityError(
                "profile must be a BookmakerCapabilityProfile"
            )
        _timestamp(self.observed_at, "observed_at")
        if not isinstance(self.observed_capabilities, frozenset):
            raise BookmakerCapabilityError(
                "observed_capabilities must be a frozenset"
            )
        for capability in self.observed_capabilities:
            if not isinstance(capability, BookmakerCapability):
                raise BookmakerCapabilityError(
                    "observed_capabilities must contain only BookmakerCapability values"
                )
            self.profile.require(capability)

        self._validate_balance()
        self._validate_positions(
            self.open_positions,
            BookmakerPositionState.OPEN,
            BookmakerCapability.OPEN_POSITIONS_READ,
            "open_positions",
        )
        self._validate_positions(
            self.settled_positions,
            BookmakerPositionState.SETTLED,
            BookmakerCapability.SETTLED_POSITIONS_READ,
            "settled_positions",
        )

    def _validate_balance(self) -> None:
        observed = BookmakerCapability.BALANCE_READ in self.observed_capabilities
        if observed != (self.balance is not None):
            raise BookmakerCapabilityError(
                "balance must be present exactly when balance_read was observed"
            )
        if self.balance is not None:
            self._validate_identity(
                self.balance.venue_id,
                self.balance.account_id,
                self.balance.adapter_id,
                "balance",
            )

    def _validate_positions(
        self,
        positions: tuple[BookmakerPositionObservation, ...],
        state: BookmakerPositionState,
        capability: BookmakerCapability,
        field: str,
    ) -> None:
        if not isinstance(positions, tuple):
            raise BookmakerCapabilityError(f"{field} must be a tuple")
        if positions and capability not in self.observed_capabilities:
            raise BookmakerCapabilityError(
                f"{field} cannot contain observations unless {capability.value} "
                "was observed"
            )
        seen: set[str] = set()
        for position in positions:
            if not isinstance(position, BookmakerPositionObservation):
                raise BookmakerCapabilityError(
                    f"{field} must contain BookmakerPositionObservation values"
                )
            self._validate_identity(
                position.venue_id,
                position.account_id,
                position.adapter_id,
                field,
            )
            if position.state is not state:
                raise BookmakerCapabilityError(
                    f"{field} contains a {position.state.value} observation"
                )
            if position.observation_id in seen:
                raise BookmakerCapabilityError(
                    f"{field} contains duplicate observation_id "
                    f"{position.observation_id}"
                )
            seen.add(position.observation_id)

    def _validate_identity(
        self,
        venue_id: str,
        account_id: str,
        adapter_id: str,
        field: str,
    ) -> None:
        expected = (
            self.profile.venue_id,
            self.profile.account_id,
            self.profile.adapter_id,
        )
        actual = (venue_id, account_id, adapter_id)
        if actual != expected:
            raise BookmakerCapabilityError(
                f"{field} identity does not match capability profile"
            )


@runtime_checkable
class ReadOnlyBookmakerAdapter(Protocol):
    """Side-effect-free adapter boundary for Stage-K account observations."""

    def capability_profile(self) -> BookmakerCapabilityProfile:
        """Return the current explicit capability statement."""

    def read_account_snapshot(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
        /,
    ) -> BookmakerAccountSnapshot:
        """Read requested supported capabilities without creating external effects."""
