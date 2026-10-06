from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import json
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
from autosport.real_execution_ledger import (
    AttemptState,
    EventType,
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
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
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
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None



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

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
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
        assert attempt.provider_evidence is None

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


def test_gate_method_rebinding_fails_closed_before_first_gate_execution(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        calls = 0

        def rebound_require(self, **kwargs):
            nonlocal calls
            del self, kwargs
            calls += 1
            raise AssertionError("rebound gate require must never execute")

        monkeypatch.setattr(type(client._gate), "require", rebound_require)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-gate-method-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert calls == 0
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}

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


def test_instance_rebound_gate_cannot_reach_authority_or_transport() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        forged_calls: list[str] = []

        class ForgedGate:
            def require(self, **kwargs):
                del kwargs
                forged_calls.append("require")
                raise AssertionError("forged gate must never execute")

        client._gate = ForgedGate()  # type: ignore[assignment]

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-shadowed-gate",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_instance_rebound_transport_cannot_execute_after_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        forged_calls: list[str] = []

        class ForgedTransport:
            def post(self, **kwargs):
                del kwargs
                forged_calls.append("post")
                raise AssertionError("forged transport must never execute")

        client._transport = ForgedTransport()  # type: ignore[assignment]

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-shadowed-transport",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_in_place_gate_state_mutation_fails_closed_before_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        object.__setattr__(client._gate, "authority_ref", "forged-authority")

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-mutated-gate",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_in_place_credential_mutation_fails_closed_before_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        object.__setattr__(client._credentials, "session_token", "forged-token")

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-mutated-credentials",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_transport_method_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        calls = 0

        def rebound_post(*args, **kwargs):
            nonlocal calls
            del args, kwargs
            calls += 1
            raise AssertionError("rebound transport post must never execute")

        monkeypatch.setattr(type(transport), "post", rebound_post)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-transport-method-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert calls == 0
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_client_clock_rebinding_fails_closed_before_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        client._clock = lambda: "2030-01-01T00:00:00+00:00"

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-clock-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_client_timeout_rebinding_fails_closed_before_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        client._timeout_seconds = 0.001

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-timeout-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_exact_client_without_constructor_binding_cannot_execute() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, _ = _prepared(tmp)
        client = object.__new__(betfair_execution.BetfairSupervisedPlaceOrdersClient)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="no canonical dependency binding",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-unbound-exact-client",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_gate_rebinding_after_outer_check_is_caught_before_first_gate_call(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        forged_calls: list[str] = []
        original_enter = WorkspaceEconomicLock.__enter__

        class ForgedGate:
            def require(self, **kwargs):
                del kwargs
                forged_calls.append("require")
                raise AssertionError("forged gate must never execute")

        def mutate_after_outer_preflight(self):
            entered = original_enter(self)
            client._gate = ForgedGate()  # type: ignore[assignment]
            return entered

        monkeypatch.setattr(WorkspaceEconomicLock, "__enter__", mutate_after_outer_preflight)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-gate-toctou-before-first-gate",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert forged_calls == []
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_gate_rebinding_after_reservation_is_caught_before_final_gate_call(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        original_begin = betfair_execution.begin_supervised_attempt

        class ForgedGate:
            def require(self, **kwargs):
                del kwargs
                raise AssertionError("forged final gate must never execute")

        def reserve_then_rebind(*args, **kwargs):
            result = original_begin(*args, **kwargs)
            client._gate = ForgedGate()  # type: ignore[assignment]
            return result

        monkeypatch.setattr(
            betfair_execution,
            "begin_supervised_attempt",
            reserve_then_rebind,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-gate-toctou-before-final-gate",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        attempt = next(
            item
            for item in view.attempts
            if item.attempt.attempt_id == "attempt-gate-toctou-before-final-gate"
        )
        assert attempt.state is AttemptState.RESERVED
        assert attempt.submitted_at is None
        assert attempt.provider_evidence is None


def test_temporary_default_gate_global_rebinding_is_rejected_before_constructor(
    monkeypatch,
) -> None:
    constructor_calls: list[str] = []

    class ForgedGate:
        def __init__(self) -> None:
            constructor_calls.append("forged")

    monkeypatch.setattr(
        betfair_execution,
        "BetfairSupervisedExecutionGate",
        ForgedGate,
    )
    credentials = betfair_execution.BetfairSessionCredentials(
        "app-key",
        "session-token",
    )

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="constructor authority changed",
    ):
        betfair_execution.BetfairSupervisedPlaceOrdersClient(credentials)

    assert constructor_calls == []


def test_explicit_forged_gate_cannot_be_canonicalized_by_client_binding() -> None:
    class ForgedGate:
        enabled = True

        def require(self, **kwargs) -> None:
            del kwargs
            raise AssertionError("forged gate must never execute")

    credentials = betfair_execution.BetfairSessionCredentials(
        "app-key",
        "session-token",
    )

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="gate must be exact canonical gate",
    ):
        betfair_execution.BetfairSupervisedPlaceOrdersClient(
            credentials,
            gate=ForgedGate(),  # type: ignore[arg-type]
        )


