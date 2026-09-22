from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import pickle
from pathlib import Path
import tempfile

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
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


NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
READ_AT = NOW - timedelta(milliseconds=100)
ACTION_ID = "a" * 64


class MarketBookTransport:
    def __init__(
        self,
        *,
        delayed: bool = False,
        back_sizes: tuple[tuple[str, str], ...] = (
            ("2.10", "5"),
            ("2.04", "4"),
            ("2.00", "6"),
        ),
        lay_sizes: tuple[tuple[str, str], ...] = (("1.99", "999"),),
        selection_status: str = "ACTIVE",
    ) -> None:
        self.delayed = delayed
        self.back_sizes = back_sizes
        self.lay_sizes = lay_sizes
        self.selection_status = selection_status
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
        lay = ",".join(
            f'{{"price":{price},"size":{size}}}'
            for price, size in self.lay_sizes
        )
        delayed = "true" if self.delayed else "false"
        return (
            '{"jsonrpc":"2.0","id":'
            + str(request["id"])
            + ',"result":[{"marketId":"1.234","isMarketDataDelayed":'
            + delayed
            + ',"status":"OPEN","version":17,"inplay":false,"betDelay":0,'
            + '"runners":[{"selectionId":42,"status":"'
            + self.selection_status
            + '","ex":{"availableToBack":['
            + back
            + '],"availableToLay":['
            + lay
            + "]}}]}]}"
        ).encode("utf-8")


def _bound(decision_at: datetime = NOW) -> BoundSupervisedExecutionPlan:
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
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("12"),
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


def _canonical_client() -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        venue_id="betfair",
        account_id="acct-1",
    )


def _synthetic_authoritative_receipt(
    transport: MarketBookTransport,
    *,
    observed_at: datetime | None = None,
) -> tuple[object, BetfairReadOnlyClient]:
    """Unit-test the resolver with a receipt issued under the production origin seal.

    Parsing is exercised through an injected non-authoritative transport; the private
    issuance seam then models the point after successful canonical provider I/O.
    Production callers cannot obtain positive authority from that injected transport.
    """
    observed = observed_at or datetime.now(timezone.utc)
    parsing_client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: observed,
        venue_id="betfair",
        account_id="acct-1",
    )
    receipt = parsing_client.read_market_book_depth("1.234", 42)
    canonical_source = _canonical_client()
    betfair_account_readonly._issue_market_book_depth(
        receipt,
        source=canonical_source,
    )
    return receipt, canonical_source

def _client(transport: MarketBookTransport) -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=lambda: READ_AT,
        venue_id="betfair",
        account_id="acct-1",
    )


def _reserved_ledger(tmp: str, bound: BoundSupervisedExecutionPlan) -> RealExecutionLedger:
    ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
    ledger.reserve_plan(bound.execution_plan)
    return ledger


def test_authenticated_market_book_receipt_can_issue_racy_positive_depth() -> None:
    transport = MarketBookTransport()
    receipt, canonical_source = _synthetic_authoritative_receipt(transport)
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            decision_at=decision_at,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert result.state is FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY
    assert result.sufficient is True
    assert result.displayed_acceptable_depth == Decimal("15")
    assert result.reasons == ()
    assert len(result.evidence_digest) == 64
    assert len(result.liquidity_overlap_key) == 64
    assert transport.calls

    # Authority is process-local issuance over the exact result object/content.
    # Structurally identical copies or durable reconstruction remain descriptive.
    assert replace(result).sufficient is False
    assert pickle.loads(pickle.dumps(result)).sufficient is False

    object.__setattr__(result, "snapshot_id", result.snapshot_id + "-tampered")
    assert result.sufficient is False


def test_forged_structurally_equal_receipt_cannot_issue_positive_truth() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    forged = replace(receipt)
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        with pytest.raises(
            BetfairReadOnlyError,
            match="lacks canonical direct Betfair provider IO origin",
        ):
            assess_authoritative_betfair_execution_feasibility(
                ledger,
                bound,
                forged,
                action_id=ACTION_ID,
                decision_at=decision_at,
                max_snapshot_age=timedelta(seconds=2),
            )


def test_response_level_delayed_data_fails_closed() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport(delayed=True)
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            decision_at=decision_at,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "DELAYED_SOURCE" in result.reasons


def test_non_active_runner_fails_closed() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport(selection_status="REMOVED")
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            decision_at=decision_at,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "SELECTION_NOT_ACTIVE" in result.reasons


def test_back_uses_available_to_back_not_available_to_lay() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport(
            back_sizes=(("2.00", "3"), ("1.99", "100")),
            lay_sizes=(("2.10", "999"),),
        )
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            decision_at=decision_at,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.displayed_acceptable_depth == Decimal("3")
    assert "DISPLAYED_DEPTH_INSUFFICIENT" in result.reasons


def test_unreserved_bound_plan_cannot_cross_product_authority_seam() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
        with pytest.raises(ValueError, match="durably reserved"):
            assess_authoritative_betfair_execution_feasibility(
                ledger,
                bound,
                receipt,
                action_id=ACTION_ID,
                decision_at=decision_at,
                max_snapshot_age=timedelta(seconds=2),
            )


def test_injected_transport_and_clock_cannot_mint_positive_provider_origin() -> None:
    receipt = _client(MarketBookTransport()).read_market_book_depth("1.234", 42)
    bound = _bound()

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(
            BetfairReadOnlyError,
            match="lacks canonical direct Betfair provider IO origin",
        ):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                decision_at=NOW,
                max_snapshot_age=timedelta(seconds=1),
            )


@pytest.mark.parametrize("mutated_field", ["transport", "clock"])
def test_post_construction_io_origin_swap_cannot_mint_positive_authority(
    monkeypatch: pytest.MonkeyPatch,
    mutated_field: str,
) -> None:
    transport = MarketBookTransport()
    receipt = _client(transport).read_market_book_depth("1.234", 42)
    client = _canonical_client()
    betfair_account_readonly._issue_market_book_depth(receipt, source=client)
    if mutated_field == "transport":
        client._transport = transport
    else:
        client._clock = lambda: READ_AT
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(
            BetfairReadOnlyError,
            match="lacks canonical direct Betfair provider IO origin",
        ):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                decision_at=decision_at,
                max_snapshot_age=timedelta(seconds=2),
            )

def test_module_urlopen_swap_invalidates_canonical_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    monkeypatch.setattr(
        betfair_account_readonly,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("patched urlopen must never become canonical provider IO")
        ),
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(
            BetfairReadOnlyError,
            match="lacks canonical direct Betfair provider IO origin",
        ):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                decision_at=decision_at,
                max_snapshot_age=timedelta(seconds=2),
            )

