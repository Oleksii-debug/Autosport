from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.forward_economic_evidence import (
    AlphaAllocation,
    BetSide,
    FamilywiseAlphaRegistry,
    ForwardDecisionObservation,
    ForwardEconomicEvidenceAccumulator,
    ForwardEconomicEvidenceError,
    ForwardEconomicProtocol,
    ResolvedPolicyOutcome,
)


UTC = timezone.utc
T0 = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
UNIVERSE_SHA = "e" * 64
RESOLVER_AUTHORITY_SHA = "f" * 64
DENOMINATION_SHA = "d" * 64
OTHER_DENOMINATION_SHA = "c" * 64
EXECUTION_SHA = "a" * 64
SETTLEMENT_SHA = "b" * 64


def _protocol(
    *,
    currency_code: str | None = None,
    denomination_authority_sha256: str | None = None,
) -> ForwardEconomicProtocol:
    registry = FamilywiseAlphaRegistry(
        family_id="forward-family-denomination",
        total_alpha=Decimal("0.2"),
        allocations=(AlphaAllocation("challenger", Decimal("0.2")),),
        sealed_at=T0,
    )
    return ForwardEconomicProtocol(
        protocol_id="forward-protocol-denomination",
        challenger_id="challenger",
        champion_id="champion",
        universe_id="universe-denomination",
        universe_sha256=UNIVERSE_SHA,
        authority_binding_sha256=RESOLVER_AUTHORITY_SHA,
        alpha_registry=registry,
        minimum_events=1,
        risk_unit_currency=Decimal("10"),
        maximum_accepted_odds=Decimal("10"),
        maximum_drawdown_currency=Decimal("100"),
        absolute_lambda=Decimal("0.5"),
        paired_lambda=Decimal("0.5"),
        start_sequence=0,
        frozen_at=T0 + timedelta(minutes=1),
        currency_code=currency_code,
        denomination_authority_sha256=denomination_authority_sha256,
    )


def _observation() -> ForwardDecisionObservation:
    return ForwardDecisionObservation(
        sequence=0,
        universe_sha256=UNIVERSE_SHA,
        universe_event_sha256="1" * 64,
        challenger_decision_sha256="2" * 64,
        champion_decision_sha256="3" * 64,
    )


def _outcome(
    policy_id: str,
    observation: ForwardDecisionObservation,
    *,
    side: BetSide,
    currency_code: str | None,
    denomination_authority_sha256: str | None,
) -> ResolvedPolicyOutcome:
    decision_sha256 = (
        observation.challenger_decision_sha256
        if policy_id == "challenger"
        else observation.champion_decision_sha256
    )
    common = dict(
        policy_id=policy_id,
        sequence=observation.sequence,
        universe_event_sha256=observation.universe_event_sha256,
        decision_sha256=decision_sha256,
        decision_committed_at=T0 + timedelta(minutes=2),
        currency_code=currency_code,
        denomination_authority_sha256=denomination_authority_sha256,
    )
    if side is BetSide.NONE:
        return ResolvedPolicyOutcome(
            **common,
            side=side,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal("0"),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
        )
    return ResolvedPolicyOutcome(
        **common,
        side=side,
        accepted_odds=Decimal("2"),
        accepted_stake=Decimal("10"),
        net_pnl_currency=Decimal("5"),
        execution_evidence_sha256=EXECUTION_SHA,
        execution_accepted_at=T0 + timedelta(minutes=3),
        settlement_evidence_sha256=SETTLEMENT_SHA,
        settlement_available_at=T0 + timedelta(hours=1),
    )


class _Resolver:
    authority_sha256 = RESOLVER_AUTHORITY_SHA

    def __init__(
        self,
        challenger: ResolvedPolicyOutcome,
        champion: ResolvedPolicyOutcome,
    ) -> None:
        self._rows = {
            "challenger": challenger,
            "champion": champion,
        }

    def resolve(
        self,
        *,
        policy_id: str,
        sequence: int,
        universe_event_sha256: str,
        decision_sha256: str,
    ) -> ResolvedPolicyOutcome:
        return self._rows[policy_id]


def _resolver(
    observation: ForwardDecisionObservation,
    *,
    challenger_currency: str | None,
    challenger_authority: str | None,
    champion_currency: str | None,
    champion_authority: str | None,
) -> _Resolver:
    return _Resolver(
        _outcome(
            "challenger",
            observation,
            side=BetSide.BACK,
            currency_code=challenger_currency,
            denomination_authority_sha256=challenger_authority,
        ),
        _outcome(
            "champion",
            observation,
            side=BetSide.NONE,
            currency_code=champion_currency,
            denomination_authority_sha256=champion_authority,
        ),
    )


