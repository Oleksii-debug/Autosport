from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from autosport import _paper_execution_reality_legacy as _legacy_reality
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionEvidenceRecord,
    PaperExecutionEvidenceRegistry,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
    PaperExecutionStateError,
    RecoveryDecision,
    execute_paper_plan,
)
from autosport.real_execution_ledger import ExecutionAction, ExecutionPlan


QUOTE_AT = "2026-09-20T03:00:00+00:00"
STARTED_AT = "2026-09-20T03:00:00.100000+00:00"
EXPIRES_AT = "2026-09-20T03:01:00+00:00"


class _HostileSide(str):
    comparisons = 0

    def __hash__(self) -> int:
        type(self).comparisons += 1
        return super().__hash__()

    def __eq__(self, other: object) -> bool:
        type(self).comparisons += 1
        return super().__eq__(other)


def _action(action_id: str, *, side: str, odds: str = "5.00", stake: str = "10.00") -> ExecutionAction:
    constructor_side = side if side.strip() == side else "BACK"
    action = ExecutionAction(
        action_id=action_id,
        bookmaker_id="paper-exchange",
        account_id="paper-account",
        event_id="event-1",
        market_id=f"market-{action_id}",
        selection_id=f"selection-{action_id}",
        side=constructor_side,
        requested_odds=odds,
        requested_stake=stake,
        quote_id=f"quote-{action_id}",
        quote_observed_at=QUOTE_AT,
        expires_at=EXPIRES_AT,
    )
    if constructor_side != side:
        object.__setattr__(action, "side", side)
    return action


def _plan(*actions: ExecutionAction) -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="lay-side-preflight-plan",
        bookmaker_profile_version="paper-profile-v1",
        decision_id="decision-1",
        approval_id="paper-only",
        created_at=QUOTE_AT,
        actions=tuple(actions),
    )


def _config() -> PaperExecutionModelConfig:
    return PaperExecutionModelConfig(
        model_id="paper-reality",
        model_version="lay-side-preflight-1",
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="test-seeded-model",
        seed="fixed-seed",
        max_quote_age_ms=5_000,
        min_delay_ms=100,
        max_delay_ms=100,
        rejected_bps=0,
        partial_bps=0,
        unknown_bps=0,
        partial_fill_bps=5_000,
        max_slippage_bps=0,
    )


def _empirical_acceptance(
    ledger: PaperExecutionLedger,
    action: ExecutionAction,
):
    record = PaperExecutionEvidenceRecord(
        action_id=action.action_id,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        event_id=action.event_id,
        market_id=action.market_id,
        selection_id=action.selection_id,
        side=action.side,
        quote_id=action.quote_id,
        outcome=PaperAttemptOutcome.ACCEPTED,
        observed_at="2026-09-20T03:00:00.250000+00:00",
        evidence_grade=EvidenceGrade.EMPIRICAL,
        evidence_source="captured-paper-observation-v1",
        accepted_odds="5.00",
        accepted_stake="10.00",
        reason="observed accepted",
    )
    registry = PaperExecutionEvidenceRegistry(ledger)
    registry.register(record)
    return record.as_observation(), registry


def test_direct_evidence_registration_revalidates_post_init_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current_action = _action("direct-evidence-mutation", side="BACK")
        record = PaperExecutionEvidenceRecord(
            action_id=current_action.action_id,
            bookmaker_id=current_action.bookmaker_id,
            account_id=current_action.account_id,
            event_id=current_action.event_id,
            market_id=current_action.market_id,
            selection_id=current_action.selection_id,
            side=current_action.side,
            quote_id=current_action.quote_id,
            outcome=PaperAttemptOutcome.ACCEPTED,
            observed_at="2026-09-20T03:00:00.250000+00:00",
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            accepted_odds="5.00",
            accepted_stake="10.00",
            reason="observed accepted",
        )
        object.__setattr__(record, "evidence_id", "")

        with pytest.raises(
            PaperExecutionIntegrityError,
            match="evidence record no longer satisfies canonical value invariants",
        ):
            ledger.register_observation_evidence(record)

        assert ledger.events() == []


