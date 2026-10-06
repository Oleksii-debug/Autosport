from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.economic_goal_provenance as provenance_module

from autosport.economic_goal import (
    AutomationLevel,
    EconomicGoalContract,
    EconomicGoalContractError,
)
from autosport.economic_goal_provenance import (
    EconomicGoalProvenance,
    EconomicGoalProvenanceError,
    contract_sha256,
    provenance_for,
    verify_provenance,
)
from autosport.economic_goal_store import EconomicGoalStore


class _GoalSubclass(EconomicGoalContract):
    pass


class _StringSubclass(str):
    pass


class _IntSubclass(int):
    pass


class _ProvenanceSubclass(EconomicGoalProvenance):
    pass


class _HostileIdentityField:
    comparisons = 0

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return True

    def __str__(self) -> str:
        raise AssertionError("hostile identity field stringification executed")


def _goal(**changes: object) -> EconomicGoalContract:
    values: dict[str, object] = {
        "goal_id": "owner-goal-v1",
        "revision": 1,
        "bankroll_id": "paper-main",
        "currency": "EUR",
        "max_stake_fraction": Decimal("0.02"),
        "max_session_loss_fraction": Decimal("0.05"),
        "max_day_loss_fraction": Decimal("0.05"),
        "max_drawdown_fraction": Decimal("0.20"),
        "max_capital_at_risk_fraction": Decimal("0.20"),
        "max_event_concentration_fraction": Decimal("0.50"),
        "max_market_concentration_fraction": Decimal("0.50"),
        "max_provider_concentration_fraction": Decimal("0.50"),
        "max_sport_concentration_fraction": Decimal("0.50"),
        "max_turnover_fraction": Decimal("1.5"),
        "max_risk_of_ruin": Decimal("0.01"),
        "max_execution_slippage_fraction": Decimal("0.01"),
        "max_quote_age_seconds": Decimal("5"),
        "minimum_data_quality": Decimal("0.70"),
        "max_concurrent_positions": 5,
        "max_parlay_legs": 4,
        "automation_level": AutomationLevel.SUPERVISED_EXECUTION,
        "blocked_sports": frozenset({"football"}),
        "blocked_providers": frozenset({"provider:a"}),
        "blocked_markets": frozenset({"market:test"}),
    }
    values.update(changes)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


def test_provenance_authority_ignores_rebound_public_exports(
    monkeypatch,
) -> None:
    goal = _goal()
    expected = provenance_for(goal)

    monkeypatch.setattr(provenance_module, "EconomicGoalContract", _GoalSubclass)
    monkeypatch.setattr(provenance_module, "EconomicGoalProvenance", _ProvenanceSubclass)
    monkeypatch.setattr(
        provenance_module,
        "economic_goal_to_payload",
        lambda contract: {"forged": True},
    )
    monkeypatch.setattr(
        provenance_module,
        "contract_sha256",
        lambda contract: "0" * 64,
    )
    monkeypatch.setattr(
        provenance_module,
        "PROVENANCE_SCHEMA",
        "autosport.forged",
    )
    monkeypatch.setattr(
        provenance_module,
        "PROVENANCE_SCHEMA_VERSION",
        999,
    )

    derived = provenance_for(goal)
    assert derived == expected
    verify_provenance(goal, derived)


def test_provenance_hash_ignores_json_and_hashlib_rebinding(
    monkeypatch,
) -> None:
    goal = _goal()
    expected = contract_sha256(goal)

    def forbidden(*args, **kwargs):
        raise AssertionError("rebound provenance primitive executed")

    monkeypatch.setattr(provenance_module.json, "dumps", forbidden)
    monkeypatch.setattr(provenance_module.hashlib, "sha256", forbidden)
    monkeypatch.setattr(
        provenance_module,
        "_canonical_json",
        lambda payload: b"forged",
    )

    assert contract_sha256(goal) == expected
    assert provenance_for(goal).contract_sha256 == expected


def test_provenance_rejects_forged_subclass_after_export_rebinding(
    monkeypatch,
) -> None:
    goal = _goal()
    canonical = provenance_for(goal)
    forged = _ProvenanceSubclass(
        schema=canonical.schema,
        schema_version=canonical.schema_version,
        goal_id=canonical.goal_id,
        revision=canonical.revision,
        bankroll_id=canonical.bankroll_id,
        contract_sha256=canonical.contract_sha256,
    )
    monkeypatch.setattr(provenance_module, "EconomicGoalProvenance", _ProvenanceSubclass)

    with pytest.raises(
        EconomicGoalProvenanceError,
        match="canonical EconomicGoalProvenance",
    ):
        verify_provenance(goal, forged)