def test_protocol_identity_binds_currency_and_denomination_authority() -> None:
    eur = _protocol(
        currency_code="EUR",
        denomination_authority_sha256=DENOMINATION_SHA,
    )
    gbp = _protocol(
        currency_code="GBP",
        denomination_authority_sha256=DENOMINATION_SHA,
    )
    other_authority = _protocol(
        currency_code="EUR",
        denomination_authority_sha256=OTHER_DENOMINATION_SHA,
    )

    assert eur.identity_sha256 != gbp.identity_sha256
    assert eur.identity_sha256 != other_authority.identity_sha256
    assert eur.to_payload()["currency_code"] == "EUR"
    assert (
        eur.to_payload()["denomination_authority_sha256"]
        == DENOMINATION_SHA
    )


def test_matching_denomination_is_self_describing_but_non_promoting() -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(
        _protocol(
            currency_code="EUR",
            denomination_authority_sha256=DENOMINATION_SHA,
        )
    )
    observation = _observation()

    step = accumulator.record(
        observation,
        _resolver(
            observation,
            challenger_currency="EUR",
            challenger_authority=DENOMINATION_SHA,
            champion_currency="EUR",
            champion_authority=DENOMINATION_SHA,
        ),
    )
    summary = accumulator.summary()

    assert step.currency_code == "EUR"
    assert step.denomination_authority_sha256 == DENOMINATION_SHA
    assert summary.currency_code == "EUR"
    assert summary.denomination_authority_sha256 == DENOMINATION_SHA
    assert summary.denomination_bound is True
    assert summary.positive_authority_verified is False
    assert summary.scientific_promotion_gate_passed is False


@pytest.mark.parametrize(
    ("currency_code", "authority_sha256", "match"),
    (
        ("GBP", DENOMINATION_SHA, "resolved currency"),
        ("EUR", OTHER_DENOMINATION_SHA, "denomination authority"),
        (None, None, "resolved currency"),
    ),
)
def test_bound_protocol_rejects_mismatched_or_missing_outcome_denomination(
    currency_code: str | None,
    authority_sha256: str | None,
    match: str,
) -> None:
    accumulator = ForwardEconomicEvidenceAccumulator(
        _protocol(
            currency_code="EUR",
            denomination_authority_sha256=DENOMINATION_SHA,
        )
    )
    observation = _observation()
    before = accumulator.summary()

    with pytest.raises(ForwardEconomicEvidenceError, match=match):
        accumulator.record(
            observation,
            _resolver(
                observation,
                challenger_currency=currency_code,
                challenger_authority=authority_sha256,
                champion_currency="EUR",
                champion_authority=DENOMINATION_SHA,
            ),
        )

    after = accumulator.summary()
    assert accumulator.steps == ()
    assert after.evidence_sha256 == before.evidence_sha256
    assert after.next_sequence == before.next_sequence


def test_unbound_legacy_evidence_stays_explicitly_audit_only() -> None:
    observation = _observation()
    accumulator = ForwardEconomicEvidenceAccumulator(_protocol())
    accumulator.record(
        observation,
        _resolver(
            observation,
            challenger_currency=None,
            challenger_authority=None,
            champion_currency=None,
            champion_authority=None,
        ),
    )

    summary = accumulator.summary()
    assert summary.currency_code is None
    assert summary.denomination_authority_sha256 is None
    assert summary.denomination_bound is False
    assert summary.positive_authority_verified is False
    assert summary.scientific_promotion_gate_passed is False

    bound_outcome = ForwardEconomicEvidenceAccumulator(_protocol())
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="cannot exceed the frozen protocol binding",
    ):
        bound_outcome.record(
            observation,
            _resolver(
                observation,
                challenger_currency="EUR",
                challenger_authority=DENOMINATION_SHA,
                champion_currency=None,
                champion_authority=None,
            ),
        )


def test_currency_code_and_authority_must_be_bound_together() -> None:
    with pytest.raises(ForwardEconomicEvidenceError, match="bound together"):
        _protocol(currency_code="EUR")

    with pytest.raises(ForwardEconomicEvidenceError, match="bound together"):
        _protocol(denomination_authority_sha256=DENOMINATION_SHA)

    with pytest.raises(ForwardEconomicEvidenceError, match="three-letter"):
        _protocol(
            currency_code="eur",
            denomination_authority_sha256=DENOMINATION_SHA,
        )
