from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    EntityLineage,
    IdentityView,
    LineageRelation,
    ParticipantIdentityError,
    ParticipantIdentityRegistry,
    RosterMembership,
)


SHA = "a" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"
T2 = "2026-01-03T00:00:00Z"


def identity(entity_id: str, kind: EntityKind, source_reference: str) -> EntityIdentity:
    return EntityIdentity(entity_id, kind, source_reference, SHA, T0, T0)


class SportSeasonIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "identity.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sport_and_season_aliases_survive_restart(self) -> None:
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        sport = identity("sport:football", EntityKind.SPORT, "canonical:football")
        season = identity("season:football:2026", EntityKind.SEASON, "provider:season-2026")
        registry.add_entity(sport)
        registry.add_entity(season)
        registry.add_alias(
            AliasRecord("provider-a", "soccer", sport.entity_id, T0, None, T0, SHA, T0)
        )
        registry.add_alias(
            AliasRecord("provider-a", "2026", season.entity_id, T0, None, T0, SHA, T0)
        )

        reopened = ParticipantIdentityRegistry(self.path)
        self.assertIs(
            reopened.resolve_alias("provider-a", "soccer", as_of=T1).kind,
            EntityKind.SPORT,
        )
        self.assertIs(
            reopened.resolve_alias("provider-a", "2026", as_of=T1).kind,
            EntityKind.SEASON,
        )

    def test_late_season_correction_preserves_decision_truth(self) -> None:
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        old = identity("season:football:2026-provider", EntityKind.SEASON, "provider:2026")
        canonical = identity("season:football:2026", EntityKind.SEASON, "canonical:2026")
        registry.add_entity(old)
        registry.add_entity(canonical)

        original = AliasRecord(
            "provider-a", "season-26", old.entity_id, T0, None, T0, SHA, T0
        )
        registry.add_alias(original)
        correction = AliasRecord(
            "provider-a",
            "season-26",
            canonical.entity_id,
            T0,
            None,
            T2,
            SHA,
            T2,
            supersedes_record_id=original.record_id,
        )
        registry.add_alias(correction)
        lineage = EntityLineage(
            old.entity_id,
            canonical.entity_id,
            LineageRelation.SUPERSEDES,
            T0,
            T2,
            T2,
            SHA,
        )
        registry.add_lineage(lineage)

        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(
            reopened.resolve_alias("provider-a", "season-26", as_of=T1).entity_id,
            old.entity_id,
        )
        self.assertEqual(
            reopened.resolve_alias(
                "provider-a",
                "season-26",
                as_of=T1,
                view=IdentityView.RESTATED_RESEARCH,
            ).entity_id,
            canonical.entity_id,
        )
        self.assertEqual(
            reopened.lineage_at(
                old.entity_id,
                as_of=T1,
                view=IdentityView.RESTATED_RESEARCH,
            ),
            (lineage,),
        )

    def test_event_roster_rejects_non_participant_entity_kinds(self) -> None:
        for kind in (EntityKind.SPORT, EntityKind.LEAGUE, EntityKind.SEASON):
            with self.subTest(kind=kind):
                path = Path(self.temporary.name) / f"identity-{kind.value.lower()}.json"
                registry = ParticipantIdentityRegistry.initialize_pristine(path)
                non_participant = identity(
                    f"{kind.value.lower()}:example",
                    kind,
                    f"canonical:{kind.value.lower()}:example",
                )
                registry.add_entity(non_participant)
                with self.assertRaisesRegex(
                    ParticipantIdentityError,
                    "PARTICIPANT or TEAM",
                ):
                    registry.add_roster_membership(
                        RosterMembership(
                            "event-1",
                            "provider-a",
                            non_participant.entity_id,
                            T0,
                            None,
                            T0,
                            SHA,
                        )
                    )
                self.assertEqual(
                    ParticipantIdentityRegistry(path).roster_at(
                        "event-1",
                        "provider-a",
                        as_of=T1,
                    ),
                    (),
                )

    def test_event_roster_accepts_participant_and_team_identities(self) -> None:
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        participant = identity("participant:1", EntityKind.PARTICIPANT, "provider:p1")
        team = identity("team:1", EntityKind.TEAM, "provider:t1")
        registry.add_entity(participant)
        registry.add_entity(team)
        registry.add_roster_membership(
            RosterMembership("event-1", "provider-a", participant.entity_id, T0, None, T0, SHA)
        )
        registry.add_roster_membership(
            RosterMembership("event-1", "provider-a", team.entity_id, T0, None, T0, "b" * 64)
        )
        reopened = ParticipantIdentityRegistry(self.path)
        self.assertEqual(
            {entity.entity_id for entity in reopened.roster_at("event-1", "provider-a", as_of=T1)},
            {participant.entity_id, team.entity_id},
        )

    def test_cross_kind_correction_fails_closed(self) -> None:
        registry = ParticipantIdentityRegistry.initialize_pristine(self.path)
        sport = identity("sport:football", EntityKind.SPORT, "canonical:football")
        season = identity("season:football:2026", EntityKind.SEASON, "canonical:2026")
        registry.add_entity(sport)
        registry.add_entity(season)

        with self.assertRaisesRegex(
            ParticipantIdentityError,
            "same EntityKind",
        ):
            registry.add_lineage(
                EntityLineage(
                    sport.entity_id,
                    season.entity_id,
                    LineageRelation.SUPERSEDES,
                    T0,
                    T0,
                    T0,
                    SHA,
                )
            )


if __name__ == "__main__":
    unittest.main()
