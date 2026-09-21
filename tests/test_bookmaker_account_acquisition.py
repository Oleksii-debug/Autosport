from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.bookmaker_account_acquisition import (
    BookmakerAccountAcquisitionError,
    BookmakerAccountAcquisitionStore,
)
from autosport.bookmaker_capability import BookmakerCapability
from autosport.bookmaker_integration_boundary import BookmakerIntegrationKind


FIXED_NOW = datetime(2026, 9, 21, 18, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def account_responses(balance: float = 100.10) -> tuple[bytes, bytes]:
    return (
        response(
            {
                "currencyCode": "GBP",
                "localeCode": "en",
                "region": "GBR",
                "timezone": "Europe/London",
            },
            1,
        ),
        response(
            {
                "availableToBetBalance": balance,
                "exposure": -12.34,
                "retainedCommission": 0.05,
                "exposureLimit": -5000.00,
            },
            2,
        ),
    )


def client_for(
    *responses: bytes,
    clock: datetime = FIXED_NOW,
    account_id: str = "account-a",
) -> tuple[BetfairReadOnlyClient, FakeTransport]:
    transport = FakeTransport(list(responses))
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: clock,
        account_id=account_id,
    )
    return client, transport


def balance_caps() -> frozenset[BookmakerCapability]:
    return frozenset({BookmakerCapability.BALANCE_READ})


