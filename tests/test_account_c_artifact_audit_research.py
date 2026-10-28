import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_account_c_artifact import audit


def make_artifact(path, *, truth="simulated", alter=False, tickets=0, diagnostic_net=None, misroute=False):
    action_names = ("PARLAY_2_HIGH_ODDS", "PARLAY_2_LOW_ODDS", "SINGLE_HIGH_ODDS", "SINGLE_LOW_ODDS", "WAIT")
    estimates = [{"action_type": name, "observations": int(name == "WAIT"), "reward_sum": "0"} for name in action_names]
    if misroute:
        for item in estimates:
            item["observations"] = int(item["action_type"] == "SINGLE_HIGH_ODDS")
    summary = {"truth": truth, "paper_only": True, "real_money_execution": False,
               "forward_evidence_eligible": False, "promotion_authorized": False,
               "profitability_claim": False, "initial_bankroll": "10000", "final_balance": "10000",
               "policy_generation": 1, "counters": {"SINGLE": 0, "PARLAY_2": 0, "WAIT": 1,
                   "NETWORK_SNAPSHOTS": 2, "NETWORK_FAILURES": 0}}
    summary["estimates"] = {e["action_type"]: {"observations": e["observations"], "reward_sum": "0", "mean_reward": "0"} for e in estimates}
    files = {"endurance_summary.json": json.dumps(summary).encode(),
             "campaign_state.json": json.dumps({"summary": summary, "policy": {"estimates": estimates}}).encode(),
             "paper_book.json": json.dumps({"initial_bankroll": "10000", "balance": "10000",
                                             "tickets": [{}] * tickets}).encode()}
    if diagnostic_net is not None:
        files["paper_diagnostics.json"] = json.dumps({"truth": "simulated", "paper_only": True,
            "profitability_claim": False, "ticket_count": tickets, "initial_bankroll": "10000",
            "final_balance": "10000", "net_reward": diagnostic_net, "maximum_drawdown": "0",
            "gross_stake": "0"}).encode()
    manifest = "".join(hashlib.sha256(body).hexdigest() + "  account-c-paper-evidence/" + name + "\n"
                       for name, body in sorted(files.items()))
    if alter:
        files["paper_book.json"] += b" "
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
        archive.writestr("artifact-sha256.txt", manifest)


def test_simulated_result_is_checked_without_real_profit_claim(tmp_path):
    path = tmp_path / "artifact.zip"
    make_artifact(path)
    result = audit(path)
    assert result["manifest_files_verified"] == 3
    assert result["wait_count"] == 1 and result["simulated_change"] == "0"
    assert result["action_estimates"]["WAIT"]["observations"] == 1
    assert result["profitability_claim"] is False
    assert result["manifest_checks_internal_consistency_only"] is True


@pytest.mark.parametrize("options, message", [({"alter": True}, "digest"),
                                               ({"truth": "observed"}, "truth"),
                                               ({"tickets": 1}, "count mismatch"),
                                               ({"misroute": True}, "family counters")])
def test_corrupt_or_mislabelled_artifacts_are_rejected(tmp_path, options, message):
    path = tmp_path / "artifact.zip"
    make_artifact(path, **options)
    with pytest.raises(ValueError, match=message):
        audit(path)


def test_extra_unmanifested_member_and_parent_path_are_rejected(tmp_path):
    path = tmp_path / "artifact.zip"
    make_artifact(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("extra.txt", "unaccounted")
    with pytest.raises(ValueError, match="membership"):
        audit(path)
    make_artifact(path)
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("../escape.txt", "untrusted")
    with pytest.raises(ValueError, match="unsafe"):
        audit(path)


def test_diagnostic_net_must_match_balances(tmp_path):
    path = tmp_path / "artifact.zip"
    make_artifact(path, diagnostic_net="5")
    with pytest.raises(ValueError, match="diagnostic economics"):
        audit(path)
    make_artifact(path, diagnostic_net="0")
    assert audit(path)["simulated_diagnostics"]["net_over_stake"] is None
