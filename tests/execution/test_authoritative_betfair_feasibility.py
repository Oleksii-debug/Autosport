from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import time
import urllib.request as urllib_request

import pytest

import autosport.betfair_account_readonly as betfair_account_readonly
import autosport.execution.feasibility as feasibility_module
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
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
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
        before_response=None,
    ) -> None:
        self.delayed = delayed
        self.back_sizes = back_sizes
        self.lay_sizes = lay_sizes
        self.selection_status = selection_status
        self.before_response = before_response
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
        if self.before_response is not None:
            self.before_response()
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


def _bound(
    decision_at: datetime = NOW,
    *,
    requested_odds: Decimal = Decimal("2.00"),
    requested_stake: Decimal = Decimal("12"),
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


def _two_leg_bound(
    decision_at: datetime,
) -> BoundSupervisedExecutionPlan:
    base = _bound(decision_at)
    first = base.execution_plan.actions[0]
    second = replace(
        first,
        action_id="b" * 64,
        quote_id="quote-2",
    )
    provisional = ExecutionPlan(
        plan_id="provisional-two-leg",
        bookmaker_profile_version=base.execution_plan.bookmaker_profile_version,
        decision_id=base.execution_plan.decision_id,
        approval_id=base.execution_plan.approval_id,
        created_at=base.execution_plan.created_at,
        actions=(first, second),
    )
    constraints = (
        base.constraints[0],
        replace(base.constraints[0], leg_id=second.action_id),
    )
    binding_sha = _bound_binding_sha256(
        provisional,
        base.portfolio_plan_sha256,
        base.economic_goal_contract_sha256,
        base.intent_id,
        base.intent_sha256,
        base.approval_fingerprint,
        base.profile_bindings,
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
        portfolio_plan_sha256=base.portfolio_plan_sha256,
        economic_goal_contract_sha256=base.economic_goal_contract_sha256,
        intent_id=base.intent_id,
        intent_sha256=base.intent_sha256,
        approval_fingerprint=base.approval_fingerprint,
        profile_bindings=base.profile_bindings,
        constraints=constraints,
    )


def _canonical_client() -> BetfairReadOnlyClient:
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        venue_id="betfair",
        account_id="acct-1",
    )


class _BytesResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def read(self, amount: int = -1) -> bytes:
        return self._payload if amount < 0 else self._payload[:amount]


class _CanonicalUrlOpenerHarness:
    def __init__(self, transport: MarketBookTransport) -> None:
        self._transport = transport

    def open(self, request, data=None, timeout=None):
        header_items = {key.lower(): value for key, value in request.header_items()}
        payload = self._transport.post(
            request.full_url,
            headers={
                "X-Application": header_items["x-application"],
                "X-Authentication": header_items["x-authentication"],
                "Content-Type": header_items["content-type"],
            },
            body=request.data or b"",
            timeout_seconds=float(timeout),
        )
        return _BytesResponse(payload)


def _synthetic_authoritative_receipt(
    transport: MarketBookTransport,
) -> tuple[object, BetfairReadOnlyClient]:
    """Exercise the canonical MarketBook read path without external network IO.

    The production module's captured urlopen function, default transport/method and
    product clock remain unchanged. Only urllib's test-process opener is temporarily
    replaced below that captured function, so authority still originates from
    BetfairReadOnlyClient.read_market_book_depth rather than a callable mint seam.
    """
    original_opener = urllib_request._opener
    try:
        urllib_request._opener = _CanonicalUrlOpenerHarness(transport)
        canonical_source = _canonical_client()
        receipt = canonical_source.read_market_book_depth("1.234", 42)
    finally:
        urllib_request._opener = original_opener
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


def test_authentic_market_book_receipt_before_bound_quote_fails_closed() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(MarketBookTransport())
    observed_at = datetime.fromisoformat(receipt.evidence.observed_at)
    decision_at = observed_at + timedelta(seconds=2)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(
            ValueError,
            match="market-book depth observation predates durable execution quote",
        ):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                max_snapshot_age=timedelta(seconds=3),
            )

