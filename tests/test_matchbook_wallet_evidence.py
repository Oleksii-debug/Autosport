from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from urllib.parse import parse_qs, urlparse

import pytest

from autosport.matchbook_provider import (
    MatchbookHttpJsonResponse,
    MatchbookTransportError,
)
from autosport.matchbook_wallet_evidence import (
    MatchbookWalletEvidenceClient,
    MatchbookWalletEvidenceError,
    TransactionType,
    deduplicate_wallet_windows,
    validate_balance_evidence,
    validate_wallet_window,
)


AFTER = "2026-09-21T00:00:00Z"
BEFORE = "2026-09-22T00:00:00Z"


def _row(
    ident: int,
    *,
    when: str,
    kind: str,
    debit: str = "0",
    credit: str = "0",
    balance: str = "100",
    product: str = "Exchange",
    detail: str = "provider cash row",
    currency: str = "GBP",
    third_party: str | None = None,
) -> dict[str, object]:
    return {
        "id": ident,
        "time": when,
        "transaction-type": kind,
        "product": product,
        "detail": detail,
        "debit": Decimal(debit),
        "credit": Decimal(credit),
        "balance": Decimal(balance),
        "currency": currency,
        "third-party-transaction-id": third_party,
    }


def _response(
    payload: object,
    status: int = 200,
) -> MatchbookHttpJsonResponse:
    raw = json.dumps(
        payload,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return MatchbookHttpJsonResponse(
        payload=payload,
        status_code=status,
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
    def __init__(
        self,
        pages: dict[int, list[dict[str, object]]],
        *,
        balance: dict[str, object] | None = None,
    ) -> None:
        self.pages = pages
        self.balance = balance or {
            "id": 77,
            "balance": Decimal("116.25"),
            "exposure": Decimal("10"),
            "commission-reserve": Decimal("1.20"),
            "free-funds": Decimal("105.05"),
        }
        self.calls: list[
            tuple[str, dict[str, str], float]
        ] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> MatchbookHttpJsonResponse:
        self.calls.append(
            (url, dict(headers), timeout)
        )
        parsed = urlparse(url)
        if parsed.path.endswith("/transactions"):
            query = parse_qs(parsed.query)
            offset = int(query["offset"][0])
            return _response(
                {
                    "transactions": self.pages.get(
                        offset,
                        [],
                    )
                }
            )
        if parsed.path.endswith("/account/balance"):
            return _response(self.balance)
        raise AssertionError(parsed.path)


def _client(
    transport: _Transport,
    *,
    token: str = "super-secret-session",
) -> MatchbookWalletEvidenceClient:
    return MatchbookWalletEvidenceClient(
        token,
        transport=transport,
        clock=_Clock(),
        sleeper=lambda _: None,
    )


def _window(
    transport: _Transport,
    *,
    per_page: int = 2,
):
    return _client(
        transport
    ).read_transaction_window(
        after=AFTER,
        before=BEFORE,
        per_page=per_page,
    )


def test_reads_closed_window_and_preserves_cashflow_classes() -> None:
    transport = _Transport(
        {
            0: [
                _row(
                    1,
                    when="2026-09-21T10:00:00Z",
                    kind="Payout",
                    credit="12.50",
                    balance="112.50",
                ),
                _row(
                    2,
                    when="2026-09-21T11:00:00Z",
                    kind="Commission",
                    debit="-1.25",
                    balance="111.25",
                    third_party="",
                ),
            ],
            2: [
                _row(
                    3,
                    when="2026-09-21T12:00:00Z",
                    kind="Bonus",
                    credit="5",
                    balance="116.25",
                    product="Account",
                ),
            ],
        }
    )

    evidence = _window(transport)

    assert evidence.pagination_complete is True
    assert [page.offset for page in evidence.pages] == [
        0,
        2,
    ]
    assert len(evidence.transactions) == 3
    assert evidence.total_cash_delta == Decimal(
        "16.25"
    )
    assert evidence.payout_cashflow == Decimal(
        "12.50"
    )
    assert evidence.commission_cashflow == Decimal(
        "-1.25"
    )
    assert evidence.bonus_cashflow == Decimal(
        "5"
    )
    assert evidence.transfer_cashflow == Decimal(
        "0"
    )
    assert evidence.manual_cashflow == Decimal(
        "0"
    )
    assert evidence.cancel_cashflow == Decimal(
        "0"
    )

    assert evidence.strategy_net_economic_pnl is None
    assert evidence.provider_api_cost_cashflow is None
    assert evidence.positive_net_edge_proven is False
    assert (
        evidence.transactions[1].third_party_transaction_id
        is None
    )
    assert validate_wallet_window(evidence) is True


def test_query_is_frozen_and_secret_never_enters_url_or_evidence() -> None:
    transport = _Transport({0: []})
    token = "token-DO-NOT-LEAK"
    evidence = _client(
        transport,
        token=token,
    ).read_transaction_window(
        after=AFTER,
        before=BEFORE,
        transaction_types=(
            TransactionType.PAYOUT,
            TransactionType.COMMISSION,
        ),
        per_page=20,
    )

    assert len(transport.calls) == 1
    url, headers, timeout = transport.calls[0]
    query = parse_qs(urlparse(url).query)
    assert query == {
        "offset": ["0"],
        "per-page": ["20"],
        "transaction-type": [
            "payout,commission"
        ],
        "after": [AFTER],
        "before": [BEFORE],
    }
    assert headers["session-token"] == token
    assert timeout > 0
    assert token not in url
    assert token not in repr(evidence)


def test_balance_is_snapshot_not_attribution_authority() -> None:
    evidence = _client(
        _Transport({0: []})
    ).read_balance()

    assert evidence.account_id == "77"
    assert evidence.balance == Decimal("116.25")
    assert evidence.exposure == Decimal("10")
    assert evidence.commission_reserve == Decimal(
        "1.20"
    )
    assert evidence.free_funds == Decimal("105.05")
    assert (
        evidence.transaction_attribution_authorized
        is False
    )
    assert evidence.strategy_reward_authorized is False
    assert validate_balance_evidence(evidence) is True


@pytest.mark.parametrize(
    "field",
    [
        "debit",
        "credit",
        "balance",
    ],
)
def test_rejects_binary_float_money_ingress(
    field: str,
) -> None:
    row = _row(
        1,
        when="2026-09-21T10:00:00Z",
        kind="Manual",
        debit="-2",
        balance="98",
        product="Account",
    )
    row[field] = (
        -2.0 if field == "debit" else 2.0
    )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="exact decimal data",
    ):
        _window(_Transport({0: [row]}))


@pytest.mark.parametrize(
    "when",
    [
        "2026-09-20T23:59:59Z",
        AFTER,
        BEFORE,
        "2026-09-22T00:00:01Z",
    ],
)
def test_rejects_rows_outside_frozen_query_window(
    when: str,
) -> None:
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="outside query window",
    ):
        _window(
            _Transport(
                {
                    0: [
                        _row(
                            1,
                            when=when,
                            kind="Manual",
                            debit="-1",
                            balance="99",
                            product="Account",
                        )
                    ]
                }
            )
        )


