from __future__ import annotations

import json
from types import FunctionType

import pytest

import autosport.account_snapshot_acquisition as acquisition_module
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


def test_durable_retry_never_reissues_provider_origin(
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
    assert first.source_authority_proven is False

    for label in ("A", "B"):
        retry_calls = _install_transport(monkeypatch, [])
        with pytest.raises(
            AccountSnapshotAcquisitionError,
            match="durable acquisition cannot reissue provider-origin authority",
        ):
            BetfairAccountSnapshotAcquirer(
                database,
                _credentials(label),
                account_id="default-account",
            ).acquire(
                _balance_capabilities(),
                acquisition_id="shared-live-request",
            )
        assert retry_calls == []


def _public_acquire() -> FunctionType:
    candidate = vars(BetfairAccountSnapshotAcquirer)["acquire"]
    assert type(candidate) is FunctionType
    return candidate


def _extract_inner_authority_boundary():
    closure = _public_acquire().__closure__
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


def test_k07_account_details_metadata_spoof_cannot_mint_reader_authority(
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

    def forged_details(self):
        nonlocal attacker_calls
        attacker_calls += 1
        del self
        raise AssertionError("forged account details reader executed")

    # Spoof every cheap wrapper-identification field. The owning acquisition
    # authority must still derive executable identity from the canonical K07
    # source rather than trusting a marker/module/qualname tuple.
    forged_details.__module__ = (
        "autosport._betfair_account_identity_io_snapshot_guard"
    )
    forged_details.__qualname__ = (
        "_install_guard.<locals>.guarded_read_account_details"
    )
    forged_details._autosport_k07_io_snapshot_sealed = True

    monkeypatch.setattr(
        betfair_readonly.BetfairReadOnlyClient,
        "read_account_details",
        forged_details,
    )

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="account snapshot reader dispatch changed: read_account_details",
    ):
        acquirer.acquire(
            _balance_capabilities(),
            acquisition_id="k07-details-metadata-spoof",
        )

    assert attacker_calls == 0
    assert calls == []


def test_closure_boundary_does_not_expose_hidden_client_state(tmp_path) -> None:
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    raw_acquire = _public_acquire()
    authority = _extract_inner_authority_boundary()

    assert not hasattr(acquirer, "_client")
    assert not hasattr(acquirer, "_store")
    for name in (
        "_state",
        "_publish_live",
        "_retry_live",
        "_bind",
        "_issued",
        "_live",
        "_lock",
    ):
        assert not hasattr(authority, name)


def test_raw_acquire_metadata_does_not_expose_live_issuer_function() -> None:
    raw_acquire = _public_acquire()
    reachable = _reachable_function_names(raw_acquire)

    # Durable record/resolve helpers may remain closure-reachable because they cannot
    # mint live provider origin. There must be no independently invocable registrar,
    # state-returning capability, or mutable live registry on the recovered boundary.
    assert "issue_live" not in reachable
    assert "current_live" not in reachable
    assert "state" not in reachable

    authority = _extract_inner_authority_boundary()
    for name in (
        "publish_live",
        "retry_live",
        "state",
        "bind",
        "_publish_live",
        "_retry_live",
        "_state",
        "_bind",
        "_issued",
        "_live",
        "_lock",
    ):
        assert not hasattr(authority, name)

    # Acquisition remains the only provider-I/O operation. There is deliberately no
    # reusable positive live-origin assertion capability on the recovered boundary.
    assert callable(authority.acquire)
    assert not hasattr(authority, "assert_live")
    assert not hasattr(authority, "_fingerprint")


def test_boundary_cannot_rebind_initialized_acquirer_state(tmp_path) -> None:
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    raw_acquire = _public_acquire()
    authority = _extract_inner_authority_boundary()

    # The previous boundary exposed _state/_bind and therefore the exact hidden client
    # and credential origin. Ordinary recovered-boundary API no longer exposes either.
    assert not hasattr(authority, "_state")
    assert not hasattr(authority, "_bind")
    assert not hasattr(authority, "_publish_live")
    assert not hasattr(authority, "_retry_live")


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
    assert live.source_authority_proven is False

    durable = acquirer.resolve(live.receipt.acquisition_id)
    assert durable is not live
    assert durable.source_authority_proven is False

    # Recursive ordinary FunctionType metadata traversal of the raw owner cannot recover
    # a callable live issuer that could turn this durable object back into source authority.
    raw_acquire = _public_acquire()
    reachable = _reachable_function_names(raw_acquire)
    assert "issue_live" not in reachable
    assert "current_live" not in reachable

    authority = _extract_inner_authority_boundary()
    assert not hasattr(authority, "_publish_live")
    assert not hasattr(authority, "_state")
    assert not hasattr(authority, "_live")
    assert durable.source_authority_proven is False

    # Even the remaining safe acquisition operation cannot accept a durable snapshot
    # as a substitute for a canonically initialized acquirer/provider read.
    with pytest.raises(AccountSnapshotAcquisitionError):
        authority.acquire(
            durable,
            _balance_capabilities(),
            acquisition_id="durable-cannot-self-promote-again",
        )
    assert durable.source_authority_proven is False


def test_recovered_boundary_has_no_reusable_live_origin_capability(
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
    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="no-reusable-live-origin",
    )
    assert len(calls) == 3
    assert acquired.source_authority_proven is False

    authority = _extract_inner_authority_boundary()
    assert not hasattr(authority, "assert_live")
    assert not hasattr(authority, "_fingerprint")
    assert not hasattr(authority, "_live")

    # The public compatibility assertion remains permanently fail-closed even for
    # the exact object returned by a canonical provider read.
    assert not hasattr(
        acquisition_module,
        "assert_account_snapshot_acquisition_authoritative",
    )


def test_v4_closure_cell_replacement_cannot_create_live_origin_authority(
    tmp_path,
    monkeypatch,
) -> None:
    _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    acquired = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="closure-cell-v4",
    )
    durable = acquirer.resolve(acquired.receipt.acquisition_id)
    authority = _extract_inner_authority_boundary()

    # V4 depended on assert_live selecting a string-keyed live registry closure cell.
    # The boundary now has no such method or registry. Any remaining mapping snapshot
    # is initialization state keyed by acquirer object identity and cannot turn either
    # returned or reconstructed durable evidence into positive origin authority.
    assert not hasattr(authority, "assert_live")
    for method_name in ("initialize", "acquire", "resolve", "verify"):
        method = vars(type(authority))[method_name]
        for cell in method.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if isinstance(value, dict):
                assert acquired.receipt.acquisition_id not in value

    assert acquired.source_authority_proven is False
    assert durable.source_authority_proven is False
    for candidate in (acquired, durable):
        assert candidate.source_authority_proven is False


def test_mappingproxy_module_rebind_cannot_restore_positive_origin_authority(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        acquisition_module,
        "MappingProxyType",
        lambda value: dict(value),
    )
    calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    acquired = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    ).acquire(
        _balance_capabilities(),
        acquisition_id="mappingproxy-no-live-origin",
    )

    assert len(calls) == 3
    assert acquired.source_authority_proven is False
    assert not hasattr(
        acquisition_module,
        "assert_account_snapshot_acquisition_authoritative",
    )
