from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import autosport.betfair_account_readonly as betfair_readonly
from autosport.account_snapshot_acquisition import BetfairAccountSnapshotAcquirer
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.bookmaker_capability import BookmakerCapability


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


_DETAILS = _response(
    {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
    },
    1,
)
_FUNDS = _response(
    {
        "availableToBetBalance": 100.10,
        "exposure": -12.34,
        "retainedCommission": 0.05,
        "exposureLimit": -5000.00,
    },
    2,
)


def _credentials() -> BetfairSessionCredentials:
    return BetfairSessionCredentials(
        "APP-SECRET-SENTINEL",
        "SESSION-SECRET-SENTINEL",
    )


def _balance_capabilities() -> frozenset[BookmakerCapability]:
    return frozenset({BookmakerCapability.BALANCE_READ})


def _install_transport(monkeypatch, responses: list[bytes]):
    queue = list(responses)
    calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not queue:
            raise AssertionError("unexpected provider I/O")
        return queue.pop(0)

    monkeypatch.setattr(
        betfair_readonly.UrllibBetfairHttpTransport,
        "post",
        post,
    )
    return calls


class _ControlledDateTime(datetime):
    current = datetime(2026, 9, 21, 18, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if tz is None:
            return value.replace(tzinfo=None)
        return value.astimezone(tz)


def test_same_acquisition_id_is_no_io_retry_but_new_id_preserves_identical_later_read(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    monkeypatch.setattr(betfair_readonly, "datetime", _ControlledDateTime)

    first_calls = _install_transport(monkeypatch, [_DETAILS, _FUNDS])
    first = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
    ).acquire(
        _balance_capabilities(),
        acquisition_id="read-attempt-1",
    )
    assert len(first_calls) == 2
    assert first.receipt.acquired_at == "2026-09-21T18:00:00+00:00"

    # Exact retry of the same product read identity must resolve before provider I/O.
    _ControlledDateTime.current = datetime(
        2026, 9, 21, 18, 1, 0, tzinfo=timezone.utc
    )
    retry_calls = _install_transport(monkeypatch, [])
    retry = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
    ).acquire(
        _balance_capabilities(),
        acquisition_id="read-attempt-1",
    )
    assert retry_calls == []
    assert retry.receipt == first.receipt
    assert retry.snapshot == first.snapshot

    # A genuinely new acquisition identity represents a new product-owned provider
    # read. Byte-identical content must not collapse the later observation back onto
    # the stale first receipt.
    second_calls = _install_transport(monkeypatch, [_DETAILS, _FUNDS])
    second = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
    ).acquire(
        _balance_capabilities(),
        acquisition_id="read-attempt-2",
    )
    assert len(second_calls) == 2
    assert second.receipt.acquisition_id != first.receipt.acquisition_id
    assert second.receipt.snapshot_content_sha256 == first.receipt.snapshot_content_sha256
    assert second.receipt.source_payload_sha256 == first.receipt.source_payload_sha256
    assert second.receipt.acquired_at == "2026-09-21T18:01:00+00:00"
    assert second.receipt.acquired_at != first.receipt.acquired_at

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM account_snapshot_acquisitions"
        ).fetchone()[0]
    assert count == 2
