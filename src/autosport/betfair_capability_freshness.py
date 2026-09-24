"""Fail-closed Betfair market-data freshness evidence.

Positive REST freshness can be issued only from a canonical, provider-native
MarketBook delay observation. Configuration such as application-key class
never grants freshness or product write authority. Stream freshness remains
unknown until a separate canonical Stream issuer exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from weakref import ref

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .betfair_marketbook_freshness import BetfairMarketBookDelayObservation


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
        raise BetfairCapabilityFreshnessError(f"{field} must be ISO-8601") from exc
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


def _max_age(value: object, *, subminute: bool = False) -> int:
    if type(value) is not int or value <= 0:
        raise BetfairCapabilityFreshnessError(
            "max_age_seconds must be a positive exact integer"
        )
    if subminute and value > 60:
        raise BetfairCapabilityFreshnessError(
            "sub-minute evidence max_age_seconds cannot exceed 60"
        )
    return value


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairCapabilityFreshnessEvidence:
    """Immutable exact-market Betfair freshness evidence.

    A non-delayed MarketBook response is positive REST freshness only when this
    object was issued from a canonical adapter observation. Any bound bookmaker
    profile and configured account reference are configuration/provenance scope,
    not provider-authenticated account identity or positive freshness authority.
    The public FRESH delay-state name means only "provider did not flag this
    REST response as delayed"; positive consumption additionally checks bounded
    age from the local receipt timestamp. Neither fact proves provider-native
    quote generation/publish age. It does not prove Stream freshness, financial
    authority, or whole-product readiness.
    """

    profile_id: str
    venue_id: str
    configured_account_ref: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    market_id: str
    environment: BetfairProviderEnvironment
    application_key_class: BetfairApplicationKeyClass
    market_data_delay_state: BetfairMarketDataDelayState
    stream_freshness_mode: BetfairStreamFreshnessMode
    observed_at: str
    source_ref: str
    source_payload_sha256: str
    authenticated_context_sha256: str | None = None

    def __post_init__(self) -> None:
        _sha256_hex(self.profile_id, "profile_id")
        _text(self.venue_id, "venue_id")
        _text(self.configured_account_ref, "configured_account_ref")
        _text(self.adapter_id, "adapter_id")
        _text(self.adapter_version, "adapter_version")
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise BetfairCapabilityFreshnessError(
                "profile_version must be a positive exact integer"
            )
        _text(self.market_id, "market_id")
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
        if self.authenticated_context_sha256 is not None:
            _sha256_hex(
                self.authenticated_context_sha256,
                "authenticated_context_sha256",
            )

        if (
            self.application_key_class is BetfairApplicationKeyClass.DELAYED
            and self.market_data_delay_state is BetfairMarketDataDelayState.FRESH
        ):
            raise BetfairCapabilityFreshnessError(
                "delayed application key cannot assert fresh market data"
            )
        if self.stream_freshness_mode is not BetfairStreamFreshnessMode.UNKNOWN:
            raise BetfairCapabilityFreshnessError(
                "MarketBook observation cannot assert Stream freshness"
            )

    @classmethod
    def from_market_book_observation(
        cls,
        profile: BookmakerCapabilityProfile,
        observation: BetfairMarketBookDelayObservation,
        *,
        application_key_class: BetfairApplicationKeyClass | None = None,
    ) -> "BetfairCapabilityFreshnessEvidence":
        profile = _exact_profile(profile)
        if type(observation) is not BetfairMarketBookDelayObservation:
            raise BetfairCapabilityFreshnessError(
                "observation must be an exact BetfairMarketBookDelayObservation"
            )
        observation.assert_authoritative()

        observed_key_class = {
            "unknown": BetfairApplicationKeyClass.UNKNOWN,
            "delayed": BetfairApplicationKeyClass.DELAYED,
            "live": BetfairApplicationKeyClass.LIVE,
        }[observation.application_key_class]
        if application_key_class is not None:
            if type(application_key_class) is not BetfairApplicationKeyClass:
                raise BetfairCapabilityFreshnessError(
                    "application_key_class must be a BetfairApplicationKeyClass value"
                )
            if (
                observed_key_class is not BetfairApplicationKeyClass.UNKNOWN
                and application_key_class is not observed_key_class
            ):
                raise BetfairCapabilityFreshnessError(
                    "caller application_key_class conflicts with authenticated provider metadata"
                )
        resolved_key_class = (
            observed_key_class
            if observed_key_class is not BetfairApplicationKeyClass.UNKNOWN
            else (
                application_key_class
                if application_key_class is not None
                else BetfairApplicationKeyClass.UNKNOWN
            )
        )

        observed = _timestamp(observation.observed_at, "observation.observed_at")
        profile_observed = _timestamp(profile.observed_at, "profile.observed_at")
        if observed < profile_observed:
            raise BetfairCapabilityFreshnessError(
                "freshness evidence cannot predate the bound capability profile"
            )
        expected_adapter = (
            profile.venue_id,
            profile.account_id,
            profile.adapter_id,
            profile.adapter_version,
        )
        actual_adapter = (
            observation.venue_id,
            observation.configured_account_ref,
            observation.adapter_id,
            observation.adapter_version,
        )
        if expected_adapter != actual_adapter:
            raise BetfairCapabilityFreshnessError(
                "market-book observation does not match capability profile adapter identity"
            )

        delay_state = (
            BetfairMarketDataDelayState.DELAYED
            if observation.is_market_data_delayed
            else BetfairMarketDataDelayState.FRESH
        )
        return cls(
            profile_id=profile.profile_id,
            venue_id=profile.venue_id,
            configured_account_ref=observation.configured_account_ref,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_version=profile.profile_version,
            market_id=observation.market_id,
            environment=BetfairProviderEnvironment.GLOBAL_PRODUCTION_EXCHANGE,
            application_key_class=resolved_key_class,
            market_data_delay_state=delay_state,
            stream_freshness_mode=BetfairStreamFreshnessMode.UNKNOWN,
            observed_at=observation.observed_at,
            source_ref=f"betfair://market-book/{observation.market_id}",
            source_payload_sha256=observation.source_payload_sha256,
            authenticated_context_sha256=observation.authenticated_context_sha256,
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
        return False

    @property
    def proves_provider_account_identity(self) -> bool:
        """MarketBook freshness does not authenticate a configured account label."""

        return False

    @property
    def proves_provider_quote_publish_age(self) -> bool:
        """REST delay status plus local receipt time is not quote publish-time proof."""

        return False

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "configured_account_ref": self.configured_account_ref,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "application_key_class": self.application_key_class.value,
            "authenticated_context_sha256": self.authenticated_context_sha256,
            "environment": self.environment.value,
            "market_data_delay_state": self.market_data_delay_state.value,
            "market_id": self.market_id,
            "observed_at": self.observed_at,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "source_payload_sha256": self.source_payload_sha256,
            "source_ref": self.source_ref,
            "stream_freshness_mode": self.stream_freshness_mode.value,
            "venue_id": self.venue_id,
        }

    def assert_matches_profile(self, profile: BookmakerCapabilityProfile) -> None:
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
            self.configured_account_ref,
            self.adapter_id,
            self.adapter_version,
            self.profile_version,
        )
        if actual != expected:
            raise BetfairCapabilityFreshnessError(
                "freshness evidence does not match configured capability profile scope"
            )

    def _authority_fingerprint(self) -> str:
        return sha256(
            json.dumps(
                self.to_canonical_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()

    def _assert_product_issued(self) -> None:
        raise BetfairCapabilityFreshnessError(
            "positive freshness evidence was not product-issued"
        )

    def _assert_current(
        self,
        *,
        as_of: str,
        max_age_seconds: int,
    ) -> None:
        decision = _timestamp(as_of, "as_of")
        observed = _timestamp(self.observed_at, "observed_at")
        max_age = _max_age(max_age_seconds)
        if observed > decision:
            raise UnknownBetfairMarketDataFreshness(
                "Betfair freshness evidence is from the future"
            )
        age_seconds = (decision - observed).total_seconds()
        if age_seconds > max_age:
            raise UnknownBetfairMarketDataFreshness(
                "Betfair freshness evidence is stale"
            )

    def technical_place_bet_state(
        self,
        profile: BookmakerCapabilityProfile,
    ) -> BookmakerCapabilityState:
        self.assert_matches_profile(profile)
        return profile.state_of(BookmakerCapability.PLACE_BET)

    def require_live_market_data(
        self,
        profile: BookmakerCapabilityProfile,
        *,
        market_id: str,
        as_of: str,
        max_age_seconds: int,
    ) -> None:
        """Require a canonical non-delayed REST response with recent local receipt.

        This gate does not prove provider-native quote generation/publish age.
        """

        self._assert_product_issued()
        self.assert_matches_profile(profile)
        market = _text(market_id, "market_id")
        if market != self.market_id:
            raise BetfairCapabilityFreshnessError(
                "freshness evidence is for a different market"
            )
        self._assert_current(as_of=as_of, max_age_seconds=max_age_seconds)
        if self.market_data_delay_state is BetfairMarketDataDelayState.FRESH:
            if self.application_key_class is not BetfairApplicationKeyClass.LIVE:
                raise UnknownBetfairMarketDataFreshness(
                    "positive Betfair market data lacks authenticated LIVE application-key class"
                )
            if self.authenticated_context_sha256 is None:
                raise UnknownBetfairMarketDataFreshness(
                    "positive Betfair market data lacks authenticated context identity"
                )
        if self.market_data_delay_state is BetfairMarketDataDelayState.UNKNOWN:
            raise UnknownBetfairMarketDataFreshness(
                "Betfair market-data delay state is unknown"
            )
        if self.market_data_delay_state is BetfairMarketDataDelayState.DELAYED:
            raise DelayedBetfairMarketData("Betfair market data is delayed")

    def require_subminute_stream_evidence(
        self,
        profile: BookmakerCapabilityProfile,
        *,
        market_id: str,
        as_of: str,
        max_age_seconds: int,
    ) -> None:
        _max_age(max_age_seconds, subminute=True)
        self.require_live_market_data(
            profile,
            market_id=market_id,
            as_of=as_of,
            max_age_seconds=max_age_seconds,
        )
        if self.stream_freshness_mode is BetfairStreamFreshnessMode.UNKNOWN:
            raise UnknownBetfairMarketDataFreshness(
                "canonical Betfair Stream freshness has not been observed"
            )
        if self.stream_freshness_mode is BetfairStreamFreshnessMode.DELAYED_CONFLATED:
            raise DelayedBetfairMarketData(
                "Betfair stream is delayed/conflated and cannot prove sub-minute freshness"
            )


def _install_freshness_authority() -> None:
    issued: dict[int, tuple[object, str, BetfairMarketBookDelayObservation]] = {}
    raw_issuer = BetfairCapabilityFreshnessEvidence.from_market_book_observation.__func__

    def from_market_book_observation(
        cls: type[BetfairCapabilityFreshnessEvidence],
        profile: BookmakerCapabilityProfile,
        observation: BetfairMarketBookDelayObservation,
        *,
        application_key_class: BetfairApplicationKeyClass | None = None,
    ) -> BetfairCapabilityFreshnessEvidence:
        evidence = raw_issuer(
            cls,
            profile,
            observation,
            application_key_class=application_key_class,
        )
        key = id(evidence)

        def forget(_weakref: object, *, evidence_id: int = key) -> None:
            issued.pop(evidence_id, None)

        issued[key] = (
            ref(evidence, forget),
            evidence._authority_fingerprint(),
            observation,
        )
        return evidence

    def assert_product_issued(self: BetfairCapabilityFreshnessEvidence) -> None:
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairCapabilityFreshnessError(
                "positive freshness evidence was not product-issued"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairCapabilityFreshnessError(
                "positive freshness evidence changed after product issuance"
            )
        observation = record[2]
        observation.assert_authoritative()
        if self.market_data_delay_state is BetfairMarketDataDelayState.FRESH:
            observation.assert_positive_authoritative()
        if self.authenticated_context_sha256 != observation.authenticated_context_sha256:
            raise BetfairCapabilityFreshnessError(
                "freshness evidence authenticated context does not match provider observation"
            )

    BetfairCapabilityFreshnessEvidence.from_market_book_observation = classmethod(
        from_market_book_observation
    )
    BetfairCapabilityFreshnessEvidence._assert_product_issued = assert_product_issued


_install_freshness_authority()
del _install_freshness_authority
