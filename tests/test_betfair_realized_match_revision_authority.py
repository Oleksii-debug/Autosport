from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timezone
from pathlib import Path
import json

import pytest

from autosport.betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.betfair_realized_match import (
    BetfairRealizedMatchEvidence,
    RealizedMatchEvidenceError,
    resolve_betfair_realized_match,
    validate_betfair_realized_match_revision,
)
from autosport.real_execution_ledger import (
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


ATTEMPT_ID = "attempt-revision-authority"
MARKET_ID = "1.234"
EVENT_ID = "event-1"
SELECTION_ID = 10


class _Transport:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)

    def post(self, url: str, *, headers, body: bytes, timeout_seconds: float) -> bytes:
        del url, headers, body, timeout_seconds
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


def _prepared(root: Path) -> tuple[ExecutionPlan, RealExecutionLedger, str]:
    action = ExecutionAction(
        action_id="action-1",
        bookmaker_id="betfair",
        account_id="acct-1",
        event_id=EVENT_ID,
        market_id=MARKET_ID,
        selection_id=str(SELECTION_ID),
        side="BACK",
        requested_odds="3.0",
        requested_stake="10",
        quote_id="quote-1",
        quote_observed_at="2026-09-21T09:54:50+00:00",
        expires_at="2026-09-21T09:56:00+00:00",
    )
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T09:54:45+00:00",
        actions=(action,),
    )
    ledger = RealExecutionLedger(root / "real-execution.jsonl")
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id=ATTEMPT_ID,
        reserved_at="2026-09-21T09:54:52+00:00",
    )
    provider_ref = ledger.bind_provider_order_reference(
        attempt_id=ATTEMPT_ID,
        provider_id="betfair",
    )
    ledger.mark_submitted(
        ATTEMPT_ID,
        submitted_at="2026-09-21T09:54:55+00:00",
    )
    return plan, ledger, provider_ref


def _current(provider_ref: str, *, matched_price: float) -> dict[str, object]:
    return {
        "betId": "bet-1",
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "status": "EXECUTABLE",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "priceSize": {"price": 3.0, "size": 10.0},
        "averagePriceMatched": matched_price,
        "sizeMatched": 4.0,
        "sizeRemaining": 6.0,
        "customerOrderRef": provider_ref,
    }


def _cleared(provider_ref: str, *, matched_price: float) -> dict[str, object]:
    return {
        "betId": "bet-1",
        "marketId": MARKET_ID,
        "selectionId": SELECTION_ID,
        "side": "BACK",
        "placedDate": "2026-09-21T09:54:55+00:00",
        "settledDate": "2026-09-21T10:30:00+00:00",
        "priceRequested": 3.0,
        "priceMatched": matched_price,
        "sizeSettled": 4.0,
        "profit": 8.8,
        "customerOrderRef": provider_ref,
        "eventId": EVENT_ID,
    }


def _capture(
    provider_ref: str,
    *,
    observed_at: datetime,
    current: dict[str, object] | None = None,
    cleared: dict[str, object] | None = None,
):
    responses = [
        _response([{"marketId": MARKET_ID, "event": {"id": EVENT_ID}}], 1),
        _response(
            {
                "currentOrders": [] if current is None else [current],
                "moreAvailable": False,
            },
            2,
        ),
        _response(
            {
                "clearedOrders": [] if cleared is None else [cleared],
                "moreAvailable": False,
            },
            3,
        ),
    ]
    for request_id in range(4, 7):
        responses.append(
            _response({"clearedOrders": [], "moreAvailable": False}, request_id)
        )
    return BetfairReadOnlyClient(
        BetfairSessionCredentials("app-key", "session-token"),
        transport=_Transport(responses),
        clock=lambda: observed_at,
        venue_id="betfair",
        account_id="acct-1",
    ).read_execution_readback(
        action_id="action-1",
        provider_order_ref=provider_ref,
        market_id=MARKET_ID,
    )


def _resolve_current(root: Path):
    plan, ledger, provider_ref = _prepared(root)
    evidence = resolve_betfair_realized_match(
        plan,
        ledger,
        _capture(
            provider_ref,
            observed_at=datetime(2026, 9, 21, 9, 55, tzinfo=timezone.utc),
            current=_current(provider_ref, matched_price=3.1),
        ),
        attempt_id=ATTEMPT_ID,
    )
    evidence.assert_authoritative()
    return plan, ledger, provider_ref, evidence


def test_revision_rejects_subclass_virtual_authority_bypass(tmp_path: Path) -> None:
    _plan, _ledger, _provider_ref, evidence = _resolve_current(tmp_path)

    class ForgedEvidence(BetfairRealizedMatchEvidence):
        def assert_authoritative(self) -> None:
            return None

    forged = ForgedEvidence(
        **{
            field.name: getattr(evidence, field.name)
            for field in fields(BetfairRealizedMatchEvidence)
        }
    )

    with pytest.raises(TypeError, match="exact BetfairRealizedMatchEvidence"):
        validate_betfair_realized_match_revision(forged, forged)


def test_revision_rejects_exact_class_copy_without_canonical_origin(
    tmp_path: Path,
) -> None:
    _plan, _ledger, _provider_ref, evidence = _resolve_current(tmp_path)
    forged = replace(evidence)

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="not issued by canonical resolver",
    ):
        validate_betfair_realized_match_revision(evidence, forged)


def test_revision_detects_class_validator_rebinding_before_positive_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plan, _ledger, _provider_ref, evidence = _resolve_current(tmp_path)
    monkeypatch.setattr(
        BetfairRealizedMatchEvidence,
        "assert_authoritative",
        lambda self: None,
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="canonical realized match evidence authority changed",
    ):
        validate_betfair_realized_match_revision(evidence, evidence)


def test_canonical_revision_self_validation_still_passes(tmp_path: Path) -> None:
    _plan, _ledger, _provider_ref, evidence = _resolve_current(tmp_path)
    assert validate_betfair_realized_match_revision(evidence, evidence) is evidence


def test_final_cleared_price_correction_can_replace_same_size_transient_price(
    tmp_path: Path,
) -> None:
    plan, ledger, provider_ref, transient = _resolve_current(tmp_path)
    final = resolve_betfair_realized_match(
        plan,
        ledger,
        _capture(
            provider_ref,
            observed_at=datetime(2026, 9, 21, 10, 31, tzinfo=timezone.utc),
            cleared=_cleared(provider_ref, matched_price=3.2),
        ),
        attempt_id=ATTEMPT_ID,
    )

    selected = validate_betfair_realized_match_revision(transient, final)
    assert selected is final
    assert transient.provider_matched_stake == final.provider_matched_stake
    assert transient.provider_matched_odds != final.provider_matched_odds
    assert final.finalized is True


def test_same_source_same_size_price_rewrite_remains_rejected(tmp_path: Path) -> None:
    plan, ledger, provider_ref, previous = _resolve_current(tmp_path)
    current = resolve_betfair_realized_match(
        plan,
        ledger,
        _capture(
            provider_ref,
            observed_at=datetime(2026, 9, 21, 9, 56, tzinfo=timezone.utc),
            current=_current(provider_ref, matched_price=3.2),
        ),
        attempt_id=ATTEMPT_ID,
    )

    with pytest.raises(
        RealizedMatchEvidenceError,
        match="changes matched odds without new matched stake",
    ):
        validate_betfair_realized_match_revision(previous, current)
