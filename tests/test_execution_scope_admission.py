from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_registry import (
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationKind,
    bind_bookmaker_integration,
)
from autosport.execution_scope_admission import (
    ExecutionAdmissionBlocker,
    ExecutionDataMode,
    ExecutionScopeAdmissionError,
    ExecutionScopeDescriptor,
    assess_execution_scope_pre_admission,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ProfileBinding,
    _bound_binding_sha256,
)


H1 = "1" * 64
H2 = "2" * 64
H3 = "3" * 64
H4 = "4" * 64
H5 = "5" * 64


def _profile(
    state: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    *,
    observed_at: str = "2026-09-23T17:00:00+00:00",
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="account-1",
        adapter_id="betfair-write-v1",
        adapter_version="1",
        profile_version=7,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                state,
            ),
        ),
        observed_at=observed_at,
        source_ref="provider-capability-evidence",
        source_payload_sha256=H1,
    )


def _governance(
    permission: GovernancePermissionState = GovernancePermissionState.PERMITTED,
    *,
    jurisdiction: str = "SK",
) -> BookmakerGovernanceEvidence:
    return BookmakerGovernanceEvidence(
        venue_id="betfair",
        account_id="account-1",
        jurisdiction=jurisdiction,
        terms_version="terms-2026-09",
        automation_permission=permission,
        observed_at="2026-09-23T17:01:00+00:00",
        source_ref="governance-evidence",
        source_payload_sha256=H2,
    )


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="account-1",
        event_id="event-1",
        market_id="1.2345",
        selection_id="123",
        side="BACK",
        requested_odds=Decimal("2.20"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-23T17:02:00+00:00",
        expires_at="2026-09-23T17:10:00+00:00",
    )


def _bound_plan(profile: BookmakerCapabilityProfile) -> BoundSupervisedExecutionPlan:
    action = _action()
    profile_binding = ProfileBinding(
        venue_id=profile.venue_id,
        account_id=profile.account_id,
        adapter_id=profile.adapter_id,
        adapter_version=profile.adapter_version,
        profile_version=profile.profile_version,
        profile_sha256=profile.profile_id,
    )
    provisional = ExecutionPlan(
        plan_id="pending",
        bookmaker_profile_version="profile-set-v1-test",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-23T17:03:00+00:00",
        actions=(action,),
    )
    bridge_id = _bound_binding_sha256(
        provisional,
        H1,
        H2,
        "intent-1",
        H3,
        H4,
        (profile_binding,),
        (),
    )
    final = ExecutionPlan(
        plan_id=f"supervised-v2-{bridge_id}",
        bookmaker_profile_version=provisional.bookmaker_profile_version,
        decision_id=provisional.decision_id,
        approval_id=provisional.approval_id,
        created_at=provisional.created_at,
        actions=provisional.actions,
    )
    return BoundSupervisedExecutionPlan(
        execution_plan=final,
        portfolio_plan_sha256=H1,
        economic_goal_contract_sha256=H2,
        intent_id="intent-1",
        intent_sha256=H3,
        approval_fingerprint=H4,
        profile_bindings=(profile_binding,),
        constraints=(),
    )


def _scope(**changes: object) -> ExecutionScopeDescriptor:
    values: dict[str, object] = {
        "action_id": "action-1",
        "provider_product_domain": "BETFAIR_EXCHANGE",
        "environment": "production",
        "jurisdiction": "SK",
        "sport": "soccer",
        "event_id": "event-1",
        "market_id": "1.2345",
        "selection_id": "123",
        "market_semantics_id": "betfair:soccer:match_odds:v1",
        "settlement_rule_id": "betfair:soccer:match_odds:rules:v1",
        "data_mode": ExecutionDataMode.LIVE,
        "action_kind": "PLACE_BET",
        "order_type": "LIMIT",
        "side": "BACK",
        "currency": "EUR",
        "strategy_id": "strategy-1",
        "purpose": "supervised_live_execution",
        "evidence_cutoff": "2026-09-23T17:04:00+00:00",
        "attempt_id": "attempt-1",
    }
    values.update(changes)
    return ExecutionScopeDescriptor(**values)


def _assessment(
    *,
    profile: BookmakerCapabilityProfile | None = None,
    governance: BookmakerGovernanceEvidence | None = None,
):
    profile = profile or _profile()
    governance = governance or _governance()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-23T17:01:30+00:00",
        source_ref="integration-evidence",
        source_payload_sha256=H5,
    )
    return assess_execution_scope_pre_admission(
        _scope(),
        profile=profile,
        governance=governance,
        integration=integration,
        bound_plan=_bound_plan(profile),
    )


