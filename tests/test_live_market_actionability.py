from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from autosport.domain import MarketEvent
from autosport.live_market_actionability import (
    LiveInputCurrentViewOutcome,
    LiveInputWaitReason,
    LiveMarketActionabilityError,
    RegisteredLiveInputCurrentView,
    evaluate_registered_input_current_view,
)
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 23, 11, 0, 0, tzinfo=UTC)


def _event(
    *,
    selection_id: str = "selection-a",
    sequence: int = 1,
    status: str = "open",
    observed_at: datetime | None = None,
    ingest_at: datetime | None = None,
    source_at: datetime | None = None,
) -> MarketEvent:
    observed = observed_at or (AS_OF - timedelta(seconds=10))
    ingested = ingest_at or (observed + timedelta(milliseconds=10))
    return MarketEvent(
        event_id="event-1",
        market_id="market-1",
        selection_id=selection_id,
        decimal_odds=Decimal("2.10"),
        observed_ts=observed.isoformat(),
        source_id="provider-a",
        sequence=sequence,
        status=status,
        source_ts=None if source_at is None else source_at.isoformat(),
        ingest_ts=ingested.isoformat(),
        sport="soccer",
    )


def _runtime(
    *events: MarketEvent,
    selection_ids: tuple[str, ...] | None = None,
) -> tuple[BoundedMirrorInvalidationBuffer, FocusedMirrorDependencyIndex]:
    mirror = MarketMirror()
    updates = BoundedMirrorInvalidationBuffer(mirror)
    for event in events:
        updates.accept_persisted(event)
    dependencies = FocusedMirrorDependencyIndex(mirror)
    dependencies.register(
        "input-1",
        source_ids="provider-a",
        sports="soccer",
        event_ids="event-1",
        market_ids="market-1",
        selection_ids=selection_ids,
    )
    return updates, dependencies


def _evaluate(
    updates: BoundedMirrorInvalidationBuffer,
    dependencies: FocusedMirrorDependencyIndex,
    *,
    as_of: datetime = AS_OF,
    max_age: timedelta = timedelta(seconds=60),
) -> RegisteredLiveInputCurrentView:
    return evaluate_registered_input_current_view(
        updates=updates,
        dependencies=dependencies,
        input_id="input-1",
        as_of=as_of,
        max_age=max_age,
    )


def test_fresh_open_registered_input_gets_only_current_view_eligibility() -> None:
    updates, dependencies = _runtime(_event())
    result = _evaluate(updates, dependencies)

    assert result.outcome is LiveInputCurrentViewOutcome.CURRENT_VIEW_ELIGIBLE
    assert result.current_view_eligible is True
    assert result.wait_reasons == ()
    assert len(result.components) == 1
    assert result.components[0].wait_reasons == ()
    assert result.is_product_issued is True

    # This bounded child is deliberately not a final trade/action authority.
    assert result.continuity_proven is False
    assert result.depth_liquidity_proven is False
    assert result.provider_capability_proven is False
    assert result.risk_authorized is False
    assert result.execution_authorized is False
    assert result.settlement_proven is False
    assert result.real_money_authorized is False


def test_no_matching_component_waits_instead_of_treating_empty_as_current() -> None:
    updates, dependencies = _runtime()
    result = _evaluate(updates, dependencies)

    assert result.outcome is LiveInputCurrentViewOutcome.WAIT
    assert result.current_view_eligible is False
    assert result.wait_reasons == (LiveInputWaitReason.NO_COMPONENTS,)
    assert result.components == ()


@pytest.mark.parametrize("status", ["suspended", "closed", "unknown"])
def test_non_open_market_status_fails_closed(status: str) -> None:
    updates, dependencies = _runtime(_event(status=status))
    result = _evaluate(updates, dependencies)

    assert result.outcome is LiveInputCurrentViewOutcome.WAIT
    assert LiveInputWaitReason.NON_OPEN_STATUS in result.wait_reasons
    assert result.components[0].status == status


def test_stale_source_time_cannot_be_laundered_by_recent_receipt() -> None:
    updates, dependencies = _runtime(
        _event(
            observed_at=AS_OF - timedelta(seconds=2),
            ingest_at=AS_OF - timedelta(seconds=1),
            source_at=AS_OF - timedelta(minutes=10),
        )
    )
    result = _evaluate(updates, dependencies, max_age=timedelta(seconds=30))

    assert result.outcome is LiveInputCurrentViewOutcome.WAIT
    assert result.wait_reasons == (LiveInputWaitReason.STALE,)
    assert result.components[0].age_microseconds == 600_000_000


def test_future_ingest_or_observation_cannot_be_actionable_for_earlier_cut() -> None:
    updates, dependencies = _runtime(
        _event(
            observed_at=AS_OF - timedelta(seconds=2),
            ingest_at=AS_OF + timedelta(microseconds=1),
        )
    )
    result = _evaluate(updates, dependencies)

    assert result.outcome is LiveInputCurrentViewOutcome.WAIT
    assert LiveInputWaitReason.FUTURE_CAUSALITY in result.wait_reasons
    assert result.components[0].age_microseconds is None


def test_invalid_observed_timestamp_fails_closed() -> None:
    event = _event()
    invalid = MarketEvent(
        event_id=event.event_id,
        market_id=event.market_id,
        selection_id=event.selection_id,
        decimal_odds=event.decimal_odds,
        observed_ts="not-a-time",
        source_id=event.source_id,
        sequence=event.sequence,
        status=event.status,
        source_ts=None,
        ingest_ts=event.ingest_ts,
        sport=event.sport,
    )
    updates, dependencies = _runtime(invalid)
    result = _evaluate(updates, dependencies)

    assert result.outcome is LiveInputCurrentViewOutcome.WAIT
    assert LiveInputWaitReason.INVALID_CAUSAL_TIMESTAMP in result.wait_reasons


