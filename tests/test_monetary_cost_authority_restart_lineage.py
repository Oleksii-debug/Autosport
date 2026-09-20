from __future__ import annotations

from copy import deepcopy
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


T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


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


def _snapshot(*, evidence_id: str, amount: str, digest_char: str, supersedes=None, available_at=T1):
    return MonetarySourceSnapshot(
        authority_id="provider-price-cache",
        evidence_id=evidence_id,
        content_sha256=digest_char * 64,
        amount=Decimal(amount),
        currency="EUR",
        campaign_ids=("campaign-a",),
        coverage_start=T0 - timedelta(hours=1),
        coverage_end=T0,
        observed_at=T0,
        available_at=available_at,
        provenance="resolver:non-authoritative-observation",
        quality=MonetaryEvidenceQuality.ESTIMATE,
        supersedes_evidence_id=supersedes,
    )


def _rehash(record: dict[str, object]) -> None:
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


def _rehash_allocation(record: dict[str, object]) -> None:
    payload = {key: value for key, value in record.items() if key != "sha256"}
    record["sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_restart_rejects_rehashed_forged_branch_before_either_tip_can_resolve(tmp_path) -> None:
    first = _snapshot(evidence_id="price-1", amount="10", digest_char="a")
    correction = _snapshot(
        evidence_id="price-2",
        amount="7.5",
        digest_char="b",
        supersedes="price-1",
        available_at=T2,
    )
    resolver = Resolver({"first": first, "correction": correction})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "first",
        as_of=T1,
    )
    authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "correction",
        as_of=T2,
    )

    path = tmp_path / "monetary-cost-authority.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    correction_record = next(
        record
        for record in raw["sources"]
        if record["snapshot"]["supersedes_evidence_id"] == "price-1"
    )
    forged_tip = deepcopy(correction_record)
    forged_tip["snapshot"]["evidence_id"] = "price-3"
    forged_tip["snapshot"]["content_sha256"] = "c" * 64
    forged_tip["snapshot"]["amount"] = "6.25"
    _rehash(forged_tip)
    raw["sources"].append(forged_tip)
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(MonetaryAuthorityError, match="correction lineage cannot branch"):
        MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
        )


def test_live_and_restart_reject_backdated_correction_availability(tmp_path) -> None:
    first = _snapshot(
        evidence_id="price-1",
        amount="10",
        digest_char="a",
        available_at=T2,
    )
    backdated = _snapshot(
        evidence_id="price-2",
        amount="7.5",
        digest_char="b",
        supersedes="price-1",
        available_at=T1,
    )
    resolver = Resolver({"first": first, "backdated": backdated})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "first",
        as_of=T2,
    )
    with pytest.raises(
        MonetaryAuthorityError,
        match="correction availability cannot precede predecessor",
    ):
        authority.capture_source(
            MonetarySourceClass.PROVIDER_BILLING,
            "backdated",
            as_of=T2,
        )

    # Prove a locally rehashed durable correction cannot forge the same chronology.
    root = tmp_path / "restart"
    canonical_first = _snapshot(
        evidence_id="price-1",
        amount="10",
        digest_char="a",
        available_at=T1,
    )
    canonical_correction = _snapshot(
        evidence_id="price-2",
        amount="7.5",
        digest_char="b",
        supersedes="price-1",
        available_at=T2,
    )
    canonical_resolver = Resolver(
        {"first": canonical_first, "correction": canonical_correction}
    )
    canonical = MonetaryCostAuthority(
        root,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: canonical_resolver},
    )
    canonical.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "first",
        as_of=T1,
    )
    canonical.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "correction",
        as_of=T2,
    )
    path = root / "monetary-cost-authority.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    correction_record = next(
        record
        for record in raw["sources"]
        if record["snapshot"]["supersedes_evidence_id"] == "price-1"
    )
    correction_record["snapshot"]["available_at"] = T0.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    _rehash(correction_record)
    path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(
        MonetaryAuthorityError,
        match="correction availability cannot precede predecessor",
    ):
        MonetaryCostAuthority(
            root,
            source_resolvers={
                MonetarySourceClass.PROVIDER_BILLING: canonical_resolver
            },
        )


