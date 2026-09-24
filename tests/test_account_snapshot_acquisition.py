from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3

import pytest

import autosport.account_snapshot_acquisition as acquisition_module
import autosport.betfair_account_readonly as betfair_readonly
from autosport.account_snapshot_acquisition import (
    AccountSnapshotAcquisitionError,
    BetfairAccountSnapshotAcquirer,
    assert_account_snapshot_acquisition_authoritative,
)
from autosport.betfair_account_readonly import (
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.bookmaker_capability import BookmakerCapability


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


_DEVELOPER_APPS = _response(
    [
        {
            "appId": 12345,
            "appVersions": [
                {
                    "versionId": 67890,
                    "version": "1.0",
                    "applicationKey": "DEVAPP-SECRET-SENTINEL",
                    "ownerManaged": False,
                }
            ],
        }
    ],
    1,
)
_DETAILS = _response(
    {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
    },
    2,
)
_FUNDS = _response(
    {
        "availableToBetBalance": 100.10,
        "exposure": -12.34,
        "retainedCommission": 0.05,
        "exposureLimit": -5000.00,
    },
    3,
)


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
            raise AssertionError("unexpected provider call")
        return queue.pop(0)

    monkeypatch.setattr(
        betfair_readonly.UrllibBetfairHttpTransport,
        "post",
        post,
    )
    return calls


def _credentials() -> BetfairSessionCredentials:
    return BetfairSessionCredentials(
        "APP-SECRET-SENTINEL",
        "SESSION-SECRET-SENTINEL",
    )


def _balance_capabilities() -> frozenset[BookmakerCapability]:
    return frozenset({BookmakerCapability.BALANCE_READ})


class _ControlledDateTime(datetime):
    current = datetime(2026, 9, 21, 18, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        if tz is None:
            return value.replace(tzinfo=None)
        return value.astimezone(tz)


def test_raw_acquisition_minting_seams_are_not_exposed() -> None:
    assert not hasattr(BetfairAccountSnapshotAcquirer, "_read_provider_snapshot")
    assert not hasattr(acquisition_module._AccountSnapshotStore, "record")
    assert not hasattr(
        acquisition_module,
        "_install_account_snapshot_acquisition_authority",
    )


def test_caller_cannot_swap_canonical_client_or_store_after_initialization(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
    )

    assert not hasattr(acquirer, "_client")
    assert not hasattr(acquirer, "_store")
    acquirer._client = object()
    acquirer._store = object()

    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="canonical-hidden-state",
    )
    assert len(calls) == 3
    assert acquired.receipt.source_authority_proven is False
    assert acquired.source_authority_proven is True
    acquirer.verify(acquired.snapshot, acquired.receipt)
    assert (
        acquirer.resolve(acquired.receipt.acquisition_id).receipt
        == acquired.receipt
    )


def test_class_level_snapshot_reader_rebinding_cannot_mint_authority(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
    )

    def shadowed_read(self, requested_capabilities):
        raise AssertionError("shadowed account snapshot reader must not execute")

    monkeypatch.setattr(
        betfair_readonly.BetfairReadOnlyClient,
        "read_account_snapshot",
        shadowed_read,
    )

    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="class-rebinding",
    )

    assert len(calls) == 3
    assert acquired.snapshot.balance is not None
    assert acquired.receipt.source_authority_proven is False
    assert acquired.source_authority_proven is True


def test_product_owned_read_persists_restart_verifiable_receipt_without_secrets(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    database = tmp_path / "state" / "account-snapshots.sqlite3"
    acquirer = BetfairAccountSnapshotAcquirer(database, _credentials())

    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="restart-receipt",
    )

    assert acquired.snapshot.balance is not None
    assert acquired.snapshot.balance.available_balance == Decimal("100.1")
    assert acquired.receipt.source_authority_proven is False
    assert acquired.source_authority_proven is True
    assert acquired.receipt.provider_account_identity_proven is True
    assert acquired.receipt.grants_execution_authority is False
    assert acquired.receipt.grants_settlement_authority is False
    assert acquired.receipt.integration_kind == "official_api"
    assert acquired.receipt.provider_observed_at is None
    assert len(calls) == 3
    acquirer.verify(acquired.snapshot, acquired.receipt)

    reopened = BetfairAccountSnapshotAcquirer(database, _credentials())
    resolved = reopened.resolve(acquired.receipt.acquisition_id)

    assert resolved == acquired
    assert resolved.receipt.source_authority_proven is False
    assert resolved.source_authority_proven is False
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="not issued by live canonical provider acquisition",
    ):
        assert_account_snapshot_acquisition_authoritative(resolved)
    assert_account_snapshot_acquisition_authoritative(acquired)
    reopened.verify(resolved.snapshot, resolved.receipt)

    persisted = b"".join(
        path.read_bytes()
        for path in database.parent.glob(database.name + "*")
        if path.is_file()
    )
    assert b"APP-SECRET-SENTINEL" not in persisted
    assert b"SESSION-SECRET-SENTINEL" not in persisted
    assert b"DEVAPP-SECRET-SENTINEL" not in persisted


