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
    REQUIRED_COST_CLASSES,
    ResolvedCostAuthority,
    ResolvedCurrencyAuthority,
    derive_campaign_economics,
)


CAMPAIGN = "a" * 64
SESSION_SHA = "b" * 64
RUN_SHA = "c" * 64
EVAL_SHA = "d" * 64
CURRENCY_SHA = "e" * 64
T0 = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)

SESSION_REF = AuthorityRef("campaign.session", "session-1", SESSION_SHA)
SESSION_MEMBER = MembershipRef("SESSION", "session-1", SESSION_SHA)
RUN_MEMBER = MembershipRef("RUN", "run-1", RUN_SHA)
EVAL_MEMBER = MembershipRef("EVALUATION", "eval-1", EVAL_SHA)
MEMBERSHIPS = tuple(sorted((SESSION_MEMBER, RUN_MEMBER, EVAL_MEMBER)))
CURRENCY_AUTHORITY = AuthorityRef("paper.bankroll.currency", "bankroll-1", CURRENCY_SHA)


class Resolver:
    def __init__(
        self,
        costs: tuple[CostEvidence, ...] = (),
        *,
        currency: ResolvedCurrencyAuthority | None = None,
    ) -> None:
        self._costs = {item.source: _resolved(item) for item in costs}
        self._currency = currency

    def resolve_cost(self, source: AuthorityRef) -> ResolvedCostAuthority | None:
        return self._costs.get(source)

    def resolve_currency(self, source: AuthorityRef) -> ResolvedCurrencyAuthority | None:
        if self._currency is not None and self._currency.source == source:
            return self._currency
        return None


CURRENCY_RESOLUTION = ResolvedCurrencyAuthority(
    source=CURRENCY_AUTHORITY,
    campaign_sha256=CAMPAIGN,
    currency="EUR",
)


def _source(label: str, digit: str) -> AuthorityRef:
    return AuthorityRef("cost.test.canonical", label, digit * 64)


def _cost(
    cost_class: CostClass,
    *,
    source: AuthorityRef,
    truth: CostTruth = CostTruth.KNOWN_ZERO,
    basis: CostBasis | None = CostBasis.OBSERVED_INCURRED,
    treatment: CostTreatment = CostTreatment.SUBTRACT_FROM_GROSS,
    unit: CostUnit = CostUnit.MONEY,
    currency: str | None = "EUR",
    amount: Decimal | None = Decimal("0"),
    available_at: datetime = T1,
    incurred_at: datetime | None = T0,
    shared_source: bool = False,
    allocation_authority: AuthorityRef | None = None,
    supersedes: tuple[str, ...] = (),
) -> CostEvidence:
    if truth in {CostTruth.UNKNOWN_UNPROVEN, CostTruth.NOT_APPLICABLE}:
        incurred_at = None
    return CostEvidence(
        cost_class=cost_class,
        truth=truth,
        basis=basis,
        treatment=treatment,
        source=source,
        campaign_sha256=CAMPAIGN,
        memberships=MEMBERSHIPS,
        unit=unit,
        currency=currency,
        amount=amount,
        observed_at=T0,
        available_at=available_at,
        incurred_at=incurred_at,
        shared_source=shared_source,
        allocation_authority=allocation_authority,
        supersedes_cost_evidence_ids=tuple(sorted(supersedes)),
    )


def _resolved(item: CostEvidence) -> ResolvedCostAuthority:
    return ResolvedCostAuthority(
        source=item.source,
        cost_class=item.cost_class,
        truth=item.truth,
        basis=item.basis,
        treatment=item.treatment,
        campaign_sha256=item.campaign_sha256,
        memberships=item.memberships,
        unit=item.unit,
        currency=item.currency,
        amount=item.amount,
        observed_at=item.observed_at,
        available_at=item.available_at,
        incurred_at=item.incurred_at,
        shared_source=item.shared_source,
        allocation_authority=item.allocation_authority,
    )


