from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import autosport.governance_currentness as currentness
from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


def _registry_with_fresh_permitted_evidence(tmp_path: Path) -> BookmakerCapabilityRegistry:
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


def test_product_clock_helper_rebinding_cannot_mint_current_governance(monkeypatch, tmp_path: Path) -> None:
    registry = _registry_with_fresh_permitted_evidence(tmp_path)
    forged_called = False

    def forged_product_time_ns() -> int:
        nonlocal forged_called
        forged_called = True
        return 1

    monkeypatch.setattr(currentness, "_product_time_ns", forged_product_time_ns)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="product clock dispatch authority changed",
    ):
        _resolve(registry)

    assert not forged_called


def test_registry_history_class_rebinding_cannot_supply_positive_evidence(monkeypatch, tmp_path: Path) -> None:
    registry = _registry_with_fresh_permitted_evidence(tmp_path)
    hostile_called = False

    def hostile_history(self, venue_id: str, account_id: str):
        nonlocal hostile_called
        hostile_called = True
        return ()

    monkeypatch.setattr(BookmakerCapabilityRegistry, "governance_history", hostile_history)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance history dispatch authority changed",
    ):
        _resolve(registry)

    assert not hostile_called


def test_instance_shadow_cannot_redirect_registry_history(tmp_path: Path) -> None:
    registry = _registry_with_fresh_permitted_evidence(tmp_path)
    hostile_called = False

    def hostile_history(venue_id: str, account_id: str):
        nonlocal hostile_called
        hostile_called = True
        return ()

    registry.governance_history = hostile_history  # type: ignore[method-assign]

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance history dispatch authority changed",
    ):
        _resolve(registry)

    assert not hostile_called
