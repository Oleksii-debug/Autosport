from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.campaign_cost_evidence import (
    CampaignEconomicEvidenceVersion,
    CostEvidenceError,
    EconomicCompleteness,
    derive_campaign_economics,
)
from autosport.campaign_denomination import CampaignDenominationError
from autosport.campaign_economic_authority import (
    CampaignEconomicAuthorityError,
    FinalizedCampaignAuthority,
)
from autosport.campaign_economic_store import CampaignEconomicEvidenceStore
from autosport.campaign_evidence import CampaignOutcome, CampaignReadiness
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.integrity import atomic_write_json
from autosport.workspace_lock import WorkspaceEconomicLock
from test_campaign_cost_evidence import T1, T2, _cost, _fixture_authority
from test_campaign_evidence import PaperCampaignTests


def _goal(
    *,
    goal_id: str = "owner-goal-a",
    revision: int = 3,
    bankroll_id: str = "bankroll-a",
    currency: str = "EUR",
) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id=goal_id,
        revision=revision,
        bankroll_id=bankroll_id,
        currency=currency,
    )


def _runtime_provenance(goal: EconomicGoalContract) -> dict[str, object]:
    provenance = provenance_for(goal)
    return {
        "schema": provenance.schema,
        "schema_version": provenance.schema_version,
        "goal_id": provenance.goal_id,
        "revision": provenance.revision,
        "bankroll_id": provenance.bankroll_id,
        "currency": goal.currency,
        "contract_sha256": provenance.contract_sha256,
    }


def _fixture_authority_with_goal(
    goal: EconomicGoalContract,
) -> tuple[PaperCampaignTests, FinalizedCampaignAuthority]:
    fixture = PaperCampaignTests(
        methodName="test_session_evidence_hash_binds_window_and_metrics"
    )
    fixture.setUp()
    fixture._economic_goal_provenance = _runtime_provenance(goal)
    campaign = fixture.campaign()
    fixture.add_session(campaign, fixture.session())
    campaign.finalize(
        outcome=CampaignOutcome.POSITIVE,
        readiness=CampaignReadiness.ELIGIBLE,
        finalized_at="2026-09-11T00:00:00Z",
    )
    return fixture, FinalizedCampaignAuthority(campaign)


def _denomination_as_of(authority: FinalizedCampaignAuthority):
    binding = authority.denomination_binding()
    assert binding is not None
    return binding.available_at + timedelta(microseconds=1)


def _remove_persisted_binding_for_test(
    authority: FinalizedCampaignAuthority,
) -> None:
    registry = authority._registry()
    with WorkspaceEconomicLock(registry.path.parent):
        state = registry._read()
        bindings = state.get("campaign_denomination_bindings")
        assert type(bindings) is dict
        bindings.pop(authority._binding_key())
        if not bindings:
            state.pop("campaign_denomination_bindings")
        atomic_write_json(registry.path, state)


def test_missing_persisted_binding_cannot_narrow_currency_incompleteness() -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        _remove_persisted_binding_for_test(authority)
        assert authority.denomination_binding() is None
        version = derive_campaign_economics(campaign=authority, costs=(), as_of=T2)
        assert version.denomination_binding is None
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_class_monkeypatch_cannot_synthesize_positive_denomination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        issued = authority.denomination_binding()
        assert issued is not None
        _remove_persisted_binding_for_test(authority)
        monkeypatch.setattr(
            FinalizedCampaignAuthority,
            "denomination_binding",
            lambda self: issued,
        )

        version = derive_campaign_economics(
            campaign=authority,
            costs=(),
            as_of=issued.available_at + timedelta(microseconds=1),
        )
        assert version.denomination_binding is None
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_product_binding_is_reissued_from_frozen_run_not_current_goal() -> None:
    goal_a = _goal()
    before_issuance = datetime.now(timezone.utc)
    fixture, authority = _fixture_authority_with_goal(goal_a)
    try:
        binding = authority.denomination_binding()
        assert binding is not None
        assert before_issuance <= binding.available_at <= datetime.now(timezone.utc)
        assert binding.economic_goal_id == goal_a.goal_id
        assert binding.economic_goal_revision == goal_a.revision
        assert binding.economic_goal_sha256 == provenance_for(goal_a).contract_sha256
        assert binding.bankroll_id == goal_a.bankroll_id
        assert binding.currency == "EUR"
        assert binding.portfolio_identity.startswith("paper-portfolio:")
        assert binding.session_id.startswith("session-membership:")
        assert binding.run_id.startswith("run-membership:")
        assert binding.evaluation_id.startswith("evaluation-membership:")

        current_b = _goal(
            goal_id="owner-goal-b",
            revision=1,
            bankroll_id="bankroll-b",
            currency="USD",
        )
        EconomicGoalStore(fixture.workspace).initialize_owner(current_b)
        assert authority.denomination_binding() == binding
    finally:
        fixture.doCleanups()


