from __future__ import annotations

import json
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import autosport.betfair_supervised_execution as betfair_execution
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionGate,
    BetfairSupervisedPlaceOrdersClient,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.economic_goal import AutomationLevel, EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.execution_stop_authority import ExecutionStopAuthority
from autosport.real_execution_ledger import ExecutionAction


OBSERVED_AT = "2026-09-21T17:55:00+00:00"
EXPIRES_AT = "2026-09-21T18:05:00+00:00"
STOP_PATH = "execution-stop.jsonl"


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = json.loads(body.decode("utf-8"))
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "request": request,
                "timeout_seconds": timeout_seconds,
            }
        )
        instruction = request["params"]["instructions"][0]
        echoed_instruction = dict(instruction)
        echoed_instruction["limitOrder"] = {
            **instruction["limitOrder"],
            "price": 2,
            "size": 1,
        }
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "result": {
                    "status": "SUCCESS",
                    "marketId": request["params"]["marketId"],
                    "instructionReports": [
                        {
                            "status": "SUCCESS",
                            "instruction": echoed_instruction,
                            "betId": "bet-stop-composition",
                            "placedDate": OBSERVED_AT,
                            "averagePriceMatched": 2,
                            "sizeMatched": 1,
                        }
                    ],
                },
                "id": request["id"],
            },
            separators=(",", ":"),
        ).encode("utf-8")


class _BlockingRecordingTransport(_RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release blocked provider call")
        return super().post(
            url,
            headers=headers,
            body=body,
            timeout_seconds=timeout_seconds,
        )


class _FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def post(
        self,
        url: str,
        *,
        headers,
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        self.calls += 1
        raise OSError("synthetic provider failure")


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="betfair",
        account_id="acct-stop-composition",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BET_READBACK,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=OBSERVED_AT,
        source_ref="betfair://stop-composition/falsifier",
        source_payload_sha256="b" * 64,
    )


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-stop-composition",
        revision=1,
        bankroll_id="paper-bankroll",
        currency="USD",
        max_stake_fraction=Decimal("0.10"),
        max_session_loss_fraction=Decimal("1"),
        max_day_loss_fraction=Decimal("1"),
        max_drawdown_fraction=Decimal("1"),
        max_capital_at_risk_fraction=Decimal("1"),
        max_turnover_fraction=Decimal("1000"),
        max_risk_of_ruin=Decimal("1"),
        max_execution_slippage_fraction=Decimal("0.05"),
        max_quote_age_seconds=Decimal("3600"),
        max_concurrent_positions=10,
        automation_level=AutomationLevel.SUPERVISED_EXECUTION,
    )


def _action() -> ExecutionAction:
    return ExecutionAction(
        action_id="action-stop-composition",
        bookmaker_id="betfair",
        account_id="acct-stop-composition",
        event_id="event-stop-composition",
        market_id="1.23456789",
        selection_id="42",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("1.00"),
        quote_id="quote-stop-composition",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )


def _client_and_bound(workspace: Path, *, transport=None):
    goal = _goal()
    store = EconomicGoalStore(workspace)
    store.initialize_owner(goal)
    profile = _profile()
    gate = BetfairSupervisedExecutionGate.from_economic_goal_store(
        store,
        bookmaker_id="betfair",
        account_id="acct-stop-composition",
        profile_sha256=profile.profile_id,
    )
    transport = _RecordingTransport() if transport is None else transport
    client = BetfairSupervisedPlaceOrdersClient(
        BetfairSessionCredentials("app-key", "session-token"),
        gate=gate,
        transport=transport,
        clock=lambda: OBSERVED_AT,
    )
    bound = SimpleNamespace(
        economic_goal_contract_sha256=provenance_for(goal).contract_sha256,
        constraint_for=lambda _action_id: SimpleNamespace(
            max_slippage_fraction=Decimal("0.01")
        ),
        profile_for=lambda _bookmaker_id, _account_id: SimpleNamespace(
            profile_sha256=profile.profile_id,
            adapter_id=profile.adapter_id,
            adapter_version=profile.adapter_version,
            profile_version=profile.profile_version,
        ),
    )
    return client, bound, profile, transport


def _call_place_action(workspace: Path):
    client, bound, profile, transport = _client_and_bound(workspace)
    report = client.place_action(
        _action(),
        profile=profile,
        bound=bound,
        provider_order_ref="a" * 16,
        execution_workspace=workspace,
    )
    return report, transport


def _assert_provider_write_denied(workspace: Path) -> None:
    client, bound, profile, transport = _client_and_bound(workspace)
    try:
        client.place_action(
            _action(),
            profile=profile,
            bound=bound,
            provider_order_ref="a" * 16,
            execution_workspace=workspace,
        )
    except Exception:
        # Repair-mechanism neutral: the product may normalize STOP authority
        # errors at the Betfair seam or surface the canonical authority error.
        # The observable safety invariant is zero provider transport calls.
        pass
    else:
        pytest.fail("provider write was reachable without positive STOP authority")
    assert transport.calls == []


def test_missing_execution_stop_authority_denies_provider_write(tmp_path: Path) -> None:
    """A missing independent STOP authority must fail closed before transport."""

    _assert_provider_write_denied(tmp_path)


def test_durable_stopped_execution_authority_denies_provider_write(
    tmp_path: Path,
) -> None:
    """Persisted STOPPED state must override otherwise valid EconomicGoal authority."""

    authority = ExecutionStopAuthority(tmp_path / STOP_PATH)
    authority.initialize_stopped(
        operator_id="owner",
        reason="operator STOP",
        command_id="stop-composition-init",
    )

    _assert_provider_write_denied(tmp_path)


