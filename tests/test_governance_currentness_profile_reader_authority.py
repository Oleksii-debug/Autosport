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
    "surface",
    ("_profiles_from_document", "_decode_profile", "_profile_key"),
)
def test_profile_reader_class_rebinding_fails_before_hostile_dispatch(
    monkeypatch,
    tmp_path: Path,
    surface: str,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile(*args, **kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError(f"hostile profile reader executed: {surface}")

    monkeypatch.setattr(BookmakerCapabilityRegistry, surface, hostile)

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dispatch authority changed",
    ):
        _resolve(registry)

    assert not hostile_called


def test_profile_reader_instance_shadow_fails_before_hostile_dispatch(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    hostile_called = False

    def hostile(*args, **kwargs):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile instance profile reader executed")

    registry._profiles_from_document = hostile  # type: ignore[method-assign]

    with pytest.raises(
        currentness.GovernanceCurrentnessError,
        match="governance registry reader dispatch authority changed",
    ):
        _resolve(registry)

    assert not hostile_called