def test_retry_identity_is_idempotent_but_new_read_preserves_identical_content(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"

    first_calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    first = BetfairAccountSnapshotAcquirer(database, _credentials()).acquire(
        _balance_capabilities(),
        acquisition_id="read-attempt-1",
    )
    assert len(first_calls) == 3

    retry_calls = _install_transport(monkeypatch, [])
    retry = BetfairAccountSnapshotAcquirer(database, _credentials()).acquire(
        _balance_capabilities(),
        acquisition_id="read-attempt-1",
    )
    assert retry_calls == []
    assert retry == first

    second_calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    second = BetfairAccountSnapshotAcquirer(database, _credentials()).acquire(
        _balance_capabilities(),
        acquisition_id="read-attempt-2",
    )
    assert len(second_calls) == 3
    assert second.receipt.acquisition_id != first.receipt.acquisition_id
    assert (
        second.receipt.acquisition_request_id_sha256
        != first.receipt.acquisition_request_id_sha256
    )
    assert second.receipt.source_observation_id == first.receipt.source_observation_id
    assert (
        second.receipt.snapshot_content_sha256
        == first.receipt.snapshot_content_sha256
    )
    assert second.receipt.source_payload_sha256 == first.receipt.source_payload_sha256

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM account_snapshot_acquisitions"
        ).fetchone()[0]
    assert count == 2


def test_new_acquisition_id_preserves_later_identical_read_time(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "temporal.sqlite3"
    monkeypatch.setattr(betfair_readonly, "datetime", _ControlledDateTime)

    first_calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    first = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
    ).acquire(
        _balance_capabilities(),
        acquisition_id="temporal-read-1",
    )
    assert len(first_calls) == 3
    assert first.receipt.acquired_at == "2026-09-21T18:00:00+00:00"

    _ControlledDateTime.current = datetime(
        2026, 9, 21, 18, 1, 0, tzinfo=timezone.utc
    )
    retry_calls = _install_transport(monkeypatch, [])
    retry = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
    ).acquire(
        _balance_capabilities(),
        acquisition_id="temporal-read-1",
    )
    assert retry_calls == []
    assert retry == first

    second_calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    second = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
    ).acquire(
        _balance_capabilities(),
        acquisition_id="temporal-read-2",
    )
    assert len(second_calls) == 3
    assert second.receipt.acquisition_id != first.receipt.acquisition_id
    assert second.receipt.source_observation_id == first.receipt.source_observation_id
    assert (
        second.receipt.snapshot_content_sha256
        == first.receipt.snapshot_content_sha256
    )
    assert second.receipt.acquired_at == "2026-09-21T18:01:00+00:00"


def test_acquisition_id_reuse_with_changed_scope_fails_before_provider_io(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    first_calls = _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
        account_id="account-a",
    ).acquire(
        _balance_capabilities(),
        acquisition_id="scope-bound-attempt",
    )
    assert len(first_calls) == 3

    retry_calls = _install_transport(monkeypatch, [])
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="cannot be reused for another provider/account/capability scope",
    ):
        BetfairAccountSnapshotAcquirer(
            database,
            _credentials(),
            account_id="account-b",
        ).acquire(
            _balance_capabilities(),
            acquisition_id="scope-bound-attempt",
        )
    assert retry_calls == []


def test_account_scope_is_bound_and_cross_account_receipt_reuse_fails(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"

    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    first_acquirer = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
        account_id="account-a",
    )
    first = first_acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="account-a-read",
    )

    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    second_acquirer = BetfairAccountSnapshotAcquirer(
        database,
        _credentials(),
        account_id="account-b",
    )
    second = second_acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="account-b-read",
    )

    assert first.receipt.source_observation_id != second.receipt.source_observation_id
    assert first.receipt.account_id == "account-a"
    assert second.receipt.account_id == "account-b"

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="snapshot does not match durable acquisition receipt",
    ):
        first_acquirer.verify(second.snapshot, first.receipt)


