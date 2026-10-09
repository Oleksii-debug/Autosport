"""Offline Plan-4 Section-4 confirmation contract falsifiers.

No bookmaker credentials, transport, money movement, or account needed.
The final send adapter must not treat these descriptive witnesses as authority.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

import pytest

import test_betfair_supervised_execution as existing_fixtures
from autosport.betfair_execution_confirmation import (
    CONFIRMATION_FILENAME,
    BetfairExecutionConfirmationError,
    betfair_execution_confirmation_spec,
    consume_betfair_execution_confirmation,
)
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
)
from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)

CONFIRMED_AT = datetime.fromisoformat("2026-09-19T08:00:02.200000+00:00")
SUBMITTED_AT = "2026-09-19T08:00:04+00:00"
REQUEST_DIGEST = "d" * 64


def _issued_confirmation(tmp_path: Path, *, attempt_id: str = "attempt-A"):
    profile = existing_fixtures._profile()
    bound, approval, _goal = existing_fixtures._bound(profile)
    action = bound.execution_plan.actions[0]
    spec = betfair_execution_confirmation_spec(
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        review_id="review-" + attempt_id,
        risk_evidence_sha256="f" * 64,
    )
    authority = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: CONFIRMED_AT,
    )
    review = authority.prepare_review(
        review_id=spec.review_id,
        decision_id=spec.decision_id,
        bookmaker_id=spec.bookmaker_id,
        account_id=spec.account_id,
        decision_sha256=spec.decision_sha256,
        approval_evidence_sha256=spec.approval_evidence_sha256,
        risk_evidence_sha256=spec.risk_evidence_sha256,
        review_payload=spec.review_payload,
        ttl_seconds=30,
    )
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return bound, approval, action, authority, review, receipt


def test_confirmation_decision_is_bound_to_exact_attempt_and_risk(tmp_path: Path) -> None:
    bound, approval, action, _authority, review, _receipt = _issued_confirmation(tmp_path)
    unchanged = betfair_execution_confirmation_spec(
        bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-A",
        review_id=review.review_id,
        risk_evidence_sha256="f" * 64,
    )
    other_attempt = betfair_execution_confirmation_spec(
        bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-B",
        review_id="review-attempt-B",
        risk_evidence_sha256="f" * 64,
    )
    other_risk = betfair_execution_confirmation_spec(
        bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-A",
        review_id=review.review_id,
        risk_evidence_sha256="e" * 64,
    )
    assert unchanged.decision_sha256 == review.decision_sha256
    assert other_attempt.decision_sha256 != review.decision_sha256
    assert other_risk.decision_sha256 != review.decision_sha256
    assert other_risk.decision_id == unchanged.decision_id


def test_durable_confirmation_one_shot_survives_restart(tmp_path: Path) -> None:
    bound, approval, action, authority, review, receipt = _issued_confirmation(tmp_path)
    witness = consume_betfair_execution_confirmation(
        tmp_path.resolve(), bound, approval,
        action_id=action.action_id,
        attempt_id="attempt-A",
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        request_sha256=REQUEST_DIGEST,
        submitted_at=SUBMITTED_AT,
    )
    assert witness.request_sha256 == REQUEST_DIGEST
    assert datetime.fromisoformat(witness.consumed_at) == datetime.fromisoformat(SUBMITTED_AT)
    restarted = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: datetime.fromisoformat(SUBMITTED_AT),
    )
    durable = restarted.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert durable.receipt.consumed_by == witness.consumer_key
    with pytest.raises(BetfairExecutionConfirmationError):
        consume_betfair_execution_confirmation(
            tmp_path.resolve(), bound, approval,
            action_id=action.action_id,
            attempt_id="attempt-A",
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            request_sha256=REQUEST_DIGEST,
            submitted_at=SUBMITTED_AT,
        )


def test_foreign_attempt_does_not_consume_receipt(tmp_path: Path) -> None:
    bound, approval, action, authority, review, receipt = _issued_confirmation(tmp_path)
    with pytest.raises(BetfairExecutionConfirmationError):
        consume_betfair_execution_confirmation(
            tmp_path.resolve(), bound, approval,
            action_id=action.action_id,
            attempt_id="attempt-B",
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            request_sha256=REQUEST_DIGEST,
            submitted_at=SUBMITTED_AT,
        )
    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    ).receipt.consumed_at is None


def test_wrong_receipt_digest_and_invalid_request_fail_closed(tmp_path: Path) -> None:
    bound, approval, action, authority, review, receipt = _issued_confirmation(tmp_path)
    for wrong_review, wrong_request in (
        ("0" * 64, REQUEST_DIGEST),
        (review.review_sha256, "not-a-sha"),
    ):
        with pytest.raises(BetfairExecutionConfirmationError):
            consume_betfair_execution_confirmation(
                tmp_path.resolve(), bound, approval,
                action_id=action.action_id,
                attempt_id="attempt-A",
                receipt_id=receipt.receipt_id,
                expected_review_sha256=wrong_review,
                request_sha256=wrong_request,
                submitted_at=SUBMITTED_AT,
            )
    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    ).receipt.consumed_at is None


def test_direct_client_rejects_forged_admission_without_ledger_or_provider(
    tmp_path: Path, monkeypatch,
) -> None:
    """A caller-supplied digest callback is not a durable send capability."""
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, _approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    observed: list[str] = []

    def forged_admission(request_digest: str) -> None:
        observed.append(request_digest)
        raise RuntimeError("sensitive-transport-secret")

    for callback in (None, forged_admission, observed.append):
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="direct placeOrders dispatch requires canonical confirmed executor",
        ) as denied:
            client.place_action(
                action,
                profile=profile,
                bound=bound,
                provider_order_ref="a1b2c3",
                execution_workspace=tmp_path.resolve(),
                _before_transport=callback,
            )
        assert "sensitive-transport-secret" not in str(denied.value)
    assert observed == []
    assert transport.calls == []
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_missing_confirmation_denies_before_durable_attempt_and_post(
    tmp_path: Path, monkeypatch,
) -> None:
    """The high-level public executor cannot use the old unconfirmed path."""
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action,
    )

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    for receipt, review in (
        (None, None),
        ("d" * 64, None),
        (None, "e" * 64),
    ):
        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="durable confirmation receipt and review identity are required",
        ):
            execute_betfair_supervised_action(
                ledger, bound, approval,
                action_id=action.action_id,
                attempt_id="missing-confirmation",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
                confirmation_receipt_id=receipt,
                confirmation_review_sha256=review,
            )
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
    assert transport.calls == []


def test_subclass_override_cannot_replace_canonical_provider_dispatch(
    tmp_path: Path, monkeypatch,
) -> None:
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action,
        BetfairSupervisedPlaceOrdersClient,
    )
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    baseline = existing_fixtures._enabled_client(profile, transport, store=store)
    class HostileClient(BetfairSupervisedPlaceOrdersClient):
        def place_action(self, *_args, **_kwargs):
            raise AssertionError("forged provider dispatch must not run")

    hostile = HostileClient(
        baseline._credentials,
        gate=baseline._gate,
        transport=transport,
        clock=lambda: existing_fixtures.READBACK_AT,
    )
    with pytest.raises(TypeError, match="exact BetfairSupervisedPlaceOrdersClient"):
        execute_betfair_supervised_action(
            ledger, bound, approval,
            action_id=action.action_id,
            attempt_id="subclass-forge",
            profile=profile,
            client=hostile,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id="d" * 64,
            confirmation_review_sha256="e" * 64,
        )
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
    assert transport.calls == []


def test_instance_shadow_cannot_redirect_confirmed_send(
    tmp_path: Path, monkeypatch,
) -> None:
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action,
        PlaceOrdersOutcome,
    )
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    _authority, review, receipt = _operator_receipt_for_bound(
        tmp_path, bound, approval, action, attempt_id="instance-shadow",
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(
            request, matched=action.requested_stake,
            average=action.requested_odds,
        )
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    shadow_calls: list[str] = []
    def shadow(*_args, **_kwargs):
        shadow_calls.append("called")
        raise AssertionError("forged dispatch")

    monkeypatch.setattr(client, "place_action", shadow)
    result = execute_betfair_supervised_action(
        ledger, bound, approval,
        action_id=action.action_id,
        attempt_id="instance-shadow",
        profile=profile,
        client=client,
        clock=lambda: SUBMITTED_AT,
        confirmation_receipt_id=receipt.receipt_id,
        confirmation_review_sha256=review.review_sha256,
    )
    assert result.outcome is PlaceOrdersOutcome.ACCEPTED
    assert shadow_calls == []
    assert len(transport.calls) == 1


def test_caller_backdated_clock_cannot_override_trusted_quote_expiry(
    tmp_path: Path, monkeypatch,
) -> None:
    """Expiry between reservation and final send must never reach provider."""
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action,
    )
    from autosport.real_execution_ledger import (
        AttemptState, ExecutionStateError, RealExecutionLedger,
    )

    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    authority, review, receipt = _operator_receipt_for_bound(
        tmp_path, bound, approval, action, attempt_id="expired-mid-flight",
    )
    instants = iter((
        existing_fixtures.RESERVED_AT,
        existing_fixtures.QUOTE_EXPIRES_AT,
    ))
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: next(instants),
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    with pytest.raises(ExecutionStateError, match="quote expiry"):
        execute_betfair_supervised_action(
            ledger, bound, approval,
            action_id=action.action_id,
            attempt_id="expired-mid-flight",
            profile=profile, client=client,
            clock=lambda: existing_fixtures.RESERVED_AT,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )
    assert transport.calls == []
    assert ledger.attempt_state("expired-mid-flight") is AttemptState.RESERVED
    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    ).receipt.consumed_at is None
    restarted = RealExecutionLedger(ledger.path)
    assert "expired-mid-flight" in restarted.recover_uncertain()
    assert restarted.attempt_state("expired-mid-flight") is AttemptState.UNKNOWN
    assert not restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
    )


def _operator_receipt_for_bound(
    tmp_path: Path,
    bound,
    approval,
    action,
    *,
    attempt_id: str,
):
    spec = betfair_execution_confirmation_spec(
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        review_id=f"final-review-{attempt_id}",
        risk_evidence_sha256="f" * 64,
    )
    authority = SupervisedConfirmationAuthority(
        tmp_path / CONFIRMATION_FILENAME,
        clock=lambda: CONFIRMED_AT,
    )
    review = authority.prepare_review(
        review_id=spec.review_id,
        decision_id=spec.decision_id,
        bookmaker_id=spec.bookmaker_id,
        account_id=spec.account_id,
        decision_sha256=spec.decision_sha256,
        approval_evidence_sha256=spec.approval_evidence_sha256,
        risk_evidence_sha256=spec.risk_evidence_sha256,
        review_payload=spec.review_payload,
        ttl_seconds=30,
    )
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return authority, review, receipt


def test_confirmed_send_consumes_exact_receipt_and_preserves_matched_truth(
    tmp_path: Path, monkeypatch,
) -> None:
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action,
        PlaceOrdersOutcome,
    )
    from autosport.real_execution_ledger import AttemptState

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    authority, review, receipt = _operator_receipt_for_bound(
        tmp_path, bound, approval, action, attempt_id="confirmed-attempt",
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(
            request, matched=action.requested_stake,
            average=action.requested_odds,
        )
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    result = execute_betfair_supervised_action(
        ledger, bound, approval,
        action_id=action.action_id,
        attempt_id="confirmed-attempt",
        profile=profile,
        client=client,
        clock=lambda: SUBMITTED_AT,
        confirmation_receipt_id=receipt.receipt_id,
        confirmation_review_sha256=review.review_sha256,
    )
    assert len(transport.calls) == 1
    assert result.outcome is PlaceOrdersOutcome.ACCEPTED
    assert result.attempt_state is AttemptState.ACCEPTED
    verified = ledger.verified_execution_view(bound.execution_plan.plan_id)
    attempt = next(
        entry for entry in verified.attempts
        if entry.attempt.attempt_id == "confirmed-attempt"
    )
    assert attempt.state is AttemptState.ACCEPTED
    assert attempt.submitted_at == existing_fixtures.RESERVED_AT
    assert attempt.acknowledgement is not None
    assert attempt.acknowledgement.accepted_stake == action.requested_stake
    binding = authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
        require_unconsumed=False,
    )
    assert datetime.fromisoformat(binding.receipt.consumed_at) == datetime.fromisoformat(existing_fixtures.RESERVED_AT)
    assert binding.receipt.consumed_by.startswith("betfair-final-send:v1:")
    with pytest.raises(Exception):
        execute_betfair_supervised_action(
            ledger, bound, approval,
            action_id=action.action_id,
            attempt_id="confirmed-attempt",
            profile=profile, client=client,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )
    assert len(transport.calls) == 1


def test_foreign_confirmation_denies_transport_and_stays_unknown_after_restart(
    tmp_path: Path, monkeypatch,
) -> None:
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action, PlaceOrdersOutcome,
    )
    from autosport.real_execution_ledger import RealExecutionLedger, AttemptState

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    authority, review, receipt = _operator_receipt_for_bound(
        tmp_path, bound, approval, action, attempt_id="other-attempt",
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    result = execute_betfair_supervised_action(
        ledger, bound, approval,
        action_id=action.action_id,
        attempt_id="denied-attempt",
        profile=profile,
        client=client,
        clock=lambda: SUBMITTED_AT,
        confirmation_receipt_id=receipt.receipt_id,
        confirmation_review_sha256=review.review_sha256,
    )
    assert result.outcome is PlaceOrdersOutcome.UNKNOWN
    assert result.attempt_state is AttemptState.UNKNOWN
    assert transport.calls == []
    assert authority.resolve_receipt_binding(
        receipt_id=receipt.receipt_id,
        expected_review_sha256=review.review_sha256,
    ).receipt.consumed_at is None
    restarted = RealExecutionLedger(ledger.path)
    assert restarted.attempt_state("denied-attempt") is AttemptState.UNKNOWN
    assert not restarted.can_retry_action(
        plan_id=bound.execution_plan.plan_id,
        action_id=action.action_id,
    )


def test_half_supplied_confirmation_fails_before_ledger_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    from autosport.betfair_supervised_execution import (
        execute_betfair_supervised_action,
    )

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: existing_fixtures.RESERVED_AT,
    )
    profile, bound, approval, ledger, action, store = (
        existing_fixtures._prepared(str(tmp_path))
    )
    transport = existing_fixtures._Transport(
        lambda request: existing_fixtures._response(request)
    )
    client = existing_fixtures._enabled_client(profile, transport, store=store)
    with pytest.raises(BetfairSupervisedExecutionError, match="durable confirmation receipt and review identity are required"):
        execute_betfair_supervised_action(
            ledger, bound, approval,
            action_id=action.action_id, attempt_id="partial-confirm",
            profile=profile, client=client,
            clock=lambda: SUBMITTED_AT,
            confirmation_receipt_id="d" * 64,
        )
    assert transport.calls == []
    assert ledger.saga(bound.execution_plan.plan_id).attempts == {}