def test_restart_rejects_rehashed_allocation_with_forged_campaign_coverage(tmp_path) -> None:
    source = _snapshot(evidence_id="price-1", amount="10", digest_char="a")
    resolver = Resolver({"source": source})
    bootstrap = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
    )
    source_ref = bootstrap.capture_source(
        MonetarySourceClass.FIXED_ADMIN,
        "source",
        as_of=T1,
    )
    allocation = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-1",
        content_sha256="d" * 64,
        source_ref=source_ref,
        shares=(("campaign-a", Decimal("1")),),
        observed_at=T0,
        available_at=T1,
        provenance="resolver:non-authoritative-allocation",
    )
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=AllocationResolver({"allocation": allocation}),
    )
    authority.capture_allocation("allocation", as_of=T1)

    path = tmp_path / "monetary-cost-authority.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    allocation_record = raw["allocations"][0]
    allocation_record["shares"] = [["campaign-b", "1"]]
    _rehash_allocation(allocation_record)
    path.write_text(
        json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        MonetaryAuthorityError,
        match="allocation must cover source campaigns exactly",
    ):
        MonetaryCostAuthority(
            tmp_path,
            source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
            allocation_resolver=AllocationResolver({"allocation": allocation}),
        )


def test_live_and_restart_reject_allocation_before_exact_source_availability(tmp_path) -> None:
    source = _snapshot(
        evidence_id="price-1",
        amount="10",
        digest_char="a",
        available_at=T2,
    )
    resolver = Resolver({"source": source})
    bootstrap = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
    )
    source_ref = bootstrap.capture_source(
        MonetarySourceClass.FIXED_ADMIN,
        "source",
        as_of=T2,
    )
    backdated = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-1",
        content_sha256="d" * 64,
        source_ref=source_ref,
        shares=(("campaign-a", Decimal("1")),),
        observed_at=T0,
        available_at=T1,
        provenance="resolver:non-authoritative-allocation",
    )
    live = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=AllocationResolver({"allocation": backdated}),
    )
    with pytest.raises(
        MonetaryAuthorityError,
        match="allocation availability cannot precede source",
    ):
        live.capture_allocation("allocation", as_of=T2)

    root = tmp_path / "restart"
    canonical_source = _snapshot(
        evidence_id="price-1",
        amount="10",
        digest_char="a",
        available_at=T1,
    )
    canonical_resolver = Resolver({"source": canonical_source})
    canonical_bootstrap = MonetaryCostAuthority(
        root,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: canonical_resolver},
    )
    canonical_ref = canonical_bootstrap.capture_source(
        MonetarySourceClass.FIXED_ADMIN,
        "source",
        as_of=T1,
    )
    canonical_allocation = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-1",
        content_sha256="d" * 64,
        source_ref=canonical_ref,
        shares=(("campaign-a", Decimal("1")),),
        observed_at=T0,
        available_at=T2,
        provenance="resolver:non-authoritative-allocation",
    )
    canonical = MonetaryCostAuthority(
        root,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: canonical_resolver},
        allocation_resolver=AllocationResolver({"allocation": canonical_allocation}),
    )
    canonical.capture_allocation("allocation", as_of=T2)
    path = root / "monetary-cost-authority.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    allocation_record = raw["allocations"][0]
    allocation_record["available_at"] = T0.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
    _rehash_allocation(allocation_record)
    path.write_text(
        json.dumps(raw, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(
        MonetaryAuthorityError,
        match="allocation availability cannot precede source",
    ):
        MonetaryCostAuthority(
            root,
            source_resolvers={MonetarySourceClass.FIXED_ADMIN: canonical_resolver},
            allocation_resolver=AllocationResolver(
                {"allocation": canonical_allocation}
            ),
        )


def test_stale_reopened_writer_cannot_ack_competing_allocation(tmp_path) -> None:
    source = _snapshot(evidence_id="price-1", amount="10", digest_char="a")
    resolver = Resolver({"source": source})
    bootstrap = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
    )
    source_ref = bootstrap.capture_source(
        MonetarySourceClass.FIXED_ADMIN,
        "source",
        as_of=T1,
    )
    first_allocation = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-1",
        content_sha256="d" * 64,
        source_ref=source_ref,
        shares=(("campaign-a", Decimal("1")),),
        observed_at=T0,
        available_at=T1,
        provenance="resolver:non-authoritative-allocation",
    )
    competing_allocation = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-2",
        content_sha256="e" * 64,
        source_ref=source_ref,
        shares=(("campaign-a", Decimal("1")),),
        observed_at=T0,
        available_at=T1,
        provenance="resolver:non-authoritative-allocation",
    )

    first = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=AllocationResolver({"allocation": first_allocation}),
    )
    stale = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=AllocationResolver({"allocation": competing_allocation}),
    )
    first.capture_allocation("allocation", as_of=T1)
    with pytest.raises(
        MonetaryAuthorityError,
        match="cache changed since load; reopen before mutation",
    ):
        stale.capture_allocation("allocation", as_of=T1)

    restarted = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=AllocationResolver({"allocation": first_allocation}),
    )
    raw = json.loads(
        (tmp_path / "monetary-cost-authority.json").read_text(encoding="utf-8")
    )
    assert len(raw["allocations"]) == 1
    assert restarted.resolve_source(source_ref, as_of=T1).snapshot == source


