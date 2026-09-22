from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from autosport.policy_utility_evidence import (
    AuthorityRef,
    DecisionKind,
    PolicyUtilityError,
    PolicyUtilityEvidence,
    PolicyUtilityStore,
    UtilityCompleteness,
    UtilityTruthClass,
)


UTC_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _ref(family: str = "campaign-economics", evidence_id: str = "econ-1", sha: str = SHA_A) -> AuthorityRef:
    return AuthorityRef(family=family, evidence_id=evidence_id, sha256=sha)


def _evidence(**overrides: object) -> PolicyUtilityEvidence:
    values: dict[str, object] = {
        "environment_id": "env-1",
        "episode_id": "episode-1",
        "action_id": "action-1",
        "outcome_id": "outcome-1",
        "reward_id": "reward-1",
        "transition_id": "transition-1",
        "policy_id": "policy-1",
        "model_id": "model-1",
        "strategy_id": "strategy-1",
        "config_sha256": SHA_A,
        "protocol_sha256": SHA_B,
        "economic_goal_fingerprint": SHA_C,
        "risk_fingerprint": SHA_D,
        "bankroll_id": "bankroll-1",
        "portfolio_identity": "portfolio-1",
        "utility_definition_family": "owner-net-utility",
        "utility_definition_version": "v1",
        "utility_definition_sha256": SHA_E,
        "completeness": UtilityCompleteness.INCOMPLETE,
        "truth_class": UtilityTruthClass.OBSERVED,
        "decision_kind": DecisionKind.POSITIONED,
        "available_at": UTC_NOW,
        "currency": None,
        "utility_value": None,
        "authority_refs": (_ref(),),
        "denominator_ref": None,
        "counterfactual_ref": None,
        "support_count": None,
        "effective_sample_size": None,
        "uncertainty": None,
    }
    values.update(overrides)
    return PolicyUtilityEvidence(**values)  # type: ignore[arg-type]


def test_schema_v1_can_never_self_authorize_policy_update() -> None:
    evidence = _evidence(currency="EUR", utility_value=Decimal("1.25"))

    assert evidence.source_resolved is False
    assert evidence.policy_update_eligible is False
    with pytest.raises(PolicyUtilityError, match="product-owned authority re-resolution"):
        evidence.require_policy_update_eligible()


def test_roundtrip_is_deterministic_and_derived_positive_truth_is_rejected() -> None:
    evidence = _evidence()
    raw = evidence.to_dict()

    assert PolicyUtilityEvidence.from_dict(raw) == evidence
    assert raw["evidence_id"] == evidence.evidence_id
    assert raw["semantic_key"] == evidence.semantic_key

    forged = dict(raw)
    forged["policy_update_eligible"] = True
    with pytest.raises(PolicyUtilityError, match="cannot carry positive authority/eligibility"):
        PolicyUtilityEvidence.from_dict(forged)

    forged = dict(raw)
    forged["source_resolved"] = True
    with pytest.raises(PolicyUtilityError, match="cannot carry positive authority/eligibility"):
        PolicyUtilityEvidence.from_dict(forged)


def test_schema_version_requires_exact_integer_type() -> None:
    raw = _evidence().to_dict()
    assert PolicyUtilityEvidence.from_dict(dict(raw)) == _evidence()

    for invalid in (True, 1.0):
        forged = dict(raw)
        forged["schema_version"] = invalid
        with pytest.raises(PolicyUtilityError, match="unsupported policy utility schema_version"):
            PolicyUtilityEvidence.from_dict(forged)


def test_available_at_requires_canonical_utc_wire_text() -> None:
    raw = _evidence().to_dict()
    assert raw["available_at"] == "2026-09-20T12:00:00Z"
    assert PolicyUtilityEvidence.from_dict(dict(raw)) == _evidence()

    noncanonical = dict(raw)
    noncanonical["available_at"] = "2026-09-20T12:00:00+00:00"
    with pytest.raises(PolicyUtilityError, match="canonical ISO-8601 UTC text"):
        PolicyUtilityEvidence.from_dict(noncanonical)


def test_direct_constructor_rejects_enum_string_lookalikes() -> None:
    with pytest.raises(PolicyUtilityError, match="completeness must be UtilityCompleteness"):
        _evidence(
            completeness="UNSUPPORTED",
            currency="EUR",
            utility_value=Decimal("5"),
        )

    with pytest.raises(PolicyUtilityError, match="truth_class must be UtilityTruthClass"):
        _evidence(truth_class="SIMULATED")

    with pytest.raises(PolicyUtilityError, match="decision_kind must be DecisionKind"):
        _evidence(
            decision_kind="WAIT_NO_BET",
            currency="EUR",
            utility_value=Decimal("1"),
        )


