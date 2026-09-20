from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.campaign_cost_evidence import (
    AuthorityRef,
    CampaignEconomicEvidenceStore,
    CostBasis,
    CostClass,
    CostEvidence,
    CostEvidenceError,
    CostStoreError,
    CostTreatment,
    CostTruth,
    CostUnit,
    EconomicCompleteness,
    MembershipRef,
    derive_campaign_economics,
)


CAMPAIGN = "a" * 64
SESSION_SHA = "b" * 64
RUN_SHA = "c" * 64
EVAL_SHA = "d" * 64
CURRENCY_SHA = "e" * 64
SOURCE_SHA = "f" * 64
T0 = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


SESSION_REF = AuthorityRef("campaign.session", "session-1", SESSION_SHA)
SESSION_MEMBER = MembershipRef("SESSION", "session-1", SESSION_SHA)
RUN_MEMBER = MembershipRef("RUN", "run-1", RUN_SHA)
EVAL_MEMBER = MembershipRef("EVALUATION", "eval-1", EVAL_SHA)
MEMBERSHIPS = tuple(sorted((SESSION_MEMBER, RUN_MEMBER, EVAL_MEMBER)))
CURRENCY_AUTHORITY = AuthorityRef("paper.bankroll.currency", "bankroll-1", CURRENCY_SHA)


def _source(label: str, digest: str = SOURCE_SHA) -> AuthorityRef:
    return AuthorityRef("cost.test", label, digest)


def _money_cost(
    *,
    cost_class: CostClass = CostClass.PROVIDER_DATA,
    source: AuthorityRef | None = None,
    truth: CostTruth = CostTruth.KNOWN_AMOUNT,
    basis: CostBasis | None = CostBasis.OBSERVED_INCURRED,
    treatment: CostTreatment = CostTreatment.SUBTRACT_FROM_GROSS,
    currency: str | None = "EUR",
    amount: Decimal | None = Decimal("2.5"),
    available_at: datetime = T1,
    shared_source: bool = False,
    allocation_authority: AuthorityRef | None = None,
) -> CostEvidence:
    return CostEvidence(
        cost_class=cost_class,
        truth=truth,
        basis=basis,
        treatment=treatment,
        source=source or _source(f"source-{cost_class.value}"),
        campaign_sha256=CAMPAIGN,
        memberships=MEMBERSHIPS,
        unit=CostUnit.MONEY,
        currency=currency,
        amount=amount,
        incurred_at=T0,
        observed_at=T0,
        available_at=available_at,
        shared_source=shared_source,
        allocation_authority=allocation_authority,
    )


def _derive(
    *,
    costs: tuple[CostEvidence, ...] = (),
    required: tuple[CostClass, ...] = (),
    currency: str | None = "EUR",
    currency_authority: AuthorityRef | None = CURRENCY_AUTHORITY,
    gross: Decimal = Decimal("10"),
    as_of: datetime = T2,
    previous=None,
):
    return derive_campaign_economics(
        campaign_sha256=CAMPAIGN,
        session_evidence_refs=(SESSION_REF,),
        membership_refs=MEMBERSHIPS,
        gross_run_pnl=gross,
        currency=currency,
        currency_authority=currency_authority,
        required_cost_classes=required,
        costs=costs,
        as_of=as_of,
        previous=previous,
    )


def test_positive_campaign_pnl_without_currency_is_not_complete_net_economics() -> None:
    version = _derive(currency=None, currency_authority=None)

    assert version.gross_run_pnl == Decimal("10")
    assert version.net_after_known_costs is None
    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert version.incomplete_reasons == ("MISSING_CAMPAIGN_CURRENCY_AUTHORITY",)


def test_observed_incurred_money_cost_is_subtracted_once() -> None:
    cost = _money_cost(amount=Decimal("2.5"))
    version = _derive(costs=(cost,), required=(CostClass.PROVIDER_DATA,))

    assert version.known_cost_total == Decimal("2.5")
    assert version.net_after_known_costs == Decimal("7.5")
    assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS
    assert version.incomplete_reasons == ()


