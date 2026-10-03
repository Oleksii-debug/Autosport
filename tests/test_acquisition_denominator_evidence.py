from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from autosport._evaluation_universe_structural_gate import (
    _authorize_structural_intake_for_tests,
)
from autosport.acquisition_denominator_evidence import (
    AcquisitionCoverageStrength,
    AcquisitionDenominatorEvidence,
    AcquisitionDenominatorEvidenceError,
    build_acquisition_denominator_evidence,
    require_complete_acquisition_coverage,
)
from autosport.causal_collector import CollectorDeltaStore
from autosport.evaluation_intake import (
    ObservationEnumerationWitness,
    ObservationIntakeLedger,
)
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    FunnelStage,
    SlotState,
    build_frozen_universe,
)
from autosport.scheduled_source_universe import ScheduledSourceUniverseError
from autosport.source_universe_commitment import build_source_universe_commitment


SOURCE_ID = "source-x"
RUN_ID = "run-1"
STREAM_EPOCH = "epoch-1"
ANCHOR = "2026-09-22T00:00:00+00:00"
H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


class _Resolver:
    def __init__(self, witness: ObservationEnumerationWitness) -> None:
        self._witness = witness

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        assert enumeration_id == self._witness.enumeration_id
        return self._witness

    def terminal_enumeration_id(self, **identity: str) -> str:
        assert identity["source_id"] == SOURCE_ID
        return self._witness.enumeration_id


def _row() -> EvaluationRow:
    return EvaluationRow(
        row_key="scheduled-denominator-row",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        slot_state=SlotState.NO_EVENT,
        decision_stage=FunnelStage.OBSERVED_SLOT,
        attrition_reason=AttritionReason.NO_EVENT,
        sport="football",
        provider_id="provider-1",
        source_id=SOURCE_ID,
        event_id=None,
        market_id=None,
        selection_id=None,
        source_at="2026-09-22T00:00:00Z",
        received_at="2026-09-22T00:00:01Z",
        committed_at="2026-09-22T00:00:02Z",
        detection_at=None,
        decision_at=None,
        quote_set_sha256=None,
        freshness_policy_sha256=H3,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=H,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1",
        settlement_proof_id="settlement-proof-1",
        execution_model_id=None,
        execution_run_id=None,
        execution_plan_id=None,
        execution_action_id=None,
        decision_quote_id=None,
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-22T01:00:00Z",
        dependence_cluster_keys=("source:source-x",),
    )


def _universe(*, frozen_at: str = "2026-09-22T00:00:20Z"):
    item = _row()
    witness = ObservationEnumerationWitness(
        enumeration_id="enumeration-1",
        session_id="session-1",
        source_id=SOURCE_ID,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        cycle_index=1,
        source_range_id="range-1",
        stream_epoch=STREAM_EPOCH,
        start_cursor="cursor-0",
        end_cursor="cursor-1",
        acquisition_sha256=H3,
        row_keys=(item.row_key,),
        row_evidence_sha256=((item.row_key, item.row_id),),
        exhaustive=True,
        gap_free=True,
        committed_at="2026-09-22T00:00:02.500000Z",
        evaluation_not_before="2026-09-22T00:00:03Z",
        outcome_reveal_not_before=item.outcome_reveal_not_before,
    )
    with TemporaryDirectory() as workspace:
        ledger = ObservationIntakeLedger(
            workspace,
            authority_id="intake-1",
            enumeration_resolver=_Resolver(witness),
        )
        ledger.append_cycle(enumeration_id=witness.enumeration_id)
        _authorize_structural_intake_for_tests(ledger)
        return build_frozen_universe(
            intake_ledger=ledger,
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at=frozen_at,
            rows=(item,),
        )


def _ensure_schedule(store: CollectorDeltaStore, *, end_slot: int = 0) -> None:
    store._ensure_collector_schedule(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        anchor_at=ANCHOR,
        interval_seconds=10,
        max_items=250,
        evaluation_start_slot_ordinal=0,
        evaluation_end_slot_ordinal=end_slot,
    )


def _start_slot(store: CollectorDeltaStore, ordinal: int) -> int:
    slot = store._next_collector_schedule_slot(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
    )
    assert slot["slot_ordinal"] == ordinal
    return store._begin_scheduled_collector_cycle(
        source_id=SOURCE_ID,
        run_id=RUN_ID,
        stream_epoch=STREAM_EPOCH,
        max_items=250,
        slot_ordinal=slot["slot_ordinal"],
        due_at=slot["due_at"],
        attempted_at=slot["due_at"],
    )


