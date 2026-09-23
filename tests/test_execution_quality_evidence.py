from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from autosport.evaluation_universe import EvaluationUniverseLedger, FunnelStage
from autosport.execution_quality_evidence import (
    ClockStatus,
    EvidenceReadinessStatus,
    ExecutionEconomicsStatus,
    ExecutionEvidencePlane,
    PriceMovement,
    project_paper_execution_quality,
)
from autosport.paper_execution_reality import PaperAttemptOutcome


class _Resolver:
    def __init__(self, attempts):
        self._attempts = {attempt.attempt_id: attempt for attempt in attempts}

    def resolve(self, *, row, attempt_id, reality_sha256):
        attempt = self._attempts[attempt_id]
        assert reality_sha256 == ("f" * 64)
        assert attempt.action_id == row.execution_action_id
        return attempt


class _FixtureLedger(EvaluationUniverseLedger):
    @property
    def ledger_sha256(self) -> str:
        return "a" * 64


def _row(index: int):
    return SimpleNamespace(
        row_id=f"row-{index}",
        row_key=f"row-key-{index}",
        decision_stage=FunnelStage.EXECUTION_MODEL_ELIGIBLE,
        execution_action_id=f"action-{index}",
        event_id=f"event-{index}",
        sport="tennis",
        market_id="match-odds",
        selection_id=f"selection-{index}",
        provider_id="decision-feed",
    )


def _attempt(
    index: int,
    outcome: PaperAttemptOutcome,
    *,
    execution_odds: str | None,
    execution_stake: str | None,
):
    return SimpleNamespace(
        attempt_id=f"attempt-{index}",
        run_id="paper-run-1",
        action_id=f"action-{index}",
        bookmaker_id="paper-bookmaker",
        side="BACK",
        outcome=outcome,
        decision_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        execution_odds=None if execution_odds is None else Decimal(execution_odds),
        execution_stake=None if execution_stake is None else Decimal(execution_stake),
        delay_ms=index * 10,
        quote_age_ms=index * 100,
        evidence_grade=SimpleNamespace(value="SYNTHETIC"),
        evidence_source="paper-model",
        evidence_id=None,
        evidence_sha256=None,
    )


def _event(row, stage: FunnelStage, index: int):
    return SimpleNamespace(
        row_id=row.row_id,
        stage=stage,
        execution_attempt_id=f"attempt-{index}",
        execution_reality_sha256="f" * 64,
    )


def _ledger_with_outcomes():
    rows = tuple(_row(index) for index in range(1, 5))
    attempts = (
        _attempt(
            1,
            PaperAttemptOutcome.ACCEPTED,
            execution_odds="2.1",
            execution_stake="10",
        ),
        _attempt(
            2,
            PaperAttemptOutcome.PARTIAL,
            execution_odds="1.9",
            execution_stake="5",
        ),
        _attempt(
            3,
            PaperAttemptOutcome.REJECTED,
            execution_odds=None,
            execution_stake=None,
        ),
        _attempt(
            4,
            PaperAttemptOutcome.UNKNOWN,
            execution_odds=None,
            execution_stake=None,
        ),
    )
    events = []
    outcome_stages = (
        FunnelStage.ACCEPTED,
        FunnelStage.PARTIAL,
        FunnelStage.REJECTED,
        FunnelStage.UNKNOWN,
    )
    for index, (row, stage) in enumerate(zip(rows, outcome_stages), start=1):
        events.append(_event(row, FunnelStage.ATTEMPTED, index))
        events.append(_event(row, stage, index))
    # The accepted row later moved on. Projection must retain its original
    # immutable attempt outcome rather than looking only at the current stage.
    events.append(
        SimpleNamespace(
            row_id=rows[0].row_id,
            stage=FunnelStage.RECONCILED,
            execution_attempt_id=None,
            execution_reality_sha256=None,
        )
    )

    ledger = object.__new__(_FixtureLedger)
    object.__setattr__(
        ledger,
        "universe",
        SimpleNamespace(rows=rows, universe_sha256="b" * 64),
    )
    object.__setattr__(ledger, "events", tuple(events))
    object.__setattr__(ledger, "paper_resolver", _Resolver(attempts))
    return ledger


