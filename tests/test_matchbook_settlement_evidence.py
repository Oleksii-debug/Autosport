from __future__ import annotations

import hashlib

import pytest

from autosport.matchbook_settlement_evidence import (
    MatchbookPaginationEvidenceError,
    MatchbookSettledBetObservation,
    MatchbookSettledReportPage,
    MatchbookSettledReportScope,
    MatchbookSettledReportTraversal,
    MatchbookSettledStatus,
    MatchbookSettlementEvidenceError,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _scope(**changes) -> MatchbookSettledReportScope:
    values = {
        "account_context_id": "matchbook-account:owner-ref",
        "session_generation_id": "matchbook-session-generation:7",
        "after_utc": "2026-09-20T00:00:00Z",
        "before_utc": "2026-09-21T00:00:00Z",
        "sport_ids": ("1", "2"),
        "event_ids": ("event-1", "event-2"),
        "market_ids": ("market-1", "market-2"),
    }
    values.update(changes)
    return MatchbookSettledReportScope(**values)


def _row(
    bet_id: str = "bet-1",
    *,
    status: MatchbookSettledStatus | str = MatchbookSettledStatus.WIN,
    sport_id: str = "1",
    event_id: str = "event-1",
    market_id: str = "market-1",
    settled_at: str = "2026-09-20T12:00:00Z",
    digest_label: str | None = None,
) -> MatchbookSettledBetObservation:
    return MatchbookSettledBetObservation(
        provider_bet_id=bet_id,
        provider_sport_id=sport_id,
        provider_event_id=event_id,
        provider_market_id=market_id,
        status=status,
        settled_at_utc=settled_at,
        provider_row_sha256=_sha(digest_label or bet_id),
    )


def _page(
    *,
    scope: MatchbookSettledReportScope | None = None,
    offset: int = 0,
    per_page: int = 2,
    rows: tuple[MatchbookSettledBetObservation, ...] = (),
    observed_at: str = "2026-09-20T13:00:00Z",
    request_label: str | None = None,
    response_label: str | None = None,
) -> MatchbookSettledReportPage:
    return MatchbookSettledReportPage(
        scope=scope or _scope(),
        offset=offset,
        per_page=per_page,
        observed_at_utc=observed_at,
        request_semantics_sha256=_sha(request_label or f"request-{offset}"),
        raw_response_sha256=_sha(response_label or f"response-{offset}"),
        rows=rows,
    )


def test_scope_canonicalizes_timezone_and_filter_order() -> None:
    first = _scope(
        after_utc="2026-09-20T02:00:00+02:00",
        sport_ids=("2", "1"),
        event_ids=("event-2", "event-1"),
        market_ids=("market-2", "market-1"),
    )
    second = _scope()

    assert first == second
    assert first.scope_id == second.scope_id
    assert first.after_utc == "2026-09-20T00:00:00Z"


def test_scope_rejects_duplicate_filter_identity() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="duplicates"):
        _scope(sport_ids=("1", "1"))


def test_scope_rejects_inverted_window() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="after_utc"):
        _scope(
            after_utc="2026-09-21T00:00:01Z",
            before_utc="2026-09-21T00:00:00Z",
        )


def test_scope_rejects_naive_timestamp() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="timezone"):
        _scope(after_utc="2026-09-20T00:00:00")


@pytest.mark.parametrize(
    "status",
    [
        MatchbookSettledStatus.WIN,
        MatchbookSettledStatus.LOSE,
        MatchbookSettledStatus.PUSH,
        MatchbookSettledStatus.PUSH_WIN,
        MatchbookSettledStatus.PUSH_LOSE,
    ],
)
def test_all_documented_provider_statuses_round_trip_distinctly(
    status: MatchbookSettledStatus,
) -> None:
    row = _row(status=status)

    assert row.status is status
    assert row.to_payload()["status"] == status.value


def test_provider_status_is_case_sensitive_and_not_coerced() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="documented"):
        _row(status="win")


def test_push_variants_do_not_collapse_to_one_status() -> None:
    observations = {
        _row(f"bet-{status.value}", status=status).status
        for status in (
            MatchbookSettledStatus.PUSH,
            MatchbookSettledStatus.PUSH_WIN,
            MatchbookSettledStatus.PUSH_LOSE,
        )
    }

    assert observations == {
        MatchbookSettledStatus.PUSH,
        MatchbookSettledStatus.PUSH_WIN,
        MatchbookSettledStatus.PUSH_LOSE,
    }


def test_page_rejects_future_settlement_relative_to_observation() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="future evidence"):
        _page(
            rows=(_row(settled_at="2026-09-20T14:00:00Z"),),
            observed_at="2026-09-20T13:00:00Z",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sport_id", "99", "sport"),
        ("event_id", "other-event", "event"),
        ("market_id", "other-market", "market"),
    ],
)
def test_page_rejects_row_outside_requested_filter_scope(
    field: str,
    value: str,
    message: str,
) -> None:
    kwargs = {field: value}
    with pytest.raises(MatchbookSettlementEvidenceError, match=message):
        _page(rows=(_row(**kwargs),))


