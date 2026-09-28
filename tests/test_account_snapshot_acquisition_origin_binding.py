from __future__ import annotations

import json

import pytest

import autosport.betfair_account_readonly as betfair_readonly
from autosport.account_snapshot_acquisition import (
    AccountSnapshotAcquisitionError,
    BetfairAccountSnapshotAcquirer,
)
from autosport.betfair_account_readonly import BetfairSessionCredentials
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


def _credentials(label: str) -> BetfairSessionCredentials:
    return BetfairSessionCredentials(
        f"APP-SECRET-{label}",
        f"SESSION-SECRET-{label}",
    )


def _balance_capabilities() -> frozenset[BookmakerCapability]:
    return frozenset({BookmakerCapability.BALANCE_READ})


def test_live_retry_cannot_cross_authenticated_credential_origin(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    first_calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    first = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    ).acquire(
        _balance_capabilities(),
        acquisition_id="shared-live-request",
    )
    assert len(first_calls) == 3
    assert first.source_authority_proven is True

    # The original live object remains strongly referenced above. A second canonical
    # acquirer with different credentials but the same caller-local account label must
    # not inherit that remote-provider capability from the idempotency fast path.
    different_origin_calls = _install_transport(monkeypatch, [])
    second = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("B"),
        account_id="default-account",
    )
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="bound to a different authenticated credential origin",
    ):
        second.acquire(
            _balance_capabilities(),
            acquisition_id="shared-live-request",
        )
    assert different_origin_calls == []

    # A reconstructed canonical acquirer carrying the exact same in-memory credential
    # values remains an idempotent retry of the same authenticated origin and performs
    # no provider I/O while the exact issued object is still live.
    same_origin_calls = _install_transport(monkeypatch, [])
    retry = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    ).acquire(
        _balance_capabilities(),
        acquisition_id="shared-live-request",
    )
    assert retry is first
    assert same_origin_calls == []


def _extract_outer_guard_raw_acquire():
    guarded = vars(BetfairAccountSnapshotAcquirer)["acquire"]
    closure = guarded.__closure__
    assert closure is not None
    candidates = [
        cell.cell_contents
        for cell in closure
        if callable(cell.cell_contents)
        and getattr(cell.cell_contents, "__name__", None) == "acquire"
        and cell.cell_contents is not guarded
    ]
    assert len(candidates) == 1
    return candidates[0]


def test_inner_live_retry_origin_survives_outer_guard_bypass(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    first_calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    first_acquirer = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    )
    first = first_acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="inner-origin-request",
    )
    assert len(first_calls) == 3
    assert first.source_authority_proven is True

    # The outer origin guard is ordinary Python and its closure exposes the owning
    # acquisition callable. The owning live-authority model must therefore enforce
    # the credential origin itself rather than relying on the wrapper as the only
    # cross-credential fence.
    raw_acquire = _extract_outer_guard_raw_acquire()

    different_origin_calls = _install_transport(monkeypatch, [])
    second = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("B"),
        account_id="default-account",
    )
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="bound to a different authenticated credential origin",
    ):
        raw_acquire(
            second,
            _balance_capabilities(),
            acquisition_id="inner-origin-request",
        )
    assert different_origin_calls == []

    same_origin_calls = _install_transport(monkeypatch, [])
    same_origin = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    )
    retry = raw_acquire(
        same_origin,
        _balance_capabilities(),
        acquisition_id="inner-origin-request",
    )
    assert retry is first
    assert same_origin_calls == []


def _extract_inner_acquirer_state(raw_acquire):
    closure = raw_acquire.__closure__
    assert closure is not None
    candidates = [
        cell.cell_contents
        for cell in closure
        if callable(cell.cell_contents)
        and getattr(cell.cell_contents, "__name__", None) == "state"
    ]
    assert len(candidates) == 1
    return candidates[0]


def test_account_snapshot_reader_class_dispatch_rebind_fails_before_provider_io(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    attacker_calls = 0

    def forged_funds(self):
        nonlocal attacker_calls
        attacker_calls += 1
        del self
        raise AssertionError("forged account funds reader executed")

    monkeypatch.setattr(
        betfair_readonly.BetfairReadOnlyClient,
        "read_account_funds",
        forged_funds,
    )

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="account snapshot reader dispatch changed: read_account_funds",
    ):
        acquirer.acquire(
            _balance_capabilities(),
            acquisition_id="class-dispatch-rebind",
        )

    assert attacker_calls == 0
    assert calls == []


def test_account_snapshot_reader_instance_shadow_fails_before_provider_io(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(monkeypatch, [])
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    raw_acquire = _extract_outer_guard_raw_acquire()
    state = _extract_inner_acquirer_state(raw_acquire)
    _, client = state(acquirer)
    attacker_calls = 0

    def forged_funds():
        nonlocal attacker_calls
        attacker_calls += 1
        raise AssertionError("instance-shadowed account funds reader executed")

    monkeypatch.setattr(
        client,
        "read_account_funds",
        forged_funds,
        raising=False,
    )

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="instance-level dispatch shadow: read_account_funds",
    ):
        acquirer.acquire(
            _balance_capabilities(),
            acquisition_id="instance-dispatch-shadow",
        )

    assert attacker_calls == 0
    assert calls == []
