from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.monetary_cost_authority import (
    CampaignMonetaryAllocation,
    IncurredMonetaryReceipt,
    MonetaryCostAuthorityError,
    MonetaryCostAuthorityStore,
    MonetaryEvidenceQuality,
    MonetarySourceClass,
    UsageEvidenceRef,
    monetary_allocation_source_family,
)


UTC = timezone.utc


def _t(hour: int) -> datetime:
    return datetime(2026, 9, 20, hour, 0, tzinfo=UTC)


def _sha(char: str) -> str:
    return char * 64


def _receipt(*, amount: str = "20", supersedes: str | None = None, available_at: datetime | None = None) -> IncurredMonetaryReceipt:
    return IncurredMonetaryReceipt(
        source_class=MonetarySourceClass.PROVIDER_DATA,
        source_family="betfair.cleared-orders.market",
        source_authority_id="betfair:venue/account-7",
        source_evidence_id="market-statement-2026-09-20",
        source_sha256=_sha("a") if supersedes is None else _sha("b"),
        amount=Decimal(amount),
        currency="EUR",
        incurred_from=_t(7),
        incurred_to=_t(8),
        observed_at=_t(8),
        available_at=available_at or _t(8),
        quality=MonetaryEvidenceQuality.INCURRED_RECEIPT,
        supersedes_receipt_id=supersedes,
    )


def _store(tmp_path) -> MonetaryCostAuthorityStore:
    return MonetaryCostAuthorityStore(
        tmp_path / "workspace",
        authority_root=tmp_path / "authority",
    )


def test_receipt_allocation_resolves_exact_campaign_money(tmp_path) -> None:
    store = _store(tmp_path)
    receipt = _receipt()
    store.append_receipt(receipt)
    allocation = CampaignMonetaryAllocation(
        receipt_id=receipt.receipt_id,
        receipt_sha256=receipt.record_sha256,
        campaign_sha256=_sha("c"),
        amount=Decimal("7.5"),
        currency="EUR",
        allocated_at=_t(9),
        usage_refs=(UsageEvidenceRef("provider.request", "request-1", _sha("d")),),
    )
    store.append_allocation(allocation)

    resolved = store.resolve_cost_source(
        family=monetary_allocation_source_family(),
        evidence_id=allocation.allocation_id,
        sha256=allocation.record_sha256,
        campaign_sha256=_sha("c"),
        as_of=_t(10),
    )
    assert resolved.amount == Decimal("7.5")
    assert resolved.currency == "EUR"
    assert resolved.source_class is MonetarySourceClass.PROVIDER_DATA
    assert resolved.source_authority_id == "betfair:venue/account-7"


def test_allocations_cannot_exceed_source_amount(tmp_path) -> None:
    store = _store(tmp_path)
    receipt = _receipt(amount="10")
    store.append_receipt(receipt)
    first = CampaignMonetaryAllocation(
        receipt.receipt_id,
        receipt.record_sha256,
        _sha("c"),
        Decimal("7"),
        "EUR",
        _t(9),
    )
    store.append_allocation(first)
    with pytest.raises(MonetaryCostAuthorityError, match="exceed"):
        store.append_allocation(
            CampaignMonetaryAllocation(
                receipt.receipt_id,
                receipt.record_sha256,
                _sha("e"),
                Decimal("4"),
                "EUR",
                _t(9),
            )
        )


def test_usage_receipt_cannot_be_allocated_twice(tmp_path) -> None:
    store = _store(tmp_path)
    receipt = _receipt()
    store.append_receipt(receipt)
    usage = UsageEvidenceRef("parlay.response", "req-9", _sha("f"))
    store.append_allocation(
        CampaignMonetaryAllocation(
            receipt.receipt_id,
            receipt.record_sha256,
            _sha("c"),
            Decimal("5"),
            "EUR",
            _t(9),
            (usage,),
        )
    )
    with pytest.raises(MonetaryCostAuthorityError, match="allocated more than once"):
        store.append_allocation(
            CampaignMonetaryAllocation(
                receipt.receipt_id,
                receipt.record_sha256,
                _sha("e"),
                Decimal("5"),
                "EUR",
                _t(9),
                (usage,),
            )
        )


def test_currency_conversion_is_never_implicit(tmp_path) -> None:
    store = _store(tmp_path)
    receipt = _receipt()
    store.append_receipt(receipt)
    with pytest.raises(MonetaryCostAuthorityError, match="currency"):
        store.append_allocation(
            CampaignMonetaryAllocation(
                receipt.receipt_id,
                receipt.record_sha256,
                _sha("c"),
                Decimal("5"),
                "USD",
                _t(9),
            )
        )


def test_available_correction_invalidates_old_source_for_later_as_of(tmp_path) -> None:
    store = _store(tmp_path)
    original = _receipt(amount="20")
    store.append_receipt(original)
    allocation = CampaignMonetaryAllocation(
        original.receipt_id,
        original.record_sha256,
        _sha("c"),
        Decimal("5"),
        "EUR",
        _t(9),
    )
    store.append_allocation(allocation)
    correction = _receipt(
        amount="18",
        supersedes=original.receipt_id,
        available_at=_t(11),
    )
    store.append_receipt(correction)

    store.resolve_cost_source(
        family=monetary_allocation_source_family(),
        evidence_id=allocation.allocation_id,
        sha256=allocation.record_sha256,
        campaign_sha256=_sha("c"),
        as_of=_t(10),
    )
    with pytest.raises(MonetaryCostAuthorityError, match="superseded"):
        store.resolve_cost_source(
            family=monetary_allocation_source_family(),
            evidence_id=allocation.allocation_id,
            sha256=allocation.record_sha256,
            campaign_sha256=_sha("c"),
            as_of=_t(12),
        )


def test_tampered_state_fails_closed_after_restart(tmp_path) -> None:
    store = _store(tmp_path)
    receipt = _receipt()
    store.append_receipt(receipt)
    path = tmp_path / "workspace" / "monetary_cost_authority" / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["receipts"][0]["amount"] = "999"
    path.write_text(json.dumps(raw), encoding="utf-8")

    restarted = _store(tmp_path)
    with pytest.raises(MonetaryCostAuthorityError):
        restarted.verify()


def test_future_allocation_cannot_be_backdated(tmp_path) -> None:
    store = _store(tmp_path)
    receipt = _receipt()
    store.append_receipt(receipt)
    allocation = CampaignMonetaryAllocation(
        receipt.receipt_id,
        receipt.record_sha256,
        _sha("c"),
        Decimal("5"),
        "EUR",
        _t(12),
    )
    store.append_allocation(allocation)
    with pytest.raises(MonetaryCostAuthorityError, match="backdated"):
        store.resolve_cost_source(
            family=monetary_allocation_source_family(),
            evidence_id=allocation.allocation_id,
            sha256=allocation.record_sha256,
            campaign_sha256=_sha("c"),
            as_of=_t(11),
        )
