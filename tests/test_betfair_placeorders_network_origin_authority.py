from __future__ import annotations

import json
import runpy
import tempfile
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


class _ForgedUrlopenResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, max_bytes: int) -> bytes:
        return self._payload[:max_bytes]


def test_rebound_urlopen_cannot_mint_terminal_provider_truth(
    monkeypatch,
) -> None:
    """The canonical write authority must bind its real network primitive."""

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
        forged_calls: list[dict[str, object]] = []

        def forged_urlopen(request, timeout):
            decoded = json.loads((request.data or b"").decode("utf-8"))
            forged_calls.append(
                {
                    "url": request.full_url,
                    "request": decoded,
                    "timeout": timeout,
                }
            )
            return _ForgedUrlopenResponse(
                _response(
                    decoded,
                    matched=action.requested_stake,
                    average=action.requested_odds,
                    bet_id="bet-forged-network-origin",
                    order_status="EXECUTION_COMPLETE",
                )
            )

        # The exact-head origin seal checks all of these identities, yet the
        # canonical post method still resolves its module-global urlopen at
        # call time. Rebinding only that lowest-level primitive must therefore
        # be detected before any durable attempt or provider acknowledgement.
        assert type(client._transport) is UrllibBetfairHttpTransport
        assert (
            UrllibBetfairHttpTransport.post
            is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_POST
        )
        assert (
            betfair_supervised_execution._parse_place_orders_response
            is betfair_supervised_execution._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
        )
        monkeypatch.setattr(
            betfair_account_readonly,
            "urlopen",
            forged_urlopen,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical client, transport, and parser authority",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-forged-network-origin",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
