from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message
from io import BytesIO
import json

import pytest

import autosport.smarkets_session_context as session_context
from autosport.smarkets_account_activity import (
    SmarketsAccountActivityError,
    assert_smarkets_account_activity_page_authoritative,
    parse_smarkets_account_activity_page,
)
from autosport.smarkets_session_context import (
    SmarketsAccountActivityQuery,
    open_smarkets_authenticated_session,
)


def _account_payload(currency: str = "GBP") -> bytes:
    return (
        '{"account":{'
        '"account_id":"provider-account-A",'
        '"balance":"1000.00",'
        '"available_balance":"900.00",'
        '"exposure":"100.00",'
        f'"currency":"{currency}"'
        '}}'
    ).encode("utf-8")


class _FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        date: str = "Mon, 21 Sep 2026 10:00:00 GMT",
    ) -> None:
        self._stream = BytesIO(payload)
        self.url = url
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Date"] = date
        self.headers["Content-Length"] = str(len(payload))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._stream.read(size)


def _row(
    *,
    seq: int = 20,
    subseq: int = 3,
    source: str = "market.settle",
    timestamp: str = "2026-09-21T10:00:00+00:00",
    commission: str | None = "2.20",
    money_change: str | None = "107.80",
    market_id: str | None = "128939",
    order_id: str | None = None,
    label: str | None = "autosport",
) -> dict[str, object]:
    return {
        "amount": "110.00",
        "commission": commission,
        "contract_id": "123456",
        "event_id": "565413",
        "exposure": "0.00",
        "label": label,
        "market_id": market_id,
        "money": "1107.80",
        "money_change": money_change,
        "order_id": order_id,
        "price": 5000,
        "quantity": 100000,
        "quantity_change": -100000,
        "quantity_user_currency": 100000,
        "quantity_user_currency_change": -100000,
        "seq": seq,
        "side": "buy",
        "source": source,
        "subseq": subseq,
        "timestamp": timestamp,
    }


def _activity_payload(
    rows: list[dict[str, object]],
    *,
    pagination: object = ...,
    extra_root: dict[str, object] | None = None,
) -> bytes:
    value: dict[str, object] = {"account_activity": rows}
    if pagination is not ...:
        value["pagination"] = pagination
    if extra_root:
        value.update(extra_root)
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _session_with_activity(
    monkeypatch,
    payload: bytes,
    *,
    query: SmarketsAccountActivityQuery | None = None,
    currency: str = "GBP",
):
    request_query = "" if query is None else query.to_query_string()
    activity_url = session_context.SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT
    if request_query:
        activity_url = f"{activity_url}?{request_query}"
    responses = iter(
        (
            _FakeResponse(
                _account_payload(currency),
                url=session_context.SMARKETS_ACCOUNTS_ENDPOINT,
            ),
            _FakeResponse(
                payload,
                url=activity_url,
                date="Mon, 21 Sep 2026 10:00:01 GMT",
            ),
        )
    )
    monkeypatch.setattr(
        session_context,
        "_open_accounts_request",
        lambda request, timeout: next(responses),
    )
    monkeypatch.setattr(
        session_context,
        "_new_generation_id",
        lambda: "generation-economic-A",
    )
    times = iter(
        (
            "2026-09-21T10:00:00+00:00",
            "2026-09-21T10:00:02+00:00",
        )
    )
    monkeypatch.setattr(session_context, "_utc_now_iso", lambda: next(times))

    session = open_smarkets_authenticated_session("session-secret")
    read = session.acquire_account_activity(query=query)
    return session, read


def test_session_issued_statement_row_preserves_provider_economics_and_scope(
    monkeypatch,
) -> None:
    query = SmarketsAccountActivityQuery(
        timestamp_min=datetime(2026, 9, 21, 9, tzinfo=timezone.utc),
        timestamp_max=datetime(2026, 9, 21, 11, tzinfo=timezone.utc),
        limit=20,
        market_ids=("128939",),
        sort="-seq,-subseq",
    )
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()], pagination={"next_page": None}),
        query=query,
        currency="EUR",
    )

    page = parse_smarkets_account_activity_page(session, read)
    assert_smarkets_account_activity_page_authoritative(page)

    assert page.provider_account_id == "provider-account-A"
    assert page.provider_currency == "EUR"
    assert page.source_payload_sha256 == read.payload_sha256
    assert page.source_read_evidence_sha256 == read.evidence_sha256
    assert page.pagination_present is True
    assert page.next_page_query is None
    assert page.query_exhausted is True
    assert len(page.rows) == 1

    row = page.rows[0]
    assert row.provider_row_id == (20, 3)
    assert row.source == "market.settle"
    assert row.commission == Decimal("2.20")
    assert row.money_change == Decimal("107.80")
    assert row.market_id == "128939"
    assert row.order_id is None
    assert row.attribution_scope == "market"
    assert row.provider_currency == "EUR"


