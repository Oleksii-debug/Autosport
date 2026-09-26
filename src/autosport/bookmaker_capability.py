"""Fail-closed bookmaker capability and account observation contracts.

This module is deliberately data/policy only. It models technical capability evidence and
read-only account observations. It never performs network access, credentials handling,
bet placement/cancellation/cashout, bankroll mutation, or canonical execution settlement.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
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
    """Provider-native funds evidence; never an inferred canonical balance ledger."""

    venue_id: str
    account_id: str
    adapter_id: str
    observation_id: str
    currency: str
    available_balance: Decimal
    observed_at: str
    source_payload_sha256: str
    total_balance: Decimal | None = None
    total_balance_source_ref: str | None = None
    exposure: Decimal | None = None
    retained_commission: Decimal | None = None
    exposure_limit: Decimal | None = None

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.observation_id, "observation_id")
        _currency(self.currency)
        _money(self.available_balance, "available_balance")
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.source_payload_sha256, "source_payload_sha256")

        if self.total_balance is None:
            if self.total_balance_source_ref is not None:
                raise BookmakerCapabilityError(
                    "total_balance_source_ref requires total_balance"
                )
        else:
            _money(self.total_balance, "total_balance")
            if self.available_balance > self.total_balance:
                raise BookmakerCapabilityError(
                    "available_balance cannot exceed total_balance"
                )
            if self.total_balance_source_ref is None:
                raise BookmakerCapabilityError(
                    "total_balance requires explicit total_balance_source_ref"
                )
            _text(self.total_balance_source_ref, "total_balance_source_ref")

        for field in ("exposure", "retained_commission", "exposure_limit"):
            value = getattr(self, field)
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise BookmakerCapabilityError(f"{field} must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class BookmakerPositionObservation:
    """Provider-native position evidence, not canonical settlement/P&L truth.

    ``provider_amount`` deliberately has no universal stake/liability meaning. Its exact
    provider meaning is carried by ``provider_amount_semantics`` (for example Betfair
    ``size`` can be recorded as ``backer_stake`` for both BACK and LAY). ``provider_side``
    preserves the provider's side label when one exists. ``provider_status`` preserves an
    opaque provider-native position/settlement status without interpreting it as canonical
    settlement or P&L truth. Consumers must not infer liability, canonical stake, settlement,
    or P&L from these fields without a separate authoritative contract.

    ``stake=`` remains an input-only compatibility shim for this not-yet-merged lineage. It
    is converted to ``provider_amount`` with semantics ``legacy_stake`` and is never stored
    as a position field. Provider adapters must use the explicit provider-native fields.
    """

    venue_id: str
    account_id: str
    adapter_id: str
    observation_id: str
    external_position_id: str
    state: BookmakerPositionState
    currency: str
    observed_at: str
    source_payload_sha256: str
    provider_amount: Decimal | None = None
    provider_amount_semantics: str | None = None
    provider_side: str | None = None
    decimal_odds: Decimal | None = None
    gross_return: Decimal | None = None
    external_receipt_id: str | None = None
    stake: InitVar[Decimal | None] = None
    provider_status: str | None = None

    def __post_init__(self, stake: Decimal | None) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.observation_id, "observation_id")
        _text(self.external_position_id, "external_position_id")
        if not isinstance(self.state, BookmakerPositionState):
            raise BookmakerCapabilityError(
                "state must be a BookmakerPositionState value"
            )

        provider_amount = self.provider_amount
        provider_amount_semantics = self.provider_amount_semantics
        if stake is not None:
            if provider_amount is not None or provider_amount_semantics is not None:
                raise BookmakerCapabilityError(
                    "legacy stake cannot be combined with provider_amount semantics"
                )
            provider_amount = stake
            provider_amount_semantics = "legacy_stake"
        if provider_amount is None:
            raise BookmakerCapabilityError("provider_amount is required")
        if provider_amount_semantics is None:
            raise BookmakerCapabilityError("provider_amount_semantics is required")
        _money(provider_amount, "provider_amount")
        _text(provider_amount_semantics, "provider_amount_semantics")
        object.__setattr__(self, "provider_amount", provider_amount)
        object.__setattr__(self, "provider_amount_semantics", provider_amount_semantics)
        if self.provider_side is not None:
            _text(self.provider_side, "provider_side")
        if self.provider_status is not None:
            _text(self.provider_status, "provider_status")

        _currency(self.currency)
        _timestamp(self.observed_at, "observed_at")
        _sha256(self.source_payload_sha256, "source_payload_sha256")
        if self.decimal_odds is not None:
            _money(self.decimal_odds, "decimal_odds", positive=True)
            if self.decimal_odds <= Decimal("1"):
                raise BookmakerCapabilityError(
                    "decimal_odds must be greater than 1"
                )
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
        snapshot_at = _timestamp(self.observed_at, "observed_at")
        self._validate_not_after_snapshot(
            self.profile.observed_at,
            snapshot_at,
            "profile",
        )
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

        self._validate_balance(snapshot_at)
        self._validate_positions(
            self.open_positions,
            BookmakerPositionState.OPEN,
            BookmakerCapability.OPEN_POSITIONS_READ,
            "open_positions",
            snapshot_at,
        )
        self._validate_positions(
            self.settled_positions,
            BookmakerPositionState.SETTLED,
            BookmakerCapability.SETTLED_POSITIONS_READ,
            "settled_positions",
            snapshot_at,
        )
        self._validate_cross_state_position_identity()

    def _validate_cross_state_position_identity(self) -> None:
        open_external_ids = {
            position.external_position_id for position in self.open_positions
        }
        settled_external_ids = {
            position.external_position_id for position in self.settled_positions
        }
        duplicates = open_external_ids & settled_external_ids
        if duplicates:
            duplicate = min(duplicates)
            raise BookmakerCapabilityError(
                "open_positions and settled_positions contain the same "
                f"external_position_id {duplicate}"
            )

    def _validate_balance(self, snapshot_at: datetime) -> None:
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
            self._validate_not_after_snapshot(
                self.balance.observed_at,
                snapshot_at,
                "balance",
            )

    def _validate_positions(
        self,
        positions: tuple[BookmakerPositionObservation, ...],
        state: BookmakerPositionState,
        capability: BookmakerCapability,
        field: str,
        snapshot_at: datetime,
    ) -> None:
        if not isinstance(positions, tuple):
            raise BookmakerCapabilityError(f"{field} must be a tuple")
        if positions and capability not in self.observed_capabilities:
            raise BookmakerCapabilityError(
                f"{field} cannot contain observations unless {capability.value} "
                "was observed"
            )
        seen_observation_ids: set[str] = set()
        seen_external_position_ids: set[str] = set()
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
            self._validate_not_after_snapshot(
                position.observed_at,
                snapshot_at,
                field,
            )
            if position.observation_id in seen_observation_ids:
                raise BookmakerCapabilityError(
                    f"{field} contains duplicate observation_id "
                    f"{position.observation_id}"
                )
            seen_observation_ids.add(position.observation_id)
            if position.external_position_id in seen_external_position_ids:
                raise BookmakerCapabilityError(
                    f"{field} contains duplicate external_position_id "
                    f"{position.external_position_id}"
                )
            seen_external_position_ids.add(position.external_position_id)

    @staticmethod
    def _validate_not_after_snapshot(
        observed_at: str,
        snapshot_at: datetime,
        field: str,
    ) -> None:
        if _timestamp(observed_at, f"{field}.observed_at") > snapshot_at:
            raise BookmakerCapabilityError(
                f"{field} observed_at cannot be later than snapshot observed_at"
            )

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