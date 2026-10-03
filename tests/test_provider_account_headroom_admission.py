from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

import autosport.betfair_account_readonly as betfair_readonly
import autosport.provider_account_headroom_admission as headroom_module
from autosport.account_snapshot_acquisition import (
    AuthoritativeAccountSnapshot,
    BetfairAccountSnapshotAcquirer,
)
from autosport.betfair_account_readonly import BetfairSessionCredentials
from autosport.bookmaker_capability import BookmakerCapability
from autosport.provider_account_headroom_admission import (
    HeadroomDecision,
    ProviderAccountHeadroomError,
    ProviderAccountHeadroomStale,
    ProviderAccountHeadroomUnsupported,
    assess_provider_account_headroom,
    reserve_observed_provider_headroom,
)
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)


def _response(result: object, request_id: int) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "result": result, "id": request_id},
        separators=(",", ":"),
    ).encode("utf-8")


_DEVELOPER_APPS = _response(
    [
        {
            "appId": 12345,
            "appVersions": [
                {
                    "versionId": 67890,
                    "version": "1.0",
                    "applicationKey": "DEVAPP-SECRET-SENTINEL",
                    "ownerManaged": False,
                }
            ],
        }
    ],
    1,
)
_DETAILS = _response(
    {
        "currencyCode": "GBP",
        "localeCode": "en",
        "region": "GBR",
        "timezone": "Europe/London",
    },
    2,
)


def _funds(available: str, *, exposure: str = "-12.34") -> bytes:
    return _response(
        {
            "availableToBetBalance": float(available),
            "exposure": float(exposure),
            "retainedCommission": 0.05,
            "exposureLimit": -5000.00,
        },
        3,
    )


def _install_transport(monkeypatch, responses: list[bytes]) -> list[dict[str, object]]:
    queue = list(responses)
    calls: list[dict[str, object]] = []

    def post(self, url, *, headers, body, timeout_seconds):
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if not queue:
            raise AssertionError("unexpected provider call")
        return queue.pop(0)

    monkeypatch.setattr(
        betfair_readonly.UrllibBetfairHttpTransport,
        "post",
        post,
    )
    return calls


def _credentials() -> BetfairSessionCredentials:
    return BetfairSessionCredentials(
        "APP-SECRET-SENTINEL",
        "SESSION-SECRET-SENTINEL",
    )


def _acquire_balance(monkeypatch, tmp_path, available: str = "100.10"):
    calls = _install_transport(
        monkeypatch,
        [_DEVELOPER_APPS, _DETAILS, _funds(available)],
    )
    acquired = BetfairAccountSnapshotAcquirer(
        tmp_path / "account.sqlite3",
        _credentials(),
        account_id="acct-1",
    ).acquire(
        frozenset({BookmakerCapability.BALANCE_READ}),
        acquisition_id=f"headroom-{available}",
    )
    assert acquired.snapshot.balance is not None
    return acquired, calls


def _action(
    action_id: str,
    stake: str,
    *,
    account_id: str = "acct-1",
) -> ExecutionAction:
    now = datetime.now(timezone.utc)
    return ExecutionAction(
        action_id=action_id,
        bookmaker_id="betfair",
        account_id=account_id,
        event_id=f"event-{action_id}",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side="BACK",
        requested_odds="2.50",
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=(now - timedelta(seconds=2)).isoformat(),
        expires_at=(now + timedelta(minutes=2)).isoformat(),
    )


def _plan(plan_id: str, action: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id=plan_id,
        bookmaker_profile_version="profile-v1",
        decision_id=f"decision-{plan_id}",
        approval_id=f"approval-{plan_id}",
        created_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        actions=(action,),
    )


def _ledger_with_plans(tmp_path, *plans: ExecutionPlan) -> RealExecutionLedger:
    ledger = RealExecutionLedger(tmp_path / "real-ledger.jsonl")
    for item in plans:
        ledger.reserve_plan(item)
    return ledger


def test_provider_exposure_is_not_double_subtracted(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100.10")
    action = _action("a1", "95")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))

    assessment = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )

    assert assessment.provider_available_to_bet == Decimal("100.1")
    assert assessment.definitely_unreflected_product_liability == 0
    assert assessment.unknown_reflection_product_liability == 0
    assert assessment.lower_headroom == Decimal("100.1")
    assert assessment.upper_headroom == Decimal("100.1")
    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND
    assert assessment.provider_atomicity_proven is False
    assert assessment.provider_balance_generation_cas_proven is False
    assert assessment.execution_authority is False
    assert assessment.real_money_readiness is False


