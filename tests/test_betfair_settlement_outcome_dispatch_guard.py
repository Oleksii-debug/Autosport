from __future__ import annotations

import pytest

import autosport.betfair_settlement_outcome_evidence as outcome


def test_projection_rebinding_fails_closed_before_forged_projection(monkeypatch) -> None:
    called = False

    def forged_projection(_revision):
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(outcome, "_project", forged_projection)

    with pytest.raises(
        outcome.BetfairOutcomeEvidenceError,
        match="outcome authority dispatch changed",
    ):
        outcome._require_dispatch()

    assert called is False


def test_evidence_type_rebinding_fails_closed(monkeypatch) -> None:
    class ForgedEvidence:
        pass

    monkeypatch.setattr(outcome, "BetfairBinarySelectionOutcomeEvidence", ForgedEvidence)

    with pytest.raises(
        outcome.BetfairOutcomeEvidenceError,
        match="outcome authority dispatch changed",
    ):
        outcome._require_dispatch()


def test_in_place_projection_code_replacement_fails_closed(monkeypatch) -> None:
    def forged_projection(_revision):
        return object()

    monkeypatch.setattr(outcome._PROJECT, "__code__", forged_projection.__code__)

    with pytest.raises(
        outcome.BetfairOutcomeEvidenceError,
        match="outcome authority dispatch changed",
    ):
        outcome._require_dispatch()
