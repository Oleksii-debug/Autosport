"""Update preflight result objects must never be constructible as install grants."""

from dataclasses import replace
from pathlib import Path

import pytest

from autosport.windows_update_admission import WindowsUpdateDecision
from autosport.windows_update_signature import VerifiedWindowsUpdate
from autosport.windows_update_snapshot import WindowsUpdateSnapshot


def _decision() -> WindowsUpdateDecision:
    return WindowsUpdateDecision(
        "ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.1.0", 1, 1
    )


@pytest.mark.parametrize("flag", [True, 1, "true", None])
def test_update_decision_cannot_claim_install_authority(flag):
    with pytest.raises(ValueError):
        replace(_decision(), install_authorized=flag)


@pytest.mark.parametrize("flag", [True, 1, "true", None])
def test_signed_preflight_cannot_claim_install_authority(flag):
    with pytest.raises(ValueError):
        VerifiedWindowsUpdate(_decision(), "a" * 64, "b" * 40, "1.1.0", 1, flag)


@pytest.mark.parametrize("flag", [True, 1, "true", None])
def test_snapshot_cannot_claim_install_authority(flag):
    with pytest.raises(ValueError):
        WindowsUpdateSnapshot(Path("candidate.zip"), "a" * 64, 123, flag)


def test_normal_preflight_records_are_never_install_grants():
    decision = _decision()
    signed = VerifiedWindowsUpdate(decision, "a" * 64, "b" * 40, "1.1.0", 1)
    snapshot = WindowsUpdateSnapshot(Path("candidate.zip"), "a" * 64, 123)
    assert decision.install_authorized is False
    assert signed.install_authorized is False
    assert snapshot.install_authorized is False


def test_mutated_snapshot_authority_is_rejected_before_filesystem_access():
    from autosport.windows_update_snapshot import WindowsUpdateSnapshotError

    snapshot = WindowsUpdateSnapshot(Path("nonexistent.zip"), "a" * 64, 123)
    object.__setattr__(snapshot, "install_authorized", True)
    with pytest.raises(WindowsUpdateSnapshotError, match="cannot authorize installation"):
        snapshot.recheck()