def test_corrupt_execution_stop_authority_denies_provider_write(tmp_path: Path) -> None:
    """Corrupt STOP evidence must never be treated as implicit ARMED authority."""

    authority = ExecutionStopAuthority(tmp_path / STOP_PATH)
    authority.initialize_stopped(
        operator_id="owner",
        reason="safe initialization",
        command_id="stop-composition-corrupt-init",
    )
    authority.anchor_path.write_text("{}\n", encoding="utf-8")

    _assert_provider_write_denied(tmp_path)


def test_explicit_armed_execution_stop_authority_keeps_bounded_write_reachable(
    tmp_path: Path,
) -> None:
    """Positive control: explicit durable ARM preserves the existing bounded seam."""

    authority = ExecutionStopAuthority(tmp_path / STOP_PATH)
    authority.initialize_stopped(
        operator_id="owner",
        reason="safe initialization",
        command_id="stop-composition-arm-init",
    )
    authority.arm(
        operator_id="owner",
        reason="supervised write explicitly armed",
        confirmation_id="stop-composition-confirmation",
        expected_revision=1,
        command_id="stop-composition-arm",
    )

    report, transport = _call_place_action(tmp_path)

    assert report.instruction.bet_id == "bet-stop-composition"
    assert len(transport.calls) == 1



def test_inflight_provider_write_holds_stop_linearization_until_transport_exits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    authority = ExecutionStopAuthority(tmp_path / STOP_PATH)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="safe initialization",
        command_id="stop-composition-race-init",
    )
    armed = authority.arm(
        operator_id="owner",
        reason="supervised write explicitly armed",
        confirmation_id="stop-composition-race-confirmation",
        expected_revision=stopped.revision,
        command_id="stop-composition-race-arm",
    )
    transport = _BlockingRecordingTransport()
    client, bound, profile, _ = _client_and_bound(
        tmp_path,
        transport=transport,
    )
    stop_lock_attempted = Event()
    original_authority_lock = authority._authority_operation_lock

    @contextmanager
    def observed_authority_lock():
        stop_lock_attempted.set()
        with original_authority_lock():
            yield

    monkeypatch.setattr(
        authority,
        "_authority_operation_lock",
        observed_authority_lock,
    )

    def place():
        return client.place_action(
            _action(),
            profile=profile,
            bound=bound,
            provider_order_ref="b" * 16,
            execution_workspace=tmp_path,
        )

    def stop():
        return authority.stop(
            operator_id="owner",
            reason="concurrent operator STOP",
            expected_revision=armed.revision,
            command_id="stop-composition-race-stop",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        place_future = pool.submit(place)
        assert transport.entered.wait(timeout=5)
        stop_future = pool.submit(stop)
        assert stop_lock_attempted.wait(timeout=5)
        assert not stop_future.done()

        transport.release.set()
        report = place_future.result(timeout=5)
        stopped_after_call = stop_future.result(timeout=5)

    assert report.instruction.bet_id == "bet-stop-composition"
    assert len(transport.calls) == 1
    assert stopped_after_call.mode.value == "STOPPED"

    denied_client, denied_bound, denied_profile, denied_transport = _client_and_bound(
        tmp_path
    )
    with pytest.raises(Exception):
        denied_client.place_action(
            _action(),
            profile=denied_profile,
            bound=denied_bound,
            provider_order_ref="c" * 16,
            execution_workspace=tmp_path,
        )
    assert denied_transport.calls == []


def test_stop_authority_global_rebind_cannot_bypass_provider_fence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    authority = ExecutionStopAuthority(tmp_path / STOP_PATH)
    authority.initialize_stopped(
        operator_id="owner",
        reason="operator STOP",
        command_id="stop-composition-rebind-init",
    )
    client, bound, profile, transport = _client_and_bound(tmp_path)

    class _BypassAuthority:
        @contextmanager
        def admission_lease(self):
            yield None

    monkeypatch.setattr(
        betfair_execution,
        "ExecutionStopAuthority",
        _BypassAuthority,
    )

    with pytest.raises(Exception, match="STOP admission authority changed"):
        client.place_action(
            _action(),
            profile=profile,
            bound=bound,
            provider_order_ref="e" * 16,
            execution_workspace=tmp_path,
        )

    assert transport.calls == []


def test_transport_exception_releases_stop_admission_lease(tmp_path: Path) -> None:
    authority = ExecutionStopAuthority(tmp_path / STOP_PATH)
    stopped = authority.initialize_stopped(
        operator_id="owner",
        reason="safe initialization",
        command_id="stop-composition-error-init",
    )
    armed = authority.arm(
        operator_id="owner",
        reason="supervised write explicitly armed",
        confirmation_id="stop-composition-error-confirmation",
        expected_revision=stopped.revision,
        command_id="stop-composition-error-arm",
    )
    transport = _FailingTransport()
    client, bound, profile, _ = _client_and_bound(
        tmp_path,
        transport=transport,
    )

    with pytest.raises(Exception, match="ambiguous"):
        client.place_action(
            _action(),
            profile=profile,
            bound=bound,
            provider_order_ref="d" * 16,
            execution_workspace=tmp_path,
        )

    stopped_after_error = authority.stop(
        operator_id="owner",
        reason="STOP after failed provider call",
        expected_revision=armed.revision,
        command_id="stop-composition-error-stop",
    )
    assert stopped_after_error.mode.value == "STOPPED"
    assert transport.calls == 1
