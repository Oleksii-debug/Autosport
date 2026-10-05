from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from autosport import _paper_execution_decimal_resource_guard as decimal_guard
from autosport import _paper_execution_reality_legacy as legacy
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
)


_OBSERVED_AT = "2026-10-05T05:00:00+00:00"


def _record(*, odds: Decimal, stake: Decimal) -> PaperExecutionEvidenceRecord:
    return PaperExecutionEvidenceRecord(
        action_id="paper-decimal-bound-action",
        bookmaker_id="paper-decimal-bound-bookmaker",
        account_id="paper-decimal-bound-account",
        event_id="paper-decimal-bound-event",
        market_id="paper-decimal-bound-market",
        selection_id="paper-decimal-bound-selection",
        side="LAY",
        quote_id="paper-decimal-bound-quote",
        outcome=PaperAttemptOutcome.ACCEPTED,
        observed_at=_OBSERVED_AT,
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source="paper-decimal-bound-test",
        accepted_odds=odds,
        accepted_stake=stake,
    )


def test_empirical_odds_fail_before_huge_fixed_point_materialization() -> None:
    with pytest.raises(ValueError, match="fixed-point representation exceeds resource limit"):
        _record(odds=Decimal("1E+100000000"), stake=Decimal("1"))


def test_empirical_stake_fail_before_huge_fractional_materialization() -> None:
    with pytest.raises(ValueError, match="fixed-point representation exceeds resource limit"):
        _record(odds=Decimal("2"), stake=Decimal("1E-100000000"))


def test_derived_decimal_text_uses_same_execution_resource_boundary() -> None:
    with pytest.raises(ValueError, match="fixed-point representation exceeds resource limit"):
        legacy._decimal_text(Decimal("1E+100000000"))


def test_bounded_empirical_evidence_is_context_independent() -> None:
    record = _record(
        odds=Decimal("9.87654321987654321"),
        stake=Decimal("123456789.123456789"),
    )
    expected = record.evidence_sha256

    with localcontext() as context:
        context.prec = 6
        assert record.to_dict()["accepted_odds"] == "9.87654321987654321"
        assert record.to_dict()["accepted_stake"] == "123456789.123456789"
        assert record.evidence_sha256 == expected


def test_decimal_resource_validator_code_drift_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forged_validator(value: Decimal) -> None:
        del value
        return None

    monkeypatch.setattr(
        decimal_guard._RESOURCE_VALIDATOR,
        "__code__",
        forged_validator.__code__,
    )

    with pytest.raises(ValueError, match="Decimal resource authority drifted"):
        _record(odds=Decimal("5"), stake=Decimal("10"))
