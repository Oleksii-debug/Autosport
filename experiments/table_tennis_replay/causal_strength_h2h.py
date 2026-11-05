"""Read-only synthetic/research strength and H2H comparison; no training/bets.

Inputs are prevalidated market quotes and canonical as-known identity/evidence.
Registry status is not external identity or rights verification. Fixed formulas
are illustrative, uncalibrated heuristics, with no money or promotion authority.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, localcontext

from autosport.opponent_intelligence import (
    InvalidationReason, InvalidationTarget, OpponentIntelligenceStore,
    RecomputeStatus, SnapshotState,
)
from autosport.participant_identity import IdentityView, ParticipantIdentityError
from causal_h2h_observer import SOURCE, _history_identity_status, feature
from record import instant

PRIOR_WEIGHT = Decimal(5)
MIN_EFFECTIVE_SUPPORT = 2
MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _newly_relevant_form(edges, selected_ids, fixture_id, before, registry, records,
                         players, league_id):
    """Flag known alias changes that might bring another edge into form."""
    resolved = {}
    for edge in edges:
        if edge.event_id == fixture_id or edge.performance_id in selected_ids:
            continue
        record = records.get(edge.performance_id)
        if record is None:
            return True
        obs = record.observation
        ids = []
        for alias in (obs.subject_alias, obs.opponent_alias, obs.league_alias):
            key = (obs.source_id, alias)
            if key not in resolved:
                try:
                    resolved[key] = registry.resolve_alias(
                        obs.source_id, alias, as_of=before,
                        view=IdentityView.AS_KNOWN_AT_DECISION).entity_id
                except ParticipantIdentityError:
                    resolved[key] = None
            ids.append(resolved[key])
        pair = {ids[0], ids[1]}
        # Exactly one current side: direct meetings are reserved for H2H.
        if ids[2] == league_id and len(pair & players) == 1:
            return True
    return False


def _summary(participant, inputs, before):
    values = OpponentIntelligenceStore._derive_snapshot_values(
        participant_entity_id=participant, inputs=inputs, causal_cutoff=before,
        min_support=MIN_EFFECTIVE_SUPPORT, max_age_seconds=MAX_AGE_SECONDS)
    support, effective, opponents, mean, radius, state, last, age = values
    # The canonical snapshot reports integer age seconds; additionally enforce
    # the configured boundary at full timestamp precision for this experiment.
    exact_fresh = None if last is None else instant(before) - instant(last) <= timedelta(seconds=MAX_AGE_SECONDS)
    if exact_fresh is False:
        mean, radius, state = None, None, SnapshotState.INSUFFICIENT
    return {"support": support, "effective_sample": effective, "opponent_count": opponents,
            "mean_score": mean, "support_radius_not_ci": radius, "state": state.value,
            "last_observed_at": last, "age_seconds": age, "within_exact_max_age": exact_fresh}


def compare(fixture_id, market, feature_cutoff, registry, store):
    """Compare fixed candidate values, as known strictly before a decision."""
    baseline = feature(fixture_id, market, feature_cutoff, registry, store)
    result = {"market_home_probability": baseline["market_home_probability"],
              "market_h2h_home_probability": baseline["h2h_home_probability"],
              "h2h_status": baseline["h2h_status"], "h2h_support": baseline["prior_meetings"],
              "strength_status": "blocked_by_identity_or_h2h_history",
              "home_form": None, "away_form": None,
              "strength_home_probability": None, "strength_h2h_home_probability": None,
              "source_rights_verified": False, "identity_independently_verified": False,
              "calibration_verified": False, "model_training": False,
              "bookmaker_execution": False, "profitability_claim": False}
    if baseline["h2h_status"] not in ("ready", "no_prior_history"):
        return result
    before = (instant(feature_cutoff) - timedelta(microseconds=1)).isoformat()
    home, away, league, _ = market
    # feature already applies current-alias/lineage guards on this same view.
    try:
        home_id, away_id, league_id = [
            registry.resolve_alias(SOURCE, label, as_of=before,
                                   view=IdentityView.AS_KNOWN_AT_DECISION).entity_id
            for label in (home, away, league)]
    except ParticipantIdentityError:
        result["strength_status"] = "identity_unresolved_during_read"
        return result
    players = {home_id, away_id}
    edges = store.graph_edges(as_of=before, view=IdentityView.AS_KNOWN_AT_DECISION,
                              sport_id="table_tennis", market_context_id="match-outcome")
    relevant = [edge for edge in edges if edge.event_id != fixture_id and
                edge.league_entity_id == league_id and
                len({edge.subject_entity_id, edge.opponent_entity_id} & players) == 1]
    selected_ids = {edge.performance_id for edge in relevant}
    if any(item.target_id in selected_ids and item.target_kind is InvalidationTarget.OPPONENT_EDGE and
           item.reason is InvalidationReason.IDENTITY_CORRECTION and
           item.recompute_status is RecomputeStatus.REQUIRED and
           instant(item.detected_at) < instant(feature_cutoff) for item in store.invalidations()):
        result["strength_status"] = "history_identity_invalidated"
        return result
    records = {record.performance_id: record for record in store._active_performances(
        as_of=before, view=IdentityView.AS_KNOWN_AT_DECISION)} if edges else {}
    status = _history_identity_status(relevant, before, registry, records)
    if status is not None:
        result["strength_status"] = status
        return result
    if _newly_relevant_form(edges, selected_ids, fixture_id, before, registry, records,
                            players, league_id):
        result["strength_status"] = "history_identity_unreconciled"
        return result
    if any(registry.lineage_at(entity, as_of=before, view=IdentityView.AS_KNOWN_AT_DECISION)
           for edge in relevant for entity in
           (edge.subject_entity_id, edge.opponent_entity_id, edge.league_entity_id)):
        result["strength_status"] = "history_identity_lineage_unreconciled"
        return result
    ids = [edge.event_id for edge in relevant]
    if len(ids) != len(set(ids)) or any(edge.score not in ("0", "1") for edge in relevant):
        result["strength_status"] = "history_ambiguous"
        return result
    home_records = [records[edge.performance_id] for edge in relevant
                    if home_id in (edge.subject_entity_id, edge.opponent_entity_id)]
    away_records = [records[edge.performance_id] for edge in relevant
                    if away_id in (edge.subject_entity_id, edge.opponent_entity_id)]
    hf, af = _summary(home_id, home_records, before), _summary(away_id, away_records, before)
    result.update(home_form=hf, away_form=af, strength_status="insufficient_or_stale")
    if hf["state"] != SnapshotState.SUPPORTED.value or af["state"] != SnapshotState.SUPPORTED.value:
        return result
    with localcontext() as ctx:
        ctx.prec = 28
        def shrink(form):
            n = Decimal(form["effective_sample"])
            return (n * Decimal(form["mean_score"]) + PRIOR_WEIGHT / 2) / (n + PRIOR_WEIGHT)
        strength = Decimal("0.5") + (shrink(hf) - shrink(af)) / 2
        result.update(strength_status="ready", strength_home_probability=str(strength))
        if baseline["h2h_status"] == "ready":
            wins, n = Decimal(baseline["prior_home_wins"]), Decimal(baseline["prior_meetings"])
            result["strength_h2h_home_probability"] = str((wins + PRIOR_WEIGHT * strength) / (n + PRIOR_WEIGHT))
    return result
