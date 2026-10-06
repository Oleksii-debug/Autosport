import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from autosport.participant_identity import (
    ParticipantIdentityError,
    ParticipantIdentityRegistry,
)


def _write_registry(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")


def test_restart_rejects_duplicate_schema_version_key() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "identity.json"
        _write_registry(
            path,
            (
                '{"schema":"autosport.participant_identity",'
                '"version":999,"version":1,'
                '"entities":[],"aliases":[],"rosters":[],"lineages":[]}'
            ),
        )

        with pytest.raises(
            ParticipantIdentityError,
            match="duplicate identity registry JSON key: version",
        ):
            ParticipantIdentityRegistry(path)


def test_restart_rejects_duplicate_nested_entity_identity_key() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "identity.json"
        _write_registry(
            path,
            (
                '{"schema":"autosport.participant_identity","version":1,'
                '"entities":[{'
                '"entity_id":"sport:poison","entity_id":"sport:football",'
                '"kind":"SPORT","source_reference":"canonical:football",'
                '"evidence_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
                '"first_known_at":"2026-01-01T00:00:00Z",'
                '"available_at":"2026-01-01T00:00:00Z"}],'
                '"aliases":[],"rosters":[],"lineages":[]}'
            ),
        )

        with pytest.raises(
            ParticipantIdentityError,
            match="duplicate identity registry JSON key: entity_id",
        ):
            ParticipantIdentityRegistry(path)


@pytest.mark.parametrize(
    ("collection", "record"),
    (
        (
            "aliases",
            {
                "source_id": "provider-a",
                "alias": "Player A",
                "entity_id": "participant:a",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_until": None,
                "available_at": "2026-01-01T00:00:00Z",
                "evidence_sha256": "b" * 64,
                "recorded_at": "2026-01-01T00:00:00Z",
                "relation": "CONFIRMED",
                "supersedes_record_id": None,
            },
        ),
        (
            "rosters",
            {
                "event_id": "event-1",
                "source_id": "provider-a",
                "entity_id": "participant:a",
                "member_from": "2026-01-01T00:00:00Z",
                "member_until": None,
                "available_at": "2026-01-01T00:00:00Z",
                "evidence_sha256": "c" * 64,
            },
        ),
        (
            "lineages",
            {
                "predecessor_entity_id": "participant:a",
                "successor_entity_id": "participant:b",
                "relation": "SUPERSEDES",
                "effective_from": "2026-01-01T00:00:00Z",
                "available_at": "2026-01-01T00:00:00Z",
                "recorded_at": "2026-01-01T00:00:00Z",
                "evidence_sha256": "d" * 64,
                "valid_until": None,
            },
        ),
    ),
)
def test_restart_rejects_duplicate_persisted_records(
    collection: str,
    record: dict[str, object],
) -> None:
    entities = [
        {
            "entity_id": entity_id,
            "kind": "PARTICIPANT",
            "source_reference": f"canonical:{entity_id}",
            "evidence_sha256": "a" * 64,
            "first_known_at": "2026-01-01T00:00:00Z",
            "available_at": "2026-01-01T00:00:00Z",
        }
        for entity_id in ("participant:a", "participant:b")
    ]
    payload: dict[str, object] = {
        "schema": "autosport.participant_identity",
        "version": 1,
        "entities": entities,
        "aliases": [],
        "rosters": [],
        "lineages": [],
    }
    payload[collection] = [record, dict(record)]

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "identity.json"
        _write_registry(path, json.dumps(payload, sort_keys=True))

        with pytest.raises(
            ParticipantIdentityError,
            match=rf"duplicate identity registry {collection} record",
        ):
            ParticipantIdentityRegistry(path)
