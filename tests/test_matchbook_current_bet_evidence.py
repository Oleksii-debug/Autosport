from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.matchbook_current_bet_evidence import (
    MatchbookCurrentBetEvidenceError,
    MatchbookCurrentBetObservation,
    MatchbookCurrentBetPaginationError,
    MatchbookCurrentBetReportPage,
    MatchbookCurrentBetReportScope,
    MatchbookCurrentBetReportTraversal,
)


OBSERVED = "2026-09-22T08:20:00Z"
ROW_SHA = "a" * 64
REQ_SHA = "b" * 64
RAW_SHA = "c" * 64


def _scope(**changes: object) -> MatchbookCurrentBetReportScope:
    values: dict[str, object] = {
        "account_context_id": "matchbook-account-1",
        "session_generation_id": "session-generation-7",
        "sport_ids": ("15",),
        "event_ids": ("101",),
        "market_ids": ("202",),
    }
    values.update(changes)
    return MatchbookCurrentBetReportScope(**values)  # type: ignore[arg-type]


def _row(
    bet_id: str = "bet-1",
    *,
    sport_id: str = "15",
    event_id: str = "101",
    market_id: str = "202",
    submitted_at: str = "2026-09-22T08:00:00Z",
    odds: Decimal = Decimal("2.50"),
    stake: Decimal = Decimal("10.00"),
) -> MatchbookCurrentBetObservation:
    return MatchbookCurrentBetObservation(
        provider_bet_id=bet_id,
        provider_sport_id=sport_id,
        provider_event_id=event_id,
        provider_market_id=market_id,
        provider_runner_id="303",
        provider_side="back",
        odds=odds,
        stake=stake,
        submitted_at_utc=submitted_at,
        provider_row_sha256=ROW_SHA,
        provider_offer_id="offer-404",
    )


def _page(
    *,
    scope: MatchbookCurrentBetReportScope | None = None,
    offset: int = 0,
    per_page: int = 2,
    rows: tuple[MatchbookCurrentBetObservation, ...] | None = None,
    observed_at: str = OBSERVED,
) -> MatchbookCurrentBetReportPage:
    return MatchbookCurrentBetReportPage(
        scope=scope or _scope(),
        offset=offset,
        per_page=per_page,
        observed_at_utc=observed_at,
        request_semantics_sha256=REQ_SHA,
        raw_response_sha256=RAW_SHA,
        rows=(_row(),) if rows is None else rows,
    )


def test_scope_is_explicit_get_decimal_and_omits_ambiguous_time_filters() -> None:
    payload = _scope().to_payload()

    assert payload["http_method"] == "GET"
    assert payload["endpoint"] == "/edge/rest/reports/v2/bets/current"
    assert payload["odds_type"] == "DECIMAL"
    assert "after" not in payload
    assert "before" not in payload
    assert "session_token" not in payload


def test_row_preserves_native_identity_and_compact_exact_decimal_values() -> None:
    row = _row(
        odds=Decimal("2.5000"),
        stake=Decimal("1E+100000"),
    )

    payload = row.to_payload()
    assert payload["provider_bet_id"] == "bet-1"
    assert payload["provider_offer_id"] == "offer-404"
    assert payload["provider_side"] == "back"
    assert payload["odds"] == "25e-1"
    assert payload["stake"] == "1e100000"
    assert len(payload["stake"]) < 16


@pytest.mark.parametrize("field", ["odds", "stake"])
def test_binary_float_cannot_enter_exact_numeric_evidence(field: str) -> None:
    values = {
        "provider_bet_id": "bet-1",
        "provider_sport_id": "15",
        "provider_event_id": "101",
        "provider_market_id": "202",
        "provider_runner_id": "303",
        "provider_side": "back",
        "odds": Decimal("2.5"),
        "stake": Decimal("10"),
        "submitted_at_utc": "2026-09-22T08:00:00Z",
        "provider_row_sha256": ROW_SHA,
    }
    values[field] = 2.5

    with pytest.raises(MatchbookCurrentBetEvidenceError, match="exact finite positive Decimal"):
        MatchbookCurrentBetObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_non_positive_or_nonfinite_numeric_evidence_fails_closed(bad: Decimal) -> None:
    with pytest.raises(MatchbookCurrentBetEvidenceError):
        _row(stake=bad)


def test_page_rejects_duplicate_bet_identity() -> None:
    with pytest.raises(MatchbookCurrentBetEvidenceError, match="duplicated within"):
        _page(rows=(_row("bet-1"), _row("bet-1")))


