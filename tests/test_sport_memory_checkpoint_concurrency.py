from __future__ import annotations

import threading

from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import ParticipantIdentityRegistry
from autosport.sport_memory_checkpoint import (
    initialize_or_open_bound_sport_memory_runtime,
)

from test_sport_memory_matchup_decision import (
    T2,
    T2_CONSUMED,
    _bound_runtime_pair,
    _scope,
)


def _second_bound_runtime(tmp_path):
    identity = ParticipantIdentityRegistry(tmp_path / "participant-identity.json")
    opponent = OpponentIntelligenceStore(
        tmp_path / "opponent-intelligence.json",
        identity,
    )
    return initialize_or_open_bound_sport_memory_runtime(
        tmp_path / "bound-sport-memory.json",
        tmp_path / "bound-sport-memory-authority.json",
        identity,
        opponent,
    )


def test_two_stale_bound_runtimes_preserve_both_acknowledged_matchup_writes(
    tmp_path,
):
    first = _bound_runtime_pair(tmp_path)
    second = _second_bound_runtime(tmp_path)

    # Freeze both callers from the same durable runtime image. Before the
    # checkpoint transaction repair, each call could validate from this stale
    # in-memory prefix and the second writer would either erase the first update
    # or fail the late whole-file CAS instead of making useful forward progress.
    first_matchup = first.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T2,
    )
    second_matchup = second.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T2,
    )
    assert first_matchup == second_matchup

    barrier = threading.Barrier(3)
    errors: list[BaseException] = []
    results: dict[str, tuple] = {}

    def consume(runtime, decision_id: str, matchup) -> None:
        try:
            barrier.wait(timeout=5)
            results[decision_id] = runtime.record_matchup_consumption(
                decision_id=decision_id,
                matchup=matchup,
                decision_cutoff=T2,
                consumed_at=T2_CONSUMED,
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    left = threading.Thread(
        target=consume,
        args=(first, "decision-concurrent-a", first_matchup),
    )
    right = threading.Thread(
        target=consume,
        args=(second, "decision-concurrent-b", second_matchup),
    )
    left.start()
    right.start()
    barrier.wait(timeout=5)
    left.join(timeout=10)
    right.join(timeout=10)

    assert not left.is_alive()
    assert not right.is_alive()
    assert not errors
    assert set(results) == {"decision-concurrent-a", "decision-concurrent-b"}
    assert all(len(records) == 2 for records in results.values())

    reopened = _second_bound_runtime(tmp_path)
    for decision_id in results:
        durable = reopened.consumptions_for_decision(decision_id)
        assert len(durable) == 2
        assert durable == results[decision_id]


def test_stale_bound_reader_reloads_newer_runtime_checkpoint_before_matchup(tmp_path):
    writer = _bound_runtime_pair(tmp_path)
    stale_reader = _second_bound_runtime(tmp_path)

    earlier = stale_reader.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T2,
    )

    # Publish a later pair through another live runtime without reopening the
    # reader. The product read must refresh the sport-memory checkpoint itself,
    # not merely revalidate upstream identity/opponent roots around stale caches.
    from test_sport_memory_matchup_decision import (
        CODE_SHA,
        DEPENDENCY_SHA,
        T3,
        T3_PUBLISHED,
        T4,
    )

    for participant in ("participant-a", "participant-b"):
        writer.materialize(
            participant_entity_id=participant,
            scope=_scope(),
            causal_cutoff=T3,
            published_at=T3_PUBLISHED,
            code_sha256=CODE_SHA,
            dependency_sha256=DEPENDENCY_SHA,
            min_support=1,
        )

    later = stale_reader.matchup_as_of(
        "participant-a",
        "participant-b",
        _scope(),
        as_of=T4,
    )
    assert later.subject_memory_id != earlier.subject_memory_id
    assert later.opponent_memory_id != earlier.opponent_memory_id
    assert later.causal_cutoff == T3