def test_authenticated_market_book_receipt_cannot_bypass_provider_limit_authority() -> None:
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
            max_snapshot_age=timedelta(seconds=2),
        )

    # Canonical provider depth plus matching adapter/profile identity is not
    # provider/account/currency/market standard-LIMIT admissibility authority.
    # Keep the real displayed-depth observation, but do not mint positive
    # executable-feasibility truth until that separate authority is composed.
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False
    assert result.displayed_acceptable_depth == Decimal("15")
    assert "LIMIT_AUTHORITY_REJECTED" in result.reasons
    assert len(result.evidence_digest) == 64
    assert len(result.liquidity_overlap_key) == 64
    assert transport.calls


def test_unknown_market_price_ladder_cannot_mint_positive_admissibility() -> None:
    transport = MarketBookTransport(
        back_sizes=(("2.02", "100"), ("2.00", "100")),
    )
    receipt, canonical_source = _synthetic_authoritative_receipt(transport)
    decision_at = datetime.now(timezone.utc)
    bound = _bound(
        decision_at,
        requested_odds=Decimal("2.01"),
        requested_stake=Decimal("12"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    # 2.01 is not a valid CLASSIC tick above 2.00, but other market ladders
    # differ. Without canonical MarketDescription/price-ladder evidence the
    # product cannot positively prove this LIMIT price admissible.
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False
    assert result.displayed_acceptable_depth == Decimal("100")
    assert "LIMIT_AUTHORITY_REJECTED" in result.reasons


def test_unknown_currency_jurisdiction_minimum_rule_cannot_mint_positive_admissibility() -> None:
    transport = MarketBookTransport(back_sizes=(("10.00", "100"),))
    receipt, canonical_source = _synthetic_authoritative_receipt(transport)
    decision_at = datetime.now(timezone.utc)
    bound = _bound(
        decision_at,
        requested_odds=Decimal("10.00"),
        requested_stake=Decimal("0.50"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    # Minimum stake / minimum payout rules depend on authenticated account
    # currency and jurisdiction. Profile identity cannot substitute for that
    # versioned provider rule evidence.
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False
    assert result.displayed_acceptable_depth == Decimal("100")
    assert "LIMIT_AUTHORITY_REJECTED" in result.reasons

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
                max_snapshot_age=timedelta(seconds=1),
            )


@pytest.mark.parametrize("mutated_field", ["transport", "clock"])
def test_post_construction_io_origin_swap_cannot_mint_positive_authority(
    monkeypatch: pytest.MonkeyPatch,
    mutated_field: str,
) -> None:
    transport = MarketBookTransport()
    receipt, client = _synthetic_authoritative_receipt(transport)
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
                max_snapshot_age=timedelta(seconds=2),
            )



def test_urlopen_kwdefault_and_module_alias_substitution_cannot_mint_provider_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = MarketBookTransport()
    harness = _CanonicalUrlOpenerHarness(transport)
    substituted_urlopen = harness.open
    canonical_post = betfair_account_readonly.UrllibBetfairHttpTransport.post
    original_kwdefaults = canonical_post.__kwdefaults__ or {}
    assert original_kwdefaults.get("_urlopen") is not None

    mutated_kwdefaults = dict(original_kwdefaults)
    mutated_kwdefaults["_urlopen"] = substituted_urlopen
    monkeypatch.setattr(canonical_post, "__kwdefaults__", mutated_kwdefaults)
    monkeypatch.setattr(
        betfair_account_readonly,
        "urlopen",
        substituted_urlopen,
    )

    source = _canonical_client()
    receipt = source.read_market_book_depth("1.234", 42)
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
                max_snapshot_age=timedelta(seconds=2),
            )



def test_instance_rpc_substitution_cannot_mint_market_book_authority() -> None:
    source = _canonical_client()
    called = False

    def forged_rpc(method, params):
        nonlocal called
        called = True
        raise AssertionError("forged RPC must not execute")

    source._rpc = forged_rpc
    with pytest.raises(
        BetfairReadOnlyError,
        match="canonical MarketBook RPC dispatch changed",
    ):
        source.read_market_book_depth("1.234", 42)
    assert called is False


def test_class_rpc_substitution_cannot_mint_market_book_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_rpc = BetfairReadOnlyClient._rpc

    def forged_rpc(self, method, params):
        raise AssertionError("forged RPC must not execute")

    monkeypatch.setattr(BetfairReadOnlyClient, "_rpc", forged_rpc)
    source = _canonical_client()
    with pytest.raises(
        BetfairReadOnlyError,
        match="canonical MarketBook RPC dispatch changed",
    ):
        source.read_market_book_depth("1.234", 42)

    monkeypatch.setattr(BetfairReadOnlyClient, "_rpc", canonical_rpc)


def test_post_issuance_rpc_rebind_revokes_market_book_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )

    def forged_rpc(self, method, params):
        raise AssertionError("forged RPC must not execute")

    monkeypatch.setattr(BetfairReadOnlyClient, "_rpc", forged_rpc)
    bound = _bound(datetime.now(timezone.utc))
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
                max_snapshot_age=timedelta(seconds=2),
            )