def test_same_snapshot_two_writer_race_only_one_reserves(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    first_action = _action("a1", "80")
    second_action = _action("a2", "80")
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", first_action),
        _plan("p2", second_action),
    )

    first = assess_provider_account_headroom(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    second_from_same_generation = assess_provider_account_headroom(
        ledger, acquired, plan_id="p2", action_id="a2"
    )
    assert first.ledger_snapshot_sha256 == second_from_same_generation.ledger_snapshot_sha256
    assert first.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND
    assert second_from_same_generation.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND

    reserved = reserve_observed_provider_headroom(
        ledger,
        acquired,
        first,
        attempt_id="attempt-1",
    )
    assert reserved.product_internal_reservation_proven is True
    assert reserved.provider_atomicity_proven is False
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED

    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="execution ledger changed",
    ):
        reserve_observed_provider_headroom(
            ledger,
            acquired,
            second_from_same_generation,
            attempt_id="attempt-2",
        )

    recomputed = assess_provider_account_headroom(
        ledger, acquired, plan_id="p2", action_id="a2"
    )
    assert recomputed.definitely_unreflected_product_liability == Decimal("80")
    assert recomputed.unknown_reflection_product_liability == 0
    assert recomputed.lower_headroom == Decimal("20")
    assert recomputed.upper_headroom == Decimal("20")
    assert recomputed.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND


def test_submitted_liability_with_unknown_balance_coverage_forces_wait(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    first_action = _action("a1", "70")
    second_action = _action("a2", "50")
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", first_action),
        _plan("p2", second_action),
    )
    first = assess_provider_account_headroom(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    reserve_observed_provider_headroom(
        ledger, acquired, first, attempt_id="attempt-1"
    )
    ledger.mark_submitted("attempt-1")

    second = assess_provider_account_headroom(
        ledger, acquired, plan_id="p2", action_id="a2"
    )
    assert second.definitely_unreflected_product_liability == 0
    assert second.unknown_reflection_product_liability == Decimal("70")
    assert second.lower_headroom == Decimal("30")
    assert second.upper_headroom == Decimal("100")
    assert second.decision is HeadroomDecision.WAIT_COVERAGE


def test_account_authority_alias_rebinding_cannot_forge_available_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "5")
    assert acquired.snapshot.balance is not None
    forged_balance = replace(
        acquired.snapshot.balance,
        available_balance=Decimal("1000000"),
    )
    forged = AuthoritativeAccountSnapshot(
        replace(acquired.snapshot, balance=forged_balance),
        acquired.receipt,
    )
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    hostile_calls = []

    def forged_authority(_acquired):
        hostile_calls.append(True)

    monkeypatch.setattr(
        headroom_module,
        "assert_account_snapshot_acquisition_authoritative",
        forged_authority,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="account snapshot headroom authority changed",
    ):
        assess_provider_account_headroom(
            ledger,
            forged,
            plan_id="p1",
            action_id="a1",
        )

    assert hostile_calls == []


def test_other_provider_account_cannot_donate_headroom(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "1000")
    foreign = _action("a1", "20", account_id="acct-2")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", foreign))

    with pytest.raises(
        ProviderAccountHeadroomUnsupported,
        match="provider/account mismatches live balance acquisition",
    ):
        assess_provider_account_headroom(
            ledger, acquired, plan_id="p1", action_id="a1"
        )


def test_caller_reconstructed_assessment_cannot_reserve(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = assess_provider_account_headroom(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    forged = replace(assessment)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="not canonically issued",
    ):
        reserve_observed_provider_headroom(
            ledger, acquired, forged, attempt_id="attempt-1"
        )


def test_module_attempt_state_rebinding_cannot_bypass_headroom_gate(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "5")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )
    assert assessment.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND
    hostile_calls = []

    def hostile_attempt_state(_ledger, _attempt_id):
        hostile_calls.append(True)
        return AttemptState.RESERVED

    monkeypatch.setattr(
        headroom_module,
        "_ATTEMPT_STATE",
        hostile_attempt_state,
    )

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="execution ledger headroom authority changed",
    ):
        reserve_observed_provider_headroom(
            ledger,
            acquired,
            assessment,
            attempt_id="attempt-1",
        )

    assert hostile_calls == []
    with pytest.raises(KeyError):
        ledger.attempt_state("attempt-1")


def test_exact_attempt_retry_is_idempotent_after_reservation(monkeypatch, tmp_path) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    assessment = assess_provider_account_headroom(
        ledger, acquired, plan_id="p1", action_id="a1"
    )

    first = reserve_observed_provider_headroom(
        ledger, acquired, assessment, attempt_id="attempt-1"
    )
    second = reserve_observed_provider_headroom(
        ledger, acquired, assessment, attempt_id="attempt-1"
    )

    assert second.attempt_fingerprint == first.attempt_fingerprint
    assert second.reserved_at == first.reserved_at
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED


