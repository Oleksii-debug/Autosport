from __future__ import annotations

import json
from types import FunctionType

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


def _extract_inner_authority_boundary(raw_acquire):
    closure = raw_acquire.__closure__
    assert closure is not None
    candidates = [
        cell.cell_contents
        for cell in closure
        if type(cell.cell_contents).__name__ == "_AccountSnapshotAuthorityBoundary"
    ]
    assert len(candidates) == 1
    return candidates[0]


def _reachable_function_names(root: FunctionType) -> set[str]:
    pending = [root]
    seen: set[int] = set()
    names: set[str] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        names.add(current.__name__)
        for value in current.__defaults__ or ():
            if type(value) is FunctionType:
                pending.append(value)
        for value in (current.__kwdefaults__ or {}).values():
            if type(value) is FunctionType:
                pending.append(value)
        for cell in current.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if type(value) is FunctionType:
                pending.append(value)
    return names


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
    authority = _extract_inner_authority_boundary(raw_acquire)
    _, client, _ = authority.state(acquirer)
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


def test_raw_acquire_metadata_does_not_expose_live_issuer_function() -> None:
    raw_acquire = _extract_outer_guard_raw_acquire()
    reachable = _reachable_function_names(raw_acquire)

    # Durable record/resolve helpers may remain closure-reachable because they cannot
    # mint live provider origin. The live issuer/retry/state primitives must not.
    assert "issue_live" not in reachable
    assert "current_live" not in reachable
    assert "state" not in reachable


def test_mutable_client_credentials_cannot_retarget_init_origin(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    credentials_a = _credentials("A")
    credentials_b = _credentials("B")
    first_calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    first_acquirer = BetfairAccountSnapshotAcquirer(
        database,
        credentials_a,
        account_id="default-account",
    )
    first = first_acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="mutable-client-origin",
    )
    assert len(first_calls) == 3
    assert first.source_authority_proven is True

    second = BetfairAccountSnapshotAcquirer(
        database,
        credentials_b,
        account_id="default-account",
    )
    raw_acquire = _extract_outer_guard_raw_acquire()
    authority = _extract_inner_authority_boundary(raw_acquire)
    _, client, init_origin = authority.state(second)
    assert init_origin == credentials_b

    # This is ordinary attribute assignment on the hidden client recovered through the
    # same raw-acquire closure surface. Origin authority must not be re-derived from it.
    client._credentials = credentials_a
    different_origin_calls = _install_transport(monkeypatch, [])
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="credential origin changed after initialization",
    ):
        raw_acquire(
            second,
            _balance_capabilities(),
            acquisition_id="mutable-client-origin",
        )
    assert different_origin_calls == []
    assert first.source_authority_proven is True


def test_durable_resolve_remains_non_authoritative_without_new_provider_read(
    tmp_path,
    monkeypatch,
) -> None:
    calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    live = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="durable-cannot-self-promote",
    )
    assert len(calls) == 3
    assert live.source_authority_proven is True

    durable = acquirer.resolve(live.receipt.acquisition_id)
    assert durable is not live
    assert durable.source_authority_proven is False

    # Recursive ordinary FunctionType metadata traversal of the raw owner cannot recover
    # a callable live issuer that could turn this durable object back into source authority.
    raw_acquire = _extract_outer_guard_raw_acquire()
    reachable = _reachable_function_names(raw_acquire)
    assert "issue_live" not in reachable
    assert "current_live" not in reachable
