from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.campaign_cost_evidence import CostTruth
from autosport.monetary_cost_authority import (
    MonetaryAuthorityError,
    MonetaryCostAuthority,
    MonetaryEvidenceQuality,
    MonetarySourceClass,
    MonetarySourceSnapshot,
    SharedAllocationSnapshot,
)
from test_campaign_cost_evidence import _fixture_authority


T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)


class Resolver:
    def __init__(self, values):
        self.values = values

    def resolve(self, locator: str, *, as_of: datetime):
        return self.values.get(locator)


class AllocationResolver:
    def __init__(self, values):
        self.values = values

    def resolve(self, locator: str, *, as_of: datetime):
        return self.values.get(locator)


def _snapshot(campaign_ids, *, evidence_id="invoice-1", amount="12.50", quality=MonetaryEvidenceQuality.INCURRED, available_at=T1, supersedes=None, currency="EUR"):
    return MonetarySourceSnapshot(
        authority_id="billing-ledger",
        evidence_id=evidence_id,
        content_sha256=("a" if evidence_id == "invoice-1" else "b") * 64,
        amount=Decimal(amount),
        currency=currency,
        campaign_ids=tuple(sorted(campaign_ids)),
        coverage_start=T0 - timedelta(hours=1),
        coverage_end=T0,
        observed_at=T0,
        available_at=available_at,
        provenance="resolver:billing-statement",
        quality=quality,
        supersedes_evidence_id=supersedes,
    )


def test_product_resolver_issues_exact_incurred_cost_and_rejects_forgery(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        campaign_id = campaign.projection().campaign_id
        source = _snapshot((campaign_id,))
        authority = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: Resolver({"invoice": source})},
        )
        source_ref = authority.capture_source(
            MonetarySourceClass.PROVIDER_BILLING,
            "invoice",
            as_of=T1,
        )
        cost = authority.issue_cost_evidence(campaign=campaign, source_ref=source_ref, as_of=T1)

        assert cost.amount == Decimal("12.50")
        assert cost.currency == "EUR"
        assert cost.truth is CostTruth.KNOWN_AMOUNT
        assert authority.verify_cost_evidence(campaign=campaign, evidence=cost, as_of=T1)
        assert not authority.verify_cost_evidence(
            campaign=campaign,
            evidence=replace(cost, amount=Decimal("999")),
            as_of=T1,
        )
    finally:
        fixture.doCleanups()


def test_estimate_simulation_and_future_availability_cannot_mint_incurred_truth(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        campaign_id = campaign.projection().campaign_id
        estimate = _snapshot((campaign_id,), quality=MonetaryEvidenceQuality.ESTIMATE)
        simulated = _snapshot((campaign_id,), evidence_id="simulation-1", quality=MonetaryEvidenceQuality.SIMULATED)
        future = _snapshot((campaign_id,), evidence_id="future-1", available_at=T3)
        authority = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={
                MonetarySourceClass.PROVIDER_BILLING: Resolver(
                    {"public-price": estimate, "simulation": simulated, "future": future}
                )
            },
        )

        estimate_ref = authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "public-price", as_of=T1)
        simulated_ref = authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "simulation", as_of=T1)
        with pytest.raises(MonetaryAuthorityError, match="estimate/simulation"):
            authority.issue_cost_evidence(campaign=campaign, source_ref=estimate_ref, as_of=T1)
        with pytest.raises(MonetaryAuthorityError, match="estimate/simulation"):
            authority.issue_cost_evidence(campaign=campaign, source_ref=simulated_ref, as_of=T1)
        with pytest.raises(MonetaryAuthorityError, match="future-available"):
            authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "future", as_of=T1)
    finally:
        fixture.doCleanups()