def test_provenance_rejects_contract_subclass() -> None:
    canonical = _goal()
    derived = _GoalSubclass(
        goal_id=canonical.goal_id,
        revision=canonical.revision,
        bankroll_id=canonical.bankroll_id,
        currency=canonical.currency,
        objective=canonical.objective,
        max_stake_fraction=canonical.max_stake_fraction,
        max_stake_amount=canonical.max_stake_amount,
        max_session_loss_fraction=canonical.max_session_loss_fraction,
        max_day_loss_fraction=canonical.max_day_loss_fraction,
        max_drawdown_fraction=canonical.max_drawdown_fraction,
        max_capital_at_risk_fraction=canonical.max_capital_at_risk_fraction,
        max_event_concentration_fraction=canonical.max_event_concentration_fraction,
        max_market_concentration_fraction=canonical.max_market_concentration_fraction,
        max_provider_concentration_fraction=canonical.max_provider_concentration_fraction,
        max_sport_concentration_fraction=canonical.max_sport_concentration_fraction,
        max_turnover_fraction=canonical.max_turnover_fraction,
        max_risk_of_ruin=canonical.max_risk_of_ruin,
        max_execution_slippage_fraction=canonical.max_execution_slippage_fraction,
        max_quote_age_seconds=canonical.max_quote_age_seconds,
        minimum_data_quality=canonical.minimum_data_quality,
        max_concurrent_positions=canonical.max_concurrent_positions,
        max_parlay_legs=canonical.max_parlay_legs,
        automation_level=canonical.automation_level,
        emergency_stop=canonical.emergency_stop,
        blocked_sports=canonical.blocked_sports,
        blocked_providers=canonical.blocked_providers,
        blocked_markets=canonical.blocked_markets,
    )

    with pytest.raises(EconomicGoalContractError, match="canonical EconomicGoalContract"):
        contract_sha256(derived)
    with pytest.raises(EconomicGoalContractError, match="canonical EconomicGoalContract"):
        provenance_for(derived)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("goal_id", " padded "),
        ("goal_id", "goal\x00suffix"),
        ("goal_id", "g" * 513),
        ("bankroll_id", " padded "),
        ("bankroll_id", "b" * 513),
    ],
)
def test_provenance_rejects_noncanonical_or_unbounded_identity_text(
    field: str,
    value: str,
) -> None:
    values = {
        "schema": "autosport.economic_goal_provenance",
        "schema_version": 1,
        "goal_id": "goal",
        "revision": 1,
        "bankroll_id": "bankroll",
        "contract_sha256": "a" * 64,
    }
    values[field] = value

    with pytest.raises(EconomicGoalProvenanceError):
        EconomicGoalProvenance(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", _StringSubclass("autosport.economic_goal_provenance")),
        ("schema_version", _IntSubclass(1)),
        ("goal_id", _StringSubclass("goal")),
        ("revision", _IntSubclass(1)),
        ("bankroll_id", _StringSubclass("bankroll")),
        ("contract_sha256", _StringSubclass("a" * 64)),
    ],
)
def test_provenance_evidence_rejects_scalar_subclasses(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "schema": "autosport.economic_goal_provenance",
        "schema_version": 1,
        "goal_id": "goal",
        "revision": 1,
        "bankroll_id": "bankroll",
        "contract_sha256": "a" * 64,
    }
    values[field] = value

    with pytest.raises(EconomicGoalProvenanceError):
        EconomicGoalProvenance(**values)  # type: ignore[arg-type]



def test_provenance_revalidation_ignores_validator_rebinding(
    monkeypatch,
) -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    object.__setattr__(goal, "max_stake_fraction", object())
    object.__setattr__(evidence, "goal_id", object())

    monkeypatch.setattr(
        provenance_module._CANONICAL_GOAL_TYPE,
        "__post_init__",
        lambda self: None,
    )
    monkeypatch.setattr(
        provenance_module._CANONICAL_PROVENANCE_TYPE,
        "__post_init__",
        lambda self: None,
    )

    with pytest.raises(EconomicGoalContractError):
        contract_sha256(goal)

    with pytest.raises(EconomicGoalProvenanceError):
        verify_provenance(_goal(), evidence)


def test_verify_provenance_rejects_contract_subclass_before_reads() -> None:
    canonical = _goal()
    evidence = provenance_for(canonical)
    derived = _GoalSubclass(
        goal_id=canonical.goal_id,
        revision=canonical.revision,
        bankroll_id=canonical.bankroll_id,
        currency=canonical.currency,
        objective=canonical.objective,
        max_stake_fraction=canonical.max_stake_fraction,
        max_stake_amount=canonical.max_stake_amount,
        max_session_loss_fraction=canonical.max_session_loss_fraction,
        max_day_loss_fraction=canonical.max_day_loss_fraction,
        max_drawdown_fraction=canonical.max_drawdown_fraction,
        max_capital_at_risk_fraction=canonical.max_capital_at_risk_fraction,
        max_event_concentration_fraction=canonical.max_event_concentration_fraction,
        max_market_concentration_fraction=canonical.max_market_concentration_fraction,
        max_provider_concentration_fraction=canonical.max_provider_concentration_fraction,
        max_sport_concentration_fraction=canonical.max_sport_concentration_fraction,
        max_turnover_fraction=canonical.max_turnover_fraction,
        max_risk_of_ruin=canonical.max_risk_of_ruin,
        max_execution_slippage_fraction=canonical.max_execution_slippage_fraction,
        max_quote_age_seconds=canonical.max_quote_age_seconds,
        minimum_data_quality=canonical.minimum_data_quality,
        max_concurrent_positions=canonical.max_concurrent_positions,
        max_parlay_legs=canonical.max_parlay_legs,
        automation_level=canonical.automation_level,
        emergency_stop=canonical.emergency_stop,
        blocked_sports=canonical.blocked_sports,
        blocked_providers=canonical.blocked_providers,
        blocked_markets=canonical.blocked_markets,
    )

    with pytest.raises(
        EconomicGoalProvenanceError,
        match="canonical EconomicGoalContract",
    ):
        verify_provenance(derived, evidence)


