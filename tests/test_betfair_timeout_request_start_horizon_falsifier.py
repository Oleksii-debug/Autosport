from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json

import autosport.betfair_timeout_reconciliation as timeout_resolution
import autosport.real_execution_ledger as ledger_module
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_timeout_reconciliation import (
    BetfairTimeoutResolutionKind,
    resolve_betfair_timeout_provider_state,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


_TIMEOUT_BOUNDARY = "2026-09-21T18:00:00+00:00"
_BEFORE_DEADLINE = "2026-09-21T18:00:14.900000+00:00"
_AFTER_DEADLINE = "2026-09-21T18:00:15.100000+00:00"


class _ControlledClock:
    def __init__(self, value: str) -> None:
        self.value = datetime.fromisoformat(value)

    def __call__(self) -> datetime:
        return self.value

    def set(self, value: str) -> None:
        self.value = datetime.fromisoformat(value)


class _CrossingReadbackTransport:
    """Make currentOrders start pre-horizon but return after the horizon."""

    def __init__(self, clock: _ControlledClock) -> None:
        self.clock = clock
        self.current_request_started_at: str | None = None

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, timeout_seconds
        request = json.loads(body.decode("utf-8"))
        method = request["method"]
        request_id = request["id"]

        if method.endswith("listMarketCatalogue"):
            # The next request begins just before the timeout visibility deadline.
            self.clock.set(_BEFORE_DEADLINE)
            result: object = [
                {"marketId": "1.234", "event": {"id": "event-1"}}
            ]
        elif method.endswith("listCurrentOrders"):
            self.current_request_started_at = self.clock().isoformat()
            # Simulate transport latency carrying this already-started request over
            # the provider's +15s visibility horizon before the response is stamped.
            self.clock.set(_AFTER_DEADLINE)
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            result = {"clearedOrders": [], "moreAvailable": False}
        else:  # pragma: no cover - the strict adapter must not call another method.
            raise AssertionError(f"unexpected Betfair method {method}")

        return json.dumps(
            {"jsonrpc": "2.0", "result": result, "id": request_id},
            separators=(",", ":"),
        ).encode("utf-8")


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        quote_id="quote-1",
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
        source_ref="betfair://profile/request-start-horizon-test",
        source_payload_sha256="a" * 64,
    )


def _ledger_with_ambiguous_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "_now", lambda: _TIMEOUT_BOUNDARY)
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T17:59:01+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(tmp_path / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T17:59:50+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-21T17:59:55+00:00",
    )
    ledger.mark_unknown(
        "attempt-1",
        reason="betfair_placeOrders_ambiguous_effect_requires_readback",
        observed_at="2026-09-21T17:59:57+00:00",
    )
    return ledger, action, provider_ref


def test_pre_horizon_rpc_start_cannot_become_definitive_absence_from_late_response(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref = _ledger_with_ambiguous_timeout(
        tmp_path, monkeypatch
    )
    clock = _ControlledClock("2026-09-21T18:00:14.800000+00:00")
    transport = _CrossingReadbackTransport(clock)

    class _AuthorityDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock()
            return value if tz is None else value.astimezone(tz)

    # Production captures the real system clock, not the adapter's injectable
    # response clock.  The test controls that independent authority clock only to
    # deterministically place the canonical capture start before the +15s boundary.
    monkeypatch.setattr(timeout_resolution, "datetime", _AuthorityDateTime)

    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=transport,
        clock=clock,
        venue_id="betfair",
        account_id="acct-1",
    )
    readback = client.read_execution_readback(
        action_id=action.action_id,
        market_id=action.market_id,
        provider_order_ref=provider_ref,
    )
    profile = _profile()

    assert transport.current_request_started_at == _BEFORE_DEADLINE
    # Current adapter evidence is stamped after transport.post() returns, so this
    # assertion documents that response observation alone crossed the deadline.
    assert readback.current_pages[0].evidence.observed_at == _AFTER_DEADLINE

    result = resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=readback,
    )

    assert (
        result.kind
        is BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert result.evidence is None
