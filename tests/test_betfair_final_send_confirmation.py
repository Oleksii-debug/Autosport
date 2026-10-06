from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tempfile

import pytest

import autosport.betfair_execution_confirmation as confirmation_runtime
import autosport.betfair_supervised_execution as betfair_execution_runtime
import autosport.supervised_confirmation as confirmation_store_runtime
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
from autosport.supervised_confirmation import (
    SupervisedConfirmationAuthority,
    SupervisedConfirmationConflictError,
)
from test_betfair_supervised_execution import (
    QUOTE_EXPIRES_AT,
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



def test_confirmation_binding_validator_rebinding_fails_before_submission(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-validator-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        monkeypatch.setattr(
            confirmation_runtime,
            "_require_confirmation_binding",
            lambda *args, **kwargs: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED


def test_generic_confirmation_method_rebinding_fails_before_submission(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-generic-confirmation-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        original = (
            confirmation_store_runtime.SupervisedConfirmationAuthority
            .resolve_receipt_binding
        )

        def rebound(self, *args, **kwargs):
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            confirmation_store_runtime.SupervisedConfirmationAuthority,
            "resolve_receipt_binding",
            rebound,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED



def test_coordinated_cached_resolver_root_rebinding_fails_closed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-coordinated-resolver-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        original = (
            confirmation_store_runtime.SupervisedConfirmationAuthority
            .resolve_receipt_binding
        )

        def rebound(self, *args, **kwargs):
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            confirmation_store_runtime.SupervisedConfirmationAuthority,
            "resolve_receipt_binding",
            rebound,
        )
        monkeypatch.setattr(
            confirmation_runtime,
            "_RESOLVE_BINDING",
            rebound,
        )
        monkeypatch.setattr(
            confirmation_runtime,
            "_RESOLVE_BINDING_CODE",
            rebound.__code__,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED


def test_last_provider_seam_clock_sample_blocks_exact_quote_expiry(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-final-seam-quote-expiry"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _issue_confirmation(
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
            QUOTE_EXPIRES_AT,
        )

        with pytest.raises(
            BetfairFinalConfirmationDenied,
            match="quote expired at provider send seam",
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
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=False,
        )
        assert binding.receipt.consumed_at is not None
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )



def test_coordinated_cached_consumer_root_rebinding_fails_closed(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-coordinated-consumer-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        original = (
            confirmation_store_runtime.SupervisedConfirmationAuthority
            .consume_receipt
        )

        def rebound(self, *args, **kwargs):
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            confirmation_store_runtime.SupervisedConfirmationAuthority,
            "consume_receipt",
            rebound,
        )
        monkeypatch.setattr(
            confirmation_runtime,
            "_CONSUME_RECEIPT",
            rebound,
        )
        monkeypatch.setattr(
            confirmation_runtime,
            "_CONSUME_RECEIPT_CODE",
            rebound.__code__,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED


def test_raw_final_send_callables_are_not_module_level_bypass_surfaces() -> None:
    assert not hasattr(
        betfair_execution_runtime,
        "_canonical_place_action_dispatch",
    )
    assert not hasattr(
        betfair_execution_runtime,
        "_place_action_with_final_durable_authority",
    )
    assert not hasattr(
        betfair_execution_runtime,
        "_execute_betfair_supervised_action_core",
    )


def test_restart_recovery_promotes_confirmation_denial_to_unknown_without_retry(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-denied-restart"
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

        with pytest.raises(BetfairFinalConfirmationDenied):
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

        from autosport.real_execution_ledger import RealExecutionLedger

        restarted = RealExecutionLedger(Path(tmp) / "real.jsonl")
        promoted = restarted.recover_uncertain(
            reason="restart_after_final_confirmation_denial"
        )
        assert attempt_id in promoted
        assert restarted.attempt_state(attempt_id) is AttemptState.UNKNOWN
        assert not restarted.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )



def test_rebound_outer_confirmation_guard_cannot_mask_transitive_drift(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-outer-guard-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        monkeypatch.setattr(
            betfair_execution_runtime,
            "_betfair_confirmation_graph_unchanged",
            lambda: True,
        )
        monkeypatch.setattr(
            confirmation_runtime,
            "_require_confirmation_binding",
            lambda *args, **kwargs: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED


def test_in_place_confirmation_guard_code_swap_fails_before_provider_io(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-guard-code-swap"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        guard = betfair_execution_runtime._betfair_confirmation_graph_unchanged

        def forged_guard() -> bool:
            return True

        monkeypatch.setattr(guard, "__code__", forged_guard.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation guard authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED



def test_confirmation_graph_helper_rebinding_cannot_self_authorize(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-helper-rebound"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        monkeypatch.setattr(
            betfair_execution_runtime,
            "_callable_graph_unchanged",
            lambda *args, **kwargs: True,
        )
        monkeypatch.setattr(
            confirmation_runtime,
            "_require_confirmation_binding",
            lambda *args, **kwargs: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED


def test_confirmation_graph_snapshot_retarget_cannot_self_authorize(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-confirmation-snapshot-retarget"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT)

        monkeypatch.setattr(
            confirmation_runtime,
            "_require_confirmation_binding",
            lambda *args, **kwargs: None,
        )
        retargeted = tuple(
            (name, value, getattr(value, "__code__", None))
            for name, value in sorted(vars(confirmation_runtime).items())
            if callable(value)
        )
        monkeypatch.setattr(
            betfair_execution_runtime,
            "_BETFAIR_CONFIRMATION_CALLABLE_GRAPH",
            retargeted,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="confirmation authority changed",
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
        assert ledger.attempt_state(attempt_id) is AttemptState.RESERVED



def test_receipt_for_other_attempt_never_reaches_provider(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target_attempt = "attempt-target-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        _authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id="attempt-other-confirmation",
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT, SUBMITTED_AT, SUBMITTED_AT)

        with pytest.raises(
            BetfairFinalConfirmationDenied,
            match="confirmation denied final provider send",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id=target_attempt,
                profile=profile,
                client=client,
                confirmation_receipt_id=receipt.receipt_id,
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(target_attempt) is AttemptState.SUBMITTED
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_preconsumed_confirmation_never_reaches_provider(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-preconsumed-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        authority.consume_receipt(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            consumer_key="foreign-consumer",
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT, SUBMITTED_AT, SUBMITTED_AT)

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
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        assert not ledger.can_retry_action(
            plan_id=bound.execution_plan.plan_id,
            action_id=action.action_id,
        )


def test_foreign_workspace_confirmation_never_reaches_provider(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-foreign-workspace-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        foreign = Path(tmp) / "foreign-confirmation-workspace"
        authority, review, receipt = _issue_confirmation(
            foreign,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT, SUBMITTED_AT, SUBMITTED_AT)

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
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=True,
        )
        assert binding.receipt.consumed_at is None


def test_confirmation_lifetime_cannot_exceed_approval(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-overlong-confirmation"
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        authority, review, receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
            ttl_seconds=3600,
        )
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_times(monkeypatch, RESERVED_AT, SUBMITTED_AT, SUBMITTED_AT)

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
                confirmation_review_sha256=review.review_sha256,
            )

        assert transport.calls == []
        assert ledger.attempt_state(attempt_id) is AttemptState.SUBMITTED
        binding = authority.resolve_receipt_binding(
            receipt_id=receipt.receipt_id,
            expected_review_sha256=review.review_sha256,
            require_unconsumed=True,
        )
        assert binding.receipt.consumed_at is None



def test_risk_evidence_cannot_rebind_same_final_send_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        attempt_id = "attempt-risk-evidence-stability"
        profile, bound, approval, _ledger, action, _goal_store = _prepared(tmp)
        authority, review, _receipt = _issue_confirmation(
            tmp,
            bound,
            approval,
            action,
            attempt_id=attempt_id,
        )
        changed = betfair_execution_confirmation_spec(
            bound,
            approval,
            action_id=action.action_id,
            attempt_id=attempt_id,
            review_id="review-risk-evidence-changed",
            risk_evidence_sha256="e" * 64,
        )

        assert changed.decision_id == review.decision_id
        assert changed.decision_sha256 != review.decision_sha256
        with pytest.raises(
            SupervisedConfirmationConflictError,
            match="decision_id is already bound to different durable decision evidence",
        ):
            authority.prepare_review(
                review_id=changed.review_id,
                decision_id=changed.decision_id,
                bookmaker_id=changed.bookmaker_id,
                account_id=changed.account_id,
                decision_sha256=changed.decision_sha256,
                approval_evidence_sha256=changed.approval_evidence_sha256,
                risk_evidence_sha256=changed.risk_evidence_sha256,
                review_payload=changed.review_payload,
                ttl_seconds=30,
            )


def test_final_send_review_payload_preserves_domain_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, _ledger, action, _goal_store = _prepared(tmp)
        spec = betfair_execution_confirmation_spec(
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-review-schema",
            review_id="review-schema",
            risk_evidence_sha256="f" * 64,
        )

        assert spec.review_payload["schema"] == "autosport.betfair_final_send_review"
        assert spec.review_payload["schema_version"] == 1
        assert spec.review_payload["decision_id"] == spec.decision_id
        assert spec.review_payload["decision_sha256"] == spec.decision_sha256
        assert spec.review_payload["risk_evidence_sha256"] == spec.risk_evidence_sha256
