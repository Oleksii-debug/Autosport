from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import gc
import json
from pathlib import Path
import sqlite3

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)
from autosport.bookmaker_account_acquisition import (
    BookmakerAccountAcquisitionError,
    BookmakerAccountAcquisitionStore,
    assert_bookmaker_account_acquisition_authoritative,
)
from autosport.bookmaker_capability import BookmakerCapability
from autosport.campaign_provider_scope_authority import CampaignProviderScopeError
from autosport.bookmaker_integration_boundary import BookmakerIntegrationKind


class TransportHarness:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = self

        def post(
            transport: UrllibBetfairHttpTransport,
            url: str,
            *,
            headers,
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            assert type(transport) is UrllibBetfairHttpTransport
            harness.calls.append(
                {
                    "url": url,
                    "headers": dict(headers),
                    "body": body,
                    "timeout_seconds": timeout_seconds,
                }
            )
            if not harness.responses:
                raise AssertionError("unexpected canonical transport call")
            return harness.responses.pop(0)

        monkeypatch.setattr(UrllibBetfairHttpTransport, "post", post)


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def account_responses(balance: float = 100.10) -> tuple[bytes, bytes, bytes]:
    return (
        response(
            [
                {
                    "appId": 12345,
                    "appVersions": [
                        {
                            "versionId": 67890,
                            "ownerManaged": False,
                            "applicationKey": "provider-returned-app-key",
                            "version": "1.0",
                        }
                    ],
                }
            ],
            1,
        ),
        response(
            {
                "currencyCode": "GBP",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            },
            2,
        ),
        response(
            {
                "availableToBetBalance": balance,
                "exposure": -12.34,
                "retainedCommission": 0.05,
                "exposureLimit": -5000.00,
            },
            3,
        ),
    )


def credentials() -> BetfairSessionCredentials:
    return BetfairSessionCredentials("app-secret", "session-secret")


def balance_caps() -> frozenset[BookmakerCapability]:
    return frozenset({BookmakerCapability.BALANCE_READ})


def acquire_balance(
    store: BookmakerAccountAcquisitionStore,
    *,
    acquisition_id: str,
    account_id: str = "account-a",
):
    return store.acquire_betfair(
        credentials(),
        balance_caps(),
        acquisition_id=acquisition_id,
        account_id=account_id,
    )


def test_live_product_acquisition_is_authoritative_but_durable_receipt_is_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store_path = tmp_path / "account-acquisition.sqlite3"
    store = BookmakerAccountAcquisitionStore(store_path)

    acquired = acquire_balance(store, acquisition_id="attempt-1")

    assert acquired.source_authority_proven is True
    assert acquired.receipt.source_authority_proven is False
    assert acquired.allocation_authority_proven is False
    assert acquired.atomicity_proven is False
    assert acquired.receipt.allocation_authority_proven is False
    assert acquired.receipt.atomicity_proven is False
    assert acquired.receipt.execution_authorized is False
    assert acquired.receipt.real_money_execution is False
    assert acquired.receipt.integration_kind is BookmakerIntegrationKind.OFFICIAL_API
    assert len(acquired.receipt.authenticated_account_identity_sha256) == 64
    assert acquired.receipt.account_identity_observed_at
    assert acquired.receipt.provider_native_observed_at is None
    assert acquired.snapshot.balance is not None
    assert acquired.snapshot.balance.available_balance == Decimal("100.1")
    assert len(harness.calls) == 3
    assert store.count() == 1
    assert_bookmaker_account_acquisition_authoritative(acquired)

    reopened = BookmakerAccountAcquisitionStore(store_path)
    resolved = reopened.resolve(acquired.receipt.receipt_id)

    assert resolved.receipt == acquired.receipt
    assert resolved.snapshot == acquired.snapshot
    assert resolved.source_authority_proven is False
    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="not issued by live canonical provider acquisition",
    ):
        assert_bookmaker_account_acquisition_authoritative(resolved)


def test_same_live_acquisition_id_returns_ephemeral_issued_object_without_more_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    first = acquire_balance(store, acquisition_id="attempt-1")
    calls_after_first = len(harness.calls)
    retry = acquire_balance(store, acquisition_id="attempt-1")

    assert retry is first
    assert retry.source_authority_proven is True
    assert len(harness.calls) == calls_after_first == 3
    assert store.count() == 1


def test_lost_ephemeral_origin_requires_new_acquisition_id_to_reacquire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    acquired = acquire_balance(store, acquisition_id="attempt-1")
    receipt_id = acquired.receipt.receipt_id
    del acquired
    gc.collect()

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="cannot reissue provider-origin authority",
    ):
        acquire_balance(store, acquisition_id="attempt-1")
    assert len(harness.calls) == 3

    durable = store.resolve(receipt_id)
    assert durable.source_authority_proven is False


def test_new_acquisition_id_keeps_identical_provider_bytes_as_new_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(
        [*account_responses(), *account_responses()]
    )
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    first = acquire_balance(store, acquisition_id="attempt-1")
    second = acquire_balance(store, acquisition_id="attempt-2")

    assert first.receipt.provider_response_sha256 == second.receipt.provider_response_sha256
    assert first.receipt.receipt_id != second.receipt.receipt_id
    assert first.receipt.observation_key != second.receipt.observation_key
    assert first.receipt.acquisition_id == "attempt-1"
    assert second.receipt.acquisition_id == "attempt-2"
    assert len(harness.calls) == 6
    assert store.count() == 2


