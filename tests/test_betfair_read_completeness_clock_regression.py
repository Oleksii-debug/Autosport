from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.betfair_read_completeness import (
    BetfairObservationCompleteness,
    BetfairReadCompletenessObserver,
    BetfairReadCompletenessWitness,
)


NOW = datetime(2026, 9, 21, 20, 0, tzinfo=timezone.utc)


class OneResponseTransport:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._used = False

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        if self._used:
            raise AssertionError("unexpected second provider call")
        self._used = True
        return self._payload


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def _empty_current_orders_response() -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {"currentOrders": [], "moreAvailable": False},
            "id": 1,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _complete_witness(*, started_at: datetime, finished_at: datetime) -> BetfairReadCompletenessWitness:
    return BetfairReadCompletenessWitness(
        operation="listCurrentOrders",
        completeness=BetfairObservationCompleteness.COMPLETE_FOR_DECLARED_QUERY_WINDOW,
        query_sha256="a" * 64,
        attempt_id="b" * 64,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        pages=((0, 1000, False, "c" * 64),),
        rows_observed=0,
    )


def test_complete_witness_rejects_finished_before_started() -> None:
    with pytest.raises(BetfairReadOnlyError, match="finished_at"):
        _complete_witness(
            started_at=NOW,
            finished_at=NOW - timedelta(microseconds=1),
        )


def test_observer_clock_regression_cannot_issue_authoritative_complete_witness() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=OneResponseTransport(_empty_current_orders_response()),
        clock=lambda: NOW,
        venue_id="betfair",
        account_id="acct-1",
    )
    observer = BetfairReadCompletenessObserver(
        client,
        clock=SequenceClock(NOW, NOW - timedelta(seconds=1)),
    )

    with pytest.raises(BetfairReadOnlyError, match="finished_at"):
        observer.read_current_orders(page_size=10)


def test_zero_duration_complete_interval_remains_structurally_valid() -> None:
    witness = _complete_witness(started_at=NOW, finished_at=NOW)

    assert witness.started_at == witness.finished_at
    assert witness.authoritative is False