def test_duplicate_transaction_identity_across_pages_fails_closed() -> None:
    same = _row(
        1,
        when="2026-09-21T10:00:00Z",
        kind="Transfer",
        debit="-5",
        balance="95",
        product="Account",
    )
    transport = _Transport(
        {
            0: [
                same,
                _row(
                    2,
                    when="2026-09-21T10:30:00Z",
                    kind="Manual",
                    debit="-1",
                    balance="94",
                    product="Account",
                ),
            ],
            2: [same],
        }
    )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="duplicate transaction id",
    ):
        _window(transport)


def test_max_page_budget_never_becomes_complete_window() -> None:
    client = _client(
        _Transport(
            {
                0: [
                    _row(
                        1,
                        when="2026-09-21T10:00:00Z",
                        kind="Payout",
                        credit="1",
                        balance="101",
                    )
                ]
            }
        )
    )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="pagination exceeded max_pages",
    ):
        client.read_transaction_window(
            after=AFTER,
            before=BEFORE,
            per_page=1,
            max_pages=1,
        )


def test_requires_raw_body_digest_for_both_surfaces() -> None:
    def no_digest(
        url: str,
        headers: dict[str, str],
        timeout: float,
    ):
        payload = (
            {"transactions": []}
            if urlparse(url).path.endswith(
                "/transactions"
            )
            else {
                "id": 77,
                "balance": Decimal("1"),
                "exposure": Decimal("0"),
                "commission-reserve": Decimal("0"),
                "free-funds": Decimal("1"),
            }
        )
        return MatchbookHttpJsonResponse(
            payload,
            200,
            {},
            None,
        )

    client = MatchbookWalletEvidenceClient(
        "token",
        transport=no_digest,
        clock=_Clock(),
        sleeper=lambda _: None,
    )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="body SHA-256",
    ):
        client.read_transaction_window(
            after=AFTER,
            before=BEFORE,
        )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="body SHA-256",
    ):
        client.read_balance()


