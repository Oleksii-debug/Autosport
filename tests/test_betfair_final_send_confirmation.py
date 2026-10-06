from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import pytest

import autosport.supervised_execution as supervised_execution
from autosport.betfair_execution_confirmation import (
    CONFIRMATION_FILENAME,
    betfair_execution_confirmation_spec,
)
from autosport.betfair_supervised_execution import (
    BetfairFinalConfirmationDenied,
    BetfairSupervisedExecutionError,
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
)
from autosport.real_execution_ledger import AttemptState
from autosport.supervised_confirmation import SupervisedConfirmationAuthority
from test_betfair_supervised_execution import (
    RESERVED_AT,
    SUBMITTED_AT,
    _Transport,
    _enabled_client,
    _prepared,
    _response,
)


_CONFIRMATION_AT = datetime.fromisoformat(
    "2026-09-19T08:00:02.200000+00:00"
)


def _set_trusted_times(monkeypatch, *values: str) -> None:
    assert values
    sequence = iter(values)
    final = values[-1]

    def trusted_now() -> str:
        try:
            return next(sequence)
        except StopIteration:
            return final

    monkeypatch.setattr(
        supervised_execution,
        "_trusted_now",
        trusted_now,
    )


def _issue_confirmation(
    root: str,
    bound,
    approval,
    action,
    *,
    attempt_id: str,
    ttl_seconds: int = 120,
):
    workspace = Path(root).resolve()
    spec = betfair_execution_confirmation_spec(
        bound,
        approval,
        action_id=action.action_id,
        attempt_id=attempt_id,
        review_id=f"review-{attempt_id}",
        risk_evidence_sha256="f" * 64,
    )
    authority = SupervisedConfirmationAuthority(
        workspace / CONFIRMATION_FILENAME,
        clock=lambda: _CONFIRMATION_AT,
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
        ttl_seconds=ttl_seconds,
    )
    receipt = authority.confirm_review(
        review_id=review.review_id,
        expected_review_sha256=review.review_sha256,
    )
    return authority, review, receipt


def test_missing_confirmation_fails_closed_before_durable_submission(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="requires durable operator confirmation",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-missing-confirmation",
                profile=profile,
                client=client,
            )

        assert transport.calls == []
        assert (
            ledger.attempt_state("attempt-missing-confirmation")
            is AttemptState.RESERVED
        )


def test_exact_confirmation_is_consumed_before_only_provider_send(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmed"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(
            monkeypatch,
            RESERVED_AT,
            SUBMITTED_AT,
            SUBMITTED_AT,
            SUBMITTED_AT,
        )

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            profile=profile,
            client=client,
            confirmation_receipt_id=receipt.receipt_id,
            confirmation_review_sha256=review.review_sha256,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert len(transport.calls) == 1
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=False,
        )
        assert binding.receipt.consumed_by is not None
        assert binding.receipt.consumed_by.startswith("betfair-final-send:v1:")
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        attempt = next(
            item
            for item in view.attempts
            if item.attempt.attempt_id == attempt_id
        )
        assert attempt.submitted_request_sha256 is not None


def test_mismatched_confirmation_digest_denies_after_submitted_before_post(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-wrong-review"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, _review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(
            monkeypatch,
            RESERVED_AT,
            SUBMITTED_AT,
            SUBMITTED_AT,
        )

        with pytest.raises(
            BetfairFinalConfirmationDenied,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256="0" * 64,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_confirmation_expiring_during_durable_recheck_never_reaches_post(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-expired-at-send"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
            ttl_seconds=2,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(
            monkeypatch,
            RESERVED_AT,
            SUBMITTED_AT,
            "2026-09-19T08:00:04.300000+00:00",
        )

        with pytest.raises(
            BetfairFinalConfirmationDenied,
            match="confirmation changed before provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=attempt_id,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )
