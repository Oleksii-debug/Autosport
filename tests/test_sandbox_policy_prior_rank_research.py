import json
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare_sandbox_policy_prior import PriorRankCampaign, later_snapshots, load_prior
from autosport.paper_live_sandbox_campaign import PARLAY_ACTIONS
from autosport.transparent_bandit_policy import ActionEstimate
from test_paper_live_sandbox_campaign import _plan


def test_tampered_prior_is_rejected(tmp_path):
    prior = load_prior()
    prior["truth"] = "real"
    path = tmp_path / "prior.json"
    path.write_text(json.dumps(prior), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_prior(path)


def test_prior_cannot_override_wait_or_admit_a_negative_action():
    campaign = PriorRankCampaign(_plan(), prior=load_prior())
    def estimates(high, low):
        return tuple(ActionEstimate(e.action_type, 8, Decimal(high if e.action_type.endswith("HIGH_ODDS") else low))
                     if e.action_type in PARLAY_ACTIONS else e for e in campaign.policy.estimates)
    campaign.policy = replace(campaign.policy, estimates=estimates("-1", "-1"))
    assert campaign._explore_then_admit_family(PARLAY_ACTIONS) == frozenset({"WAIT"})
    campaign.policy = replace(campaign.policy, estimates=estimates("-1", "1"))
    assert campaign._explore_then_admit_family(PARLAY_ACTIONS) == frozenset({"PARLAY_2_LOW_ODDS"})


@pytest.mark.parametrize("scenario", ["loss", "prior_adverse"])
def test_snapshots_begin_after_source_artifact_and_remain_ordered(scenario):
    data = later_snapshots(scenario)
    assert data[0].observed_at > "2026-10-28T04:42:57"
    assert all(a.observed_at < b.observed_at for a, b in zip(data, data[1:]))
    assert len({s.snapshot_sha256 for s in data}) == len(data)
