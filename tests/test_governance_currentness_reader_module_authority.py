from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.governance_currentness as currentness
from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


def _registry(tmp_path: Path) -> BookmakerCapabilityRegistry:
    registry = BookmakerCapabilityRegistry(tmp_path / "registry.json")
    registry.register_governance(
        BookmakerGovernanceEvidence(
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction="SK",
            terms_version="terms-v1",
            automation_permission=GovernancePermissionState.PERMITTED,
            observed_at=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
            source_ref="https://provider.example/terms-v1",
            source_payload_sha256="a" * 64,
        )
    )
    return registry


def _resolve(registry: BookmakerCapabilityRegistry):
    return currentness.resolve_current_governance(
        registry,
        venue_id="provider-x",
        account_id="account-1",
        jurisdiction="SK",
        max_age_seconds=600,
    )


def test_registry_module_alias_substitution_fails_closed(monkeypatch, tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    facade = SimpleNamespace(
        strict_json_loads=currentness._CANONICAL_STRICT_JSON_LOADS,
        BookmakerGovernanceEvidence=BookmakerGovernanceEvidence,
        GovernancePermissionState=GovernancePermissionState,
    )
    monkeypatch.setattr(currentness, "_registry_module", facade)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dependency authority changed",
    ):
        _resolve(registry)


def test_registry_schema_substitution_fails_closed(monkeypatch, tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    monkeypatch.setattr(BookmakerCapabilityRegistry, "SCHEMA_VERSION", 2)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dependency authority changed",
    ):
        _resolve(registry)