def _finish(
    store: CollectorDeltaStore,
    cycle_seq: int,
    *,
    status: str = "SUCCESS",
    completed_at: str,
) -> None:
    store._finish_collector_cycle(
        source_id=SOURCE_ID,
        cycle_seq=cycle_seq,
        status=status,
        completed_at=completed_at,
        catalog_changes=(),
        observed_delta_ids=(),
        committed_delta_ids=(),
        duplicate_delta_ids=(),
        error_code=None if status == "SUCCESS" else status.lower(),
    )


def _build(
    store: CollectorDeltaStore,
    path: Path,
    *,
    start_cycle_seq: int,
    end_cycle_seq: int,
    end_slot: int,
    frozen_at: str = "2026-09-22T00:00:20Z",
):
    source = build_source_universe_commitment(
        store,
        expected_store_path=path,
        source_id=SOURCE_ID,
        start_cycle_seq=start_cycle_seq,
        end_cycle_seq=end_cycle_seq,
    )
    return build_acquisition_denominator_evidence(
        store,
        source,
        _universe(frozen_at=frozen_at),
        expected_store_path=path,
        expected_source_id=SOURCE_ID,
        expected_run_id=RUN_ID,
        expected_start_slot_ordinal=0,
        expected_end_slot_ordinal=end_slot,
    )


def test_complete_empty_before_freeze_is_explicit_observed_zero_coverage() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store)
        cycle = _start_slot(store, 0)
        _finish(
            store,
            cycle,
            completed_at="2026-09-22T00:00:05+00:00",
        )

        evidence = _build(
            store,
            path,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
            end_slot=0,
        )

        assert evidence.expected_slot_count == 1
        assert evidence.success_nonempty_count == 0
        assert evidence.success_empty_count == 1
        assert evidence.provider_unavailable_count == 0
        assert evidence.pending_or_late_terminal_count == 0
        assert evidence.terminal_after_freeze_count == 0
        assert evidence.acquisition_complete_by_universe_freeze is True
        assert (
            evidence.coverage_strength
            is AcquisitionCoverageStrength.SCHEDULED_CYCLE_WINDOW_COMPLETE
        )
        assert evidence.positive_evaluation_lineage_complete is False
        assert evidence.external_provider_universe_complete is False
        assert evidence.promotion_ready is False
        assert require_complete_acquisition_coverage(evidence) is evidence


def test_provider_failure_stays_visible_and_blocks_whole_universe_qualification() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store)
        cycle = _start_slot(store, 0)
        _finish(
            store,
            cycle,
            status="PROVIDER_UNAVAILABLE",
            completed_at="2026-09-22T00:00:05+00:00",
        )

        evidence = _build(
            store,
            path,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
            end_slot=0,
        )

        assert evidence.success_empty_count == 0
        assert evidence.provider_unavailable_count == 1
        assert evidence.pending_or_late_terminal_count == 0
        assert evidence.acquisition_complete_by_universe_freeze is False
        assert (
            evidence.coverage_strength
            is AcquisitionCoverageStrength.INCOMPLETE_OR_UNKNOWN
        )
        assert evidence.positive_evaluation_lineage_complete is False
        with pytest.raises(
            AcquisitionDenominatorEvidenceError,
            match="failed, stopped, pending, late, or post-cutoff",
        ):
            require_complete_acquisition_coverage(evidence)


def test_later_success_cannot_rewrite_earlier_frozen_universe_as_complete() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store)
        cycle = _start_slot(store, 0)
        _finish(
            store,
            cycle,
            completed_at="2026-09-22T00:00:30+00:00",
        )

        current_source = build_source_universe_commitment(
            store,
            expected_store_path=path,
            source_id=SOURCE_ID,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
        )
        assert current_source.success_count == 1
        assert current_source.zero_result_success_count == 1
        assert current_source.provider_observation_complete is True

        evidence = build_acquisition_denominator_evidence(
            store,
            current_source,
            _universe(frozen_at="2026-09-22T00:00:20Z"),
            expected_store_path=path,
            expected_source_id=SOURCE_ID,
            expected_run_id=RUN_ID,
            expected_start_slot_ordinal=0,
            expected_end_slot_ordinal=0,
        )

        assert evidence.success_empty_count == 0
        assert evidence.pending_or_late_terminal_count == 1
        assert evidence.terminal_after_freeze_count == 1
        assert evidence.acquisition_complete_by_universe_freeze is False
        with pytest.raises(AcquisitionDenominatorEvidenceError):
            require_complete_acquisition_coverage(evidence)


