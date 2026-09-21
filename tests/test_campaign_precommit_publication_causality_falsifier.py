from __future__ import annotations

from pathlib import Path

import pytest

from autosport.campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    CampaignPrecommitManifestError,
    write_campaign_precommit_manifest_once,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64


def _manifest(
    *,
    campaign_id: str,
    committed_at: str,
    observation_not_before: str,
    observation_not_after: str,
) -> CampaignPrecommitManifest:
    return CampaignPrecommitManifest(
        campaign_id=campaign_id,
        source_id="betfair:exchange",
        source_snapshot_sha256=A,
        committed_at=committed_at,
        observation_not_before=observation_not_before,
        observation_not_after=observation_not_after,
        evaluation_universe_sha256=B,
        strategy_version_id="strategy-v17",
        champion_version_id="model-v42",
        baseline_version_id="market-baseline-v3",
        cost_contract_sha256=C,
        multiplicity_policy_sha256=D,
        stopping_policy_sha256=E,
        restart_policy_sha256=F,
        causal_evidence_policy_sha256=ZERO,
        config_sha256=ONE,
    )


def test_past_observation_start_cannot_be_minted_as_prospective_precommit(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    # Non-vacuity control: the same publication surface accepts an actually
    # prospective manifest on this platform.
    future = _manifest(
        campaign_id="paper-forward-control",
        committed_at="2099-12-31T00:00:00Z",
        observation_not_before="2100-01-01T00:00:00Z",
        observation_not_after="2100-01-02T00:00:00Z",
    )
    control_path = evidence_dir / "control.json"
    assert (
        write_campaign_precommit_manifest_once(control_path, future)
        == future.manifest_sha256
    )
    assert control_path.exists()

    # A caller-provided committed_at timestamp is not causal publication proof.
    # In 2026 this observation window started long ago, while the record claims
    # a 1999 commitment. Publishing it now must fail closed rather than creating
    # a new artifact that can be mistaken for prospective evidence.
    backdated = _manifest(
        campaign_id="paper-forward-backdated",
        committed_at="1999-12-31T23:59:59Z",
        observation_not_before="2000-01-01T00:00:00Z",
        observation_not_after="2100-01-01T00:00:00Z",
    )
    target = evidence_dir / "backdated.json"

    with pytest.raises(CampaignPrecommitManifestError):
        write_campaign_precommit_manifest_once(target, backdated)

    assert not target.exists()