def test_exact_freshness_boundary_is_inclusive_but_fractional_excess_waits() -> None:
    exact_updates, exact_dependencies = _runtime(
        _event(observed_at=AS_OF - timedelta(seconds=60))
    )
    exact = _evaluate(
        exact_updates,
        exact_dependencies,
        max_age=timedelta(seconds=60),
    )
    assert exact.current_view_eligible is True

    stale_updates, stale_dependencies = _runtime(
        _event(observed_at=AS_OF - timedelta(seconds=60, microseconds=1))
    )
    stale = _evaluate(
        stale_updates,
        stale_dependencies,
        max_age=timedelta(seconds=60),
    )
    assert stale.current_view_eligible is False
    assert stale.wait_reasons == (LiveInputWaitReason.STALE,)


def test_weakest_component_blocks_multi_component_registered_input() -> None:
    updates, dependencies = _runtime(
        _event(selection_id="selection-a", sequence=1),
        _event(
            selection_id="selection-b",
            sequence=1,
            observed_at=AS_OF - timedelta(minutes=5),
        ),
        selection_ids=("selection-a", "selection-b"),
    )
    result = _evaluate(updates, dependencies, max_age=timedelta(seconds=60))

    assert result.outcome is LiveInputCurrentViewOutcome.WAIT
    assert result.wait_reasons == (LiveInputWaitReason.STALE,)
    by_selection = {item.quote_key: item for item in result.components}
    assert len(by_selection) == 2
    assert sum(not item.wait_reasons for item in result.components) == 1


def test_repeated_evaluation_is_deterministic_until_canonical_mirror_changes() -> None:
    first_event = _event()
    updates, dependencies = _runtime(first_event)
    first = _evaluate(updates, dependencies)
    duplicate = _evaluate(updates, dependencies)

    assert duplicate == first
    assert duplicate.evidence_sha256 == first.evidence_sha256

    updates.accept_persisted(
        _event(
            sequence=2,
            observed_at=AS_OF - timedelta(seconds=1),
            ingest_at=AS_OF - timedelta(milliseconds=500),
        )
    )
    successor = _evaluate(updates, dependencies)

    assert successor.mirror_revision > first.mirror_revision
    assert successor.evidence_sha256 != first.evidence_sha256
    assert first.components[0].sequence == 1
    assert successor.components[0].sequence == 2


def test_result_cannot_be_dataclasses_replace_forged_into_new_authority() -> None:
    updates, dependencies = _runtime(_event())
    result = _evaluate(updates, dependencies)

    with pytest.raises(TypeError):
        replace(result, evidence_sha256="0" * 64)

    with pytest.raises(LiveMarketActionabilityError):
        RegisteredLiveInputCurrentView(
            _issuer=None,
            input_id=result.input_id,
            as_of=result.as_of,
            max_age_microseconds=result.max_age_microseconds,
            mirror_revision=result.mirror_revision,
            outcome=result.outcome,
            wait_reasons=result.wait_reasons,
            components=result.components,
            evidence_sha256=result.evidence_sha256,
        )


def test_deepcopied_result_cannot_replay_process_local_positive_authority() -> None:
    updates, dependencies = _runtime(_event())
    result = _evaluate(updates, dependencies)

    copied = deepcopy(result)
    assert copied == result
    assert copied.is_product_issued is False
    with pytest.raises(
        LiveMarketActionabilityError,
        match="not current process-issued evidence",
    ):
        _ = copied.current_view_eligible


def test_low_level_field_mutation_revokes_issued_positive_result() -> None:
    updates, dependencies = _runtime(_event())
    result = _evaluate(updates, dependencies)
    assert result.current_view_eligible is True

    object.__setattr__(result, "outcome", LiveInputCurrentViewOutcome.WAIT)

    assert result.is_product_issued is False
    with pytest.raises(
        LiveMarketActionabilityError,
        match="not current process-issued evidence",
    ):
        _ = result.current_view_eligible


def test_dependency_index_from_different_mirror_is_rejected() -> None:
    updates, _ = _runtime(_event())

    other_mirror = MarketMirror()
    other_dependencies = FocusedMirrorDependencyIndex(other_mirror)
    other_dependencies.register("input-1", source_ids="provider-a")

    with pytest.raises(
        LiveMarketActionabilityError,
        match="same canonical MarketMirror",
    ):
        _evaluate(updates, other_dependencies)


def test_unknown_input_is_not_silently_treated_as_empty_wait() -> None:
    updates, dependencies = _runtime(_event())

    with pytest.raises(
        LiveMarketActionabilityError,
        match="unknown registered live input",
    ):
        evaluate_registered_input_current_view(
            updates=updates,
            dependencies=dependencies,
            input_id="missing-input",
            as_of=AS_OF,
            max_age=timedelta(seconds=60),
        )


@pytest.mark.parametrize(
    "as_of,max_age,exc_type",
    [
        (datetime(2026, 9, 23, 11, 0, 0), timedelta(seconds=1), LiveMarketActionabilityError),
        (AS_OF, timedelta(microseconds=-1), LiveMarketActionabilityError),
        (AS_OF, "60", TypeError),
    ],
)
def test_invalid_boundary_policy_fails_before_issuing_result(
    as_of: object,
    max_age: object,
    exc_type: type[Exception],
) -> None:
    updates, dependencies = _runtime(_event())

    with pytest.raises(exc_type):
        evaluate_registered_input_current_view(
            updates=updates,
            dependencies=dependencies,
            input_id="input-1",
            as_of=as_of,  # type: ignore[arg-type]
            max_age=max_age,  # type: ignore[arg-type]
        )