from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, localcontext
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from autosport import betfair_account_readonly as _readonly
from autosport.betfair_account_funds_precheck import (
    evaluate_betfair_account_funds,
)
from autosport.betfair_account_identity import (
    build_betfair_authenticated_client,
)
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.betfair_pretrade_reservation import (
    BetfairPreTradeReservationError,
    BetfairPreTradeReservationStore,
    ReservationStatus,
    worst_case_incremental_exposure,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    ExternalEffectReconciliation,
    RealExecutionLedger,
    ReconciliationSnapshot,
)


TS = "2026-09-23T18:00:00+00:00"
RESERVED_1 = "2026-09-23T18:00:10+00:00"
RESERVED_2 = "2026-09-23T18:00:11+00:00"
SUBMITTED = "2026-09-23T18:00:20+00:00"
UNKNOWN = "2026-09-23T18:00:25+00:00"
RECONCILED = "2026-09-23T18:00:30+00:00"
ACKED = "2026-09-23T18:00:35+00:00"
EXPIRES = "2026-09-23T19:00:00+00:00"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        assert limit >= len(self._payload)
        return self._payload


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    balance: object = 1000,
    currency: str = "EUR",
) -> list[str]:
    methods: list[str] = []

    def urlopen(request, timeout: float):
        assert timeout > 0
        rpc = json.loads(request.data.decode("utf-8"))
        methods.append(rpc["method"])
        if rpc["method"].endswith("getAccountDetails"):
            result = {
                "currencyCode": currency,
                "localeCode": "en",
                "region": "SVK",
                "timezone": "Europe/Bratislava",
            }
        elif rpc["method"].endswith("getAccountFunds"):
            result = {
                "availableToBetBalance": balance,
                "exposure": 0,
                "retainedCommission": 0,
                "exposureLimit": -1000,
            }
        else:
            raise AssertionError(rpc["method"])
        payload = {
            "jsonrpc": "2.0",
            "id": rpc["id"],
            "result": result,
        }
        return _Response(
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    monkeypatch.setattr(_readonly, "urlopen", urlopen)
    return methods


def _client(
    *,
    app: str = "app-key",
    session: str = "session-token",
):
    return build_betfair_authenticated_client(
        BetfairSessionCredentials(app, session),
        account_label="pretrade-test",
    )


def _action(
    action_id: str,
    *,
    side: str = "BACK",
    odds: str = "2.50",
    stake: str = "10.00",
) -> ExecutionAction:
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=TS,
        expires_at=EXPIRES,
    )


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-v1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at=TS,
        actions=tuple(actions),
    )


def _ledger(tmp_path, *actions: ExecutionAction) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    ledger.reserve_plan(_plan(*actions))
    for index, action in enumerate(actions):
        ledger.begin_attempt(
            plan_id="plan-1",
            action_id=action.action_id,
            attempt_id=f"try-{index + 1}",
            reserved_at=RESERVED_1 if index == 0 else RESERVED_2,
        )
    return ledger


def _precheck(client, action: ExecutionAction):
    return evaluate_betfair_account_funds(
        client,
        worst_case_incremental_exposure(action),
        required_currency_code="EUR",
    )


def _store(tmp_path) -> BetfairPreTradeReservationStore:
    return BetfairPreTradeReservationStore(
        tmp_path / "pretrade.sqlite",
        account_id="acct-1",
        currency_code="EUR",
    )


def test_back_and_lay_use_exact_worst_case_exposure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()

    back = _action("back", side="BACK", odds="2.5", stake="10")
    assert worst_case_incremental_exposure(back) == Decimal("10")

    lay = _action("lay", side="LAY", odds="20", stake="10")
    assert worst_case_incremental_exposure(lay) == Decimal("190")

    ledger = _ledger(tmp_path, lay)
    result = _store(tmp_path).reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, lay),
        execution_ledger=ledger,
        customer_order_ref="order-ref-1",
    )
    assert result.reserved_amount == Decimal("190")
    assert result.ledger_state is AttemptState.RESERVED
    assert result.status is ReservationStatus.ACTIVE


