from __future__ import annotations

from pathlib import Path
import hashlib
import multiprocessing
import os
import tempfile

import pytest

import autosport.betfair_supervised_execution as betfair_execution
from autosport.betfair_supervised_execution import (
    BetfairSupervisedExecutionError,
    PlaceOrdersOutcome,
    execute_betfair_supervised_action,
)
from autosport.execution_quote_chain import build_execution_quote_chain_evidence
from autosport.real_execution_ledger import (
    AttemptState,
    ExecutionLedgerBusyError,
    RealExecutionLedger,
)
from autosport.supervised_execution import (
    SupervisedExecutionError,
    revoke_supervised_approval,
)
from autosport.workspace_lock import WorkspaceEconomicLock
from test_betfair_supervised_execution import (
    APPROVAL_EXPIRES_AT,
    QUOTE_EXPIRES_AT,
    READBACK_AT,
    RESERVED_AT,
    SUBMITTED_AT,
    _Transport,
    _enabled_client,
    _prepared,
    _response,
)


def _crash_during_provider_send_worker(workspace: str) -> None:
    import autosport.supervised_execution as supervised_execution

    supervised_execution._trusted_now = lambda: RESERVED_AT
    profile, bound, approval, ledger, action, goal_store = _prepared(workspace)
    trusted_times = iter((RESERVED_AT, SUBMITTED_AT))
    supervised_execution._trusted_now = lambda: next(trusted_times, SUBMITTED_AT)

    def crash_after_submitted(_request):
        os._exit(91)

    transport = _Transport(crash_after_submitted)
    client = _enabled_client(profile, transport, store=goal_store)
    execute_betfair_supervised_action(
        ledger,
        bound,
        approval,
        action_id=action.action_id,
        attempt_id="attempt-crash-after-submitted",
        profile=profile,
        client=client,
        clock=lambda: SUBMITTED_AT,
    )
    os._exit(92)


@pytest.fixture(autouse=True)
def _fixed_final_fence_clock(monkeypatch) -> None:
    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        lambda: RESERVED_AT,
    )


def _set_trusted_clock_sequence(monkeypatch, *values: str) -> None:
    assert values
    sequence = iter(values)
    final = values[-1]

    def trusted_now() -> str:
        try:
            return next(sequence)
        except StopIteration:
            return final

    monkeypatch.setattr(
        "autosport.supervised_execution._trusted_now",
        trusted_now,
    )


def _assert_reserved_without_submission(
    ledger: RealExecutionLedger,
    plan_id: str,
    attempt_id: str,
) -> None:
    view = ledger.verified_execution_view(plan_id)
    attempts = [
        item
        for item in view.attempts
        if item.attempt.attempt_id == attempt_id
    ]
    assert len(attempts) == 1
    assert attempts[0].state is AttemptState.RESERVED
    assert attempts[0].submitted_at is None