def test_page_rejects_future_submitted_row() -> None:
    with pytest.raises(MatchbookCurrentBetEvidenceError, match="future evidence"):
        _page(rows=(_row(submitted_at="2026-09-22T08:20:01Z"),))


@pytest.mark.parametrize(
    ("field", "row", "message"),
    [
        ("sport_ids", _row(sport_id="99"), "sport"),
        ("event_ids", _row(event_id="999"), "event"),
        ("market_ids", _row(market_id="999"), "market"),
    ],
)
def test_page_enforces_requested_scope(
    field: str,
    row: MatchbookCurrentBetObservation,
    message: str,
) -> None:
    with pytest.raises(MatchbookCurrentBetEvidenceError, match=message):
        _page(rows=(row,))


def test_full_page_requires_another_page_for_local_walk_closure() -> None:
    page = _page(rows=(_row("bet-1"), _row("bet-2")))
    traversal = MatchbookCurrentBetReportTraversal(scope=page.scope, pages=(page,))

    assert traversal.pagination_walk_closed is False
    with pytest.raises(MatchbookCurrentBetPaginationError, match="short final page"):
        traversal.require_pagination_walk_closed()


def test_short_page_closes_only_local_walk_not_authoritative_absence() -> None:
    page = _page(rows=(_row("bet-1"),))
    traversal = MatchbookCurrentBetReportTraversal(scope=page.scope, pages=(page,))

    assert traversal.pagination_walk_closed is True
    assert traversal.authoritative_absence_proven is False
    assert traversal.provider_snapshot_atomicity_proven is False
    assert traversal.current_state_finality_proven is False


def test_contiguous_two_page_walk_is_structurally_valid() -> None:
    scope = _scope()
    first = _page(
        scope=scope,
        offset=0,
        rows=(_row("bet-1"), _row("bet-2")),
        observed_at="2026-09-22T08:20:00Z",
    )
    second = _page(
        scope=scope,
        offset=2,
        rows=(_row("bet-3"),),
        observed_at="2026-09-22T08:20:01Z",
    )

    traversal = MatchbookCurrentBetReportTraversal(
        scope=scope,
        pages=(first, second),
    )

    assert traversal.pagination_walk_closed is True
    assert [row.provider_bet_id for row in traversal.positive_observations()] == [
        "bet-1",
        "bet-2",
        "bet-3",
    ]


@pytest.mark.parametrize("offset", [1, 3])
def test_walk_rejects_offset_gap_or_overlap(offset: int) -> None:
    scope = _scope()
    first = _page(
        scope=scope,
        offset=0,
        rows=(_row("bet-1"), _row("bet-2")),
    )
    second = _page(scope=scope, offset=offset, rows=(_row("bet-3"),))

    with pytest.raises(MatchbookCurrentBetPaginationError, match="gap, overlap"):
        MatchbookCurrentBetReportTraversal(scope=scope, pages=(first, second))


def test_walk_rejects_duplicate_identity_across_pages_even_if_row_is_identical() -> None:
    scope = _scope()
    first = _page(
        scope=scope,
        offset=0,
        rows=(_row("bet-1"), _row("bet-2")),
    )
    second = _page(scope=scope, offset=2, rows=(_row("bet-2"),))

    with pytest.raises(MatchbookCurrentBetPaginationError, match="repeats across"):
        MatchbookCurrentBetReportTraversal(scope=scope, pages=(first, second))


def test_walk_rejects_page_after_short_terminal_page() -> None:
    scope = _scope()
    first = _page(scope=scope, offset=0, rows=(_row("bet-1"),))
    second = _page(scope=scope, offset=2, rows=())

    with pytest.raises(MatchbookCurrentBetPaginationError, match="short terminal"):
        MatchbookCurrentBetReportTraversal(scope=scope, pages=(first, second))


def test_walk_rejects_observation_time_rollback() -> None:
    scope = _scope()
    first = _page(
        scope=scope,
        offset=0,
        rows=(_row("bet-1"), _row("bet-2")),
        observed_at="2026-09-22T08:20:02Z",
    )
    second = _page(
        scope=scope,
        offset=2,
        rows=(_row("bet-3"),),
        observed_at="2026-09-22T08:20:01Z",
    )

    with pytest.raises(MatchbookCurrentBetPaginationError, match="moves backwards"):
        MatchbookCurrentBetReportTraversal(scope=scope, pages=(first, second))