def test_failed_source_persist_rolls_back_before_retry_and_restart(
    tmp_path,
    monkeypatch,
) -> None:
    source = _snapshot(evidence_id="price-1", amount="10", digest_char="a")
    resolver = Resolver({"source": source})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    real_persist = authority._persist

    def fail_persist() -> None:
        raise OSError("injected persistence failure")

    monkeypatch.setattr(authority, "_persist", fail_persist)
    with pytest.raises(OSError, match="injected persistence failure"):
        authority.capture_source(
            MonetarySourceClass.PROVIDER_BILLING,
            "source",
            as_of=T1,
        )

    monkeypatch.setattr(authority, "_persist", real_persist)
    source_ref = authority.capture_source(
        MonetarySourceClass.PROVIDER_BILLING,
        "source",
        as_of=T1,
    )
    restarted = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.PROVIDER_BILLING: resolver},
    )
    assert restarted.resolve_source(source_ref, as_of=T1).snapshot == source


def test_failed_allocation_persist_rolls_back_before_retry_and_restart(
    tmp_path,
    monkeypatch,
) -> None:
    source = _snapshot(evidence_id="price-1", amount="10", digest_char="a")
    resolver = Resolver({"source": source})
    bootstrap = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
    )
    source_ref = bootstrap.capture_source(
        MonetarySourceClass.FIXED_ADMIN,
        "source",
        as_of=T1,
    )
    allocation = SharedAllocationSnapshot(
        authority_id="allocation-ledger-cache",
        evidence_id="allocation-1",
        content_sha256="d" * 64,
        source_ref=source_ref,
        shares=(("campaign-a", Decimal("1")),),
        observed_at=T0,
        available_at=T1,
        provenance="resolver:non-authoritative-allocation",
    )
    allocation_resolver = AllocationResolver({"allocation": allocation})
    authority = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=allocation_resolver,
    )
    real_persist = authority._persist

    def fail_persist() -> None:
        raise OSError("injected persistence failure")

    monkeypatch.setattr(authority, "_persist", fail_persist)
    with pytest.raises(OSError, match="injected persistence failure"):
        authority.capture_allocation("allocation", as_of=T1)

    monkeypatch.setattr(authority, "_persist", real_persist)
    authority.capture_allocation("allocation", as_of=T1)
    restarted = MonetaryCostAuthority(
        tmp_path,
        source_resolvers={MonetarySourceClass.FIXED_ADMIN: resolver},
        allocation_resolver=allocation_resolver,
    )
    raw = json.loads(
        (tmp_path / "monetary-cost-authority.json").read_text(encoding="utf-8")
    )
    assert len(raw["allocations"]) == 1
    assert restarted.resolve_source(source_ref, as_of=T1).snapshot == source
