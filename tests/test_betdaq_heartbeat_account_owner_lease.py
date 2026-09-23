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


def _client(username: str = "same-user") -> BetdaqAccountReadOnlyClient:
    return BetdaqAccountReadOnlyClient(
        BetdaqCredentials(
            username=username,
            password=f"{username}-password",
            application_identifier=f"{username}-application",
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