def test_direct_evidence_registration_rejects_ledger_subclass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _LedgerSubclass(Path(tmp) / "paper-execution.jsonl")
        current_action = _action("direct-evidence-ledger-subclass", side="BACK")
        record = PaperExecutionEvidenceRecord(
            action_id=current_action.action_id,
            bookmaker_id=current_action.bookmaker_id,
            account_id=current_action.account_id,
            event_id=current_action.event_id,
            market_id=current_action.market_id,
            selection_id=current_action.selection_id,
            side=current_action.side,
            quote_id=current_action.quote_id,
            outcome=PaperAttemptOutcome.REJECTED,
            observed_at="2026-09-20T03:00:00.250000+00:00",
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            reason="observed rejected",
        )

        with pytest.raises(TypeError, match="exact PaperExecutionLedger"):
            ledger.register_observation_evidence(record)

        assert ledger.events() == []


def test_direct_reserve_rejects_noncanonical_run_id_before_durable_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current_plan = _plan(_action("direct-run-id", side="BACK"))
        current_config = _config()

        with pytest.raises(
            PaperExecutionStateError,
            match="run_id does not match canonical",
        ):
            ledger.reserve_run(
                run_id="caller-selected-run-id",
                trigger_id="direct-run-id",
                plan=current_plan,
                config=current_config,
                started_at=STARTED_AT,
                observation_evidence_ids={},
            )

        assert ledger.events() == []


def test_direct_reserve_rejects_mapping_subclass_before_durable_write() -> None:
    class HostileEvidenceMap(dict):
        pass

    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current_plan = _plan(_action("direct-evidence-map", side="BACK"))
        current_config = _config()
        trigger_id = "direct-evidence-map"
        run_id = _legacy_reality._run_id(
            current_plan,
            trigger_id,
            current_config,
        )

        with pytest.raises(
            TypeError,
            match="exact dict",
        ):
            ledger.reserve_run(
                run_id=run_id,
                trigger_id=trigger_id,
                plan=current_plan,
                config=current_config,
                started_at=STARTED_AT,
                observation_evidence_ids=HostileEvidenceMap(),
            )

        assert ledger.events() == []


def test_direct_record_attempt_requires_durable_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = PaperExecutionLedger(Path(tmp) / "source.jsonl")
        current_plan = _plan(_action("direct-attempt", side="BACK"))
        current_config = _config()
        run = execute_paper_plan(
            plan=current_plan,
            trigger_id="direct-attempt",
            config=current_config,
            ledger=source,
            started_at=STARTED_AT,
        )
        attempt = run.attempts[0]
        target = PaperExecutionLedger(Path(tmp) / "target.jsonl")

        with pytest.raises(
            PaperExecutionStateError,
            match="requires exactly one durable reservation",
        ):
            target.record_attempt(attempt)

        assert target.events() == []


def test_direct_record_attempt_must_match_reserved_action_prefix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = PaperExecutionLedger(Path(tmp) / "source.jsonl")
        current_plan = _plan(_action("reserved-action", side="BACK"))
        current_config = _config()
        trigger_id = "reserved-action"
        run = execute_paper_plan(
            plan=current_plan,
            trigger_id=trigger_id,
            config=current_config,
            ledger=source,
            started_at=STARTED_AT,
        )
        attempt = run.attempts[0]
        object.__setattr__(attempt, "action_id", "different-action")

        target = PaperExecutionLedger(Path(tmp) / "target.jsonl")
        run_id = _legacy_reality._run_id(current_plan, trigger_id, current_config)
        target.reserve_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=current_plan,
            config=current_config,
            started_at=STARTED_AT,
            observation_evidence_ids={},
        )
        events_before = target.events()

        with pytest.raises(
            PaperExecutionStateError,
            match="action_id does not match durable reserved action",
        ):
            target.record_attempt(attempt)

        assert target.events() == events_before


def test_direct_complete_rejects_list_pending_ids_before_ledger_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")

        with pytest.raises(TypeError, match="pending_action_ids.*tuple"):
            ledger.complete_run(
                run_id="direct-complete-list",
                pending_action_ids=[],
                recovery_decision=RecoveryDecision.NO_EXPOSURE,
                worst_case_exposure=Decimal("0"),
            )

        assert ledger.events() == []


def test_direct_complete_rejects_string_exposure_before_ledger_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")

        with pytest.raises(TypeError, match="worst_case_exposure.*exact Decimal"):
            ledger.complete_run(
                run_id="direct-complete-string-exposure",
                pending_action_ids=(),
                recovery_decision=RecoveryDecision.NO_EXPOSURE,
                worst_case_exposure="0",
            )

        assert ledger.events() == []


