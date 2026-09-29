from __future__ import annotations

from pathlib import Path

import pytest

import autosport.campaign_precommit_manifest as precommit_module
from autosport.campaign_precommit_manifest import (
    CampaignPrecommitManifest,
    publish_campaign_precommit_manifest,
)


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ZERO = "0" * 64
ONE = "1" * 64


def _manifest() -> CampaignPrecommitManifest:
    return CampaignPrecommitManifest(
        campaign_id="paper-forward-dispatch-seal",
        source_id="betfair:exchange",
        source_snapshot_sha256=A,
        committed_at="2099-12-31T19:00:00Z",
        observation_not_before="2100-01-01T06:00:00Z",
        observation_not_after="2100-01-08T06:00:00Z",
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


def test_installed_publisher_fails_closed_before_rebound_publication_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module rebinding must be rejected before the hostile target can dispatch."""

    workspace = tmp_path / "workspace"
    evidence = workspace / "evidence"
    evidence.mkdir(parents=True)
    target = evidence / "precommit.json"
    authority_root = tmp_path / "machine-authority"
    original_authority_factory = precommit_module._publication_authority
    hostile_called = False

    def hostile_publication_authority(*args: object, **kwargs: object) -> object:
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("module-global publication authority dispatch intercepted")

    monkeypatch.setattr(
        precommit_module,
        "_publication_authority",
        hostile_publication_authority,
    )

    with pytest.raises(RuntimeError, match="global dispatch authority changed"):
        publish_campaign_precommit_manifest(
            target,
            _manifest(),
            workspace=workspace,
            authority_root=authority_root,
        )

    assert hostile_called is False
    assert not target.exists()
    assert original_authority_factory is not hostile_publication_authority
