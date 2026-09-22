from __future__ import annotations

import gc
from datetime import datetime
from decimal import Decimal
import json

import pytest

import autosport.betfair_timeout_reconciliation as timeout_resolution
import autosport.real_execution_ledger as ledger_module
import autosport.supervised_provider_evidence as provider_evidence
from autosport.betfair_account_readonly import (
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyClient,
    BetfairSessionCredentials,
)
from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.real_execution_ledger import (
    AcknowledgementStatus,
    ExecutionAction,
    ExecutionPlan,
    RealExecutionLedger,
)
from autosport.supervised_provider_evidence import (
    ProviderEvidenceError,
    VerifiedProviderAbsenceEvidence,
    VerifiedProviderEffectEvidence,
)


LEDGER_TIMEOUT_BOUNDARY = "2026-09-21T18:00:00+00:00"
UNKNOWN_OBSERVED_AT = "2026-09-21T17:59:57+00:00"


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
        source_ref="betfair://profile/timeout-test",
        source_payload_sha256="a" * 64,
    )


def _ledger_with_timeout(
    tmp_path,
    monkeypatch,
    *,
    reason: str = "betfair_placeOrders_ambiguous_effect_requires_readback",
    bind_provider_ref: bool = True,
):
    monkeypatch.setattr(ledger_module, "_now", lambda: LEDGER_TIMEOUT_BOUNDARY)
    action = _action()
    plan = ExecutionPlan(
        plan_id="plan-1",
        bookmaker_profile_version="profile-1",
        decision_id="decision-1",
        approval_id="approval-1",
        created_at="2026-09-21T17:59:01+00:00",
        actions=(action,),
    )
    path = tmp_path / "real-execution.jsonl"
    ledger = RealExecutionLedger(path)
    ledger.reserve_plan(plan)
    ledger.begin_attempt(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        attempt_id="attempt-1",
        reserved_at="2026-09-21T17:59:50+00:00",
    )
    provider_ref = None
    if bind_provider_ref:
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
        reason=reason,
        observed_at=UNKNOWN_OBSERVED_AT,
    )
    return ledger, action, provider_ref, path


def _absence(
    observed_at: str,
    provider_ref: str | None,
) -> VerifiedProviderAbsenceEvidence:
    return VerifiedProviderAbsenceEvidence(
        bookmaker_id="betfair",
        account_id="acct-1",
        action_id="action-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        observed_at=observed_at,
        current_source_payload_sha256="1" * 64,
        cleared_source_payload_sha256="2" * 64,
        evidence_id="3" * 64,
        provider_order_ref=provider_ref,
    )


def _effect(observed_at: str, provider_ref: str) -> VerifiedProviderEffectEvidence:
    return VerifiedProviderEffectEvidence(
        bookmaker_id="betfair",
        account_id="acct-1",
        action_id="action-1",
        adapter_id="betfair-exchange-jsonrpc-readonly",
        adapter_version="1",
        profile_version=1,
        event_id="event-1",
        market_id="1.234",
        selection_id="42",
        external_receipt_id="bet-1",
        observed_at=observed_at,
        source_payload_sha256="4" * 64,
        status=AcknowledgementStatus.ACCEPTED,
        accepted_odds=Decimal("2.0"),
        accepted_stake=Decimal("10"),
        evidence_id="5" * 64,
        provider_order_ref=provider_ref,
    )


