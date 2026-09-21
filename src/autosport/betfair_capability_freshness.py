"""Fail-closed Betfair market-data freshness evidence.

This module is evidence/policy only. It distinguishes the production exchange
environment, configured application-key class, observed provider delay state, and
stream freshness. It never performs provider I/O, stores credentials, grants
financial authority, or treats a delayed application key as a sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


class BetfairCapabilityFreshnessError(ValueError):
    """Raised when Betfair freshness evidence is malformed or contradictory."""


class UnknownBetfairMarketDataFreshness(BetfairCapabilityFreshnessError):
    """Raised when current market-data freshness has not been proven."""


class DelayedBetfairMarketData(BetfairCapabilityFreshnessError):
    """Raised when delayed data is offered for a live-evidence use."""


class BetfairProviderEnvironment(str, Enum):
    GLOBAL_PRODUCTION_EXCHANGE = "betfair_global_production_exchange"


class BetfairApplicationKeyClass(str, Enum):
    UNKNOWN = "unknown"
    DELAYED = "delayed"
    LIVE = "live"


class BetfairMarketDataDelayState(str, Enum):
    UNKNOWN = "unknown"
    DELAYED = "delayed"
    FRESH = "fresh"


class BetfairStreamFreshnessMode(str, Enum):
    UNKNOWN = "unknown"
    DELAYED_CONFLATED = "delayed_conflated"
    LIVE = "live"


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairCapabilityFreshnessError(
            f"{field} must be a non-empty trimmed exact string"
        )
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BetfairCapabilityFreshnessError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairCapabilityFreshnessError(
            f"{field} must include a timezone offset"
        )
    return parsed


def _sha256_hex(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise BetfairCapabilityFreshnessError(
            f"{field} must be lowercase 64-character SHA-256"
        )
    return text


def _exact_profile(profile: object) -> BookmakerCapabilityProfile:
    if type(profile) is not BookmakerCapabilityProfile:
        raise BetfairCapabilityFreshnessError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    for fact in profile.facts:
        if type(fact) is not BookmakerCapabilityFact:
            raise BetfairCapabilityFreshnessError(
                "profile facts must be exact BookmakerCapabilityFact values"
            )
        if type(fact.capability) is not BookmakerCapability:
            raise BetfairCapabilityFreshnessError(
                "profile capability must be an exact BookmakerCapability value"
            )
        if type(fact.state) is not BookmakerCapabilityState:
            raise BetfairCapabilityFreshnessError(
                "profile capability state must be an exact BookmakerCapabilityState value"
            )
    return profile


@dataclass(frozen=True, slots=True)
class BetfairCapabilityFreshnessEvidence:
    """Immutable Betfair data-freshness evidence for one capability profile.

    application_key_class is configuration/evidence metadata only. LIVE never
    proves fresh data and DELAYED never means sandbox. Fresh live-market use is
    admitted only from an explicit non-delayed provider observation plus the
    relevant technical read capability in the bound BookmakerCapabilityProfile.

    This contract has no field capable of granting product write authority.
    A technically write-capable profile therefore remains technical evidence only.
    """

    profile_id: str
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    environment: BetfairProviderEnvironment
    application_key_class: BetfairApplicationKeyClass
    market_data_delay_state: BetfairMarketDataDelayState
    stream_freshness_mode: BetfairStreamFreshnessMode
    observed_at: str
    source_ref: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _sha256_hex(self.profile_id, "profile_id")
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.adapter_version, "adapter_version")
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise BetfairCapabilityFreshnessError(
                "profile_version must be a positive exact integer"
            )
        if type(self.environment) is not BetfairProviderEnvironment:
            raise BetfairCapabilityFreshnessError(
                "environment must be a BetfairProviderEnvironment value"
            )
        if type(self.application_key_class) is not BetfairApplicationKeyClass:
            raise BetfairCapabilityFreshnessError(
                "application_key_class must be a BetfairApplicationKeyClass value"
            )
        if type(self.market_data_delay_state) is not BetfairMarketDataDelayState:
            raise BetfairCapabilityFreshnessError(
                "market_data_delay_state must be a BetfairMarketDataDelayState value"
            )
        if type(self.stream_freshness_mode) is not BetfairStreamFreshnessMode:
            raise BetfairCapabilityFreshnessError(
                "stream_freshness_mode must be a BetfairStreamFreshnessMode value"
            )
        _timestamp(self.observed_at, "observed_at")
        _text(self.source_ref, "source_ref")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")

        if (
            self.application_key_class is BetfairApplicationKeyClass.DELAYED
            and self.market_data_delay_state is BetfairMarketDataDelayState.FRESH
        ):
            raise BetfairCapabilityFreshnessError(
                "delayed application key cannot assert fresh market data"
            )
        if (
            self.application_key_class is BetfairApplicationKeyClass.DELAYED
            and self.stream_freshness_mode is BetfairStreamFreshnessMode.LIVE
        ):
            raise BetfairCapabilityFreshnessError(
                "delayed application key cannot assert live stream freshness"
            )
        if (
            self.market_data_delay_state is BetfairMarketDataDelayState.DELAYED
            and self.stream_freshness_mode is BetfairStreamFreshnessMode.LIVE
        ):
            raise BetfairCapabilityFreshnessError(
                "delayed market data cannot assert live stream freshness"
            )

    @classmethod
    def from_profile(
        cls,
        profile: BookmakerCapabilityProfile,
        *,
        environment: BetfairProviderEnvironment,
        application_key_class: BetfairApplicationKeyClass,
        market_data_delay_state: BetfairMarketDataDelayState,
        stream_freshness_mode: BetfairStreamFreshnessMode,
        observed_at: str,
        source_ref: str,
        source_payload_sha256: str,
    ) -> "BetfairCapabilityFreshnessEvidence":
        profile = _exact_profile(profile)
        observed = _timestamp(observed_at, "observed_at")
        profile_observed = _timestamp(profile.observed_at, "profile.observed_at")
        if observed < profile_observed:
            raise BetfairCapabilityFreshnessError(
                "freshness evidence cannot predate the bound capability profile"
            )
        return cls(
            profile_id=profile.profile_id,
            venue_id=profile.venue_id,
            account_id=profile.account_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_version=profile.profile_version,
            environment=environment,
            application_key_class=application_key_class,
            market_data_delay_state=market_data_delay_state,
            stream_freshness_mode=stream_freshness_mode,
            observed_at=observed_at,
            source_ref=source_ref,
            source_payload_sha256=source_payload_sha256,
        )

    @property
    def evidence_id(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    @property
    def is_production_exchange(self) -> bool:
        return (
            self.environment
            is BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE
        )

    @property
    def grants_product_write_authority(self) -> bool:
        """Freshness/configuration evidence can never expand financial authority."""
        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "application_key_class": self.application_key_class.value,
            "environment": self.environment.value,
            "market_data_delay_state": self.market_data_delay_state.value,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "stream_freshness_mode": self.stream_freshness_mode.value,
            "venue_id": self.venue_id,
        }

    def assert_matches_profile(
        self,
        profile: BookmakerCapabilityProfile,
    ) -> None:
        profile = _exact_profile(profile)
        expected = (
            profile.profile_id,
            profile.venue_id,
            profile.account_id,
            profile.adapter_id,
            profile.adapter_version,
            profile.profile_version,
        )
        actual = (
            self.profile_id,
            self.venue_id,
            self.account_id,
            self.adapter_id,
            self.adapter_version,
            self.profile_version,
        )
        if actual != expected:
            raise BetfairCapabilityFreshnessError(
                "freshness evidence does not match capability profile identity"
            )

    def technical_place_bet_state(
        self,
        profile: BookmakerCapabilityProfile,
    ) -> BookmakerCapabilityState:
        """Return technical evidence only; never a product write permission."""
        self.assert_matches_profile(profile)
        return profile.state_of(BookmakerCapability.PLACE_BET)

    def require_live_market_data(
        self,
        profile: BookmakerCapabilityProfile,
    ) -> None:
        self.assert_matches_profile(profile)
        profile.require(BookmakerCapability.LIVE_QUOTES_READ)
        if self.market_data_delay_state is BetfairMarketDataDelayState.UNKNOWN:
            raise UnknownBetfairMarketDataFreshness(
                "Betfair market-data delay state is unknown"
            )
        if self.market_data_delay_state is BetfairMarketDataDelayState.DELAYED:
            raise DelayedBetfairMarketData(
                "Betfair market data is delayed"
            )

    def require_subminute_stream_evidence(
        self,
        profile: BookmakerCapabilityProfile,
    ) -> None:
        self.require_live_market_data(profile)
        if self.stream_freshness_mode is BetfairStreamFreshnessMode.UNKNOWN:
            raise UnknownBetfairMarketDataFreshness(
                "Betfair stream freshness is unknown"
            )
        if (
            self.stream_freshness_mode
            is BetfairStreamFreshnessMode.DELAYED_CONFLATED
        ):
            raise DelayedBetfairMarketData(
                "Betfair stream is delayed/conflated and cannot prove sub-minute freshness"
            )