def test_temporary_transport_substitution_cannot_be_restored_into_authority() -> None:
    source = _canonical_client()
    canonical_transport = source._transport
    source._transport = MarketBookTransport()
    receipt = source.read_market_book_depth("1.234", 42)
    source._transport = canonical_transport

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        betfair_account_readonly.assert_market_book_depth_authoritative(receipt)


def test_temporary_clock_substitution_cannot_be_restored_into_authority() -> None:
    source = _canonical_client()
    canonical_clock = source._clock
    original_opener = urllib_request._opener
    try:
        urllib_request._opener = _CanonicalUrlOpenerHarness(MarketBookTransport())
        source._clock = lambda: READ_AT
        receipt = source.read_market_book_depth("1.234", 42)
    finally:
        source._clock = canonical_clock
        urllib_request._opener = original_opener

    with pytest.raises(
        BetfairReadOnlyError,
        match="lacks canonical direct Betfair provider IO origin",
    ):
        betfair_account_readonly.assert_market_book_depth_authoritative(receipt)


def test_market_book_clock_module_global_rebind_cannot_change_authority_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_clock = betfair_account_readonly._CANONICAL_MARKET_BOOK_CLOCK
    monkeypatch.setattr(
        betfair_account_readonly,
        "datetime",
        object(),
    )
    # Definition-time defaults keep the standard-library datetime/timezone
    # dependencies authoritative despite the module-global alias substitution.
    value = canonical_clock()
    assert isinstance(value, datetime)
    assert value.tzinfo is not None and value.utcoffset() is not None


def test_market_book_authority_exposes_no_module_level_mint_or_registry() -> None:
    assert not hasattr(betfair_account_readonly, "_issue_market_book_depth")
    assert not hasattr(betfair_account_readonly, "_MARKET_BOOK_DEPTH_ISSUED")


def test_market_book_authority_declares_trusted_process_boundary() -> None:
    assert (
        betfair_account_readonly.MARKET_BOOK_AUTHORITY_TRUST_BOUNDARY
        == "trusted-process-api-provenance-v1"
    )

def test_authoritative_market_book_binds_rest_acquisition_interval() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    acquisition_started_at = (
        betfair_account_readonly.market_book_depth_acquisition_started_at(receipt)
    )
    response_received_at = datetime.fromisoformat(receipt.evidence.observed_at)

    assert acquisition_started_at.tzinfo is not None
    assert response_received_at.tzinfo is not None
    assert acquisition_started_at <= response_received_at


def test_authoritative_feasibility_uses_acquisition_start_for_freshness() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    acquisition_started_at = (
        betfair_account_readonly.market_book_depth_acquisition_started_at(receipt)
    )
    response_received_at = datetime.fromisoformat(receipt.evidence.observed_at)
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert result.observed_at == acquisition_started_at
    assert result.received_at == response_received_at
    assert result.observed_at <= result.received_at <= result.decision_at
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN



def test_assessment_helper_rebind_cannot_mint_authoritative_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))
    canonical_assess = feasibility_module._assess_execution_feasibility

    def forged_assess(request, snapshot, limits, *, max_snapshot_age, product_owned):
        baseline = canonical_assess(
            request,
            snapshot,
            limits,
            max_snapshot_age=max_snapshot_age,
            product_owned=product_owned,
        )
        return replace(
            baseline,
            state=FeasibilityState.SNAPSHOT_DEPTH_SUFFICIENT_BUT_RACY,
            reasons=(),
            displayed_acceptable_depth=request.requested_stake,
        )

    monkeypatch.setattr(
        feasibility_module,
        "_assess_execution_feasibility",
        forged_assess,
    )
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(
            RuntimeError,
            match="canonical execution feasibility assessor changed",
        ):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                max_snapshot_age=timedelta(seconds=2),
            )


