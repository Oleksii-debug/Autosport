"""Fail-closed bookmaker capability and account observation contracts.

This module is deliberately data/policy only. It models technical capability evidence and
read-only account observations. It never performs network access, credentials handling,
bet placement/cancellation/cashout, bankroll mutation, or canonical execution settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Protocol, runtime_checkable


class BookmakerCapabilityError(ValueError):
    """Raised when bookmaker capability/account evidence violates the contract."""


class UnknownBookmakerCapability(BookmakerCapabilityError):
    """Raised when requested capability has no conclusive technical evidence."""


class UnsupportedBookmakerCapability(BookmakerCapabilityError):
    """Raised when requested capability is conclusively unsupported."""


class BookmakerCapability(str, Enum):
    """Technical capability vocabulary.

    Write-like values are reserved as evidence vocabulary only. This module does not expose
    methods that perform those actions.
    """

    ACCOUNT_IDENTITY_READ = "account_identity_read"
    BALANCE_READ = "balance_read"
    LIMITS_READ = "limits_read"
    PREMATCH_QUOTES_READ = "prematch_quotes_read"
    LIVE_QUOTES_READ = "live_quotes_read"
    BETSLIP_READ = "betslip_read"
    OPEN_POSITIONS_READ = "open_positions_read"
    SETTLED_POSITIONS_READ = "settled_positions_read"
    PLACE_BET = "place_bet"
    BET_READBACK = "bet_readback"
    CASHOUT = "cashout"
    CANCEL_BET = "cancel_bet"


class BookmakerCapabilityState(str, Enum):
    UNKNOWN = "unknown"
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


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
class BookmakerCapabilityFact:
    capability: BookmakerCapability
    state: BookmakerCapabilityState

    def __post_init__(self) -> None:
        if not isinstance(self.capability, BookmakerCapability):
            raise BookmakerCapabilityError(
                "capability must be a BookmakerCapability value"
            )
        if not isinstance(self.state, BookmakerCapabilityState):
            raise BookmakerCapabilityError(
                "state must be a BookmakerCapabilityState value"
            )


@dataclass(frozen=True, slots=True)
class BookmakerCapabilityProfile:
    """Versioned technical capability evidence for one venue/account/adapter scope."""

    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    facts: tuple[BookmakerCapabilityFact, ...]
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.adapter_version, "adapter_version")
        if (
            not isinstance(self.profile_version, int)
            or isinstance(self.profile_version, bool)
            or self.profile_version < 1
        ):
            raise BookmakerCapabilityError("profile_version must be a positive integer")
        if not isinstance(self.facts, tuple):
            raise BookmakerCapabilityError("facts must be a tuple")
        seen: set[BookmakerCapability] = set()
        for fact in self.facts:
            if not isinstance(fact, BookmakerCapabilityFact):
                raise BookmakerCapabilityError(
                    "facts must contain only BookmakerCapabilityFact values"
                )
            if fact.capability in seen:
                raise BookmakerCapabilityError(
                    f"duplicate capability fact: {fact.capability.value}"
                )
            seen.add(fact.capability)
        _timestamp(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

    @property
    def profile_id(self) -> str:
        payload = self.to_canonical_dict()
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "facts": [
                {"capability": fact.capability.value, "state": fact.state.value}
                for fact in sorted(self.facts, key=lambda item: item.capability.value)
            ],
            "observed_at": self.observed_at,
            "profile_version": self.profile_version,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "venue_id": self.venue_id,
        }

    def state_of(self, capability: BookmakerCapability) -> BookmakerCapabilityState:
        if not isinstance(capability, BookmakerCapability):
            raise BookmakerCapabilityError(
                "capability must be a BookmakerCapability value"
            )
        for fact in self.facts:
            if fact.capability is capability:
                return fact.state
        return BookmakerCapabilityState.UNKNOWN

    def supports(self, capability: BookmakerCapability) -> bool:
        return self.state_of(capability) is BookmakerCapabilityState.SUPPORTED

    def require(self, capability: BookmakerCapability) -> None:
        state = self.state_of(capability)
        if state is BookmakerCapabilityState.UNKNOWN:
            raise UnknownBookmakerCapability(
                f"{self.venue_id}/{self.account_id} capability "
                f"{capability.value} is unknown"
            )
        if state is BookmakerCapabilityState.UNSUPPORTED:
            raise UnsupportedBookmakerCapability(
                f"{self.venue_id}/{self.account_id} does not support "
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
    """Provider-native position evidence, not canonical settlement/P&L truth."""

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
        """Return current technical capability evidence."""

    def read_account_snapshot(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
        /,
    ) -> BookmakerAccountSnapshot:
        """Read supported capabilities without creating external effects."""
