from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

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


def _snapshot(
    campaign_ids=("campaign-a",),
    *,
    evidence_id="price-1",
    amount="12.5",
    quality=MonetaryEvidenceQuality.ESTIMATE,
    available_at=T1,
    supersedes=None,
    currency="EUR",
    authority_id="provider-price-cache",
):
    digit = "a" if evidence_id == "price-1" else "b"
    return MonetarySourceSnapshot(
        authority_id=authority_id,
        evidence_id=evidence_id,
        content_sha256=digit * 64,
        amount=Decimal(amount),
        currency=currency,
        campaign_ids=tuple(sorted(campaign_ids)),
        coverage_start=T0 - timedelta(hours=1),
        coverage_end=T0,
        observed_at=T0,
        available_at=available_at,
        provenance="resolver:non-authoritative-observation",
        quality=quality,
        supersedes_evidence_id=supersedes,
    )


def test_injected_resolver_cannot_mint_incurred_monetary_truth(tmp_path) -> None:
    incurred = _snapshot(quality=MonetaryEvidenceQuality.INCURRED)
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={
            MonetarySourceClass.PROVIDER_BILLING: Resolver({"invoice": incurred})
        },
    )

    with pytest.raises(MonetaryAuthorityError, match="injected resolver cannot mint"):
        authority.capture_source(
            MonetarySourceClass.PROVIDER_BILLING,
            "invoice",
            as_of=T1,
        )


def test_estimate_and_simulation_are_restart_safe_but_never_incurred_authority(tmp_path) -> None:
    estimate = _snapshot()
    simulated = _snapshot(
        evidence_id="simulation-1",
        quality=MonetaryEvidenceQuality.SIMULATED,
    )
    resolver = Resolver({"price": estimate, "simulation": simulated})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    estimate_ref = authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "price",
        as_of=T1,
    )
    simulation_ref = authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "simulation",
        as_of=T1,
    )

    restarted = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    assert restarted.resolve_source(estimate_ref, as_of=T1).snapshot == estimate
    assert restarted.resolve_source(simulation_ref, as_of=T1).snapshot == simulated

    fixture, campaign = _fixture_authority()
    try:
        with pytest.raises(MonetaryAuthorityError, match="no product-owned incurred"):
            restarted.issue_cost_evidence(
                campaign=campaign,
                source_ref=estimate_ref,
                as_of=T1,
            )
    finally:
        fixture.doCleanups()


def test_future_observation_cannot_be_captured_as_of_past(tmp_path) -> None:
    future = _snapshot(available_at=T3)
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={
            MonetarySourceClass.PROVIDER_BILLING: Resolver({"future": future})
        },
    )
    with pytest.raises(MonetaryAuthorityError, match="future-available"):
        authority.capture_source(
            MonetarySourceClass.PROVIDER_BILLING,
            "future",
            as_of=T1,
        )


def test_shared_estimate_allocation_conserves_exactly_one_and_restarts(tmp_path) -> None:
    campaigns = ("campaign-a", "campaign-b")
    source = _snapshot(campaigns, amount="20")
    source_resolver = Resolver({"shared": source})
    bootstrap = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: source_resolver},
    )
    source_ref = bootstrap.capture_source(
        MonetarySourceClass.FIXED_ADMIN,
        "shared",
        as_of=T1,
    )
    allocation = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-1",
        content_sha256="c" * 64,
        source_ref=source_ref,
        shares=(("campaign-a", Decimal("0.25")), ("campaign-b", Decimal("0.75"))),
        observed_at=T0,
        available_at=T1,
        provenance="resolver:non-authoritative-allocation",
    )
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: source_resolver},
        allocation_resolver=AllocationResolver({"allocation": allocation}),
    )
    allocation_ref = authority.capture_allocation("allocation", as_of=T1)
    assert allocation_ref.sha256 == allocation.sha256

    restarted = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: source_resolver},
        allocation_resolver=AllocationResolver({"allocation": allocation}),
    )
    assert restarted.resolve_source(source_ref, as_of=T1).snapshot == source

    with pytest.raises(MonetaryAuthorityError, match="conserve"):
        SharedAllocationSnapshot(
            authority_id="allocation-ledger-cache",
            evidence_id="bad-allocation",
            content_sha256="d" * 64,
            source_ref=source_ref,
            shares=(("campaign-a", Decimal("0.4")), ("campaign-b", Decimal("0.5"))),
            observed_at=T0,
            available_at=T1,
            provenance="resolver:non-authoritative-allocation",
        )


def test_estimate_correction_is_append_only_and_stales_predecessor(tmp_path) -> None:
    first = _snapshot(amount="10")
    correction = _snapshot(
        evidence_id="price-2",
        amount="7.5",
        available_at=T2,
        supersedes="price-1",
    )
    resolver = Resolver({"first": first, "correction": correction})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    first_ref = authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "first",
        as_of=T1,
    )
    correction_ref = authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "correction",
        as_of=T2,
    )

    with pytest.raises(MonetaryAuthorityError, match="visible append-only correction"):
        authority.resolve_source(first_ref, as_of=T2)
    assert authority.resolve_source(correction_ref, as_of=T2).snapshot == correction

    restarted = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    with pytest.raises(MonetaryAuthorityError, match="visible append-only correction"):
        restarted.resolve_source(first_ref, as_of=T2)
    assert restarted.resolve_source(correction_ref, as_of=T2).snapshot == correction


def test_recomputed_local_hash_cannot_upgrade_cache_to_incurred_truth(tmp_path) -> None:
    estimate = _snapshot()
    resolver = Resolver({"price": estimate})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "price",
        as_of=T1,
    )

    path = tmp_path / "monetary-cost-authority.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    record = raw["sources"][0]
    record["snapshot"]["quality"] = "INCURRED"
    record["snapshot"]["amount"] = "999"
    payload = {
        "schema_version": record["schema_version"],
        "source_class": record["source_class"],
        "snapshot": record["snapshot"],
    }
    record["sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(MonetaryAuthorityError, match="cannot contain incurred"):
        MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )


def test_unknown_durable_fields_and_positive_authority_marker_fail_closed(tmp_path) -> None:
    estimate = _snapshot()
    resolver = Resolver({"price": estimate})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "price", as_of=T1)
    path = tmp_path / "monetary-cost-authority.json"

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["sources"][0]["caller_asserted_paid"] = True
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(MonetaryAuthorityError, match="keys mismatch"):
        MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )

    # Restore a canonical cache, then falsify only the authority marker.
    path.unlink()
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    authority.capture_source(MonetarySourceClass.PROVIDER_BILLING, "price", as_of=T1)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["authoritative_incurred"] = True
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with pytest.raises(MonetaryAuthorityError, match="cannot claim incurred authority"):
        MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )


def test_malformed_amount_currency_and_authority_identity_fail_closed() -> None:
    with pytest.raises(MonetaryAuthorityError):
        _snapshot(amount="-1")
    with pytest.raises(MonetaryAuthorityError):
        _snapshot(amount="NaN")
    with pytest.raises(MonetaryAuthorityError, match="currency"):
        _snapshot(currency="eur")
    with pytest.raises(MonetaryAuthorityError, match="cannot contain"):
        _snapshot(authority_id="caller:forged")