def test_page_rejects_row_outside_requested_time_window() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="not strictly after"):
        _page(rows=(_row(settled_at="2026-09-19T23:59:59Z"),))


def test_page_rejects_row_equal_to_strict_after_boundary() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="not strictly after"):
        _page(rows=(_row(settled_at="2026-09-20T00:00:00Z"),))


def test_page_rejects_row_equal_to_strict_before_boundary() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="not strictly before"):
        _page(
            rows=(_row(settled_at="2026-09-21T00:00:00Z"),),
            observed_at="2026-09-21T00:00:01Z",
        )


def test_page_accepts_row_strictly_inside_requested_time_window() -> None:
    page = _page(rows=(_row(settled_at="2026-09-20T12:00:00Z"),))
    assert page.rows[0].settled_at_utc == "2026-09-20T12:00:00Z"


def test_page_rejects_duplicate_provider_bet_identity() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="duplicated"):
        _page(
            per_page=3,
            rows=(
                _row("same", digest_label="row-a"),
                _row("same", digest_label="row-b"),
            ),
        )


def test_page_rejects_more_rows_than_per_page() -> None:
    with pytest.raises(MatchbookSettlementEvidenceError, match="more rows"):
        _page(
            per_page=1,
            rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
        )


def test_page_digest_binds_exact_request_and_raw_response() -> None:
    row = _row()
    first = _page(rows=(row,), request_label="request-a", response_label="response-a")
    second = _page(rows=(row,), request_label="request-b", response_label="response-a")
    third = _page(rows=(row,), request_label="request-a", response_label="response-b")

    assert first.page_evidence_id != second.page_evidence_id
    assert first.page_evidence_id != third.page_evidence_id


def test_traversal_requires_zero_initial_offset_and_no_gaps() -> None:
    scope = _scope()
    with pytest.raises(MatchbookPaginationEvidenceError, match="gap"):
        MatchbookSettledReportTraversal(
            scope=scope,
            pages=(
                _page(
                    scope=scope,
                    offset=2,
                    rows=(_row("bet-3", event_id="event-2"),),
                ),
            ),
        )

    with pytest.raises(MatchbookPaginationEvidenceError, match="gap"):
        MatchbookSettledReportTraversal(
            scope=scope,
            pages=(
                _page(
                    scope=scope,
                    offset=0,
                    rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
                ),
                _page(
                    scope=scope,
                    offset=3,
                    rows=(),
                    observed_at="2026-09-20T13:01:00Z",
                ),
            ),
        )


def test_traversal_rejects_per_page_change() -> None:
    scope = _scope()
    with pytest.raises(MatchbookPaginationEvidenceError, match="per_page"):
        MatchbookSettledReportTraversal(
            scope=scope,
            pages=(
                _page(
                    scope=scope,
                    offset=0,
                    per_page=2,
                    rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
                ),
                _page(
                    scope=scope,
                    offset=2,
                    per_page=3,
                    rows=(),
                    observed_at="2026-09-20T13:01:00Z",
                ),
            ),
        )


def test_traversal_rejects_scope_drift() -> None:
    first_scope = _scope()
    second_scope = _scope(session_generation_id="matchbook-session-generation:8")

    with pytest.raises(MatchbookPaginationEvidenceError, match="scope differs"):
        MatchbookSettledReportTraversal(
            scope=first_scope,
            pages=(
                _page(scope=second_scope, rows=()),
            ),
        )


def test_traversal_rejects_duplicate_bet_across_pages_as_unstable_snapshot() -> None:
    scope = _scope()
    with pytest.raises(MatchbookPaginationEvidenceError, match="repeats"):
        MatchbookSettledReportTraversal(
            scope=scope,
            pages=(
                _page(
                    scope=scope,
                    offset=0,
                    rows=(_row("same"), _row("bet-2", event_id="event-2")),
                ),
                _page(
                    scope=scope,
                    offset=2,
                    rows=(_row("same"),),
                    observed_at="2026-09-20T13:01:00Z",
                ),
            ),
        )


def test_traversal_rejects_page_observation_time_rollback() -> None:
    scope = _scope()
    with pytest.raises(MatchbookPaginationEvidenceError, match="moves backwards"):
        MatchbookSettledReportTraversal(
            scope=scope,
            pages=(
                _page(
                    scope=scope,
                    offset=0,
                    rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
                    observed_at="2026-09-20T13:02:00Z",
                ),
                _page(
                    scope=scope,
                    offset=2,
                    rows=(),
                    observed_at="2026-09-20T13:01:00Z",
                ),
            ),
        )


def test_short_page_must_be_terminal() -> None:
    scope = _scope()
    with pytest.raises(MatchbookPaginationEvidenceError, match="short terminal"):
        MatchbookSettledReportTraversal(
            scope=scope,
            pages=(
                _page(scope=scope, offset=0, rows=(_row("bet-1"),)),
                _page(
                    scope=scope,
                    offset=2,
                    rows=(),
                    observed_at="2026-09-20T13:01:00Z",
                ),
            ),
        )