def test_wire_enum_values_fail_with_contract_error() -> None:
    raw = _evidence().to_dict()
    for field, invalid in (
        ("completeness", "COMPLETE"),
        ("truth_class", "FACT"),
        ("decision_kind", "BET_LATER"),
    ):
        forged = dict(raw)
        forged[field] = invalid
        with pytest.raises(PolicyUtilityError, match="unsupported policy utility enum value"):
            PolicyUtilityEvidence.from_dict(forged)


def test_direct_constructor_rejects_authority_ref_lookalikes() -> None:
    with pytest.raises(PolicyUtilityError, match="authority_refs must contain exact AuthorityRef"):
        _evidence(authority_refs=("campaign-economics",))

    with pytest.raises(PolicyUtilityError, match="denominator_ref must be AuthorityRef or None"):
        _evidence(denominator_ref="denominator-row")

    with pytest.raises(PolicyUtilityError, match="counterfactual_ref must be AuthorityRef or None"):
        _evidence(counterfactual_ref="qualification")


def test_digest_and_semantic_key_tamper_fail_closed() -> None:
    raw = _evidence().to_dict()

    forged = dict(raw)
    forged["evidence_id"] = SHA_B
    with pytest.raises(PolicyUtilityError, match="evidence digest mismatch"):
        PolicyUtilityEvidence.from_dict(forged)

    forged = dict(raw)
    forged["semantic_key"] = SHA_B
    with pytest.raises(PolicyUtilityError, match="semantic key mismatch"):
        PolicyUtilityEvidence.from_dict(forged)


def test_unsupported_utility_cannot_smuggle_monetary_value() -> None:
    with pytest.raises(PolicyUtilityError, match="UNSUPPORTED utility cannot carry"):
        _evidence(
            completeness=UtilityCompleteness.UNSUPPORTED,
            currency="EUR",
            utility_value=Decimal("5"),
        )


def test_utility_value_requires_canonical_currency() -> None:
    with pytest.raises(PolicyUtilityError, match="requires canonical currency"):
        _evidence(utility_value=Decimal("1"))

    with pytest.raises(PolicyUtilityError, match="uppercase three-letter"):
        _evidence(currency="eur", utility_value=Decimal("1"))

    with pytest.raises(PolicyUtilityError, match="uppercase three-letter"):
        _evidence(currency=123)

    with pytest.raises(PolicyUtilityError, match="finite Decimal"):
        _evidence(currency="EUR", utility_value=Decimal("NaN"))


def test_nonzero_wait_requires_nonobserved_truth_and_counterfactual_authority(tmp_path) -> None:
    with pytest.raises(PolicyUtilityError, match="non-zero WAIT/NO_BET"):
        _evidence(
            decision_kind=DecisionKind.WAIT_NO_BET,
            currency="EUR",
            utility_value=Decimal("1"),
        )

    zero = _evidence(
        decision_kind=DecisionKind.WAIT_NO_BET,
        currency="EUR",
        utility_value=Decimal("0"),
    )
    assert zero.utility_value == Decimal("0")
    assert PolicyUtilityEvidence.from_dict(zero.to_dict()) == zero

    denominator = _ref("denominator", "row-1", SHA_B)
    counterfactual = _ref("counterfactual", "qualification-1", SHA_C)
    with pytest.raises(PolicyUtilityError, match="OBSERVED WAIT/NO_BET"):
        _evidence(
            decision_kind=DecisionKind.WAIT_NO_BET,
            truth_class=UtilityTruthClass.OBSERVED,
            currency="EUR",
            utility_value=Decimal("-0.5"),
            denominator_ref=denominator,
            counterfactual_ref=counterfactual,
        )

    estimated = _evidence(
        decision_kind=DecisionKind.WAIT_NO_BET,
        truth_class=UtilityTruthClass.ESTIMATED,
        currency="EUR",
        utility_value=Decimal("-0.5"),
        denominator_ref=denominator,
        counterfactual_ref=counterfactual,
        support_count=12,
        effective_sample_size=Decimal("8.5"),
        uncertainty=Decimal("0.2"),
    )
    path = tmp_path / "wait-utility.jsonl"
    assert PolicyUtilityStore(path).append(estimated) is True
    assert PolicyUtilityStore(path).get(estimated.evidence_id) == estimated
    assert PolicyUtilityEvidence.from_dict(estimated.to_dict()) == estimated
    assert estimated.policy_update_eligible is False