def test_null_commission_does_not_alias_explicit_zero_commission(monkeypatch) -> None:
    null_row = _row(seq=21, commission=None, money_change="0.00")
    zero_row = _row(seq=20, commission="0.00", money_change="0.00")
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload(
            [null_row, zero_row],
            pagination={"next_page": None},
        ),
    )

    page = parse_smarkets_account_activity_page(session, read)

    assert page.rows[0].commission is None
    assert page.rows[1].commission == Decimal("0.00")
    assert page.rows[0].row_sha256 != page.rows[1].row_sha256


def test_missing_pagination_never_mints_complete_query_claim(monkeypatch) -> None:
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()]),
    )

    page = parse_smarkets_account_activity_page(session, read)

    assert page.pagination_present is False
    assert page.next_page_query is None
    assert page.query_exhausted is False


def test_zero_limit_query_never_mints_exhausted_window(monkeypatch) -> None:
    query = SmarketsAccountActivityQuery(limit=0)
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([], pagination={"next_page": None}),
        query=query,
    )

    page = parse_smarkets_account_activity_page(session, read)

    assert page.query_exhausted is False


def test_next_page_binds_last_provider_cursor_and_original_scope(monkeypatch) -> None:
    query = SmarketsAccountActivityQuery(
        limit=1,
        market_ids=("128939",),
        sort="seq,subseq",
        sources=("market.settle",),
    )
    next_page = (
        "?limit=1&market_id=128939&sort=seq%2Csubseq"
        "&source=market.settle"
        "&pagination_last_seq=20&pagination_last_subseq=3"
    )
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()], pagination={"next_page": next_page}),
        query=query,
    )

    page = parse_smarkets_account_activity_page(session, read)

    assert page.query_exhausted is False
    assert page.next_page_query == next_page


def test_next_page_cannot_change_market_scope(monkeypatch) -> None:
    query = SmarketsAccountActivityQuery(
        limit=1,
        market_ids=("128939",),
        sort="seq,subseq",
    )
    next_page = (
        "?limit=1&market_id=999999&sort=seq%2Csubseq"
        "&pagination_last_seq=20&pagination_last_subseq=3"
    )
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()], pagination={"next_page": next_page}),
        query=query,
    )

    with pytest.raises(SmarketsAccountActivityError, match="changes request scope"):
        parse_smarkets_account_activity_page(session, read)


def test_next_page_cursor_must_match_last_row(monkeypatch) -> None:
    query = SmarketsAccountActivityQuery(limit=1, sort="-seq,-subseq")
    next_page = (
        "?limit=1&sort=-seq%2C-subseq"
        "&pagination_last_seq=999&pagination_last_subseq=1"
    )
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()], pagination={"next_page": next_page}),
        query=query,
    )

    with pytest.raises(SmarketsAccountActivityError, match="does not match"):
        parse_smarkets_account_activity_page(session, read)


def test_duplicate_provider_row_identity_is_rejected(monkeypatch) -> None:
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload(
            [_row(seq=20, subseq=3), _row(seq=20, subseq=3)],
            pagination={"next_page": None},
        ),
    )

    with pytest.raises(SmarketsAccountActivityError, match="repeats provider"):
        parse_smarkets_account_activity_page(session, read)


def test_provider_row_order_must_match_query_sort(monkeypatch) -> None:
    query = SmarketsAccountActivityQuery(limit=20, sort="-seq,-subseq")
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload(
            [_row(seq=19), _row(seq=20)],
            pagination={"next_page": None},
        ),
        query=query,
    )

    with pytest.raises(SmarketsAccountActivityError, match="descending"):
        parse_smarkets_account_activity_page(session, read)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.pop("seq"), "lacks required evidence field"),
        (lambda row: row.__setitem__("source", "invented.provider.event"), "documented"),
        (lambda row: row.__setitem__("commission", "NaN"), "finite exact decimal"),
        (lambda row: row.__setitem__("market_id", "market-A"), "numeric provider id"),
        (lambda row: row.__setitem__("side", "BACK"), "side must"),
        (lambda row: row.__setitem__("unknown_field", "x"), "unsupported fields"),
    ],
)
def test_malformed_economic_rows_fail_closed(monkeypatch, mutation, message) -> None:
    row = _row()
    mutation(row)
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([row], pagination={"next_page": None}),
    )

    with pytest.raises(SmarketsAccountActivityError, match=message):
        parse_smarkets_account_activity_page(session, read)