def test_understated_funds_precheck_cannot_underreserve_durable_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("lay", side="LAY", odds="20", stake="10")
    ledger = _ledger(tmp_path, action)
    understated = evaluate_betfair_account_funds(
        client,
        Decimal("10"),
        required_currency_code="EUR",
    )

    with pytest.raises(
        BetfairPreTradeReservationError,
        match="liability does not match durable action",
    ):
        _store(tmp_path).reserve(
            plan_id="plan-1",
            attempt_id="try-1",
            funds_precheck=understated,
            execution_ledger=ledger,
        )


def test_local_reservations_close_same_balance_double_spend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=100)
    client = _client()
    first = _action("a1", stake="70")
    second = _action("a2", stake="40")
    ledger = _ledger(tmp_path, first, second)
    store = _store(tmp_path)

    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, first),
        execution_ledger=ledger,
    )
    with pytest.raises(
        BetfairPreTradeReservationError,
        match="minus local reservations",
    ):
        store.reserve(
            plan_id="plan-1",
            attempt_id="try-2",
            funds_precheck=_precheck(client, second),
            execution_ledger=ledger,
        )
    assert store.active_reserved_amount() == Decimal("70")


def test_concurrent_admission_has_at_most_one_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=100)
    client = _client()
    first = _action("a1", stake="70")
    second = _action("a2", stake="70")
    ledger = _ledger(tmp_path, first, second)
    store = _store(tmp_path)
    checks = {
        "try-1": _precheck(client, first),
        "try-2": _precheck(client, second),
    }

    def run(attempt_id: str) -> str:
        try:
            store.reserve(
                plan_id="plan-1",
                attempt_id=attempt_id,
                funds_precheck=checks[attempt_id],
                execution_ledger=ledger,
            )
        except BetfairPreTradeReservationError:
            return "rejected"
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("try-1", "try-2")))

    assert results.count("reserved") == 1
    assert results.count("rejected") == 1
    assert store.active_reserved_amount() == Decimal("70")


def test_unknown_and_accepted_attempts_keep_full_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    store = _store(tmp_path)
    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )

    ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
    ledger.mark_unknown("try-1", reason="timeout", observed_at=UNKNOWN)
    unknown = store.sync_from_ledger(
        attempt_id="try-1",
        execution_ledger=ledger,
    )
    assert unknown.ledger_state is AttemptState.UNKNOWN
    assert unknown.active
    assert store.active_reserved_amount() == Decimal("25")

    ledger.reconcile_found(
        ExternalEffectReconciliation(
            attempt_id="try-1",
            evidence_id="provider-found",
            external_receipt_id="bet-1",
            observed_at=RECONCILED,
            source="provider-readback",
        )
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="try-1",
            external_receipt_id="bet-1",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at=ACKED,
            accepted_odds="2.5",
            accepted_stake="25",
            reconciliation_evidence_id="provider-found",
        )
    )
    accepted = store.sync_from_ledger(
        attempt_id="try-1",
        execution_ledger=ledger,
    )
    assert accepted.ledger_state is AttemptState.ACCEPTED
    assert accepted.active
    assert store.active_reserved_amount() == Decimal("25")


def test_reconciled_not_found_is_the_only_uncertain_release_path_here(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    store = _store(tmp_path)
    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )

    ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
    ledger.mark_unknown("try-1", reason="timeout", observed_at=UNKNOWN)
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="try-1",
            evidence_id="provider-absence",
            observed_at=RECONCILED,
            external_effect_found=False,
            source="provider-readback",
        )
    )
    released = store.sync_from_ledger(
        attempt_id="try-1",
        execution_ledger=ledger,
    )
    assert released.ledger_state is AttemptState.RECONCILED_NOT_FOUND
    assert released.status is ReservationStatus.RELEASED
    assert not released.active
    assert store.active_reserved_amount() == Decimal("0")


def test_restart_preserves_active_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    first_store = _store(tmp_path)
    first = first_store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )

    restarted = _store(tmp_path)
    assert restarted.get("try-1") == first
    assert restarted.active_reserved_amount() == Decimal("25")


def test_deleted_reservation_row_cannot_free_older_unresolved_ledger_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    first = _action("a1", stake="25")
    second = _action("a2", stake="25")
    ledger = _ledger(tmp_path, first, second)
    store = _store(tmp_path)
    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, first),
        execution_ledger=ledger,
    )

    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM reservations WHERE attempt_id = 'try-1'")

    with pytest.raises(
        BetfairPreTradeReservationError,
        match="unresolved ledger attempt lacks active local reservation",
    ):
        store.reserve(
            plan_id="plan-1",
            attempt_id="try-2",
            funds_precheck=_precheck(client, second),
            execution_ledger=ledger,
        )


