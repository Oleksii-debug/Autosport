from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairSessionCredentials,
)
from autosport.betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionReceipt,
)
from autosport.campaign_commission_allocation import (
    BetfairCommissionCampaignAllocationAuthority,
    CampaignCommissionAllocationError,
)
from autosport.campaign_economic_authority import (
    CanonicalCampaignProjection,
    CanonicalMembershipRef,
    CanonicalSessionRef,
    FinalizedCampaignAuthority,
)


UTC = timezone.utc


def _receipt(*, commission: str = "0.30", digit: str = "b", supersedes=None):
    return BetfairMarketCommissionReceipt(
        venue_id="betfair",
        account_id=f"betfair-account-evidence:{'a' * 64}",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        market_id="1.143732676",
        commission=Decimal(commission),
        profit=Decimal("4.20"),
        currency="EUR",
        settled_at=datetime(2026, 9, 20, 8, tzinfo=UTC),
        observed_at=datetime(2026, 9, 20, 10, tzinfo=UTC),
        available_at=datetime(2026, 9, 20, 10, tzinfo=UTC),
        account_details_sha256="a" * 64,
        cleared_orders_sha256=digit * 64,
        request_scope_sha256="e" * 64,
        supersedes_receipt_id=supersedes,
    )


def _projection(index: int) -> CanonicalCampaignProjection:
    digit = format(index, "x")[-1]
    session = CanonicalSessionRef(
        evidence_id=f"session-evidence-{index}",
        evidence_sha256=digit * 64,
    )
    memberships = tuple(
        sorted(
            (
                CanonicalMembershipRef("SESSION", session.evidence_id, session.evidence_sha256),
                CanonicalMembershipRef(f"RUN-{index}", f"run-{index}", (digit * 63) + "1"),
                CanonicalMembershipRef(
                    f"EVALUATION-{index}", f"eval-{index}", (digit * 63) + "2"
                ),
            )
        )
    )
    return CanonicalCampaignProjection(
        campaign_id=f"campaign-{index}",
        campaign_version=1,
        campaign_sha256=(digit * 63) + "3",
        session_refs=(session,),
        membership_refs=memberships,
        gross_run_pnl=Decimal("10"),
    )


def _campaigns(monkeypatch, count: int):
    values = [object.__new__(FinalizedCampaignAuthority) for _ in range(count)]
    projections = {id(value): _projection(index + 1) for index, value in enumerate(values)}
    monkeypatch.setattr(
        FinalizedCampaignAuthority,
        "projection",
        lambda self: projections[id(self)],
    )
    return tuple(values), projections


def _source(tmp_path) -> BetfairMarketCommissionAuthority:
    return BetfairMarketCommissionAuthority(
        tmp_path / "source-workspace",
        BetfairSessionCredentials("app-key", "session-token"),
        authority_root=tmp_path / "source-authority",
    )


def _authority(tmp_path, source):
    return BetfairCommissionCampaignAllocationAuthority(
        tmp_path / "allocation-workspace",
        source,
        authority_root=tmp_path / "allocation-authority",
    )


def _install_receipts(monkeypatch, receipts):
    by_id = {value.receipt_id: value for value in receipts}

    def resolve(self, *, receipt_id, record_sha256, as_of):
        value = by_id[receipt_id]
        assert record_sha256 == value.record_sha256
        assert as_of >= value.available_at
        return value

    monkeypatch.setattr(BetfairMarketCommissionAuthority, "resolve", resolve)


def test_shared_allocation_conserves_exact_provider_amount_and_membership(
    monkeypatch, tmp_path
) -> None:
    receipt = _receipt()
    _install_receipts(monkeypatch, (receipt,))
    source = _source(tmp_path)
    authority = _authority(tmp_path, source)
    campaigns, projections = _campaigns(monkeypatch, 2)

    allocations = authority.allocate_receipt(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=receipt.available_at + timedelta(seconds=1),
        targets=((campaigns[1], Decimal("0.20")), (campaigns[0], Decimal("0.10"))),
    )

    assert tuple(value.campaign_sha256 for value in allocations) == tuple(
        sorted(projection.campaign_sha256 for projection in projections.values())
    )
    assert sum((value.allocated_amount for value in allocations), Decimal("0")) == Decimal("0.30")
    assert all(value.source_amount == Decimal("0.30") for value in allocations)
    assert all(value.currency == "EUR" for value in allocations)
    for allocation in allocations:
        expected = next(
            value
            for value in projections.values()
            if value.campaign_sha256 == allocation.campaign_sha256
        )
        assert allocation.memberships == expected.membership_refs


def test_non_conserving_or_duplicate_campaign_split_fails_closed(monkeypatch, tmp_path) -> None:
    receipt = _receipt()
    _install_receipts(monkeypatch, (receipt,))
    source = _source(tmp_path)
    authority = _authority(tmp_path, source)
    campaigns, _ = _campaigns(monkeypatch, 2)
    as_of = receipt.available_at + timedelta(seconds=1)

    with pytest.raises(CampaignCommissionAllocationError, match="conserve"):
        authority.allocate_receipt(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=as_of,
            targets=((campaigns[0], Decimal("0.29")),),
        )
    with pytest.raises(CampaignCommissionAllocationError, match="same campaign twice"):
        authority.allocate_receipt(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=as_of,
            targets=(
                (campaigns[0], Decimal("0.10")),
                (campaigns[0], Decimal("0.20")),
            ),
        )


