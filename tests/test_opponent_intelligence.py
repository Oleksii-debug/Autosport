import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import (
    InvalidationReason,
    InvalidationTarget,
    ObservedPerformance,
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
    SnapshotState,
)
from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    EntityLineage,
    IdentityView,
    LineageRelation,
    ParticipantIdentityRegistry,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"
T3 = "2026-01-04T00:00:00Z"
T4 = "2026-01-05T00:00:00Z"
T5 = "2026-01-06T00:00:00Z"


def persisted_snapshot_digest(item: dict[str, object]) -> str:
    payload = {
        key: value
        for key, value in item.items()
        if key != "snapshot_id"
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def entity(
    entity_id: str,
    *,
    kind: EntityKind = EntityKind.PARTICIPANT,
) -> EntityIdentity:
    return EntityIdentity(
        entity_id,
        kind,
        f"provider:{entity_id}",
        SHA_A,
        T0,
        T0,
    )


def alias(
    source: str,
    text: str,
    entity_id: str,
    *,
    available: str = T0,
    supersedes: str | None = None,
) -> AliasRecord:
    return AliasRecord(
        source,
        text,
        entity_id,
        T0,
        None,
        available,
        SHA_A,
        available,
        supersedes_record_id=supersedes,
    )


def observation(
    *,
    event_id: str = "event-1",
    source: str = "provider-a",
    subject: str = "Alex",
    opponent: str = "Blair",
    score: str = "1",
    observed: str = T1,
    available: str = T1,
    recorded: str = T1,
    evidence: str = SHA_B,
    truth: EvidenceTruth = EvidenceTruth.OBSERVED,
    supersedes: str | None = None,
    sport: str = "tennis",
    league: str = "Tour A",
    market_context: str = "match-outcome",
) -> ObservedPerformance:
    return ObservedPerformance(
        event_id=event_id,
        source_id=source,
        subject_alias=subject,
        opponent_alias=opponent,
        sport_id=sport,
        league_alias=league,
        market_context_id=market_context,
        score=score,
        observed_at=observed,
        available_at=available,
        recorded_at=recorded,
        evidence_sha256=evidence,
        truth=truth,
        supersedes_performance_id=supersedes,
    )


class OpponentIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        self.identity_path = root / "identity.json"
        self.store_path = root / "opponents.json"
        self.identities = (
            ParticipantIdentityRegistry.initialize_pristine(
                self.identity_path
            )
        )
        for item in (
            entity("p-alex"),
            entity("p-blair"),
            entity("p-casey"),
            entity("p-drew"),
            entity("league-tour-a", kind=EntityKind.LEAGUE),
        ):
            self.identities.add_entity(item)
        self.identities.add_alias(
            alias("provider-a", "Alex", "p-alex")
        )
        self.identities.add_alias(
            alias("provider-a", "Blair", "p-blair")
        )
        self.identities.add_alias(
            alias("provider-a", "Casey", "p-casey")
        )
        self.identities.add_alias(
            alias("provider-a", "Drew", "p-drew")
        )
        self.identities.add_alias(
            alias("provider-a", "Tour A", "league-tour-a")
        )
        self.store = OpponentIntelligenceStore.initialize_pristine(
            self.store_path,
            self.identities,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_simulated_performance_cannot_enter_historical_graph(self):
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "requires observed evidence",
        ):
            self.store.record_performance(
                observation(truth=EvidenceTruth.SIMULATED)
            )
        self.assertEqual(self.store.graph_edges(as_of=T2), ())

    def test_persisted_performance_without_truth_fails_closed(self):
        self.store.record_performance(observation())
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        del raw["performances"][0]["observation"]["truth"]
        self.store_path.write_text(
            json.dumps(raw, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "invalid opponent intelligence state",
        ):
            OpponentIntelligenceStore(
                self.store_path,
                ParticipantIdentityRegistry(self.identity_path),
            )

    def test_decision_view_excludes_late_backfill_but_restatement_includes_it(
        self,
    ):
        first = self.store.record_performance(observation())
        late = self.store.record_performance(
            observation(
                event_id="event-2",
                opponent="Casey",
                score="0",
                observed=T1,
                available=T3,
                recorded=T3,
                evidence=SHA_C,
            )
        )
        decision_ids = {
            edge.performance_id
            for edge in self.store.graph_edges(as_of=T2)
        }
        restated_ids = {
            edge.performance_id
            for edge in self.store.graph_edges(
                as_of=T2,
                view=IdentityView.RESTATED_RESEARCH,
            )
        }
        self.assertEqual(
            decision_ids,
            {first.performance_id},
        )
        self.assertEqual(
            restated_ids,
            {first.performance_id, late.performance_id},
        )

    def test_snapshot_is_deterministic_restart_safe_and_input_order_independent(
        self,
    ):
        first = self.store.record_performance(
            observation(score="1")
        )
        second = self.store.record_performance(
            observation(
                event_id="event-2",
                opponent="Casey",
                score="0",
                evidence=SHA_C,
            )
        )
        rating, feature = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=2,
        )
        self.assertEqual(
            rating.state,
            SnapshotState.SUPPORTED,
        )
        self.assertEqual(rating.rating, "0.5")
        self.assertEqual(rating.support, 2)
        self.assertEqual(rating.effective_sample, 2)
        self.assertEqual(rating.opponent_count, 2)
        self.assertEqual(
            set(rating.input_performance_ids),
            {first.performance_id, second.performance_id},
        )
        self.assertEqual(
            feature.rating_snapshot_id,
            rating.snapshot_id,
        )

        reopened = OpponentIntelligenceStore(
            self.store_path,
            ParticipantIdentityRegistry(self.identity_path),
        )
        rating_again, feature_again = reopened.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=2,
        )
        self.assertEqual(
            rating_again.snapshot_id,
            rating.snapshot_id,
        )
        self.assertEqual(
            feature_again.snapshot_id,
            feature.snapshot_id,
        )

    def test_repeated_same_opponent_does_not_inflate_effective_sample(
        self,
    ):
        self.store.record_performance(
            observation(event_id="event-1", score="1", evidence=SHA_B)
        )
        self.store.record_performance(
            observation(event_id="event-2", score="0", evidence=SHA_C)
        )
        rating, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=2,
        )
        self.assertEqual(rating.support, 2)
        self.assertEqual(rating.effective_sample, 1)
        self.assertEqual(rating.state, SnapshotState.INSUFFICIENT)
        self.assertIsNone(rating.rating)
        self.assertIsNone(rating.uncertainty)

    def test_insufficient_or_stale_evidence_never_publishes_exact_strength(
        self,
    ):
        self.store.record_performance(observation(score="1"))
        insufficient, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=2,
        )
        self.assertEqual(
            insufficient.state,
            SnapshotState.INSUFFICIENT,
        )
        self.assertIsNone(insufficient.rating)
        self.assertIsNone(insufficient.uncertainty)

        stale, feature = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T5,
            published_at=T5,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
            max_age_seconds=60,
        )
        self.assertEqual(
            stale.state,
            SnapshotState.INSUFFICIENT,
        )
        self.assertIsNone(stale.rating)
        self.assertGreater(feature.age_seconds, 60)

    def test_outcome_correction_invalidates_edge_and_snapshots_without_rewrite(
        self,
    ):
        first = self.store.record_performance(
            observation(score="1")
        )
        rating, feature = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
        )
        correction = self.store.record_performance(
            observation(
                score="0",
                available=T3,
                recorded=T3,
                evidence=SHA_C,
                supersedes=first.performance_id,
            )
        )
        rating_invalidations = self.store.invalidations(
            rating.snapshot_id
        )
        self.assertEqual(len(rating_invalidations), 1)
        self.assertEqual(
            rating_invalidations[0].reason,
            InvalidationReason.OUTCOME_CORRECTION,
        )
        self.assertEqual(
            rating_invalidations[0].evidence_id,
            correction.performance_id,
        )
        self.assertEqual(
            rating_invalidations[0].target_kind,
            InvalidationTarget.RATING_SNAPSHOT,
        )
        self.assertEqual(
            self.store.invalidations(
                feature.snapshot_id
            )[0].target_kind,
            InvalidationTarget.FEATURE_SNAPSHOT,
        )
        self.assertEqual(
            self.store.invalidations(
                first.performance_id
            )[0].target_kind,
            InvalidationTarget.OPPONENT_EDGE,
        )

        decision_ids = {
            edge.performance_id
            for edge in self.store.graph_edges(as_of=T2)
        }
        restated_ids = {
            edge.performance_id
            for edge in self.store.graph_edges(
                as_of=T2,
                view=IdentityView.RESTATED_RESEARCH,
            )
        }
        self.assertEqual(
            decision_ids,
            {first.performance_id},
        )
        self.assertEqual(
            restated_ids,
            {correction.performance_id},
        )
        corrected_rating, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T3,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            view=IdentityView.RESTATED_RESEARCH,
            min_support=1,
        )
        self.assertEqual(
            corrected_rating.predecessor_snapshot_ids,
            (rating.snapshot_id,),
        )

        reopened = OpponentIntelligenceStore(
            self.store_path,
            ParticipantIdentityRegistry(self.identity_path),
        )
        self.assertEqual(
            reopened.invalidations(rating.snapshot_id),
            rating_invalidations,
        )

    def test_late_alias_correction_fences_restatement_until_corrected_performance(
        self,
    ):
        first = self.store.record_performance(observation())
        rating, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
        )
        original = self.identities.resolve_alias_record(
            "provider-a",
            "Alex",
            as_of=T2,
        )
        corrected = alias(
            "provider-a",
            "Alex",
            "p-drew",
            available=T4,
            supersedes=original.record_id,
        )
        self.identities.add_alias(corrected)

        self.assertEqual(
            self.identities.resolve_alias(
                "provider-a",
                "Alex",
                as_of=T2,
            ).entity_id,
            "p-alex",
        )
        self.assertEqual(
            self.identities.resolve_alias(
                "provider-a",
                "Alex",
                as_of=T2,
                view=IdentityView.RESTATED_RESEARCH,
            ).entity_id,
            "p-drew",
        )
        self.store.refresh_identity_invalidations(
            detected_at=T4
        )
        matched = self.store.invalidations(
            rating.snapshot_id
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(
            matched[0].reason,
            InvalidationReason.IDENTITY_CORRECTION,
        )
        self.assertEqual(
            matched[0].evidence_id,
            corrected.record_id,
        )
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "identity-invalidated edge",
        ):
            self.store.graph_edges(
                as_of=T2,
                view=IdentityView.RESTATED_RESEARCH,
            )

        restated = self.store.record_performance(
            observation(
                score="1",
                available=T4,
                recorded=T4,
                evidence=corrected.record_id,
                supersedes=first.performance_id,
            ),
            identity_view=IdentityView.RESTATED_RESEARCH,
        )
        edges = self.store.graph_edges(
            as_of=T2,
            view=IdentityView.RESTATED_RESEARCH,
        )
        self.assertEqual(
            [edge.performance_id for edge in edges],
            [restated.performance_id],
        )
        self.assertEqual(
            edges[0].subject_entity_id,
            "p-drew",
        )
        corrected_rating, _ = self.store.build_snapshots(
            participant_entity_id="p-drew",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T4,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            view=IdentityView.RESTATED_RESEARCH,
            min_support=1,
        )
        self.assertEqual(
            corrected_rating.predecessor_snapshot_ids,
            (rating.snapshot_id,),
        )

    def test_late_lineage_correction_marks_downstream_for_recompute(
        self,
    ):
        first = self.store.record_performance(observation())
        rating, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
        )
        self.identities.add_lineage(
            EntityLineage(
                "p-alex",
                "p-drew",
                LineageRelation.SUPERSEDES,
                T0,
                T3,
                T3,
                SHA_C,
            )
        )
        self.store.refresh_identity_invalidations(
            detected_at=T4
        )
        matched = self.store.invalidations(
            rating.snapshot_id
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(
            matched[0].reason,
            InvalidationReason.IDENTITY_CORRECTION,
        )
        self.assertEqual(
            self.store.invalidations(
                first.performance_id
            )[0].target_kind,
            InvalidationTarget.OPPONENT_EDGE,
        )

    def test_same_display_name_across_sources_never_aliases(
        self,
    ):
        self.identities.add_entity(
            entity("p-provider-b")
        )
        self.identities.add_entity(
            entity("p-b-opponent")
        )
        self.identities.add_alias(
            alias(
                "provider-b",
                "Alex",
                "p-provider-b",
            )
        )
        self.identities.add_alias(
            alias(
                "provider-b",
                "Blair",
                "p-b-opponent",
            )
        )
        self.identities.add_alias(
            alias(
                "provider-b",
                "Tour A",
                "league-tour-a",
            )
        )
        first = self.store.record_performance(
            observation(source="provider-a")
        )
        second = self.store.record_performance(
            observation(
                source="provider-b",
                event_id="event-b",
                evidence=SHA_C,
            )
        )
        by_id = {
            edge.performance_id: edge
            for edge in self.store.graph_edges(as_of=T2)
        }
        self.assertEqual(
            by_id[first.performance_id].subject_entity_id,
            "p-alex",
        )
        self.assertEqual(
            by_id[second.performance_id].subject_entity_id,
            "p-provider-b",
        )

    def test_cross_kind_opponent_pair_fails_closed(self):
        self.identities.add_entity(
            entity("team-x", kind=EntityKind.TEAM)
        )
        self.identities.add_alias(
            alias("provider-a", "Team X", "team-x")
        )
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "same-kind",
        ):
            self.store.record_performance(
                observation(opponent="Team X")
            )

    def test_duplicate_delivery_is_idempotent_and_correction_fork_fails_closed(
        self,
    ):
        raw = observation()
        first = self.store.record_performance(raw)
        self.assertEqual(
            self.store.record_performance(raw),
            first,
        )
        correction = observation(
            score="0",
            available=T3,
            recorded=T3,
            evidence=SHA_C,
            supersedes=first.performance_id,
        )
        self.store.record_performance(correction)
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "correction fork",
        ):
            self.store.record_performance(
                observation(
                    score="0.5",
                    available=T4,
                    recorded=T4,
                    evidence="d" * 64,
                    supersedes=first.performance_id,
                )
            )

    def test_same_event_pair_requires_explicit_correction_and_is_orientation_invariant(
        self,
    ):
        first = self.store.record_performance(observation(score="1"))
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "requires explicit supersedes",
        ):
            self.store.record_performance(
                observation(
                    subject="Blair",
                    opponent="Alex",
                    score="0",
                    evidence=SHA_C,
                )
            )
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "preserve event/context identity",
        ):
            self.store.record_performance(
                observation(
                    subject="Blair",
                    opponent="Alex",
                    score="0",
                    available=T3,
                    recorded=T3,
                    evidence=SHA_C,
                    supersedes=first.performance_id,
                )
            )

    def test_restart_rejects_snapshot_when_all_durable_inputs_removed(
        self,
    ):
        self.store.record_performance(observation())
        self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
        )
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        raw["performances"] = []
        self.store_path.write_text(
            json.dumps(raw, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "missing durable performance input",
        ):
            OpponentIntelligenceStore(
                self.store_path,
                ParticipantIdentityRegistry(self.identity_path),
            )

    def test_restart_rejects_rehashed_derived_snapshot_tamper(
        self,
    ):
        self.store.record_performance(observation(score="1"))
        self.store.record_performance(
            observation(
                event_id="event-2",
                opponent="Casey",
                score="0",
                evidence=SHA_C,
            )
        )
        self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=2,
        )
        original = json.loads(
            self.store_path.read_text(encoding="utf-8")
        )
        cases = (
            (
                "rating_snapshots",
                "rating",
                "0.75",
                "rating snapshot derived evidence mismatch",
            ),
            (
                "feature_snapshots",
                "age_seconds",
                original["feature_snapshots"][0]["age_seconds"] + 1,
                "feature snapshot derived evidence mismatch",
            ),
        )
        for collection, field, value, message in cases:
            with self.subTest(collection=collection, field=field):
                tampered = json.loads(json.dumps(original))
                snapshot = tampered[collection][0]
                snapshot[field] = value
                snapshot["snapshot_id"] = persisted_snapshot_digest(
                    snapshot
                )
                self.store_path.write_text(
                    json.dumps(tampered, sort_keys=True),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    OpponentIntelligenceError,
                    message,
                ):
                    OpponentIntelligenceStore(
                        self.store_path,
                        ParticipantIdentityRegistry(
                            self.identity_path
                        ),
                    )

    def test_atomic_publication_failure_does_not_mutate_memory_or_disk(
        self,
    ):
        raw = observation()
        before = self.store_path.read_text(
            encoding="utf-8"
        )
        with patch(
            "autosport.opponent_intelligence.atomic_write_json",
            side_effect=OSError("disk fault"),
        ):
            with self.assertRaises(OSError):
                self.store.record_performance(raw)
        self.assertEqual(
            self.store_path.read_text(encoding="utf-8"),
            before,
        )
        self.assertEqual(
            self.store.graph_edges(as_of=T2),
            (),
        )

    def test_restart_rejects_snapshot_with_missing_durable_input(
        self,
    ):
        record = self.store.record_performance(observation())
        rating, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
        )
        raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        raw["performances"] = [
            item
            for item in raw["performances"]
            if item["performance_id"] != record.performance_id
        ]
        self.store_path.write_text(
            json.dumps(raw, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            OpponentIntelligenceError,
            "missing durable performance input",
        ):
            OpponentIntelligenceStore(
                self.store_path,
                ParticipantIdentityRegistry(self.identity_path),
            )
        self.assertIn(record.performance_id, rating.input_performance_ids)

    def test_snapshot_identity_binds_view_cutoff_config_and_exact_inputs(
        self,
    ):
        self.store.record_performance(observation())
        decision, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
        )
        restated, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            view=IdentityView.RESTATED_RESEARCH,
            min_support=1,
        )
        different_config, _ = self.store.build_snapshots(
            participant_entity_id="p-alex",
            sport_id="tennis",
            league_entity_id="league-tour-a",
            market_context_id="match-outcome",
            causal_cutoff=T2,
            published_at=T2,
            code_sha256=SHA_A,
            dependency_sha256=SHA_B,
            min_support=1,
            max_age_seconds=120,
        )
        self.assertNotEqual(
            decision.snapshot_id,
            restated.snapshot_id,
        )
        self.assertNotEqual(
            decision.snapshot_id,
            different_config.snapshot_id,
        )
        self.assertNotEqual(
            decision.config_sha256,
            different_config.config_sha256,
        )


if __name__ == "__main__":
    unittest.main()
