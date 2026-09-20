from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport import betfair_account_readonly as betfair
from autosport.betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairAccountDetailsObservation,
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionAuthorityError,
    BetfairMarketCommissionReceipt,
    _binding,
    _state_digest,
)
from autosport.monotonic_workspace_authority import MonotonicWorkspaceAuthority


UTC = timezone.utc


def _evidence(hour: int, digit: str) -> BetfairEvidence:
    return BetfairEvidence(
        datetime(2026, 9, 20, hour, 0, tzinfo=UTC).isoformat(),
        digit * 64,
    )


def _install_provider(
    monkeypatch,
    *,
    commission: str = "0.13",
    profit: str = "2.52",
    digit: str = "b",
    more: bool = False,
) -> None:
    def details(self):
        return BetfairAccountDetailsObservation(
            "EUR",
            "en",
            "GBR",
            "Europe/London",
            _evidence(9, "a"),
        )

    def rpc(self, method, params):
        assert method == betfair._LIST_CLEARED_ORDERS
        assert params["betStatus"] == "SETTLED"
        assert params["groupBy"] == "MARKET"
        assert params["marketIds"] == ["1.143732676"]
        return betfair._RpcResult(
            {
                "clearedOrders": [
                    {
                        "marketId": "1.143732676",
                        "settledDate": "2026-09-20T08:22:07.000Z",
                        "commission": Decimal(commission),
                        "profit": Decimal(profit),
                    }
                ],
                "moreAvailable": more,
            },
            _evidence(10, digit),
        )

    monkeypatch.setattr(
        BetfairReadOnlyClient, "read_account_details", details
    )
    monkeypatch.setattr(BetfairReadOnlyClient, "_rpc", rpc)


def _authority(tmp_path) -> BetfairMarketCommissionAuthority:
    return BetfairMarketCommissionAuthority(
        tmp_path / "workspace",
        BetfairSessionCredentials("app-key", "session-token"),
        authority_root=tmp_path / "authority",
    )


def _forged_receipt() -> BetfairMarketCommissionReceipt:
    return BetfairMarketCommissionReceipt(
        venue_id="betfair",
        account_id=f"betfair-account-evidence:{'a' * 64}",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        market_id="1.143732676",
        commission=Decimal("999"),
        profit=Decimal("999"),
        currency="EUR",
        settled_at=datetime(2026, 9, 20, 8, tzinfo=UTC),
        observed_at=datetime(2026, 9, 20, 10, tzinfo=UTC),
        available_at=datetime(2026, 9, 20, 10, tzinfo=UTC),
        account_details_sha256="a" * 64,
        cleared_orders_sha256="f" * 64,
        request_scope_sha256="e" * 64,
    )


def test_capture_market_uses_provider_currency_and_market_commission(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch)
    authority = _authority(tmp_path)
    receipt = authority.capture_market("1.143732676")
    assert receipt.venue_id == "betfair"
    assert receipt.account_id == f"betfair-account-evidence:{'a' * 64}"
    assert receipt.adapter_id == ADAPTER_ID
    assert receipt.adapter_version == ADAPTER_VERSION
    assert receipt.commission == Decimal("0.13")
    assert receipt.profit == Decimal("2.52")
    assert receipt.currency == "EUR"
    assert receipt.account_details_sha256 == "a" * 64
    assert receipt.cleared_orders_sha256 == "b" * 64

    resolved = authority.resolve(
        receipt_id=receipt.receipt_id,
        record_sha256=receipt.record_sha256,
        as_of=receipt.available_at + timedelta(seconds=1),
    )
    assert resolved == receipt


def test_restart_requires_authenticated_reacquisition(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch)
    authority = _authority(tmp_path)
    receipt = authority.capture_market("1.143732676")

    restarted = _authority(tmp_path)
    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="requires authenticated acquisition",
    ):
        restarted.resolve(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=receipt.available_at + timedelta(seconds=1),
        )

    reacquired = restarted.capture_market("1.143732676")
    assert reacquired == receipt
    assert (
        restarted.resolve(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=receipt.available_at + timedelta(seconds=1),
        )
        == receipt
    )


