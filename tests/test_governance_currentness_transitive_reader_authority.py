from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autosport.bookmaker_capability_registry as registry_module
import autosport.governance_currentness as currentness
from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


def _registry(tmp_path: Path) -> BookmakerCapabilityRegistry:
    registry = BookmakerCapabilityRegistry(tmp_path / "bookmaker-capability.json")
    observed_at = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    registry.register_governance(
        BookmakerGovernanceEvidence(
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction="SK",
            terms_version="terms-v1",
            automation_permission=GovernancePermissionState.PERMITTED,
            observed_at=observed_at,
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


@pytest.mark.parametrize(
    "method_name",
    (
        "_load_document",
        "_governance_from_document",
        "_governance_key",
        "_decode_governance",
    ),
)
def test_transitive_registry_reader_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path: Path,
    method_name: str,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile(*args, **kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile registry reader executed")

    monkeypatch.setattr(BookmakerCapabilityRegistry, method_name, hostile)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dispatch authority changed",
    ):
        _resolve(registry)

    assert not hostile_called


def test_instance_shadow_of_transitive_reader_fails_before_dispatch(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile(*args, **kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile instance reader executed")

    registry._load_document = hostile  # type: ignore[method-assign]

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dispatch authority changed",
    ):
        _resolve(registry)

    assert not hostile_called


def test_strict_json_loader_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile(raw: str):
        nonlocal hostile_called
        hostile_called = True
        return {"schema_version": 1, "profiles": [], "governance": []}

    monkeypatch.setattr(registry_module, "strict_json_loads", hostile)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dependency authority changed",
    ):
        _resolve(registry)

    assert not hostile_called