def test_caller_mutated_snapshot_or_receipt_cannot_reuse_durable_authority(
    tmp_path,
    monkeypatch,
) -> None:
    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
    )
    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="mutation-test",
    )

    forged_snapshot = replace(
        acquired.snapshot,
        observed_at="2099-01-01T00:00:00+00:00",
    )
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="snapshot does not match durable acquisition receipt",
    ):
        acquirer.verify(forged_snapshot, acquired.receipt)

    forged_receipt = replace(acquired.receipt, account_id="other-account")
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="receipt does not match durable acquisition authority",
    ):
        acquirer.verify(acquired.snapshot, forged_receipt)

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="integration_kind must be official_api",
    ):
        replace(acquired.receipt, integration_kind="browser_automation")


def test_betfair_provider_identity_cannot_be_relabelled(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [])

    with pytest.raises(TypeError, match="venue_id"):
        BetfairAccountSnapshotAcquirer(
            tmp_path / "account.sqlite3",
            _credentials(),
            venue_id="other-provider",
        )

    assert calls == []


def test_bet_readback_is_rejected_before_provider_transport(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
    )

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="does not authorize capability: bet_readback",
    ):
        acquirer.acquire(
            frozenset({BookmakerCapability.BET_READBACK}),
            acquisition_id="unsupported-read",
        )

    assert calls == []


def test_provider_failure_creates_no_positive_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    queue = [_DEVELOPER_APPS]

    def fail_after_identity(self, url, *, headers, body, timeout_seconds):
        if queue:
            return queue.pop(0)
        raise BetfairReadOnlyError("simulated provider timeout")

    monkeypatch.setattr(
        betfair_readonly.UrllibBetfairHttpTransport,
        "post",
        fail_after_identity,
    )
    acquirer = BetfairAccountSnapshotAcquirer(database, _credentials())

    with pytest.raises(BetfairReadOnlyError, match="simulated provider timeout"):
        acquirer.acquire(
            _balance_capabilities(),
            acquisition_id="failed-read",
        )
    assert queue == []

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM account_snapshot_acquisitions"
        ).fetchone()[0]
    assert count == 0


def test_sql_rows_are_immutable_and_record_tamper_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    acquirer = BetfairAccountSnapshotAcquirer(database, _credentials())
    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="tamper-read",
    )

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE account_snapshot_acquisitions
                SET record_json = ?
                WHERE acquisition_id = ?
                """,
                ("{}", acquired.receipt.acquisition_id),
            )

    # Simulate an out-of-band store tamper that bypassed the SQL immutability trigger.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DROP TRIGGER account_snapshot_acquisitions_no_update"
        )
        connection.execute(
            """
            UPDATE account_snapshot_acquisitions
            SET record_json = ?
            WHERE acquisition_id = ?
            """,
            ("{}", acquired.receipt.acquisition_id),
        )

    reopened = BetfairAccountSnapshotAcquirer(database, _credentials())
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="record digest mismatch",
    ):
        reopened.resolve(acquired.receipt.acquisition_id)


def test_source_observation_identity_binds_requested_capability_scope(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"

    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    balance_only = BetfairAccountSnapshotAcquirer(database, _credentials()).acquire(
        _balance_capabilities(),
        acquisition_id="balance-scope-read",
    )

    current_empty = _response(
        {"currentOrders": [], "moreAvailable": False},
        3,
    )
    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, current_empty])
    open_only = BetfairAccountSnapshotAcquirer(database, _credentials()).acquire(
        frozenset({BookmakerCapability.OPEN_POSITIONS_READ}),
        acquisition_id="open-scope-read",
    )

    assert (
        balance_only.receipt.source_observation_id
        != open_only.receipt.source_observation_id
    )
    assert balance_only.receipt.requested_capabilities == ("balance_read",)
    assert open_only.receipt.requested_capabilities == ("open_positions_read",)


def test_unknown_or_malformed_durable_id_fails_closed(
    tmp_path,
    monkeypatch,
) -> None:
    _install_transport(monkeypatch, [_DEVELOPER_APPS, _DETAILS, _FUNDS])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
    )
    acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="unknown-id-seed",
    )

    with pytest.raises(AccountSnapshotAcquisitionError, match="unknown durable"):
        acquirer.resolve("a" * 64)
    with pytest.raises(AccountSnapshotAcquisitionError, match="SHA-256"):
        acquirer.resolve("not-a-digest")
