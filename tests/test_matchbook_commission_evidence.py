from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from urllib.parse import urlparse

import pytest

from autosport.matchbook_commission_evidence import (
    CommissionCashStatus,
    CommissionPolicyTier,
    MatchbookCommissionEvidenceError,
    build_commission_policy_evidence,
    build_unallocated_commission_economic_view,
    derive_commission_cash_evidence,
)
from autosport.matchbook_provider import MatchbookHttpJsonResponse
from autosport.matchbook_wallet_evidence import (
    MatchbookWalletEvidenceClient,
    TransactionType,
)


AFTER = "2026-09-21T00:00:00Z"
BEFORE = "2026-09-22T00:00:00Z"
POLICY_URL = "https://insights.matchbook.com/education/what-is-commission/"
POLICY_SHA = "1" * 64
ACCOUNT_SHA = "2" * 64


def _row(
    ident: int,
    *,
    when: str,
    kind: str,
    debit: str = "0",
    credit: str = "0",
    balance: str = "100",
    currency: str = "GBP",
) -> dict[str, object]:
    return {
        "id": ident,
        "time": when,
        "transaction-type": kind,
        "product": "Exchange",
        "detail": f"{kind} row",
        "debit": Decimal(debit),
        "credit": Decimal(credit),
        "balance": Decimal(balance),
        "currency": currency,
        "third-party-transaction-id": None,
    }


def _response(payload: object) -> MatchbookHttpJsonResponse:
    raw = json.dumps(
        payload,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return MatchbookHttpJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256=hashlib.sha256(raw).hexdigest(),
    )


class _Clock:
    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-09-21T20:00:{self.tick:02d}Z"


class _Transport:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = list(rows)

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> MatchbookHttpJsonResponse:
        assert urlparse(url).path.endswith("/transactions")
        assert headers["session-token"] == "secret"
        assert timeout > 0
        return _response({"transactions": self.rows})


def _window(
    rows: list[dict[str, object]],
    *,
    transaction_types: tuple[TransactionType, ...] = tuple(TransactionType),
):
    return MatchbookWalletEvidenceClient(
        "secret",
        transport=_Transport(rows),
        clock=_Clock(),
        sleeper=lambda _: None,
    ).read_transaction_window(
        after=AFTER,
        before=BEFORE,
        transaction_types=transaction_types,
        per_page=100,
    )


def test_advertised_policy_is_descriptive_not_cash_truth() -> None:
    evidence = build_commission_policy_evidence(
        tier=CommissionPolicyTier.ADVERTISED_POLICY,
        source_url=POLICY_URL,
        source_sha256=POLICY_SHA,
        observed_at="2026-09-22T10:00:00+00:00",
        rate=Decimal("0.02"),
    )

    assert evidence.rate == Decimal("0.02")
    assert evidence.observed_at == "2026-09-22T10:00:00Z"
    assert evidence.account_scope_sha256 is None
    assert evidence.provider_cash_truth is False
    assert evidence.per_bet_rate_authorized is False


def test_account_effective_policy_requires_account_evidence_identity() -> None:
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="requires account evidence identity",
    ):
        build_commission_policy_evidence(
            tier=CommissionPolicyTier.ACCOUNT_EFFECTIVE_POLICY,
            source_url=POLICY_URL,
            source_sha256=POLICY_SHA,
            observed_at="2026-09-22T10:00:00Z",
            rate=Decimal("0"),
        )

    evidence = build_commission_policy_evidence(
        tier=CommissionPolicyTier.ACCOUNT_EFFECTIVE_POLICY,
        source_url=POLICY_URL,
        source_sha256=POLICY_SHA,
        observed_at="2026-09-22T10:00:00Z",
        rate=Decimal("0"),
        account_scope_sha256=ACCOUNT_SHA,
    )
    assert evidence.account_scope_sha256 == ACCOUNT_SHA
    assert evidence.provider_cash_truth is False
    assert evidence.per_bet_rate_authorized is False


@pytest.mark.parametrize("rate", [Decimal("-0.01"), Decimal("1.01")])
def test_policy_rate_outside_unit_interval_fails_closed(rate: Decimal) -> None:
    with pytest.raises(MatchbookCommissionEvidenceError, match="within"):
        build_commission_policy_evidence(
            tier=CommissionPolicyTier.ADVERTISED_POLICY,
            source_url=POLICY_URL,
            source_sha256=POLICY_SHA,
            observed_at="2026-09-22T10:00:00Z",
            rate=rate,
        )


