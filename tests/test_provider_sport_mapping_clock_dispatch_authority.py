from __future__ import annotations

import json
from pathlib import Path

import pytest

import autosport.provider_sport_mapping as mapping
from autosport.provider_sport_mapping import (
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


def test_rebound_recording_clock_cannot_backdate_positive_mapping_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderSportMappingRegistry.initialize_pristine(
        tmp_path / "sport-map.json"
    )

    # Positive chronology is claimed to be product-owned. Rebinding a module-global
    # helper must therefore fail closed rather than mint a caller-selected recorded_at.
    monkeypatch.setattr(
        mapping,
        "_utc_now",
        lambda: "2026-01-02T00:00:00Z",
    )

    with pytest.raises(
        ProviderSportMappingError,
        match="clock|chronology|dispatch|authority|record",
    ):
        registry.register_evidence(_evidence())
