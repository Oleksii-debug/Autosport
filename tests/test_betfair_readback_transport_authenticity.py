from __future__ import annotations

import runpy
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)
from autosport.supervised_provider_evidence import (
    ProviderEvidenceError,
    _evaluate_betfair_provider_state_semantics,
    assert_verified_provider_evidence_authoritative,
    verify_betfair_provider_state,
)


_HELPERS = runpy.run_path(
    str(
        Path(__file__).with_name(
            "test_betfair_readback_requested_price_authority.py"
        )
    )
)
_action = _HELPERS["_action"]
_profile = _HELPERS["_profile"]
_capture = _HELPERS["_capture"]
PROVIDER_REF = _HELPERS["PROVIDER_REF"]


def test_injected_readback_transport_cannot_mint_provider_effect_authority() -> None:
    action = _action()
    profile = _profile()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )

    with pytest.raises(ProviderEvidenceError):
        verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=PROVIDER_REF,
        )

def test_injected_transport_semantic_result_is_not_transferable_provider_authority() -> None:
    action = _action()
    profile = _profile()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )

    evidence = _evaluate_betfair_provider_state_semantics(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )

    with pytest.raises(
        ProviderEvidenceError,
        match="not issued by canonical verifier",
    ):
        assert_verified_provider_evidence_authoritative(evidence)

def _find_closure_value(root, predicate):
    pending = [root]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        if predicate(current):
            return current
        closure = getattr(current, "__closure__", None)
        if closure is None:
            continue
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if predicate(value):
                return value
            if callable(value) and getattr(value, "__closure__", None) is not None:
                pending.append(value)
    raise AssertionError("expected authority closure value was not found")


def _trusted_capture_matcher():
    return _find_closure_value(
        BetfairReadOnlyClient.read_execution_readback,
        lambda value: (
            callable(value)
            and getattr(value, "__name__", "")
            == "trusted_capture_matches"
        ),
    )


def _private_authority_opener():
    return _find_closure_value(
        BetfairReadOnlyClient._rpc,
        lambda value: (
            type(value).__module__ == "urllib.request"
            and hasattr(value, "_open")
            and hasattr(value, "_call_chain")
            and hasattr(value, "handlers")
        ),
    )


def _current_capture_witnesses(capture):
    current_order = capture.current_pages[0].orders[0]
    current_result = {
        "currentOrders": [
            {
                "betId": current_order.bet_id,
                "marketId": current_order.market_id,
                "selectionId": current_order.selection_id,
                "side": current_order.side,
                "status": current_order.status,
                "placedDate": current_order.placed_date,
                "priceSize": {
                    "price": current_order.price,
                    "size": current_order.requested_size,
                },
                "averagePriceMatched": current_order.average_price_matched,
                "sizeMatched": current_order.size_matched,
                "sizeRemaining": current_order.size_remaining,
                "customerOrderRef": current_order.customer_order_ref,
            }
        ],
        "moreAvailable": False,
    }
    witnesses = [
        (
            "SportsAPING/v1.0/listMarketCatalogue",
            {
                "filter": {"marketIds": [capture.market_id]},
                "marketProjection": ["EVENT"],
                "maxResults": 1,
            },
            [
                {
                    "marketId": capture.market_id,
                    "event": {"id": capture.market_event.event_id},
                }
            ],
            capture.market_event.evidence,
        ),
        (
            "SportsAPING/v1.0/listCurrentOrders",
            {
                "orderProjection": "ALL",
                "fromRecord": 0,
                "recordCount": capture.page_size,
                "customerOrderRefs": [capture.provider_order_ref],
                "marketIds": [capture.market_id],
            },
            current_result,
            capture.current_pages[0].evidence,
        ),
    ]
    for status, pages in capture.cleared_pages_by_status:
        witnesses.append(
            (
                "SportsAPING/v1.0/listClearedOrders",
                {
                    "betStatus": status,
                    "groupBy": "BET",
                    "fromRecord": 0,
                    "recordCount": capture.page_size,
                    "customerOrderRefs": [capture.provider_order_ref],
                    "marketIds": [capture.market_id],
                },
                {"clearedOrders": [], "moreAvailable": False},
                pages[0].evidence,
            )
        )
    return witnesses


def test_private_opener_internal_dispatch_shadow_fails_before_network() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
    )
    opener = _private_authority_opener()
    called = False

    def synthetic_open(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("synthetic opener must never be trusted")

    opener._open = synthetic_open
    try:
        with pytest.raises(
            BetfairReadOnlyError,
            match="canonical Betfair network authority changed",
        ):
            client.read_execution_readback(
                action_id="action-private-opener-falsifier",
                market_id="1.234",
            )
    finally:
        del opener._open

    assert called is False


def test_private_opener_handler_method_rebind_fails_before_network() -> None:
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
    )
    opener = _private_authority_opener()
    https_handler = next(
        handler
        for handler in opener.handlers
        if type(handler).__name__ == "HTTPSHandler"
    )
    handler_type = type(https_handler)
    original = handler_type.https_open
    called = False

    def synthetic_https_open(self, request):
        nonlocal called
        called = True
        raise AssertionError("synthetic HTTPS handler must never be trusted")

    handler_type.https_open = synthetic_https_open
    try:
        with pytest.raises(
            BetfairReadOnlyError,
            match="canonical Betfair network authority changed",
        ):
            client.read_execution_readback(
                action_id="action-handler-method-falsifier",
                market_id="1.234",
            )
    finally:
        handler_type.https_open = original

    assert called is False