def test_advertised_policy_cannot_claim_account_scope() -> None:
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="cannot claim account scope",
    ):
        build_commission_policy_evidence(
            tier=CommissionPolicyTier.ADVERTISED_POLICY,
            source_url=POLICY_URL,
            source_sha256=POLICY_SHA,
            observed_at="2026-09-22T10:00:00Z",
            account_scope_sha256=ACCOUNT_SHA,
        )


def test_extracts_only_typed_commission_cash_and_keeps_it_unallocated() -> None:
    window = _window(
        [
            _row(
                1,
                when="2026-09-21T10:00:00Z",
                kind="Payout",
                credit="100",
                balance="200",
            ),
            _row(
                2,
                when="2026-09-21T10:01:00Z",
                kind="Commission",
                debit="-2",
                balance="198",
            ),
            _row(
                3,
                when="2026-09-21T10:02:00Z",
                kind="Bonus",
                credit="25",
                balance="223",
            ),
        ]
    )

    evidence = derive_commission_cash_evidence(window)

    assert evidence.status is CommissionCashStatus.CASH_ROWS_OBSERVED_UNALLOCATED
    assert evidence.transaction_ids == ("2",)
    assert len(evidence.amounts_by_currency) == 1
    assert evidence.amounts_by_currency[0].currency == "GBP"
    assert evidence.amounts_by_currency[0].amount == Decimal("-2")
    assert evidence.amounts_by_currency[0].transaction_count == 1
    assert evidence.cash_zero_proven is False
    assert evidence.economic_allocation_authorized is False
    assert evidence.provider_api_cost_authorized is False
    assert evidence.positive_net_edge_proven is False


def test_commission_reversal_is_preserved_as_signed_cash_not_relabelled() -> None:
    evidence = derive_commission_cash_evidence(
        _window(
            [
                _row(
                    10,
                    when="2026-09-21T10:00:00Z",
                    kind="Commission",
                    debit="-2.00",
                    balance="98",
                ),
                _row(
                    11,
                    when="2026-09-21T10:01:00Z",
                    kind="Commission",
                    credit="0.50",
                    balance="98.50",
                ),
            ]
        )
    )

    assert evidence.transaction_ids == ("10", "11")
    assert evidence.amounts_by_currency[0].amount == Decimal("-1.50")
    assert evidence.amounts_by_currency[0].transaction_count == 2


def test_missing_commission_row_never_proves_zero_commission() -> None:
    evidence = derive_commission_cash_evidence(
        _window(
            [
                _row(
                    1,
                    when="2026-09-21T10:00:00Z",
                    kind="Payout",
                    credit="10",
                    balance="110",
                )
            ]
        )
    )

    assert evidence.status is CommissionCashStatus.NO_COMMISSION_ROWS_ZERO_NOT_PROVEN
    assert evidence.transaction_ids == ()
    assert evidence.amounts_by_currency == ()
    assert evidence.cash_zero_proven is False


def test_query_that_excluded_commission_cannot_mint_commission_absence() -> None:
    window = _window(
        [],
        transaction_types=(TransactionType.PAYOUT,),
    )
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="did not include commission",
    ):
        derive_commission_cash_evidence(window)


def test_mixed_currencies_remain_separate_and_are_never_fx_nettted() -> None:
    evidence = derive_commission_cash_evidence(
        _window(
            [
                _row(
                    1,
                    when="2026-09-21T10:00:00Z",
                    kind="Commission",
                    debit="-2",
                    balance="98",
                    currency="GBP",
                ),
                _row(
                    2,
                    when="2026-09-21T10:01:00Z",
                    kind="Commission",
                    debit="-3",
                    balance="95",
                    currency="EUR",
                ),
            ]
        )
    )

    assert [(item.currency, item.amount) for item in evidence.amounts_by_currency] == [
        ("EUR", Decimal("-3")),
        ("GBP", Decimal("-2")),
    ]


