from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport import betfair_settlement_outcome_evidence as outcome_module
from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_settlement_outcome_evidence import (
    BetfairOutcomeEvidenceError,
    require_current_binary_selection_outcome,
    resolve_current_binary_selection_outcome,
)
from autosport.betfair_settlement_revisions import BetfairSettlementRevisionStore
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    ExternalAcknowledgement,
    RealExecutionLedger,
)


@pytest.fixture(autouse=True)
def _isolated_monotonic_authority(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str((tmp_path / "monotonic-authority").resolve()),
    )


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.value
        self.value += timedelta(seconds=1)
        return value


class _Transport:
    def __init__(
        self,
        *,
        side: str,
        bet_outcome: str,
        status: str = "SETTLED",
        handicap: Decimal | None = None,
        voided_date: str | None = None,
    ) -> None:
        self.side = side
        self.bet_outcome = bet_outcome
        self.status = status
        self.handicap = handicap
        self.voided_date = voided_date
        self.customer_order_ref: str | None = None

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        request = json.loads(body.decode("utf-8"))
        method = request["method"]
        request_id = request["id"]
        if method.endswith("listMarketCatalogue"):
            result = [{"marketId": "1.234", "event": {"id": "event-1"}}]
        elif method.endswith("listCurrentOrders"):
            result = {"currentOrders": [], "moreAvailable": False}
        elif method.endswith("listClearedOrders"):
            rows = []
            if request["params"]["betStatus"] == self.status:
                row = {
                    "betId": "bet-777",
                    "marketId": "1.234",
                    "eventId": "event-1",
                    "selectionId": 10,
                    "side": self.side,
                    "placedDate": "2026-09-29T00:00:00+00:00",
                    "settledDate": "2026-09-29T00:30:00+00:00",
                    "priceRequested": 2,
                    "priceMatched": 2,
                    "sizeSettled": 5,
                    "profit": 4,
                    "customerOrderRef": self.customer_order_ref,
                    "betOutcome": self.bet_outcome,
                }
                if self.handicap is not None:
                    row["handicap"] = float(self.handicap)
                if self.voided_date is not None:
                    row["voidedDate"] = self.voided_date
                rows.append(row)
            result = {"clearedOrders": rows, "moreAvailable": False}
        else:  # pragma: no cover
            raise AssertionError(method)
        return json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result},
            separators=(",", ":"),
        ).encode("utf-8")


def _context(
    tmp_path,
    *,
    side: str = "BACK",
    outcome: str = "WON",
    status: str = "SETTLED",
    handicap: Decimal | None = None,
    voided_date: str | None = None,
):
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id="event-1",
        market_id="1.234",
        selection_id="10",
        side=side,
        requested_odds=Decimal("2"),
        requested_stake=Decimal("5"),
        quote_id="quote-1",
        quote_observed_at="2026-09-28T23:59:00+00:00",
        expires_at="2026-09-29T00:05:00+00:00",
    )
    ledger = RealExecutionLedger(tmp_path / "execution.jsonl")
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-28T23:59:30+00:00",
        actions=(action,),
    )
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-29T00:00:00+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id="attempt-1",
        provider_id="betfair",
    )
    ledger.mark_submitted(
        "attempt-1",
        submitted_at="2026-09-29T00:01:00+00:00",
    )
    ledger.acknowledge(
        ExternalAcknowledgement(
            attempt_id="attempt-1",
            external_receipt_id="bet-777",
            status=AcknowledgementStatus.ACCEPTED,
            acknowledged_at="2026-09-29T00:02:00+00:00",
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("5"),
        )
    )
    transport = _Transport(
        side=side,
        bet_outcome=outcome,
        status=status,
        handicap=handicap,
        voided_date=voided_date,
    )
    transport.customer_order_ref = provider_ref
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=transport,
        clock=_Clock(),
        venue_id="betfair",
        account_id="acct-1",
    )
    store = BetfairSettlementRevisionStore(tmp_path / "settlement.jsonl")
    return store, ledger, plan, action, provider_ref, transport, client


def _capture(client: BetfairReadOnlyClient, provider_ref: str):
    return client.read_execution_readback(
        action_id="action-1",
        market_id="1.234",
        provider_order_ref=provider_ref,
    )


def _ingest(store, ledger, plan, action, capture):
    return store.ingest(
        ledger,
        plan_id=plan.plan_id,
        attempt_id="attempt-1",
        action=action,
        capture=capture,
    )


def _resolve(store):
    return resolve_current_binary_selection_outcome(
        store,
        bookmaker_id="betfair",
        account_id="acct-1",
        external_bet_id="bet-777",
    )


def test_back_won_projects_selection_win_without_claiming_finality(tmp_path) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(tmp_path)
    revision = _ingest(store, ledger, plan, action, _capture(client, ref)).revision

    evidence = _resolve(store)

    assert evidence.selection_won is True
    assert evidence.target_value == 1
    assert evidence.side == "BACK"
    assert evidence.bet_outcome == "WON"
    assert evidence.settlement_revision_id == revision.revision_id
    assert evidence.settlement_content_sha256 == revision.content_sha256
    assert evidence.permanent_final is False
    assert evidence.training_label_authorized is False
    assert require_current_binary_selection_outcome(store, evidence) == evidence


@pytest.mark.parametrize(
    ("outcome", "expected_won", "expected_target"),
    (("WON", False, 0), ("LOST", True, 1)),
)
def test_lay_bet_outcome_is_inverted_to_selection_outcome(
    tmp_path,
    outcome: str,
    expected_won: bool,
    expected_target: int,
) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(
        tmp_path,
        side="LAY",
        outcome=outcome,
    )
    _ingest(store, ledger, plan, action, _capture(client, ref))

    evidence = _resolve(store)

    assert evidence.side == "LAY"
    assert evidence.bet_outcome == outcome
    assert evidence.selection_won is expected_won
    assert evidence.target_value == expected_target


