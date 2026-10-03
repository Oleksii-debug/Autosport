from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

import autosport.policy_utility_evidence as utility_module

from autosport.policy_utility_evidence import (
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


class _AdversarialEffectiveSampleSize(Decimal):
    def __new__(cls, value: str) -> "_AdversarialEffectiveSampleSize":
        return super().__new__(cls, value)

    def is_finite(self) -> bool:
        return True

    def __le__(self, other: object) -> bool:
        return False

    def __gt__(self, other: object) -> bool:
        return False


def _estimated_evidence(
    effective_sample_size: Decimal,
) -> PolicyUtilityEvidence:
    return PolicyUtilityEvidence(
        environment_id="env-ess-subclass",
        episode_id="episode-ess-subclass",
        action_id="action-ess-subclass",
        outcome_id="outcome-ess-subclass",
        reward_id="reward-ess-subclass",
        transition_id="transition-ess-subclass",
        policy_id="policy-ess-subclass",
        model_id="model-ess-subclass",
        strategy_id="strategy-ess-subclass",
        config_sha256=SHA_A,
        protocol_sha256=SHA_B,
        economic_goal_fingerprint=SHA_C,
        risk_fingerprint=SHA_D,
        bankroll_id="bankroll-ess-subclass",
        portfolio_identity="portfolio-ess-subclass",
        utility_definition_family="owner-net-utility",
        utility_definition_version="v1",
        utility_definition_sha256=SHA_E,
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.ESTIMATED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=NOW,
        support_count=2,
        effective_sample_size=effective_sample_size,
        uncertainty=Decimal("0"),
    )


def test_decimal_subclass_cannot_bypass_ess_raw_support_bound() -> None:
    boundary = _estimated_evidence(Decimal("2"))
    assert boundary.effective_sample_size == Decimal("2")

    hostile = _AdversarialEffectiveSampleSize("1000")
    assert hostile > Decimal("2") is False

    with pytest.raises(
        PolicyUtilityError,
        match="effective_sample_size",
    ):
        _estimated_evidence(hostile)


def test_decimal_subclass_cannot_enter_other_policy_utility_numeric_fields() -> None:
    baseline = _estimated_evidence(Decimal("1"))

    hostile_utility = _AdversarialEffectiveSampleSize("1")
    with pytest.raises(PolicyUtilityError, match="utility_value"):
        replace(
            baseline,
            currency="EUR",
            utility_value=hostile_utility,
        )

    hostile_uncertainty = _AdversarialEffectiveSampleSize("0")
    with pytest.raises(PolicyUtilityError, match="uncertainty"):
        replace(
            baseline,
            uncertainty=hostile_uncertainty,
        )



class _HostileAuthorityRef:
    def __lt__(self, other: object) -> bool:
        raise AssertionError("hostile AuthorityRef ordering executed")


def test_module_decimal_rebind_cannot_replace_exact_numeric_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _estimated_evidence(Decimal("1"))
    wire = baseline.to_dict()
    hostile = _AdversarialEffectiveSampleSize("1000")
    monkeypatch.setattr(
        utility_module,
        "Decimal",
        _AdversarialEffectiveSampleSize,
    )

    with pytest.raises(PolicyUtilityError, match="effective_sample_size"):
        _estimated_evidence(hostile)

    restored = PolicyUtilityEvidence.from_dict(wire)
    assert type(restored.effective_sample_size) is Decimal
    assert restored.effective_sample_size == Decimal("1")


def test_module_authority_ref_rebind_cannot_replace_exact_reference_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = _estimated_evidence(Decimal("1"))
    hostile = _HostileAuthorityRef()
    monkeypatch.setattr(utility_module, "AuthorityRef", _HostileAuthorityRef)

    with pytest.raises(
        PolicyUtilityError,
        match="authority_refs must contain exact AuthorityRef values",
    ):
        replace(
            baseline,
            authority_refs=(hostile,),  # type: ignore[arg-type]
        )


def test_decimal_identity_is_independent_of_ambient_context_precision() -> None:
    baseline = _estimated_evidence(Decimal("1"))

    with localcontext() as context:
        context.prec = 6
        first = replace(
            baseline,
            currency="EUR",
            utility_value=Decimal("1.2345671"),
        )
        second = replace(
            baseline,
            currency="EUR",
            utility_value=Decimal("1.2345672"),
        )

        assert first.payload()["utility_value"] == "1.2345671"
        assert second.payload()["utility_value"] == "1.2345672"
        assert first.evidence_id != second.evidence_id

        restored = PolicyUtilityEvidence.from_dict(first.to_dict())
        assert restored.utility_value == Decimal("1.2345671")
        assert restored.evidence_id == first.evidence_id
