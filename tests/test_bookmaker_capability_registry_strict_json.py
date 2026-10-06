from pathlib import Path

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerCapabilityRegistryError,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


_TS = "2026-09-17T16:00:00+00:00"
_HASH = "c" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_TS,
        source_ref="capability-probe",
        source_payload_sha256=_HASH,
    )


def _governance() -> BookmakerGovernanceEvidence:
    return BookmakerGovernanceEvidence(
        venue_id="book-a",
        account_id="acct-a",
        jurisdiction="SK",
        terms_version="2026-09",
        automation_permission=GovernancePermissionState.UNKNOWN,
        observed_at=_TS,
        source_ref="terms-snapshot",
        source_payload_sha256=_HASH,
    )


def _assert_corrupt(path: Path) -> None:
    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="unreadable or corrupt",
    ):
        BookmakerCapabilityRegistry(path).profile_history(
            "book-a", "acct-a", "adapter-a"
        )


def test_registry_rejects_duplicate_top_level_key(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"profiles":[],"governance":[]}',
        encoding="utf-8",
    )
    _assert_corrupt(path)


def test_registry_rejects_duplicate_profile_entry_key(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    profile = _profile()
    assert registry.register_profile(profile)

    raw = path.read_text(encoding="utf-8")
    needle = f'"profile_id":"{profile.profile_id}"'
    assert needle in raw
    path.write_text(raw.replace(needle, f"{needle},{needle}", 1), encoding="utf-8")

    _assert_corrupt(path)


def test_registry_rejects_duplicate_governance_entry_key(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    evidence = _governance()
    assert registry.register_governance(evidence)

    raw = path.read_text(encoding="utf-8")
    needle = f'"evidence_id":"{evidence.evidence_id}"'
    assert needle in raw
    path.write_text(raw.replace(needle, f"{needle},{needle}", 1), encoding="utf-8")

    _assert_corrupt(path)


def test_registry_rejects_nonstandard_json_constant(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        '{"schema_version":1,"profiles":[],"governance":[],"extra":NaN}',
        encoding="utf-8",
    )
    _assert_corrupt(path)