def test_verify_provenance_rejects_provenance_subclass() -> None:
    goal = _goal()
    canonical = provenance_for(goal)
    derived = _ProvenanceSubclass(
        schema=canonical.schema,
        schema_version=canonical.schema_version,
        goal_id=canonical.goal_id,
        revision=canonical.revision,
        bankroll_id=canonical.bankroll_id,
        contract_sha256=canonical.contract_sha256,
    )

    with pytest.raises(
        EconomicGoalProvenanceError,
        match="canonical EconomicGoalProvenance",
    ):
        verify_provenance(goal, derived)



def test_provenance_for_revalidates_mutated_contract_before_identity_reads() -> None:
    goal = _goal()
    _HostileIdentityField.comparisons = 0
    object.__setattr__(
        goal,
        "goal_id",
        _HostileIdentityField(),
    )

    with pytest.raises(EconomicGoalContractError):
        provenance_for(goal)

    assert _HostileIdentityField.comparisons == 0


def test_verify_provenance_revalidates_mutated_contract_before_comparison() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    _HostileIdentityField.comparisons = 0
    object.__setattr__(
        goal,
        "goal_id",
        _HostileIdentityField(),
    )

    with pytest.raises(EconomicGoalContractError):
        verify_provenance(goal, evidence)

    assert _HostileIdentityField.comparisons == 0


def test_verify_provenance_revalidates_mutated_evidence_before_comparison() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    _HostileIdentityField.comparisons = 0
    object.__setattr__(
        evidence,
        "goal_id",
        _HostileIdentityField(),
    )

    with pytest.raises(EconomicGoalProvenanceError):
        verify_provenance(goal, evidence)

    assert _HostileIdentityField.comparisons == 0


def test_decision_identity_revalidates_mutated_evidence_before_formatting() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    object.__setattr__(
        evidence,
        "goal_id",
        _HostileIdentityField(),
    )

    with pytest.raises(EconomicGoalProvenanceError):
        _ = evidence.decision_identity


def test_provenance_is_deterministic_and_revision_specific() -> None:
    goal = _goal()
    first = provenance_for(goal)
    second = provenance_for(goal)

    assert first == second
    assert first.schema_version == 1
    assert first.contract_sha256 == contract_sha256(goal)
    assert first.decision_identity == f"{goal.goal_id}@{goal.revision}:{first.contract_sha256}"

    changed = replace(goal, revision=2, max_stake_fraction=Decimal("0.01"))
    changed_provenance = provenance_for(changed)
    assert changed_provenance.contract_sha256 != first.contract_sha256
    assert changed_provenance.decision_identity != first.decision_identity


def test_provenance_verifies_after_durable_restart_readback(tmp_path) -> None:
    goal = _goal()
    store = EconomicGoalStore(tmp_path)
    store.initialize_owner(goal)

    restored = EconomicGoalStore(tmp_path).load()
    verify_provenance(restored, provenance_for(restored))
    assert restored == goal


def test_provenance_fails_closed_after_contract_tampering() -> None:
    goal = _goal()
    evidence = provenance_for(goal)
    tampered = replace(goal, max_stake_fraction=Decimal("0.019"))

    with pytest.raises(EconomicGoalProvenanceError, match="contract_sha256 mismatch"):
        verify_provenance(tampered, evidence)


def test_provenance_rejects_identity_rebinding() -> None:
    goal = _goal()
    evidence = provenance_for(goal)

    with pytest.raises(EconomicGoalProvenanceError, match="goal_id mismatch"):
        verify_provenance(replace(goal, goal_id="other-goal"), evidence)

    with pytest.raises(EconomicGoalProvenanceError, match="revision mismatch"):
        verify_provenance(replace(goal, revision=2), evidence)

    with pytest.raises(EconomicGoalProvenanceError, match="bankroll_id mismatch"):
        verify_provenance(replace(goal, bankroll_id="other-bankroll"), evidence)


def test_provenance_schema_validation_is_fail_closed() -> None:
    with pytest.raises(EconomicGoalProvenanceError, match="SHA-256 hex"):
        EconomicGoalProvenance(
            schema="autosport.economic_goal_provenance",
            schema_version=1,
            goal_id="goal",
            revision=1,
            bankroll_id="bankroll",
            contract_sha256="not-a-digest",
        )