def test_walk_rejects_scope_change() -> None:
    first_scope = _scope()
    second_scope = _scope(market_ids=("999",))
    first = _page(
        scope=first_scope,
        offset=0,
        rows=(_row("bet-1"), _row("bet-2")),
    )
    second = _page(
        scope=second_scope,
        offset=2,
        rows=(_row("bet-3", market_id="999"),),
    )

    with pytest.raises(MatchbookCurrentBetPaginationError, match="scope changes"):
        MatchbookCurrentBetReportTraversal(
            scope=first_scope,
            pages=(first, second),
        )


def test_caller_built_graph_never_mints_provider_origin_or_current_state() -> None:
    page = _page(rows=(_row("fabricated"),))
    traversal = MatchbookCurrentBetReportTraversal(scope=page.scope, pages=(page,))

    assert traversal.authenticated_provider_origin_proven is False
    assert traversal.product_issued_request_semantics_proven is False
    assert traversal.settlement_truth_proven is False
    assert traversal.economic_pnl_truth_proven is False
    assert traversal.execution_authority is False
    with pytest.raises(MatchbookCurrentBetEvidenceError, match="provider origin"):
        traversal.require_authenticated_provider_origin()
    with pytest.raises(MatchbookCurrentBetEvidenceError, match="authoritative provider current state"):
        traversal.require_authoritative_current_state()


def test_require_observed_bet_is_only_in_graph_rebinding_protection() -> None:
    row = _row("bet-7")
    page = _page(rows=(row,))
    traversal = MatchbookCurrentBetReportTraversal(scope=page.scope, pages=(page,))

    assert traversal.require_observed_bet(
        provider_bet_id="bet-7",
        expected_provider_row_sha256=ROW_SHA,
    ) == row
    assert traversal.authenticated_provider_origin_proven is False

    with pytest.raises(MatchbookCurrentBetEvidenceError, match="does not match"):
        traversal.require_observed_bet(
            provider_bet_id="bet-7",
            expected_provider_row_sha256="d" * 64,
        )


def test_evidence_identity_changes_if_row_digest_changes() -> None:
    row = _row()
    changed = replace(row, provider_row_sha256="d" * 64)

    assert row.observation_id != changed.observation_id


@pytest.mark.parametrize(
    ("field", "value"),
    [("offset", True), ("per_page", True), ("offset", 1.0), ("per_page", 2.0)],
)
def test_page_numeric_controls_reject_bool_and_float_aliases(
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "scope": _scope(),
        "offset": 0,
        "per_page": 2,
        "observed_at_utc": OBSERVED,
        "request_semantics_sha256": REQ_SHA,
        "raw_response_sha256": RAW_SHA,
        "rows": (_row(),),
    }
    kwargs[field] = value

    with pytest.raises(MatchbookCurrentBetEvidenceError):
        MatchbookCurrentBetReportPage(**kwargs)  # type: ignore[arg-type]


def test_page_pagination_controls_enforce_provider_int32_domain() -> None:
    int32_max = (1 << 31) - 1
    page = _page(offset=int32_max, per_page=int32_max, rows=())

    assert page.offset == int32_max
    assert page.per_page == int32_max

    for field in ("offset", "per_page"):
        kwargs: dict[str, object] = {
            "scope": _scope(),
            "offset": 0,
            "per_page": 2,
            "observed_at_utc": OBSERVED,
            "request_semantics_sha256": REQ_SHA,
            "raw_response_sha256": RAW_SHA,
            "rows": (),
        }
        kwargs[field] = int32_max + 1
        with pytest.raises(MatchbookCurrentBetEvidenceError, match="signed int32"):
            MatchbookCurrentBetReportPage(**kwargs)  # type: ignore[arg-type]


def test_scope_rejects_duplicate_filter_ids() -> None:
    with pytest.raises(MatchbookCurrentBetEvidenceError, match="duplicates"):
        _scope(event_ids=("101", "101"))


def test_traversal_payload_keeps_all_stronger_authority_flags_false() -> None:
    page = _page(rows=(_row(),))
    traversal = MatchbookCurrentBetReportTraversal(scope=page.scope, pages=(page,))

    payload = traversal.to_payload()
    for key in (
        "authenticated_provider_origin_proven",
        "product_issued_request_semantics_proven",
        "provider_snapshot_atomicity_proven",
        "authoritative_absence_proven",
        "current_state_finality_proven",
        "settlement_truth_proven",
        "economic_pnl_truth_proven",
        "execution_authority",
    ):
        assert payload[key] is False
