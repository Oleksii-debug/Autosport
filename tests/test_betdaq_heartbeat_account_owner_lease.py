from __future__ import annotations

import pytest

from autosport.betdaq_account_readonly import (
    BetdaqAccountReadOnlyClient,
    BetdaqCredentials,
)
from autosport.betdaq_heartbeat_safety import (
    BetdaqHeartbeatSafetyController,
    BetdaqHeartbeatSafetyError,
)
from autosport.execution_stop_authority import ExecutionStopAuthority


def _client(
    username: str = "same-user",
    *,
    application_identifier: str | None = None,
) -> BetdaqAccountReadOnlyClient:
    return BetdaqAccountReadOnlyClient(
        BetdaqCredentials(
            username=username,
            password=f"{username}-password",
            application_identifier=(
                application_identifier
                if application_identifier is not None
                else f"{username}-application"
            ),
        )
    )


def test_same_authenticated_account_rejects_parallel_owner_on_different_state_path(
    tmp_path,
) -> None:
    """#1735 falsifier 16: state-path choice cannot mint a second local owner."""
    account = _client()
    stop = ExecutionStopAuthority(tmp_path / "stop.jsonl")
    first = BetdaqHeartbeatSafetyController(
        account_client=account,
        stop_authority=stop,
        state_path=tmp_path / "owner-a" / "heartbeat.json",
    )
    second = None
    try:
        with pytest.raises(BetdaqHeartbeatSafetyError):
            second = BetdaqHeartbeatSafetyController(
                account_client=account,
                stop_authority=stop,
                state_path=tmp_path / "owner-b" / "heartbeat.json",
            )
    finally:
        if second is not None:
            second.close()
        first.close()


def test_different_authenticated_accounts_may_have_independent_local_owners(
    tmp_path,
) -> None:
    """The product singleton is account-context scoped, not process-global."""
    stop = ExecutionStopAuthority(tmp_path / "stop.jsonl")
    first = BetdaqHeartbeatSafetyController(
        account_client=_client("account-a"),
        stop_authority=stop,
        state_path=tmp_path / "owner-a" / "heartbeat.json",
    )
    second = BetdaqHeartbeatSafetyController(
        account_client=_client("account-b"),
        stop_authority=stop,
        state_path=tmp_path / "owner-b" / "heartbeat.json",
    )
    try:
        assert first.status().provider_registration_active is False
        assert second.status().provider_registration_active is False
    finally:
        second.close()
        first.close()


def test_same_punter_different_application_ids_cannot_mint_parallel_owner(
    tmp_path,
) -> None:
    """BETDAQ heartbeat is Punter-scoped, not application-identifier scoped."""
    stop = ExecutionStopAuthority(tmp_path / "stop.jsonl")
    first = BetdaqHeartbeatSafetyController(
        account_client=_client(
            "same-user",
            application_identifier="application-a",
        ),
        stop_authority=stop,
        state_path=tmp_path / "owner-a" / "heartbeat.json",
    )
    second = None
    try:
        with pytest.raises(BetdaqHeartbeatSafetyError):
            second = BetdaqHeartbeatSafetyController(
                account_client=_client(
                    "same-user",
                    application_identifier="application-b",
                ),
                stop_authority=stop,
                state_path=tmp_path / "owner-b" / "heartbeat.json",
            )
    finally:
        if second is not None:
            second.close()
        first.close()


def test_owner_lease_path_persists_no_plaintext_credentials(tmp_path) -> None:
    username = "secret-user"
    password = f"{username}-password"
    application_identifier = "secret-application"
    stop = ExecutionStopAuthority(tmp_path / "stop.jsonl")
    controller = BetdaqHeartbeatSafetyController(
        account_client=BetdaqAccountReadOnlyClient(
            BetdaqCredentials(
                username=username,
                password=password,
                application_identifier=application_identifier,
            )
        ),
        stop_authority=stop,
        state_path=tmp_path / "arbitrary" / "heartbeat.json",
    )
    try:
        lease_path = str(controller._lease.path)
        assert username not in lease_path
        assert password not in lease_path
        assert application_identifier not in lease_path
        assert "arbitrary" not in lease_path
    finally:
        controller.close()

def test_same_punter_different_stop_paths_cannot_mint_parallel_owner(
    tmp_path,
) -> None:
    """#1735 falsifier 16: caller STOP path cannot split one Punter owner."""
    account = _client()
    first = BetdaqHeartbeatSafetyController(
        account_client=account,
        stop_authority=ExecutionStopAuthority(
            tmp_path / "workspace-a" / "stop-a.jsonl"
        ),
        state_path=tmp_path / "owner-a" / "heartbeat.json",
    )
    second = None
    try:
        with pytest.raises(BetdaqHeartbeatSafetyError):
            second = BetdaqHeartbeatSafetyController(
                account_client=account,
                stop_authority=ExecutionStopAuthority(
                    tmp_path / "workspace-b" / "stop-b.jsonl"
                ),
                state_path=tmp_path / "owner-b" / "heartbeat.json",
            )
    finally:
        if second is not None:
            second.close()
        first.close()


def test_different_punters_with_different_stop_paths_remain_independent(
    tmp_path,
) -> None:
    """The machine-root lease is Punter-scoped, never a BETDAQ global singleton."""
    first = BetdaqHeartbeatSafetyController(
        account_client=_client("account-root-a"),
        stop_authority=ExecutionStopAuthority(
            tmp_path / "workspace-a" / "stop-a.jsonl"
        ),
        state_path=tmp_path / "owner-a" / "heartbeat.json",
    )
    second = BetdaqHeartbeatSafetyController(
        account_client=_client("account-root-b"),
        stop_authority=ExecutionStopAuthority(
            tmp_path / "workspace-b" / "stop-b.jsonl"
        ),
        state_path=tmp_path / "owner-b" / "heartbeat.json",
    )
    try:
        assert first.status().provider_registration_active is False
        assert second.status().provider_registration_active is False
    finally:
        second.close()
        first.close()


def test_owner_lease_rejects_rebound_product_root_resolver(
    tmp_path,
    monkeypatch,
) -> None:
    """Caller class mutation cannot redirect the canonical machine-root lease."""
    stop = ExecutionStopAuthority(tmp_path / "stop.jsonl")

    def hostile_root():
        raise AssertionError("hostile product-root resolver executed")

    monkeypatch.setattr(
        ExecutionStopAuthority,
        "_product_monotonic_authority_root",
        staticmethod(hostile_root),
    )
    with pytest.raises(
        BetdaqHeartbeatSafetyError,
        match="product authority root resolver changed",
    ):
        BetdaqHeartbeatSafetyController(
            account_client=_client("resolver-guard-user"),
            stop_authority=stop,
            state_path=tmp_path / "heartbeat.json",
        )

def test_different_punters_cannot_share_one_durable_state_writer(
    tmp_path,
) -> None:
    """Independent Punter owners cannot concurrently mutate one state/anchor pair."""
    shared_state = tmp_path / "shared" / "heartbeat.json"
    first = BetdaqHeartbeatSafetyController(
        account_client=_client("state-writer-account-a"),
        stop_authority=ExecutionStopAuthority(tmp_path / "stop-a.jsonl"),
        state_path=shared_state,
    )
    second = None
    try:
        with pytest.raises(
            BetdaqHeartbeatSafetyError,
            match="another process owns",
        ):
            second = BetdaqHeartbeatSafetyController(
                account_client=_client("state-writer-account-b"),
                stop_authority=ExecutionStopAuthority(tmp_path / "stop-b.jsonl"),
                state_path=shared_state,
            )
    finally:
        if second is not None:
            second.close()
        first.close()