def test_short_final_page_proves_only_pagination_exhaustion() -> None:
    scope = _scope()
    traversal = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(
            _page(
                scope=scope,
                offset=0,
                rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
            ),
            _page(
                scope=scope,
                offset=2,
                rows=(_row("bet-3", market_id="market-2"),),
                observed_at="2026-09-20T13:01:00Z",
            ),
        ),
    )

    traversal.require_pagination_exhausted()
    assert traversal.pagination_exhausted is True
    assert traversal.authenticated_provider_origin_proven is False
    assert traversal.product_issued_request_semantics_proven is False
    assert all(
        row.authenticated_provider_origin_proven is False
        for row in traversal.positive_observations()
    )
    assert all(
        page.authenticated_provider_origin_proven is False
        and page.product_issued_request_semantics_proven is False
        for page in traversal.pages
    )
    assert traversal.provider_snapshot_atomicity_proven is False
    assert traversal.authoritative_absence_proven is False
    assert traversal.commission_truth_proven is False
    assert traversal.net_pnl_truth_proven is False
    assert [row.provider_bet_id for row in traversal.positive_observations()] == [
        "bet-1",
        "bet-2",
        "bet-3",
    ]


def test_full_final_page_is_not_pagination_exhaustion() -> None:
    scope = _scope()
    traversal = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(
            _page(
                scope=scope,
                offset=0,
                rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
            ),
        ),
    )

    assert traversal.pagination_exhausted is False
    with pytest.raises(MatchbookPaginationEvidenceError, match="not exhausted"):
        traversal.require_pagination_exhausted()


def test_empty_page_can_close_traversal_after_full_page() -> None:
    scope = _scope()
    traversal = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(
            _page(
                scope=scope,
                offset=0,
                rows=(_row("bet-1"), _row("bet-2", event_id="event-2")),
            ),
            _page(
                scope=scope,
                offset=2,
                rows=(),
                observed_at="2026-09-20T13:01:00Z",
            ),
        ),
    )

    assert traversal.pagination_exhausted is True
    assert len(traversal.positive_observations()) == 2


def test_traversal_evidence_id_is_deterministic_for_same_exact_evidence() -> None:
    scope = _scope()
    pages = (
        _page(
            scope=scope,
            offset=0,
            rows=(_row("bet-1"),),
        ),
    )

    first = MatchbookSettledReportTraversal(scope=scope, pages=pages)
    second = MatchbookSettledReportTraversal(scope=scope, pages=pages)

    assert first.traversal_evidence_id == second.traversal_evidence_id


def test_consumer_re_resolves_exact_positive_bet_row_identity() -> None:
    scope = _scope()
    row = _row("bet-verified", digest_label="exact-row")
    traversal = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(_page(scope=scope, rows=(row,)),),
    )

    resolved = traversal.require_observed_bet(
        provider_bet_id="bet-verified",
        expected_provider_row_sha256=_sha("exact-row"),
    )

    assert resolved is row


def test_consumer_cannot_rebind_or_invent_settled_bet_row() -> None:
    scope = _scope()
    row = _row("bet-verified", digest_label="exact-row")
    traversal = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(_page(scope=scope, rows=(row,)),),
    )

    with pytest.raises(MatchbookSettlementEvidenceError, match="does not match"):
        traversal.require_observed_bet(
            provider_bet_id="bet-verified",
            expected_provider_row_sha256=_sha("different-row"),
        )

    with pytest.raises(MatchbookSettlementEvidenceError, match="absent"):
        traversal.require_observed_bet(
            provider_bet_id="bet-invented",
            expected_provider_row_sha256=_sha("exact-row"),
        )


def test_detached_traversal_cannot_authorize_authenticated_provider_origin() -> None:
    scope = _scope()
    traversal = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(
            _page(
                scope=scope,
                rows=(_row("caller-minted", digest_label="caller-row"),),
                request_label="caller-request",
                response_label="caller-response",
            ),
        ),
    )

    assert traversal.authenticated_provider_origin_proven is False
    assert traversal.product_issued_request_semantics_proven is False
    with pytest.raises(
        MatchbookSettlementEvidenceError,
        match="does not prove authenticated provider origin",
    ):
        traversal.require_authenticated_provider_origin()


def test_caller_supplied_digests_do_not_change_origin_truth_or_evidence_id_flags() -> None:
    scope = _scope()
    first = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(
            _page(
                scope=scope,
                rows=(_row("bet-a", digest_label="row-a"),),
                request_label="request-a",
                response_label="response-a",
            ),
        ),
    )
    second = MatchbookSettledReportTraversal(
        scope=scope,
        pages=(
            _page(
                scope=scope,
                rows=(_row("bet-a", digest_label="row-a"),),
                request_label="request-b",
                response_label="response-b",
            ),
        ),
    )

    assert first.traversal_evidence_id != second.traversal_evidence_id
    for traversal in (first, second):
        payload = traversal.to_payload()
        assert payload["authenticated_provider_origin_proven"] is False
        assert payload["product_issued_request_semantics_proven"] is False
        with pytest.raises(
            MatchbookSettlementEvidenceError,
            match="does not prove authenticated provider origin",
        ):
            traversal.require_authenticated_provider_origin()