def test_caller_constructed_snapshot_cannot_reuse_durable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    acquired = acquire_balance(store, acquisition_id="attempt-1")
    assert acquired.snapshot.balance is not None

    forged_balance = replace(
        acquired.snapshot.balance,
        available_balance=Decimal("999999"),
    )
    forged_snapshot = replace(acquired.snapshot, balance=forged_balance)

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="snapshot payload does not match acquisition receipt",
    ):
        acquired.receipt.verify_snapshot(forged_snapshot)

    assert not hasattr(store, "persist_snapshot")
    assert not hasattr(store, "authorize_snapshot")


def test_receipt_from_one_account_cannot_authorize_another_account_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(
        [*account_responses(), *account_responses()]
    )
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    acquired_a = acquire_balance(
        store,
        acquisition_id="account-a-attempt",
        account_id="account-a",
    )
    acquired_b = acquire_balance(
        store,
        acquisition_id="account-b-attempt",
        account_id="account-b",
    )

    assert acquired_a.receipt.receipt_id != acquired_b.receipt.receipt_id
    with pytest.raises(BookmakerAccountAcquisitionError):
        acquired_a.receipt.verify_snapshot(acquired_b.snapshot)


def test_durable_snapshot_byte_tamper_fails_closed_after_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store_path = tmp_path / "account.sqlite3"
    store = BookmakerAccountAcquisitionStore(store_path)
    acquired = acquire_balance(store, acquisition_id="attempt-1")

    connection = sqlite3.connect(store_path)
    try:
        row = connection.execute(
            """
            SELECT record_json
            FROM bookmaker_account_acquisition
            WHERE receipt_id = ?
            """,
            (acquired.receipt.receipt_id,),
        ).fetchone()
        assert row is not None
        tampered = row[0].replace(
            '"available_balance":"100.1"',
            '"available_balance":"999999"',
            1,
        )
        assert tampered != row[0]
        connection.execute(
            """
            UPDATE bookmaker_account_acquisition
            SET record_json = ?
            WHERE receipt_id = ?
            """,
            (tampered, acquired.receipt.receipt_id),
        )
        connection.commit()
    finally:
        connection.close()

    reopened = BookmakerAccountAcquisitionStore(store_path)
    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="record hash mismatch",
    ):
        reopened.resolve(acquired.receipt.receipt_id)


def test_credentials_never_persist_in_receipt_or_snapshot_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store_path = tmp_path / "account.sqlite3"
    store = BookmakerAccountAcquisitionStore(store_path)

    acquired = acquire_balance(store, acquisition_id="attempt-1")
    durable_bytes = store_path.read_bytes()

    assert b"app-secret" not in durable_bytes
    assert b"session-secret" not in durable_bytes
    assert b"provider-returned-app-key" not in durable_bytes
    assert "app-secret" not in repr(acquired)
    assert "session-secret" not in repr(acquired)


def test_provider_read_failure_cannot_mint_positive_acquisition_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness([b"not-json"])
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    with pytest.raises(CampaignProviderScopeError, match="not valid JSON"):
        acquire_balance(store, acquisition_id="failed-attempt")

    assert len(harness.calls) == 1
    assert store.count() == 0


def test_authority_entrypoint_rejects_caller_supplied_client_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    client = BetfairReadOnlyClient(credentials())
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="exact BetfairSessionCredentials",
    ):
        store.acquire_betfair(  # type: ignore[arg-type]
            client,
            balance_caps(),
            acquisition_id="attempt-1",
        )

    assert harness.calls == []
    assert store.count() == 0


def test_acquisition_id_reuse_with_different_scope_or_capabilities_fails_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    first = acquire_balance(
        store,
        acquisition_id="stable-attempt",
        account_id="account-a",
    )
    assert first.receipt.account_id == "account-a"
    calls_after_first = len(harness.calls)

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="another provider/account/adapter scope",
    ):
        acquire_balance(
            store,
            acquisition_id="stable-attempt",
            account_id="account-b",
        )

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="another capability request",
    ):
        store.acquire_betfair(
            credentials(),
            frozenset(
                {
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapability.OPEN_POSITIONS_READ,
                }
            ),
            acquisition_id="stable-attempt",
            account_id="account-a",
        )

    assert len(harness.calls) == calls_after_first == 3
    assert store.count() == 1


def test_untyped_snapshot_capability_is_rejected_before_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="not typed account-snapshot acquisition evidence",
    ):
        store.acquire_betfair(
            credentials(),
            frozenset({BookmakerCapability.BET_READBACK}),
            acquisition_id="attempt-1",
        )

    assert harness.calls == []
    assert store.count() == 0


def test_empty_or_non_exact_capability_container_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = TransportHarness(list(account_responses()))
    harness.install(monkeypatch)
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    with pytest.raises(BookmakerAccountAcquisitionError):
        store.acquire_betfair(
            credentials(),
            frozenset(),
            acquisition_id="attempt-empty",
        )

    with pytest.raises(BookmakerAccountAcquisitionError):
        store.acquire_betfair(  # type: ignore[arg-type]
            credentials(),
            {BookmakerCapability.BALANCE_READ},
            acquisition_id="attempt-container",
        )

    assert harness.calls == []
    assert store.count() == 0