def test_product_owned_acquisition_is_durable_and_does_not_widen_authority(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "account-acquisition.sqlite3"
    client, _ = client_for(*account_responses())
    store = BookmakerAccountAcquisitionStore(store_path)

    acquired = store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")

    assert acquired.source_authority_proven is True
    assert acquired.allocation_authority_proven is False
    assert acquired.atomicity_proven is False
    assert acquired.receipt.source_authority_proven is True
    assert acquired.receipt.allocation_authority_proven is False
    assert acquired.receipt.atomicity_proven is False
    assert acquired.receipt.execution_authorized is False
    assert acquired.receipt.real_money_execution is False
    assert acquired.receipt.integration_kind is BookmakerIntegrationKind.OFFICIAL_API
    assert acquired.receipt.provider_native_observed_at is None
    assert acquired.snapshot.balance is not None
    assert acquired.snapshot.balance.available_balance == Decimal("100.1")
    assert store.count() == 1

    reopened = BookmakerAccountAcquisitionStore(store_path)
    resolved = reopened.resolve(acquired.receipt.receipt_id)

    assert resolved.receipt == acquired.receipt
    assert resolved.snapshot == acquired.snapshot
    assert resolved.snapshot.balance == acquired.snapshot.balance


def test_same_acquisition_id_resolves_durable_receipt_before_provider_io(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    first_client, _ = client_for(
        *account_responses(),
        clock=FIXED_NOW,
    )
    retry_client, retry_transport = client_for(
        *account_responses(),
        clock=FIXED_NOW + timedelta(minutes=5),
    )

    first = store.acquire_betfair(
        first_client,
        balance_caps(),
        acquisition_id="attempt-1",
    )
    retry = store.acquire_betfair(
        retry_client,
        balance_caps(),
        acquisition_id="attempt-1",
    )

    assert retry.receipt.receipt_id == first.receipt.receipt_id
    assert retry.receipt.observation_key == first.receipt.observation_key
    assert retry.snapshot.observed_at == first.snapshot.observed_at
    assert retry_transport.calls == []
    assert store.count() == 1


def test_new_acquisition_id_preserves_new_temporal_observation_even_for_same_bytes(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    first_client, _ = client_for(*account_responses(), clock=FIXED_NOW)
    later_client, later_transport = client_for(
        *account_responses(),
        clock=FIXED_NOW + timedelta(minutes=5),
    )

    first = store.acquire_betfair(
        first_client,
        balance_caps(),
        acquisition_id="attempt-1",
    )
    later = store.acquire_betfair(
        later_client,
        balance_caps(),
        acquisition_id="attempt-2",
    )

    assert later.receipt.receipt_id != first.receipt.receipt_id
    assert later.receipt.observation_key != first.receipt.observation_key
    assert later.snapshot.observed_at == (
        FIXED_NOW + timedelta(minutes=5)
    ).isoformat()
    assert later.snapshot.balance is not None
    assert first.snapshot.balance is not None
    assert later.snapshot.balance.available_balance == first.snapshot.balance.available_balance
    assert len(later_transport.calls) == 2
    assert store.count() == 2


def test_caller_constructed_snapshot_cannot_reuse_durable_receipt(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    client, _ = client_for(*account_responses())
    acquired = store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")
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
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    client_a, _ = client_for(*account_responses(), account_id="account-a")
    client_b, _ = client_for(*account_responses(), account_id="account-b")

    acquired_a = store.acquire_betfair(
        client_a,
        balance_caps(),
        acquisition_id="account-a-attempt",
    )
    acquired_b = store.acquire_betfair(
        client_b,
        balance_caps(),
        acquisition_id="account-b-attempt",
    )

    assert acquired_a.receipt.receipt_id != acquired_b.receipt.receipt_id
    with pytest.raises(BookmakerAccountAcquisitionError):
        acquired_a.receipt.verify_snapshot(acquired_b.snapshot)


def test_durable_snapshot_byte_tamper_fails_closed_after_restart(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "account.sqlite3"
    store = BookmakerAccountAcquisitionStore(store_path)
    client, _ = client_for(*account_responses())
    acquired = store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")

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


def test_credentials_are_never_persisted_in_receipt_or_snapshot_store(
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "account.sqlite3"
    store = BookmakerAccountAcquisitionStore(store_path)
    client, _ = client_for(*account_responses())

    acquired = store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")
    durable_bytes = store_path.read_bytes()

    assert b"app-secret" not in durable_bytes
    assert b"session-secret" not in durable_bytes
    assert "app-secret" not in repr(acquired)
    assert "session-secret" not in repr(acquired)


def test_provider_read_failure_cannot_mint_positive_acquisition_receipt(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    client, _ = client_for(b"not-json")

    with pytest.raises(BetfairReadOnlyError):
        store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")

    assert store.count() == 0


def test_instance_method_shadow_cannot_mint_product_acquisition_authority(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    client, transport = client_for(*account_responses())
    client.read_account_snapshot = lambda _caps: object()  # type: ignore[method-assign]

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="shadowed on the client instance",
    ):
        store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")

    assert transport.calls == []
    assert store.count() == 0


def test_client_subclass_cannot_mint_product_acquisition_authority(
    tmp_path: Path,
) -> None:
    class CallerClient(BetfairReadOnlyClient):
        pass

    transport = FakeTransport(list(account_responses()))
    client = CallerClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=lambda: FIXED_NOW,
    )
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="exact canonical BetfairReadOnlyClient",
    ):
        store.acquire_betfair(client, balance_caps(), acquisition_id="attempt-1")

    assert transport.calls == []
    assert store.count() == 0


def test_acquisition_id_reuse_with_different_scope_or_capabilities_fails_before_io(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    first_client, _ = client_for(*account_responses(), account_id="account-a")
    first = store.acquire_betfair(
        first_client,
        balance_caps(),
        acquisition_id="stable-attempt",
    )
    assert first.receipt.account_id == "account-a"

    wrong_account, wrong_account_transport = client_for(
        *account_responses(),
        account_id="account-b",
    )
    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="another provider/account/adapter scope",
    ):
        store.acquire_betfair(
            wrong_account,
            balance_caps(),
            acquisition_id="stable-attempt",
        )
    assert wrong_account_transport.calls == []

    wrong_caps, wrong_caps_transport = client_for(*account_responses())
    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="another capability request",
    ):
        store.acquire_betfair(
            wrong_caps,
            frozenset(
                {
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapability.OPEN_POSITIONS_READ,
                }
            ),
            acquisition_id="stable-attempt",
        )
    assert wrong_caps_transport.calls == []
    assert store.count() == 1


def test_untyped_snapshot_capability_is_rejected_before_provider_io(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    client, transport = client_for(*account_responses())

    with pytest.raises(
        BookmakerAccountAcquisitionError,
        match="not typed account-snapshot acquisition evidence",
    ):
        store.acquire_betfair(
            client,
            frozenset({BookmakerCapability.BET_READBACK}),
            acquisition_id="attempt-1",
        )

    assert transport.calls == []
    assert store.count() == 0


def test_empty_or_non_exact_capability_container_is_rejected(
    tmp_path: Path,
) -> None:
    store = BookmakerAccountAcquisitionStore(tmp_path / "account.sqlite3")
    client, transport = client_for(*account_responses())

    with pytest.raises(BookmakerAccountAcquisitionError):
        store.acquire_betfair(
            client,
            frozenset(),
            acquisition_id="attempt-empty",
        )

    with pytest.raises(BookmakerAccountAcquisitionError):
        store.acquire_betfair(  # type: ignore[arg-type]
            client,
            {BookmakerCapability.BALANCE_READ},
            acquisition_id="attempt-container",
        )

    assert transport.calls == []
    assert store.count() == 0