def test_favorable_current_evidence_still_cannot_mint_write_admission() -> None:
    assessment = _assessment()

    assert assessment.provider_action_documented is True
    assert assessment.governance_automation_permitted is True
    assert assessment.integration_channel_bound is True
    assert assessment.product_supervised_action_issued is True

    assert assessment.provider_write_entitlement_proven is False
    assert assessment.authenticated_context_proven is False
    assert assessment.current_scope_capability_proven is False
    assert assessment.market_data_mode_suitable is False
    assert assessment.sport_market_semantics_proven is False
    assert assessment.settlement_rule_supported is False
    assert assessment.economic_risk_gates_passed is False
    assert assessment.dispatch_request_exactly_bound is False
    assert assessment.execution_admitted is False
    assert (
        ExecutionAdmissionBlocker.WRITE_ENTITLEMENT_UNPROVEN
        in assessment.blocking_reasons
    )


@pytest.mark.parametrize(
    "state",
    [
        BookmakerCapabilityState.UNKNOWN,
        BookmakerCapabilityState.UNSUPPORTED,
    ],
)
def test_missing_or_negative_place_bet_capability_is_explicit_blocker(
    state: BookmakerCapabilityState,
) -> None:
    assessment = _assessment(profile=_profile(state))

    assert assessment.provider_action_documented is False
    assert (
        ExecutionAdmissionBlocker.ACTION_NOT_DOCUMENTED
        in assessment.blocking_reasons
    )
    assert assessment.execution_admitted is False


@pytest.mark.parametrize(
    "permission",
    [
        GovernancePermissionState.UNKNOWN,
        GovernancePermissionState.PROHIBITED,
    ],
)
def test_governance_never_substitutes_for_write_entitlement(
    permission: GovernancePermissionState,
) -> None:
    assessment = _assessment(
        governance=_governance(permission),
    )

    assert assessment.governance_automation_permitted is False
    assert (
        ExecutionAdmissionBlocker.GOVERNANCE_AUTOMATION_NOT_PERMITTED
        in assessment.blocking_reasons
    )
    assert assessment.provider_write_entitlement_proven is False
    assert assessment.execution_admitted is False


def test_scope_must_match_product_issued_action_identity() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-23T17:01:30+00:00",
        source_ref="integration-evidence",
        source_payload_sha256=H5,
    )

    with pytest.raises(
        ExecutionScopeAdmissionError,
        match="scope does not match supervised action identity",
    ):
        assess_execution_scope_pre_admission(
            _scope(market_id="other-market"),
            profile=profile,
            governance=_governance(),
            integration=integration,
            bound_plan=_bound_plan(profile),
        )


def test_governance_jurisdiction_must_match_exact_scope() -> None:
    profile = _profile()
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-23T17:01:30+00:00",
        source_ref="integration-evidence",
        source_payload_sha256=H5,
    )

    with pytest.raises(
        ExecutionScopeAdmissionError,
        match="account/jurisdiction",
    ):
        assess_execution_scope_pre_admission(
            _scope(),
            profile=profile,
            governance=_governance(jurisdiction="GB"),
            integration=integration,
            bound_plan=_bound_plan(profile),
        )


def test_future_evidence_cannot_be_used_at_scope_cutoff() -> None:
    profile = _profile(observed_at="2026-09-23T17:05:00+00:00")
    integration = bind_bookmaker_integration(
        profile,
        integration_kind=BookmakerIntegrationKind.OFFICIAL_API,
        observed_at="2026-09-23T17:05:30+00:00",
        source_ref="integration-evidence",
        source_payload_sha256=H5,
    )

    with pytest.raises(
        ExecutionScopeAdmissionError,
        match="future evidence",
    ):
        assess_execution_scope_pre_admission(
            _scope(evidence_cutoff="2026-09-23T17:04:00+00:00"),
            profile=profile,
            governance=_governance(),
            integration=integration,
            bound_plan=_bound_plan(profile),
        )


def test_scope_identity_changes_for_sport_semantics_and_mode() -> None:
    base = _scope()
    sport = replace(base, sport="tennis")
    semantics = replace(
        base,
        market_semantics_id="betfair:soccer:correct_score:v1",
    )
    mode = replace(base, data_mode=ExecutionDataMode.PREMATCH)

    assert len(
        {
            base.descriptor_sha256,
            sport.descriptor_sha256,
            semantics.descriptor_sha256,
            mode.descriptor_sha256,
        }
    ) == 4


def test_assessment_cannot_be_replaced_into_positive_execution_admission() -> None:
    assessment = _assessment()
    replaced = replace(
        assessment,
        provider_action_documented=True,
        governance_automation_permitted=True,
        integration_channel_bound=True,
        product_supervised_action_issued=True,
    )

    assert replaced.provider_write_entitlement_proven is False
    assert replaced.execution_admitted is False
    assert replaced.assessment_sha256 == assessment.assessment_sha256


def test_scope_is_bound_to_exact_profile_and_integration_evidence() -> None:
    profile = _profile()
    assessment = _assessment(profile=profile)

    assert assessment.profile_id == profile.profile_id
    assert len(assessment.scope_sha256) == 64
    assert len(assessment.assessment_sha256) == 64
    assert assessment.scope_sha256 != assessment.scope.descriptor_sha256