def test_gross_positive_result_stays_net_unresolved_without_attribution() -> None:
    commission = derive_commission_cash_evidence(
        _window(
            [
                _row(
                    7,
                    when="2026-09-21T10:00:00Z",
                    kind="Commission",
                    debit="-2",
                    balance="98",
                )
            ]
        )
    )
    view = build_unallocated_commission_economic_view(
        economic_scope_id="market:42",
        currency="GBP",
        gross_pnl=Decimal("100"),
        commission=commission,
    )

    assert view.gross_pnl == Decimal("100")
    assert view.observed_commission_cash_same_currency == Decimal("-2")
    assert view.net_economic_pnl is None
    assert view.commission_allocation_status == "UNALLOCATED_PROVIDER_COMMISSION"
    assert view.positive_net_edge_proven is False


def test_no_commission_row_still_leaves_net_unresolved() -> None:
    commission = derive_commission_cash_evidence(_window([]))
    view = build_unallocated_commission_economic_view(
        economic_scope_id="market:42",
        currency="GBP",
        gross_pnl=Decimal("100"),
        commission=commission,
    )

    assert view.observed_commission_cash_same_currency is None
    assert view.net_economic_pnl is None
    assert view.commission_allocation_status == "COMMISSION_APPLICABILITY_UNRESOLVED"
    assert view.positive_net_edge_proven is False


def test_foreign_currency_commission_does_not_become_scope_net_cost() -> None:
    commission = derive_commission_cash_evidence(
        _window(
            [
                _row(
                    8,
                    when="2026-09-21T10:00:00Z",
                    kind="Commission",
                    debit="-4",
                    balance="96",
                    currency="EUR",
                )
            ]
        )
    )
    view = build_unallocated_commission_economic_view(
        economic_scope_id="market:42",
        currency="GBP",
        gross_pnl=Decimal("100"),
        commission=commission,
    )

    assert view.observed_commission_cash_same_currency is None
    assert view.net_economic_pnl is None
    assert view.positive_net_edge_proven is False


def test_tampered_commission_evidence_is_rejected_before_economic_use() -> None:
    commission = derive_commission_cash_evidence(
        _window(
            [
                _row(
                    8,
                    when="2026-09-21T10:00:00Z",
                    kind="Commission",
                    debit="-4",
                    balance="96",
                )
            ]
        )
    )
    forged = replace(
        commission,
        economic_allocation_authorized=True,
    )
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="allocation authority",
    ):
        build_unallocated_commission_economic_view(
            economic_scope_id="market:42",
            currency="GBP",
            gross_pnl=Decimal("100"),
            commission=forged,
        )


def test_digest_is_stable_across_equivalent_decimal_encodings() -> None:
    first = build_commission_policy_evidence(
        tier=CommissionPolicyTier.ADVERTISED_POLICY,
        source_url=POLICY_URL,
        source_sha256=POLICY_SHA,
        observed_at="2026-09-22T10:00:00Z",
        rate=Decimal("0.0200"),
    )
    second = build_commission_policy_evidence(
        tier=CommissionPolicyTier.ADVERTISED_POLICY,
        source_url=POLICY_URL,
        source_sha256=POLICY_SHA,
        observed_at="2026-09-22T12:00:00+02:00",
        rate=Decimal("0.02"),
    )

    assert first.evidence_sha256 == second.evidence_sha256


def test_policy_effective_interval_must_move_forward() -> None:
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="must precede",
    ):
        build_commission_policy_evidence(
            tier=CommissionPolicyTier.ADVERTISED_POLICY,
            source_url=POLICY_URL,
            source_sha256=POLICY_SHA,
            observed_at="2026-09-22T10:00:00Z",
            effective_from="2026-10-01T00:00:00Z",
            effective_to="2026-09-01T00:00:00Z",
        )


def test_binary_float_policy_rate_is_rejected() -> None:
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="finite Decimal",
    ):
        build_commission_policy_evidence(
            tier=CommissionPolicyTier.ADVERTISED_POLICY,
            source_url=POLICY_URL,
            source_sha256=POLICY_SHA,
            observed_at="2026-09-22T10:00:00Z",
            rate=0.02,  # type: ignore[arg-type]
        )


def test_extreme_decimal_exponent_fails_before_large_canonical_materialization() -> None:
    with pytest.raises(
        MatchbookCommissionEvidenceError,
        match="bounded exact-decimal domain",
    ):
        build_commission_policy_evidence(
            tier=CommissionPolicyTier.ADVERTISED_POLICY,
            source_url=POLICY_URL,
            source_sha256=POLICY_SHA,
            observed_at="2026-09-22T10:00:00Z",
            rate=Decimal("1E-1000000"),
        )