def test_direct_complete_rejects_decimal_subclass_before_ledger_read() -> None:
    class HostileDecimal(Decimal):
        pass

    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")

        with pytest.raises(TypeError, match="worst_case_exposure.*exact Decimal"):
            ledger.complete_run(
                run_id="direct-complete-decimal-subclass",
                pending_action_ids=(),
                recovery_decision=RecoveryDecision.NO_EXPOSURE,
                worst_case_exposure=HostileDecimal("0"),
            )

        assert ledger.events() == []


def test_direct_attempt_plan_identity_must_match_durable_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = PaperExecutionLedger(Path(tmp) / "source-plan-id.jsonl")
        current_action = _action("direct-plan-id", side="BACK")
        current_plan = _plan(current_action)
        current_config = _config()
        trigger_id = "direct-plan-id"
        run = execute_paper_plan(
            plan=current_plan,
            trigger_id=trigger_id,
            config=current_config,
            ledger=source,
            started_at=STARTED_AT,
        )
        attempt = run.attempts[0]
        object.__setattr__(attempt, "plan_id", "other-valid-plan-id")

        target = PaperExecutionLedger(Path(tmp) / "target-plan-id.jsonl")
        run_id = _legacy_reality._run_id(current_plan, trigger_id, current_config)
        target.reserve_run(
            run_id=run_id,
            trigger_id=trigger_id,
            plan=current_plan,
            config=current_config,
            started_at=STARTED_AT,
            observation_evidence_ids={},
        )
        events_before = target.events()

        with pytest.raises(
            PaperExecutionStateError,
            match="plan/model identity is not authorized",
        ):
            target.record_attempt(attempt)

        assert target.events() == events_before


def test_evidence_registry_cannot_be_retargeted_after_construction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        redirected = PaperExecutionLedger(Path(tmp) / "redirected-evidence.jsonl")
        registry = PaperExecutionEvidenceRegistry(ledger)
        registry._ledger = redirected

        with pytest.raises(
            PaperExecutionStateError,
            match="ledger authority changed after construction",
        ):
            registry.resolve("missing-evidence")

        assert ledger.events() == []
        assert redirected.events() == []


def test_execution_rejects_evidence_registry_bound_to_different_ledger() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        evidence_ledger = PaperExecutionLedger(Path(tmp) / "evidence-execution.jsonl")
        current_action = _action("cross-ledger-evidence", side="BACK")
        record = PaperExecutionEvidenceRecord(
            action_id=current_action.action_id,
            bookmaker_id=current_action.bookmaker_id,
            account_id=current_action.account_id,
            event_id=current_action.event_id,
            market_id=current_action.market_id,
            selection_id=current_action.selection_id,
            side=current_action.side,
            quote_id=current_action.quote_id,
            outcome=PaperAttemptOutcome.ACCEPTED,
            observed_at="2026-09-20T03:00:00.250000+00:00",
            evidence_grade=EvidenceGrade.EMPIRICAL,
            evidence_source="captured-paper-observation-v1",
            accepted_odds="5.00",
            accepted_stake="10.00",
            reason="observed accepted",
        )
        registry = PaperExecutionEvidenceRegistry(evidence_ledger)
        registry.register(record)
        run_events_before = list(run_ledger.events())

        with pytest.raises(
            PaperExecutionStateError,
            match="bound to the exact run ledger",
        ):
            execute_paper_plan(
                plan=_plan(current_action),
                trigger_id="cross-ledger-evidence",
                config=_config(),
                ledger=run_ledger,
                started_at=STARTED_AT,
                observations={current_action.action_id: record.as_observation()},
                evidence_registry=registry,
            )

        assert run_ledger.events() == run_events_before


def test_mutated_config_identity_fails_before_fingerprint_or_reservation() -> None:
    class HostileModelId(str):
        calls = 0

        def __hash__(self) -> int:
            type(self).calls += 1
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            type(self).calls += 1
            return super().__eq__(other)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("config-hostile", side="LAY")
        config = _config()
        HostileModelId.calls = 0
        object.__setattr__(
            config,
            "model_id",
            HostileModelId(config.model_id),
        )

        with pytest.raises(
            PaperExecutionStateError,
            match="model_id must retain exact canonical text authority",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="config-hostile",
                config=config,
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert HostileModelId.calls == 0
        assert ledger.events() == []


def test_mutated_action_exact_string_value_fails_semantic_revalidation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("action-empty-id", side="LAY")
        object.__setattr__(lay, "bookmaker_id", "")

        with pytest.raises(
            PaperExecutionStateError,
            match="action no longer satisfies canonical value invariants",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="action-empty-id",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert ledger.events() == []


def test_mutated_plan_exact_timestamp_value_fails_semantic_revalidation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("plan-bad-time", side="LAY")
        plan = _plan(lay)
        object.__setattr__(plan, "created_at", "not-a-timestamp")

        with pytest.raises(
            PaperExecutionStateError,
            match="plan no longer satisfies canonical value invariants",
        ):
            execute_paper_plan(
                plan=plan,
                trigger_id="plan-bad-time",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert ledger.events() == []


def test_mutated_config_exact_string_value_fails_semantic_revalidation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("config-empty-seed", side="LAY")
        config = _config()
        object.__setattr__(config, "seed", "")

        with pytest.raises(
            PaperExecutionStateError,
            match="config no longer satisfies canonical value invariants",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="config-empty-seed",
                config=config,
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert ledger.events() == []


def test_mutated_config_range_fails_before_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("config-range", side="LAY")
        config = _config()
        object.__setattr__(config, "max_quote_age_ms", 0)

        with pytest.raises(
            PaperExecutionStateError,
            match="max_quote_age_ms must remain positive",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="config-range",
                config=config,
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert ledger.events() == []


class _LedgerSubclass(PaperExecutionLedger):
    pass


def test_ledger_subclass_cannot_replace_durable_authority() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _LedgerSubclass(Path(tmp) / "paper-execution.jsonl")
        lay = _action("ledger-subclass", side="LAY")

        with pytest.raises(TypeError, match="exact PaperExecutionLedger"):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="ledger-subclass",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert ledger.events() == []


def test_mutated_plan_identity_fails_before_fingerprint_or_reservation() -> None:
    class HostilePlanId(str):
        calls = 0

        def __hash__(self) -> int:
            type(self).calls += 1
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            type(self).calls += 1
            return super().__eq__(other)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("plan-hostile", side="LAY")
        plan = _plan(lay)
        HostilePlanId.calls = 0
        object.__setattr__(
            plan,
            "plan_id",
            HostilePlanId(plan.plan_id),
        )

        with pytest.raises(
            PaperExecutionStateError,
            match="plan_id must retain exact canonical text authority",
        ):
            execute_paper_plan(
                plan=plan,
                trigger_id="plan-hostile",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert HostilePlanId.calls == 0
        assert ledger.events() == []


def test_mutated_side_subclass_fails_before_dispatch_hooks_or_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current = _action("hostile-side", side="BACK", odds="2.00", stake="5.00")
        plan = _plan(current)
        _HostileSide.comparisons = 0
        object.__setattr__(current, "side", _HostileSide("LAY"))
        before = len(ledger.events())

        with pytest.raises(
            PaperExecutionStateError,
            match="canonical ExecutionAction side authority",
        ):
            execute_paper_plan(
                plan=plan,
                trigger_id="hostile-side",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        assert _HostileSide.comparisons == 0
        assert len(ledger.events()) == before


@pytest.mark.parametrize("side", ("lay", " LAY "))
def test_noncanonical_lay_in_multi_leg_plan_fails_before_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay", side=side)
        back = _action("back", side="BACK", odds="2.00", stake="5.00")
        observation, registry = _empirical_acceptance(ledger, lay)
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="canonical BACK or LAY"):
            execute_paper_plan(
                plan=_plan(lay, back),
                trigger_id=f"mixed-{side!r}",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={lay.action_id: observation},
                evidence_registry=registry,
            )

        assert len(ledger.events()) == before


@pytest.mark.parametrize("side", ("lay", " LAY "))
def test_noncanonical_single_leg_lay_fails_before_run_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay", side=side)
        observation, registry = _empirical_acceptance(ledger, lay)
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="canonical BACK or LAY"):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id=f"single-{side!r}",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                observations={lay.action_id: observation},
                evidence_registry=registry,
            )

        assert len(ledger.events()) == before




def test_unsuspended_lay_without_empirical_observation_fails_before_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay-no-evidence", side="LAY")
        before = len(ledger.events())

        with pytest.raises(
            PaperExecutionStateError,
            match="requires explicit empirical execution evidence",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="lay-no-evidence",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        assert len(ledger.events()) == before


def test_suspended_lay_rejects_mutated_action_identity_before_reservation() -> None:
    class HostileIdentity(str):
        calls = 0

        def __eq__(self, other: object) -> bool:
            type(self).calls += 1
            return super().__eq__(other)

    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay-hostile-identity", side="LAY")
        HostileIdentity.calls = 0
        object.__setattr__(
            lay,
            "bookmaker_id",
            HostileIdentity(lay.bookmaker_id),
        )

        with pytest.raises(
            PaperExecutionStateError,
            match="bookmaker_id must retain exact canonical text authority",
        ):
            execute_paper_plan(
                plan=_plan(lay),
                trigger_id="lay-hostile-identity",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids=frozenset({lay.action_id}),
            )

        assert HostileIdentity.calls == 0
        assert ledger.events() == []


def test_suspended_lay_is_durable_no_exposure_without_empirical_fill_evidence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        lay = _action("lay-suspended", side="LAY")

        first = execute_paper_plan(
            plan=_plan(lay),
            trigger_id="lay-suspended",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            suspended_action_ids=frozenset({lay.action_id}),
        )
        second = execute_paper_plan(
            plan=_plan(lay),
            trigger_id="lay-suspended",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            suspended_action_ids=frozenset({lay.action_id}),
        )

        assert first == second
        assert first.completed is True
        assert len(first.attempts) == 1
        assert first.attempts[0].side == "LAY"
        assert first.attempts[0].outcome is PaperAttemptOutcome.REJECTED
        assert first.attempts[0].suspended is True
        assert first.attempts[0].execution_odds is None
        assert first.attempts[0].execution_stake is None
        assert first.worst_case_exposure == 0
        assert first.recovery_decision.value == "NO_EXPOSURE"
        assert first.pending_action_ids == ()
        assert [event["event_type"] for event in ledger.events()] == [
            "RUN_RESERVED",
            "ATTEMPT_RECORDED",
            "RUN_COMPLETED",
        ]




def test_noncanonical_suspension_container_fails_before_reservation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        back = _action("back-bad-suspension-container", side="BACK", odds="2.00", stake="5.00")
        before = len(ledger.events())

        with pytest.raises(TypeError, match="frozenset"):
            execute_paper_plan(
                plan=_plan(back),
                trigger_id="bad-suspension-container",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
                suspended_action_ids={back.action_id},  # type: ignore[arg-type]
            )

        assert len(ledger.events()) == before


def test_suspension_state_is_bound_to_durable_run_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        back = _action(
            "back-suspension-identity",
            side="BACK",
            odds="2.00",
            stake="5.00",
        )

        first = execute_paper_plan(
            plan=_plan(back),
            trigger_id="back-suspension-identity",
            config=_config(),
            ledger=ledger,
            started_at=STARTED_AT,
            suspended_action_ids=frozenset({back.action_id}),
        )
        assert first.attempts[0].suspended is True
        assert first.attempts[0].outcome is PaperAttemptOutcome.REJECTED

        with pytest.raises(
            PaperExecutionStateError,
            match="execution-control state conflicts with durable reservation",
        ):
            execute_paper_plan(
                plan=_plan(back),
                trigger_id="back-suspension-identity",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        events = ledger.events()
        assert [event["event_type"] for event in events] == [
            "RUN_RESERVED",
            "ATTEMPT_RECORDED",
            "RUN_COMPLETED",
        ]
        reserve = events[0]
        assert reserve["payload"]["suspended_action_ids"] == [back.action_id]


@pytest.mark.parametrize("side", ("SIDEWAYS", "back", " BACK "))
def test_unsupported_or_noncanonical_nonlay_side_fails_before_reservation(side: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = PaperExecutionLedger(Path(tmp) / "paper-execution.jsonl")
        current = _action("bad-side", side=side, odds="2.00", stake="5.00")
        before = len(ledger.events())

        with pytest.raises(PaperExecutionStateError, match="canonical BACK or LAY"):
            execute_paper_plan(
                plan=_plan(current),
                trigger_id=f"bad-{side!r}",
                config=_config(),
                ledger=ledger,
                started_at=STARTED_AT,
            )

        assert len(ledger.events()) == before