def test_instance_plan_view_substitution_cannot_choose_decision_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)

        def forged_view(plan_id: str):
            raise AssertionError("instance-substituted plan view must not execute")

        monkeypatch.setattr(ledger, "verified_execution_view", forged_view)
        result = assess_authoritative_betfair_execution_feasibility(
            ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "LIMIT_AUTHORITY_REJECTED" in result.reasons


def test_class_plan_view_rebind_revokes_feasibility_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))

    def forged_view(self, plan_id: str):
        raise AssertionError("class-substituted plan view must not execute")

    monkeypatch.setattr(
        RealExecutionLedger,
        "verified_execution_view",
        forged_view,
    )
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        with pytest.raises(
            RuntimeError,
            match="canonical execution ledger verified plan view changed",
        ):
            assess_authoritative_betfair_execution_feasibility(
                ledger,
                bound,
                receipt,
                action_id=ACTION_ID,
                max_snapshot_age=timedelta(seconds=2),
            )


def test_plan_view_code_mutation_revokes_feasibility_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))
    canonical_view = RealExecutionLedger.verified_execution_view

    def forged_view(self, plan_id: str):
        raise AssertionError("mutated plan view must not execute")

    monkeypatch.setattr(canonical_view, "__code__", forged_view.__code__)
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        with pytest.raises(
            RuntimeError,
            match="canonical execution ledger verified plan view changed",
        ):
            assess_authoritative_betfair_execution_feasibility(
                ledger,
                bound,
                receipt,
                action_id=ACTION_ID,
                max_snapshot_age=timedelta(seconds=2),
            )


def test_stale_multi_leg_plan_cannot_issue_liquidity_decision_evidence() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _two_leg_bound(datetime.now(timezone.utc))
    second_action_id = "b" * 64

    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        first_action = bound.execution_plan.actions[0]
        transition_at = datetime.now(timezone.utc)
        ledger.begin_attempt(
            plan_id=bound.execution_plan.plan_id,
            action_id=first_action.action_id,
            attempt_id="attempt-1",
            reserved_at=transition_at.isoformat(),
        )
        ledger.mark_submitted(
            "attempt-1",
            submitted_at=(transition_at + timedelta(milliseconds=1)).isoformat(),
        )
        ledger.acknowledge(
            ExternalAcknowledgement(
                attempt_id="attempt-1",
                external_receipt_id="receipt-1",
                status=AcknowledgementStatus.ACCEPTED,
                acknowledged_at=(
                    transition_at + timedelta(milliseconds=2)
                ).isoformat(),
                accepted_odds=first_action.requested_odds,
                accepted_stake=first_action.requested_stake,
            )
        )

        with pytest.raises(
            ValueError,
            match="execution plan is stale; recompute",
        ):
            assess_authoritative_betfair_execution_feasibility(
                ledger,
                bound,
                receipt,
                action_id=second_action_id,
                max_snapshot_age=timedelta(seconds=2),
            )