def _resolve(
    monkeypatch,
    ledger,
    action,
    provider_ref,
    evidence,
    *,
    absence_floor: str | None = None,
    absence_ceiling: str | None = None,
    capture_started_at: str | None = None,
    elapsed_visibility_ready: bool = True,
):
    calls = []

    def fake_verify(actual_action, profile, **kwargs):
        calls.append((actual_action, profile, kwargs))
        return evidence

    monkeypatch.setattr(timeout_resolution, "verify_betfair_provider_state", fake_verify)
    monkeypatch.setattr(
        timeout_resolution,
        "_absence_capture_floor",
        lambda readback: absence_floor or evidence.observed_at,
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_absence_capture_ceiling",
        lambda readback: absence_ceiling or evidence.observed_at,
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_betfair_readback_capture_started_at",
        lambda readback: capture_started_at or evidence.observed_at,
    )
    monkeypatch.setattr(
        timeout_resolution,
        "_timeout_elapsed_visibility_ready",
        lambda ledger, attempt_id, capture_started_monotonic_ns: elapsed_visibility_ready,
    )
    result = timeout_resolution._resolve_betfair_timeout_provider_state_core(
        ledger,
        action,
        object(),
        attempt_id="attempt-1",
        expected_profile_sha256="a" * 64,
        readback=object(),
    )
    assert calls[0][0] is action
    assert calls[0][2]["expected_provider_order_ref"] == provider_ref
    return result


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


def _empty_provider_capture(
    action: ExecutionAction,
    provider_ref: str,
    *,
    observed_at: str = "2026-09-21T18:00:16+00:00",
):
    responses = [
        _rpc_result(
            [{"marketId": action.market_id, "event": {"id": action.event_id}}],
            1,
        ),
        _rpc_result({"currentOrders": [], "moreAvailable": False}, 2),
    ]
    for request_id in range(3, 7):
        responses.append(
            _rpc_result({"clearedOrders": [], "moreAvailable": False}, request_id)
        )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_ReadbackTransport(responses),
        clock=lambda: datetime.fromisoformat(observed_at),
        venue_id="betfair",
        account_id="acct-1",
    )
    return client.read_execution_readback(
        action_id=action.action_id,
        market_id=action.market_id,
        provider_order_ref=provider_ref,
    )


def _foreign_ref_provider_capture(
    action: ExecutionAction,
    provider_ref: str,
    *,
    surface: str,
    returned_ref: str | None,
):
    assert returned_ref != provider_ref
    current_orders: list[dict[str, object]] = []
    cleared_by_status: dict[str, list[dict[str, object]]] = {
        "SETTLED": [],
        "VOIDED": [],
        "LAPSED": [],
        "CANCELLED": [],
    }
    if surface == "current":
        current_orders.append(
            {
                "betId": "bet-foreign-current",
                "marketId": action.market_id,
                "selectionId": int(action.selection_id),
                "side": action.side,
                "status": "EXECUTABLE",
                "placedDate": "2026-09-21T18:00:01+00:00",
                "priceSize": {"price": 2.0, "size": 10.0},
                "averagePriceMatched": 0,
                "sizeMatched": 0,
                "sizeRemaining": 10.0,
                **(
                    {"customerOrderRef": returned_ref}
                    if returned_ref is not None
                    else {}
                ),
            }
        )
    elif surface == "cleared":
        cleared_by_status["SETTLED"].append(
            {
                "betId": "bet-foreign-cleared",
                "marketId": action.market_id,
                "selectionId": int(action.selection_id),
                "side": action.side,
                "placedDate": "2026-09-21T18:00:01+00:00",
                "settledDate": "2026-09-21T18:00:02+00:00",
                "priceRequested": 2.0,
                "priceMatched": 2.0,
                "sizeSettled": 10.0,
                "profit": 10.0,
                **(
                    {"customerOrderRef": returned_ref}
                    if returned_ref is not None
                    else {}
                ),
                "eventId": action.event_id,
            }
        )
    else:  # pragma: no cover - parameterization below is exhaustive.
        raise AssertionError(f"unsupported surface {surface}")

    responses = [
        _rpc_result(
            [{"marketId": action.market_id, "event": {"id": action.event_id}}],
            1,
        ),
        _rpc_result(
            {"currentOrders": current_orders, "moreAvailable": False},
            2,
        ),
    ]
    for request_id, status in enumerate(
        ("SETTLED", "VOIDED", "LAPSED", "CANCELLED"),
        start=3,
    ):
        responses.append(
            _rpc_result(
                {
                    "clearedOrders": cleared_by_status[status],
                    "moreAvailable": False,
                },
                request_id,
            )
        )
    client = BetfairReadOnlyClient(
        BetfairSessionCredentials("app-secret", "session-secret"),
        transport=_ReadbackTransport(responses),
        clock=lambda: datetime.fromisoformat("2026-09-21T18:00:16+00:00"),
        venue_id="betfair",
        account_id="acct-1",
    )
    return client.read_execution_readback(
        action_id=action.action_id,
        market_id=action.market_id,
        provider_order_ref=provider_ref,
    )