def test_durable_state_and_generic_journal_never_mint_origin_after_restart(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch)
    authority = _authority(tmp_path)
    receipt = authority.capture_market("1.143732676")

    restarted = _authority(tmp_path)
    assert restarted.verify() == (receipt,)
    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="requires authenticated acquisition",
    ):
        restarted.resolve(
            receipt_id=receipt.receipt_id,
            record_sha256=receipt.record_sha256,
            as_of=receipt.available_at + timedelta(seconds=1),
        )


def test_caller_forged_state_plus_generic_journal_cannot_mint_origin(
    tmp_path,
) -> None:
    fake = _forged_receipt()
    records = (fake,)
    intended = _state_digest(records)
    binding = _binding(intended)
    generic = MonotonicWorkspaceAuthority(
        workspace=tmp_path / "workspace",
        domain="autosport.betfair_market_commission.v1",
        key="betfair:authenticated-account",
        authority_root=tmp_path / "authority",
    )
    generic.prepare(
        tx_id=intended,
        observed_state_sha256=None,
        intended_state_sha256=intended,
        semantic_binding_sha256=binding,
    )
    path = (
        tmp_path
        / "workspace"
        / "betfair_market_commission"
        / "state.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [fake.to_dict()],
                "state_sha256": intended,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    generic.commit(
        tx_id=intended,
        observed_state_sha256=intended,
        semantic_binding_sha256=binding,
    )

    restarted = _authority(tmp_path)
    assert restarted.verify() == (fake,)
    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="requires authenticated acquisition",
    ):
        restarted.resolve(
            receipt_id=fake.receipt_id,
            record_sha256=fake.record_sha256,
            as_of=fake.available_at + timedelta(seconds=1),
        )


def test_caller_cannot_relabel_authenticated_provider_identity(tmp_path) -> None:
    credentials = BetfairSessionCredentials("app-key", "session-token")
    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="identity is production-owned",
    ):
        BetfairMarketCommissionAuthority(
            tmp_path / "workspace",
            credentials,
            authority_root=tmp_path / "authority",
            account_id="caller-account",
        )
    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="identity is production-owned",
    ):
        BetfairMarketCommissionAuthority(
            tmp_path / "workspace",
            credentials,
            authority_root=tmp_path / "authority",
            venue_id="caller-venue",
        )


def test_caller_constructed_receipt_cannot_be_committed(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch)
    authority = _authority(tmp_path)
    fake = _forged_receipt()
    with pytest.raises(
        BetfairMarketCommissionAuthorityError,
        match="requires authenticated acquisition",
    ):
        authority.resolve(
            receipt_id=fake.receipt_id,
            record_sha256=fake.record_sha256,
            as_of=fake.available_at + timedelta(seconds=1),
        )
    with pytest.raises(TypeError):
        authority.capture_market("1.143732676", receipt=fake)


def test_provider_correction_supersedes_old_market_receipt(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch, commission="0.13", digit="b")
    authority = _authority(tmp_path)
    first = authority.capture_market("1.143732676")
    _install_provider(
        monkeypatch, commission="0.09", profit="2.56", digit="c"
    )
    corrected = authority.capture_market("1.143732676")
    assert corrected.supersedes_receipt_id == first.receipt_id
    assert corrected.commission == Decimal("0.09")
    with pytest.raises(
        BetfairMarketCommissionAuthorityError, match="superseded"
    ):
        authority.resolve(
            receipt_id=first.receipt_id,
            record_sha256=first.record_sha256,
            as_of=corrected.available_at + timedelta(seconds=1),
        )


def test_incomplete_provider_page_fails_closed(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch, more=True)
    authority = _authority(tmp_path)
    with pytest.raises(
        BetfairMarketCommissionAuthorityError, match="incomplete"
    ):
        authority.capture_market("1.143732676")


def test_tampered_provider_state_fails_after_restart(
    monkeypatch, tmp_path
) -> None:
    _install_provider(monkeypatch)
    authority = _authority(tmp_path)
    authority.capture_market("1.143732676")
    path = (
        tmp_path
        / "workspace"
        / "betfair_market_commission"
        / "state.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["records"][0]["commission"] = "99"
    path.write_text(json.dumps(raw), encoding="utf-8")
    restarted = _authority(tmp_path)
    with pytest.raises(BetfairMarketCommissionAuthorityError):
        restarted.verify()