def test_authoritative_decision_epoch_is_durable_plan_reservation() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        plan_view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        result = assess_authoritative_betfair_execution_feasibility(
            ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    expected_decision_at = datetime.fromisoformat(plan_view.plan_reserved_at)
    assert result.decision_at == expected_decision_at
    assert result.received_at <= result.decision_at
    assert "RECEIVED_AFTER_DECISION" not in result.reasons
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert "LIMIT_AUTHORITY_REJECTED" in result.reasons


def test_response_crossing_plan_reservation_fails_received_after_decision() -> None:
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        ledger = RealExecutionLedger(Path(tmp) / "real.jsonl")
        transport = MarketBookTransport(
            before_response=lambda: ledger.reserve_plan(bound.execution_plan)
        )
        receipt, canonical_source = _synthetic_authoritative_receipt(transport)
        plan_view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        result = assess_authoritative_betfair_execution_feasibility(
            ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    expected_decision_at = datetime.fromisoformat(plan_view.plan_reserved_at)
    assert result.observed_at <= expected_decision_at
    assert result.received_at > expected_decision_at
    assert "FUTURE_SNAPSHOT" not in result.reasons
    assert "RECEIVED_AFTER_DECISION" in result.reasons
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN


def test_snapshot_age_is_measured_at_durable_plan_reservation() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    time.sleep(0.01)
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        result = assess_authoritative_betfair_execution_feasibility(
            ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(milliseconds=1),
        )

    assert "STALE_SNAPSHOT" in result.reasons
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN


def test_receipt_after_plan_reservation_cannot_be_decision_evidence() -> None:
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        ledger = _reserved_ledger(tmp, bound)
        plan_view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        # Keep the order deterministic even on clocks with coarse scheduling.
        time.sleep(0.002)
        receipt, canonical_source = _synthetic_authoritative_receipt(
            MarketBookTransport()
        )
        result = assess_authoritative_betfair_execution_feasibility(
            ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    expected_decision_at = datetime.fromisoformat(plan_view.plan_reserved_at)
    assert result.decision_at == expected_decision_at
    assert result.observed_at > result.decision_at
    assert result.received_at > result.decision_at
    assert "FUTURE_SNAPSHOT" in result.reasons
    assert "RECEIVED_AFTER_DECISION" in result.reasons
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert result.sufficient is False


def test_decision_evidence_identity_is_restart_stable() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "real.jsonl"
        ledger = RealExecutionLedger(path)
        ledger.reserve_plan(bound.execution_plan)
        first = assess_authoritative_betfair_execution_feasibility(
            ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )
        restarted = RealExecutionLedger(path)
        second = assess_authoritative_betfair_execution_feasibility(
            restarted,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert first.decision_at == second.decision_at
    assert first.evidence_digest == second.evidence_digest
    assert first.liquidity_overlap_key == second.liquidity_overlap_key


def test_distinct_plan_reservation_events_change_decision_evidence_identity() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        first_ledger = RealExecutionLedger(Path(tmp) / "first.jsonl")
        second_ledger = RealExecutionLedger(Path(tmp) / "second.jsonl")
        first_ledger.reserve_plan(bound.execution_plan)
        second_ledger.reserve_plan(bound.execution_plan)

        first_view = first_ledger.verified_execution_view(
            bound.execution_plan.plan_id
        )
        second_view = second_ledger.verified_execution_view(
            bound.execution_plan.plan_id
        )
        first = assess_authoritative_betfair_execution_feasibility(
            first_ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )
        second = assess_authoritative_betfair_execution_feasibility(
            second_ledger,
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert first_view.plan_reserved_event_id != second_view.plan_reserved_event_id
    assert first.evidence_digest != second.evidence_digest
    assert first.state is FeasibilityState.UNKNOWN_UNPROVEN
    assert second.state is FeasibilityState.UNKNOWN_UNPROVEN


def test_caller_cannot_supply_backdated_authoritative_decision_time() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    decision_at = datetime.now(timezone.utc)
    bound = _bound(decision_at)

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(TypeError, match="decision_at"):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                decision_at=decision_at - timedelta(hours=1),
                max_snapshot_age=timedelta(seconds=2),
            )


def test_expired_action_cannot_be_revived_by_historical_time() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc) - timedelta(minutes=1))

    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(
            ValueError,
            match="execution action expired before feasibility decision",
        ):
            assess_authoritative_betfair_execution_feasibility(
                _reserved_ledger(tmp, bound),
                bound,
                receipt,
                action_id=ACTION_ID,
                max_snapshot_age=timedelta(seconds=2),
            )

def test_authoritative_path_leaves_market_state_expectations_unbound() -> None:
    receipt, canonical_source = _synthetic_authoritative_receipt(
        MarketBookTransport()
    )
    bound = _bound(datetime.now(timezone.utc))

    with tempfile.TemporaryDirectory() as tmp:
        result = assess_authoritative_betfair_execution_feasibility(
            _reserved_ledger(tmp, bound),
            bound,
            receipt,
            action_id=ACTION_ID,
            max_snapshot_age=timedelta(seconds=2),
        )

    assert "MARKET_VERSION_MISMATCH" not in result.reasons
    assert "INPLAY_MISMATCH" not in result.reasons
    assert "BET_DELAY_MISMATCH" not in result.reasons
    assert result.market_version == receipt.market_version
    assert result.inplay == receipt.inplay
    assert result.bet_delay_seconds == receipt.bet_delay_seconds
    assert result.state is FeasibilityState.UNKNOWN_UNPROVEN

