from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autosport.participant_identity import (
    AliasRecord, EntityIdentity, EntityKind, IdentityView, ParticipantIdentityError,
    ParticipantIdentityRegistry, RosterMembership, EntityLineage, LineageRelation,
)


SHA = "a" * 64
T0, T1, T2, T3 = ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z")


def entity(entity_id="p-1", available=T0):
    return EntityIdentity(entity_id, EntityKind.PARTICIPANT, "provider:p-1", SHA, T0, available)


def alias(entity_id="p-1", available=T0, valid_from=T0, valid_until=None, supersedes=None):
    return AliasRecord(
        "provider-a", "Alex", entity_id, valid_from, valid_until, available, SHA,
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
        correction = alias("p-new", available=T3, supersedes=original.record_id)
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
        registry.add_alias(AliasRecord("provider-a", "Alex", "p-provider-a", T0, None, T0, SHA))
        registry.add_alias(AliasRecord("provider-b", "Alex", "p-provider-b", T0, None, T0, SHA))
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

    def test_late_merge_lineage_is_evidence_not_historical_rewrite(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("p-old")); registry.add_entity(entity("p-canonical"))
        registry.add_alias(alias("p-old"))
        lineage = EntityLineage("p-old", "p-canonical", LineageRelation.MERGED_FROM, T1, T3, SHA)
        registry.add_lineage(lineage)
        self.assertEqual(registry.resolve_alias("provider-a", "Alex", as_of=T2).entity_id, "p-old")
        self.assertEqual(registry.lineage_at("p-old", as_of=T2), ())
        self.assertEqual(registry.lineage_at("p-old", as_of=T2, view=IdentityView.RESTATED_RESEARCH), (lineage,))
        self.assertEqual(ParticipantIdentityRegistry(self.path).lineage_at("p-canonical", as_of=T2, view=IdentityView.RESTATED_RESEARCH), (lineage,))

    def test_split_lineage_is_persisted_as_causal_evidence(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        registry.add_entity(entity("team-before")); registry.add_entity(entity("team-after"))
        lineage = EntityLineage("team-before", "team-after", LineageRelation.SPLIT_FROM, T1, T2, SHA)
        registry.add_lineage(lineage)
        self.assertEqual(registry.lineage_at("team-before", as_of=T1), ())
        self.assertEqual(registry.lineage_at("team-before", as_of=T2), (lineage,))

    def test_unknown_entity_and_invalid_interval_fail_closed(self):
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        with self.assertRaisesRegex(ParticipantIdentityError, "unknown entity"):
            registry.add_alias(alias())
        with self.assertRaisesRegex(ParticipantIdentityError, "after valid_from"):
            alias(valid_until=T0)
