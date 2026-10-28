"""Read-only integrity and simulated-result check for a completed CI artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from decimal import Decimal
from pathlib import Path, PurePosixPath
from autosport.json_integrity import strict_json_loads

MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 150 * 1024 * 1024
PREFIX = "account-c-paper-evidence/"


def audit(path: Path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [x.filename for x in entries]
        if len(names) != len(set(names)) or sum(x.file_size for x in entries) > MAX_TOTAL:
            raise ValueError("duplicate entries or oversized artifact")
        for entry in entries:
            parts = PurePosixPath(entry.filename).parts
            if (not parts or entry.filename.startswith("/") or ".." in parts or "\\" in entry.filename
                or entry.is_dir() or entry.flag_bits & 1 or entry.file_size > MAX_FILE
                or ((entry.external_attr >> 16) & 0o170000) == 0o120000):
                raise ValueError("unsafe archive entry")
        if "artifact-sha256.txt" not in names:
            raise ValueError("missing artifact manifest")
        manifest = archive.read("artifact-sha256.txt").decode("utf-8").splitlines()
        expected = {}
        for line in manifest:
            match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
            if not match or not match.group(2).startswith(PREFIX):
                raise ValueError("invalid manifest line")
            name = match.group(2)[len(PREFIX):]
            if name in expected:
                raise ValueError("duplicate manifest name")
            expected[name] = match.group(1)
        if set(expected) != set(names) - {"artifact-sha256.txt"}:
            raise ValueError("manifest membership mismatch")
        for name, digest in expected.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("artifact digest mismatch")
        summary = strict_json_loads(archive.read("endurance_summary.json").decode("utf-8"))
        state = strict_json_loads(archive.read("campaign_state.json").decode("utf-8"))
        book = strict_json_loads(archive.read("paper_book.json").decode("utf-8"))
        if (summary.get("truth") != "simulated" or summary.get("paper_only") is not True
            or summary.get("real_money_execution") is not False
            or summary.get("forward_evidence_eligible") is not False
            or summary.get("promotion_authorized") is not False
            or summary.get("profitability_claim") is not False):
            raise ValueError("simulation truth boundary mismatch")
        state_summary = state.get("summary")
        if type(state_summary) is not dict or any(summary.get(k) != v for k, v in state_summary.items()):
            raise ValueError("summary and checkpoint mismatch")
        if book.get("balance") != summary.get("final_balance") or book.get("initial_bankroll") != summary.get("initial_bankroll"):
            raise ValueError("book and summary balance mismatch")
        counters = summary["counters"]
        tickets = book["tickets"]
        if type(counters) is not dict or type(tickets) is not list:
            raise ValueError("invalid counters or tickets")
        if any(type(counters.get(k)) is not int or counters[k] < 0 for k in
               ("SINGLE", "PARLAY_2", "WAIT", "NETWORK_SNAPSHOTS", "NETWORK_FAILURES")):
            raise ValueError("invalid action counters")
        if len(tickets) != counters["SINGLE"] + counters["PARLAY_2"]:
            raise ValueError("ticket and action count mismatch")
        if type(summary.get("policy_generation")) is not int or summary["policy_generation"] != (
            counters["SINGLE"] + counters["PARLAY_2"] + counters["WAIT"]
        ):
            raise ValueError("policy generation and action count mismatch")
        if type(summary.get("initial_bankroll")) is not str or type(summary.get("final_balance")) is not str:
            raise ValueError("balance must be canonical decimal text")
        initial, final = Decimal(summary["initial_bankroll"]), Decimal(summary["final_balance"])
        if not initial.is_finite() or not final.is_finite() or str(initial) != summary["initial_bankroll"] or str(final) != summary["final_balance"]:
            raise ValueError("non-finite balance")
        policy = state.get("policy")
        estimates = policy.get("estimates") if type(policy) is dict else None
        displayed = summary.get("estimates")
        actions = {"PARLAY_2_HIGH_ODDS", "PARLAY_2_LOW_ODDS", "SINGLE_HIGH_ODDS", "SINGLE_LOW_ODDS", "WAIT"}
        if type(estimates) is not list or type(displayed) is not dict or set(displayed) != actions:
            raise ValueError("missing action estimates")
        by_action = {}
        for item in estimates:
            if type(item) is not dict or set(item) != {"action_type", "observations", "reward_sum"}:
                raise ValueError("invalid action estimate")
            name, count, reward = item["action_type"], item["observations"], item["reward_sum"]
            if name not in actions or name in by_action or type(count) is not int or count < 0 or type(reward) is not str:
                raise ValueError("invalid action identity or reward")
            value = Decimal(reward)
            if not value.is_finite() or str(value) != reward or type(displayed[name]) is not dict:
                raise ValueError("invalid action reward")
            if displayed[name].get("observations") != count or displayed[name].get("reward_sum") != reward:
                raise ValueError("action estimate and summary mismatch")
            by_action[name] = {"observations": count, "simulated_reward": reward}
        if set(by_action) != actions or sum(v["observations"] for v in by_action.values()) != summary["policy_generation"]:
            raise ValueError("policy estimate counts mismatch")
        if (by_action["WAIT"]["observations"] != counters["WAIT"]
            or Decimal(by_action["WAIT"]["simulated_reward"]) != 0
            or sum(by_action[n]["observations"] for n in actions if n.startswith("SINGLE_")) != counters["SINGLE"]
            or sum(by_action[n]["observations"] for n in actions if n.startswith("PARLAY_")) != counters["PARLAY_2"]):
            raise ValueError("policy and action-family counters mismatch")
        if sum((Decimal(v["simulated_reward"]) for v in by_action.values()), Decimal("0")) != final-initial:
            raise ValueError("policy reward and balance mismatch")
        diagnostic_summary = None
        if "paper_diagnostics.json" in expected:
            diagnostics = strict_json_loads(archive.read("paper_diagnostics.json").decode("utf-8"))
            if (type(diagnostics) is not dict or diagnostics.get("truth") != "simulated"
                or diagnostics.get("paper_only") is not True or diagnostics.get("profitability_claim") is not False
                or diagnostics.get("ticket_count") != len(tickets)
                or diagnostics.get("initial_bankroll") != summary["initial_bankroll"]
                or diagnostics.get("final_balance") != summary["final_balance"]):
                raise ValueError("diagnostic and campaign mismatch")
            net = Decimal(diagnostics["net_reward"])
            drawdown = Decimal(diagnostics["maximum_drawdown"])
            stake = Decimal(diagnostics["gross_stake"])
            if not all(x.is_finite() for x in (net, drawdown, stake)) or net != final-initial or drawdown < 0 or stake < 0:
                raise ValueError("invalid diagnostic economics")
            diagnostic_summary = {"maximum_drawdown": str(drawdown), "gross_stake": str(stake),
                                  "net_over_stake": None if stake == 0 else str(net / stake)}
        return {"artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "manifest_files_verified": len(expected), "truth": "simulated",
                "initial_balance": str(initial), "final_balance": str(final),
                "simulated_change": str(final-initial), "ticket_count": len(tickets),
                "wait_count": counters["WAIT"], "policy_generation": summary["policy_generation"],
                "network_snapshots": counters.get("NETWORK_SNAPSHOTS"),
                "network_failures": counters.get("NETWORK_FAILURES"),
                "profitability_claim": False, "independent_provider_truth_verified": False,
                "manifest_checks_internal_consistency_only": True,
                "simulated_diagnostics": diagnostic_summary, "action_estimates": by_action}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    print(json.dumps(audit(parser.parse_args().artifact), indent=2, sort_keys=True))
