from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.execution.feasibility import (
    FeasibilityState,
    assess_authoritative_betfair_execution_feasibility,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.supervised_execution import (
    BoundSupervisedExecutionPlan,
    ExecutionLegConstraint,
    ProfileBinding,
    _bound_binding_sha256,
)

NOW = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)
ACTION_ID = "a" * 64


class MarketBookTransport:
    def __init__(
        self,
        *,
        back_sizes: tuple[tuple[str, str], ...],
    ) -> None:
        self.back_sizes = back_sizes
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        self.calls.append(request)
        assert request["method"] == "SportsAPING/v1.0/listMarketBook"
        assert request["params"] == {
            "marketIds": ["1.234"],
            "priceProjection": {
                "priceData": ["EX_ALL_OFFERS"],
                "virtualise": False,
            },
        }
        assert headers["X-Application"] == "app-key"
        assert headers["X-Authentication"] == "session-token"
        back = ",".join(
            f'{{"price":{price},"size":{size}}}'
            for price, size in self.back_sizes
        )
        return (
            '{"jsonrpc":"2.0","id":'
            + str(request["id"])
            + ',"result":[{"marketId":"1.234","isMarketDataDelayed":false,'
            + '"status":"OPEN","version":17,"inplay":false,"betDelay":0,'
            + '"runners":[{"selectionId":42,"status":"ACTIVE","ex":'
            + '{"availableToBack":['
            + back
            + '],"availableToLay":[]}}]}]}'
        ).encode("utf-8")


class _FakeUrlResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeUrlResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


def _canonical_client(
    monkeypatch: pytest.MonkeyPatch,
    transport: MarketBookTransport,
) -> BetfairReadOnlyClient:
    def fake_urlopen(request, timeout: float) -> _FakeUrlResponse:
        headers = {key.lower(): value for key, value in request.header_items()}
        payload = transport.post(
            request.full_url,
            headers={
                "X-Application": headers["x-application"],
                "X-Authentication": headers["x-authentication"],
            },
            body=request.data or b"",
            timeout_seconds=timeout,
        )
        return _FakeUrlResponse(payload)

    monkeypatch.setattr(betfair_account_readonly, "urlopen", fake_urlopen)
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        venue_id="betfair",
        account_id="acct-1",
    )


def _bound(
    decision_at: datetime,
    *,
    requested_odds: Decimal,
    requested_stake: Decimal,
) -> BoundSupervisedExecutionPlan:
    quote_at = decision_at - timedelta(seconds=1)
    expires_at = decision_at + timedelta(seconds=5)
    action = ExecutionAction(
        action_id=ACTION_ID,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        side="BACK",
        requested_odds=requested_odds,
        requested_stake=requested_stake,
        quote_id="quote-1",
        quote_observed_at=quote_at.isoformat(),
        expires_at=expires_at.isoformat(),
    )
    provisional = ExecutionPlan(
        plan_id="provisional",
        bookmaker_profile_version="profile-set-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=quote_at.isoformat(),
        actions=(action,),
    )
    bindings = (
        ProfileBinding(
            venue_id="betfair",
            account_id="acct-1",
            adapter_id="betfair-exchange-jsonrpc-readonly",
            adapter_version="1",
            profile_version=1,
            profile_sha256="b" * 64,
        ),
    )
    constraints = (
        ExecutionLegConstraint(
            leg_id=ACTION_ID,
            side="BACK",
            quote_expires_at=expires_at.isoformat(),
            max_slippage_fraction=Decimal("0"),
        ),
    )
    binding_sha = _bound_binding_sha256(
        provisional,
        "c" * 64,
        "d" * 64,
        "intent-1",
        "e" * 64,
        "f" * 64,
        bindings,
        constraints,
    )
    plan = ExecutionPlan(
        plan_id=f"supervised-v2-{binding_sha}",
        bookmaker_profile_version=provisional.bookmaker_profile_version,
        decision_id=provisional.decision_id,
        approval_id=provisional.approval_id,
        created_at=provisional.created_at,
        actions=provisional.actions,
    )
    return BoundSupervisedExecutionPlan(
        execution_plan=plan,
        portfolio_plan_sha256="c" * 64,
        economic_goal_contract_sha256="d" * 64,
        intent_id="intent-1",
        intent_sha256="e" * 64,
        approval_fingerprint="f" * 64,
        profile_bindings=bindings,
        constraints=constraints,
    )


def _assess(
    tmp: str,
    bound: BoundSupervisedExecutionPlan,
    receipt: object,
    *,
    decision_at: datetime,
):
    ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    return assess_authoritative_betfair_execution_feasibility(
        ledger,
        bound,
        receipt,
        action_id=ACTION_ID,
        decision_at=decision_at,
        max_snapshot_age=timedelta(seconds=2),
    )


def test_unknown_price_ladder_cannot_mint_positive_standard_limit_admissibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile identity is not provider price-ladder authority.

    At 2.01 a CLASSIC ladder would reject the submitted price, while another
    provider ladder can have different semantics. Because #761 does not carry
    canonical MARKET_DESCRIPTION / priceLadderDescription evidence, it cannot
    prove that this ordinary LIMIT price is admissible and must fail closed.
    """

    transport = MarketBookTransport(back_sizes=(("2.02", "100"), ("2.00", "100")))
    receipt = _canonical_client(monkeypatch, transport).read_market_book_depth(
        "1.234", 42
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(
        decision_at,
        requested_odds=Decimal("2.01"),
        requested_stake=Decimal("12"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        result = _assess(tmp, bound, receipt, decision_at=decision_at)

    assert result.state is not FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY


def test_unknown_currency_jurisdiction_minimum_rule_cannot_mint_positive_admissibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile binding cannot stand in for Betfair minimum-stake rule evidence.

    Betfair standard LIMIT minimum admissibility is currency/jurisdiction/rule
    dependent and can include the documented Min Bet Payout exception. With no
    canonical account currency/region + versioned provider-rule evidence, a
    sub-unit stake cannot be classified as positively admissible merely because
    displayed market depth exists.
    """

    transport = MarketBookTransport(back_sizes=(("10.00", "100"),))
    receipt = _canonical_client(monkeypatch, transport).read_market_book_depth(
        "1.234", 42
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(
        decision_at,
        requested_odds=Decimal("10.00"),
        requested_stake=Decimal("0.50"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        result = _assess(tmp, bound, receipt, decision_at=decision_at)

    assert result.state is not FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY
