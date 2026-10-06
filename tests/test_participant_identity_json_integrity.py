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
