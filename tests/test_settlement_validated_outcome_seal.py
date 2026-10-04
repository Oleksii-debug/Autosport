from __future__ import annotations

import pytest

import autosport.continuous_session as session


def _resolution() -> session.SettlementResolution:
    return session.SettlementResolution(
        event_identity="provider:event-1",
        settlement_ref="result-1",
        quote_outcomes={"quote-1": "win"},
        evidence_id="evidence-1",
        evidence_sha256="0" * 64,
        available_at="2026-09-26T12:00:00+00:00",
    )


def test_validated_outcome_digest_is_not_object_state_and_reinit_is_rejected() -> None:
    resolution = _resolution()
    resolution.validate(as_of="2026-09-26T12:00:00+00:00")
    outcomes = resolution.quote_outcomes

    assert type(outcomes) is session._ValidatedQuoteOutcomes
    assert outcomes.validated_sha256 == session._settlement_quote_outcomes_sha256(outcomes)

    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(outcomes, "_validated_sha256", "f" * 64)
    with pytest.raises(TypeError, match="cannot be reinitialized"):
        outcomes.__init__({"quote-1": "loss"})


def test_direct_dict_mutation_after_validation_is_detected_by_closure_owned_digest() -> None:
    resolution = _resolution()
    resolution.validate(as_of="2026-09-26T12:00:00+00:00")
    outcomes = resolution.quote_outcomes

    # Even an explicit base-type mutation bypassing ordinary subclass methods cannot
    # rewrite the issuance digest because that digest does not live on the object.
    dict.__setitem__(outcomes, "quote-1", "loss")

    with pytest.raises(ValueError, match="changed after validation"):
        resolution.validate(as_of="2026-09-26T12:00:00+00:00")


def test_digest_dispatch_rebinding_cannot_hide_post_validation_mutation(monkeypatch) -> None:
    resolution = _resolution()
    resolution.validate(as_of="2026-09-26T12:00:00+00:00")
    outcomes = resolution.quote_outcomes
    original_digest = outcomes.validated_sha256

    dict.__setitem__(outcomes, "quote-1", "loss")
    monkeypatch.setattr(
        session,
        "_settlement_quote_outcomes_sha256",
        lambda _outcomes: original_digest,
    )

    with pytest.raises(
        session.ContinuousSessionError,
        match="settlement outcome digest authority changed",
    ):
        resolution.validate(as_of="2026-09-26T12:00:00+00:00")


def test_digest_dispatch_rebinding_cannot_mint_new_validated_outcomes(monkeypatch) -> None:
    monkeypatch.setattr(
        session,
        "_settlement_quote_outcomes_sha256",
        lambda _outcomes: "0" * 64,
    )

    with pytest.raises(
        session.ContinuousSessionError,
        match="settlement outcome digest authority changed",
    ):
        _resolution().validate(as_of="2026-09-26T12:00:00+00:00")


def test_digest_executable_mutation_is_rejected_before_validation() -> None:
    digest = session._settlement_quote_outcomes_sha256
    original_code = digest.__code__
    forged_code = (lambda _outcomes: "0" * 64).__code__
    digest.__code__ = forged_code
    try:
        with pytest.raises(
            session.ContinuousSessionError,
            match="settlement outcome digest authority changed",
        ):
            _resolution().validate(as_of="2026-09-26T12:00:00+00:00")
    finally:
        digest.__code__ = original_code


def test_validated_outcome_type_rebinding_is_rejected_before_validation(monkeypatch) -> None:
    monkeypatch.setattr(session, "_ValidatedQuoteOutcomes", dict)

    with pytest.raises(
        session.ContinuousSessionError,
        match="validated settlement outcome type changed",
    ):
        _resolution().validate(as_of="2026-09-26T12:00:00+00:00")


def test_settlement_validation_helper_rebinding_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(session, "_instant", lambda _value, _name: None)

    with pytest.raises(
        session.ContinuousSessionError,
        match="settlement resolution helper authority changed: _instant",
    ):
        _resolution().validate(as_of="2026-09-26T12:00:00+00:00")