from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Iterable

from .domain import MarketEvent
from .forecasting import ForecastRecord


_MAX_OPPORTUNITIES = 10_000
_SHA256_HEX = frozenset("0123456789abcdef")


class OpportunityContractError(ValueError):
    """Raised when an opportunity/plan contract is ambiguous or non-canonical."""


class StrategyClass(str, Enum):
    """Canonical strategy-class vocabulary from binding decision contract #356."""

    PREDICTIVE_EDGE = "predictive_edge"
    LIVE_PRICE_MOVEMENT = "live_price_movement"
    ARBITRAGE = "arbitrage"
    DUTCHING = "dutching"
    HEDGE_REBALANCE = "hedge_rebalance"
    HYBRID = "hybrid"


class OpportunityDecision(str, Enum):
    WAIT = "wait"
    ZERO = "zero"
    ACTIONABLE = "actionable"


def _canonical_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise OpportunityContractError(
            f"{field_name} must be a non-empty trimmed string"
        )
    if "\x00" in value:
        raise OpportunityContractError(f"{field_name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OpportunityContractError(
            f"{field_name} must be UTF-8 encodable"
        ) from exc
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _canonical_text(value, field_name)


def _optional_sport(value: object, field_name: str = "quote sport") -> str | None:
    if value is None:
        return None
    sport = _canonical_text(value, field_name)
    if sport != sport.lower() or "|" in sport or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in sport
    ):
        raise OpportunityContractError(
            f"{field_name} must be a lowercase canonical sport identity"
        )
    return sport


def _quote_key(
    event_id: str,
    market_id: str,
    selection_id: str,
    sport: str | None,
) -> str:
    if sport is None:
        return f"{event_id}|{market_id}|{selection_id}"
    return f"sport-v1|{sport}|{event_id}|{market_id}|{selection_id}"


def _canonical_hash(value: object, field_name: str) -> str:
    text = _canonical_text(value, field_name)
    if len(text) != 64 or any(character not in _SHA256_HEX for character in text):
        raise OpportunityContractError(
            f"{field_name} must be a canonical lowercase SHA-256 digest"
        )
    return text