def _complete_costs(*, provider_amount: Decimal = Decimal("2.5")) -> tuple[CostEvidence, ...]:
    values = (
        _cost(
            CostClass.PROVIDER_DATA,
            source=_source("provider-bill", "1"),
            truth=CostTruth.KNOWN_AMOUNT,
            amount=provider_amount,
        ),
        _cost(
            CostClass.MODEL_COMPUTE_AI,
            source=_source("compute-none", "2"),
            truth=CostTruth.NOT_APPLICABLE,
            basis=CostBasis.AUTHORITATIVE_DECLARATION,
            treatment=CostTreatment.INFORMATIONAL,
            currency=None,
            amount=None,
        ),
        _cost(
            CostClass.EXECUTION_SLIPPAGE,
            source=_source("execution-slippage", "3"),
            truth=CostTruth.KNOWN_AMOUNT,
            basis=CostBasis.EMPIRICAL_DERIVED,
            treatment=CostTreatment.EMBEDDED_IN_GROSS,
            amount=Decimal("1.25"),
        ),
        _cost(
            CostClass.EXECUTION_FEES_COMMISSION_TAX,
            source=_source("execution-fees-zero", "4"),
        ),
        _cost(
            CostClass.FIXED_CAMPAIGN,
            source=_source("fixed-zero", "5"),
        ),
    )
    assert tuple(sorted((item.cost_class for item in values), key=lambda item: item.value)) == REQUIRED_COST_CLASSES
    return tuple(sorted(values, key=lambda item: item.cost_evidence_id))


def _derive(
    *,
    costs: tuple[CostEvidence, ...],
    resolver: Resolver | None = None,
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
        costs=costs,
        as_of=as_of,
        resolver=resolver or Resolver(costs, currency=CURRENCY_RESOLUTION),
        previous=previous,
    )


def test_all_required_cost_classes_are_fixed_by_code_not_caller_policy() -> None:
    assert set(REQUIRED_COST_CLASSES) == set(CostClass)

    provider_only = (_complete_costs()[0],)
    version = _derive(costs=provider_only)

    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert any(reason.startswith("MISSING_COST_CLASS:") for reason in version.incomplete_reasons)


def test_resolved_observed_costs_produce_complete_net_economics() -> None:
    costs = _complete_costs(provider_amount=Decimal("2.5"))
    version = _derive(costs=costs)

    assert version.known_cost_total == Decimal("2.5")
    assert version.net_after_known_costs == Decimal("7.5")
    assert version.completeness is EconomicCompleteness.COMPLETE_NET_ECONOMICS
    assert version.incomplete_reasons == ()


def test_fabricated_authority_ref_cannot_close_completeness() -> None:
    costs = _complete_costs()
    resolver = Resolver(tuple(item for item in costs if item.cost_class is not CostClass.PROVIDER_DATA), currency=CURRENCY_RESOLUTION)

    version = _derive(costs=costs, resolver=resolver)

    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert any(reason.startswith("UNRESOLVED_SOURCE_AUTHORITY:PROVIDER_DATA:") for reason in version.incomplete_reasons)
    assert "UNVERIFIED_COST_CLASS:PROVIDER_DATA" in version.incomplete_reasons


def test_resolver_payload_must_match_cost_claim_exactly() -> None:
    costs = _complete_costs()
    provider = next(item for item in costs if item.cost_class is CostClass.PROVIDER_DATA)
    forged = replace(provider, amount=Decimal("0"), truth=CostTruth.KNOWN_ZERO)
    all_claims = tuple(sorted((forged,) + tuple(item for item in costs if item is not provider), key=lambda item: item.cost_evidence_id))
    resolver = Resolver(costs, currency=CURRENCY_RESOLUTION)

    version = _derive(costs=all_claims, resolver=resolver)

    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert any(reason.startswith("UNRESOLVED_SOURCE_AUTHORITY:PROVIDER_DATA:") for reason in version.incomplete_reasons)


def test_missing_or_fabricated_campaign_currency_authority_is_incomplete() -> None:
    costs = _complete_costs()
    missing = _derive(costs=costs, currency=None, currency_authority=None)
    assert missing.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "MISSING_CAMPAIGN_CURRENCY_AUTHORITY" in missing.incomplete_reasons

    unresolved = _derive(costs=costs, resolver=Resolver(costs, currency=None))
    assert unresolved.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "UNRESOLVED_CAMPAIGN_CURRENCY_AUTHORITY" in unresolved.incomplete_reasons


def test_execution_slippage_already_in_run_pnl_is_not_subtracted_twice() -> None:
    costs = _complete_costs(provider_amount=Decimal("2"))
    version = _derive(costs=costs)

    assert version.known_cost_total == Decimal("2")
    assert version.net_after_known_costs == Decimal("8")


def test_configured_estimate_cannot_be_promoted_to_complete_actual_net() -> None:
    costs = list(_complete_costs())
    provider_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.PROVIDER_DATA)
    provider = costs[provider_index]
    costs[provider_index] = replace(provider, basis=CostBasis.CONFIGURED_ESTIMATE)
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))

    version = _derive(costs=costs_tuple)

    assert version.completeness is EconomicCompleteness.ESTIMATED_NET_ECONOMICS


