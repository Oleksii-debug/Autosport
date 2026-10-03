from __future__ import annotations

import gc
import json
from types import FunctionType, MappingProxyType
from weakref import ref

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


def test_closure_boundary_does_not_expose_hidden_client_state(tmp_path) -> None:
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    raw_acquire = _extract_outer_guard_raw_acquire()
    authority = _extract_inner_authority_boundary(raw_acquire)

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
    raw_acquire = _extract_outer_guard_raw_acquire()
    reachable = _reachable_function_names(raw_acquire)

    # Durable record/resolve helpers may remain closure-reachable because they cannot
    # mint live provider origin. There must be no independently invocable registrar,
    # state-returning capability, or mutable live registry on the recovered boundary.
    assert "issue_live" not in reachable
    assert "current_live" not in reachable
    assert "state" not in reachable

    authority = _extract_inner_authority_boundary(raw_acquire)
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

    # The remaining authority-bearing operation is safe to invoke: it accepts an
    # acquirer + request and must execute the canonical provider acquisition path.
    assert callable(authority.acquire)
    assert callable(authority.assert_live)


def test_boundary_cannot_rebind_initialized_acquirer_state(tmp_path) -> None:
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    raw_acquire = _extract_outer_guard_raw_acquire()
    authority = _extract_inner_authority_boundary(raw_acquire)

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

    authority = _extract_inner_authority_boundary(raw_acquire)
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


def _boundary_mapping_snapshots(authority: object) -> list[MappingProxyType]:
    snapshots: list[MappingProxyType] = []
    seen: set[int] = set()
    for method_name in ("initialize", "acquire", "resolve", "verify", "assert_live"):
        method = vars(type(authority))[method_name]
        for cell in method.__closure__ or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if type(value) is MappingProxyType and id(value) not in seen:
                seen.add(id(value))
                snapshots.append(value)
    return snapshots


def test_closure_boundary_direct_object_setattr_cannot_replace_live_authority(
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
        acquisition_id="direct-object-setattr",
    )
    assert len(calls) == 3
    durable = acquirer.resolve(live.receipt.acquisition_id)
    assert live.source_authority_proven is True
    assert durable.source_authority_proven is False

    authority = _extract_inner_authority_boundary(
        _extract_outer_guard_raw_acquire()
    )
    forged = MappingProxyType(
        {
            durable.receipt.acquisition_id: (
                ref(durable),
                authority._fingerprint(durable),
                _credentials("A"),
            )
        }
    )

    with pytest.raises(AttributeError):
        object.__setattr__(
            authority,
            "_AccountSnapshotAuthorityBoundary__live",
            forged,
        )

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="not issued by live canonical provider acquisition",
    ):
        authority.assert_live(durable)
    authority.assert_live(live)
    assert durable.source_authority_proven is False


@pytest.mark.parametrize(
    "storage_name",
    (
        "_AccountSnapshotAuthorityBoundary__issued",
        "_AccountSnapshotAuthorityBoundary__live",
        "_AccountSnapshotAuthorityBoundary__lock",
    ),
)
def test_closure_boundary_has_no_replaceable_authority_storage(
    tmp_path,
    storage_name: str,
) -> None:
    BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    authority = _extract_inner_authority_boundary(
        _extract_outer_guard_raw_acquire()
    )

    assert not hasattr(authority, storage_name)
    with pytest.raises(AttributeError):
        setattr(authority, storage_name, object())
    with pytest.raises(AttributeError):
        object.__setattr__(authority, storage_name, object())


def test_closure_boundary_authority_snapshots_are_immutable_and_copy_isolated(
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
    live = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="closure-snapshot-copy",
    )
    durable = acquirer.resolve(live.receipt.acquisition_id)
    authority = _extract_inner_authority_boundary(
        _extract_outer_guard_raw_acquire()
    )

    snapshots = _boundary_mapping_snapshots(authority)
    assert snapshots
    live_snapshots = [
        snapshot
        for snapshot in snapshots
        if live.receipt.acquisition_id in snapshot
    ]
    assert live_snapshots
    canonical_live_snapshot = live_snapshots[0]

    with pytest.raises(TypeError):
        canonical_live_snapshot[durable.receipt.acquisition_id] = (
            ref(durable),
            authority._fingerprint(durable),
            _credentials("A"),
        )

    forged = dict(canonical_live_snapshot)
    forged[durable.receipt.acquisition_id] = (
        ref(durable),
        authority._fingerprint(durable),
        _credentials("A"),
    )
    assert forged is not canonical_live_snapshot

    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="not issued by live canonical provider acquisition",
    ):
        authority.assert_live(durable)
    authority.assert_live(live)


def test_closure_boundary_immutable_live_snapshot_preserves_weakref_expiry(
    tmp_path,
    monkeypatch,
) -> None:
    database = tmp_path / "account.sqlite3"
    calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _FUNDS],
    )
    acquirer = BetfairAccountSnapshotAcquirer(
        database,
        _credentials("A"),
        account_id="default-account",
    )
    live = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="weakref-expiry",
    )
    assert len(calls) == 3
    live_ref = ref(live)
    del live
    gc.collect()
    assert live_ref() is None

    retry_calls = _install_transport(monkeypatch, [])
    with pytest.raises(
        AccountSnapshotAcquisitionError,
        match="durable acquisition cannot reissue provider-origin authority",
    ):
        acquirer.acquire(
            _balance_capabilities(),
            acquisition_id="weakref-expiry",
        )
    assert retry_calls == []


def test_closure_boundary_storage_ignores_mappingproxy_module_rebind(
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
    acquirer = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials("A"),
        account_id="default-account",
    )
    live = acquirer.acquire(
        _balance_capabilities(),
        acquisition_id="mappingproxy-rebind",
    )
    assert len(calls) == 3

    authority = _extract_inner_authority_boundary(
        _extract_outer_guard_raw_acquire()
    )
    snapshots = _boundary_mapping_snapshots(authority)
    assert snapshots
    live_snapshots = [
        snapshot
        for snapshot in snapshots
        if live.receipt.acquisition_id in snapshot
    ]
    assert live_snapshots
    assert all(type(snapshot) is MappingProxyType for snapshot in snapshots)

    with pytest.raises(TypeError):
        live_snapshots[0]["forged"] = object()

