from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_UP, localcontext
from types import SimpleNamespace

from autosport.evaluation_universe import EvaluationUniverseLedger, FunnelStage
from autosport.execution_quality_evidence import project_paper_execution_quality
from autosport.paper_execution_reality import PaperAttemptOutcome


class _Resolver:
    def __init__(self, attempt):
        self._attempt = attempt

    def resolve(self, *, row, attempt_id, reality_sha256):
        assert attempt_id == self._attempt.attempt_id
        assert reality_sha256 == "f" * 64
        assert row.execution_action_id == self._attempt.action_id
        return self._attempt


class _FixtureLedger(EvaluationUniverseLedger):
    @property
    def ledger_sha256(self) -> str:
        return "a" * 64


def _ledger():
    row = SimpleNamespace(
        row_id="row-1",
        row_key="row-key-1",
        decision_stage=FunnelStage.EXECUTION_MODEL_ELIGIBLE,
        execution_action_id="action-1",
        event_id="event-1",
        sport="tennis",
        market_id="match-odds",
        selection_id="selection-1",
        provider_id="decision-feed",
    )
    attempt = SimpleNamespace(
        attempt_id="attempt-1",
        run_id="paper-run-1",
        action_id="action-1",
        bookmaker_id="paper-bookmaker",
        side="BACK",
        outcome=PaperAttemptOutcome.PARTIAL,
        decision_odds=Decimal("3"),
        requested_stake=Decimal("3"),
        execution_odds=Decimal("2"),
        execution_stake=Decimal("1"),
        delay_ms=7,
        quote_age_ms=11,
        evidence_grade=SimpleNamespace(value="SYNTHETIC"),
        evidence_source="paper-model",
        evidence_id=None,
        evidence_sha256=None,
    )
    events = (
        SimpleNamespace(
            row_id=row.row_id,
            stage=FunnelStage.ATTEMPTED,
            execution_attempt_id="attempt-1",
            execution_reality_sha256="f" * 64,
        ),
        SimpleNamespace(
            row_id=row.row_id,
            stage=FunnelStage.PARTIAL,
            execution_attempt_id="attempt-1",
            execution_reality_sha256="f" * 64,
        ),
    )
    ledger = object.__new__(_FixtureLedger)
    object.__setattr__(
        ledger,
        "universe",
        SimpleNamespace(rows=(row,), universe_sha256="b" * 64),
    )
    object.__setattr__(ledger, "events", events)
    object.__setattr__(ledger, "paper_resolver", _Resolver(attempt))
    return ledger


def _project_under_hostile_context(*, precision, rounding, emin, emax, clamp):
    with localcontext() as context:
        context.prec = precision
        context.rounding = rounding
        context.Emin = emin
        context.Emax = emax
        context.clamp = clamp
        return project_paper_execution_quality(_ledger())


def test_derived_economics_and_report_hash_ignore_ambient_decimal_context():
    narrow = _project_under_hostile_context(
        precision=6,
        rounding=ROUND_DOWN,
        emin=-9,
        emax=9,
        clamp=1,
    )
    wide = _project_under_hostile_context(
        precision=37,
        rounding=ROUND_UP,
        emin=-999,
        emax=999,
        clamp=0,
    )

    assert narrow.to_payload() == wide.to_payload()
    assert narrow.report_sha256 == wide.report_sha256

    sample = narrow.samples[0]
    assert sample.fill_ratio is not None
    assert sample.signed_price_spread_bps is not None
    assert sample.fill_ratio != Decimal("0.333333")
    assert sample.signed_price_spread_bps != Decimal("-3333.33")

    assert narrow.protocol == "autosport-execution-quality-evidence/v2"
    assert narrow.to_payload()["derived_decimal_arithmetic"] == {
        "precision": 50,
        "rounding": "ROUND_HALF_EVEN",
        "emin": -999999,
        "emax": 999999,
        "clamp": 0,
    }