def test_dimensionless_compute_cost_cannot_be_subtracted_from_money() -> None:
    costs = list(_complete_costs())
    compute_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.MODEL_COMPUTE_AI)
    costs[compute_index] = _cost(
        CostClass.MODEL_COMPUTE_AI,
        source=_source("voc-dimensionless", "6"),
        truth=CostTruth.KNOWN_AMOUNT,
        basis=CostBasis.EMPIRICAL_DERIVED,
        treatment=CostTreatment.INFORMATIONAL,
        unit=CostUnit.COMPUTE_CREDITS,
        currency=None,
        amount=Decimal("12"),
    )
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))

    version = _derive(costs=costs_tuple)

    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "INFORMATIONAL_ONLY:MODEL_COMPUTE_AI" in version.incomplete_reasons


def test_unknown_applicable_cost_blocks_complete_net_economics() -> None:
    costs = list(_complete_costs())
    provider_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.PROVIDER_DATA)
    costs[provider_index] = _cost(
        CostClass.PROVIDER_DATA,
        source=_source("provider-unknown", "7"),
        truth=CostTruth.UNKNOWN_UNPROVEN,
        basis=None,
        treatment=CostTreatment.INFORMATIONAL,
        currency="EUR",
        amount=None,
    )
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))

    version = _derive(costs=costs_tuple)

    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "UNRESOLVED_COST_CLASS:PROVIDER_DATA" in version.incomplete_reasons


def test_cross_currency_cost_stays_incomplete_without_fx() -> None:
    costs = list(_complete_costs())
    provider_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.PROVIDER_DATA)
    provider = costs[provider_index]
    costs[provider_index] = replace(provider, currency="USD")
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))

    version = _derive(costs=costs_tuple)

    assert version.known_cost_total == Decimal("0")
    assert version.completeness is EconomicCompleteness.INCOMPLETE_NET_ECONOMICS
    assert "CROSS_CURRENCY:PROVIDER_DATA:USD" in version.incomplete_reasons


def test_future_available_cost_cannot_backdate_version() -> None:
    costs = list(_complete_costs())
    provider_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.PROVIDER_DATA)
    costs[provider_index] = replace(costs[provider_index], available_at=T2 + timedelta(seconds=1))
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))

    with pytest.raises(CostEvidenceError, match="future-available"):
        _derive(costs=costs_tuple, as_of=T2)


def test_wrong_campaign_or_membership_fails_closed() -> None:
    costs = list(_complete_costs())
    provider_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.PROVIDER_DATA)
    costs[provider_index] = replace(costs[provider_index], campaign_sha256="8" * 64)
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))
    with pytest.raises(CostEvidenceError, match="different campaign"):
        _derive(costs=costs_tuple)

    costs = list(_complete_costs())
    alien = MembershipRef("RUN", "alien-run", "9" * 64)
    provider_index = next(i for i, item in enumerate(costs) if item.cost_class is CostClass.PROVIDER_DATA)
    costs[provider_index] = replace(costs[provider_index], memberships=(alien,))
    costs_tuple = tuple(sorted(costs, key=lambda item: item.cost_evidence_id))
    with pytest.raises(CostEvidenceError, match="outside the finalized campaign"):
        _derive(costs=costs_tuple)


def test_shared_subtractive_bill_requires_allocation_authority() -> None:
    with pytest.raises(CostEvidenceError, match="allocation authority"):
        _cost(
            CostClass.PROVIDER_DATA,
            source=_source("shared-bill", "a"),
            truth=CostTruth.KNOWN_AMOUNT,
            amount=Decimal("4"),
            shared_source=True,
        )


def test_successor_cannot_silently_drop_prior_cost() -> None:
    costs = _complete_costs()
    first = _derive(costs=costs)
    without_provider = tuple(item for item in costs if item.cost_class is not CostClass.PROVIDER_DATA)

    with pytest.raises(CostEvidenceError, match="silently drop"):
        _derive(costs=without_provider, previous=first)


def test_successor_requires_explicit_same_class_supersession() -> None:
    costs = _complete_costs()
    first = _derive(costs=costs, as_of=T1)
    provider = next(item for item in costs if item.cost_class is CostClass.PROVIDER_DATA)
    replacement = _cost(
        CostClass.PROVIDER_DATA,
        source=_source("provider-final-bill", "b"),
        truth=CostTruth.KNOWN_AMOUNT,
        amount=Decimal("4"),
        available_at=T2,
        supersedes=(provider.cost_evidence_id,),
    )
    successor_costs = tuple(sorted((replacement,) + tuple(item for item in costs if item is not provider), key=lambda item: item.cost_evidence_id))
    resolver = Resolver(successor_costs, currency=CURRENCY_RESOLUTION)

    second = _derive(costs=successor_costs, resolver=resolver, as_of=T2, previous=first)

    assert second.previous_version_id == first.version_id
    assert second.known_cost_total == Decimal("4")
    assert second.net_after_known_costs == Decimal("6")


