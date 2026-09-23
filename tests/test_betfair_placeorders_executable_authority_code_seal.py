from __future__ import annotations

import runpy
import tempfile
from pathlib import Path

import pytest

import autosport.betfair_supervised_execution as betfair_supervised_execution
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    BetfairSupervisedExecutionGate,
    BetfairSupervisedPlaceOrdersClient,
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
)
from autosport.real_execution_ledger import AttemptState


_HELPERS = runpy.run_path(
    str(Path(__file__).with_name("test_betfair_supervised_execution.py"))
)
_prepared = _HELPERS["_prepared"]
READBACK_AT = _HELPERS["READBACK_AT"]
SUBMITTED_AT = _HELPERS["SUBMITTED_AT"]


def _forged_http_post(
    self,
    url,
    *,
    headers,
    body,
    timeout_seconds,
):
    return b'{"jsonrpc":"2.0","id":1,"result":{}}'


def _forged_parser(
    payload,
    *,
    request_id,
    request_sha256,
    action,
    provider_order_ref,
    observed_at,
):
    return None


def _forged_place_action(
    self,
    action,
    *,
    profile,
    bound,
    provider_order_ref,
    execution_workspace,
    _transport_post=None,
    _response_parser=None,
):
    return BetfairPlaceExecutionReport(
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        action_id=action.action_id,
        provider_order_ref=provider_order_ref,
        market_id=action.market_id,
        request_id=1,
        request_sha256="0" * 64,
        response_sha256="1" * 64,
        observed_at="2026-09-23T10:00:00Z",
        status="SUCCESS",
        error_code=None,
        instruction=BetfairInstructionReport(
            status="SUCCESS",
            error_code=None,
            bet_id="bet-forged-code-object",
            placed_date="2026-09-23T10:00:00Z",
            average_price_matched=action.requested_odds,
            size_matched=action.requested_stake,
            order_status="EXECUTION_COMPLETE",
        ),
    )


def _client_for(tmp: str):
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
    return profile, bound, approval, ledger, action, client


def test_in_place_http_code_replacement_fails_before_durable_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        target = betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_POST
        original_code = target.__code__
        assert target is betfair_supervised_execution.UrllibBetfairHttpTransport.post
        try:
            target.__code__ = _forged_http_post.__code__
            assert target is betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_POST

            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-http-code-replaced",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            target.__code__ = original_code


def test_in_place_parser_code_replacement_fails_before_durable_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        target = betfair_supervised_execution._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE
        original_code = target.__code__
        assert target is betfair_supervised_execution._parse_place_orders_response
        try:
            target.__code__ = _forged_parser.__code__
            assert target is betfair_supervised_execution._CANONICAL_PARSE_PLACE_ORDERS_RESPONSE

            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-parser-code-replaced",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            target.__code__ = original_code


def test_midflight_place_action_code_replacement_cannot_mint_terminal_truth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        target = betfair_supervised_execution._CANONICAL_BETFAIR_PLACE_ACTION
        original_code = target.__code__
        mutated = False

        def mutate_on_submit() -> str:
            nonlocal mutated
            if not mutated:
                target.__code__ = _forged_place_action.__code__
                mutated = True
            return SUBMITTED_AT

        try:
            result = execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-midflight-code-replaced",
                profile=profile,
                client=client,
                clock=mutate_on_submit,
            )

            assert mutated is True
            assert result.outcome is PlaceOrdersOutcome.UNKNOWN
            assert result.attempt_state is AttemptState.UNKNOWN
            assert result.evidence_id is None
            assert result.external_receipt_id is None
            assert (
                ledger.attempt_state("attempt-midflight-code-replaced")
                is AttemptState.UNKNOWN
            )
        finally:
            target.__code__ = original_code

def test_provider_http_post_closure_replacement_fails_before_durable_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        target = betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_POST
        closure = target.__closure__
        assert closure is not None
        assert "private_opener" in target.__code__.co_freevars
        cell = closure[target.__code__.co_freevars.index("private_opener")]
        original_value = cell.cell_contents
        try:
            # Function identity and __code__ stay canonical while a captured
            # provider-network authority object is persistently replaced.
            cell.cell_contents = object()
            assert (
                target
                is betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_POST
            )
            assert (
                target.__code__
                is betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_POST_CODE
            )

            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-provider-http-closure-replaced",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            cell.cell_contents = original_value