def test_instance_shadow_cannot_bypass_snapshot_or_reservation_cas(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    calls = {"snapshot": 0, "begin": 0}

    def fake_snapshot():
        calls["snapshot"] += 1
        raise AssertionError("instance-shadowed verified_snapshot must not run")

    def fake_begin_attempt(**kwargs):
        calls["begin"] += 1
        raise AssertionError("instance-shadowed begin_attempt must not run")

    ledger.verified_snapshot = fake_snapshot
    ledger.begin_attempt = fake_begin_attempt

    assessment = assess_provider_account_headroom(
        ledger, acquired, plan_id="p1", action_id="a1"
    )
    reserved = reserve_observed_provider_headroom(
        ledger, acquired, assessment, attempt_id="attempt-1"
    )

    assert calls == {"snapshot": 0, "begin": 0}
    assert reserved.product_internal_reservation_proven is True
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED



class _SequencedDateTime(datetime):
    values: list[datetime] = []

    @classmethod
    def now(cls, tz=None):
        if not cls.values:
            raise AssertionError("unexpected provider clock read")
        value = cls.values.pop(0)
        if tz is None:
            return cls(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
            )
        return cls.fromtimestamp(value.timestamp(), tz=tz)


def test_headroom_clock_rebinding_cannot_mint_fresh_balance(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "10")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    hostile_calls = []

    def hostile_clock():
        hostile_calls.append(True)
        assert acquired.snapshot.balance is not None
        return datetime.fromisoformat(acquired.snapshot.balance.observed_at)

    monkeypatch.setattr(headroom_module, "_utc_now", hostile_clock)

    with pytest.raises(
        ProviderAccountHeadroomError,
        match="headroom clock authority changed",
    ):
        assess_provider_account_headroom(
            ledger,
            acquired,
            plan_id="p1",
            action_id="a1",
        )

    assert hostile_calls == []


def test_freshness_is_bound_to_balance_observation_not_later_snapshot_time(
    monkeypatch,
    tmp_path,
) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=2)
    # developer-app identity evidence, account-details evidence, account-funds
    # evidence, then the final BookmakerAccountSnapshot observation time.
    _SequencedDateTime.values = [old, old, old, now]
    monkeypatch.setattr(betfair_readonly, "datetime", _SequencedDateTime)

    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    assert acquired.snapshot.balance is not None
    assert acquired.snapshot.balance.observed_at != acquired.receipt.acquired_at

    ledger = _ledger_with_plans(
        tmp_path,
        _plan("p1", _action("a1", "10")),
    )
    with pytest.raises(
        ProviderAccountHeadroomStale,
        match="provider balance observation exceeds",
    ):
        assess_provider_account_headroom(
            ledger,
            acquired,
            plan_id="p1",
            action_id="a1",
        )


def test_unrelated_provider_attempt_does_not_block_betfair_account_scope(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    target = _action("target", "20")
    foreign = ExecutionAction(
        action_id="foreign",
        bookmaker_id="betdaq",
        account_id="betdaq-account",
        event_id="event-foreign",
        market_id="market-foreign",
        selection_id="selection-foreign",
        side="BACK",
        requested_odds="2.00",
        requested_stake="900",
        quote_id="quote-foreign",
        quote_observed_at=(datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
    )
    ledger = _ledger_with_plans(
        tmp_path,
        _plan("target-plan", target),
        _plan("foreign-plan", foreign),
    )
    ledger.begin_attempt(
        plan_id="foreign-plan",
        action_id="foreign",
        attempt_id="foreign-attempt",
    )
    ledger.mark_submitted("foreign-attempt")

    assessment = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id="target-plan",
        action_id="target",
    )

    assert assessment.definitely_unreflected_product_liability == 0
    assert assessment.unknown_reflection_product_liability == 0
    assert assessment.lower_headroom == Decimal("100")
    assert assessment.upper_headroom == Decimal("100")
    assert assessment.decision is HeadroomDecision.SUFFICIENT_LOWER_BOUND


def test_recomputed_insufficient_assessment_can_resolve_exact_existing_attempt(
    monkeypatch,
    tmp_path,
) -> None:
    acquired, _ = _acquire_balance(monkeypatch, tmp_path, "100")
    action = _action("a1", "100")
    ledger = _ledger_with_plans(tmp_path, _plan("p1", action))
    initial = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )
    first = reserve_observed_provider_headroom(
        ledger,
        acquired,
        initial,
        attempt_id="attempt-1",
    )

    recomputed = assess_provider_account_headroom(
        ledger,
        acquired,
        plan_id="p1",
        action_id="a1",
    )
    assert recomputed.decision is HeadroomDecision.INSUFFICIENT_UPPER_BOUND
    assert recomputed.definitely_unreflected_product_liability == Decimal("100")

    replay = reserve_observed_provider_headroom(
        ledger,
        acquired,
        recomputed,
        attempt_id="attempt-1",
    )
    assert replay.attempt_fingerprint == first.attempt_fingerprint
    assert replay.reserved_at == first.reserved_at
    assert ledger.attempt_state("attempt-1") is AttemptState.RESERVED
