from __future__ import annotations

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import autosport.provider_sport_mapping as mapping


def test_register_evidence_rejects_rebound_evidence_parser_before_positive_binding(monkeypatch):
    raw = b"not-json-provider-evidence"

    def forged_parser(cls, source_snapshot_bytes: bytes):
        assert source_snapshot_bytes == raw
        return mapping.ProviderSportEvidence(
            provider_namespace="betfair",
            provider_sport_id="forged-provider-sport",
            canonical_sport="football",
            valid_from="2026-01-01T00:00:00Z",
            valid_until=None,
            evidence_available_at="2026-01-02T00:00:00Z",
            source_snapshot_sha256=hashlib.sha256(source_snapshot_bytes).hexdigest(),
        )

    monkeypatch.setattr(
        mapping.ProviderSportEvidence,
        "from_exact_bytes",
        classmethod(forged_parser),
    )

    with TemporaryDirectory() as tmp:
        registry = mapping.ProviderSportMappingRegistry.initialize_pristine(
            Path(tmp) / "sport-map.json"
        )
        with pytest.raises(mapping.ProviderSportMappingError, match="authority|canonical|evidence"):
            registry.register_evidence(raw)
        assert registry.bindings == ()
