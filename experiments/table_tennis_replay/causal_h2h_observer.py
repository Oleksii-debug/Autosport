"""Freeze source-price and identity-gated H2H observations before next result read.

Research-only, no bets, training, promotion, or product-store mutation. Public
source prices and locally timed receipts do not establish rights or execution.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from autosport.opponent_intelligence import (
    InvalidationReason, InvalidationTarget, OpponentIntelligenceStore, RecomputeStatus,
)
from autosport.participant_identity import (
    EntityKind, IdentityView, ParticipantIdentityError, ParticipantIdentityRegistry,
)
from dated_result_audit import audit as audit_dated_results
from delay_policy_audit import _exact_snapshot
from identity_coverage_audit import _observed_labels
from prospective_capture import _utc_now, _write_exclusive, capture_one, verify_session
from record import instant

SOURCE = "moviodds.com"


def feature(fixture_id, market, feature_cutoff, registry, store):
    """Construct an observational probability and canonical as-known H2H view."""
    if store.identity_registry is not registry:
        raise ValueError("opponent store and identity registry must share authority")
    cutoff = instant(feature_cutoff)
    # Existing stores use <= for as-of; issue #2591 requires strict-before.
    before = (cutoff - timedelta(microseconds=1)).isoformat()
    home, away, league, quotes = market
    with localcontext() as ctx:
        ctx.prec = 28
        market_home = quotes[away][1] / (quotes[home][1] + quotes[away][1])
    result = {"fixture_id": fixture_id, "market_home_probability": str(market_home),
              "h2h_status": "identity_unresolved", "prior_meetings": None,
              "prior_home_wins": None, "h2h_home_probability": None}
    try:
        alias_records = [registry.resolve_alias_record(SOURCE, label, as_of=before,
                         view=IdentityView.AS_KNOWN_AT_DECISION) for label in (home, away, league)]
        home_id, away_id, league_id = [registry.resolve_alias(SOURCE, label, as_of=before,
                                       view=IdentityView.AS_KNOWN_AT_DECISION)
                                       for label in (home, away, league)]
    except ParticipantIdentityError:
        return result
    if any(record.relation != "CONFIRMED" for record in alias_records):
        result["h2h_status"] = "identity_not_confirmed"
        return result
    # A later alias correction needs explicit review/reconstruction before its
    # current identity can safely reuse historical opponent evidence.
    if any(record.supersedes_record_id is not None for record in alias_records):
        result["h2h_status"] = "identity_revision_unreconciled"
        return result
    if (home_id.kind is not EntityKind.PARTICIPANT or away_id.kind is not EntityKind.PARTICIPANT or
            league_id.kind is not EntityKind.LEAGUE or home_id.entity_id == away_id.entity_id):
        result["h2h_status"] = "identity_incompatible"
        return result
    if any(registry.lineage_at(entity.entity_id, as_of=before, view=IdentityView.AS_KNOWN_AT_DECISION)
           for entity in (home_id, away_id, league_id)):
        result["h2h_status"] = "identity_lineage_unreconciled"
        return result
    edges = store.graph_edges(as_of=before, view=IdentityView.AS_KNOWN_AT_DECISION,
                              sport_id="table_tennis", league_entity_id=league_id.entity_id,
                              market_context_id="match-outcome")
    relevant = [edge for edge in edges if edge.event_id != fixture_id and
                {edge.subject_entity_id, edge.opponent_entity_id} == {home_id.entity_id, away_id.entity_id}]
    relevant_ids = {edge.performance_id for edge in relevant}
    if any(item.target_id in relevant_ids and item.target_kind is InvalidationTarget.OPPONENT_EDGE and
           item.reason is InvalidationReason.IDENTITY_CORRECTION and
           item.recompute_status is RecomputeStatus.REQUIRED and instant(item.detected_at) < cutoff
           for item in store.invalidations()):
        result["h2h_status"] = "history_identity_invalidated"
        return result
    ids = [edge.event_id for edge in relevant]
    if len(ids) != len(set(ids)) or any(edge.score not in ("0", "1") for edge in relevant):
        result["h2h_status"] = "history_ambiguous"
        return result
    wins = sum((edge.score == "1") if edge.subject_entity_id == home_id.entity_id
               else (edge.score == "0") for edge in relevant)
    n = len(relevant)
    result.update(prior_meetings=n, prior_home_wins=wins,
                  h2h_status="ready" if n else "no_prior_history")
    if n:
        with localcontext() as ctx:
            ctx.prec = 28
            # Fixed illustrative five-observation market anchor from existing
            # h2h_exploratory.py; it is not a trained or optimized model.
            result["h2h_home_probability"] = str((Decimal(wins) + 5 * market_home) / (n + 5))
    return result


def prepare_prior(prior_session, prior_body, prior_receipt):
    """Verify both the prior session and later date-filtered source response."""
    audit = audit_dated_results(prior_session, prior_body, prior_receipt)
    previous_ids, _, _ = _observed_labels(prior_session)
    body = strict_json_loads(prior_body.decode("utf-8"))
    known_results = {row["fixture_id"] for row in body["results"]}
    return {"prior_audit": audit, "previous_fixture_ids": previous_ids,
            "known_result_ids": known_results, "received_at": prior_receipt["received_at"],
            "result_sha256": prior_receipt["sha256"]}


def build_journal(session, prior, registry, store, *, feature_cutoff):
    verified = verify_session(session)
    if set(verified) != {(1, "odds")}:
        raise ValueError("freeze requires exactly one odds response and no current results")
    receipt = verified[(1, "odds")]
    if not instant(prior["received_at"]) < instant(receipt["requested_at"]):
        raise ValueError("prior results were not received before new odds request")
    cutoff = instant(feature_cutoff)
    age = (cutoff - instant(receipt["received_at"])).total_seconds()
    if not 0 < age <= 90:
        raise ValueError("feature cutoff must follow fresh local odds receipt")
    board = _exact_snapshot(session, 1, receipt)
    counts = Counter()
    forecasts = []
    for identity, state in sorted(board.items()):
        if state is None:
            counts["market_invalid"] += 1
            continue
        counts["market_eligible"] += 1
        if identity in prior["previous_fixture_ids"]:
            counts["excluded_prior_fixture_id"] += 1
            continue
        if identity in prior["known_result_ids"]:
            counts["excluded_prior_known_result_id"] += 1
            continue
        row = feature(identity, state, feature_cutoff, registry, store)
        counts["h2h_" + row["h2h_status"]] += 1
        forecasts.append(row)
    return {"schema": "research.tt_causal_h2h_observation.v1",
            "feature_cutoff": cutoff.isoformat(), "odds_received_at": receipt["received_at"],
            "odds_sha256": receipt["sha256"], "prior_result_sha256": prior["result_sha256"],
            "prior_result_received_at": prior["received_at"],
            "counts": {**dict(counts), "forecast_fixtures": len(forecasts)}, "forecasts": forecasts,
            "source_rights_verified": False, "identity_verified": False,
            "independent_holdout": False, "model_training_authorized": False,
            "bookmaker_prices_accepted": False, "real_money_execution": False,
            "note": "Market-implied probabilities are observational; H2H requires prior canonical identities."}


def verify_journal(root):
    """Verify exact forecast bytes and strict local receipt ordering."""
    root = Path(root)
    payload = (root / "forecast.body.json").read_bytes()
    envelope = strict_json_loads((root / "forecast.receipt.json").read_text("utf-8"))
    journal = strict_json_loads(payload.decode("utf-8"))
    verified = verify_session(root / "snapshots")
    if (envelope.get("schema") != "research.tt_forecast_receipt.v1" or
            type(envelope.get("bytes")) is not int or envelope["bytes"] != len(payload) or
            envelope.get("sha256") != hashlib.sha256(payload).hexdigest() or
            envelope.get("odds_sha256") != verified[(1, "odds")]["sha256"] or
            envelope.get("odds_sha256") != journal.get("odds_sha256") or
            envelope.get("prior_result_sha256") != journal.get("prior_result_sha256") or
            instant(envelope.get("computed_at")) != instant(journal.get("feature_cutoff"))):
        raise ValueError("forecast receipt does not bind journal and source")
    if not (instant(verified[(1, "odds")]["received_at"]) < instant(envelope["computed_at"]) <=
            instant(envelope["frozen_at"]) < instant(verified[(1, "results")]["requested_at"])):
        raise ValueError("forecast freeze receipt chronology invalid")
    return journal, envelope, verified


def run(prior_session, prior_body_path, prior_receipt_path, root):
    prior_body = Path(prior_body_path).read_bytes()
    prior_receipt = strict_json_loads(Path(prior_receipt_path).read_text("utf-8"))
    prior = prepare_prior(prior_session, prior_body, prior_receipt)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    # Explicitly pristine private fixture stores: never resolve names by
    # guessing or mutate the existing product's identity/opponent stores.
    registry = ParticipantIdentityRegistry.initialize_pristine(root / "identity-empty.json")
    store = OpponentIntelligenceStore.initialize_pristine(root / "opponents-empty.json", registry)
    session = root / "snapshots"
    capture_one(session, 1, "odds")
    computed_at = _utc_now()
    journal = build_journal(session, prior, registry, store, feature_cutoff=computed_at.isoformat())
    payload = (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_exclusive(root / "forecast.body.json", payload)
    frozen_at = _utc_now()
    if not 0 < (frozen_at - instant(journal["odds_received_at"])).total_seconds() <= 90:
        raise ValueError("forecast could not be frozen while local snapshot was fresh")
    forecast_receipt = {"schema": "research.tt_forecast_receipt.v1", "sha256": hashlib.sha256(payload).hexdigest(),
                        "bytes": len(payload), "computed_at": computed_at.isoformat(),
                        "frozen_at": frozen_at.isoformat(), "odds_sha256": journal["odds_sha256"],
                        "prior_result_sha256": prior["result_sha256"]}
    _write_exclusive(root / "forecast.receipt.json",
                     (json.dumps(forecast_receipt, sort_keys=True, indent=2) + "\n").encode("utf-8"))
    # Forecast bytes and their receipt are fsynced before this request starts.
    received = capture_one(session, 1, "results")
    journal, forecast_receipt, verified = verify_journal(root)
    result_raw = (session / "0001.results.body.json").read_bytes()
    if hashlib.sha256(result_raw).hexdigest() != verified[(1, "results")]["sha256"]:
        raise ValueError("results body changed after verification")
    later = strict_json_loads(result_raw.decode("utf-8"))
    chosen = {row["fixture_id"] for row in journal["forecasts"]}
    # This is a receipt/ID inventory only; neither finished status nor an
    # outcome is inferred from an ID being absent or present.
    present = {row.get("fixture_id") for row in later["results"] if isinstance(row, dict)}
    summary = {"forecast_sha256": forecast_receipt["sha256"], "frozen_at": forecast_receipt["frozen_at"],
               "later_results_requested_at": received["requested_at"],
               "later_results_received_at": received["received_at"],
               "counts": journal["counts"], "exact_ids_present_in_later_results": len(chosen & present),
               "note": "Result ID presence is not a qualified outcome, settlement or evidence of timing."}
    _write_exclusive(root / "summary.json", (json.dumps(summary, indent=2) + "\n").encode("utf-8"))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prior_session", type=Path)
    parser.add_argument("prior_result_body", type=Path)
    parser.add_argument("prior_result_receipt", type=Path)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    summary = run(args.prior_session, args.prior_result_body, args.prior_result_receipt, args.output_directory)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
