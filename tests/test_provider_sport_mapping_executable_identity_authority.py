from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping
from autosport.provider_sport_mapping import (
    ProviderSportEvidence,
    ProviderSportMappingError,
    ProviderSportMappingRegistry,
)


def _evidence() -> bytes:
    return json.dumps(
        {
            "schema": "autosport.provider_sport_mapping_evidence",
            "schema_version": 1,
            "provider_namespace": "betfair",
            "provider_sport_id": "1",
            "canonical_sport": "football",
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_until": None,
            "evidence_available_at": "2026-01-02T00:00:00Z",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_in_place_recording_clock_code_mutation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderSportMappingRegistry.initialize_pristine(tmp_path / "sport-map.json")

    def forged_clock() -> str:
        return "2026-01-02T00:00:00Z"

    monkeypatch.setattr(mapping._utc_now, "__code__", forged_clock.__code__)

    with pytest.raises(
        ProviderSportMappingError,
        match="clock|chronology|dispatch|authority|record|executable",
    ):
        registry.register_evidence(_evidence())


def test_in_place_evidence_parser_code_mutation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderSportMappingRegistry.initialize_pristine(tmp_path / "sport-map.json")
    parser_descriptor = vars(ProviderSportEvidence)["from_exact_bytes"]
    parser = parser_descriptor.__func__

    def forged_parser(cls, raw_bytes: bytes):
        del raw_bytes
        return cls(
            provider_namespace="betfair",
            provider_sport_id="1",
            canonical_sport="football",
            valid_from="2026-01-01T00:00:00Z",
            valid_until=None,
            evidence_available_at="2026-01-02T00:00:00Z",
            source_snapshot_sha256="0" * 64,
        )

    monkeypatch.setattr(parser, "__code__", forged_parser.__code__)

    with pytest.raises(
        ProviderSportMappingError,
        match="parser|evidence|dispatch|authority|executable",
    ):
        registry.register_evidence(_evidence())