def test_account_market_and_order_scope_are_not_synthesized(monkeypatch) -> None:
    rows = [
        _row(seq=22, market_id=None, order_id=None, source="deposit"),
        _row(seq=21, market_id="128939", order_id=None, source="market.settle"),
        _row(
            seq=20,
            market_id="128939",
            order_id="1518455672861",
            source="order.settle",
        ),
    ]
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload(rows, pagination={"next_page": None}),
    )

    page = parse_smarkets_account_activity_page(session, read)

    assert [row.attribution_scope for row in page.rows] == [
        "account",
        "market",
        "order",
    ]


def test_unknown_root_field_and_malformed_pagination_fail_closed(monkeypatch) -> None:
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload(
            [_row()],
            pagination={"next_page": None},
            extra_root={"caller_complete": True},
        ),
    )
    with pytest.raises(SmarketsAccountActivityError, match="unsupported fields"):
        parse_smarkets_account_activity_page(session, read)


def test_payload_rebinding_cannot_mint_economic_evidence(monkeypatch) -> None:
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()], pagination={"next_page": None}),
    )
    object.__setattr__(
        read,
        "_payload",
        _activity_payload(
            [_row(commission="999.99")],
            pagination={"next_page": None},
        ),
    )

    with pytest.raises(SmarketsAccountActivityError, match="payload bytes"):
        parse_smarkets_account_activity_page(session, read)


def test_cross_session_read_cannot_mint_economic_evidence(monkeypatch) -> None:
    activity = _activity_payload([_row()], pagination={"next_page": None})
    responses = iter(
        (
            _FakeResponse(
                _account_payload(),
                url=session_context.SMARKETS_ACCOUNTS_ENDPOINT,
            ),
            _FakeResponse(
                _account_payload(),
                url=session_context.SMARKETS_ACCOUNTS_ENDPOINT,
            ),
            _FakeResponse(
                activity,
                url=session_context.SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT,
            ),
        )
    )
    monkeypatch.setattr(
        session_context,
        "_open_accounts_request",
        lambda request, timeout: next(responses),
    )
    generations = iter(("generation-A", "generation-B"))
    monkeypatch.setattr(
        session_context,
        "_new_generation_id",
        lambda: next(generations),
    )
    times = iter(
        (
            "2026-09-21T10:00:00+00:00",
            "2026-09-21T10:00:00+00:00",
            "2026-09-21T10:00:01+00:00",
        )
    )
    monkeypatch.setattr(session_context, "_utc_now_iso", lambda: next(times))

    session_a = open_smarkets_authenticated_session("token-A")
    session_b = open_smarkets_authenticated_session("token-B")
    read_a = session_a.acquire_account_activity()

    with pytest.raises(SmarketsAccountActivityError, match="not authoritative"):
        parse_smarkets_account_activity_page(session_b, read_a)


def test_issued_page_rejects_post_issuance_field_mutation(monkeypatch) -> None:
    session, read = _session_with_activity(
        monkeypatch,
        _activity_payload([_row()], pagination={"next_page": None}),
    )
    page = parse_smarkets_account_activity_page(session, read)
    assert_smarkets_account_activity_page_authoritative(page)

    object.__setattr__(page, "provider_currency", "USD")

    with pytest.raises(SmarketsAccountActivityError, match="fields changed"):
        assert_smarkets_account_activity_page_authoritative(page)


def test_page_does_not_expose_raw_provider_payload_or_session_secret(monkeypatch) -> None:
    payload = _activity_payload(
        [_row(label="private-strategy-marker")],
        pagination={"next_page": None},
    )
    session, read = _session_with_activity(monkeypatch, payload)

    page = parse_smarkets_account_activity_page(session, read)

    assert "session-secret" not in repr(page)
    assert payload.decode("utf-8") not in repr(page)