def test_trusted_network_witness_rejects_envelope_account_relabel() -> None:
    action = _action()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )
    matcher = _trusted_capture_matcher()
    witnesses = _current_capture_witnesses(capture)
    original_account = capture.account_id

    object.__setattr__(capture, "account_id", "forged-account")
    try:
        assert not matcher(
            capture,
            witnesses,
            venue_id=capture.venue_id,
            account_id=original_account,
            action_id=capture.action_id,
            market_id=capture.market_id,
            provider_order_ref=capture.provider_order_ref,
            page_size=capture.page_size,
        )
    finally:
        object.__setattr__(capture, "account_id", original_account)


def test_trusted_network_witness_rejects_forged_current_dto_field() -> None:
    action = _action()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )
    matcher = _trusted_capture_matcher()
    witnesses = _current_capture_witnesses(capture)

    assert matcher(
        capture,
        witnesses,
        venue_id=capture.venue_id,
        account_id=capture.account_id,
        action_id=capture.action_id,
        market_id=capture.market_id,
        provider_order_ref=capture.provider_order_ref,
        page_size=capture.page_size,
    )

    order = capture.current_pages[0].orders[0]
    original = order.average_price_matched
    object.__setattr__(
        order,
        "average_price_matched",
        Decimal("999"),
    )
    try:
        assert not matcher(
            capture,
            witnesses,
            venue_id=capture.venue_id,
            account_id=capture.account_id,
            action_id=capture.action_id,
            market_id=capture.market_id,
            provider_order_ref=capture.provider_order_ref,
            page_size=capture.page_size,
        )
    finally:
        object.__setattr__(
            order,
            "average_price_matched",
            original,
        )


def test_trusted_network_witness_rejects_status_remaining_contradiction() -> None:
    action = _action()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )
    matcher = _trusted_capture_matcher()
    witnesses = _current_capture_witnesses(capture)
    order = capture.current_pages[0].orders[0]

    assert order.status == "EXECUTABLE"
    assert order.size_remaining > 0

    # Model a transient bypass of the separately composed DTO __post_init__ guard:
    # the trusted provider bytes and forged exact-type DTO agree on the same
    # contradictory tuple. The closure-owned byte re-derivation must independently
    # enforce the provider state relation rather than accepting field equality alone.
    original_status = order.status
    object.__setattr__(order, "status", "EXECUTION_COMPLETE")
    witnesses[1][2]["currentOrders"][0]["status"] = "EXECUTION_COMPLETE"
    try:
        with pytest.raises(
            BetfairReadOnlyError,
            match="EXECUTION_COMPLETE current order must have zero size_remaining",
        ):
            matcher(
                capture,
                witnesses,
                venue_id=capture.venue_id,
                account_id=capture.account_id,
                action_id=capture.action_id,
                market_id=capture.market_id,
                provider_order_ref=capture.provider_order_ref,
                page_size=capture.page_size,
            )
    finally:
        object.__setattr__(order, "status", original_status)


def test_trusted_network_witness_uses_exact_current_size_upper_bound() -> None:
    action = _action()
    capture = _capture(
        action,
        surface="current",
        provider_requested_price=2.0,
    )
    matcher = _trusted_capture_matcher()
    witnesses = _current_capture_witnesses(capture)
    order = capture.current_pages[0].orders[0]

    requested = Decimal("1")
    matched = Decimal("0.50000000000000000000000000005")
    remaining = Decimal("0.5")
    original = (
        order.requested_size,
        order.size_matched,
        order.size_remaining,
    )
    object.__setattr__(order, "requested_size", requested)
    object.__setattr__(order, "size_matched", matched)
    object.__setattr__(order, "size_remaining", remaining)
    raw = witnesses[1][2]["currentOrders"][0]
    raw["priceSize"]["size"] = requested
    raw["sizeMatched"] = matched
    raw["sizeRemaining"] = remaining
    try:
        with localcontext() as context:
            context.prec = 28
            # Context-sensitive addition rounds the exact overage back to 1.
            assert matched + remaining == requested
            with pytest.raises(
                BetfairReadOnlyError,
                match="size_matched plus size_remaining cannot exceed requested_size",
            ):
                matcher(
                    capture,
                    witnesses,
                    venue_id=capture.venue_id,
                    account_id=capture.account_id,
                    action_id=capture.action_id,
                    market_id=capture.market_id,
                    provider_order_ref=capture.provider_order_ref,
                    page_size=capture.page_size,
                )
    finally:
        object.__setattr__(order, "requested_size", original[0])
        object.__setattr__(order, "size_matched", original[1])
        object.__setattr__(order, "size_remaining", original[2])