def test_shared_allocation_conserves_source_and_is_deterministic(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        campaign_id = campaign.projection().campaign_id
        other = "zz-other-campaign"
        campaigns = tuple(sorted((campaign_id, other)))
        source = _snapshot(campaigns, amount="20")
        source_resolver = Resolver({"shared": source})
        bootstrap = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.FIXED_ADMIN: source_resolver},
        )
        source_ref = bootstrap.capture_source(MonetarySourceClass.FIXED_ADMIN, "shared", as_of=T1)
        shares = tuple(sorted(((campaign_id, Decimal("0.25")), (other, Decimal("0.75")))))
        allocation = SharedAllocationSnapshot(
            authority_id="owner-allocation-ledger",
            evidence_id="allocation-1",
            content_sha256="c" * 64,
            source_ref=source_ref,
            shares=shares,
            observed_at=T0,
            available_at=T1,
            provenance="resolver:owner-allocation-record",
        )
        authority = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.FIXED_ADMIN: source_resolver},
            allocation_resolver=AllocationResolver({"allocation": allocation}),
        )
        authority.capture_allocation("allocation", as_of=T1)
        cost = authority.issue_cost_evidence(campaign=campaign, source_ref=source_ref, as_of=T1)
        expected_share = dict(shares)[campaign_id]
        assert cost.amount == Decimal("20") * expected_share
        assert cost.shared_source
        assert cost.allocation_source is not None

        with pytest.raises(MonetaryAuthorityError, match="conserve"):
            SharedAllocationSnapshot(
                authority_id="owner-allocation-ledger",
                evidence_id="bad-allocation",
                content_sha256="d" * 64,
                source_ref=source_ref,
                shares=((campaign_id, Decimal("0.4")), (other, Decimal("0.5"))),
                observed_at=T0,
                available_at=T1,
                provenance="resolver:owner-allocation-record",
            )
    finally:
        fixture.doCleanups()


def test_append_only_correction_and_restart_preserve_exact_source_truth(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        campaign_id = campaign.projection().campaign_id
        first = _snapshot((campaign_id,), amount="10")
        correction = _snapshot(
            (campaign_id,),
            evidence_id="invoice-2",
            amount="7.50",
            available_at=T2,
            supersedes="invoice-1",
        )
        resolver = Resolver({"first": first, "correction": correction})
        authority = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )
        first_ref = authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "first", as_of=T1)
        old_cost = authority.issue_cost_evidence(campaign=campaign, source_ref=first_ref, as_of=T1)
        correction_ref = authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "correction", as_of=T2)

        with pytest.raises(MonetaryAuthorityError, match="visible append-only correction"):
            authority.issue_cost_evidence(campaign=campaign, source_ref=first_ref, as_of=T2)
        corrected = authority.issue_cost_evidence(campaign=campaign, source_ref=correction_ref, as_of=T2)
        assert corrected.amount == Decimal("7.50")
        assert corrected.supersedes_cost_evidence_ids == (old_cost.cost_evidence_id,)

        restarted = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )
        assert restarted.issue_cost_evidence(campaign=campaign, source_ref=correction_ref, as_of=T2) == corrected
    finally:
        fixture.doCleanups()


def test_durable_digest_tamper_fails_on_restart(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        campaign_id = campaign.projection().campaign_id
        resolver = Resolver({"invoice": _snapshot((campaign_id,))})
        authority = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )
        authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "invoice", as_of=T1)
        path = tmp_path / "monetary-cost-authority.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["sources"][0]["snapshot"]["amount"] = "999"
        path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        with pytest.raises(MonetaryAuthorityError, match="digest mismatch"):
            MonetaryCostAuthority(
                tmp_path,
                source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
            )
    finally:
        fixture.doCleanups()


def test_wrong_campaign_negative_nonfinite_and_currency_fail_closed(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        source = _snapshot(("different-campaign",))
        resolver = Resolver({"wrong": source})
        authority = MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )
        source_ref = authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "wrong", as_of=T1)
        with pytest.raises(MonetaryAuthorityError, match="does not apply"):
            authority.issue_cost_evidence(campaign=campaign, source_ref=source_ref, as_of=T1)

        with pytest.raises(MonetaryAuthorityError):
            _snapshot(("x",), amount="-1")
        with pytest.raises(MonetaryAuthorityError):
            _snapshot(("x",), amount="NaN")
        with pytest.raises(MonetaryAuthorityError, match="currency"):
            _snapshot(("x",), currency="eur")
    finally:
        fixture.doCleanups()
