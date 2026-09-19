import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autosport.opponent_graph import (
    ObservedPerformance,
    OpponentGraphError,
    OpponentGraphStore,
    SnapshotStatus,
)
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    EntityLineage,
    IdentityView,
    LineageRelation,
    ParticipantIdentityRegistry,
    RosterMembership,
)


SHA = "a" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T3 = "2026-01-04T00:00:00Z"
T4 = "2026-01-05T00:00:00Z"
T10 = "2026-01-11T00:00:00Z"


def identity(entity_id, kind=EntityKind.PARTICIPANT, available=T0):
    return EntityIdentity(
        entity_id=entity_id,
        kind=kind,
        source_reference=f"provider:{entity_id}",
        evidence_sha256=SHA,
        first_known_at=T0,
        available_at=available,
    )


def outcome(
    outcome_id,
    participant="p1",
    opponent="p2",
    *,
    event_id="event-1",
    source_id="provider-a",
    sport="table-tennis",
    league="league-1",
    score="1",
    observed=T1,
    available=T1,
    recorded=None,
    evidence=SHA,
    supersedes=None,
    reason=None,
):
    return ObservedPerformance(
        outcome_id=outcome_id,
        event_id=event_id,
        source_id=source_id,
        sport=sport,
        league_id=league,
        participant_id=participant,
        opponent_id=opponent,
        score=score,
        observed_at=observed,
        available_at=available,
        recorded_at=available if recorded is None else recorded,
        evidence_sha256=evidence,
        supersedes_outcome_id=supersedes,
        correction_reason=reason,
    )


class OpponentGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.identity_path = self.root / "identity.json"
        self.graph_path = self.root / "graph.json"
        self.registry = ParticipantIdentityRegistry.initialize_pristine(self.identity_path)
        self.registry.add_entity(identity("p1"))
        self.registry.add_entity(identity("p2"))
        self.registry.add_entity(identity("p3"))
        self.registry.add_entity(identity("league-1", EntityKind.LEAGUE))
        self.registry.add_entity(identity("league-2", EntityKind.LEAGUE))
        for event_id in ("event-1", "event-2", "event-3", "event-4"):
            for entity_id in ("p1", "p2", "p3"):
                self.registry.add_roster_membership(
                    RosterMembership(
                        event_id, "provider-a", entity_id, T0, None, T0, SHA
                    )
                )
        self.store = OpponentGraphStore.initialize_pristine(
            self.graph_path, self.registry
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_identity_authority_exposes_causal_entity_lookup(self):
        late = identity("late", available=T3)
        self.registry.add_entity(late)
        with self.assertRaisesRegex(Exception, "not known"):
            self.registry.entity_at("late", as_of=T2)
        self.assertEqual(
            self.registry.entity_at(
                "late", as_of=T2, view=IdentityView.RESTATED_RESEARCH
            ),
            late,
        )

    def test_outcome_requires_canonical_roster_and_matching_kinds(self):
        self.registry.add_entity(identity("team-1", EntityKind.TEAM))
        with self.assertRaisesRegex(OpponentGraphError, "kinds must match"):
            self.store.add_outcome(outcome("o-kind", opponent="team-1"))
        with self.assertRaisesRegex(OpponentGraphError, "canonical event roster"):
            self.store.add_outcome(outcome("o-roster", event_id="unknown-event"))

    def test_same_display_name_never_aliases_distinct_canonical_ids(self):
        self.registry.add_alias(
            AliasRecord("provider-a", "Alex", "p1", T0, None, T0, SHA, T0)
        )
        self.registry.add_alias(
            AliasRecord("provider-b", "Alex", "p2", T0, None, T0, SHA, T0)
        )
        self.store.add_outcome(outcome("o1", participant="p1", opponent="p3"))
        self.store.add_outcome(
            outcome(
                "o2",
                participant="p2",
                opponent="p3",
                event_id="event-2",
                observed=T2,
                available=T2,
                score="0",
            )
        )
        p1_edges = self.store.opponent_edges(
            "p1", sport="table-tennis", league_id="league-1", causal_cutoff=T3
        )
        p2_edges = self.store.opponent_edges(
            "p2", sport="table-tennis", league_id="league-1", causal_cutoff=T3
        )
        self.assertEqual(p1_edges[0].input_outcome_ids, ("o1",))
        self.assertEqual(p2_edges[0].input_outcome_ids, ("o2",))

    def test_future_and_late_backfill_are_excluded_from_decision_view(self):
        self.store.add_outcome(
            outcome("late", observed=T1, available=T3, recorded=T3)
        )
        decision = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=1,
        )
        self.assertEqual(decision.status, SnapshotStatus.INSUFFICIENT)
        self.assertEqual(decision.input_outcome_ids, ())

        restated = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            view=IdentityView.RESTATED_RESEARCH,
            min_support=1,
            stale_after_seconds=999999,
        )
        self.assertEqual(restated.status, SnapshotStatus.READY)
        self.assertEqual(restated.input_outcome_ids, ("late",))

    def test_reordered_delivery_has_deterministic_snapshot_identity(self):
        first_path = self.root / "first.json"
        second_path = self.root / "second.json"
        first = OpponentGraphStore.initialize_pristine(first_path, self.registry)
        second = OpponentGraphStore.initialize_pristine(second_path, self.registry)
        a = outcome("a", observed=T1, available=T1, score="1")
        b = outcome(
            "b", event_id="event-2", observed=T2, available=T2, score="0"
        )
        first.add_outcome(a)
        first.add_outcome(b)
        second.add_outcome(b)
        second.add_outcome(a)
        s1 = first.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T3,
            min_support=2,
            stale_after_seconds=999999,
        )
        s2 = second.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T3,
            min_support=2,
            stale_after_seconds=999999,
        )
        self.assertEqual(s1.snapshot_id, s2.snapshot_id)
        self.assertEqual(s1.rating, s2.rating)
        self.assertEqual(s1.input_digest, s2.input_digest)

    def test_repeated_snapshot_build_is_idempotent(self):
        self.store.add_outcome(outcome("o1"))
        first = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=1,
            stale_after_seconds=999999,
        )
        second = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=1,
            stale_after_seconds=999999,
        )
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(len(self.store._snapshots), 1)

    def test_duplicate_delivery_is_idempotent_after_restart(self):
        record = outcome("o1")
        self.store.add_outcome(record)
        self.store.add_outcome(record)
        reopened = OpponentGraphStore(self.graph_path, self.registry)
        reopened.add_outcome(record)
        edges = reopened.opponent_edges(
            "p1", sport="table-tennis", league_id="league-1", causal_cutoff=T2
        )
        self.assertEqual(edges[0].support_count, 1)
        self.assertEqual(edges[0].wins, 1)

    def test_conflicting_duplicate_and_correction_fork_fail_closed(self):
        original = outcome("o1")
        self.store.add_outcome(original)
        with self.assertRaisesRegex(OpponentGraphError, "conflicting immutable"):
            self.store.add_outcome(outcome("o1", score="0"))
        self.store.add_outcome(
            outcome(
                "o2",
                available=T2,
                recorded=T2,
                score="0",
                evidence="b" * 64,
                supersedes="o1",
                reason="provider correction",
            )
        )
        with self.assertRaisesRegex(OpponentGraphError, "fork"):
            self.store.add_outcome(
                outcome(
                    "o3",
                    available=T3,
                    recorded=T3,
                    score="0.5",
                    evidence="c" * 64,
                    supersedes="o1",
                    reason="second correction",
                )
            )

    def test_correction_preserves_old_snapshot_and_creates_restated_predecessor(self):
        original = outcome("o1")
        self.store.add_outcome(original)
        decision = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=1,
            stale_after_seconds=999999,
        )
        corrected = outcome(
            "o2",
            available=T3,
            recorded=T3,
            score="0",
            evidence="b" * 64,
            supersedes="o1",
            reason="official correction",
        )
        self.store.add_outcome(corrected)
        requests = self.store.invalidations_for_snapshot(decision.snapshot_id)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].affected_outcome_ids, ("o1",))
        self.assertEqual(self.store.get_snapshot(decision.snapshot_id), decision)

        restated = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            view=IdentityView.RESTATED_RESEARCH,
            min_support=1,
            stale_after_seconds=999999,
        )
        self.assertEqual(restated.predecessor_snapshot_id, decision.snapshot_id)
        self.assertEqual(restated.input_outcome_ids, ("o2",))
        self.assertNotEqual(restated.snapshot_id, decision.snapshot_id)

    def test_cross_sport_and_league_contexts_never_mix(self):
        self.store.add_outcome(outcome("tt", league="league-1"))
        self.store.add_outcome(
            outcome(
                "football",
                sport="football",
                league="league-2",
                event_id="event-2",
                observed=T2,
                available=T2,
            )
        )
        snapshot = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T3,
            min_support=1,
            stale_after_seconds=999999,
        )
        self.assertEqual(snapshot.input_outcome_ids, ("tt",))

    def test_insufficient_and_stale_snapshots_do_not_publish_exact_strength(self):
        self.store.add_outcome(outcome("o1"))
        insufficient = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=2,
        )
        self.assertEqual(insufficient.status, SnapshotStatus.INSUFFICIENT)
        self.assertIsNone(insufficient.rating)
        self.assertIsNone(insufficient.uncertainty)

        stale = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T10,
            min_support=1,
            stale_after_seconds=60,
        )
        self.assertEqual(stale.status, SnapshotStatus.STALE)
        self.assertIsNone(stale.rating)
        self.assertIsNone(stale.uncertainty)

    def test_ready_snapshot_binds_support_uncertainty_inputs_and_algorithm(self):
        self.store.add_outcome(outcome("o1", score="1"))
        self.store.add_outcome(
            outcome(
                "o2",
                event_id="event-2",
                observed=T2,
                available=T2,
                score="0.5",
            )
        )
        snapshot = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T3,
            min_support=2,
            stale_after_seconds=999999,
        )
        self.assertEqual(snapshot.status, SnapshotStatus.READY)
        self.assertEqual(snapshot.support_count, 2)
        self.assertIsNotNone(snapshot.rating)
        self.assertIsNotNone(snapshot.uncertainty)
        self.assertEqual(snapshot.algorithm_family, "LINEAR_PAIRWISE")
        self.assertEqual(snapshot.algorithm_version, "1")
        self.assertEqual(snapshot.input_outcome_ids, ("o1", "o2"))
        self.assertEqual(len(snapshot.config_digest), 64)
        self.assertEqual(len(snapshot.input_digest), 64)

    def test_identity_lineage_creates_recompute_fence_until_corrected_outcome(self):
        self.registry.add_entity(identity("p-new", available=T3))
        self.registry.add_roster_membership(
            RosterMembership(
                "event-1", "provider-a", "p-new", T0, None, T3, "b" * 64
            )
        )
        original = outcome("o1", participant="p1", opponent="p2")
        self.store.add_outcome(original)
        old_snapshot = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=1,
            stale_after_seconds=999999,
        )

        lineage = EntityLineage(
            "p1",
            "p-new",
            LineageRelation.SUPERSEDES,
            T0,
            T3,
            T3,
            "c" * 64,
        )
        self.registry.add_lineage(lineage)
        request = self.store.record_identity_lineage(lineage)
        self.assertIn("o1", request.affected_outcome_ids)
        self.assertIn(old_snapshot.snapshot_id, request.affected_snapshot_ids)

        with self.assertRaisesRegex(OpponentGraphError, "identity correction"):
            self.store.build_snapshot(
                "p1",
                sport="table-tennis",
                league_id="league-1",
                causal_cutoff=T4,
                min_support=1,
                stale_after_seconds=999999,
            )

        self.store.add_outcome(
            outcome(
                "o2",
                participant="p-new",
                opponent="p2",
                available=T3,
                recorded=T3,
                evidence="d" * 64,
                supersedes="o1",
                reason="canonical identity restatement",
            )
        )
        corrected = self.store.build_snapshot(
            "p-new",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T4,
            min_support=1,
            stale_after_seconds=999999,
        )
        self.assertEqual(corrected.input_outcome_ids, ("o2",))
        self.assertEqual(corrected.status, SnapshotStatus.READY)

    def test_identity_change_without_canonical_lineage_is_refused(self):
        self.registry.add_entity(identity("p-new", available=T2))
        self.registry.add_roster_membership(
            RosterMembership(
                "event-1", "provider-a", "p-new", T0, None, T2, "b" * 64
            )
        )
        self.store.add_outcome(outcome("o1"))
        with self.assertRaisesRegex(OpponentGraphError, "requires canonical identity lineage"):
            self.store.add_outcome(
                outcome(
                    "o2",
                    participant="p-new",
                    opponent="p2",
                    available=T3,
                    recorded=T3,
                    evidence="c" * 64,
                    supersedes="o1",
                    reason="unsupported identity rewrite",
                )
            )

    def test_failed_atomic_persist_does_not_publish_outcome_or_snapshot(self):
        before = self.graph_path.read_bytes()
        with patch("autosport.opponent_graph.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.add_outcome(outcome("o1"))
        self.assertEqual(self.graph_path.read_bytes(), before)
        self.assertEqual(
            OpponentGraphStore(self.graph_path, self.registry).opponent_edges(
                "p1",
                sport="table-tennis",
                league_id="league-1",
                causal_cutoff=T2,
            ),
            (),
        )

        self.store.add_outcome(outcome("o1"))
        before = self.graph_path.read_bytes()
        with patch("autosport.opponent_graph.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.build_snapshot(
                    "p1",
                    sport="table-tennis",
                    league_id="league-1",
                    causal_cutoff=T2,
                    min_support=1,
                    stale_after_seconds=999999,
                )
        self.assertEqual(self.graph_path.read_bytes(), before)

    def test_tampered_snapshot_input_digest_fails_on_restart(self):
        self.store.add_outcome(outcome("o1"))
        snapshot = self.store.build_snapshot(
            "p1",
            sport="table-tennis",
            league_id="league-1",
            causal_cutoff=T2,
            min_support=1,
            stale_after_seconds=999999,
        )
        raw = self.graph_path.read_text(encoding="utf-8")
        self.graph_path.write_text(
            raw.replace(snapshot.input_digest, "f" * 64), encoding="utf-8"
        )
        with self.assertRaisesRegex(OpponentGraphError, "snapshot_id|input_digest"):
            OpponentGraphStore(self.graph_path, self.registry)


if __name__ == "__main__":
    unittest.main()
