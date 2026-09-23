from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from enum import StrEnum
from typing import Iterable

from .evaluation_universe import (
    EvaluationUniverseIntegrityError,
    EvaluationUniverseLedger,
    FunnelStage,
)
from .paper_execution_reality import PaperAttemptOutcome


PROTOCOL = "autosport-execution-quality-evidence/v2"
DERIVED_DECIMAL_PRECISION = 50
DERIVED_DECIMAL_ROUNDING = "ROUND_HALF_EVEN"


class ExecutionEvidencePlane(StrEnum):
    PAPER_EXECUTION_MODEL = "PAPER_EXECUTION_MODEL"


class PriceMovement(StrEnum):
    HIGHER_ODDS = "HIGHER_ODDS"
    SAME_ODDS = "SAME_ODDS"
    LOWER_ODDS = "LOWER_ODDS"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceReadinessStatus(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ClockStatus(StrEnum):
    CLOCK_DOMAIN_UNPROVEN = "CLOCK_DOMAIN_UNPROVEN"


class ExecutionEconomicsStatus(StrEnum):
    EXECUTION_ECONOMICS_MISSING = "EXECUTION_ECONOMICS_MISSING"


_OUTCOME_STAGE = {
    FunnelStage.ACCEPTED: PaperAttemptOutcome.ACCEPTED,
    FunnelStage.PARTIAL: PaperAttemptOutcome.PARTIAL,
    FunnelStage.REJECTED: PaperAttemptOutcome.REJECTED,
    FunnelStage.UNKNOWN: PaperAttemptOutcome.UNKNOWN,
}
_OUTCOME_STAGES = frozenset(_OUTCOME_STAGE)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _derived_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = DERIVED_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return numerator / denominator


def _nearest_rank(values: tuple[Decimal, ...], percentile: int) -> Decimal | None:
    if not values:
        return None
    rank = max(1, math.ceil((percentile / 100) * len(values)))
    return values[rank - 1]


@dataclass(frozen=True, slots=True)
class NumericDistribution:
    count: int
    minimum: Decimal | None
    p50: Decimal | None
    p95: Decimal | None
    p99: Decimal | None
    maximum: Decimal | None

    @classmethod
    def from_values(cls, values: Iterable[Decimal | int]) -> "NumericDistribution":
        ordered = tuple(sorted(Decimal(value) for value in values))
        if not ordered:
            return cls(0, None, None, None, None, None)
        return cls(
            count=len(ordered),
            minimum=ordered[0],
            p50=_nearest_rank(ordered, 50),
            p95=_nearest_rank(ordered, 95),
            p99=_nearest_rank(ordered, 99),
            maximum=ordered[-1],
        )

    def to_payload(self) -> dict[str, object]:
        def render(value: Decimal | None) -> str | None:
            return None if value is None else _decimal_text(value)

        return {
            "count": self.count,
            "min": render(self.minimum),
            "p50": render(self.p50),
            "p95": render(self.p95),
            "p99": render(self.p99),
            "max": render(self.maximum),
        }


@dataclass(frozen=True, slots=True)
class PaperExecutionQualitySample:
    row_id: str
    row_key: str
    run_id: str
    action_id: str
    attempt_id: str
    event_id: str
    sport: str
    market_id: str
    selection_id: str
    provider_id: str
    decision_source_provider_id: str
    side: str
    outcome: PaperAttemptOutcome
    decision_odds: Decimal
    requested_stake: Decimal
    execution_odds: Decimal | None
    execution_stake: Decimal | None
    fill_ratio: Decimal | None
    signed_odds_delta: Decimal | None
    signed_price_spread_bps: Decimal | None
    signed_adverse_price_slippage_bps: Decimal | None
    price_movement: PriceMovement
    model_delay_ms: int
    model_quote_age_ms: int
    evidence_grade: str
    evidence_source: str
    evidence_id: str | None
    evidence_sha256: str | None
    execution_reality_sha256: str
    evidence_plane: ExecutionEvidencePlane = ExecutionEvidencePlane.PAPER_EXECUTION_MODEL

    def to_payload(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "row_key": self.row_key,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "event_id": self.event_id,
            "sport": self.sport,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "provider_id": self.provider_id,
            "decision_source_provider_id": self.decision_source_provider_id,
            "side": self.side,
            "outcome": self.outcome.value,
            "decision_odds": _decimal_text(self.decision_odds),
            "requested_stake": _decimal_text(self.requested_stake),
            "execution_odds": (
                None if self.execution_odds is None else _decimal_text(self.execution_odds)
            ),
            "execution_stake": (
                None if self.execution_stake is None else _decimal_text(self.execution_stake)
            ),
            "fill_ratio": (
                None if self.fill_ratio is None else _decimal_text(self.fill_ratio)
            ),
            "signed_odds_delta": (
                None
                if self.signed_odds_delta is None
                else _decimal_text(self.signed_odds_delta)
            ),
            "signed_price_spread_bps": (
                None
                if self.signed_price_spread_bps is None
                else _decimal_text(self.signed_price_spread_bps)
            ),
            "signed_adverse_price_slippage_bps": (
                None
                if self.signed_adverse_price_slippage_bps is None
                else _decimal_text(self.signed_adverse_price_slippage_bps)
            ),
            "price_movement": self.price_movement.value,
            "model_delay_ms": self.model_delay_ms,
            "model_quote_age_ms": self.model_quote_age_ms,
            "evidence_grade": self.evidence_grade,
            "evidence_source": self.evidence_source,
            "evidence_id": self.evidence_id,
            "evidence_sha256": self.evidence_sha256,
            "execution_reality_sha256": self.execution_reality_sha256,
            "evidence_plane": self.evidence_plane.value,
        }


@dataclass(frozen=True, slots=True)
class PaperExecutionQualitySlice:
    provider_id: str
    sport: str
    market_id: str
    sample_count: int
    outcome_counts: tuple[tuple[str, int], ...]
    fill_ratio: NumericDistribution
    signed_price_spread_bps: NumericDistribution
    signed_adverse_price_slippage_bps: NumericDistribution

    def to_payload(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "sport": self.sport,
            "market_id": self.market_id,
            "sample_count": self.sample_count,
            "outcome_counts": dict(self.outcome_counts),
            "fill_ratio": self.fill_ratio.to_payload(),
            "signed_price_spread_bps": self.signed_price_spread_bps.to_payload(),
            "signed_adverse_price_slippage_bps": (
                self.signed_adverse_price_slippage_bps.to_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class PaperExecutionQualityReport:
    ledger_sha256: str
    universe_sha256: str
    frozen_denominator_rows: int
    execution_model_eligible_rows: int
    attempted_rows: int
    outcome_observation_rows: int
    outcome_counts: tuple[tuple[str, int], ...]
    price_observation_count: int
    price_observation_missing_count: int
    fill_observation_count: int
    fill_observation_missing_count: int
    real_latency_observation_count: int
    clock_status: ClockStatus
    quality_status: EvidenceReadinessStatus
    live_execution_economics_status: ExecutionEconomicsStatus
    model_delay_ms: NumericDistribution
    model_quote_age_ms: NumericDistribution
    fill_ratio: NumericDistribution
    signed_price_spread_bps: NumericDistribution
    signed_adverse_price_slippage_bps: NumericDistribution
    slices: tuple[PaperExecutionQualitySlice, ...]
    samples: tuple[PaperExecutionQualitySample, ...]
    protocol: str = PROTOCOL
    evidence_plane: ExecutionEvidencePlane = ExecutionEvidencePlane.PAPER_EXECUTION_MODEL
    percentile_method: str = "nearest_rank"

    @property
    def report_sha256(self) -> str:
        return _digest(self.to_payload(include_digest=False))

    def to_payload(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol": self.protocol,
            "evidence_plane": self.evidence_plane.value,
            "ledger_sha256": self.ledger_sha256,
            "universe_sha256": self.universe_sha256,
            "frozen_denominator_rows": self.frozen_denominator_rows,
            "execution_model_eligible_rows": self.execution_model_eligible_rows,
            "attempted_rows": self.attempted_rows,
            "outcome_observation_rows": self.outcome_observation_rows,
            "outcome_counts": dict(self.outcome_counts),
            "price_observation_count": self.price_observation_count,
            "price_observation_missing_count": self.price_observation_missing_count,
            "fill_observation_count": self.fill_observation_count,
            "fill_observation_missing_count": self.fill_observation_missing_count,
            "real_latency_observation_count": self.real_latency_observation_count,
            "clock_status": self.clock_status.value,
            "quality_status": self.quality_status.value,
            "live_execution_economics_status": self.live_execution_economics_status.value,
            "percentile_method": self.percentile_method,
            "derived_decimal_arithmetic": {
                "precision": DERIVED_DECIMAL_PRECISION,
                "rounding": DERIVED_DECIMAL_ROUNDING,
            },
            "distributions": {
                "model_delay_ms": self.model_delay_ms.to_payload(),
                "model_quote_age_ms": self.model_quote_age_ms.to_payload(),
                "fill_ratio": self.fill_ratio.to_payload(),
                "signed_price_spread_bps": self.signed_price_spread_bps.to_payload(),
                "signed_adverse_price_slippage_bps": (
                    self.signed_adverse_price_slippage_bps.to_payload()
                ),
            },
            "slices": [item.to_payload() for item in self.slices],
            "samples": [item.to_payload() for item in self.samples],
        }
        if include_digest:
            payload["report_sha256"] = self.report_sha256
        return payload


def _price_metrics(
    decision_odds: Decimal,
    execution_odds: Decimal | None,
    *,
    side: str,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, PriceMovement]:
    if execution_odds is None:
        return None, None, None, PriceMovement.UNAVAILABLE
    with localcontext() as context:
        context.prec = DERIVED_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        delta = execution_odds - decision_odds
        spread_bps = ((execution_odds / decision_odds) - Decimal(1)) * Decimal(10_000)
        if side == "BACK":
            adverse_slippage_bps = -spread_bps
        elif side == "LAY":
            adverse_slippage_bps = spread_bps
        else:
            raise EvaluationUniverseIntegrityError(
                "execution-quality PAPER attempt has unsupported bet side"
            )
    if delta > 0:
        movement = PriceMovement.HIGHER_ODDS
    elif delta < 0:
        movement = PriceMovement.LOWER_ODDS
    else:
        movement = PriceMovement.SAME_ODDS
    return delta, spread_bps, adverse_slippage_bps, movement


def _sample_for_event(ledger: EvaluationUniverseLedger, row, event) -> PaperExecutionQualitySample:
    if ledger.paper_resolver is None:
        raise EvaluationUniverseIntegrityError(
            "execution-quality evidence requires canonical PAPER resolver"
        )
    assert event.execution_attempt_id is not None
    assert event.execution_reality_sha256 is not None
    attempt = ledger.paper_resolver.resolve(
        row=row,
        attempt_id=event.execution_attempt_id,
        reality_sha256=event.execution_reality_sha256,
    )
    expected_outcome = _OUTCOME_STAGE[event.stage]
    if attempt.outcome is not expected_outcome:
        raise EvaluationUniverseIntegrityError(
            "execution-quality event disagrees with canonical PAPER outcome"
        )
    if row.event_id is None or row.market_id is None or row.selection_id is None:
        raise EvaluationUniverseIntegrityError(
            "execution-model eligible row lacks market identity"
        )
    if attempt.outcome is PaperAttemptOutcome.UNKNOWN:
        fill_ratio = None
    elif attempt.outcome is PaperAttemptOutcome.REJECTED:
        fill_ratio = Decimal(0)
    else:
        assert attempt.execution_stake is not None
        fill_ratio = _derived_ratio(attempt.execution_stake, attempt.requested_stake)
    delta, spread_bps, adverse_slippage_bps, movement = _price_metrics(
        attempt.decision_odds,
        attempt.execution_odds,
        side=attempt.side,
    )
    return PaperExecutionQualitySample(
        row_id=row.row_id,
        row_key=row.row_key,
        run_id=attempt.run_id,
        action_id=attempt.action_id,
        attempt_id=attempt.attempt_id,
        event_id=row.event_id,
        sport=row.sport,
        market_id=row.market_id,
        selection_id=row.selection_id,
        provider_id=attempt.bookmaker_id,
        decision_source_provider_id=row.provider_id,
        side=attempt.side,
        outcome=attempt.outcome,
        decision_odds=attempt.decision_odds,
        requested_stake=attempt.requested_stake,
        execution_odds=attempt.execution_odds,
        execution_stake=attempt.execution_stake,
        fill_ratio=fill_ratio,
        signed_odds_delta=delta,
        signed_price_spread_bps=spread_bps,
        signed_adverse_price_slippage_bps=adverse_slippage_bps,
        price_movement=movement,
        model_delay_ms=attempt.delay_ms,
        model_quote_age_ms=attempt.quote_age_ms,
        evidence_grade=attempt.evidence_grade.value,
        evidence_source=attempt.evidence_source,
        evidence_id=attempt.evidence_id,
        evidence_sha256=attempt.evidence_sha256,
        execution_reality_sha256=event.execution_reality_sha256,
    )


def _slice(samples: tuple[PaperExecutionQualitySample, ...]) -> tuple[PaperExecutionQualitySlice, ...]:
    groups: dict[tuple[str, str, str], list[PaperExecutionQualitySample]] = {}
    for sample in samples:
        groups.setdefault(
            (sample.provider_id, sample.sport, sample.market_id),
            [],
        ).append(sample)
    output: list[PaperExecutionQualitySlice] = []
    for key in sorted(groups):
        members = groups[key]
        counts = {outcome.value: 0 for outcome in PaperAttemptOutcome}
        for member in members:
            counts[member.outcome.value] += 1
        output.append(
            PaperExecutionQualitySlice(
                provider_id=key[0],
                sport=key[1],
                market_id=key[2],
                sample_count=len(members),
                outcome_counts=tuple(sorted(counts.items())),
                fill_ratio=NumericDistribution.from_values(
                    member.fill_ratio
                    for member in members
                    if member.fill_ratio is not None
                ),
                signed_price_spread_bps=NumericDistribution.from_values(
                    member.signed_price_spread_bps
                    for member in members
                    if member.signed_price_spread_bps is not None
                ),
                signed_adverse_price_slippage_bps=NumericDistribution.from_values(
                    member.signed_adverse_price_slippage_bps
                    for member in members
                    if member.signed_adverse_price_slippage_bps is not None
                ),
            )
        )
    return tuple(output)


def project_paper_execution_quality(
    ledger: EvaluationUniverseLedger,
) -> PaperExecutionQualityReport:
    """Project immutable PAPER execution evidence without claiming live latency/economics.

    `delay_ms` and `quote_age_ms` are PAPER execution-model fields.  They are
    intentionally reported under model-specific names and never promoted into
    real/provider latency.  Provider/local clocks are not subtracted here.
    """
    if not isinstance(ledger, EvaluationUniverseLedger):
        raise TypeError("ledger must be EvaluationUniverseLedger")

    rows = {row.row_id: row for row in ledger.universe.rows}
    eligible_rows = {
        row.row_id
        for row in ledger.universe.rows
        if row.decision_stage is FunnelStage.EXECUTION_MODEL_ELIGIBLE
    }
    attempted_rows = {
        event.row_id for event in ledger.events if event.stage is FunnelStage.ATTEMPTED
    }

    samples: list[PaperExecutionQualitySample] = []
    seen_outcomes: set[str] = set()
    for event in ledger.events:
        if event.stage not in _OUTCOME_STAGES:
            continue
        if event.row_id in seen_outcomes:
            raise EvaluationUniverseIntegrityError(
                "multiple PAPER outcome observations exist for one frozen row"
            )
        row = rows[event.row_id]
        if row.row_id not in eligible_rows:
            raise EvaluationUniverseIntegrityError(
                "PAPER outcome lies outside execution-model eligible denominator"
            )
        samples.append(_sample_for_event(ledger, row, event))
        seen_outcomes.add(event.row_id)

    ordered = tuple(sorted(samples, key=lambda item: (item.row_id, item.attempt_id)))
    counts = {outcome.value: 0 for outcome in PaperAttemptOutcome}
    for sample in ordered:
        counts[sample.outcome.value] += 1

    price_observations = tuple(
        sample.signed_price_spread_bps
        for sample in ordered
        if sample.signed_price_spread_bps is not None
    )
    adverse_price_observations = tuple(
        sample.signed_adverse_price_slippage_bps
        for sample in ordered
        if sample.signed_adverse_price_slippage_bps is not None
    )
    fill_observations = tuple(
        sample.fill_ratio for sample in ordered if sample.fill_ratio is not None
    )
    attempted_count = len(attempted_rows)
    if len(ordered) > attempted_count:
        raise EvaluationUniverseIntegrityError(
            "PAPER outcome observations exceed the durable attempted denominator"
        )
    return PaperExecutionQualityReport(
        ledger_sha256=ledger.ledger_sha256,
        universe_sha256=ledger.universe.universe_sha256,
        frozen_denominator_rows=len(ledger.universe.rows),
        execution_model_eligible_rows=len(eligible_rows),
        attempted_rows=attempted_count,
        outcome_observation_rows=len(ordered),
        outcome_counts=tuple(sorted(counts.items())),
        price_observation_count=len(price_observations),
        price_observation_missing_count=attempted_count - len(price_observations),
        fill_observation_count=len(fill_observations),
        fill_observation_missing_count=attempted_count - len(fill_observations),
        real_latency_observation_count=0,
        clock_status=ClockStatus.CLOCK_DOMAIN_UNPROVEN,
        quality_status=(
            EvidenceReadinessStatus.PROVISIONAL
            if ordered
            else EvidenceReadinessStatus.INSUFFICIENT_DATA
        ),
        live_execution_economics_status=(
            ExecutionEconomicsStatus.EXECUTION_ECONOMICS_MISSING
        ),
        model_delay_ms=NumericDistribution.from_values(
            sample.model_delay_ms for sample in ordered
        ),
        model_quote_age_ms=NumericDistribution.from_values(
            sample.model_quote_age_ms for sample in ordered
        ),
        fill_ratio=NumericDistribution.from_values(fill_observations),
        signed_price_spread_bps=NumericDistribution.from_values(price_observations),
        signed_adverse_price_slippage_bps=NumericDistribution.from_values(
            adverse_price_observations
        ),
        slices=_slice(ordered),
        samples=ordered,
    )