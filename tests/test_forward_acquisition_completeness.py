from __future__ import annotations

import json

import pytest

from autosport.forward_acquisition_completeness import (
    AcquisitionTerminalState,
    ForwardAcquisitionError,
    ForwardAcquisitionIntegrityError,
    ForwardAcquisitionPlan,
    ForwardAcquisitionStore,
    ProviderOpportunitySpec,
    TerminalAcquisitionRecord,
    build_terminal_record,
    terminal_idempotency_key,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64


def _spec(
    provider: str,
    opportunity: str,
    *,
    config: str = B,
    max_attempts: int = 3,
):
    return ProviderOpportunitySpec(
        provider_id=provider,
        opportunity_id=opportunity,
        request_sha256=A,
        config_sha256=config,
        retry_policy_sha256=C,
        max_attempts=max_attempts,
    )


def _plan(*specs: ProviderOpportunitySpec) -> ForwardAcquisitionPlan:
    return ForwardAcquisitionPlan(
        evaluation_id="forward-eval-1",
        frozen_at="2026-09-21T18:00:00Z",
        opportunities=tuple(specs),
    )


def _success_nonempty(plan, spec, *, accepted=2, rejected=0):
    return build_terminal_record(
        plan,
        spec,
        state=AcquisitionTerminalState.SUCCEEDED_NONEMPTY,
        attempt_count=1,
        started_at="2026-09-21T18:01:00Z",
        terminal_at="2026-09-21T18:02:00Z",
        response_evidence_sha256=D,
        provider_row_count=accepted + rejected,
        accepted_row_count=accepted,
        rejected_row_count=rejected,
        rejection_evidence_sha256=E if rejected else None,
    )


def _success_empty(plan, spec):
    return build_terminal_record(
        plan,
        spec,
        state=AcquisitionTerminalState.SUCCEEDED_EMPTY,
        attempt_count=1,
        started_at="2026-09-21T18:01:00Z",
        terminal_at="2026-09-21T18:02:00Z",
        response_evidence_sha256=D,
    )


def test_plan_identity_is_order_independent_but_config_drift_forks_identity():
    left = _spec("p1", "m1")
    right = _spec("p2", "m2")
    assert _plan(left, right).plan_sha256 == _plan(right, left).plan_sha256
    changed = _spec("p1", "m1", config=F)
    assert changed.spec_sha256 != left.spec_sha256
    assert _plan(changed, right).plan_sha256 != _plan(left, right).plan_sha256


def test_freeze_is_idempotent_but_cannot_rebind_plan(tmp_path):
    first = _plan(_spec("p1", "m1"))
    store = ForwardAcquisitionStore(tmp_path)
    assert store.freeze_plan(first) == first.plan_sha256
    assert store.freeze_plan(first) == first.plan_sha256
    with pytest.raises(ForwardAcquisitionIntegrityError, match="different authority"):
        store.freeze_plan(_plan(_spec("p1", "m1", config=F)))


def test_missing_is_not_laundered_into_empty(tmp_path):
    p1 = _spec("p1", "m1")
    p2 = _spec("p2", "m2")
    plan = _plan(p1, p2)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    store.append_terminal(_success_empty(plan, p1))
    report = store.completeness(as_of="2026-09-21T18:03:00Z")
    assert report.succeeded_empty == ("p1:m1",)
    assert report.missing == ("p2:m2",)
    assert report.exhaustive_terminal is False
    assert report.all_successful is False
    assert report.forward_evidence_ready is False


def test_true_empty_requires_positive_response_evidence():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    with pytest.raises(ForwardAcquisitionError, match="positive response evidence"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.SUCCEEDED_EMPTY,
            attempt_count=1,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
        )


def test_nonempty_and_empty_partitions_cannot_contradict_provider_row_count():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    with pytest.raises(ForwardAcquisitionError, match="SUCCEEDED_NONEMPTY"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.SUCCEEDED_NONEMPTY,
            attempt_count=1,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
            response_evidence_sha256=D,
        )
    with pytest.raises(ForwardAcquisitionError, match="SUCCEEDED_EMPTY"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.SUCCEEDED_EMPTY,
            attempt_count=1,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
            response_evidence_sha256=D,
            provider_row_count=1,
            accepted_row_count=1,
        )


def test_failure_cannot_carry_rows_or_positive_response_evidence():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    with pytest.raises(ForwardAcquisitionError, match="positive response"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.FAILED_PERMANENT,
            attempt_count=1,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
            response_evidence_sha256=D,
            failure_evidence_sha256=E,
        )
    with pytest.raises(ForwardAcquisitionError, match="materialized provider rows"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.FAILED_PERMANENT,
            attempt_count=1,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
            failure_evidence_sha256=E,
            provider_row_count=1,
            accepted_row_count=1,
        )


def test_retryable_failure_must_exhaust_frozen_retry_budget():
    spec = _spec("p1", "m1", max_attempts=3)
    plan = _plan(spec)
    with pytest.raises(ForwardAcquisitionIntegrityError, match="maximum attempt count"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.FAILED_RETRYABLE_EXHAUSTED,
            attempt_count=2,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
            failure_evidence_sha256=E,
        )


def test_permanent_failure_stays_visible_in_complete_report(tmp_path):
    spec = _spec("p1", "auth")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    record = build_terminal_record(
        plan,
        spec,
        state=AcquisitionTerminalState.FAILED_PERMANENT,
        attempt_count=1,
        started_at="2026-09-21T18:01:00Z",
        terminal_at="2026-09-21T18:02:00Z",
        failure_evidence_sha256=E,
    )
    store.append_terminal(record)
    report = store.completeness(as_of="2026-09-21T18:03:00Z")
    assert report.exhaustive_terminal is True
    assert report.failed_permanent == ("p1:auth",)
    assert report.all_successful is False
    assert report.forward_evidence_ready is False


def test_retry_exhaustion_is_visible_and_not_empty(tmp_path):
    spec = _spec("p1", "429", max_attempts=2)
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    record = build_terminal_record(
        plan,
        spec,
        state=AcquisitionTerminalState.FAILED_RETRYABLE_EXHAUSTED,
        attempt_count=2,
        started_at="2026-09-21T18:01:00Z",
        terminal_at="2026-09-21T18:04:00Z",
        failure_evidence_sha256=E,
    )
    store.append_terminal(record)
    report = store.completeness(as_of="2026-09-21T18:05:00Z")
    assert report.failed_retryable_exhausted == ("p1:429",)
    assert report.succeeded_empty == ()


def test_parser_drop_cannot_be_laundered_into_empty_or_ready(tmp_path):
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    store.append_terminal(_success_nonempty(plan, spec, accepted=0, rejected=3))
    report = store.completeness(as_of="2026-09-21T18:03:00Z")
    assert report.succeeded_nonempty == ("p1:m1",)
    assert report.rejected_provider_rows == 3
    assert report.exhaustive_terminal is True
    assert report.all_successful is True
    assert report.forward_evidence_ready is False


def test_row_accounting_requires_explicit_rejection_evidence():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    with pytest.raises(ForwardAcquisitionError, match="explicit rejection evidence"):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.SUCCEEDED_NONEMPTY,
            attempt_count=1,
            started_at="2026-09-21T18:01:00Z",
            terminal_at="2026-09-21T18:02:00Z",
            response_evidence_sha256=D,
            provider_row_count=2,
            accepted_row_count=1,
            rejected_row_count=1,
        )


