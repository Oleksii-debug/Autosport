from __future__ import annotations

from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from autosport import _paper_execution_decimal_resource_guard as decimal_guard
from autosport import _paper_execution_reality_legacy as legacy
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
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


def test_oversized_second_member_fails_before_any_sibling_formatting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _record(odds=Decimal("5"), stake=Decimal("10"))
    object.__setattr__(record, "accepted_stake", Decimal("1E+100000000"))
    serialized: list[Decimal] = []
    original_serializer = legacy._decimal_text

    def spy(value: Decimal) -> str:
        serialized.append(value)
        return original_serializer(value)

    monkeypatch.setattr(legacy, "_decimal_text", spy)

    with pytest.raises(ValueError, match="fixed-point representation exceeds resource limit"):
        record.to_dict()

    assert serialized == []


def test_evidence_serializer_preserves_noninjectable_public_call_shape() -> None:
    record = _record(odds=Decimal("5"), stake=Decimal("10"))

    assert legacy.PaperExecutionEvidenceRecord.to_dict.__defaults__ is None
    assert legacy.PaperExecutionEvidenceRecord.to_dict.__kwdefaults__ is None
    with pytest.raises(TypeError):
        record.to_dict(lambda _: {})  # type: ignore[call-arg]


def test_boundary_sized_fixed_point_value_remains_serializable() -> None:
    record = _record(odds=Decimal("1E+8191"), stake=Decimal("1"))

    rendered = record.to_dict()["accepted_odds"]

    assert isinstance(rendered, str)
    assert len(rendered) == 8192


def test_reload_rejects_oversized_evidence_under_same_resource_law() -> None:
    raw = _record(odds=Decimal("5"), stake=Decimal("10")).to_dict()
    raw["accepted_stake"] = "1E-100000000"

    with pytest.raises(legacy.PaperExecutionIntegrityError, match="invalid evidence record"):
        legacy.PaperExecutionEvidenceRecord.from_dict(raw)


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


def test_mutated_oversized_evidence_cannot_partially_append(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "paper-execution.jsonl"
    registry = PaperExecutionEvidenceRegistry(PaperExecutionLedger(ledger_path))
    record = _record(odds=Decimal("5"), stake=Decimal("10"))
    object.__setattr__(record, "accepted_odds", Decimal("1E+100000000"))

    with pytest.raises(ValueError, match="fixed-point representation exceeds resource limit"):
        registry.register(record)

    assert not ledger_path.exists() or ledger_path.read_bytes() == b""


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
