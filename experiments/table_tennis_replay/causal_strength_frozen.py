"""Frozen, observational comparison of existing TT form and H2H heuristics.

No fitting, independent identity verification, source rights, execution, or
promotion is asserted. A digest is useful only if anchored independently
before outcomes are observed. Local receipt clocks are not trusted timestamps.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import math
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import ParticipantIdentityRegistry
from causal_h2h_observer import build_journal, prepare_prior, verify_journal
from causal_h2h_score import _eligible_rows
from causal_strength_h2h import compare
from delay_policy_audit import _exact_snapshot
from next_day_player_eval import _scores
from prospective_capture import _utc_now, _write_exclusive, verify_session
from record import instant

SCHEMA = "research.tt_strength_h2h_observation.v1"
PAPER_PROTOCOL = {"version": 1, "stake_units": "1", "cost_per_ticket": "0.01",
                  "winning_payout_haircut": "0.02", "min_edge_units": "0.02"}
POLICIES = ("market", "form", "form_h2h", "common_opponents")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _common_probability(row):
    shared = row.get("common_opponent_form")
    if shared is None or shared.get("status") != "descriptive":
        return None
    diff = Decimal(shared["home_minus_away_not_probability"])
    if not diff.is_finite() or not -1 < diff < 1:
        raise ValueError("invalid common-opponent signal")
    with localcontext() as ctx:
        ctx.prec = 28
        return str((1 + diff) / 2)


def _economic_decision(row, state):
    """Freeze hypothetical single-ticket choices using only decision-time quotes."""
    home, away, _, quotes = state
    odds = (quotes[home][1], quotes[away][1])
    data = [row["market_home_probability"], row["strength_home_probability"],
            row["strength_h2h_home_probability"], _common_probability(row)]
    with localcontext() as ctx:
        ctx.prec = 28
        cost = Decimal(PAPER_PROTOCOL["cost_per_ticket"])
        haircut = Decimal(PAPER_PROTOCOL["winning_payout_haircut"])
        floor = Decimal(PAPER_PROTOCOL["min_edge_units"])
        actions = {}
        for policy, raw in zip(POLICIES, data, strict=True):
            if raw is None:
                actions[policy] = None
                continue
            _probability(raw)
            p = Decimal(raw)
            edge = (p * odds[0] * (1 - haircut) - 1 - cost,
                    (1 - p) * odds[1] * (1 - haircut) - 1 - cost)
            actions[policy] = ("WAIT" if edge[0] == edge[1] or max(edge) < floor
                               else "HOME" if edge[0] > edge[1] else "AWAY")
    return {"displayed_odds": [str(o) for o in odds], "actions": actions}


def _economic_replay(population, outcomes, snapshot):
    """Compare each candidate to market/WAIT on its same supported fixtures."""
    settled = {row["fixture_id"]: row["outcome"] for row in outcomes}
    frozen = {}
    for row in population:
        identity = row["fixture_id"]
        state = snapshot.get(identity)
        if state is None:
            raise ValueError("missing decision-time quotes for economic replay")
        decision = _economic_decision(row, state)
        if row.get("paper_economic_decision") != decision:
            raise ValueError("frozen economic decision does not match source quotes and policy")
        frozen[identity] = decision

    def tally(ids, policy):
        chosen = resolved = wins = 0
        net = Decimal(0)
        with localcontext() as ctx:
            ctx.prec = 28
            loss = Decimal(1) + Decimal(PAPER_PROTOCOL["cost_per_ticket"])
            haircut = Decimal(PAPER_PROTOCOL["winning_payout_haircut"])
            for identity in ids:
                decision = frozen[identity]
                action = "WAIT" if policy == "wait" else decision["actions"][policy]
                if action not in ("HOME", "AWAY"):
                    continue
                chosen += 1
                if identity not in settled:
                    continue
                resolved += 1
                won = settled[identity] == int(action == "HOME")
                wins += int(won)
                payout = Decimal(decision["displayed_odds"][int(action == "AWAY")]) * (1 - haircut)
                net += payout - loss if won else -loss
            pending = chosen - resolved
            floor = net - pending * loss
        return {"selected": chosen, "settled": resolved, "unresolved": pending, "wins": wins,
                "settled_net_units": str(net), "if_all_unresolved_lose_units": str(floor)}

    matched = {}
    for policy in POLICIES[1:]:
        ids = [identity for identity, row in frozen.items() if row["actions"][policy] is not None]
        matched[policy] = {"supported_fixtures": len(ids),
                           "results": {name: tally(ids, name) for name in ("wait", "market", policy)}}
    return {"protocol": dict(PAPER_PROTOCOL), "matched": matched,
            "single_ticket_stress_only": True, "accepted_quotes_verified": False,
            "real_money_execution": False, "profitability_claim": False}


def build_strength_journal(session, prior, registry, store, *, feature_cutoff):
    """Build candidates from the same eligible quotes and strict causal view.

    The caller must persist and independently anchor the returned bytes before
    any later results are requested; this function alone proves no chronology.
    """
    journal = build_journal(session, prior, registry, store, feature_cutoff=feature_cutoff)
    verified = verify_session(session)
    if set(verified) != {(1, "odds")}:
        raise ValueError("freeze requires no current results")
    board = _exact_snapshot(session, 1, verified[(1, "odds")])
    for row in journal["forecasts"]:
        candidate = compare(row["fixture_id"], board[row["fixture_id"]],
                            feature_cutoff, registry, store)
        for baseline_key, candidate_key in (("market_home_probability", "market_home_probability"),
                                             ("h2h_status", "h2h_status"),
                                             ("prior_meetings", "h2h_support"),
                                             ("h2h_home_probability", "market_h2h_home_probability")):
            if row[baseline_key] != candidate[candidate_key]:
                raise ValueError("baseline changed during candidate construction")
        for key in ("strength_status", "home_form", "away_form", "common_opponent_form",
                    "strength_home_probability", "strength_h2h_home_probability"):
            row[key] = candidate[key]
        row["paper_economic_decision"] = _economic_decision(row, board[row["fixture_id"]])
    journal["schema"] = SCHEMA
    journal["paper_economic_protocol"] = dict(PAPER_PROTOCOL)
    journal["note"] = ("Fixed illustrative form/H2H heuristics on local source quotes; "
                       "not fitted, calibrated, independently verified, or executable.")
    return journal


def freeze(root, prior_session, prior_body, prior_receipt, identity_path, opponents_path,
           *, clock=_utc_now):
    """Create exclusive forecast files after an odds-only capture.

    Stores are loaded afresh and their hashes bound to the forecast. The caller
    must separately archive and externally time-anchor the forecast digest and
    input snapshots. This function does not request current results.
    """
    root = Path(root)
    session = root / "snapshots"
    paths = (Path(identity_path), Path(opponents_path))
    if any(not p.is_file() for p in paths):
        raise ValueError("identity and opponent stores must already exist")
    hashes = [_digest(p) for p in paths]
    registry = ParticipantIdentityRegistry(paths[0])
    store = OpponentIntelligenceStore(paths[1], registry)
    prior = prepare_prior(prior_session, Path(prior_body).read_bytes(),
                          strict_json_loads(Path(prior_receipt).read_text("utf-8")))
    computed = clock()
    journal = build_strength_journal(session, prior, registry, store,
                                     feature_cutoff=computed.isoformat())
    if [_digest(p) for p in paths] != hashes:
        raise ValueError("input stores changed during forecast")
    journal["identity_store_sha256"], journal["opponent_store_sha256"] = hashes
    payload = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_exclusive(root / "forecast.body.json", payload)
    frozen = clock()
    if not (computed <= frozen and
            0 < (frozen - instant(journal["odds_received_at"])).total_seconds() <= 90):
        raise ValueError("forecast freeze chronology or odds freshness invalid")
    if set(verify_session(session)) != {(1, "odds")}:
        raise ValueError("current results appeared during freeze")
    receipt = {"schema": "research.tt_forecast_receipt.v1",
               "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
               "computed_at": computed.isoformat(), "frozen_at": frozen.isoformat(),
               "odds_sha256": journal["odds_sha256"],
               "prior_result_sha256": journal["prior_result_sha256"]}
    receipt_payload = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _write_exclusive(root / "forecast.receipt.json", receipt_payload)
    return {**receipt, "receipt_sha256": hashlib.sha256(receipt_payload).hexdigest()}


def _probability(value):
    if not isinstance(value, str):
        raise ValueError("candidate probability must be a decimal string")
    try:
        p = Decimal(value)
        f = float(p)
        if not p.is_finite() or not math.isfinite(f) or not 0 < f < 1:
            raise ValueError("outside open probability interval")
    except (InvalidOperation, OverflowError, ValueError) as exc:
        raise ValueError("invalid candidate probability") from exc
    return f


def evaluate(root, raw, receipt, *, expected_forecast_sha256, expected_receipt_sha256):
    """Compare each candidate to market on the *same* eligible fixtures.

    Both digests must come from a separately anchored pre-outcome record.
    Merely passing digests of local files proves no freeze.
    """
    journal, envelope, verified = verify_journal(root)
    if (not isinstance(expected_receipt_sha256, str) or
            _digest(Path(root) / "forecast.receipt.json") != expected_receipt_sha256):
        raise ValueError("receipt does not match independently pinned digest")
    if journal.get("schema") != SCHEMA:
        raise ValueError("wrong candidate journal schema")
    if (not isinstance(expected_forecast_sha256, str) or
            expected_forecast_sha256 != envelope["sha256"]):
        raise ValueError("forecast does not match independently pinned digest")
    for key in ("identity_store_sha256", "opponent_store_sha256"):
        value = journal.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("missing input-store digest")
    forecasts = journal.get("forecasts")
    if not isinstance(forecasts, list):
        raise ValueError("invalid frozen forecast population")
    candidates = {}
    statuses = Counter()
    for row in forecasts:
        fixture = row.get("fixture_id")
        if not isinstance(fixture, str) or not fixture or fixture in candidates:
            raise ValueError("invalid or duplicate candidate fixture")
        status, h2h = row.get("strength_status"), row.get("h2h_status")
        if not isinstance(status, str) or not status or not isinstance(h2h, str) or not h2h:
            raise ValueError("missing candidate status")
        form, combined = row.get("strength_home_probability"), row.get("strength_h2h_home_probability")
        if (status == "ready") != (form is not None):
            raise ValueError("form status/probability mismatch")
        if (status == "ready" and h2h == "ready") != (combined is not None):
            raise ValueError("H2H status/probability mismatch")
        common = _common_probability(row)
        candidates[fixture] = {"form": _probability(form) if form is not None else None,
                               "form_h2h": _probability(combined) if combined is not None else None,
                               "common_opponents": _probability(common) if common is not None else None}
        statuses["form_" + status] += 1
        statuses["h2h_" + h2h] += 1
    counts, rows, followup, decision_bound = _eligible_rows(
        root, raw, receipt, journal, envelope, verified)
    economic = None
    if "paper_economic_protocol" in journal:
        if journal["paper_economic_protocol"] != PAPER_PROTOCOL:
            raise ValueError("unsupported frozen economic protocol")
        snapshot = _exact_snapshot(Path(root) / "snapshots", 1, verified[(1, "odds")])
        economic = _economic_replay(forecasts, rows, snapshot)
    elif any("paper_economic_decision" in row for row in forecasts):
        raise ValueError("economic decisions require a frozen protocol")
    for row in rows:
        row.update(candidates[row["fixture_id"]])
    form_rows = [r for r in rows if r["form"] is not None]
    h2h_rows = [r for r in rows if r["form_h2h"] is not None]
    common_rows = [r for r in rows if r["common_opponents"] is not None]
    def cohort(items, keys):
        return {"matches": len(items), "fixture_ids": [r["fixture_id"] for r in items],
                "scores": {key: _scores(items, key) for key in keys}}
    return {"schema": "research.tt_frozen_strength_score.v1",
            "forecast_sha256": envelope["sha256"], "frozen_candidates": len(forecasts),
            "candidate_status_counts": dict(sorted(statuses.items())),
            "outcome_counts": {key: counts[key] for key in ("eligible", "not_present_in_followup",
                "invalid_or_unfinished_result", "source_update_after_receipt",
                "missing_or_mismatched_final_score", "identity_time_or_correction_conflict",
                "invalid_probability")},
            "all_eligible_market": cohort(rows, ("market",)),
            "matched_form": cohort(form_rows, ("market", "form")),
            "matched_h2h": cohort(h2h_rows, ("market", "form", "form_h2h")),
            "matched_common_opponents": cohort(common_rows, ("market", "common_opponents")),
            "hypothetical_single_ticket_economics": economic,
            "rows": rows, "followup_audit": followup, "decision_bound": decision_bound.isoformat(),
            "source_results_independently_verified": False, "identity_verified": False,
            "rights_verified": False, "independent_holdout": False, "calibration_verified": False,
            "execution_verified": False, "profitability_claim": False, "model_training_authorized": False,
            "note": "Descriptive matched cohorts; no uncertainty or causal effect is established."}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("freeze", help="freeze existing odds-only capture")
    capture.add_argument("root", type=Path)
    capture.add_argument("prior_session", type=Path)
    capture.add_argument("prior_body", type=Path)
    capture.add_argument("prior_receipt", type=Path)
    capture.add_argument("identity_store", type=Path)
    capture.add_argument("opponent_store", type=Path)
    score = commands.add_parser("score", help="score after later results are recorded")
    score.add_argument("root", type=Path)
    score.add_argument("followup_body", type=Path)
    score.add_argument("followup_receipt", type=Path)
    score.add_argument("output", type=Path)
    score.add_argument("--expected-forecast-sha256", required=True)
    score.add_argument("--expected-receipt-sha256", required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze(args.root, args.prior_session, args.prior_body, args.prior_receipt,
                        args.identity_store, args.opponent_store)
    else:
        result = evaluate(args.root, args.followup_body.read_bytes(),
                          strict_json_loads(args.followup_receipt.read_text("utf-8")),
                          expected_forecast_sha256=args.expected_forecast_sha256,
                          expected_receipt_sha256=args.expected_receipt_sha256)
        _write_exclusive(args.output, (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
