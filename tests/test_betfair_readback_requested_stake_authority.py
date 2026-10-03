from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
from types import FunctionType
from weakref import ref

import pytest

from betfair_execution_readback_test_support import semantic_execution_readback

import autosport.supervised_provider_evidence as provider_evidence

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.real_execution_ledger import ExecutionAction
from autosport.supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderEffectEvidence,
    assert_verified_provider_evidence_authoritative,
    verify_betfair_provider_state,
    _evaluate_betfair_provider_state_semantics,
)


PROVIDER_REF = "a" * 32
OBSERVED_AT = "2026-09-21T18:00:16+00:00"


class _ReadbackTransport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, body, timeout_seconds
        if not self.responses:
            raise AssertionError("unexpected Betfair readback call")
        return self.responses.pop(0)


def _rpc_result(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-requested-stake",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-requested-stake",
        quote_observed_at="2026-09-21T17:59:00+00:00",
        expires_at="2026-09-21T18:10:00+00:00",
    )


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at="2026-09-21T17:59:00+00:00",
        source_ref="betfair://profile/requested-stake-test",
        source_payload_sha256="a" * 64,
    )


def _capture_with_provider_requested_size(
    action: ExecutionAction,
    *,
    requested_size: float,
):
    current_order = {
        "betId": "bet-current-requested-stake",
        "marketId": action.market_id,
        "selectionId": int(action.selection_id),
        "side": action.side,
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T18:00:01+00:00",
        "priceSize": {
            "price": 2.0,
            "size": requested_size,
        },
        "averagePriceMatched": 2.0,
        "sizeMatched": 1.0,
        "sizeRemaining": requested_size - 1.0,
        "customerOrderRef": PROVIDER_REF,
    }
    responses = [
        _rpc_result(
            [{"marketId": action.market_id, "event": {"id": action.event_id}}],
            1,
        ),
        _rpc_result(
            {"currentOrders": [current_order], "moreAvailable": False},
            2,
        ),
    ]
    for request_id in range(3, 7):
        responses.append(
            _rpc_result(
                {"clearedOrders": [], "moreAvailable": False},
                request_id,
            )
        )

    return semantic_execution_readback(
        responses,
        action_id=action.action_id,
        market_id=action.market_id,
        provider_order_ref=PROVIDER_REF,
        account_id=action.account_id,
    )


def test_current_order_wrong_requested_size_cannot_mint_effect_evidence() -> None:
    action = _action()
    profile = _profile()
    capture = _capture_with_provider_requested_size(
        action,
        requested_size=11.0,
    )

    with pytest.raises(ProviderEvidenceError, match="requested (size|stake)"):
        _evaluate_betfair_provider_state_semantics(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=PROVIDER_REF,
        )


def test_provider_verifier_rejects_completeness_helper_rebind(monkeypatch) -> None:
    action = _action()
    profile = _profile()
    capture = _capture_with_provider_requested_size(
        action,
        requested_size=10.0,
    )
    hostile_called = False

    def hostile_complete_current_pages(_pages):
        nonlocal hostile_called
        hostile_called = True
        return (), "0" * 64, OBSERVED_AT

    monkeypatch.setattr(
        provider_evidence,
        "_complete_current_pages",
        hostile_complete_current_pages,
    )
    with pytest.raises(
        ProviderEvidenceError,
        match="provider evidence executable authority changed",
    ):
        verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=PROVIDER_REF,
        )
    assert hostile_called is False


def _unwrap_name_resolution_guard(function: FunctionType) -> FunctionType:
    current = function
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        closure = current.__closure__ or ()
        freevars = current.__code__.co_freevars
        if "root" not in freevars:
            return current
        candidate = closure[freevars.index("root")].cell_contents
        if not isinstance(candidate, FunctionType):
            return current
        current = candidate
    return current


def _reachable_closure_values(function: FunctionType) -> dict[str, list[object]]:
    found: dict[str, list[object]] = {}
    pending = [function]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        closure = current.__closure__ or ()
        for name, cell in zip(current.__code__.co_freevars, closure, strict=True):
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            found.setdefault(name, []).append(value)
            if isinstance(value, FunctionType):
                pending.append(value)
    return found


def test_provider_authority_expectations_are_immutable_closure_witnesses() -> None:
    roots = (
        _unwrap_name_resolution_guard(provider_evidence.verify_betfair_provider_state),
        _unwrap_name_resolution_guard(
            provider_evidence.assert_verified_provider_evidence_authoritative
        ),
    )
    found: dict[str, list[object]] = {}
    for root_function in roots:
        for name, values in _reachable_closure_values(root_function).items():
            found.setdefault(name, []).extend(values)

    expected_names = (
        "sealed_verify_graph",
        "sealed_fingerprint_graph",
        "sealed_wrapper_bindings",
        "sealed_profile_descriptors",
        "sealed_evidence_descriptors",
    )
    for name in expected_names:
        assert name in found, f"missing provider authority witness: {name}"
        assert all(type(value) is tuple for value in found[name]), (
            f"provider authority witness remains caller-mutable: {name}"
        )


def test_equal_caller_copy_cannot_mint_provider_evidence_authority() -> None:
    action = _action()
    profile = _profile()
    capture = _capture_with_provider_requested_size(
        action,
        requested_size=10.0,
    )
    evidence = _evaluate_betfair_provider_state_semantics(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )

    assert isinstance(evidence, VerifiedProviderEffectEvidence)
    assert_verified_provider_evidence_authoritative(evidence)

    forged = replace(evidence)
    assert forged == evidence
    with pytest.raises(ProviderEvidenceError, match="origin authority"):
        assert_verified_provider_evidence_authoritative(forged)


def test_reachable_closure_dict_injection_cannot_mint_provider_authority() -> None:
    action = _action()
    profile = _profile()
    capture = _capture_with_provider_requested_size(
        action,
        requested_size=10.0,
    )
    evidence = _evaluate_betfair_provider_state_semantics(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=PROVIDER_REF,
    )
    forged = replace(evidence)
    fingerprint = provider_evidence._verified_provider_evidence_fingerprint(forged)

    roots = (
        _unwrap_name_resolution_guard(provider_evidence.verify_betfair_provider_state),
        _unwrap_name_resolution_guard(
            provider_evidence.assert_verified_provider_evidence_authoritative
        ),
    )
    attacked = 0
    for root_function in roots:
        for cell in root_function.__closure__ or ():
            try:
                candidate = cell.cell_contents
            except ValueError:
                continue
            if (
                type(candidate) is dict
                and candidate
                and all(type(key) is int for key in candidate)
            ):
                attacked += 1
                key = id(forged)
                prior = candidate.get(key)
                had_prior = key in candidate
                candidate[key] = (ref(forged), fingerprint)
                try:
                    with pytest.raises(ProviderEvidenceError):
                        provider_evidence.assert_verified_provider_evidence_authoritative(
                            forged
                        )
                finally:
                    if had_prior:
                        candidate[key] = prior
                    else:
                        candidate.pop(key, None)

    # Old registry-backed authority necessarily exposed at least one such map after
    # issuing the legitimate evidence; the repaired origin-reverification path may
    # expose none. Either way the caller-created equal object is never authoritative.
    with pytest.raises(ProviderEvidenceError):
        provider_evidence.assert_verified_provider_evidence_authoritative(forged)
    assert attacked == 0