def test_store_rejects_directly_constructed_forged_complete_version(tmp_path) -> None:
    costs = _complete_costs()
    resolver = Resolver(costs, currency=CURRENCY_RESOLUTION)
    valid = _derive(costs=costs, resolver=resolver)
    forged = replace(
        valid,
        known_cost_total=Decimal("0"),
        net_after_known_costs=Decimal("10"),
        completeness=EconomicCompleteness.COMPLETE_NET_ECONOMICS,
    )
    store = CampaignEconomicEvidenceStore(tmp_path, resolver=resolver)

    with pytest.raises(CostStoreError, match="derived fields"):
        store.append(forged)


def test_append_only_successor_restart_and_exact_retry(tmp_path) -> None:
    costs = _complete_costs()
    first_resolver = Resolver(costs, currency=CURRENCY_RESOLUTION)
    first = _derive(costs=costs, resolver=first_resolver, as_of=T1)
    provider = next(item for item in costs if item.cost_class is CostClass.PROVIDER_DATA)
    replacement = _cost(
        CostClass.PROVIDER_DATA,
        source=_source("provider-final-bill", "c"),
        truth=CostTruth.KNOWN_AMOUNT,
        amount=Decimal("4"),
        available_at=T2,
        supersedes=(provider.cost_evidence_id,),
    )
    successor_costs = tuple(sorted((replacement,) + tuple(item for item in costs if item is not provider), key=lambda item: item.cost_evidence_id))
    combined_resolver = Resolver(tuple(costs) + successor_costs, currency=CURRENCY_RESOLUTION)
    second = _derive(costs=successor_costs, resolver=combined_resolver, as_of=T2, previous=first)

    store = CampaignEconomicEvidenceStore(tmp_path, resolver=combined_resolver)
    assert store.append(first) == first.version_id
    assert store.append(first) == first.version_id
    assert store.append(second) == second.version_id
    assert store.latest(CAMPAIGN) == second
    assert store.verify_chain(CAMPAIGN) == (first, second)
    assert store.load(CAMPAIGN, first.version_id) == first

    restarted = CampaignEconomicEvidenceStore(tmp_path, resolver=combined_resolver)
    assert restarted.latest(CAMPAIGN) == second
    assert restarted.verify_chain(CAMPAIGN) == (first, second)


def test_tampered_version_fails_closed(tmp_path) -> None:
    costs = _complete_costs()
    resolver = Resolver(costs, currency=CURRENCY_RESOLUTION)
    version = _derive(costs=costs, resolver=resolver)
    store = CampaignEconomicEvidenceStore(tmp_path, resolver=resolver)
    store.append(version)
    path = tmp_path / "campaign_economics" / CAMPAIGN / "versions" / f"{version.version_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["known_cost_total"] = "0"
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    with pytest.raises(CostStoreError, match="integrity validation"):
        CampaignEconomicEvidenceStore(tmp_path, resolver=resolver).latest(CAMPAIGN)


def test_head_rollback_is_detected_while_newer_version_file_survives(tmp_path) -> None:
    costs = _complete_costs()
    provider = next(item for item in costs if item.cost_class is CostClass.PROVIDER_DATA)
    replacement = _cost(
        CostClass.PROVIDER_DATA,
        source=_source("provider-final-bill", "d"),
        truth=CostTruth.KNOWN_AMOUNT,
        amount=Decimal("4"),
        available_at=T2,
        supersedes=(provider.cost_evidence_id,),
    )
    successor_costs = tuple(sorted((replacement,) + tuple(item for item in costs if item is not provider), key=lambda item: item.cost_evidence_id))
    resolver = Resolver(tuple(costs) + successor_costs, currency=CURRENCY_RESOLUTION)
    first = _derive(costs=costs, resolver=resolver, as_of=T1)
    second = _derive(costs=successor_costs, resolver=resolver, as_of=T2, previous=first)
    store = CampaignEconomicEvidenceStore(tmp_path, resolver=resolver)
    store.append(first)
    store.append(second)

    head_path = tmp_path / "campaign_economics" / CAMPAIGN / "head.json"
    head_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "campaign_sha256": CAMPAIGN,
                "version_id": first.version_id,
                "record_sha256": first.record_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CostStoreError, match="orphaned or rolled-back"):
        CampaignEconomicEvidenceStore(tmp_path, resolver=resolver).latest(CAMPAIGN)