def test_payload_tamper_is_detected_on_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    store = _store(tmp_path)
    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )

    with sqlite3.connect(store.path) as conn:
        raw = conn.execute(
            "SELECT payload_json FROM reservations WHERE attempt_id = 'try-1'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE reservations SET payload_json = ? WHERE attempt_id = 'try-1'",
            (raw.replace('"reserved_amount":"25"', '"reserved_amount":"1"'),),
        )

    with pytest.raises(
        BetfairPreTradeReservationError,
        match="payload hash mismatch",
    ):
        _store(tmp_path)


def test_active_old_authenticated_context_blocks_new_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    first_client = _client(app="app-a", session="session-a")
    second_client = _client(app="app-b", session="session-b")
    first = _action("a1", stake="25")
    second = _action("a2", stake="25")
    ledger = _ledger(tmp_path, first, second)
    store = _store(tmp_path)
    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(first_client, first),
        execution_ledger=ledger,
    )

    with pytest.raises(
        BetfairPreTradeReservationError,
        match="different authenticated account context",
    ):
        store.reserve(
            plan_id="plan-1",
            attempt_id="try-2",
            funds_precheck=_precheck(second_client, second),
            execution_ledger=ledger,
        )



def test_lay_liability_is_independent_of_ambient_decimal_precision() -> None:
    action = _action(
        "lay-precision",
        side="LAY",
        odds="123.456",
        stake="78.901",
    )
    with localcontext() as context:
        context.prec = 2
        assert worst_case_incremental_exposure(action) == Decimal("9661.900856")


def test_later_advanced_unreserved_attempt_blocks_earlier_new_exposure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    first = _action("a1", stake="25")
    second = _action("a2", stake="25")
    ledger = _ledger(tmp_path, first, second)
    ledger.mark_submitted("try-2", submitted_at=SUBMITTED)

    with pytest.raises(
        BetfairPreTradeReservationError,
        match="unresolved ledger attempt lacks active local reservation",
    ):
        _store(tmp_path).reserve(
            plan_id="plan-1",
            attempt_id="try-1",
            funds_precheck=_precheck(client, first),
            execution_ledger=ledger,
        )


def test_sync_can_skip_intermediate_unknown_observation_when_ledger_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    store = _store(tmp_path)
    store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )

    ledger.mark_submitted("try-1", submitted_at=SUBMITTED)
    submitted = store.sync_from_ledger(
        attempt_id="try-1",
        execution_ledger=ledger,
    )
    assert submitted.ledger_state is AttemptState.SUBMITTED

    ledger.mark_unknown("try-1", reason="timeout", observed_at=UNKNOWN)
    ledger.reconcile_not_found(
        ReconciliationSnapshot(
            attempt_id="try-1",
            evidence_id="provider-absence",
            observed_at=RECONCILED,
            external_effect_found=False,
            source="provider-readback",
        )
    )
    released = store.sync_from_ledger(
        attempt_id="try-1",
        execution_ledger=ledger,
    )
    assert released.status is ReservationStatus.RELEASED
    assert store.active_reserved_amount() == Decimal("0")



def test_refreshing_funds_evidence_in_same_context_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    store = _store(tmp_path)

    first = store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )
    refreshed = store.reserve(
        plan_id="plan-1",
        attempt_id="try-1",
        funds_precheck=_precheck(client, action),
        execution_ledger=ledger,
    )

    assert refreshed == first
    assert store.active_reserved_amount() == Decimal("25")


def test_copied_funds_precheck_cannot_mint_reservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _install_provider(monkeypatch, balance=1000)
    client = _client()
    action = _action("a1", stake="25")
    ledger = _ledger(tmp_path, action)
    copied = replace(_precheck(client, action))

    with pytest.raises(
        BetfairPreTradeReservationError,
        match="lacks current product authority",
    ):
        _store(tmp_path).reserve(
            plan_id="plan-1",
            attempt_id="try-1",
            funds_precheck=copied,
            execution_ledger=ledger,
        )
