"""Research experiment only; no product policy or profitability assertion."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare_account_c_recovery_probe import START
from compare_account_c_shadow_recovery import ShadowRecovery, ShadowRecoveryCapped, snapshots
from evaluate_account_c_shadow_random_regimes import snapshots as random_snapshots
from autosport.paper_live_sandbox_campaign import BlindPaperCampaignPlan


def test_shadow_history_appears_only_after_wait_outcome_without_ticket():
    plan = BlindPaperCampaignPlan(campaign_id="shadow-test", started_at=START.isoformat(),
                                 cutoff_at=(START + timedelta(hours=1)).isoformat())
    campaign = ShadowRecovery(plan, seed=11)
    data = snapshots("loss")
    saw_shadow = False
    for decision, outcome in zip(data, data[1:]):
        before_wait, before_tickets = campaign.counters["WAIT"], len(campaign.book.tickets)
        before_history = sum(len(values) for values in campaign.shadow.values())
        campaign._admissible_actions(decision)
        assert sum(len(values) for values in campaign.shadow.values()) == before_history
        campaign.step(decision, outcome)
        if campaign.counters["WAIT"] > before_wait:
            assert len(campaign.book.tickets) == before_tickets
            assert sum(len(values) for values in campaign.shadow.values()) >= before_history
            saw_shadow = True
    assert saw_shadow
    assert campaign.shadow_admissions == 0


def test_capped_variant_never_exceeds_four_tickets_after_readmission():
    plan = BlindPaperCampaignPlan(campaign_id="shadow-cap", started_at=START.isoformat(),
                                 cutoff_at=(START + timedelta(hours=1)).isoformat())
    campaign = ShadowRecoveryCapped(plan, seed=11)
    data = snapshots("recovery")
    for decision, outcome in zip(data, data[1:]):
        campaign.step(decision, outcome)
        assert campaign.tickets_after_readmission <= 4
    assert campaign.shadow_admissions > 0
    assert campaign.tickets_after_readmission == 4


def test_random_regimes_are_reproducible_and_timestamp_ordered():
    first, replay = random_snapshots(7), random_snapshots(7)
    other = random_snapshots(8)
    assert [s.snapshot_sha256 for s in first] == [s.snapshot_sha256 for s in replay]
    assert [s.snapshot_sha256 for s in first] != [s.snapshot_sha256 for s in other]
    assert all(left.available_at < right.observed_at for left, right in zip(first, first[1:]))