@pytest.mark.parametrize("attribute", ("_open", "_call_chain", "error"))
def test_private_opener_internal_dispatch_shadow_fails_before_durable_attempt(
    attribute: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        target = betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_POST
        closure = target.__closure__
        assert closure is not None
        assert "private_opener" in target.__code__.co_freevars
        private_opener = closure[
            target.__code__.co_freevars.index("private_opener")
        ].cell_contents
        assert (
            private_opener
            is betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
        )
        assert attribute not in getattr(private_opener, "__dict__", {})
        forged_calls: list[tuple[object, ...]] = []

        def forged_dispatch(*args, **kwargs):
            forged_calls.append(args)
            raise AssertionError(
                "forged private-opener dispatch must not be reached"
            )

        setattr(private_opener, attribute, forged_dispatch)
        try:
            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id=f"attempt-private-opener-{attribute}",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert forged_calls == []
            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            delattr(private_opener, attribute)



@pytest.mark.parametrize(
    "mapping_name",
    ("handle_open", "process_request", "process_response"),
)
def test_private_opener_https_dispatch_map_rewrite_fails_before_durable_attempt(
    mapping_name: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        private_opener = (
            betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
        )
        mapping = getattr(private_opener, mapping_name)
        assert type(mapping) is dict
        assert "https" in mapping
        original_handlers = mapping["https"]
        forged_calls: list[tuple[object, ...]] = []

        class ForgedHttpsHandler:
            def https_open(self, *args, **kwargs):
                forged_calls.append(args)
                raise AssertionError("forged HTTPS handler must not be reached")

            def https_request(self, request):
                forged_calls.append((request,))
                return request

            def https_response(self, request, response):
                forged_calls.append((request, response))
                return response

        mapping["https"] = [ForgedHttpsHandler()]
        try:
            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id=f"attempt-private-opener-map-{mapping_name}",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert forged_calls == []
            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            mapping["https"] = original_handlers


def test_private_https_handler_instance_dispatch_shadow_fails_before_durable_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        private_opener = (
            betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
        )
        https_handlers = private_opener.handle_open["https"]
        https_handler = next(
            handler
            for handler in https_handlers
            if hasattr(type(handler), "https_open")
        )
        assert "https_open" not in getattr(https_handler, "__dict__", {})
        forged_calls: list[tuple[object, ...]] = []

        def forged_https_open(*args, **kwargs):
            forged_calls.append(args)
            raise AssertionError("forged HTTPS handler dispatch must not be reached")

        https_handler.https_open = forged_https_open
        try:
            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-private-opener-https-shadow",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert forged_calls == []
            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            delattr(https_handler, "https_open")


def test_private_opener_error_dispatch_map_rewrite_fails_before_durable_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        private_opener = (
            betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
        )
        error_mapping = private_opener.handle_error
        assert type(error_mapping) is dict and error_mapping
        protocol = next(iter(error_mapping))
        by_code = error_mapping[protocol]
        assert type(by_code) is dict and by_code
        code = next(iter(by_code))
        original_handlers = by_code[code]
        by_code[code] = [object()]
        try:
            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-private-opener-error-map",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            by_code[code] = original_handlers

@pytest.mark.parametrize("status_code", (301, 302, 303, 307, 308))
def test_private_provider_opener_rejects_authenticated_redirect_request(
    status_code: int,
) -> None:
    private_opener = (
        betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
    )
    redirect_type = (
        betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_REDIRECT_HANDLER
    )
    redirect_handlers = [
        handler
        for handler in private_opener.handlers
        if type(handler) is redirect_type
    ]
    assert len(redirect_handlers) == 1
    redirect_handler = redirect_handlers[0]
    assert "redirect_request" in getattr(redirect_handler, "__dict__", {})

    request = betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_REQUEST(
        betfair_supervised_execution.BETTING_JSON_RPC_ENDPOINT,
        data=b"{}",
        headers={
            "X-Application": "app-key",
            "X-Authentication": "session-token",
        },
        method="POST",
    )
    redirected = redirect_handler.redirect_request(
        request,
        None,
        status_code,
        "redirect",
        {"Location": "https://attacker.invalid/steal"},
        "https://attacker.invalid/steal",
    )
    assert redirected is None


def test_private_redirect_policy_shadow_fails_before_durable_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, client = _client_for(tmp)
        private_opener = (
            betfair_supervised_execution._CANONICAL_PROVIDER_HTTP_PRIVATE_OPENER
        )
        redirect_type = (
            betfair_supervised_execution._CANONICAL_URLLIB_BETFAIR_HTTP_REDIRECT_HANDLER
        )
        redirect_handler = next(
            handler
            for handler in private_opener.handlers
            if type(handler) is redirect_type
        )
        original = redirect_handler.redirect_request
        forged_calls: list[str] = []

        def allow_redirect(request, response, code, message, headers, new_url):
            forged_calls.append(new_url)
            return request

        redirect_handler.redirect_request = allow_redirect
        try:
            with pytest.raises(
                BetfairSupervisedExecutionError,
                match="canonical client, transport",
            ):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-private-opener-redirect-shadow",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )

            assert forged_calls == []
            assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
        finally:
            redirect_handler.redirect_request = original