def _optional_hash(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _canonical_hash(value, field_name)


def _finite_decimal(
    value: object,
    field_name: str,
    *,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise OpportunityContractError(
            f"{field_name} must be an exact finite Decimal"
        )
    if nonnegative and (value < 0 or (value.is_zero() and value.is_signed())):
        raise OpportunityContractError(f"{field_name} must be non-negative")
    return value


def _decimal_from_serialized(value: object, field_name: str) -> Decimal:
    if type(value) is not str or not value or value.strip() != value:
        raise OpportunityContractError(
            f"{field_name} must be a canonical finite Decimal string"
        )
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise OpportunityContractError(
            f"{field_name} must be a canonical finite Decimal string"
        ) from exc
    if not result.is_finite() or str(result) != value:
        raise OpportunityContractError(
            f"{field_name} must be a canonical finite Decimal string"
        )
    return result


def _canonical_json_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, order=True)
class EvidenceRef:
    """Opaque pointer to evidence owned by another canonical authority."""

    authority: str
    reference: str

    def __post_init__(self) -> None:
        _canonical_text(self.authority, "evidence authority")
        _canonical_text(self.reference, "evidence reference")

    def to_dict(self) -> dict[str, str]:
        return {"authority": self.authority, "reference": self.reference}

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceRef":
        if type(raw) is not dict or set(raw) != {"authority", "reference"}:
            raise OpportunityContractError(
                "evidence reference must contain canonical fields"
            )
        return cls(
            authority=_canonical_text(raw["authority"], "evidence authority"),
            reference=_canonical_text(raw["reference"], "evidence reference"),
        )


def _sorted_unique_evidence(
    values: Iterable[EvidenceRef],
    field_name: str,
) -> tuple[EvidenceRef, ...]:
    refs = tuple(values)
    if any(not isinstance(item, EvidenceRef) for item in refs):
        raise OpportunityContractError(
            f"{field_name} must contain only EvidenceRef values"
        )
    ordered = tuple(sorted(refs))
    if len(set(ordered)) != len(ordered):
        raise OpportunityContractError(
            f"{field_name} contains duplicate references"
        )
    return ordered


@dataclass(frozen=True, slots=True)
class QuoteRef:
    """Immutable reference snapshot of one canonical MarketEvent quote."""

    event_id: str
    market_id: str
    selection_id: str
    source_id: str
    sequence: int
    decimal_odds: Decimal
    observed_ts: str
    source_ts: str | None
    ingest_ts: str
    market_event_hash: str
    market_snapshot_hash: str | None = None
    sport: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "market_id",
            "selection_id",
            "source_id",
            "observed_ts",
            "ingest_ts",
        ):
            _canonical_text(getattr(self, name), f"quote {name}")
        _optional_text(self.source_ts, "quote source_ts")
        _optional_sport(self.sport)
        if type(self.sequence) is not int or self.sequence < 0:
            raise OpportunityContractError(
                "quote sequence must be a non-negative non-boolean int"
            )
        odds = _finite_decimal(self.decimal_odds, "quote decimal_odds")
        if odds <= 1:
            raise OpportunityContractError(
                "quote decimal_odds must be greater than 1"
            )
        _canonical_hash(self.market_event_hash, "market_event_hash")
        _optional_hash(self.market_snapshot_hash, "market_snapshot_hash")

    @property
    def quote_key(self) -> str:
        return _quote_key(
            self.event_id,
            self.market_id,
            self.selection_id,
            self.sport,
        )

    @property
    def identity_key(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.sport or "",
            self.source_id,
            self.event_id,
            self.market_id,
            self.selection_id,
            self.sequence,
        )

    @classmethod
    def from_market_event(
        cls,
        event: MarketEvent,
        *,
        market_snapshot_hash: str | None = None,
    ) -> "QuoteRef":
        if not isinstance(event, MarketEvent):
            raise OpportunityContractError("quote source must be a MarketEvent")
        try:
            canonical = MarketEvent.from_dict(event.to_dict())
        except (TypeError, ValueError) as exc:
            raise OpportunityContractError(
                "quote source MarketEvent is non-canonical"
            ) from exc
        payload = canonical.to_dict()
        return cls(
            event_id=canonical.event_id,
            market_id=canonical.market_id,
            selection_id=canonical.selection_id,
            source_id=canonical.source_id,
            sequence=canonical.sequence,
            decimal_odds=canonical.decimal_odds,
            observed_ts=canonical.observed_ts,
            source_ts=canonical.source_ts,
            ingest_ts=canonical.ingest_ts,
            market_event_hash=_canonical_json_hash(payload),
            market_snapshot_hash=_optional_hash(
                market_snapshot_hash,
                "market_snapshot_hash",
            ),
            sport=canonical.sport,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "source_id": self.source_id,
            "sequence": self.sequence,
            "decimal_odds": str(self.decimal_odds),
            "observed_ts": self.observed_ts,
            "source_ts": self.source_ts,
            "ingest_ts": self.ingest_ts,
            "market_event_hash": self.market_event_hash,
            "market_snapshot_hash": self.market_snapshot_hash,
        }
        if self.sport is not None:
            payload["sport"] = self.sport
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> "QuoteRef":
        expected = {
            "event_id",
            "market_id",
            "selection_id",
            "source_id",
            "sequence",
            "decimal_odds",
            "observed_ts",
            "source_ts",
            "ingest_ts",
            "market_event_hash",
            "market_snapshot_hash",
        }
        if type(raw) is not dict or frozenset(raw) not in {
            frozenset(expected),
            frozenset(expected | {"sport"}),
        }:
            raise OpportunityContractError(
                "quote reference must contain canonical fields"
            )
        sequence = raw["sequence"]
        if type(sequence) is not int:
            raise OpportunityContractError(
                "quote sequence must be a non-boolean int"
            )
        return cls(
            event_id=_canonical_text(raw["event_id"], "quote event_id"),
            market_id=_canonical_text(raw["market_id"], "quote market_id"),
            selection_id=_canonical_text(
                raw["selection_id"], "quote selection_id"
            ),
            source_id=_canonical_text(raw["source_id"], "quote source_id"),
            sequence=sequence,
            decimal_odds=_decimal_from_serialized(
                raw["decimal_odds"], "quote decimal_odds"
            ),
            observed_ts=_canonical_text(
                raw["observed_ts"], "quote observed_ts"
            ),
            source_ts=_optional_text(raw["source_ts"], "quote source_ts"),
            ingest_ts=_canonical_text(raw["ingest_ts"], "quote ingest_ts"),
            market_event_hash=_canonical_hash(
                raw["market_event_hash"], "market_event_hash"
            ),
            market_snapshot_hash=_optional_hash(
                raw["market_snapshot_hash"], "market_snapshot_hash"
            ),
            sport=_optional_sport(raw.get("sport")),
        )


