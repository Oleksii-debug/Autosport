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