def test_voided_settlement_cannot_become_binary_selection_outcome(tmp_path) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(
        tmp_path,
        outcome="LOST",
        status="VOIDED",
    )
    _ingest(store, ledger, plan, action, _capture(client, ref))

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="only SETTLED provider status",
    ):
        _resolve(store)


def test_handicap_settlement_cannot_mint_unqualified_selection_label(tmp_path) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(
        tmp_path,
        handicap=Decimal("-1.5"),
    )
    revision = _ingest(store, ledger, plan, action, _capture(client, ref)).revision
    assert revision.provider_handicap == Decimal("-1.5")

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="handicap settlement cannot prove unqualified binary selection outcome",
    ):
        _resolve(store)


def test_void_dated_settlement_cannot_mint_binary_selection_label(tmp_path) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(
        tmp_path,
        voided_date="2026-09-29T00:31:00+00:00",
    )
    revision = _ingest(store, ledger, plan, action, _capture(client, ref)).revision
    assert revision.provider_voided_date == "2026-09-29T00:31:00+00:00"

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="void-dated settlement cannot prove binary selection outcome",
    ):
        _resolve(store)


def test_settlement_correction_invalidates_previous_outcome_evidence(tmp_path) -> None:
    store, ledger, plan, action, ref, transport, client = _context(tmp_path)
    first = _ingest(store, ledger, plan, action, _capture(client, ref)).revision
    evidence = _resolve(store)
    assert evidence.settlement_revision_id == first.revision_id
    assert evidence.target_value == 1

    transport.bet_outcome = "LOST"
    corrected = _ingest(store, ledger, plan, action, _capture(client, ref)).revision
    assert corrected.revision_id != first.revision_id

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="superseded by settlement correction",
    ):
        require_current_binary_selection_outcome(store, evidence)

    current = _resolve(store)
    assert current.settlement_revision_id == corrected.revision_id
    assert current.target_value == 0

def test_outcome_dispatch_guard_root_rebind_fails_before_hostile_checker(
    tmp_path,
    monkeypatch,
) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(tmp_path)
    _ingest(store, ledger, plan, action, _capture(client, ref))
    captured_resolve = resolve_current_binary_selection_outcome
    hostile_calls: list[str] = []

    def hostile_guard() -> None:
        hostile_calls.append("guard")
        raise AssertionError("hostile dispatch checker executed")

    monkeypatch.setattr(outcome_module, "_require_dispatch", hostile_guard)

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="public dispatch changed",
    ):
        captured_resolve(
            store,
            bookmaker_id="betfair",
            account_id="acct-1",
            external_bet_id="bet-777",
        )

    assert hostile_calls == []


def test_outcome_dispatch_guard_code_mutation_fails_before_projection(
    tmp_path,
    monkeypatch,
) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(tmp_path)
    _ingest(store, ledger, plan, action, _capture(client, ref))
    guard = outcome_module._require_dispatch
    original_code = guard.__code__

    monkeypatch.setattr(
        guard,
        "__code__",
        original_code.replace(co_name="hostile_dispatch_checker"),
    )

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="public dispatch changed",
    ):
        resolve_current_binary_selection_outcome(
            store,
            bookmaker_id="betfair",
            account_id="acct-1",
            external_bet_id="bet-777",
        )


def test_outcome_project_and_expected_witness_rebind_cannot_move_together(
    tmp_path,
    monkeypatch,
) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(tmp_path)
    _ingest(store, ledger, plan, action, _capture(client, ref))
    hostile_calls: list[str] = []

    def hostile_project(_revision):
        hostile_calls.append("project")
        raise AssertionError("hostile projection executed")

    monkeypatch.setattr(outcome_module, "_project", hostile_project)
    monkeypatch.setattr(outcome_module, "_PROJECT", hostile_project)
    monkeypatch.setattr(outcome_module, "_PROJECT_CODE", hostile_project.__code__)

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="authority dispatch changed",
    ):
        resolve_current_binary_selection_outcome(
            store,
            bookmaker_id="betfair",
            account_id="acct-1",
            external_bet_id="bet-777",
        )

    assert hostile_calls == []


def test_captured_require_rejects_public_resolver_root_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(tmp_path)
    _ingest(store, ledger, plan, action, _capture(client, ref))
    evidence = _resolve(store)
    captured_require = require_current_binary_selection_outcome
    hostile_calls: list[str] = []

    def hostile_resolve(*_args, **_kwargs):
        hostile_calls.append("resolve")
        raise AssertionError("hostile public resolver executed")

    monkeypatch.setattr(
        outcome_module,
        "resolve_current_binary_selection_outcome",
        hostile_resolve,
    )

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="public dispatch changed",
    ):
        captured_require(store, evidence)

    assert hostile_calls == []


def test_captured_resolve_rejects_public_require_root_rebind(
    tmp_path,
    monkeypatch,
) -> None:
    store, ledger, plan, action, ref, _transport, client = _context(tmp_path)
    _ingest(store, ledger, plan, action, _capture(client, ref))
    captured_resolve = resolve_current_binary_selection_outcome

    def hostile_require(*_args, **_kwargs):
        raise AssertionError("hostile public require executed")

    monkeypatch.setattr(
        outcome_module,
        "require_current_binary_selection_outcome",
        hostile_require,
    )

    with pytest.raises(
        BetfairOutcomeEvidenceError,
        match="public dispatch changed",
    ):
        captured_resolve(
            store,
            bookmaker_id="betfair",
            account_id="acct-1",
            external_bet_id="bet-777",
        )