def test_projection_preserves_frozen_denominator_and_all_attempt_outcomes():
    report = project_paper_execution_quality(_ledger_with_outcomes())

    assert report.evidence_plane is ExecutionEvidencePlane.PAPER_EXECUTION_MODEL
    assert report.frozen_denominator_rows == 4
    assert report.execution_model_eligible_rows == 4
    assert report.attempted_rows == 4
    assert report.outcome_observation_rows == 4
    assert dict(report.outcome_counts) == {
        "ACCEPTED": 1,
        "PARTIAL": 1,
        "REJECTED": 1,
        "UNKNOWN": 1,
    }
    assert [sample.outcome for sample in report.samples] == [
        PaperAttemptOutcome.ACCEPTED,
        PaperAttemptOutcome.PARTIAL,
        PaperAttemptOutcome.REJECTED,
        PaperAttemptOutcome.UNKNOWN,
    ]
    assert [sample.fill_ratio for sample in report.samples] == [
        Decimal("1"),
        Decimal("0.5"),
        Decimal("0"),
        None,
    ]
    assert [sample.price_movement for sample in report.samples] == [
        PriceMovement.HIGHER_ODDS,
        PriceMovement.LOWER_ODDS,
        PriceMovement.UNAVAILABLE,
        PriceMovement.UNAVAILABLE,
    ]
    assert report.price_observation_count == 2
    assert report.price_observation_missing_count == 2
    assert report.fill_observation_count == 3
    assert report.fill_observation_missing_count == 1
    assert report.fill_ratio.count == 3


def test_projection_never_promotes_paper_model_fields_to_real_latency_or_live_economics():
    report = project_paper_execution_quality(_ledger_with_outcomes())
    payload = report.to_payload()

    assert report.real_latency_observation_count == 0
    assert report.clock_status is ClockStatus.CLOCK_DOMAIN_UNPROVEN
    assert (
        report.live_execution_economics_status
        is ExecutionEconomicsStatus.EXECUTION_ECONOMICS_MISSING
    )
    assert report.quality_status is EvidenceReadinessStatus.PROVISIONAL
    assert payload["distributions"]["model_delay_ms"]["count"] == 4
    assert payload["distributions"]["model_quote_age_ms"]["count"] == 4
    assert "real_latency_ms" not in payload["distributions"]
    assert report.samples[0].provider_id == "paper-bookmaker"
    assert report.samples[0].decision_source_provider_id == "decision-feed"


def test_projection_payload_and_digest_are_deterministic():
    first = project_paper_execution_quality(_ledger_with_outcomes())
    second = project_paper_execution_quality(_ledger_with_outcomes())

    assert first.to_payload() == second.to_payload()
    assert first.report_sha256 == second.report_sha256
    assert len(first.report_sha256) == 64


def test_attempt_without_outcome_is_insufficient_not_a_fill():
    row = _row(1)
    attempt = _attempt(
        1,
        PaperAttemptOutcome.ACCEPTED,
        execution_odds="2.1",
        execution_stake="10",
    )
    ledger = object.__new__(_FixtureLedger)
    object.__setattr__(
        ledger,
        "universe",
        SimpleNamespace(rows=(row,), universe_sha256="b" * 64),
    )
    object.__setattr__(
        ledger,
        "events",
        (_event(row, FunnelStage.ATTEMPTED, 1),),
    )
    object.__setattr__(ledger, "paper_resolver", _Resolver((attempt,)))

    report = project_paper_execution_quality(ledger)

    assert report.attempted_rows == 1
    assert report.outcome_observation_rows == 0
    assert report.quality_status is EvidenceReadinessStatus.INSUFFICIENT_DATA
    assert report.price_observation_count == 0
    assert report.samples == ()