def test_cost_embedded_in_authoritative_gross_is_not_subtracted_twice() -> None:
    slippage = _money_cost(
        cost_class=CostClass.EXECUTION_SLIPPAGE,
        treatment=CostTreatment.EMBEDDED_IN_GROSS,
        basis=CostBasis.EMPIRICAL_DERIVED,
        amount=Decimal("1.25"),
    )
    version = _derive(costs=(slippage,), required=(CostClass.EXECUTION_SLIPPAGE,))

    assert version.known_cost_total == Decimal("0")
    assert version.net_after_known_costs == Decimal("10")
    assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS


def test_configured_or_synthetic_amount_can_only_produce_estimated_net_economics() -> None:
    estimate = _money_cost(basis=CostBasis.CONFIGURED_ESTIMATE, amount=Decimal("3"))
    version = _derive(costs=(estimate,), required=(CostClass.PROVIDER_DATA,))

    assert version.net_after_known_costs == Decimal("7")
    assert version.completeness is EconomicCompleteness.ESTIMATED_NET_ECONOMICS


def test_dimensionless_compute_cost_cannot_be_subtracted_from_bankroll_money() -> None:
    compute = CostEvidence(
        cost_class=CostClass.MODEL_COMPUTE_AI,
        truth=CostTruth.KNOWN_AMOUNT,
        basis=CostBasis.EMPIRICAL_DERIVED,
        treatment=CostTreatment.INFORMATIONAL,
        source=_source("voc-compute"),
        campaign_sha256=CAMPAIGN,
        memberships=MEMBERSHIPS,
        unit=CostUnit.COMPUTE_CREDITS,
        currency=None,
        amount=Decimal("12"),
        incurred_at=T0,
        observed_at=T0,
        available_at=T1,
    )
    version = _derive(costs=(compute,), required=(CostClass.MODEL_COMPUTE_AI,))

    assert version.known_cost_total == Decimal("0")
    assert version.net_after_known_costs == Decimal("10")
    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "INFORMATIONAL_ONLY:MODEL_COMPUTE_AI" in version.incomplete_reasons


def test_unknown_applicable_cost_blocks_complete_net_economics() -> None:
    unknown = _money_cost(
        truth=CostTruth.UNKNOWN_UNPROVEN,
        basis=None,
        treatment=CostTreatment.INFORMATIONAL,
        currency="EUR",
        amount=None,
    )
    version = _derive(costs=(unknown,), required=(CostClass.PROVIDER_DATA,))

    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "UNRESOLVED_COST_CLASS:PROVIDER_DATA" in version.incomplete_reasons


def test_authoritatively_not_applicable_cost_class_can_be_complete() -> None:
    not_applicable = _money_cost(
        truth=CostTruth.NOT_APPLICABLE,
        basis=CostBasis.AUTHORITATIVE_DECLARATION,
        treatment=CostTreatment.INFORMATIONAL,
        currency=None,
        amount=None,
    )
    version = _derive(costs=(not_applicable,), required=(CostClass.PROVIDER_DATA,))

    assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS
    assert version.known_cost_total == Decimal("0")


def test_cross_currency_cost_stays_incomplete_without_fx_authority() -> None:
    usd_cost = _money_cost(currency="USD", amount=Decimal("2"))
    version = _derive(costs=(usd_cost,), required=(CostClass.PROVIDER_DATA,))

    assert version.known_cost_total == Decimal("0")
    assert version.net_after_known_costs == Decimal("10")
    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "CROSS_CURRENCY:PROVIDER_DATA:USD" in version.incomplete_reasons


def test_future_available_cost_cannot_backdate_an_earlier_economic_version() -> None:
    future = _money_cost(available_at=T2 + timedelta(seconds=1))

    with pytest.raises(CostEvidenceError, match="future-available"):
        _derive(costs=(future,), required=(CostClass.PROVIDER_DATA,), as_of=T2)


