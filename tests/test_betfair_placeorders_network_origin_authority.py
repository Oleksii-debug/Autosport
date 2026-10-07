from __future__ import annotations

import json
import runpy
import tempfile
from pathlib import Path
from urllib.parse import quote_from_bytes

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
READBACK_AT = _HELPERS["READBACK_AT"]
SUBMITTED_AT = _HELPERS["SUBMITTED_AT"]


def test_canonical_transport_authority_tracks_real_build_opener_graph() -> None:
    """The supervised seal must match the actual account transport dependencies."""

    post_globals = UrllibBetfairHttpTransport.post.__globals__
    assert "urlopen" not in post_globals
    assert (
        post_globals["Request"]
        is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_REQUEST
    )
    assert (
        post_globals["build_opener"]
        is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_BUILD_OPENER
    )
    assert (
        post_globals["HTTPRedirectHandler"]
        is betfair_supervised_execution
        ._CANONICAL_URLLIB_BETFAIR_HTTP_REDIRECT_HANDLER
    )


def test_rebound_build_opener_cannot_mint_terminal_provider_truth(
    monkeypatch,
) -> None:
    """A rebound account transport factory must fail before a durable attempt."""

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
        forged_calls: list[object] = []

        def forged_build_opener(*handlers):
            forged_calls.append(handlers)
            raise AssertionError("rebound build_opener must never be invoked")

        assert type(client._transport) is UrllibBetfairHttpTransport
        assert (
            UrllibBetfairHttpTransport.post
            is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_POST
        )
        monkeypatch.setattr(
            betfair_account_readonly,
            "build_opener",
            forged_build_opener,
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
                attempt_id="attempt-forged-build-opener",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_rebound_request_constructor_cannot_redirect_private_opener_to_forged_bytes(
    monkeypatch,
) -> None:
    """Canonical provider truth must bind the Request constructor origin too."""

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

        def forged_request(url, *, data, headers, method):
            decoded = json.loads(data.decode("utf-8"))
            forged_calls.append(
                {
                    "url": url,
                    "request": decoded,
                    "headers": headers,
                    "method": method,
                }
            )
            return "data:application/json," + quote_from_bytes(b"{}")

        assert type(client._transport) is UrllibBetfairHttpTransport
        assert (
            UrllibBetfairHttpTransport.post
            is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_POST
        )
        assert (
            betfair_account_readonly.build_opener
            is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_BUILD_OPENER
        )
        assert (
            betfair_supervised_execution._parse_place_orders_response
            is betfair_supervised_execution._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
        )
        monkeypatch.setattr(
            betfair_account_readonly,
            "Request",
            forged_request,
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
                attempt_id="attempt-forged-request-origin",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_dual_rebound_build_opener_and_canonical_alias_cannot_mint_provider_truth(
    monkeypatch,
) -> None:
    """Rebinding both mutable aliases cannot replace the captured opener graph."""

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
        forged_calls: list[object] = []

        def forged_build_opener(*handlers):
            forged_calls.append(handlers)
            raise AssertionError("forged build_opener must never be invoked")

        monkeypatch.setattr(
            betfair_account_readonly,
            "build_opener",
            forged_build_opener,
        )
        monkeypatch.setattr(
            betfair_supervised_execution,
            "_CANONICAL_URLLIB_BETFAIR_BUILD_OPENER",
            forged_build_opener,
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
                attempt_id="attempt-forged-dual-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
