from __future__ import annotations

from decimal import Decimal

import pytest

from autosport import _paper_execution_decision_origin as origin_module
from autosport import _paper_execution_decision_origin_instance_guard as instance_guard
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


DECISION_ID = "decision-origin-canonical-reserve-1"
RUN_ID = "paper-run-origin-canonical-reserve-1"
STARTED_AT = "2026-09-21T00:21:00+00:00"


def _plan() -> ExecutionPlan:
    action = ExecutionAction(
        action_id="action-origin-canonical-reserve-1",
        bookmaker_id="paper-provider",
        account_id="paper-account",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.10"),
        requested_stake=Decimal("5.00"),
        quote_id="quote-1",
        quote_observed_at="2026-09-21T00:20:59+00:00",
        expires_at="2026-09-21T00:22:00+00:00",
    )
    return ExecutionPlan(
        plan_id="plan-origin-canonical-reserve-1",
        bookmaker_profile_version="paper-origin-test-v1",
        decision_id=DECISION_ID,
        approval_id="paper-only",
        created_at=STARTED_AT,
        actions=(action,),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="origin-canonical-reserve-model",
        model_version="v1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="focused-origin-canonical-reserve-test",
        seed="origin-canonical-reserve-seed",
        max_quote_age_ms=60_000,
    )


def _reserve(target, *, observation_evidence_ids):
    return instance_guard._STABLE_RESERVE_RUN(
        target,
        run_id=RUN_ID,
        trigger_id=DECISION_ID,
        plan=_plan(),
        config=_config(),
        started_at=STARTED_AT,
        observation_evidence_ids=observation_evidence_ids,
    )


def test_origin_binding_only_extends_pristine_canonical_reserve_payload(tmp_path) -> None:
    plain = PaperExecutionLedger(tmp_path / "plain.jsonl")
    bound = PaperExecutionLedger(tmp_path / "bound.jsonl")
    origin = origin_module.DecisionRecordOrigin(
        decision_id=DECISION_ID,
        record_sha256="a" * 64,
    )

    _reserve(plain, observation_evidence_ids={"b": "2", "a": "1"})
    _reserve(
        instance_guard._CanonicalReservationView(bound, origin),
        observation_evidence_ids={"b": "2", "a": "1"},
    )

    plain_events = plain.events(RUN_ID)
    bound_events = bound.events(RUN_ID)
    assert len(plain_events) == 1
    assert len(bound_events) == 1
    assert plain_events[0]["event_type"] == "RUN_RESERVED"
    assert bound_events[0]["event_type"] == "RUN_RESERVED"

    plain_payload = plain_events[0]["payload"]
    bound_payload = dict(bound_events[0]["payload"])
    assert bound_payload.pop("decision_origin") == origin.to_dict()
    assert bound_payload == plain_payload
    assert plain_payload["observation_evidence_ids"] == {"a": "1", "b": "2"}


def test_origin_bound_reserve_keeps_pristine_preappend_rejection(tmp_path) -> None:
    plain = PaperExecutionLedger(tmp_path / "plain-invalid.jsonl")
    bound = PaperExecutionLedger(tmp_path / "bound-invalid.jsonl")
    origin = origin_module.DecisionRecordOrigin(
        decision_id=DECISION_ID,
        record_sha256="b" * 64,
    )

    with pytest.raises(AttributeError):
        _reserve(plain, observation_evidence_ids=None)
    with pytest.raises(AttributeError):
        _reserve(
            instance_guard._CanonicalReservationView(bound, origin),
            observation_evidence_ids=None,
        )

    assert plain.events(RUN_ID) == ()
    assert bound.events(RUN_ID) == ()


def test_mutable_append_alias_cannot_replace_sealed_reservation_sink(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = PaperExecutionLedger(tmp_path / "sealed-append.jsonl")
    origin = origin_module.DecisionRecordOrigin(
        decision_id=DECISION_ID,
        record_sha256="c" * 64,
    )
    attacker_called = False

    def forged_append(*args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutable append alias must not be authority")

    monkeypatch.setattr(instance_guard, "_STABLE_APPEND_EVENT", forged_append)
    _reserve(
        instance_guard._CanonicalReservationView(ledger, origin),
        observation_evidence_ids={},
    )

    assert attacker_called is False
    events = ledger.events(RUN_ID)
    assert len(events) == 1
    assert events[0]["payload"]["decision_origin"] == origin.to_dict()


def test_mutable_reserve_alias_cannot_replace_installed_originless_reserve(
    tmp_path,
    monkeypatch,
) -> None:
    ledger = PaperExecutionLedger(tmp_path / "sealed-reserve.jsonl")
    attacker_called = False

    def forged_reserve(*args, **kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutable reserve alias must not be authority")

    monkeypatch.setattr(instance_guard, "_STABLE_RESERVE_RUN", forged_reserve)
    ledger.reserve_run(
        run_id=RUN_ID,
        trigger_id=DECISION_ID,
        plan=_plan(),
        config=_config(),
        started_at=STARTED_AT,
        observation_evidence_ids={},
    )

    assert attacker_called is False
    events = ledger.events(RUN_ID)
    assert len(events) == 1
    assert events[0]["event_type"] == "RUN_RESERVED"
