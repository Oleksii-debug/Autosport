from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def test_pristine_durable_registry_does_not_make_caller_json_positive_origin() -> None:
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "sport-map.json"
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(path)
        raw = (
            b'{"canonical_sport":"football","evidence_available_at":"2026-01-02T00:00:00Z",'
            b'"provider_namespace":"betfair","provider_sport_id":"caller-chosen-id",'
            b'"schema":"autosport.provider_sport_mapping_evidence","schema_version":1,'
            b'"valid_from":"2026-01-01T00:00:00Z","valid_until":null}'
        )

        # Exact bytes and a durable container prove integrity/durability only. They do
        # not prove that the provider or a product-owned curation authority asserted
        # the caller-chosen mapping.
        with pytest.raises(
            mapping.ProviderSportMappingError,
            match="origin|curation|authority|provenance",
        ):
            registry.register_evidence(raw)

        assert registry.bindings == ()
        with pytest.raises(mapping.ProviderSportMappingError):
            registry.resolve(
                provider_namespace="betfair",
                provider_sport_id="caller-chosen-id",
                as_of="2026-01-03T00:00:00Z",
            )
