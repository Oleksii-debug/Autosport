from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autosport.scientific_registry import ScientificRegistry
from autosport.storage import SQLiteMarketStore


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _legacy_dataset_entry(
    *,
    cutoff: str = "2026-01-01T00:00:00Z",
    envelope_available: str = "2026-01-01T00:00:01Z",
    payload_available: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "dataset_snapshot_id": "dataset-legacy-1",
        "manifest_sha256": "1" * 64,
        "source_identity": "source-a",
        "license_identity": "license-a",
        "causal_cutoff": cutoff,
        "outcome_reveal_after": None,
    }
    if payload_available is not None:
        payload["available_at"] = payload_available
    entry: dict[str, object] = {
        "record_type": "DatasetSnapshot",
        "record_id": "dataset-legacy-1",
        "available_at": envelope_available,
        "payload": payload,
        "record_sha256": "",
    }
    entry["record_sha256"] = _digest(
        {
            "record_type": entry["record_type"],
            "record_id": entry["record_id"],
            "available_at": entry["available_at"],
            "payload": entry["payload"],
        }
    )
    return entry


def test_legacy_dataset_snapshot_uses_hashed_envelope_availability() -> None:
    ScientificRegistry._validate_entry(_legacy_dataset_entry())


def test_legacy_dataset_snapshot_cannot_hide_future_cutoff() -> None:
    with pytest.raises(
        ValueError,
        match="DatasetSnapshot available_at must not precede causal_cutoff",
    ):
        ScientificRegistry._validate_entry(
            _legacy_dataset_entry(
                cutoff="2026-01-01T00:00:02Z",
                envelope_available="2026-01-01T00:00:01Z",
            )
        )


def test_explicit_dataset_payload_availability_must_match_envelope() -> None:
    with pytest.raises(
        ValueError,
        match="DatasetSnapshot payload/envelope availability mismatch",
    ):
        ScientificRegistry._validate_entry(
            _legacy_dataset_entry(
                envelope_available="2026-01-01T00:00:01Z",
                payload_available="2026-01-01T00:00:02Z",
            )
        )


def test_pristine_store_bootstrap_keeps_one_workspace_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "machine-authority"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    store = SQLiteMarketStore(workspace / "market.db")
    try:
        append_authority = store._market_append_authority()
        availability_authority = store._market_append_availability_authority()
        assert append_authority.workspace_instance_id == availability_authority.workspace_instance_id
        assert append_authority.authority_root_selection.validate_existing()
        assert availability_authority.authority_root_selection.validate_existing()
    finally:
        store.close()


def test_pristine_store_restart_preserves_workspace_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_root = tmp_path / "machine-authority"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv(
        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT",
        str(authority_root.resolve()),
    )

    first = SQLiteMarketStore(workspace / "market.db")
    try:
        first_identity = first._market_append_authority().workspace_instance_id
    finally:
        first.close()

    restarted = SQLiteMarketStore(workspace / "market.db")
    try:
        assert restarted._market_append_authority().workspace_instance_id == first_identity
        assert (
            restarted._market_append_availability_authority().workspace_instance_id
            == first_identity
        )
    finally:
        restarted.close()