def test_mixed_empty_failure_pending_partition_cannot_drop_a_scheduled_slot() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store, end_slot=2)

        success = _start_slot(store, 0)
        _finish(
            store,
            success,
            completed_at="2026-09-22T00:00:05+00:00",
        )
        failed = _start_slot(store, 1)
        _finish(
            store,
            failed,
            status="LOCAL_FAILURE",
            completed_at="2026-09-22T00:00:12+00:00",
        )
        pending = _start_slot(store, 2)

        evidence = _build(
            store,
            path,
            start_cycle_seq=success,
            end_cycle_seq=pending,
            end_slot=2,
            frozen_at="2026-09-22T00:00:25Z",
        )

        assert evidence.expected_slot_count == 3
        partition = (
            evidence.success_nonempty_count
            + evidence.success_empty_count
            + evidence.provider_unavailable_count
            + evidence.local_failure_count
            + evidence.stop_requested_count
            + evidence.pending_or_late_terminal_count
        )
        assert partition == 3
        assert evidence.success_empty_count == 1
        assert evidence.local_failure_count == 1
        assert evidence.pending_or_late_terminal_count == 1
        assert evidence.acquisition_complete_by_universe_freeze is False


def test_evidence_digest_is_deterministic_but_positive_authority_is_not_copyable() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store)
        cycle = _start_slot(store, 0)
        _finish(
            store,
            cycle,
            completed_at="2026-09-22T00:00:05+00:00",
        )

        first = _build(
            store,
            path,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
            end_slot=0,
        )
        second = _build(
            store,
            path,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
            end_slot=0,
        )
        assert first.to_payload() == second.to_payload()
        assert first.evidence_sha256 == second.evidence_sha256

        copied = object.__new__(AcquisitionDenominatorEvidence)
        for name in first.__dataclass_fields__:
            object.__setattr__(copied, name, getattr(first, name))

        with pytest.raises(
            AcquisitionDenominatorEvidenceError,
            match="not current product-issued authority",
        ):
            require_complete_acquisition_coverage(copied)




def test_favorable_short_window_cannot_launder_missing_frozen_schedule_tail() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store, end_slot=1)

        cycle = _start_slot(store, 0)
        _finish(
            store,
            cycle,
            completed_at="2026-09-22T00:00:05+00:00",
        )
        source = build_source_universe_commitment(
            store,
            expected_store_path=path,
            source_id=SOURCE_ID,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
        )

        with pytest.raises(
            ScheduledSourceUniverseError,
            match="prospectively frozen evaluation window",
        ):
            build_acquisition_denominator_evidence(
                store,
                source,
                _universe(),
                expected_store_path=path,
                expected_source_id=SOURCE_ID,
                expected_run_id=RUN_ID,
                expected_start_slot_ordinal=0,
                expected_end_slot_ordinal=0,
            )


def test_missing_due_slot_cannot_disappear_from_acquisition_denominator() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "collector.db"
        store = CollectorDeltaStore(path)
        _ensure_schedule(store, end_slot=1)

        cycle = _start_slot(store, 0)
        _finish(
            store,
            cycle,
            completed_at="2026-09-22T00:00:05+00:00",
        )
        source = build_source_universe_commitment(
            store,
            expected_store_path=path,
            source_id=SOURCE_ID,
            start_cycle_seq=cycle,
            end_cycle_seq=cycle,
        )

        with pytest.raises(ScheduledSourceUniverseError):
            build_acquisition_denominator_evidence(
                store,
                source,
                _universe(),
                expected_store_path=path,
                expected_source_id=SOURCE_ID,
                expected_run_id=RUN_ID,
                expected_start_slot_ordinal=0,
                expected_end_slot_ordinal=1,
            )

def test_direct_construction_cannot_mint_positive_coverage() -> None:
    with pytest.raises(TypeError, match="product-issued"):
        AcquisitionDenominatorEvidence(
            schema_version=1,
            acquisition_complete_by_universe_freeze=True,
        )