def test_credentials_subclass_cannot_be_canonicalized_by_client_binding() -> None:
    class ForgedCredentials(betfair_execution.BetfairSessionCredentials):
        pass

    credentials = ForgedCredentials("app-key", "session-token")

    with pytest.raises(
        BetfairSupervisedExecutionError,
        match="credentials must be exact canonical credentials",
    ):
        betfair_execution.BetfairSupervisedPlaceOrdersClient(credentials)


def test_client_getattribute_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        original = type(client).__getattribute__
        hostile_calls: list[str] = []

        def rebound_getattribute(self, name):
            if name == "_gate":
                hostile_calls.append(name)
            return original(self, name)

        monkeypatch.setattr(type(client), "__getattribute__", rebound_getattribute)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-client-getattribute-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_calls == []
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_gate_getattribute_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        gate_type = type(client._gate)
        original = gate_type.__getattribute__
        hostile_calls: list[str] = []

        def rebound_getattribute(self, name):
            if name == "require":
                hostile_calls.append(name)
            return original(self, name)

        monkeypatch.setattr(gate_type, "__getattribute__", rebound_getattribute)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-gate-getattribute-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_calls == []
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_transport_getattribute_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        transport_type = type(transport)
        original = transport_type.__getattribute__
        hostile_calls: list[str] = []

        def rebound_getattribute(self, name):
            if name == "post":
                hostile_calls.append(name)
            return original(self, name)

        monkeypatch.setattr(transport_type, "__getattribute__", rebound_getattribute)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-transport-getattribute-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_calls == []
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_gate_owner_authority_method_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        gate_type = type(client._gate)
        hostile_calls = 0

        def rebound_owner_authority(self, **kwargs):
            nonlocal hostile_calls
            del self, kwargs
            hostile_calls += 1
            raise AssertionError("rebound owner authority must never execute")

        monkeypatch.setattr(
            gate_type,
            "_require_current_owner_authority",
            rebound_owner_authority,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-gate-owner-method-rebinding",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_calls == 0
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_provider_write_preflight_has_no_mutable_weak_binding_registry() -> None:
    preflight = betfair_execution._canonical_place_client_preflight
    for cell in preflight.__closure__ or ():
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        assert type(value).__name__ != "WeakKeyDictionary"


def test_provider_write_dependency_proof_mutation_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        namespace = object.__getattribute__(client, "__dict__")
        namespace["_autosport_provider_write_dependency_proof"] = b"\\x00" * 32

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="dependency binding changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-mutated-client-proof",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_provider_network_origin_predicate_is_immutable() -> None:
    predicate = betfair_execution._execution_provider_network_dispatch_is_current
    assert predicate.__closure__ is None
    assert type(predicate.__defaults__) is tuple


def test_structural_transport_response_cannot_mint_durable_provider_truth() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-structural-origin",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert ledger.provider_evidence_binding("attempt-structural-origin") is None
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        attempt = next(
            item
            for item in view.attempts
            if item.attempt.attempt_id == "attempt-structural-origin"
        )
        assert attempt.submitted_at is not None
        assert attempt.provider_evidence is None

def test_late_canonical_transport_shadow_cannot_mint_provider_origin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        gate = betfair_execution.BetfairSupervisedExecutionGate.from_economic_goal_store(
            goal_store,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
        )
        client = betfair_execution.BetfairSupervisedPlaceOrdersClient(
            betfair_execution.BetfairSessionCredentials(
                "app-key",
                "session-token",
            ),
            gate=gate,
            clock=lambda: READBACK_AT,
        )
        transport = object.__getattribute__(client, "_transport")
        forged_calls: list[dict[str, object]] = []

        def forged_post(
            url: str,
            *,
            headers,
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            request = json.loads(body.decode("utf-8"))
            forged_calls.append(
                {
                    "url": url,
                    "headers": dict(headers),
                    "request": request,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        original_append = ledger._append

        def append_then_shadow(
            kind,
            plan_id,
            action_id,
            attempt_id,
            payload,
        ) -> None:
            original_append(
                kind,
                plan_id,
                action_id,
                attempt_id,
                payload,
            )
            if kind is EventType.ATTEMPT_SUBMITTED:
                # Reproduce the exact irreversible-boundary race: preflight has
                # already succeeded and SUBMITTED is durable, but provider I/O
                # has not started yet.
                transport.post = forged_post

        ledger._append = append_then_shadow

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-late-transport-shadow",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert forged_calls == []
        assert (
            ledger.provider_evidence_binding(
                "attempt-late-transport-shadow"
            )
            is None
        )

def test_caller_clock_cannot_mint_provider_origin_authority(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, _approval, _ledger, action, goal_store = _prepared(tmp)
        gate = betfair_execution.BetfairSupervisedExecutionGate.from_economic_goal_store(
            goal_store,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
        )
        calls: list[dict[str, object]] = []

        def provider_post(
            self,
            url: str,
            *,
            headers,
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            request = json.loads(body.decode("utf-8"))
            calls.append(
                {
                    "url": url,
                    "headers": dict(headers),
                    "request": request,
                    "timeout_seconds": timeout_seconds,
                }
            )
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        monkeypatch.setattr(
            betfair_execution.UrllibBetfairHttpTransport,
            "post",
            provider_post,
        )
        monkeypatch.setattr(
            betfair_execution,
            "_execution_provider_network_dispatch_is_current",
            lambda: True,
        )
        client = betfair_execution.BetfairSupervisedPlaceOrdersClient(
            betfair_execution.BetfairSessionCredentials(
                "app-key",
                "session-token",
            ),
            gate=gate,
            clock=lambda: READBACK_AT,
        )

        report = client.place_action(
            action,
            profile=profile,
            bound=bound,
            provider_order_ref="a" * 32,
            execution_workspace=Path(tmp),
        )

        assert len(calls) == 1
        assert report.observed_at == READBACK_AT
        assert report.provider_origin_authoritative is False


def test_product_clock_can_retain_provider_origin_authority(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, _approval, _ledger, action, goal_store = _prepared(tmp)
        gate = betfair_execution.BetfairSupervisedExecutionGate.from_economic_goal_store(
            goal_store,
            bookmaker_id="betfair",
            account_id="acct-1",
            profile_sha256=profile.profile_id,
        )

        def provider_post(
            self,
            url: str,
            *,
            headers,
            body: bytes,
            timeout_seconds: float,
        ) -> bytes:
            request = json.loads(body.decode("utf-8"))
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        monkeypatch.setattr(
            betfair_execution.UrllibBetfairHttpTransport,
            "post",
            provider_post,
        )
        monkeypatch.setattr(
            betfair_execution,
            "_execution_provider_network_dispatch_is_current",
            lambda: True,
        )
        client = betfair_execution.BetfairSupervisedPlaceOrdersClient(
            betfair_execution.BetfairSessionCredentials(
                "app-key",
                "session-token",
            ),
            gate=gate,
        )

        report = client.place_action(
            action,
            profile=profile,
            bound=bound,
            provider_order_ref="b" * 32,
            execution_workspace=Path(tmp),
        )

        assert report.provider_origin_authoritative is True


def test_post_response_dependency_drift_fails_closed_after_submission() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        holder: dict[str, object] = {}

        def mutate_after_response(request):
            client = holder["client"]
            assert isinstance(
                client,
                betfair_execution.BetfairSupervisedPlaceOrdersClient,
            )
            client._clock = lambda: "2030-01-01T00:00:00+00:00"
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        transport = _Transport(mutate_after_response)
        client = _enabled_client(profile, transport, store=goal_store)
        holder["client"] = client

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-post-response-dependency-drift",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert len(transport.calls) == 1
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert (
            ledger.provider_evidence_binding(
                "attempt-post-response-dependency-drift"
            )
            is None
        )
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        attempt = next(
            item
            for item in view.attempts
            if item.attempt.attempt_id
            == "attempt-post-response-dependency-drift"
        )
        assert attempt.submitted_at is not None
        assert attempt.provider_evidence is None

def test_rebound_place_response_parser_cannot_mint_provider_origin_authority(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)
        original_parser = betfair_execution._parse_place_orders_response

        def hostile_parser(*args, **kwargs):
            report = original_parser(*args, **kwargs)
            return replace(
                report,
                provider_origin_authoritative=True,
            )

        monkeypatch.setattr(
            betfair_execution,
            "_parse_place_orders_response",
            hostile_parser,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-place-parser",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        assert all(
            item.attempt.attempt_id != "attempt-rebound-place-parser"
            for item in view.attempts
        )

def test_rebound_execution_report_constructor_cannot_upgrade_structural_response(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(
            lambda request: _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )
        )
        client = _enabled_client(profile, transport, store=goal_store)
        original_report_type = betfair_execution.BetfairPlaceExecutionReport

        def hostile_report_constructor(**kwargs):
            kwargs["provider_origin_authoritative"] = True
            return original_report_type(**kwargs)

        monkeypatch.setattr(
            betfair_execution,
            "BetfairPlaceExecutionReport",
            hostile_report_constructor,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-report-constructor",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        view = ledger.verified_execution_view(bound.execution_plan.plan_id)
        assert all(
            item.attempt.attempt_id != "attempt-rebound-report-constructor"
            for item in view.attempts
        )



def test_action_serializer_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_to_dict(self):
            payload = {
                "action_id": self.action_id,
                "bookmaker_id": self.bookmaker_id,
                "account_id": self.account_id,
                "event_id": self.event_id,
                "market_id": self.market_id,
                "selection_id": self.selection_id,
                "side": self.side,
                "requested_odds": str(self.requested_odds),
                "requested_stake": "0.01",
                "quote_id": self.quote_id,
                "quote_observed_at": self.quote_observed_at,
                "expires_at": self.expires_at,
            }
            return payload

        monkeypatch.setattr(
            betfair_execution.ExecutionAction,
            "to_dict",
            hostile_to_dict,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-action-serializer",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_capability_require_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_require(self, capability):
            del self, capability
            return None

        monkeypatch.setattr(
            betfair_execution.BookmakerCapabilityProfile,
            "require",
            hostile_require,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-capability-require",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_execution_action_field_descriptor_rebinding_fails_before_read(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        hostile_reads = 0

        def hostile_side(self):
            nonlocal hostile_reads
            del self
            hostile_reads += 1
            return "BACK"

        monkeypatch.setattr(
            betfair_execution.ExecutionAction,
            "side",
            property(hostile_side),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-action-field",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_reads == 0
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_report_post_init_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_post_init(self):
            del self
            return None

        monkeypatch.setattr(
            betfair_execution.BetfairPlaceExecutionReport,
            "__post_init__",
            hostile_post_init,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-report-post-init",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_instruction_post_init_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_post_init(self):
            del self
            return None

        monkeypatch.setattr(
            betfair_execution.BetfairInstructionReport,
            "__post_init__",
            hostile_post_init,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-instruction-post-init",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_report_evidence_id_getter_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.BetfairPlaceExecutionReport,
            "evidence_id",
            property(lambda self: "0" * 64),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-report-evidence-id",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_ledger_acknowledge_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_acknowledge(self, acknowledgement):
            del self, acknowledgement
            raise AssertionError("hostile acknowledgement must never execute")

        monkeypatch.setattr(
            betfair_execution.RealExecutionLedger,
            "acknowledge",
            hostile_acknowledge,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-ledger-ack",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []


def test_action_serializer_in_place_code_swap_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        serializer = betfair_execution.ExecutionAction.to_dict

        def hostile_serializer(self):
            return {
                "action_id": self.action_id,
                "bookmaker_id": self.bookmaker_id,
                "account_id": self.account_id,
                "event_id": self.event_id,
                "market_id": self.market_id,
                "selection_id": self.selection_id,
                "side": self.side,
                "requested_odds": str(self.requested_odds),
                "requested_stake": "0.01",
                "quote_id": self.quote_id,
                "quote_observed_at": self.quote_observed_at,
                "expires_at": self.expires_at,
            }

        monkeypatch.setattr(serializer, "__code__", hostile_serializer.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-action-serializer-code-swap",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_capability_require_in_place_code_swap_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        require = betfair_execution.BookmakerCapabilityProfile.require

        def hostile_require(self, capability):
            del self, capability
            return None

        monkeypatch.setattr(require, "__code__", hostile_require.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-capability-require-code-swap",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_trusted_now_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution._supervised_execution_runtime,
            "_trusted_now",
            lambda: "2035-01-01T00:00:00+00:00",
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-trusted-now",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_enter_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_enter(self):
            return self

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "__enter__",
            hostile_enter,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-workspace-lock-enter",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_bound_verify_binding_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_verify(self):
            del self
            return None

        monkeypatch.setattr(
            betfair_execution.BoundSupervisedExecutionPlan,
            "verify_binding",
            hostile_verify,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-bound-verify",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_approval_require_active_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_require_active(self, at):
            del self, at
            return None

        monkeypatch.setattr(
            betfair_execution.SupervisedApproval,
            "require_active",
            hostile_require_active,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-approval-active",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_approval_fingerprint_getter_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.SupervisedApproval,
            "fingerprint",
            property(lambda self: bound.approval_fingerprint),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-approval-fingerprint",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_execution_plan_fingerprint_getter_rebinding_fails_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        durable = ledger.saga(bound.execution_plan.plan_id).plan_fingerprint

        monkeypatch.setattr(
            betfair_execution.ExecutionPlan,
            "fingerprint",
            property(lambda self: durable),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-plan-fingerprint",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_profile_state_of_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.BookmakerCapabilityProfile,
            "state_of",
            lambda self, capability: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-profile-state-of",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_profile_canonical_serializer_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.BookmakerCapabilityProfile,
            "to_canonical_dict",
            lambda self: {"venue_id": self.venue_id},
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-profile-serializer",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_bound_binding_function_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution._supervised_execution_runtime,
            "_bound_binding_sha256",
            lambda *args, **kwargs: "0" * 64,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-bound-binding-function",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_bound_binding_in_place_code_swap_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        binding = betfair_execution._supervised_execution_runtime._bound_binding_sha256

        def hostile_binding(*args, **kwargs):
            del args, kwargs
            return "0" * 64

        monkeypatch.setattr(binding, "__code__", hostile_binding.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-bound-binding-code-swap",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_constraint_serializer_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.ExecutionLegConstraint,
            "to_dict",
            lambda self: {
                "leg_id": self.leg_id,
                "side": self.side,
                "quote_expires_at": self.quote_expires_at,
                "max_slippage_fraction": "0",
            },
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-constraint-serializer",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_durable_approval_lookup_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.RealExecutionLedger,
            "supervised_approval_is_active",
            lambda self, **kwargs: True,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-durable-approval-lookup",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_begin_attempt_rebinding_fails_closed_before_attempt(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.RealExecutionLedger,
            "begin_attempt",
            lambda self, **kwargs: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-begin-attempt",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_request_id_method_instance_shadow_fails_closed_before_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        object.__getattribute__(client, "__dict__")["_next_request_id"] = lambda: 1

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical dependency binding|canonical Betfair client",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-shadow-request-id",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_acknowledgement_post_init_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.ExternalAcknowledgement,
            "__post_init__",
            lambda self: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-ack-post-init",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_acknowledgement_serializer_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.ExternalAcknowledgement,
            "to_dict",
            lambda self: {"attempt_id": self.attempt_id},
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-ack-serializer",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_acknowledgement_serializer_code_swap_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        serializer = betfair_execution.ExternalAcknowledgement.to_dict

        def hostile_serializer(self):
            return {"attempt_id": self.attempt_id}

        monkeypatch.setattr(serializer, "__code__", hostile_serializer.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-ack-serializer-code-swap",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_acknowledgement_field_descriptor_rebinding_fails_before_read(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        hostile_reads = 0

        def hostile_status(self):
            nonlocal hostile_reads
            del self
            hostile_reads += 1
            return betfair_execution.AcknowledgementStatus.ACCEPTED

        monkeypatch.setattr(
            betfair_execution.ExternalAcknowledgement,
            "status",
            property(hostile_status),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-ack-field",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_reads == 0
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_midflight_acknowledgement_serializer_mutation_becomes_unknown(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)

        def mutate_after_submission(request):
            monkeypatch.setattr(
                betfair_execution.ExternalAcknowledgement,
                "to_dict",
                lambda self: {"attempt_id": self.attempt_id},
            )
            return _response(
                request,
                matched=action.requested_stake,
                average=action.requested_odds,
            )

        transport = _Transport(mutate_after_submission)
        client = _enabled_client(profile, transport, store=goal_store)

        result = execute_betfair_supervised_action(
            ledger,
            bound,
            approval,
            action_id=action.action_id,
            attempt_id="attempt-midflight-ack-mutation",
            profile=profile,
            client=client,
            clock=lambda: SUBMITTED_AT,
        )

        assert len(transport.calls) == 1
        assert result.outcome is PlaceOrdersOutcome.UNKNOWN
        assert result.attempt_state is AttemptState.UNKNOWN
        assert result.evidence_id is None
        assert result.external_receipt_id is None
        assert ledger.provider_evidence_binding("attempt-midflight-ack-mutation") is None


def test_workspace_lock_acquire_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "acquire",
            lambda self: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-workspace-acquire",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_os_lock_dispatch_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "_lock_handle",
            lambda self, handle: None,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-workspace-os-lock",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_store_load_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        original_load = betfair_execution.EconomicGoalStore.load

        monkeypatch.setattr(
            betfair_execution.EconomicGoalStore,
            "load",
            lambda self: original_load(goal_store),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-owner-load",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_provenance_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution,
            "provenance_for",
            lambda goal: type("ForgedProvenance", (), {"contract_sha256": "0" * 64})(),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-owner-provenance",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_field_descriptor_rebinding_fails_before_read(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        hostile_reads = 0

        def hostile_automation_level(self):
            nonlocal hostile_reads
            del self
            hostile_reads += 1
            return betfair_execution.AutomationLevel.HIGHER_AUTONOMY

        monkeypatch.setattr(
            betfair_execution.EconomicGoalContract,
            "automation_level",
            property(hostile_automation_level),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-rebound-owner-field",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert hostile_reads == 0
        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_acquire_code_swap_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        acquire = betfair_execution.WorkspaceEconomicLock.acquire

        def hostile_acquire(self):
            del self
            return None

        monkeypatch.setattr(acquire, "__code__", hostile_acquire.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-workspace-acquire-code-swap",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_store_load_code_swap_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        load = betfair_execution.EconomicGoalStore.load

        def hostile_load(self):
            del self
            return None

        monkeypatch.setattr(load, "__code__", hostile_load.__code__)

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-owner-load-code-swap",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_getattribute_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_getattribute(self, name):
            return object.__getattribute__(self, name)

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "__getattribute__",
            hostile_getattribute,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-workspace-getattribute-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_setattr_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_setattr(self, name, value):
            object.__setattr__(self, name, value)

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "__setattr__",
            hostile_setattr,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-workspace-setattr-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_store_getattribute_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_getattribute(self, name):
            return object.__getattribute__(self, name)

        monkeypatch.setattr(
            betfair_execution.EconomicGoalStore,
            "__getattribute__",
            hostile_getattribute,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-owner-store-getattribute-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_file_name_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "FILE_NAME",
            ".hostile-economic-run.lock",
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-workspace-file-name-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_init_kwdefault_mutation_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)
        init = betfair_execution.WorkspaceEconomicLock.__dict__["__init__"]
        kwdefaults = init.__kwdefaults__
        assert kwdefaults is not None
        assert kwdefaults["file_name"] is None

        monkeypatch.setitem(
            kwdefaults,
            "file_name",
            ".hostile-economic-run.lock",
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-workspace-init-kwdefault-mutation",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_store_setattr_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_setattr(self, name, value):
            object.__setattr__(self, name, value)

        monkeypatch.setattr(
            betfair_execution.EconomicGoalStore,
            "__setattr__",
            hostile_setattr,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-owner-store-setattr-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_workspace_lock_file_name_equality_spoof_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    class EqualitySpoof:
        def __eq__(self, other):
            return other == ".economic-run.lock"

    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.WorkspaceEconomicLock,
            "FILE_NAME",
            EqualitySpoof(),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-workspace-file-name-equality-spoof",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_economic_goal_store_file_name_equality_spoof_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    class EqualitySpoof:
        def __eq__(self, other):
            return other == "economic_goal_contract.json"

    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        monkeypatch.setattr(
            betfair_execution.EconomicGoalStore,
            "FILE_NAME",
            EqualitySpoof(),
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-owner-store-file-name-equality-spoof",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
        assert ledger.saga(bound.execution_plan.plan_id).attempts == {}


def test_real_execution_ledger_getattribute_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_getattribute(self, name):
            return object.__getattribute__(self, name)

        monkeypatch.setattr(
            betfair_execution.RealExecutionLedger,
            "__getattribute__",
            hostile_getattribute,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-ledger-getattribute-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []


def test_real_execution_ledger_setattr_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_setattr(self, name, value):
            object.__setattr__(self, name, value)

        monkeypatch.setattr(
            betfair_execution.RealExecutionLedger,
            "__setattr__",
            hostile_setattr,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-ledger-setattr-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []


def test_bound_execution_getattribute_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_getattribute(self, name):
            return object.__getattribute__(self, name)

        monkeypatch.setattr(
            betfair_execution.BoundSupervisedExecutionPlan,
            "__getattribute__",
            hostile_getattribute,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-bound-getattribute-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []


def test_execution_action_getattribute_rebinding_fails_closed_before_attempt(
    monkeypatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile, bound, approval, ledger, action, goal_store = _prepared(tmp)
        transport = _Transport(lambda request: _response(request))
        client = _enabled_client(profile, transport, store=goal_store)

        def hostile_getattribute(self, name):
            return object.__getattribute__(self, name)

        monkeypatch.setattr(
            betfair_execution.ExecutionAction,
            "__getattribute__",
            hostile_getattribute,
        )

        with pytest.raises(
            BetfairSupervisedExecutionError,
            match="canonical Betfair client dispatch changed",
        ):
            execute_betfair_supervised_action(
                ledger,
                bound,
                approval,
                action_id=action.action_id,
                attempt_id="attempt-action-getattribute-rebind",
                profile=profile,
                client=client,
                clock=lambda: SUBMITTED_AT,
            )

        assert transport.calls == []