@dataclass(frozen=True, slots=True)
class ForecastRef:
    """Causal forecast evidence bound to one exact QuoteRef snapshot."""

    forecast_id: str
    forecast_hash: str
    quote_key: str
    probability: Decimal
    input_cutoff_ts: str
    market_snapshot_hash: str
    quote_market_event_hash: str

    def __post_init__(self) -> None:
        _canonical_text(self.forecast_id, "forecast_id")
        _canonical_hash(self.forecast_hash, "forecast_hash")
        _canonical_text(self.quote_key, "forecast quote_key")
        probability = _finite_decimal(
            self.probability, "forecast probability"
        )
        if probability < 0 or probability > 1:
            raise OpportunityContractError(
                "forecast probability must be between 0 and 1"
            )
        _canonical_text(self.input_cutoff_ts, "forecast input_cutoff_ts")
        _canonical_hash(self.market_snapshot_hash, "market_snapshot_hash")
        _canonical_hash(
            self.quote_market_event_hash,
            "quote_market_event_hash",
        )

    @classmethod
    def from_forecast(
        cls,
        forecast: ForecastRecord,
        quote: QuoteRef,
    ) -> "ForecastRef":
        if not isinstance(forecast, ForecastRecord):
            raise OpportunityContractError(
                "forecast source must be a ForecastRecord"
            )
        if not isinstance(quote, QuoteRef):
            raise OpportunityContractError("forecast quote must be a QuoteRef")
        if forecast.quote_key != quote.quote_key:
            raise OpportunityContractError(
                "forecast quote_key does not match bound QuoteRef"
            )
        if forecast.market_snapshot_hash is None:
            raise OpportunityContractError(
                "forecast requires canonical market_snapshot_hash evidence"
            )
        if quote.market_snapshot_hash is None:
            raise OpportunityContractError(
                "bound QuoteRef requires market_snapshot_hash for forecast evidence"
            )
        if forecast.market_snapshot_hash != quote.market_snapshot_hash:
            raise OpportunityContractError(
                "forecast market snapshot does not match bound QuoteRef"
            )
        return cls(
            forecast_id=forecast.forecast_id,
            forecast_hash=forecast.canonical_hash,
            quote_key=forecast.quote_key,
            probability=forecast.probability,
            input_cutoff_ts=forecast.input_cutoff_ts,
            market_snapshot_hash=forecast.market_snapshot_hash,
            quote_market_event_hash=quote.market_event_hash,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "forecast_id": self.forecast_id,
            "forecast_hash": self.forecast_hash,
            "quote_key": self.quote_key,
            "probability": str(self.probability),
            "input_cutoff_ts": self.input_cutoff_ts,
            "market_snapshot_hash": self.market_snapshot_hash,
            "quote_market_event_hash": self.quote_market_event_hash,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "ForecastRef":
        expected = {
            "forecast_id",
            "forecast_hash",
            "quote_key",
            "probability",
            "input_cutoff_ts",
            "market_snapshot_hash",
            "quote_market_event_hash",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise OpportunityContractError(
                "forecast reference must contain canonical fields"
            )
        return cls(
            forecast_id=_canonical_text(raw["forecast_id"], "forecast_id"),
            forecast_hash=_canonical_hash(
                raw["forecast_hash"], "forecast_hash"
            ),
            quote_key=_canonical_text(
                raw["quote_key"], "forecast quote_key"
            ),
            probability=_decimal_from_serialized(
                raw["probability"], "forecast probability"
            ),
            input_cutoff_ts=_canonical_text(
                raw["input_cutoff_ts"], "forecast input_cutoff_ts"
            ),
            market_snapshot_hash=_canonical_hash(
                raw["market_snapshot_hash"], "market_snapshot_hash"
            ),
            quote_market_event_hash=_canonical_hash(
                raw["quote_market_event_hash"], "quote_market_event_hash"
            ),
        )


@dataclass(frozen=True, slots=True)
class Opportunity:
    strategy_class: StrategyClass
    decision: OpportunityDecision
    quotes: tuple[QuoteRef, ...]
    claims_probability_edge: bool = False
    forecasts: tuple[ForecastRef, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.strategy_class, StrategyClass):
            raise OpportunityContractError(
                "strategy_class must be a StrategyClass"
            )
        if not isinstance(self.decision, OpportunityDecision):
            raise OpportunityContractError(
                "decision must be an OpportunityDecision"
            )
        if type(self.claims_probability_edge) is not bool:
            raise OpportunityContractError(
                "claims_probability_edge must be a boolean"
            )
        if (
            self.strategy_class is StrategyClass.PREDICTIVE_EDGE
            and not self.claims_probability_edge
        ):
            raise OpportunityContractError(
                "PREDICTIVE_EDGE must claim a probability edge"
            )
        if self.claims_probability_edge and self.strategy_class not in {
            StrategyClass.PREDICTIVE_EDGE,
            StrategyClass.HYBRID,
        }:
            raise OpportunityContractError(
                "probability edge is supported only for PREDICTIVE_EDGE or HYBRID"
            )

        quotes = tuple(self.quotes)
        if not quotes:
            raise OpportunityContractError(
                "opportunity requires at least one quote"
            )
        if any(not isinstance(item, QuoteRef) for item in quotes):
            raise OpportunityContractError(
                "opportunity quotes must be QuoteRef values"
            )
        quotes = tuple(sorted(quotes, key=lambda item: item.identity_key))
        identities = [item.identity_key for item in quotes]
        if len(set(identities)) != len(identities):
            raise OpportunityContractError(
                "opportunity contains duplicate quote identity"
            )
        quote_keys = [item.quote_key for item in quotes]
        if len(set(quote_keys)) != len(quote_keys):
            raise OpportunityContractError(
                "opportunity quote serialization is ambiguous across structured identities"
            )
        object.__setattr__(self, "quotes", quotes)

        forecasts = tuple(self.forecasts)
        if any(not isinstance(item, ForecastRef) for item in forecasts):
            raise OpportunityContractError(
                "opportunity forecasts must be ForecastRef values"
            )
        forecasts = tuple(
            sorted(
                forecasts,
                key=lambda item: (
                    item.quote_key,
                    item.forecast_id,
                    item.forecast_hash,
                ),
            )
        )
        forecast_ids = [item.forecast_id for item in forecasts]
        forecast_hashes = [item.forecast_hash for item in forecasts]
        if (
            len(set(forecast_ids)) != len(forecast_ids)
            or len(set(forecast_hashes)) != len(forecast_hashes)
        ):
            raise OpportunityContractError(
                "opportunity contains duplicate forecast evidence"
            )

        quote_by_key = {item.quote_key: item for item in quotes}
        for forecast in forecasts:
            quote = quote_by_key.get(forecast.quote_key)
            if quote is None:
                raise OpportunityContractError(
                    "forecast reference does not belong to an opportunity quote"
                )
            if (
                forecast.quote_market_event_hash != quote.market_event_hash
                or forecast.market_snapshot_hash != quote.market_snapshot_hash
            ):
                raise OpportunityContractError(
                    "forecast evidence does not bind the exact opportunity quote snapshot"
                )

        if self.claims_probability_edge:
            covered = {item.quote_key for item in forecasts}
            if covered != set(quote_by_key):
                raise OpportunityContractError(
                    "probability-edge opportunity requires forecast evidence for every quote"
                )
        object.__setattr__(self, "forecasts", forecasts)
        object.__setattr__(
            self,
            "evidence_refs",
            _sorted_unique_evidence(
                self.evidence_refs,
                "opportunity evidence_refs",
            ),
        )

    def _decision_independent_payload(self) -> dict[str, Any]:
        return {
            "strategy_class": self.strategy_class.value,
            "claims_probability_edge": self.claims_probability_edge,
            "quotes": [item.to_dict() for item in self.quotes],
            "forecasts": [item.to_dict() for item in self.forecasts],
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
        }

    @property
    def conflict_key(self) -> str:
        """Stable identity for one evidence-defined opportunity before decision state."""

        return _canonical_json_hash(self._decision_independent_payload())

    @property
    def opportunity_id(self) -> str:
        return _canonical_json_hash(self._identity_payload())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            **self._decision_independent_payload(),
            "decision": self.decision.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            **self._identity_payload(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "Opportunity":
        expected = {
            "opportunity_id",
            "strategy_class",
            "decision",
            "claims_probability_edge",
            "quotes",
            "forecasts",
            "evidence_refs",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise OpportunityContractError(
                "opportunity must contain canonical fields"
            )
        try:
            strategy_class = StrategyClass(raw["strategy_class"])
            decision = OpportunityDecision(raw["decision"])
        except (TypeError, ValueError) as exc:
            raise OpportunityContractError(
                "opportunity enum value is unsupported"
            ) from exc
        if type(raw["claims_probability_edge"]) is not bool:
            raise OpportunityContractError(
                "claims_probability_edge must be a boolean"
            )
        if (
            type(raw["quotes"]) is not list
            or type(raw["forecasts"]) is not list
            or type(raw["evidence_refs"]) is not list
        ):
            raise OpportunityContractError(
                "opportunity collections must be JSON arrays"
            )
        result = cls(
            strategy_class=strategy_class,
            decision=decision,
            quotes=tuple(
                QuoteRef.from_dict(item) for item in raw["quotes"]
            ),
            claims_probability_edge=raw["claims_probability_edge"],
            forecasts=tuple(
                ForecastRef.from_dict(item) for item in raw["forecasts"]
            ),
            evidence_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in raw["evidence_refs"]
            ),
        )
        expected_id = _canonical_hash(
            raw["opportunity_id"], "opportunity_id"
        )
        if result.opportunity_id != expected_id:
            raise OpportunityContractError(
                "opportunity_id does not match canonical contents"
            )
        return result


@dataclass(frozen=True, slots=True)
class OpportunitySet:
    opportunities: tuple[Opportunity, ...]

    def __post_init__(self) -> None:
        values = tuple(self.opportunities)
        if len(values) > _MAX_OPPORTUNITIES:
            raise OpportunityContractError(
                f"opportunity set exceeds {_MAX_OPPORTUNITIES} members"
            )
        if any(not isinstance(item, Opportunity) for item in values):
            raise OpportunityContractError(
                "opportunity set must contain only Opportunity values"
            )
        ordered = tuple(
            sorted(values, key=lambda item: item.opportunity_id)
        )
        ids = [item.opportunity_id for item in ordered]
        if len(set(ids)) != len(ids):
            raise OpportunityContractError(
                "opportunity set contains duplicate members"
            )
        conflict_keys = [item.conflict_key for item in ordered]
        if len(set(conflict_keys)) != len(conflict_keys):
            raise OpportunityContractError(
                "opportunity set contains conflicting decisions for the same canonical opportunity"
            )
        object.__setattr__(self, "opportunities", ordered)

    @property
    def opportunity_set_id(self) -> str:
        return _canonical_json_hash(
            {
                "opportunity_ids": [
                    item.opportunity_id for item in self.opportunities
                ]
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_set_id": self.opportunity_set_id,
            "opportunities": [
                item.to_dict() for item in self.opportunities
            ],
        }

    @classmethod
    def from_dict(cls, raw: object) -> "OpportunitySet":
        if (
            type(raw) is not dict
            or set(raw) != {"opportunity_set_id", "opportunities"}
        ):
            raise OpportunityContractError(
                "opportunity set must contain canonical fields"
            )
        if type(raw["opportunities"]) is not list:
            raise OpportunityContractError(
                "opportunities must be a JSON array"
            )
        result = cls(
            opportunities=tuple(
                Opportunity.from_dict(item)
                for item in raw["opportunities"]
            )
        )
        expected_id = _canonical_hash(
            raw["opportunity_set_id"], "opportunity_set_id"
        )
        if result.opportunity_set_id != expected_id:
            raise OpportunityContractError(
                "opportunity_set_id does not match canonical members"
            )
        return result


@dataclass(frozen=True, slots=True)
class PlanAllocation:
    opportunity_id: str
    stake: Decimal

    def __post_init__(self) -> None:
        _canonical_hash(
            self.opportunity_id,
            "allocation opportunity_id",
        )
        _finite_decimal(
            self.stake,
            "allocation stake",
            nonnegative=True,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "opportunity_id": self.opportunity_id,
            "stake": str(self.stake),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PlanAllocation":
        if (
            type(raw) is not dict
            or set(raw) != {"opportunity_id", "stake"}
        ):
            raise OpportunityContractError(
                "allocation must contain canonical fields"
            )
        return cls(
            opportunity_id=_canonical_hash(
                raw["opportunity_id"],
                "allocation opportunity_id",
            ),
            stake=_decimal_from_serialized(
                raw["stake"],
                "allocation stake",
            ),
        )


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    """Non-executing stake-vector snapshot bound to upstream authorities."""

    opportunity_set: OpportunitySet
    allocations: tuple[PlanAllocation, ...] = ()
    portfolio_evidence_refs: tuple[EvidenceRef, ...] = field(
        default_factory=tuple
    )
    risk_evidence_refs: tuple[EvidenceRef, ...] = field(
        default_factory=tuple
    )
    ledger_state_refs: tuple[EvidenceRef, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity_set, OpportunitySet):
            raise OpportunityContractError(
                "opportunity_set must be an OpportunitySet"
            )

        allocations = tuple(self.allocations)
        if any(
            not isinstance(item, PlanAllocation) for item in allocations
        ):
            raise OpportunityContractError(
                "allocations must contain only PlanAllocation values"
            )
        allocations = tuple(
            sorted(allocations, key=lambda item: item.opportunity_id)
        )
        allocation_ids = [item.opportunity_id for item in allocations]
        if len(set(allocation_ids)) != len(allocation_ids):
            raise OpportunityContractError(
                "portfolio plan contains duplicate allocations"
            )
        members = {
            opportunity.opportunity_id: opportunity
            for opportunity in self.opportunity_set.opportunities
        }
        for allocation in allocations:
            if allocation.opportunity_id not in members:
                raise OpportunityContractError(
                    "portfolio plan references an opportunity outside its canonical set"
                )
            if (
                allocation.stake > 0
                and members[allocation.opportunity_id].decision
                is not OpportunityDecision.ACTIONABLE
            ):
                raise OpportunityContractError(
                    "WAIT/ZERO opportunity cannot receive positive stake"
                )
        object.__setattr__(self, "allocations", allocations)

        portfolio_refs = _sorted_unique_evidence(
            self.portfolio_evidence_refs,
            "portfolio_evidence_refs",
        )
        risk_refs = _sorted_unique_evidence(
            self.risk_evidence_refs,
            "risk_evidence_refs",
        )
        ledger_refs = _sorted_unique_evidence(
            self.ledger_state_refs,
            "ledger_state_refs",
        )
        if any(allocation.stake > 0 for allocation in allocations):
            if not portfolio_refs or not risk_refs or not ledger_refs:
                raise OpportunityContractError(
                    "positive allocation requires portfolio, risk, and ledger-state evidence"
                )
        object.__setattr__(
            self,
            "portfolio_evidence_refs",
            portfolio_refs,
        )
        object.__setattr__(self, "risk_evidence_refs", risk_refs)
        object.__setattr__(self, "ledger_state_refs", ledger_refs)

    @property
    def plan_id(self) -> str:
        return _canonical_json_hash(self._identity_payload())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "opportunity_set_id": self.opportunity_set.opportunity_set_id,
            "allocations": [item.to_dict() for item in self.allocations],
            "portfolio_evidence_refs": [
                item.to_dict() for item in self.portfolio_evidence_refs
            ],
            "risk_evidence_refs": [
                item.to_dict() for item in self.risk_evidence_refs
            ],
            "ledger_state_refs": [
                item.to_dict() for item in self.ledger_state_refs
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "opportunity_set": self.opportunity_set.to_dict(),
            **self._identity_payload(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "PortfolioPlan":
        expected = {
            "plan_id",
            "opportunity_set",
            "opportunity_set_id",
            "allocations",
            "portfolio_evidence_refs",
            "risk_evidence_refs",
            "ledger_state_refs",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise OpportunityContractError(
                "portfolio plan must contain canonical fields"
            )
        for name in (
            "allocations",
            "portfolio_evidence_refs",
            "risk_evidence_refs",
            "ledger_state_refs",
        ):
            if type(raw[name]) is not list:
                raise OpportunityContractError(
                    f"{name} must be a JSON array"
                )
        opportunity_set = OpportunitySet.from_dict(
            raw["opportunity_set"]
        )
        serialized_set_id = _canonical_hash(
            raw["opportunity_set_id"],
            "opportunity_set_id",
        )
        if opportunity_set.opportunity_set_id != serialized_set_id:
            raise OpportunityContractError(
                "portfolio plan opportunity_set_id does not match embedded set"
            )
        result = cls(
            opportunity_set=opportunity_set,
            allocations=tuple(
                PlanAllocation.from_dict(item)
                for item in raw["allocations"]
            ),
            portfolio_evidence_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in raw["portfolio_evidence_refs"]
            ),
            risk_evidence_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in raw["risk_evidence_refs"]
            ),
            ledger_state_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in raw["ledger_state_refs"]
            ),
        )
        expected_plan_id = _canonical_hash(raw["plan_id"], "plan_id")
        if result.plan_id != expected_plan_id:
            raise OpportunityContractError(
                "plan_id does not match canonical contents"
            )
        return result