@pytest.mark.parametrize(
    ("surface", "returned_ref"),
    (
        ("current", "f" * 32),
        ("current", None),
        ("cleared", "f" * 32),
        ("cleared", None),
    ),
)
def test_foreign_or_missing_returned_customer_order_ref_cannot_become_absence(
    surface: str,
    returned_ref: str | None,
) -> None:
    action = _action()
    profile = _profile()
    provider_ref = "a" * 32
    capture = _foreign_ref_provider_capture(
        action,
        provider_ref,
        surface=surface,
        returned_ref=returned_ref,
    )

    with pytest.raises(
        ProviderEvidenceError,
        match=rf"{surface}-order customerOrderRef conflicts with captured execution scope",
    ):
        provider_evidence.verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=provider_ref,
        )


def test_timeout_resolver_propagates_foreign_customer_order_ref_failure(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _foreign_ref_provider_capture(
        action,
        provider_ref,
        surface="current",
        returned_ref="f" * 32,
    )

    with pytest.raises(
        ProviderEvidenceError,
        match="current-order customerOrderRef conflicts with captured execution scope",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            profile,
            attempt_id="attempt-1",
            expected_profile_sha256=profile.profile_id,
            readback=capture,
        )


def test_complete_empty_before_visibility_horizon_stays_indeterminate(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _absence("2026-09-21T18:00:14.999999+00:00", provider_ref)
    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    assert result.definitive is False
    assert result.timeout_boundary_at == LEDGER_TIMEOUT_BOUNDARY
    assert result.visibility_deadline == "2026-09-21T18:00:15+00:00"
    assert result.evidence is None
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="did not pass durable Betfair timeout visibility authority",
    ):
        timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_complete_empty_exactly_at_visibility_horizon_can_issue_absence(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _absence("2026-09-21T18:00:15+00:00", provider_ref)
    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    assert result.definitive is True
    assert result.evidence is evidence
    timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_capture_started_before_deadline_cannot_become_absence_when_last_rpc_finishes_late(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _absence("2026-09-21T18:00:16+00:00", provider_ref)
    result = _resolve(
        monkeypatch,
        ledger,
        action,
        provider_ref,
        evidence,
        absence_floor="2026-09-21T18:00:14.900000+00:00",
    )

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    assert result.evidence is None
    with pytest.raises(timeout_resolution.BetfairTimeoutResolutionError):
        timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_complete_empty_inside_cleared_history_window_can_issue_absence(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _absence("2026-12-19T18:00:00+00:00", provider_ref)

    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    assert result.definitive is True
    assert result.evidence is evidence
    timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_complete_empty_exactly_at_cleared_history_boundary_fails_closed(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _absence("2026-12-20T18:00:00+00:00", provider_ref)

    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_OUTSIDE_CLEARED_HISTORY
    assert result.definitive is False
    assert result.evidence is None
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="did not pass durable Betfair timeout visibility authority",
    ):
        timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_complete_empty_after_cleared_history_window_stays_indeterminate_on_reread(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None

    first = _absence("2026-12-21T18:00:00+00:00", provider_ref)
    second = _absence("2026-12-22T18:00:00+00:00", provider_ref)
    first_result = _resolve(monkeypatch, ledger, action, provider_ref, first)
    second_result = _resolve(monkeypatch, ledger, action, provider_ref, second)

    assert first_result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_OUTSIDE_CLEARED_HISTORY
    assert second_result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_OUTSIDE_CLEARED_HISTORY
    assert first_result.evidence is None
    assert second_result.evidence is None
    assert first_result.definitive is False
    assert second_result.definitive is False


def test_provider_effect_still_wins_after_cleared_history_window(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _effect("2026-12-21T18:00:00+00:00", provider_ref)

    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.EFFECT_PRESENT
    assert result.definitive is True
    assert result.evidence is evidence


def test_direct_generic_bound_absence_is_not_timeout_authoritative(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)

    direct = provider_evidence.verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=provider_ref,
    )
    assert isinstance(direct, VerifiedProviderAbsenceEvidence)
    with pytest.raises(ProviderEvidenceError, match="timeout-horizon authority"):
        provider_evidence.assert_verified_provider_evidence_authoritative(direct)

    monkeypatch.setattr(
        timeout_resolution,
        "_timeout_elapsed_visibility_ready",
        lambda ledger, attempt_id, capture_started_monotonic_ns: True,
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="timeout resolver executable authority changed",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            profile,
            attempt_id="attempt-1",
            expected_profile_sha256=profile.profile_id,
            readback=capture,
        )


def test_bound_absence_not_issued_by_timeout_resolver_is_rejected() -> None:
    evidence = _absence(
        "2026-09-21T18:00:30+00:00",
        "0123456789abcdef0123456789abcdef",
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="did not pass durable Betfair timeout visibility authority",
    ):
        timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_legacy_unbound_absence_keeps_generic_authority_scope() -> None:
    evidence = _absence("2026-09-21T18:00:30+00:00", None)
    timeout_resolution.assert_betfair_timeout_absence_authoritative(evidence)


def test_provider_effect_wins_before_visibility_horizon(tmp_path, monkeypatch) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    evidence = _effect("2026-09-21T18:00:01+00:00", provider_ref)
    result = _resolve(monkeypatch, ledger, action, provider_ref, evidence)

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.EFFECT_PRESENT
    assert result.definitive is True
    assert result.evidence is evidence


def test_caller_unknown_observed_at_cannot_shorten_durable_horizon(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    result = _resolve(
        monkeypatch,
        ledger,
        action,
        provider_ref,
        _absence("2026-09-21T18:00:12.500000+00:00", provider_ref),
    )
    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    assert result.evidence is None


def test_restart_preserves_original_visibility_deadline(tmp_path, monkeypatch) -> None:
    ledger, action, provider_ref, path = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    original_sha = ledger.verified_snapshot().sha256

    restarted = RealExecutionLedger(path)
    evidence = _absence("2026-09-21T18:00:14+00:00", provider_ref)
    result = _resolve(monkeypatch, restarted, action, provider_ref, evidence)

    assert result.visibility_deadline == "2026-09-21T18:00:15+00:00"
    assert result.ledger_snapshot_sha256 == original_sha
    assert result.evidence is None


def test_stale_readback_before_durable_timeout_boundary_fails_closed(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="predates durable ambiguous placement boundary",
    ):
        _resolve(
            monkeypatch,
            ledger,
            action,
            provider_ref,
            _absence("2026-09-21T17:59:59+00:00", provider_ref),
        )


def test_noncanonical_unknown_reason_cannot_mint_timeout_authority(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(
        tmp_path,
        monkeypatch,
        reason="process_restart",
    )
    assert provider_ref is not None
    monkeypatch.setattr(
        timeout_resolution,
        "verify_betfair_provider_state",
        lambda *args, **kwargs: pytest.fail("verifier must not run"),
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="not a canonical ambiguous Betfair",
    ):
        timeout_resolution._resolve_betfair_timeout_provider_state_core(
            ledger,
            action,
            object(),
            attempt_id="attempt-1",
            expected_profile_sha256="a" * 64,
            readback=object(),
        )


def test_missing_durable_provider_ref_cannot_mint_timeout_authority(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(
        tmp_path,
        monkeypatch,
        bind_provider_ref=False,
    )
    assert provider_ref is None
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="lacks durable provider order reference",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            object(),
            attempt_id="attempt-1",
            expected_profile_sha256="a" * 64,
            readback=object(),
        )


def _set_timeout_authority_wall_clock(monkeypatch, value: str) -> None:
    fixed = datetime.fromisoformat(value)

    class _AuthorityDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(timeout_resolution, "datetime", _AuthorityDateTime)


def test_first_postdeadline_negative_capture_never_proves_elapsed_horizon(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    _set_timeout_authority_wall_clock(
        monkeypatch, "2026-09-21T18:00:16+00:00"
    )
    ticks = iter([1_000_000_000])
    monkeypatch.setattr(timeout_resolution, "monotonic_ns", lambda: next(ticks))

    capture = _empty_provider_capture(action, provider_ref)
    result = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        _profile(),
        attempt_id="attempt-1",
        expected_profile_sha256=_profile().profile_id,
        readback=capture,
    )

    assert (
        result.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert result.evidence is None


def test_forward_wall_clock_jump_cannot_manufacture_elapsed_visibility(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None

    wall_values = iter(
        [
            datetime.fromisoformat("2026-09-21T18:00:01+00:00"),
            datetime.fromisoformat("2026-09-21T18:00:30+00:00"),
        ]
    )

    class _JumpingAuthorityDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = next(wall_values)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(timeout_resolution, "datetime", _JumpingAuthorityDateTime)
    ticks = iter([1_000_000_000, 1_200_000_000])
    monkeypatch.setattr(timeout_resolution, "monotonic_ns", lambda: next(ticks))
    profile = _profile()

    first_capture = _empty_provider_capture(
        action,
        provider_ref,
        observed_at="2026-09-21T18:00:01+00:00",
    )
    first = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=first_capture,
    )
    assert first.evidence is None

    second_capture = _empty_provider_capture(
        action,
        provider_ref,
        observed_at="2026-09-21T18:00:30+00:00",
    )
    second = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=second_capture,
    )

    assert (
        second.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert second.evidence is None


def test_fresh_negative_capture_after_full_monotonic_horizon_can_issue_absence(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    _set_timeout_authority_wall_clock(
        monkeypatch, "2026-09-21T18:00:16+00:00"
    )
    ticks = iter([1_000_000_000, 16_000_000_000])
    monkeypatch.setattr(timeout_resolution, "monotonic_ns", lambda: next(ticks))
    profile = _profile()

    first_capture = _empty_provider_capture(action, provider_ref)
    first = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=first_capture,
    )
    assert (
        first.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert first.evidence is None

    second_capture = _empty_provider_capture(action, provider_ref)
    second = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=second_capture,
    )
    assert (
        second.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    )
    assert isinstance(second.evidence, VerifiedProviderAbsenceEvidence)
    timeout_resolution.assert_betfair_timeout_absence_authoritative(second.evidence)


def test_restart_requires_new_process_local_elapsed_horizon(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, path = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    _set_timeout_authority_wall_clock(
        monkeypatch, "2026-09-21T18:00:16+00:00"
    )
    ticks = iter([1_000_000_000, 16_000_000_000])
    monkeypatch.setattr(timeout_resolution, "monotonic_ns", lambda: next(ticks))
    profile = _profile()

    first_capture = _empty_provider_capture(action, provider_ref)
    first = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=first_capture,
    )
    assert first.evidence is None

    restarted = RealExecutionLedger(path)
    second_capture = _empty_provider_capture(action, provider_ref)
    second = timeout_resolution.resolve_betfair_timeout_provider_state(
        restarted,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=second_capture,
    )
    assert (
        second.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_BEFORE_VISIBILITY_HORIZON
    )
    assert second.evidence is None


def test_monotonic_capture_regression_fails_closed(tmp_path, monkeypatch) -> None:
    ledger, _, _, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert (
        timeout_resolution._timeout_elapsed_visibility_ready(
            ledger, "attempt-1", 100
        )
        is False
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="monotonic capture clock regressed",
    ):
        timeout_resolution._timeout_elapsed_visibility_ready(
            ledger, "attempt-1", 99
        )


def test_visibility_horizon_is_fixed_provider_constant() -> None:
    assert timeout_resolution.BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS == 15
    assert timeout_resolution.BETFAIR_CLEARED_HISTORY_MAX_AGE_DAYS == 90


def test_positive_effect_retires_prior_elapsed_visibility_anchor(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    anchor_key = (id(ledger), "attempt-1")
    assert (
        timeout_resolution._timeout_elapsed_visibility_ready(
            ledger,
            "attempt-1",
            1_000_000_000,
        )
        is False
    )
    assert anchor_key in timeout_resolution._timeout_elapsed_visibility_anchors

    result = _resolve(
        monkeypatch,
        ledger,
        action,
        provider_ref,
        _effect("2026-09-21T18:00:16+00:00", provider_ref),
    )

    assert result.kind is timeout_resolution.BetfairTimeoutResolutionKind.EFFECT_PRESENT
    assert anchor_key not in timeout_resolution._timeout_elapsed_visibility_anchors


def test_outside_history_absence_retires_elapsed_visibility_anchor(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    anchor_key = (id(ledger), "attempt-1")
    assert (
        timeout_resolution._timeout_elapsed_visibility_ready(
            ledger,
            "attempt-1",
            1_000_000_000,
        )
        is False
    )
    assert anchor_key in timeout_resolution._timeout_elapsed_visibility_anchors

    result = _resolve(
        monkeypatch,
        ledger,
        action,
        provider_ref,
        _absence("2026-12-21T18:00:00+00:00", provider_ref),
    )

    assert (
        result.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.INDETERMINATE_OUTSIDE_CLEARED_HISTORY
    )
    assert anchor_key not in timeout_resolution._timeout_elapsed_visibility_anchors


def test_definitive_absence_anchor_lifetime_follows_live_authority_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    _set_timeout_authority_wall_clock(
        monkeypatch,
        "2026-09-21T18:00:16+00:00",
    )
    ticks = iter([1_000_000_000, 16_000_000_000])
    monkeypatch.setattr(timeout_resolution, "monotonic_ns", lambda: next(ticks))
    profile = _profile()
    anchor_key = (id(ledger), "attempt-1")

    first_capture = _empty_provider_capture(action, provider_ref)
    first = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=first_capture,
    )
    assert first.evidence is None
    assert anchor_key in timeout_resolution._timeout_elapsed_visibility_anchors

    second_capture = _empty_provider_capture(action, provider_ref)
    second = timeout_resolution.resolve_betfair_timeout_provider_state(
        ledger,
        action,
        profile,
        attempt_id="attempt-1",
        expected_profile_sha256=profile.profile_id,
        readback=second_capture,
    )
    assert (
        second.kind
        is timeout_resolution.BetfairTimeoutResolutionKind.ABSENT_AFTER_VISIBILITY_HORIZON
    )
    assert isinstance(second.evidence, VerifiedProviderAbsenceEvidence)
    timeout_resolution.assert_betfair_timeout_absence_authoritative(second.evidence)
    assert anchor_key in timeout_resolution._timeout_elapsed_visibility_anchors

    del second
    gc.collect()

    assert anchor_key not in timeout_resolution._timeout_elapsed_visibility_anchors


def test_public_timeout_authority_rejects_horizon_rebind(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)

    monkeypatch.setattr(
        timeout_resolution,
        "BETFAIR_TIMEOUT_VISIBILITY_HORIZON_SECONDS",
        0,
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="timeout resolver executable authority changed",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            profile,
            attempt_id="attempt-1",
            expected_profile_sha256=profile.profile_id,
            readback=capture,
        )


def test_provider_absence_assertion_does_not_trust_rebound_timeout_symbol(
    tmp_path, monkeypatch
) -> None:
    _, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)
    direct = provider_evidence.verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=provider_ref,
    )
    assert isinstance(direct, VerifiedProviderAbsenceEvidence)

    monkeypatch.setattr(
        timeout_resolution,
        "assert_betfair_timeout_absence_authoritative",
        lambda evidence: None,
    )
    with pytest.raises(ProviderEvidenceError, match="timeout-horizon authority"):
        provider_evidence.assert_verified_provider_evidence_authoritative(direct)


def test_provider_evidence_assertion_rejects_fingerprint_rebind(
    tmp_path, monkeypatch
) -> None:
    _, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)
    direct = provider_evidence.verify_betfair_provider_state(
        action,
        profile,
        expected_profile_sha256=profile.profile_id,
        readback=capture,
        expected_provider_order_ref=provider_ref,
    )
    assert isinstance(direct, VerifiedProviderAbsenceEvidence)

    monkeypatch.setattr(
        provider_evidence,
        "_verified_provider_evidence_fingerprint",
        lambda evidence: "0" * 64,
    )
    with pytest.raises(
        ProviderEvidenceError,
        match="provider evidence authority binding changed",
    ):
        provider_evidence.assert_verified_provider_evidence_authoritative(direct)


def test_provider_verifier_rejects_transitive_helper_rebind(
    tmp_path, monkeypatch
) -> None:
    _, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)

    monkeypatch.setattr(
        provider_evidence,
        "_complete_current_pages",
        lambda pages: ((), "0" * 64, capture.observed_at),
    )
    with pytest.raises(
        ProviderEvidenceError,
        match="provider evidence executable authority changed",
    ):
        provider_evidence.verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=provider_ref,
        )


def test_timeout_authority_rejects_same_function_code_mutation(
    tmp_path, monkeypatch
) -> None:
    ledger, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)
    helper = timeout_resolution._absence_capture_floor

    def forged_floor(readback):
        del readback
        return "2099-01-01T00:00:00+00:00"

    monkeypatch.setattr(helper, "__code__", forged_floor.__code__)
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="timeout resolver executable code changed",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            profile,
            attempt_id="attempt-1",
            expected_profile_sha256=profile.profile_id,
            readback=capture,
        )


def test_provider_verifier_rejects_readback_origin_method_rebind(
    tmp_path, monkeypatch
) -> None:
    _, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)

    monkeypatch.setattr(
        type(capture),
        "assert_authoritative",
        lambda self: None,
    )
    with pytest.raises(
        ProviderEvidenceError,
        match="provider readback origin authority method changed",
    ):
        provider_evidence.verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=provider_ref,
        )