def test_durable_revocation_after_attempt_reservation_denies_final_send(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        original_begin = betfair_execution.begin_supervised_attempt

        def begin_then_revoke(*args, **kwargs):
            attempt = original_begin(*args, **kwargs)
            revoke_supervised_approval(
                ledger,
                bound,
                approval,
                revocation_evidence_sha256="d" * 64,
            )
            return attempt

        monkeypatch.setattr(
            betfair_execution,
            "begin_supervised_attempt",
            begin_then_revoke,
        )

        with pytest.raises(
            SupervisedExecutionError,
            match="durable supervised approval is missing or revoked",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-revoked-before-final-send",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-revoked-before-final-send",
        )


def test_cross_instance_revocation_committed_before_final_fence_denies(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        competing = RealExecutionLedger(Path(tmp) / "real.jsonl")
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        original_begin = betfair_execution.begin_supervised_attempt

        def begin_then_competing_revoke(*args, **kwargs):
            attempt = original_begin(*args, **kwargs)
            competing.revoke_supervised_approval(
                plan_id=bound.execution_plan.plan_id,
                approval_id=approval.ledger_identity,
                approval_fingerprint=approval.fingerprint,
                revoked_at=RESERVED_AT,
                revocation_evidence_sha256="c" * 64,
            )
            return attempt

        monkeypatch.setattr(
            betfair_execution,
            "begin_supervised_attempt",
            begin_then_competing_revoke,
        )

        with pytest.raises(
            SupervisedExecutionError,
            match="durable supervised approval is missing or revoked",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-cross-instance-revoked-first",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-cross-instance-revoked-first",
        )


def test_process_kill_after_submitted_releases_writer_for_restart_recovery(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_crash_during_provider_send_worker,
            args=(tmp,),
        )
        process.start()
        process.join(timeout=30)
        if process.is_alive():
            process.kill()
            process.join()
            pytest.fail("provider-send crash worker did not terminate")

        assert process.exitcode == 91

        restarted = RealExecutionLedger(Path(tmp) / "real.jsonl")
        assert restarted._lock_path.exists()
        assert (
            restarted.attempt_state("attempt-crash-after-submitted")
            is AttemptState.SUBMITTED
        )

        monkeypatch.setattr(
            "autosport.real_execution_ledger._now",
            lambda: READBACK_AT,
        )
        assert restarted.recover_uncertain() == (
            "attempt-crash-after-submitted",
        )
        assert (
            restarted.attempt_state("attempt-crash-after-submitted")
            is AttemptState.UNKNOWN
        )
        events = restarted._events()
        attempt_events = [
            event
            for event in events
            if event["attempt_id"] == "attempt-crash-after-submitted"
        ]
        assert attempt_events
        assert not restarted.can_retry_action(
            plan_id=attempt_events[0]["plan_id"],
            action_id=attempt_events[0]["action_id"],
        )


def test_cross_instance_revocation_cannot_commit_during_provider_send() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        competing = RealExecutionLedger(Path(tmp) / "real.jsonl")
        revocation_was_fenced = False

        def respond_while_revocation_attempts_to_commit(request):
            nonlocal revocation_was_fenced
            with pytest.raises(ExecutionLedgerBusyError):
                competing.revoke_supervised_approval(
                    plan_id=bound.execution_plan.plan_id,
                    approval_id=approval.ledger_identity,
                    approval_fingerprint=approval.fingerprint,
                    revoked_at=SUBMITTED_AT,
                    revocation_evidence_sha256="e" * 64,
                )
            revocation_was_fenced = True
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        transport = _Transport(respond_while_revocation_attempts_to_commit)
        client = _enabled_client(profile, transport, store=goal_store)
        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-revoke-race",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert revocation_was_fenced
        assert len(transport.calls) == 1
        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert result.attempt_state is AttemptState.ACCEPTED
        assert ledger.supervised_approval_is_active(
            plan_id=bound.execution_plan.plan_id,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
        )

        competing.revoke_supervised_approval(
            plan_id=bound.execution_plan.plan_id,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
            revoked_at=SUBMITTED_AT,
            revocation_evidence_sha256="e" * 64,
        )
        assert not ledger.supervised_approval_is_active(
            plan_id=bound.execution_plan.plan_id,
            approval_id=approval.ledger_identity,
            approval_fingerprint=approval.fingerprint,
        )


def test_submitted_fact_is_durable_and_cross_instance_visible_before_post(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        observer = RealExecutionLedger(Path(tmp) / "real.jsonl")
        observed_submitted = False
        _set_trusted_clock_sequence(
            monkeypatch,
            RESERVED_AT,
            SUBMITTED_AT,
        )

        def inspect_then_respond(request):
            nonlocal observed_submitted
            view = observer.verified_execution_view(bound.execution_plan.plan_id)
            attempt = next(
                item
                for item in view.attempts
                if item.attempt.attempt_id == "attempt-durable-before-post"
            )
            assert attempt.state is AttemptState.SUBMITTED
            assert attempt.submitted_at == SUBMITTED_AT
            assert attempt.provider_order_ref is not None
            observed_submitted = True
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        transport = _Transport(inspect_then_respond)
        client = _enabled_client(profile, transport, store=goal_store)
        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-durable-before-post",
            profile=profile,
            client=client,
            clock=lambda: "1900-01-01T00:00:00+00:00",
        )

        assert observed_submitted
        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert result.attempt_state is AttemptState.ACCEPTED



def test_final_send_persists_exact_serialized_request_digest_across_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-durable-request-digest",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.ACCEPTED
        assert len(transport.calls) == 1
        request = transport.calls[0]["request"]
        expected_digest = hashlib.sha256(
            betfair_execution._canonical_bytes(request)
        ).hexdigest()

        restarted = RealExecutionLedger(Path(tmp) / "real.jsonl")
        view = restarted.verified_execution_view(
            bound.execution_plan.plan_id
        )
        attempt = next(
            item
            for item in view.attempts
            if item.attempt.attempt_id
            == "attempt-durable-request-digest"
        )
        assert attempt.submitted_request_sha256 == expected_digest
        assert attempt.provider_evidence is not None
        assert attempt.provider_evidence.request_sha256 == expected_digest

        chain = build_execution_quote_chain_evidence(
            restarted,
            plan_id=bound.execution_plan.plan_id,
            attempt_id="attempt-durable-request-digest",
        )
        assert chain.submission_instruction_sha256 == expected_digest
        assert chain.provider_request_sha256 == expected_digest
        assert chain.submit_instruction_identity_bound is True
        assert chain.provider_request_correlation_bound is True
        assert chain.actual_submitted_instruction_bound is True

        reconstructed = dict(request)
        reconstructed["id"] = request["id"] + 1
        reconstructed_digest = hashlib.sha256(
            betfair_execution._canonical_bytes(reconstructed)
        ).hexdigest()
        assert reconstructed_digest != attempt.submitted_request_sha256



def test_quote_expiry_exact_boundary_denies_before_submitted_or_transport(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_clock_sequence(
            monkeypatch,
            RESERVED_AT,
            QUOTE_EXPIRES_AT,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="final send is at/after quote expiry",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-expired-quote-final-send",
                profile=profile,
                client=client,
                clock=lambda: RESERVED_AT,
            )

        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-expired-quote-final-send",
        )


def test_caller_clock_cannot_mask_trusted_approval_expiry_before_transport(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        _set_trusted_clock_sequence(
            monkeypatch,
            RESERVED_AT,
            APPROVAL_EXPIRES_AT,
        )

        with pytest.raises(
            SupervisedExecutionError,
            match="supervised approval is not active",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-expired-approval-final-send",
                profile=profile,
                client=client,
                clock=lambda: RESERVED_AT,
            )

        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-expired-approval-final-send",
        )


def test_final_send_cannot_precede_attempt_reservation(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        before_reservation = "2026-09-19T08:00:02.500000+00:00"
        _set_trusted_clock_sequence(
            monkeypatch,
            RESERVED_AT,
            before_reservation,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="final send time precedes attempt reservation",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-final-clock-before-reservation",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-final-clock-before-reservation",
        )


def test_second_local_gate_denial_is_not_mislabeled_provider_unknown(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        original_require = type(client._gate).require
        calls = 0

        def deny_second_require(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BetfairSupervisedExecutionError(
                    "synthetic final local authority denial"
                )
            return original_require(self, **kwargs)

        monkeypatch.setattr(type(client._gate), "require", deny_second_require)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="synthetic final local authority denial",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-local-final-denial",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert calls == 2
        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-local-final-denial",
        )




def test_instance_shadowed_place_action_cannot_mint_submission_or_evidence(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        forged_calls: list[str] = []

        def forged_place_action(*args, **kwargs):
            del args
            forged_calls.append("forged")
            callback = kwargs["_before_transport"]
            callback("f" * 64)
            raise AssertionError("shadowed place_action must never execute")

        monkeypatch.setattr(client, "place_action", forged_place_action)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="shadows canonical place_action dispatch",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-shadowed-place-action",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert transport.calls == []
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        attempt = next(
            item
            for item in view.attempts
            if item.attempt.attempt_id == "attempt-shadowed-place-action"
        )
        assert attempt.state is AttemptState.RESERVED
        assert attempt.submitted_at is None
        assert attempt.submitted_request_sha256 is None
        assert attempt.provider_evidence is None



def test_unexpected_transport_exception_after_submitted_becomes_unknown() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)

        def crash_after_transport_entry(_request):
            raise RuntimeError("synthetic transport crash")

        transport = _Transport(crash_after_transport_entry)
        client = _enabled_client(profile, transport, store=goal_store)
        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-unexpected-transport-crash",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert len(transport.calls) == 1
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert (
            ledger.attempt_state("attempt-unexpected-transport-crash")
            is AttemptState.UNKNOWN
        )


def test_post_response_local_validation_failure_remains_effect_ambiguous() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(
            profile,
            transport,
            store=goal_store,
            observed_at="not-an-iso-time",
        )

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-invalid-post-response-clock",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert len(transport.calls) == 1
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN


def test_final_writer_lock_conflict_fails_closed_before_submission(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        original_bind = ledger.bind_provider_order_reference
        competing_writer = WorkspaceEconomicLock(
            Path(tmp),
            file_name=ledger._lock_path.name,
        )

        def bind_then_block_final_writer(*args, **kwargs):
            provider_ref = original_bind(*args, **kwargs)
            competing_writer.acquire()
            return provider_ref

        monkeypatch.setattr(
            ledger,
            "bind_provider_order_reference",
            bind_then_block_final_writer,
        )

        try:
            with pytest.raises(ExecutionLedgerBusyError):
                execute_betfair_supervised_action(
                    ledger,
                    bound,
                    approval,
                    action_id=action.action_id,
                    attempt_id="attempt-final-writer-busy",
                    profile=profile,
                    client=client,
                    clock=lambda: SUBMITTED_AT,
                )
        finally:
            competing_writer.release()

        assert transport.calls == []
        _assert_reserved_without_submission(
            ledger,
            bound.execution_plan.plan_id,
            "attempt-final-writer-busy",
        )
