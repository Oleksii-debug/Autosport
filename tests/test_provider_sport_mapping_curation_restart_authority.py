from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def _digest(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_legacy_registry(
    path: Path,
    *,
    provider_sport_id: str,
    canonical_sport: str,
) -> None:
    binding = mapping.ProviderSportBinding(
        provider_namespace="betfair",
        provider_sport_id=provider_sport_id,
        canonical_sport=canonical_sport,
        valid_from="2026-01-01T00:00:00Z",
        valid_until=None,
        evidence_available_at="2026-01-02T00:00:00Z",
        source_snapshot_sha256="a" * 64,
        recorded_at="2026-01-02T00:00:01Z",
    )
    unsigned = {
        "schema": "autosport.provider_sport_mapping",
        "schema_version": 2,
        "bindings": [binding.payload()],
    }
    payload = {**unsigned, "registry_sha256": _digest(unsigned)}
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def test_restart_does_not_grandfather_legacy_caller_authored_mapping() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        _write_legacy_registry(
            path,
            provider_sport_id="caller-chosen-id",
            canonical_sport="football",
        )

        registry = mapping.ProviderSportMappingRegistry(path)
        assert len(registry.bindings) == 1

        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="curation|authority|origin|provenance",
        ):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="caller-chosen-id",
                as_of="2026-01-03T00:00:00Z",
            )


def test_restart_preserves_source_reviewed_curated_mapping() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        _write_legacy_registry(
            path,
            provider_sport_id="1",
            canonical_sport="football",
        )

        registry = mapping.ProviderSportMappingRegistry(path)
        resolution = registry.resolve(
            provider_namespace="betfair",
            provider_sport_id="1",
            as_of="2026-01-03T00:00:00Z",
        )

        assert resolution.provider_sport_id == "1"
        assert resolution.canonical_sport == "football"