def test_429_and_5xx_retry_but_401_does_not() -> None:
    for status in (429, 503):
        attempts = 0

        def retrying(
            url: str,
            headers: dict[str, str],
            timeout: float,
        ):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise MatchbookTransportError(
                    f"HTTP {status}",
                    status_code=status,
                    retry_after=0,
                )
            return _response(
                {"transactions": []}
            )

        client = MatchbookWalletEvidenceClient(
            "token",
            transport=retrying,
            clock=_Clock(),
            sleeper=lambda _: None,
            max_attempts=2,
        )
        evidence = client.read_transaction_window(
            after=AFTER,
            before=BEFORE,
        )
        assert evidence.pagination_complete is True
        assert attempts == 2

    attempts = 0

    def unauthorized(
        url: str,
        headers: dict[str, str],
        timeout: float,
    ):
        nonlocal attempts
        attempts += 1
        raise MatchbookTransportError(
            "HTTP 401",
            status_code=401,
        )

    client = MatchbookWalletEvidenceClient(
        "token",
        transport=unauthorized,
        clock=_Clock(),
        sleeper=lambda _: None,
        max_attempts=5,
    )
    with pytest.raises(MatchbookTransportError):
        client.read_transaction_window(
            after=AFTER,
            before=BEFORE,
        )
    assert attempts == 1


def test_unknown_or_ambiguous_transaction_type_fails_closed() -> None:
    unknown = _row(
        1,
        when="2026-09-21T10:00:00Z",
        kind="Mystery",
        credit="1",
        balance="101",
    )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="unsupported transaction type",
    ):
        _window(
            _Transport({0: [unknown]})
        )

    ambiguous = _row(
        1,
        when="2026-09-21T10:00:00Z",
        kind="Payout",
        credit="1",
        balance="101",
    )
    ambiguous["type"] = "payout"
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="ambiguous transaction field aliases",
    ):
        _window(
            _Transport({0: [ambiguous]})
        )


def test_cached_evidence_forgery_is_detected() -> None:
    wallet = _window(
        _Transport(
            {
                0: [
                    _row(
                        1,
                        when="2026-09-21T10:00:00Z",
                        kind="Payout",
                        credit="2",
                        balance="102",
                    )
                ]
            }
        )
    )
    balance = _client(
        _Transport({0: []})
    ).read_balance()

    assert validate_wallet_window(
        replace(
            wallet,
            payout_cashflow=Decimal("999"),
        )
    ) is False
    assert validate_balance_evidence(
        replace(
            balance,
            evidence_sha256="0" * 64,
        )
    ) is False


def test_restart_overlap_dedup_and_conflict_detection() -> None:
    base_row = _row(
        1,
        when="2026-09-21T10:00:00Z",
        kind="Manual",
        debit="-10",
        balance="90",
        product="Account",
    )
    first = _window(
        _Transport({0: [base_row]})
    )
    second = _window(
        _Transport({0: [dict(base_row)]})
    )

    merged = deduplicate_wallet_windows(
        (first, second)
    )
    assert len(merged) == 1
    assert merged[0].transaction_id == "1"
    assert merged[0].cash_delta == Decimal("-10")

    changed = dict(base_row)
    changed["balance"] = Decimal("89")
    third = _window(
        _Transport({0: [changed]})
    )
    with pytest.raises(
        MatchbookWalletEvidenceError,
        match="transaction id changed across reread",
    ):
        deduplicate_wallet_windows(
            (first, third)
        )


def test_bonus_transfer_manual_never_become_strategy_reward() -> None:
    evidence = _window(
        _Transport(
            {
                0: [
                    _row(
                        1,
                        when="2026-09-21T09:00:00Z",
                        kind="Bonus",
                        credit="25",
                        balance="125",
                        product="Account",
                    ),
                    _row(
                        2,
                        when="2026-09-21T10:00:00Z",
                        kind="Transfer",
                        debit="-10",
                        balance="115",
                        product="Account",
                    ),
                ],
                2: [
                    _row(
                        3,
                        when="2026-09-21T11:00:00Z",
                        kind="Manual",
                        credit="3",
                        balance="118",
                        product="Account",
                    )
                ],
            }
        )
    )

    assert evidence.bonus_cashflow == Decimal("25")
    assert evidence.transfer_cashflow == Decimal("-10")
    assert evidence.manual_cashflow == Decimal("3")
    assert evidence.total_cash_delta == Decimal("18")
    assert evidence.strategy_net_economic_pnl is None
    assert evidence.provider_api_cost_cashflow is None
    assert evidence.positive_net_edge_proven is False
