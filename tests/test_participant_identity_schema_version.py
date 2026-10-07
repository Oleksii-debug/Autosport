from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosport.participant_identity import (
    AliasRecord,
    EntityIdentity,
    EntityKind,
    ParticipantIdentityError,
    ParticipantIdentityRegistry,
)


SHA = "a" * 64
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-02T00:00:00Z"


def _identity(entity_id: str, kind: EntityKind) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity_id,
        kind=kind,
        source_reference=f"canonical:{entity_id}",
        evidence_sha256=SHA,
        first_known_at=T0,
        available_at=T0,
    )


def _rewrite_version(path: Path, version: int) -> bytes:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = version
    path.write_text(
        json.dumps(raw, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path.read_bytes()


def test_new_registry_publishes_semantic_v2(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"

    ParticipantIdentityRegistry.initialize_pristine(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == "autosport.participant_identity"
    assert raw["version"] == 2


def test_legacy_v1_old_vocabulary_loads_and_upgrades_on_next_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity.json"
    registry = ParticipantIdentityRegistry.initialize_pristine(path)
    team = _identity("team:legacy", EntityKind.TEAM)
    registry.add_entity(team)
    registry.add_alias(
        AliasRecord(
            source_id="provider-a",
            alias="legacy-team",
            entity_id=team.entity_id,
            valid_from=T0,
            valid_until=None,
            available_at=T0,
            evidence_sha256=SHA,
            recorded_at=T0,
        )
    )

    _rewrite_version(path, 1)

    legacy = ParticipantIdentityRegistry(path)
    assert legacy.resolve_alias("provider-a", "legacy-team", as_of=T1) == team
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1

    legacy.add_entity(_identity("participant:new", EntityKind.PARTICIPANT))

    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["version"] == 2
    reopened = ParticipantIdentityRegistry(path)
    assert reopened.resolve_alias("provider-a", "legacy-team", as_of=T1) == team


@pytest.mark.parametrize("kind", (EntityKind.SPORT, EntityKind.SEASON))
def test_legacy_v1_rejects_new_v2_entity_vocabulary_without_rewrite(
    tmp_path: Path,
    kind: EntityKind,
) -> None:
    path = tmp_path / f"identity-{kind.value.lower()}.json"
    registry = ParticipantIdentityRegistry.initialize_pristine(path)
    registry.add_entity(_identity(f"{kind.value.lower()}:new", kind))
    forged_v1 = _rewrite_version(path, 1)

    with pytest.raises(
        ParticipantIdentityError,
        match="legacy v1 identity registry contains unsupported entity kind",
    ):
        ParticipantIdentityRegistry(path)

    assert path.read_bytes() == forged_v1


def test_unknown_future_registry_version_fails_closed_without_rewrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity.json"
    ParticipantIdentityRegistry.initialize_pristine(path)
    future = _rewrite_version(path, 3)

    with pytest.raises(
        ParticipantIdentityError,
        match="unsupported identity registry schema",
    ):
        ParticipantIdentityRegistry(path)

    assert path.read_bytes() == future


def test_registry_mutation_ingress_rejects_record_subclasses(tmp_path: Path) -> None:
    from autosport.participant_identity import (
        EntityLineage,
        LineageRelation,
        RosterMembership,
    )

    class EntityIdentitySubclass(EntityIdentity):
        pass

    class AliasRecordSubclass(AliasRecord):
        pass

    class RosterMembershipSubclass(RosterMembership):
        pass

    class EntityLineageSubclass(EntityLineage):
        pass

    path = tmp_path / "identity.json"
    registry = ParticipantIdentityRegistry.initialize_pristine(path)

    with pytest.raises(TypeError, match="entity must be EntityIdentity"):
        registry.add_entity(
            EntityIdentitySubclass(
                entity_id="team:subclass",
                kind=EntityKind.TEAM,
                source_reference="canonical:team:subclass",
                evidence_sha256=SHA,
                first_known_at=T0,
                available_at=T0,
            )
        )

    team_a = _identity("team:a", EntityKind.TEAM)
    team_b = _identity("team:b", EntityKind.TEAM)
    registry.add_entity(team_a)
    registry.add_entity(team_b)

    with pytest.raises(TypeError, match="alias must be AliasRecord"):
        registry.add_alias(
            AliasRecordSubclass(
                source_id="provider-a",
                alias="team-a",
                entity_id=team_a.entity_id,
                valid_from=T0,
                valid_until=None,
                available_at=T0,
                evidence_sha256=SHA,
                recorded_at=T0,
            )
        )

    with pytest.raises(TypeError, match="membership must be RosterMembership"):
        registry.add_roster_membership(
            RosterMembershipSubclass(
                event_id="event-1",
                source_id="provider-a",
                entity_id=team_a.entity_id,
                member_from=T0,
                member_until=None,
                available_at=T0,
                evidence_sha256=SHA,
            )
        )

    with pytest.raises(TypeError, match="lineage must be EntityLineage"):
        registry.add_lineage(
            EntityLineageSubclass(
                predecessor_entity_id=team_a.entity_id,
                successor_entity_id=team_b.entity_id,
                relation=LineageRelation.SUPERSEDES,
                effective_from=T0,
                available_at=T0,
                recorded_at=T0,
                evidence_sha256=SHA,
            )
        )


def test_registry_mutation_ingress_revalidates_tampered_exact_records(
    tmp_path: Path,
) -> None:
    from autosport.participant_identity import (
        EntityLineage,
        LineageRelation,
        RosterMembership,
    )

    path = tmp_path / "identity.json"
    registry = ParticipantIdentityRegistry.initialize_pristine(path)
    team_a = _identity("team:a", EntityKind.TEAM)
    team_b = _identity("team:b", EntityKind.TEAM)
    registry.add_entity(team_a)
    registry.add_entity(team_b)

    bad_entity = _identity("team:tampered", EntityKind.TEAM)
    object.__setattr__(bad_entity, "entity_id", " team:tampered")
    with pytest.raises(ParticipantIdentityError, match="canonical string"):
        registry.add_entity(bad_entity)

    bad_alias = AliasRecord(
        source_id="provider-a",
        alias="team-a",
        entity_id=team_a.entity_id,
        valid_from=T0,
        valid_until=None,
        available_at=T0,
        evidence_sha256=SHA,
        recorded_at=T0,
    )
    object.__setattr__(bad_alias, "alias", " team-a")
    with pytest.raises(ParticipantIdentityError, match="canonical string"):
        registry.add_alias(bad_alias)

    bad_membership = RosterMembership(
        event_id="event-1",
        source_id="provider-a",
        entity_id=team_a.entity_id,
        member_from=T0,
        member_until=None,
        available_at=T0,
        evidence_sha256=SHA,
    )
    object.__setattr__(bad_membership, "source_id", " provider-a")
    with pytest.raises(ParticipantIdentityError, match="canonical string"):
        registry.add_roster_membership(bad_membership)

    bad_lineage = EntityLineage(
        predecessor_entity_id=team_a.entity_id,
        successor_entity_id=team_b.entity_id,
        relation=LineageRelation.SUPERSEDES,
        effective_from=T0,
        available_at=T0,
        recorded_at=T0,
        evidence_sha256=SHA,
    )
    object.__setattr__(bad_lineage, "evidence_sha256", "A" * 64)
    with pytest.raises(ParticipantIdentityError, match="SHA-256 hex"):
        registry.add_lineage(bad_lineage)


@pytest.mark.parametrize(
    "entity_id",
    (
        "team:one\nforged",
        "team:one\tforged",
        "team:one\rforged",
        "team:one\x7fforged",
    ),
)
def test_participant_identity_rejects_non_nul_control_aliases(entity_id: str) -> None:
    with pytest.raises(ParticipantIdentityError, match="canonical string"):
        _identity(entity_id, EntityKind.TEAM)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("source_id", "provider-a\nforged"),
        ("alias", "team-a\tforged"),
        ("entity_id", "team:a\x7fforged"),
    ),
)
def test_alias_identity_rejects_non_nul_control_aliases(
    field_name: str,
    value: str,
) -> None:
    kwargs: dict[str, object] = {
        "source_id": "provider-a",
        "alias": "team-a",
        "entity_id": "team:a",
        "valid_from": T0,
        "valid_until": None,
        "available_at": T0,
        "evidence_sha256": SHA,
        "recorded_at": T0,
    }
    kwargs[field_name] = value
    with pytest.raises(ParticipantIdentityError, match="canonical string"):
        AliasRecord(**kwargs)  # type: ignore[arg-type]


def test_alias_record_id_rejects_subclass_before_virtual_payload_dispatch() -> None:
    class HostileAliasRecord(AliasRecord):
        __slots__ = ()

        def payload(self) -> dict[str, str | None]:
            raise AssertionError("AliasRecord subclass payload must not execute")

    record = HostileAliasRecord(
        source_id="provider-a",
        alias="team-a",
        entity_id="team:a",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_until=None,
        available_at="2026-01-01T00:00:00+00:00",
        evidence_sha256="a" * 64,
        recorded_at="2026-01-01T00:00:00+00:00",
    )

    with pytest.raises(ParticipantIdentityError, match="exact AliasRecord"):
        _ = record.record_id


def test_entity_lineage_record_id_rejects_subclass_before_virtual_payload_dispatch() -> None:
    class HostileEntityLineage(EntityLineage):
        __slots__ = ()

        def payload(self) -> dict[str, str | None]:
            raise AssertionError("EntityLineage subclass payload must not execute")

    record = HostileEntityLineage(
        predecessor_entity_id="team:a",
        successor_entity_id="team:b",
        relation=LineageRelation.SUPERSEDES,
        effective_from="2026-01-01T00:00:00+00:00",
        available_at="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
        evidence_sha256="b" * 64,
    )

    with pytest.raises(ParticipantIdentityError, match="exact EntityLineage"):
        _ = record.record_id