def test_persisted_binding_re_resolves_after_authority_restart() -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        binding = authority.denomination_binding()
        assert binding is not None
        restarted = FinalizedCampaignAuthority(authority._campaign)
        assert restarted.denomination_binding() == binding
        assert restarted.issue_denomination_binding() == binding
        with pytest.raises(TypeError):
            FinalizedCampaignAuthority(authority._campaign, clock=lambda: T1)
    finally:
        fixture.doCleanups()


def test_denominated_economics_persists_binding_and_restart_rederives_it(
    tmp_path,
) -> None:
    fixture, authority = _fixture_authority_with_goal(_goal())
    try:
        version = derive_campaign_economics(
            campaign=authority,
            costs=(),
            as_of=_denomination_as_of(authority),
        )
        assert version.denomination_binding == authority.denomination_binding()
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" not in version.incomplete_reasons
        assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
        assert version.net_after_known_costs is None
        assert "denomination_binding" in version.to_dict()

        roundtrip = CampaignEconomicEvidenceVersion.from_dict(version.to_dict())
        assert roundtrip == version

        root = tmp_path / "economic-workspace"
        authority_root = tmp_path / "monotonic-authority"
        store = CampaignEconomicEvidenceStore(
            root,
            campaign=authority,
            authority_root=authority_root,
        )
        assert store.append(version) == version.version_id

        restarted = CampaignEconomicEvidenceStore(
            root,
            campaign=authority,
            authority_root=authority_root,
        )
        loaded = restarted.latest()
        assert loaded == version
        assert loaded is not None
        assert loaded.denomination_binding == authority.denomination_binding()
    finally:
        fixture.doCleanups()


def test_non_iso_shape_valid_currency_cannot_become_positive_authority() -> None:
    fixture = PaperCampaignTests(
        methodName="test_session_evidence_hash_binds_window_and_metrics"
    )
    fixture.setUp()
    try:
        fixture._economic_goal_provenance = _runtime_provenance(
            _goal(currency="ZZZ")
        )
        campaign = fixture.campaign()
        fixture.add_session(campaign, fixture.session())
        with pytest.raises(CampaignDenominationError, match="ISO 4217"):
            campaign.finalize(
                outcome=CampaignOutcome.POSITIVE,
                readiness=CampaignReadiness.ELIGIBLE,
                finalized_at="2026-09-11T00:00:00Z",
            )
    finally:
        fixture.doCleanups()


def test_cross_currency_cost_rejects_before_campaign_money_arithmetic() -> None:
    fixture, authority = _fixture_authority_with_goal(_goal(currency="EUR"))
    try:
        with pytest.raises(CostEvidenceError, match="explicit FX authority"):
            derive_campaign_economics(
                campaign=authority,
                costs=(_cost(authority, currency="USD"),),
                as_of=_denomination_as_of(authority),
            )

        same_currency = derive_campaign_economics(
            campaign=authority,
            costs=(_cost(authority, currency="EUR"),),
            as_of=_denomination_as_of(authority),
        )
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" not in same_currency.incomplete_reasons
        assert "UNRESOLVED_COST_AUTHORITY:PROVIDER_DATA" in same_currency.incomplete_reasons
        assert same_currency.known_cost_total == Decimal("0")
        assert same_currency.net_after_known_costs is None
    finally:
        fixture.doCleanups()


def test_legacy_run_without_explicit_currency_cannot_mint_binding() -> None:
    fixture, authority = _fixture_authority()
    try:
        assert authority.denomination_binding() is None
        assert authority.issue_denomination_binding() is None
        version = derive_campaign_economics(
            campaign=authority,
            costs=(),
            as_of=T2,
        )
        assert version.denomination_binding is None
        assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in version.incomplete_reasons
    finally:
        fixture.doCleanups()


def test_caller_relabel_cannot_replace_product_issued_binding() -> None:
    fixture, authority = _fixture_authority_with_goal(_goal(currency="EUR"))
    try:
        binding = authority.denomination_binding()
        assert binding is not None
        for forged in (
            replace(binding, currency="USD", binding_id=""),
            replace(
                binding,
                economic_goal_revision=binding.economic_goal_revision + 1,
                binding_id="",
            ),
            replace(binding, economic_goal_sha256="f" * 64, binding_id=""),
            replace(binding, bankroll_id="caller-bankroll", binding_id=""),
            replace(binding, portfolio_identity="caller-portfolio", binding_id=""),
        ):
            with pytest.raises(
                CampaignEconomicAuthorityError,
                match="does not match canonical run authority",
            ):
                authority.verify_denomination_binding(forged)

        with pytest.raises(CostEvidenceError, match="future-available"):
            derive_campaign_economics(
                campaign=authority,
                costs=(),
                as_of=binding.available_at - timedelta(microseconds=1),
            )
    finally:
        fixture.doCleanups()