def test_same_receipt_cannot_be_reallocated_to_different_campaign_split(
    monkeypatch, tmp_path
) -> None:
    receipt = _receipt()
    _install_receipts(monkeypatch, (receipt,))
    source = _source(tmp_path)
    authority = _authority(tmp_path, source)
    campaigns, _ = _campaigns(monkeypatch, 2)
    as_of = receipt.available_at + timedelta(seconds=1)

    first = authority.allocate_receipt(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=as_of,
        targets=((campaigns[0], Decimal("0.10")), (campaigns[1], Decimal("0.20"))),
    )
    assert authority.allocate_receipt(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=as_of,
        targets=((campaigns[1], Decimal("0.20")), (campaigns[0], Decimal("0.10"))),
    ) == first

    with pytest.raises(CampaignCommissionAllocationError, match="already allocated differently"):
        authority.allocate_receipt(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=as_of,
            targets=((campaigns[0], Decimal("0.15")), (campaigns[1], Decimal("0.15"))),
        )


def test_restart_durable_bytes_do_not_mint_positive_allocation_authority(
    monkeypatch, tmp_path
) -> None:
    receipt = _receipt()
    _install_receipts(monkeypatch, (receipt,))
    source = _source(tmp_path)
    campaigns, _ = _campaigns(monkeypatch, 1)
    authority = _authority(tmp_path, source)
    as_of = receipt.available_at + timedelta(seconds=1)
    allocation = authority.allocate_receipt(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=as_of,
        targets=((campaigns[0], Decimal("0.30")),),
    )[0]
    assert authority.resolve(
        allocation_id=allocation.allocation_id,
        record_sha256=allocation.record_sha256,
        as_of=as_of,
    ) == allocation

    restarted = _authority(tmp_path, source)
    assert restarted.verify()[0].allocations == (allocation,)
    with pytest.raises(CampaignCommissionAllocationError, match="reacquisition"):
        restarted.resolve(
            allocation_id=allocation.allocation_id,
            record_sha256=allocation.record_sha256,
            as_of=as_of,
        )

    assert restarted.allocate_receipt(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=as_of,
        targets=((campaigns[0], Decimal("0.30")),),
    ) == (allocation,)
    assert restarted.resolve(
        allocation_id=allocation.allocation_id,
        record_sha256=allocation.record_sha256,
        as_of=as_of,
    ) == allocation


def test_source_correction_is_append_only_and_cannot_rewrite_campaign_applicability(
    monkeypatch, tmp_path
) -> None:
    first_receipt = _receipt(commission="0.30", digit="b")
    correction = _receipt(
        commission="0.24",
        digit="c",
        supersedes=first_receipt.receipt_id,
    )
    _install_receipts(monkeypatch, (first_receipt, correction))
    source = _source(tmp_path)
    authority = _authority(tmp_path, source)
    campaigns, _ = _campaigns(monkeypatch, 2)
    as_of = correction.available_at + timedelta(seconds=1)

    first = authority.allocate_receipt(
        receipt_id=first_receipt.receipt_id,
        record_sha256=first_receipt.record_sha256,
        as_of=as_of,
        targets=((campaigns[0], Decimal("0.10")), (campaigns[1], Decimal("0.20"))),
    )
    corrected = authority.allocate_receipt(
        receipt_id=correction.receipt_id,
        record_sha256=correction.record_sha256,
        as_of=as_of,
        targets=((campaigns[0], Decimal("0.08")), (campaigns[1], Decimal("0.16"))),
    )
    by_campaign = {value.campaign_sha256: value for value in first}
    assert all(
        value.supersedes_allocation_id == by_campaign[value.campaign_sha256].allocation_id
        for value in corrected
    )
    assert len(authority.verify()) == 2

    with pytest.raises(CampaignCommissionAllocationError, match="cannot rewrite campaign applicability"):
        authority.allocate_receipt(
            receipt_id=correction.receipt_id,
            record_sha256=correction.record_sha256,
            as_of=as_of,
            targets=((campaigns[0], Decimal("0.24")),),
        )


def test_state_tamper_is_detected_before_recovery(monkeypatch, tmp_path) -> None:
    receipt = _receipt()
    _install_receipts(monkeypatch, (receipt,))
    source = _source(tmp_path)
    authority = _authority(tmp_path, source)
    campaigns, _ = _campaigns(monkeypatch, 1)
    authority.allocate_receipt(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=receipt.available_at + timedelta(seconds=1),
        targets=((campaigns[0], Decimal("0.30")),),
    )

    path = tmp_path / "allocation-workspace" / "campaign_commission_allocation" / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["state_sha256"] = "f" * 64
    path.write_text(json.dumps(raw, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    restarted = _authority(tmp_path, source)
    with pytest.raises(CampaignCommissionAllocationError, match="state digest mismatch"):
        restarted.verify()