def test_exact_terminal_replay_is_idempotent_but_conflict_fails(tmp_path):
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    first = _success_nonempty(plan, spec)
    assert store.append_terminal(first) == first.record_sha256
    assert store.append_terminal(first) == first.record_sha256
    conflicting = build_terminal_record(
        plan,
        spec,
        state=AcquisitionTerminalState.SUCCEEDED_NONEMPTY,
        attempt_count=2,
        started_at="2026-09-21T18:01:00Z",
        terminal_at="2026-09-21T18:02:30Z",
        response_evidence_sha256=F,
        provider_row_count=1,
        accepted_row_count=1,
    )
    with pytest.raises(
        ForwardAcquisitionIntegrityError,
        match="conflicting terminal replay",
    ):
        store.append_terminal(conflicting)


def test_record_from_pre_drift_config_cannot_bind_new_plan():
    old = _spec("p1", "m1", config=B)
    old_plan = _plan(old)
    record = _success_nonempty(old_plan, old)
    new = _spec("p1", "m1", config=F)
    new_plan = _plan(new)
    with pytest.raises(ForwardAcquisitionIntegrityError, match="different frozen plan"):
        record.validate_against(new_plan)


def test_idempotency_key_binds_plan_and_frozen_request_config_retry_identity():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    changed = _spec("p1", "m1", config=F)
    changed_plan = _plan(changed)
    assert terminal_idempotency_key(plan, spec) != terminal_idempotency_key(
        changed_plan,
        changed,
    )