def test_wrong_campaign_or_membership_fails_closed() -> None:
    wrong_campaign = replace(_money_cost(), campaign_sha256="1" * 64)
    with pytest.raises(CostEvidenceError, match="different campaign"):
        _derive(costs=(wrong_campaign,), required=(CostClass.PROVIDER_DATA,))

    alien_membership = MembershipRef("RUN", "run-alien", "2" * 64)
    wrong_membership = replace(_money_cost(), memberships=(alien_membership,))
    with pytest.raises(CostEvidenceError, match="outside the finalized campaign"):
        _derive(costs=(wrong_membership,), required=(CostClass.PROVIDER_DATA,))


def test_same_immutable_source_cost_cannot_be_double_counted() -> None:
    shared_source = _source("same-bill")
    first = _money_cost(source=shared_source, amount=Decimal("2"))
    second = _money_cost(
        cost_class=CostClass.FIXED_CAMPAIGN,
        source=shared_source,
        amount=Decimal("1"),
    )

    with pytest.raises(CostEvidenceError, match="same immutable source"):
        _derive(
            costs=(first, second),
            required=(CostClass.PROVIDER_DATA, CostClass.FIXED_CAMPAIGN),
        )


def test_shared_subtractive_bill_requires_immutable_allocation_authority() -> None:
    with pytest.raises(CostEvidenceError, match="allocation authority"):
        _money_cost(shared_source=True, allocation_authority=None)

    allocation = AuthorityRef("billing.allocation", "allocation-1", "3" * 64)
    allocated = _money_cost(shared_source=True, allocation_authority=allocation)
    version = _derive(costs=(allocated,), required=(CostClass.PROVIDER_DATA,))
    assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS


def test_successor_version_cannot_rewrite_finalized_campaign_truth() -> None:
    first = _derive(costs=(_money_cost(amount=Decimal("2")),), required=(CostClass.PROVIDER_DATA,))

    with pytest.raises(CostEvidenceError, match="gross run P&L"):
        _derive(
            costs=(_money_cost(amount=Decimal("3")),),
            required=(CostClass.PROVIDER_DATA,),
            gross=Decimal("11"),
            previous=first,
        )


def test_late_authoritative_cost_creates_append_only_successor(tmp_path) -> None:
    unknown = _money_cost(
        truth=CostTruth.UNKNOWN_UNPROVEN,
        basis=None,
        treatment=CostTreatment.INFORMATIONAL,
        amount=None,
    )
    first = _derive(costs=(unknown,), required=(CostClass.PROVIDER_DATA,), as_of=T1)
    observed = _money_cost(
        source=_source("actual-bill", "4" * 64),
        amount=Decimal("4"),
        available_at=T2,
    )
    second = _derive(
        costs=(observed,),
        required=(CostClass.PROVIDER_DATA,),
        as_of=T2,
        previous=first,
    )

    store = CampaignEconomicEvidenceStore(tmp_path)
    first_id = store.append(first)
    second_id = store.append(second)

    assert first_id != second_id
    assert second.previous_version_id == first_id
    assert store.latest(CAMPAIGN) == second
    assert store.verify_chain(CAMPAIGN) == (first, second)
    assert store.load(CAMPAIGN, first_id) == first

    restarted = CampaignEconomicEvidenceStore(tmp_path)
    assert restarted.latest(CAMPAIGN) == second
    assert restarted.verify_chain(CAMPAIGN) == (first, second)


def test_exact_store_retry_is_idempotent(tmp_path) -> None:
    version = _derive(costs=(_money_cost(),), required=(CostClass.PROVIDER_DATA,))
    store = CampaignEconomicEvidenceStore(tmp_path)

    assert store.append(version) == version.version_id
    assert store.append(version) == version.version_id
    assert store.verify_chain(CAMPAIGN) == (version,)


def test_tampered_immutable_version_fails_closed_on_restart(tmp_path) -> None:
    version = _derive(costs=(_money_cost(),), required=(CostClass.PROVIDER_DATA,))
    store = CampaignEconomicEvidenceStore(tmp_path)
    store.append(version)

    path = (
        tmp_path
        / "campaign_economics"
        / CAMPAIGN
        / "versions"
        / f"{version.version_id}.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["gross_run_pnl"] = "11"
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    restarted = CampaignEconomicEvidenceStore(tmp_path)
    with pytest.raises(CostStoreError, match="integrity validation"):
        restarted.latest(CAMPAIGN)