def test_timeout_authority_rejects_durable_provider_ref_method_rebind(
    tmp_path, monkeypatch
) -> None:
    ledger, action, _, _ = _ledger_with_timeout(tmp_path, monkeypatch)

    monkeypatch.setattr(
        RealExecutionLedger,
        "provider_order_reference",
        lambda self, *, attempt_id, provider_id: "f" * 32,
    )
    with pytest.raises(
        timeout_resolution.BetfairTimeoutResolutionError,
        match="timeout ledger authority method changed: provider_order_reference",
    ):
        timeout_resolution.resolve_betfair_timeout_provider_state(
            ledger,
            action,
            object(),
            attempt_id="attempt-1",
            expected_profile_sha256="a" * 64,
            readback=object(),
        )

def test_provider_verifier_rejects_capability_profile_method_rebind(
    tmp_path, monkeypatch
) -> None:
    _, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()
    capture = _empty_provider_capture(action, provider_ref)

    monkeypatch.setattr(
        BookmakerCapabilityProfile,
        "require",
        lambda self, capability: None,
    )
    with pytest.raises(
        ProviderEvidenceError,
        match="provider capability profile authority method changed: require",
    ):
        provider_evidence.verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=capture,
            expected_provider_order_ref=provider_ref,
        )

def test_provider_verifier_rejects_readback_subclass_override(
    tmp_path, monkeypatch
) -> None:
    _, action, provider_ref, _ = _ledger_with_timeout(tmp_path, monkeypatch)
    assert provider_ref is not None
    profile = _profile()

    class ForgedReadback(BetfairExecutionReadbackEnvelope):
        __slots__ = ()

        def assert_authoritative(self) -> None:
            return None

    forged = object.__new__(ForgedReadback)
    with pytest.raises(
        ProviderEvidenceError,
        match="exact canonical action-scoped readback envelope",
    ):
        provider_evidence.verify_betfair_provider_state(
            action,
            profile,
            expected_profile_sha256=profile.profile_id,
            readback=forged,
            expected_provider_order_ref=provider_ref,
        )

