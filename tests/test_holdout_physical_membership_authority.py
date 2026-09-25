from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import autosport._holdout_physical_content_guard as guard
from autosport.scientific_registry import ScientificRegistry


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_raw_registry_membership_cannot_mint_holdout_disjointness(tmp_path: Path) -> None:
    """A recomputed registry envelope hash is not typed membership authority."""

    payload = {
        "dataset_snapshot_id": "snapshot-a",
        "manifest_sha256": "a" * 64,
        "source_identity": "source-a",
        "license_identity": "license-a",
        "causal_cutoff": "2026-01-01T00:00:00+00:00",
        "available_at": "2026-01-02T00:00:00+00:00",
        "outcome_reveal_after": None,
        # Current canonical DatasetSnapshot has no such typed field.  ScientificRegistry
        # envelope validation alone nevertheless accepts hash-consistent extra payload
        # data, so the physical-holdout guard must not treat it as positive authority.
        "observation_membership_sha256s": ["b" * 64],
    }
    envelope = {
        "record_type": "DatasetSnapshot",
        "record_id": "snapshot-a",
        "available_at": payload["available_at"],
        "payload": payload,
    }
    envelope["record_sha256"] = _digest(envelope)

    path = tmp_path / "scientific_registry.json"
    path.write_text(
        json.dumps(
            {"schema_version": ScientificRegistry.SCHEMA_VERSION, "records": [envelope]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
        encoding="utf-8",
    )

    registry = ScientificRegistry(path)
    with pytest.raises(
        ValueError,
        match="canonical DatasetSnapshot schema does not type observation membership authority",
    ):
        guard._snapshot_physical_identity(registry, "snapshot-a")
