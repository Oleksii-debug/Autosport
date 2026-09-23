from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


_PROVIDER_ORDER_REF = "a" * 32
_MARKET_ID = "1.234"
_EVENT_ID = "event-1"


class _StepClock:
    def __init__(self) -> None:
        self._next = 0

    def __call__(self) -> datetime:
        value = datetime(2026, 9, 21, 18, 10, tzinfo=timezone.utc) + timedelta(
            milliseconds=self._next
        )
        self._next += 1
        return value


class _TransitioningReadbackTransport:
    """First sweep is empty; the exact order becomes visible immediately after it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.initial_empty_sweep_complete = False
        self.current_order_calls = 0

    @staticmethod
    def _response(request_id: int, result: object) -> bytes:
        return json.dumps(
            {"jsonrpc": "2.0", "result": result, "id": request_id},
            separators=(",", ":"),
        ).encode("utf-8")

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "request": request,
                "timeout_seconds": timeout_seconds,
            }
        )
        request_id = request["id"]
        method = request["method"]
        params = request["params"]

        if method == "SportsAPING/v1.0/listMarketCatalogue":
            return self._response(
                request_id,
                [{"marketId": _MARKET_ID, "event": {"id": _EVENT_ID}}],
            )

        if method == "SportsAPING/v1.0/listCurrentOrders":
            self.current_order_calls += 1
            assert params["customerOrderRefs"] == [_PROVIDER_ORDER_REF]
            assert params["marketIds"] == [_MARKET_ID]
            if self.initial_empty_sweep_complete:
                return self._response(
                    request_id,
                    {
                        "currentOrders": [
                            {
                                "betId": "bet-visible-after-reread",
                                "marketId": _MARKET_ID,
                                "selectionId": 42,
                                "side": "BACK",
                                "status": "EXECUTABLE",
                                "placedDate": "2026-09-21T18:09:59+00:00",
                                "priceSize": {"price": 2.0, "size": 10.0},
                                "averagePriceMatched": 2.0,
                                "sizeMatched": 5.0,
                                "sizeRemaining": 5.0,
                                "customerOrderRef": _PROVIDER_ORDER_REF,
                            }
                        ],
                        "moreAvailable": False,
                    },
                )
            return self._response(
                request_id,
                {"currentOrders": [], "moreAvailable": False},
            )

        if method == "SportsAPING/v1.0/listClearedOrders":
            assert params["customerOrderRefs"] == [_PROVIDER_ORDER_REF]
            assert params["marketIds"] == [_MARKET_ID]
            assert params["groupBy"] == "BET"
            result = {"clearedOrders": [], "moreAvailable": False}
            if (
                params["betStatus"] == "CANCELLED"
                and not self.initial_empty_sweep_complete
            ):
                self.initial_empty_sweep_complete = True
            return self._response(request_id, result)

        raise AssertionError(f"unexpected method: {method}")


def test_empty_execution_readback_cannot_mint_absence_across_immediate_transition():
    """A bounded reread must prevent stale all-empty evidence becoming absence truth."""

    transport = _TransitioningReadbackTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=_StepClock(),
    )

    try:
        capture = client.read_execution_readback(
            action_id="action-1",
            provider_order_ref=_PROVIDER_ORDER_REF,
            market_id=_MARKET_ID,
            page_size=1000,
            max_pages=4,
        )
    except BetfairReadOnlyError as exc:
        # A repair may fail closed instead of returning the newer positive state.
        message = str(exc).lower()
        assert any(
            marker in message
            for marker in ("coher", "ambiguous", "changed", "unstable", "transition")
        )
        assert transport.initial_empty_sweep_complete
        assert transport.current_order_calls >= 2
        return

    assert transport.current_order_calls >= 2, (
        "an all-empty current+cleared scan is not enough to prove durable absence; "
        "the exact receipt scope must be re-read before a canonical capture returns"
    )
    observed_refs = {
        order.customer_order_ref
        for page in capture.current_pages
        for order in page.orders
    }
    observed_refs.update(
        order.customer_order_ref
        for _, pages in capture.cleared_pages_by_status
        for page in pages
        for order in page.orders
    )
    assert _PROVIDER_ORDER_REF in observed_refs, (
        "if the client returns instead of failing closed after a transition, the "
        "returned canonical capture must reflect the newly visible provider order"
    )


class _ClearedTransitionReadbackTransport:
    """The exact order appears only in cleared state during the second sweep."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.current_calls = 0
        self.settled_calls = 0

    @staticmethod
    def _response(request_id: int, result: object) -> bytes:
        return json.dumps(
            {"jsonrpc": "2.0", "result": result, "id": request_id},
            separators=(",", ":"),
        ).encode("utf-8")

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body)
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "request": request,
                "timeout_seconds": timeout_seconds,
            }
        )
        request_id = request["id"]
        method = request["method"]
        params = request["params"]

        if method == "SportsAPING/v1.0/listMarketCatalogue":
            return self._response(
                request_id,
                [{"marketId": _MARKET_ID, "event": {"id": _EVENT_ID}}],
            )
        if method == "SportsAPING/v1.0/listCurrentOrders":
            self.current_calls += 1
            assert params["customerOrderRefs"] == [_PROVIDER_ORDER_REF]
            assert params["marketIds"] == [_MARKET_ID]
            return self._response(
                request_id,
                {"currentOrders": [], "moreAvailable": False},
            )
        if method == "SportsAPING/v1.0/listClearedOrders":
            assert params["customerOrderRefs"] == [_PROVIDER_ORDER_REF]
            assert params["marketIds"] == [_MARKET_ID]
            assert params["groupBy"] == "BET"
            if params["betStatus"] == "SETTLED":
                self.settled_calls += 1
                if self.settled_calls == 2:
                    return self._response(
                        request_id,
                        {
                            "clearedOrders": [
                                {
                                    "betId": "bet-cleared-during-reread",
                                    "marketId": _MARKET_ID,
                                    "selectionId": 42,
                                    "side": "BACK",
                                    "placedDate": "2026-09-21T18:09:59+00:00",
                                    "settledDate": "2026-09-21T18:10:01+00:00",
                                    "priceRequested": 2.0,
                                    "priceMatched": 2.0,
                                    "sizeSettled": 10.0,
                                    "profit": 10.0,
                                    "customerOrderRef": _PROVIDER_ORDER_REF,
                                    "eventId": _EVENT_ID,
                                }
                            ],
                            "moreAvailable": False,
                        },
                    )
            return self._response(
                request_id,
                {"clearedOrders": [], "moreAvailable": False},
            )
        raise AssertionError(f"unexpected method: {method}")


def test_empty_execution_readback_fails_closed_on_cleared_transition_during_reread():
    transport = _ClearedTransitionReadbackTransport()
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=_StepClock(),
    )

    with pytest.raises(
        BetfairReadOnlyError,
        match="changed during empty-sweep coherence check",
    ):
        client.read_execution_readback(
            action_id="action-1",
            provider_order_ref=_PROVIDER_ORDER_REF,
            market_id=_MARKET_ID,
            page_size=1000,
            max_pages=4,
        )

    assert transport.current_calls == 2
    assert transport.settled_calls == 2

