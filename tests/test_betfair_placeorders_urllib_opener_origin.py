from __future__ import annotations

import json
import runpy
import tempfile
import urllib.request as urllib_request
from pathlib import Path

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
import autosport.betfair_supervised_execution as betfair_supervised_execution
from autosport.betfair_account_readonly import (
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    BetfairSupervisedExecutionGate,
    BetfairSupervisedPlaceOrdersClient,
    execute_betfair_supervised_action,
)


_HELPERS = runpy.run_path(
    str(Path(__file__).with_name("test_betfair_supervised_execution.py"))
)
_prepared = _HELPERS["_prepared"]
_response = _HELPERS["_response"]
READBACK_AT = _HELPERS["READBACK_AT"]
SUBMITTED_AT = _HELPERS["SUBMITTED_AT"]


class _ForgedResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, max_bytes: int) -> bytes:
        return self._payload[:max_bytes]


class _ForgedProcessOpener:
    def __init__(self, action) -> None:
        self._action = action
        self.calls: list[dict[str, object]] = []

    def open(self, fullurl, data=None, timeout=None):
        request_body = fullurl.data or b""
        decoded = json.loads(request_body.decode("utf-8"))
        self.calls.append(
            {
                "url": fullurl.full_url,
                "request": decoded,
                "timeout": timeout,
            }
        )
        return _ForgedResponse(
            _response(
                decoded,
                matched=self._action.requested_stake,
                average=self._action.requested_odds,
                bet_id="bet-forged-process-opener",
                order_status="EXECUTION_COMPLETE",
            )
        )


def test_process_global_urllib_opener_cannot_mint_terminal_placeorders_truth(
    monkeypatch,
) -> None:
    """Canonical urlopen identity is insufficient if its mutable opener graph moves."""

    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        gate = BetfairSupervisedExecutionGate.from_economic_goal_store(
            goal_store,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
        )
        client = BetfairSupervisedPlaceOrdersClient(
            BetfairSessionCredentials("app-key", "session-token"),
            gate=gate,
            clock=lambda: READBACK_AT,
        )
        forged_opener = _ForgedProcessOpener(action)

        assert type(client._transport) is UrllibBetfairHttpTransport
        assert (
            betfair_account_readonly.urlopen
            is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_URLOPEN
        )
        assert (
            betfair_account_readonly.urlopen.__globals__
            is urllib_request.urlopen.__globals__
        )

        # urllib.request.urlopen keeps its identity/code while delegating through
        # this process-global dependency. Existing #1212 preflight checks do not
        # bind that dependency graph.
        monkeypatch.setattr(urllib_request, "_opener", forged_opener)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical client, transport, and parser authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-forged-process-opener",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_opener.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