def test_future_terminal_evidence_fails_current_completeness(tmp_path):
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    record = build_terminal_record(
        plan,
        spec,
        state=AcquisitionTerminalState.SUCCEEDED_EMPTY,
        attempt_count=1,
        started_at="2026-09-21T18:10:00Z",
        terminal_at="2026-09-21T18:11:00Z",
        response_evidence_sha256=D,
    )
    store.append_terminal(record)
    with pytest.raises(
        ForwardAcquisitionIntegrityError,
        match="future terminal evidence",
    ):
        store.completeness(as_of="2026-09-21T18:05:00Z")


def test_acquisition_cannot_start_before_prospective_freeze():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    with pytest.raises(
        ForwardAcquisitionIntegrityError,
        match="precede prospective plan freeze",
    ):
        build_terminal_record(
            plan,
            spec,
            state=AcquisitionTerminalState.SUCCEEDED_EMPTY,
            attempt_count=1,
            started_at="2026-09-21T17:59:59Z",
            terminal_at="2026-09-21T18:01:00Z",
            response_evidence_sha256=D,
        )


def test_corrupt_terminal_digest_fails_closed_on_restart(tmp_path):
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    record = _success_nonempty(plan, spec)
    store.append_terminal(record)
    path = store.terminal_dir / f"{spec.spec_sha256}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["accepted_row_count"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ForwardAcquisitionIntegrityError):
        store.load_terminals()


def test_unexpected_terminal_file_fails_closed(tmp_path):
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    store.terminal_dir.mkdir(parents=True)
    (store.terminal_dir / "decoy.json").write_text("{}", encoding="utf-8")
    with pytest.raises(
        ForwardAcquisitionIntegrityError,
        match="unexpected durable terminal evidence",
    ):
        store.load_terminals()


def test_partial_crash_bytes_are_observable_corruption_not_recreated(tmp_path):
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    store = ForwardAcquisitionStore(tmp_path)
    store.freeze_plan(plan)
    store.terminal_dir.mkdir(parents=True)
    path = store.terminal_dir / f"{spec.spec_sha256}.json"
    path.write_text('{"schema_version":', encoding="utf-8")
    with pytest.raises(
        ForwardAcquisitionIntegrityError,
        match="cannot read durable",
    ):
        store.load_terminals()


def test_noncanonical_time_and_bool_integer_are_rejected():
    with pytest.raises(ForwardAcquisitionError, match="canonical UTC"):
        ForwardAcquisitionPlan(
            evaluation_id="e",
            frozen_at="2026-09-21T20:00:00+02:00",
            opportunities=(_spec("p1", "m1"),),
        )
    with pytest.raises(ForwardAcquisitionError, match="positive integer"):
        _spec("p1", "m1", max_attempts=True)  # type: ignore[arg-type]


def test_durable_round_trip_rejects_unknown_state_and_digest_rewrite():
    spec = _spec("p1", "m1")
    plan = _plan(spec)
    record = _success_nonempty(plan, spec)
    payload = record.durable_payload()
    assert TerminalAcquisitionRecord.from_payload(payload) == record

    unknown = dict(payload)
    unknown["state"] = "NO_BET"
    with pytest.raises(ForwardAcquisitionIntegrityError, match="unknown terminal"):
        TerminalAcquisitionRecord.from_payload(unknown)

    rewritten = dict(payload)
    rewritten["attempt_count"] = 2
    with pytest.raises(ForwardAcquisitionIntegrityError, match="digest mismatch"):
        TerminalAcquisitionRecord.from_payload(rewritten)
