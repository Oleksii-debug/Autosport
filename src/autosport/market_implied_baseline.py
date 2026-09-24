"""Causal market-implied forecast-baseline evidence.

Probability vectors are derived from canonical durable market replay and a typed outcome
roster authority.  Current main does not prove the provider origin of the Betfair
marketDefinition bytes used by the public roster issuer, so this layer explicitly keeps
outcome-roster origin/completeness false while preserving fail-closed contradiction
checks.  This module owns no scoring, promotion, execution, fill, liquidity, or
profitability authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Mapping, Sequence

from .external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
)
from .market_mirror import MarketMirror
from .market_outcomes import MarketSettlementOutcomeAuthority
from .opportunity import QuoteRef
from .storage import SQLiteMarketStore


class MarketImpliedBaselineError(ValueError):
    pass


METHOD_ID = "autosport.market-implied.proportional-reciprocal-devig.v1"
_OBSERVATION_TOKEN = object()
_COHORT_TOKEN = object()

# The public builder accepts product objects, but the resulting evidence claims
# canonical durable-history replay. Subclasses or instance-shadowed readers can
# otherwise replace events() while still satisfying isinstance(), fabricating
# a self-consistent comparator from process-local bytes.
_STORE_TYPE = SQLiteMarketStore
_STORE_EVENTS = _STORE_TYPE.events
_OUTCOME_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_OUTCOME_ASSERT_AVAILABLE = _OUTCOME_AUTHORITY_TYPE.assert_available_as_of


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise MarketImpliedBaselineError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if text != text.lower() or len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise MarketImpliedBaselineError(f"{name} must be canonical SHA-256 hex")
    return text


def _digest(payload: Mapping[str, Any] | Sequence[Any]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _utc(value: datetime, name: str) -> str:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise MarketImpliedBaselineError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_utc(value: object) -> datetime | None:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_us(value: timedelta) -> int:
    if not isinstance(value, timedelta):
        raise TypeError("max_age must be a timedelta")
    if value < timedelta(0):
        raise MarketImpliedBaselineError("max_age must be non-negative")
    return value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds


def market_implied_baseline_config_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "autosport_market_implied_baseline_config",
        "method_id": METHOD_ID,
        "odds_representation": "decimal",
        "implied_weight": "exact_reciprocal_decimal_odds",
        "normalization": "exact_sum_to_one_over_typed_roster_after_durable_contradiction_checks",
        "quote_visibility": "MarketMirror.replay_view_from_store",
        "outcome_roster_authority": "MarketSettlementOutcomeAuthority",
        "outcome_roster_origin_verified": False,
        "forecast_comparator_only": True,
        "execution_authority": False,
        "promotion_authority": False,
        "real_money_execution": False,
    }


def market_implied_baseline_config_sha256() -> str:
    return _digest(market_implied_baseline_config_payload())


@dataclass(frozen=True, slots=True)
class ExactMarketProbability:
    selection_id: str
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _text(self.selection_id, "selection_id")
        if type(self.numerator) is not int or type(self.denominator) is not int:
            raise MarketImpliedBaselineError("probability fraction must use exact integers")
        if self.numerator <= 0 or self.denominator <= 0:
            raise MarketImpliedBaselineError("probability fraction must be positive")
        reduced = Fraction(self.numerator, self.denominator)
        if (reduced.numerator, reduced.denominator) != (self.numerator, self.denominator):
            raise MarketImpliedBaselineError("probability fraction must be reduced")
        if reduced >= 1:
            raise MarketImpliedBaselineError("selection probability must be below one")

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    def to_dict(self) -> dict[str, object]:
        return {
            "selection_id": self.selection_id,
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass(frozen=True, slots=True)
class MarketImpliedBaselineEvidence:
    cohort_key: str
    decision_cutoff: str
    max_age_microseconds: int
    outcome_authority_sha256: str
    sport: str
    event_id: str
    market_id: str
    source_id: str
    market_type: str
    quote_snapshot_sha256: str
    quotes: tuple[QuoteRef, ...]
    overround_numerator: int
    overround_denominator: int
    probabilities: tuple[ExactMarketProbability, ...]
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _OBSERVATION_TOKEN:
            raise TypeError(
                "MarketImpliedBaselineEvidence must be issued from canonical market history"
            )
        for name in (
            "cohort_key", "decision_cutoff", "sport", "event_id", "market_id",
            "source_id", "market_type",
        ):
            _text(getattr(self, name), name)
        _sha(self.outcome_authority_sha256, "outcome_authority_sha256")
        _sha(self.quote_snapshot_sha256, "quote_snapshot_sha256")
        if type(self.max_age_microseconds) is not int or self.max_age_microseconds < 0:
            raise MarketImpliedBaselineError("max_age_microseconds must be non-negative")
        if type(self.quotes) is not tuple or len(self.quotes) < 2:
            raise MarketImpliedBaselineError("at least two canonical quotes are required")
        if any(not isinstance(q, QuoteRef) for q in self.quotes):
            raise MarketImpliedBaselineError("quotes must contain QuoteRef values")
        selections = tuple(q.selection_id for q in self.quotes)
        if selections != tuple(sorted(selections)) or len(set(selections)) != len(selections):
            raise MarketImpliedBaselineError("quotes must use unique canonical selection order")
        if tuple(p.selection_id for p in self.probabilities) != selections:
            raise MarketImpliedBaselineError("probabilities must match quote selections")
        if sum((p.fraction for p in self.probabilities), Fraction()) != Fraction(1):
            raise MarketImpliedBaselineError("probability vector must sum exactly to one")
        overround = Fraction(self.overround_numerator, self.overround_denominator)
        if (
            self.overround_numerator <= 0
            or self.overround_denominator <= 0
            or (overround.numerator, overround.denominator)
            != (self.overround_numerator, self.overround_denominator)
        ):
            raise MarketImpliedBaselineError("overround must be a positive reduced fraction")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport.market-implied-baseline-observation.v1",
            "cohort_key": self.cohort_key,
            "decision_cutoff": self.decision_cutoff,
            "max_age_microseconds": self.max_age_microseconds,
            "outcome_authority_sha256": self.outcome_authority_sha256,
            "sport": self.sport,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "source_id": self.source_id,
            "market_type": self.market_type,
            "quote_snapshot_sha256": self.quote_snapshot_sha256,
            "quotes": [q.to_dict() for q in self.quotes],
            "method_id": METHOD_ID,
            "overround": {
                "numerator": self.overround_numerator,
                "denominator": self.overround_denominator,
            },
            "probabilities": [p.to_dict() for p in self.probabilities],
            "truth": {
                "outcome_roster_schema_authority_present": True,
                "outcome_roster_origin_verified": False,
                "complete_verified_outcome_roster": False,
                "durable_history_roster_contradiction_absent": True,
                "decision_time_replay_visibility": True,
                "fresh_open_quotes": True,
                "source_stream_continuity_proven": False,
                "forecast_comparator_only": True,
                "execution_authority": False,
                "promotion_authority": False,
                "real_money_execution": False,
            },
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self._payload())

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        verified_evidence: "MarketImpliedBaselineEvidence" | None = None,
    ) -> "MarketImpliedBaselineEvidence":
        if not isinstance(verified_evidence, cls):
            raise MarketImpliedBaselineError(
                "readback requires independently rebuilt market-implied evidence"
            )
        if type(raw) is not dict or raw != verified_evidence.to_dict():
            raise MarketImpliedBaselineError(
                "serialized market-implied evidence does not match rebuilt evidence"
            )
        return verified_evidence


def _snapshot_hash(quotes: tuple[QuoteRef, ...]) -> str:
    return _digest(
        {
            "schema": "autosport.market-implied-quote-snapshot.v1",
            "quotes": [q.to_dict() for q in quotes],
        }
    )


def _probabilities(
    quotes: tuple[QuoteRef, ...],
) -> tuple[Fraction, tuple[ExactMarketProbability, ...]]:
    raw = tuple((q.selection_id, Fraction(1, 1) / Fraction(q.decimal_odds)) for q in quotes)
    overround = sum((weight for _selection, weight in raw), Fraction())
    if overround <= 0:
        raise MarketImpliedBaselineError("market-implied overround must be positive")
    values = tuple(
        ExactMarketProbability(
            selection_id=selection,
            numerator=(weight / overround).numerator,
            denominator=(weight / overround).denominator,
        )
        for selection, weight in raw
    )
    return overround, values


def _require_canonical_inputs(
    store: SQLiteMarketStore,
    outcome_authority: MarketSettlementOutcomeAuthority,
) -> None:
    if type(store) is not _STORE_TYPE:
        raise TypeError("store must be exact SQLiteMarketStore")
    namespace = getattr(store, "__dict__", None)
    if isinstance(namespace, dict) and "events" in namespace:
        raise MarketImpliedBaselineError(
            "canonical market store must not shadow events reader"
        )
    if _STORE_TYPE.events is not _STORE_EVENTS:
        raise MarketImpliedBaselineError(
            "canonical market store events authority was rebound"
        )
    if type(outcome_authority) is not _OUTCOME_AUTHORITY_TYPE:
        raise TypeError(
            "outcome_authority must be exact MarketSettlementOutcomeAuthority"
        )
    if _OUTCOME_AUTHORITY_TYPE.assert_available_as_of is not _OUTCOME_ASSERT_AVAILABLE:
        raise MarketImpliedBaselineError(
            "market outcome availability authority was rebound"
        )


def build_market_implied_baseline_evidence(
    *,
    cohort_key: str,
    store: SQLiteMarketStore,
    outcome_authority: MarketSettlementOutcomeAuthority,
    decision_cutoff: datetime,
    max_age: timedelta,
) -> MarketImpliedBaselineEvidence:
    """Issue one decision-time probability vector; roster-origin truth remains false."""

    key = _text(cohort_key, "cohort_key")
    _require_canonical_inputs(store, outcome_authority)
    cutoff = _utc(decision_cutoff, "decision_cutoff")
    age_us = _age_us(max_age)
    try:
        _OUTCOME_ASSERT_AVAILABLE(outcome_authority, decision_cutoff)
    except (TypeError, ValueError) as exc:
        raise MarketImpliedBaselineError(
            "verified outcome roster is not causally available at decision cutoff"
        ) from exc

    identity = outcome_authority.identity
    decision_boundary = decision_cutoff.astimezone(timezone.utc)
    causally_known_selections: set[str] = set()
    for event in store.events():
        if (
            event.source_id != identity.source_id
            or event.sport != identity.sport
            or event.event_id != identity.event_id
            or event.market_id != identity.market_id
        ):
            continue
        observed = _event_utc(event.observed_ts)
        ingested = _event_utc(event.ingest_ts)
        if observed is None or ingested is None:
            continue
        if observed > decision_boundary or ingested > decision_boundary:
            continue
        if event.market_type is not identity.market_type:
            raise MarketImpliedBaselineError(
                "canonical durable market identity contradicts verified outcome authority"
            )
        causally_known_selections.add(event.selection_id)
    if causally_known_selections.difference(outcome_authority.selection_ids):
        raise MarketImpliedBaselineError(
            "canonical durable market history contradicts verified outcome roster"
        )

    # Replay the complete decision-visible canonical market before applying the
    # asserted outcome roster.  Filtering by outcome_authority.selection_ids first
    # would let an incomplete/caller-minted roster erase a real selection that is
    # already present in durable market history.
    snapshot = MarketMirror.replay_view_from_store(
        store,
        as_of=decision_cutoff,
        max_age=max_age,
        source_ids=identity.source_id,
        sports=identity.sport,
        event_ids=identity.event_id,
        market_ids=identity.market_id,
    )
    by_selection = {event.selection_id: event for event in snapshot.events}
    if len(by_selection) != len(snapshot.events):
        raise MarketImpliedBaselineError("decision-visible snapshot has duplicate selections")
    if any(event.market_type is not identity.market_type for event in snapshot.events):
        raise MarketImpliedBaselineError(
            "decision-visible market identity contradicts verified outcome authority"
        )
    if tuple(sorted(by_selection)) != outcome_authority.selection_ids:
        raise MarketImpliedBaselineError(
            "complete fresh open quote set is unavailable or contradicts verified outcome roster"
        )
    events = tuple(by_selection[selection] for selection in outcome_authority.selection_ids)
    for event in events:
        if (
            event.sport != identity.sport
            or event.event_id != identity.event_id
            or event.market_id != identity.market_id
            or event.source_id != identity.source_id
            or event.market_type is not identity.market_type
        ):
            raise MarketImpliedBaselineError(
                "quote identity does not match verified outcome authority"
            )

    quotes = tuple(QuoteRef.from_market_event(event) for event in events)
    snapshot_sha = _snapshot_hash(quotes)
    overround, probabilities = _probabilities(quotes)
    return MarketImpliedBaselineEvidence(
        cohort_key=key,
        decision_cutoff=cutoff,
        max_age_microseconds=age_us,
        outcome_authority_sha256=outcome_authority.authority_sha256,
        sport=identity.sport,
        event_id=identity.event_id,
        market_id=identity.market_id,
        source_id=identity.source_id,
        market_type=identity.market_type.value,
        quote_snapshot_sha256=snapshot_sha,
        quotes=quotes,
        overround_numerator=overround.numerator,
        overround_denominator=overround.denominator,
        probabilities=probabilities,
        _token=_OBSERVATION_TOKEN,
    )


def market_implied_evidence_manifest_sha256(
    evidence: Sequence[MarketImpliedBaselineEvidence],
) -> str:
    if isinstance(evidence, (str, bytes)):
        raise TypeError("evidence must be a sequence")
    rows = tuple(evidence)
    if not rows or any(not isinstance(row, MarketImpliedBaselineEvidence) for row in rows):
        raise MarketImpliedBaselineError("manifest requires issued observation evidence")
    keys = tuple(row.cohort_key for row in rows)
    if keys != tuple(sorted(keys)) or len(set(keys)) != len(keys):
        raise MarketImpliedBaselineError("evidence rows must use unique canonical cohort order")
    return _digest(
        {
            "schema": "autosport.market-implied-baseline-manifest.v1",
            "method_id": METHOD_ID,
            "rows": [
                {"cohort_key": row.cohort_key, "evidence_sha256": row.evidence_sha256}
                for row in rows
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class MarketImpliedBaselineCohortEvidence:
    protocol_sha256: str
    baseline_definition_sha256: str
    evidence_scope_sha256: str
    cohort_sha256: str
    market_evidence_sha256: str
    config_sha256: str
    row_evidence_sha256: tuple[str, ...]
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _COHORT_TOKEN:
            raise TypeError(
                "MarketImpliedBaselineCohortEvidence must be issued by protocol binding"
            )
        for name in (
            "protocol_sha256", "baseline_definition_sha256", "evidence_scope_sha256",
            "cohort_sha256", "market_evidence_sha256", "config_sha256",
        ):
            _sha(getattr(self, name), name)
        if type(self.row_evidence_sha256) is not tuple or not self.row_evidence_sha256:
            raise MarketImpliedBaselineError("row evidence identities are required")
        for value in self.row_evidence_sha256:
            _sha(value, "row_evidence_sha256")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "autosport.market-implied-baseline-cohort.v1",
            "protocol_sha256": self.protocol_sha256,
            "baseline_definition_sha256": self.baseline_definition_sha256,
            "evidence_scope_sha256": self.evidence_scope_sha256,
            "cohort_sha256": self.cohort_sha256,
            "market_evidence_sha256": self.market_evidence_sha256,
            "config_sha256": self.config_sha256,
            "method_id": METHOD_ID,
            "row_evidence_sha256": list(self.row_evidence_sha256),
            "truth": {
                "same_frozen_cohort": False,
                "same_frozen_cohort_labels": True,
                "canonical_evaluation_universe_bound": False,
                "outcome_roster_origin_verified": False,
                "scientific_completeness_proven": False,
                "same_frozen_market_evidence": True,
                "source_stream_continuity_proven": False,
                "forecast_comparator_only": True,
                "metric_computed": False,
                "execution_authority": False,
                "promotion_authority": False,
                "real_money_execution": False,
            },
        }

    @property
    def evidence_sha256(self) -> str:
        return _digest(self._payload())

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "evidence_sha256": self.evidence_sha256}

    @classmethod
    def from_dict(
        cls,
        raw: object,
        *,
        verified_evidence: "MarketImpliedBaselineCohortEvidence" | None = None,
    ) -> "MarketImpliedBaselineCohortEvidence":
        if not isinstance(verified_evidence, cls):
            raise MarketImpliedBaselineError(
                "readback requires independently rebuilt cohort qualification"
            )
        if type(raw) is not dict or raw != verified_evidence.to_dict():
            raise MarketImpliedBaselineError(
                "serialized cohort evidence does not match rebuilt qualification"
            )
        return verified_evidence


def bind_market_implied_baseline_cohort(
    *,
    protocol: FrozenBaselineProtocol,
    baseline_definition: BaselineDefinition,
    evidence: Sequence[MarketImpliedBaselineEvidence],
) -> MarketImpliedBaselineCohortEvidence:
    """Bind caller labels/manifest to a protocol without claiming canonical row identity.

    This structural step is intentionally insufficient for positive same-cohort truth.
    Consumers that need canonical evaluation membership must additionally pass through
    ``market_implied_universe_binding.bind_market_implied_baseline_to_evaluation_universe``.
    Outcome-roster provider origin also remains unproven on current main.
    """

    if not isinstance(protocol, FrozenBaselineProtocol):
        raise TypeError("protocol must be FrozenBaselineProtocol")
    if not isinstance(baseline_definition, BaselineDefinition):
        raise TypeError("baseline_definition must be BaselineDefinition")
    if baseline_definition.kind is not BaselineKind.MARKET_IMPLIED_DEVIG:
        raise MarketImpliedBaselineError("baseline must be MARKET_IMPLIED_DEVIG")
    if not baseline_definition.supported:
        raise MarketImpliedBaselineError("frozen market-implied baseline is unsupported")
    frozen = next(
        (item for item in protocol.baselines if item.kind is BaselineKind.MARKET_IMPLIED_DEVIG),
        None,
    )
    if frozen != baseline_definition:
        raise MarketImpliedBaselineError("baseline definition differs from frozen protocol")
    if protocol.evaluation_contract_family is not EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE:
        raise MarketImpliedBaselineError("market-implied probability baseline is forecast-only")
    config_sha = market_implied_baseline_config_sha256()
    if baseline_definition.config_sha256 != config_sha:
        raise MarketImpliedBaselineError("baseline config does not match built-in frozen method")

    rows = tuple(evidence)
    if any(not isinstance(row, MarketImpliedBaselineEvidence) for row in rows):
        raise MarketImpliedBaselineError("cohort accepts issued observation evidence only")
    if tuple(row.cohort_key for row in rows) != protocol.evidence_scope.cohort_keys:
        raise MarketImpliedBaselineError("evidence must cover the exact frozen cohort")
    manifest_sha = market_implied_evidence_manifest_sha256(rows)
    if manifest_sha != protocol.evidence_scope.market_evidence_sha256:
        raise MarketImpliedBaselineError("evidence manifest differs from frozen market evidence")

    return MarketImpliedBaselineCohortEvidence(
        protocol_sha256=protocol.identity_sha256,
        baseline_definition_sha256=baseline_definition.definition_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        cohort_sha256=protocol.evidence_scope.cohort_sha256,
        market_evidence_sha256=manifest_sha,
        config_sha256=config_sha,
        row_evidence_sha256=tuple(row.evidence_sha256 for row in rows),
        _token=_COHORT_TOKEN,
    )
