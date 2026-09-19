from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autosport.participant_identity import (
    AliasRecord, EntityIdentity, EntityKind, IdentityView, ParticipantIdentityError,
    ParticipantIdentityRegistry, RosterMembership, EntityLineage, LineageRelation,
)


SHA = "a" * 64
T0, T1, T2, T3 = ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z")


def entity(entity_id="p-1", available=T0):
    return EntityIdentity(entity_id, EntityKind.PARTICIPANT, "provider:p-1", SHA, T0, available)


def alias(entity_id="p-1", available=T0, valid_from=T0, valid_until=None, supersedes=None, recorded=None):
    recorded_at = available if recorded is None else recorded
    return AliasRecord(
        "provider-a", "Alex", entity_id, valid_from, valid_until, available, SHA, recorded_at,
        supersedes_record_id=supersedes,
    )


class ParticipantIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "identity.json"

    def tearDown(self):
        self.temporary.cleanup()

    def test_alias_resolution_is_causal_and_reopenable(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity()); registry.add_alias(alias())
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-1")
        self.assertEqual(ParticipantIdentityRegistry(self.path).resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-1")

    def test_late_alias_is_restated_only(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity(available=T3)); registry.add_alias(alias(available=T3))
        with self.assertRaisesRegex(ParticipantIdentityError, "unambiguously"):
            registry.resolve_alias("provider-a", "Alex", as_of=T1)
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T1, view=IdentityView.RESTATED_RESEARCH).entity_id, "p-1")

    def test_conflicting_overlapping_aliases_fail_closed(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-1")); registry.add_entity(entity("p-2")); registry.add_alias(alias("p-1"))
        with self.assertRaisesRegex(ParticipantIdentityError, "conflicting alias"):
            registry.add_alias(alias("p-2"))

    def test_late_alias_correction_restates_without_rewriting_decision_truth(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-old")); registry.add_entity(entity("p-new"))
        original = alias("p-old")
        registry.add_alias(original)
        correction = alias("p-new", available=T2, recorded=T3, supersedes=original.record_id)
        registry.add_alias(correction)

        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T2).entity_id, "p-old")
        self.assertEqual(
            registry.resolve_alias("provider-a", "Alex", as_of=T2, view=IdentityView.RESTATED_RESEARCH).entity_id,
            "p-new",
        )
        self.assertEqual(
            registry.resolve_alias_record(
                "provider-a", "Alex", as_of=T2, view=IdentityView.RESTATED_RESEARCH
            ).supersedes_record_id,
            original.record_id,
        )
        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(reopened.resolve_alias("provider-a", "Alex", as_of=T2).entity_id, "p-old")
        self.assertEqual(
            reopened.resolve_alias("provider-a", "Alex", as_of=T2, view=IdentityView.RESTATED_RESEARCH).entity_id,
            "p-new",
        )
        self.assertEqual(reopened.resolve_alias("provider-a", "Alex", as_of=T3).entity_id, "p-new")

    def test_alias_correction_fork_fails_closed(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-old")); registry.add_entity(entity("p-new")); registry.add_entity(entity("p-other"))
        original = alias("p-old")
        registry.add_alias(original)
        registry.add_alias(alias("p-new", available=T2, supersedes=original.record_id))
        with self.assertRaisesRegex(ParticipantIdentityError, "correction fork"):
            registry.add_alias(alias("p-other", available=T3, supersedes=original.record_id))

    def test_same_name_is_isolated_by_provider_source(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-provider-a")); registry.add_entity(entity("p-provider-b"))
        registry.add_alias(AliasRecord("provider-a", "Alex", "p-provider-a", T0, None, T0, SHA, T0))
        registry.add_alias(AliasRecord("provider-b", "Alex", "p-provider-b", T0, None, T0, SHA, T0))
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-provider-a")
        self.assertEqual(registry.resolve_alias("provider-b", "Alex", as_of=T1).entity_id, "p-provider-b")

    def test_alias_can_change_after_non_overlapping_interval(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-1")); registry.add_entity(entity("p-2")); registry.add_alias(alias("p-1", valid_until=T2)); registry.add_alias(alias("p-2", valid_from=T2, available=T2))
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-1")
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T3).entity_id, "p-2")

    def test_roster_is_identity_only_causal_evidence(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity()); registry.add_roster_membership(RosterMembership("event-1", "provider-a", "p-1", T0, None, T2, SHA))
        self.assertEqual(registry.roster_at("event-1", "provider-a", as_of=T1), ())
        self.assertEqual([item.entity_id for item in registry.roster_at("event-1", "provider-a", as_of=T1, view=IdentityView.RESTATED_RESEARCH)], ["p-1"])

    def test_roster_cannot_expose_entity_before_entity_availability(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity(available=T3))
        registry.add_roster_membership(RosterMembership("event-1", "provider-a", "p-1", T0, None, T1, SHA))
        with self.assertRaisesRegex(ParticipantIdentityError, "entity not known"):
            registry.roster_at("event-1", "provider-a", as_of=T2)
        self.assertEqual(
            [item.entity_id for item in registry.roster_at(
                "event-1", "provider-a", as_of=T2, view=IdentityView.RESTATED_RESEARCH
            )],
            ["p-1"],
        )
        self.assertEqual(
            [item.entity_id for item in registry.roster_at("event-1", "provider-a", as_of=T3)],
            ["p-1"],
        )


    def test_distinct_overlapping_roster_evidence_deduplicates_entity_after_restart(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-1"))
        first = RosterMembership(
            "event-1", "provider-a", "p-1", T0, T3, T0, "a" * 64
        )
        second = RosterMembership(
            "event-1", "provider-a", "p-1", T0, T3, T1, "b" * 64
        )
        registry.add_roster_membership(first)
        registry.add_roster_membership(second)

        self.assertEqual(
            [item.entity_id for item in registry.roster_at("event-1", "provider-a", as_of=T2)],
            ["p-1"],
        )
        self.assertEqual(
            [item.entity_id for item in registry.roster_at(
                "event-1", "provider-a", as_of=T2, view=IdentityView.RESTATED_RESEARCH
            )],
            ["p-1"],
        )

        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(
            [item.entity_id for item in reopened.roster_at(
                "event-1", "provider-a", as_of=T2
            )],
            ["p-1"],
        )

    def test_duplicate_alias_and_roster_replay_are_idempotent_after_restart(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity())
        alias_record = alias()
        roster_record = RosterMembership("event-1", "provider-a", "p-1", T0, None, T0, SHA)
        registry.add_alias(alias_record); registry.add_alias(alias_record)
        registry.add_roster_membership(roster_record); registry.add_roster_membership(roster_record)
        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(reopened.resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-1")
        self.assertEqual(
            [item.entity_id for item in reopened.roster_at("event-1", "provider-a", as_of=T1)],
            ["p-1"],
        )

    def test_invalid_roster_temporal_order_fails_closed(self):
        with self.assertRaisesRegex(ParticipantIdentityError, "member_until must be after member_from"):
            RosterMembership("event-1", "provider-a", "p-1", T1, T0, T1, SHA)
        with self.assertRaisesRegex(ParticipantIdentityError, "cannot be available before member_from"):
            RosterMembership("event-1", "provider-a", "p-1", T1, None, T0, SHA)

    def test_recorded_at_cannot_precede_evidence_availability(self):
        with self.assertRaisesRegex(ParticipantIdentityError, "alias cannot be recorded before available_at"):
            alias(available=T2, recorded=T1)
        with self.assertRaisesRegex(ParticipantIdentityError, "lineage cannot be recorded before available_at"):
            EntityLineage("p-old", "p-new", LineageRelation.SUPERSEDES, T0, T2, T1, SHA)

    def test_end_to_end_decision_then_restatement_survives_restart(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-old")); registry.add_entity(entity("p-canonical"))
        original = alias("p-old")
        registry.add_alias(original)
        registry.add_roster_membership(RosterMembership("event-1", "provider-a", "p-old", T0, None, T0, SHA))

        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-old")
        self.assertEqual(
            [item.entity_id for item in registry.roster_at("event-1", "provider-a", as_of=T1)],
            ["p-old"],
        )

        correction = alias("p-canonical", available=T2, recorded=T3, supersedes=original.record_id)
        registry.add_alias(correction)
        lineage = EntityLineage(
            "p-old", "p-canonical", LineageRelation.SUPERSEDES, T1, T2, T3, SHA
        )
        registry.add_lineage(lineage)

        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(reopened.resolve_alias("provider-a", "Alex", as_of=T1).entity_id, "p-old")
        self.assertEqual(
            reopened.resolve_alias(
                "provider-a", "Alex", as_of=T1, view=IdentityView.RESTATED_RESEARCH
            ).entity_id,
            "p-canonical",
        )
        self.assertEqual(
            reopened.lineage_at("p-old", as_of=T1, view=IdentityView.RESTATED_RESEARCH),
            (lineage,),
        )
        self.assertEqual(
            [item.entity_id for item in reopened.roster_at("event-1", "provider-a", as_of=T1)],
            ["p-old"],
        )

    def test_late_merge_lineage_is_evidence_not_historical_rewrite(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-old")); registry.add_entity(entity("p-canonical"))
        registry.add_alias(alias("p-old"))
        lineage = EntityLineage("p-old", "p-canonical", LineageRelation.MERGED_FROM, T1, T2, T3, SHA)
        registry.add_lineage(lineage)
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T2).entity_id, "p-old")
        self.assertEqual(registry.lineage_at("p-old", as_of=T2), ())
        self.assertEqual(registry.lineage_at("p-old", as_of=T2, view=IdentityView.RESTATED_RESEARCH), (lineage,))
        self.assertEqual(registry.lineage_at("p-old", as_of=T3), (lineage,))
        self.assertEqual(ParticipantIdentityRegistry(self.path).lineage_at("p-canonical", as_of=T2, view=IdentityView.RESTATED_RESEARCH), (lineage,))

    def test_split_lineage_is_persisted_as_causal_evidence(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("team-before")); registry.add_entity(entity("team-after"))
        lineage = EntityLineage("team-before", "team-after", LineageRelation.SPLIT_FROM, T1, T2, T2, SHA)
        registry.add_lineage(lineage)
        self.assertEqual(registry.lineage_at("team-before", as_of=T1), ())
        self.assertEqual(registry.lineage_at("team-before", as_of=T2), (lineage,))

    def test_failed_persist_does_not_publish_uncommitted_identity_state(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        before = self.path.read_bytes()
        with patch("autosport.participant_identity.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                registry.add_entity(entity())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("p-1", registry._entities)
        self.assertNotIn("p-1", ParticipantIdentityRegistry(self.path)._entities)

        registry.add_entity(entity())
        alias_record = alias()
        before = self.path.read_bytes()
        with patch("autosport.participant_identity.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                registry.add_alias(alias_record)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ParticipantIdentityError, "unambiguously"):
            registry.resolve_alias("provider-a", "Alex", as_of=T1)
        with self.assertRaisesRegex(ParticipantIdentityError, "unambiguously"):
            ParticipantIdentityRegistry(self.path).resolve_alias("provider-a", "Alex", as_of=T1)

        roster_record = RosterMembership("event-1", "provider-a", "p-1", T0, None, T0, SHA)
        before = self.path.read_bytes()
        with patch("autosport.participant_identity.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                registry.add_roster_membership(roster_record)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(registry.roster_at("event-1", "provider-a", as_of=T1), ())
        self.assertEqual(ParticipantIdentityRegistry(self.path).roster_at("event-1", "provider-a", as_of=T1), ())

        registry.add_entity(entity("p-2"))
        lineage = EntityLineage("p-1", "p-2", LineageRelation.SUPERSEDES, T1, T1, T1, SHA)
        before = self.path.read_bytes()
        with patch("autosport.participant_identity.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                registry.add_lineage(lineage)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(registry.lineage_at("p-1", as_of=T2), ())
        self.assertEqual(ParticipantIdentityRegistry(self.path).lineage_at("p-1", as_of=T2), ())

    def test_lineage_interval_conflict_and_split_fanout_are_explicit(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("old")); registry.add_entity(entity("new-a")); registry.add_entity(entity("new-b"))
        supersedes = EntityLineage(
            "old", "new-a", LineageRelation.SUPERSEDES, T1, T1, T1, SHA, valid_until=T3
        )
        registry.add_lineage(supersedes)
        self.assertEqual(registry.lineage_at("old", as_of=T2), (supersedes,))
        self.assertEqual(registry.lineage_at("old", as_of=T3), ())
        with self.assertRaisesRegex(ParticipantIdentityError, "conflicting lineage"):
            registry.add_lineage(
                EntityLineage("old", "new-b", LineageRelation.SUPERSEDES, T2, T2, T2, SHA)
            )

        split_path = Path(self.temporary.name) / "split-fanout.json"
        split_registry = ParticipantIdentityRegistry.initialize_pristine(split_path)
        split_registry.add_entity(entity("old")); split_registry.add_entity(entity("new-a")); split_registry.add_entity(entity("new-b"))
        first = EntityLineage("old", "new-a", LineageRelation.SPLIT_FROM, T1, T1, T1, SHA)
        second = EntityLineage("old", "new-b", LineageRelation.SPLIT_FROM, T1, T1, T1, SHA)
        split_registry.add_lineage(first); split_registry.add_lineage(second)
        self.assertEqual(set(split_registry.lineage_at("old", as_of=T2)), {first, second})

    def test_lineage_rejects_future_entity_and_cross_kind_equivalence(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("old"))
        registry.add_entity(entity("future", available=T3))
        with self.assertRaisesRegex(ParticipantIdentityError, "referenced entity identity"):
            registry.add_lineage(
                EntityLineage("old", "future", LineageRelation.SUPERSEDES, T1, T2, T2, SHA)
            )

        league = EntityIdentity("league-1", EntityKind.LEAGUE, "provider:league-1", SHA, T0, T0)
        registry.add_entity(league)
        with self.assertRaisesRegex(ParticipantIdentityError, "same EntityKind"):
            registry.add_lineage(
                EntityLineage("old", "league-1", LineageRelation.MERGED_FROM, T1, T1, T1, SHA)
            )

    def test_non_split_lineage_cycle_is_rejected_and_survives_restart(self):
        direct_path = Path(self.temporary.name) / "direct-cycle.json"
        direct = ParticipantIdentityRegistry.initialize_pristine(direct_path)
        direct.add_entity(entity("a"))
        direct.add_entity(entity("b"))
        direct_edge = EntityLineage("a", "b", LineageRelation.SUPERSEDES, T1, T1, T1, SHA)
        direct.add_lineage(direct_edge)
        with self.assertRaisesRegex(ParticipantIdentityError, "cyclic equivalence"):
            direct.add_lineage(EntityLineage("b", "a", LineageRelation.SUPERSEDES, T1, T1, T1, "b" * 64))
        self.assertEqual(ParticipantIdentityRegistry(direct_path).lineage_at("a", as_of=T2), (direct_edge,))

        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        for entity_id in ("a", "b", "c"):
            registry.add_entity(entity(entity_id))

        first = EntityLineage("a", "b", LineageRelation.SUPERSEDES, T1, T1, T1, SHA)
        second = EntityLineage("b", "c", LineageRelation.MERGED_FROM, T1, T1, T1, SHA)
        registry.add_lineage(first)
        registry.add_lineage(second)
        with self.assertRaisesRegex(ParticipantIdentityError, "cyclic equivalence"):
            registry.add_lineage(EntityLineage("c", "a", LineageRelation.SUPERSEDES, T1, T1, T1, "c" * 64))

        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(reopened.lineage_at("a", as_of=T2), (first,))
        self.assertEqual(reopened.lineage_at("b", as_of=T2), (first, second))
        self.assertEqual(reopened.lineage_at("c", as_of=T2), (second,))
    def test_unknown_entity_and_invalid_interval_fail_closed(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        with self.assertRaisesRegex(ParticipantIdentityError, "unknown entity"):
            registry.add_alias(alias())
        with self.assertRaisesRegex(ParticipantIdentityError, "after valid_from"):
            alias(valid_until=T0)