def test_simulated_and_estimated_evidence_require_support_uncertainty() -> None:
    with pytest.raises(PolicyUtilityError, match="support, ESS and uncertainty"):
        _evidence(
            truth_class=UtilityTruthClass.ESTIMATED,
            currency="EUR",
            utility_value=Decimal("0.2"),
        )

    with pytest.raises(PolicyUtilityError, match="counterfactual authority"):
        _evidence(
            truth_class=UtilityTruthClass.SIMULATED,
            currency="EUR",
            utility_value=Decimal("0.2"),
            support_count=10,
            effective_sample_size=Decimal("7.5"),
            uncertainty=Decimal("0.1"),
        )

    simulated = _evidence(
        truth_class=UtilityTruthClass.SIMULATED,
        currency="EUR",
        utility_value=Decimal("0.2"),
        support_count=10,
        effective_sample_size=Decimal("7.5"),
        uncertainty=Decimal("0.1"),
        counterfactual_ref=_ref("counterfactual", "sim-1", SHA_C),
    )
    assert simulated.truth_class is UtilityTruthClass.SIMULATED
    assert simulated.policy_update_eligible is False


def test_bool_is_not_accepted_as_support_count() -> None:
    with pytest.raises(PolicyUtilityError, match="positive integer"):
        _evidence(
            truth_class=UtilityTruthClass.ESTIMATED,
            support_count=True,
            effective_sample_size=Decimal("1"),
            uncertainty=Decimal("0"),
        )


def test_effective_sample_size_cannot_exceed_raw_support_count() -> None:
    with pytest.raises(PolicyUtilityError, match="cannot exceed support_count"):
        _evidence(
            truth_class=UtilityTruthClass.ESTIMATED,
            support_count=2,
            effective_sample_size=Decimal("2.0001"),
            uncertainty=Decimal("0"),
        )

    boundary = _evidence(
        truth_class=UtilityTruthClass.ESTIMATED,
        support_count=2,
        effective_sample_size=Decimal("2"),
        uncertainty=Decimal("0"),
    )
    assert boundary.effective_sample_size == Decimal("2")


def test_deserialization_rejects_effective_sample_size_above_support_count() -> None:
    raw = _evidence(
        truth_class=UtilityTruthClass.ESTIMATED,
        support_count=2,
        effective_sample_size=Decimal("2"),
        uncertainty=Decimal("0"),
    ).to_dict()
    raw["effective_sample_size"] = "3"

    with pytest.raises(PolicyUtilityError, match="cannot exceed support_count"):
        PolicyUtilityEvidence.from_dict(raw)


def test_authority_refs_must_be_sorted_unique() -> None:
    ref_a = _ref("a-family", "a", SHA_A)
    ref_b = _ref("b-family", "b", SHA_B)

    with pytest.raises(PolicyUtilityError, match="must be sorted"):
        _evidence(authority_refs=(ref_b, ref_a))

    with pytest.raises(PolicyUtilityError, match="must be unique"):
        _evidence(authority_refs=(ref_a, ref_a))


def test_same_causal_update_with_changed_utility_definition_is_semantic_drift(tmp_path) -> None:
    store = PolicyUtilityStore(tmp_path / "utility.jsonl")
    first = _evidence()
    changed = _evidence(
        utility_definition_version="v2",
        utility_definition_sha256=SHA_A,
    )

    assert first.semantic_key == changed.semantic_key
    assert first.evidence_id != changed.evidence_id
    assert store.append(first) is True
    with pytest.raises(PolicyUtilityError, match="semantic drift"):
        store.append(changed)


def test_store_duplicate_is_idempotent_and_restart_resolves_exact_evidence(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    evidence = _evidence()
    store = PolicyUtilityStore(path)

    assert store.append(evidence) is True
    assert store.append(evidence) is False
    assert store.get(evidence.evidence_id) == evidence

    reopened = PolicyUtilityStore(path)
    assert reopened.list() == (evidence,)
    assert reopened.get(evidence.evidence_id) == evidence


def test_store_tamper_fails_closed_on_restart(tmp_path) -> None:
    path = tmp_path / "utility.jsonl"
    evidence = _evidence()
    assert PolicyUtilityStore(path).append(evidence) is True

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["risk_fingerprint"] = SHA_A
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(PolicyUtilityError, match="policy utility evidence digest mismatch"):
        PolicyUtilityStore(path)


def test_from_dict_rejects_unknown_fields_and_noncanonical_decimal() -> None:
    raw = _evidence(currency="EUR", utility_value=Decimal("1.5")).to_dict()
    with_extra = dict(raw)
    with_extra["caller_ready"] = True
    with pytest.raises(PolicyUtilityError, match="keys mismatch"):
        PolicyUtilityEvidence.from_dict(with_extra)

    noncanonical = dict(raw)
    noncanonical["utility_value"] = "1.500"
    with pytest.raises(PolicyUtilityError, match="canonical decimal text"):
        PolicyUtilityEvidence.from_dict(noncanonical)