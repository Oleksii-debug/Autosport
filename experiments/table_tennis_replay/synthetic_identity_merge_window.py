"""Illustrate receipt-limited identity merges on invented TT fixtures only.

This is not a Sportradar client, response parser, monitoring service, identity
proof, or runtime guard. The public source documents seven-day mapping retention;
the input records and every label here are invented, not provider observations.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from autosport.opponent_intelligence import OpponentIntelligenceStore
from autosport.participant_identity import (
    EntityIdentity, EntityKind, EntityLineage, LineageRelation,
    ParticipantIdentityRegistry,
)
from causal_strength_h2h import compare
from record import instant
from synthetic_tt_learning_cycle import BASE, FIXTURE_SHA, _fixture_stores, _time

# Whitelisted fictitious labels prevent this example from becoming a data adapter.
LABELS = frozenset({"A", "B", "C", "D", "E", "F", "A2", "Z", "Z2"})
START = BASE
WEEK = timedelta(days=7)
MAX_RECEIPTS = 128
MAX_MAPPINGS_PER_RECEIPT = 16
SCHEMA = "research.synthetic_identity_merge_receipts.v1"


def scenario_hash(receipts):
    """Hash caller-provided synthetic receipts; a hash alone proves no freshness."""
    try:
        raw = json.dumps(receipts, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid synthetic receipts") from exc
    return hashlib.sha256(raw).hexdigest()


def _validated(receipts):
    if type(receipts) is not list or len(receipts) > MAX_RECEIPTS:
        raise ValueError("synthetic receipts must be a bounded list")
    previous = None
    known_targets = {}
    result = []
    for receipt in receipts:
        if type(receipt) is not dict or set(receipt) != {"received_at", "mappings"}:
            raise ValueError("invalid synthetic receipt fields")
        if type(receipt["received_at"]) is not str:
            raise ValueError("invalid synthetic receipt time")
        try:
            received = instant(receipt["received_at"])
        except (ValueError, AttributeError) as exc:
            raise ValueError("invalid synthetic receipt time") from exc
        if received < START or (previous is not None and received <= previous):
            raise ValueError("synthetic receipt times must increase from start")
        previous = received
        mappings = receipt["mappings"]
        if type(mappings) is not list or len(mappings) > MAX_MAPPINGS_PER_RECEIPT:
            raise ValueError("synthetic mappings must be a bounded list")
        seen = set()
        snapshot = []
        for item in mappings:
            if type(item) is not dict or set(item) != {"old", "retained"}:
                raise ValueError("invalid synthetic mapping")
            old, retained = item["old"], item["retained"]
            if type(old) is not str or type(retained) is not str or old not in LABELS or retained not in LABELS or old == retained:
                raise ValueError("synthetic mapping requires distinct fixture labels")
            if old in seen:
                raise ValueError("duplicate source identity within synthetic receipt")
            if old in known_targets and known_targets[old] != retained:
                raise ValueError("conflicting synthetic mappings")
            seen.add(old)
            known_targets[old] = retained
            snapshot.append((old, retained))
        result.append((received, tuple(snapshot), scenario_hash(receipt)))
    # A cyclic chain can never be admitted as canonical non-split lineage.
    for origin in known_targets:
        cursor, walked = origin, set()
        while cursor in known_targets:
            if cursor in walked:
                raise ValueError("cyclic synthetic mappings")
            walked.add(cursor)
            cursor = known_targets[cursor]
    return result


def audit(receipts, *, expected_sha256, decision_at):
    """Reconstruct one strict pre-decision synthetic knowledge cut in isolation."""
    observed = _validated(receipts)
    if type(expected_sha256) is not str or scenario_hash(receipts) != expected_sha256:
        raise ValueError("synthetic receipt digest differs from external pin")
    if type(decision_at) is not str:
        raise ValueError("invalid synthetic decision time")
    try:
        cutoff = instant(decision_at)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid synthetic decision time") from exc
    if cutoff <= START:
        raise ValueError("synthetic decision must follow start")
    visible = [(at, pairs, digest) for at, pairs, digest in observed if at < cutoff]
    gaps = [b - a for a, b in zip([START] + [x[0] for x in visible],
                                  [x[0] for x in visible] + [cutoff])]

    with TemporaryDirectory(prefix="tt-synthetic-merge-") as directory:
        registry, store = _fixture_stores(Path(directory))
        for label in ("A2", "Z", "Z2"):
            registry.add_entity(EntityIdentity(label, EntityKind.PARTICIPANT,
                                               "invented:" + label, FIXTURE_SHA,
                                               _time(0), _time(0)))
        seen = {}
        for at, pairs, digest in visible:
            for old, retained in pairs:
                if old in seen:
                    continue  # A repeated observation does not replace first receipt time.
                local_time = at.isoformat()
                registry.add_lineage(EntityLineage(old, retained, LineageRelation.MERGED_FROM,
                                                   local_time, local_time, local_time, digest))
                seen[old] = retained
        market = ("A", "B", "L", {"A": ("1", Decimal("1.5")), "B": ("2", Decimal("2.5"))})
        before_restart = compare("invented-current", market, cutoff.isoformat(), registry, store)
        bytes_before = registry.path.read_bytes()
        reopened = ParticipantIdentityRegistry(registry.path)
        reopened_store = OpponentIntelligenceStore(store.path, reopened)
        after_restart = compare("invented-current", market, cutoff.isoformat(), reopened, reopened_store)
        if before_restart != after_restart or registry.path.read_bytes() != bytes_before:
            raise ValueError("synthetic identity state changed on reopen or read")
        before = (cutoff - timedelta(microseconds=1)).isoformat()
        current_lineages = len(reopened.lineage_at("A", as_of=before))
        out = {
            "schema": "research.tt_synthetic_merge_window.v1",
            "decision_at": cutoff.isoformat(), "visible_receipts": len(visible),
            "persisted_distinct_mappings": len(seen), "current_subject_lineages": current_lineages,
            "last_visible_receipt_mapping_count": len(visible[-1][1]) if visible else None,
            "gap_at_least_seven_days_can_hide_entry": any(gap >= WEEK for gap in gaps),
            "max_unobserved_gap_seconds": max(gaps).total_seconds(),
            "h2h_status": after_restart["h2h_status"],
            "h2h_support": after_restart["h2h_support"],
            "strength_status": after_restart["strength_status"],
            "strength_h2h_home_probability": after_restart["strength_h2h_home_probability"],
            "reopened_registry_matches": True, "only_synthetic_receipts": True,
            "polling_coverage_verified": False, "mapping_truth_verified": False,
            "physical_identity_verified": False, "participant_kind_from_provider_verified": False,
            "api_access_verified": False, "live_adapter_or_runtime_gate": False,
            "model_training_authorized": False, "real_money_execution": False,
        }
    return out


def example_receipts(*, saw_merge=True):
    """Build a hypothetical transcript; no downloaded provider payload is used."""
    early = {"received_at": _time(20), "mappings": []}
    merge = {"received_at": _time(40), "mappings": [{"old": "A", "retained": "A2"}]}
    later = {"received_at": _time(8 * 24 * 60 * 60), "mappings": []}
    return [early, merge, later] if saw_merge else [early, later]


def run_demo():
    """Two same-transcript hypothetical worlds demonstrate missing coverage."""
    observed = example_receipts()
    missing = example_receipts(saw_merge=False)
    pin, missing_pin = scenario_hash(observed), scenario_hash(missing)
    # Under this intentionally idealized lifetime, a merge first listed at 40s
    # expires after seven days. Polls at 20s and eight days both miss it. The
    # same empty polls also arise if there was no merge: distinguishability fails.
    times = [instant(item["received_at"]) for item in missing]
    hypothetical_start = instant(_time(40))
    no_merge_world = [False for _ in times]
    expired_merge_world = [hypothetical_start <= t < hypothetical_start + WEEK for t in times]
    return {
        "schema": "research.tt_synthetic_identity_merge_demo.v1", "simulated_only": True,
        "observed_receipt_sha256": pin, "missing_receipt_sha256": missing_pin,
        "at_merge_receipt": audit(observed, expected_sha256=pin, decision_at=_time(40)),
        "after_merge_receipt": audit(observed, expected_sha256=pin, decision_at=_time(40.000001)),
        "after_entry_disappears": audit(observed, expected_sha256=pin, decision_at=_time(8 * 24 * 60 * 60 + 1)),
        "missed_entry": audit(missing, expected_sha256=missing_pin, decision_at=_time(8 * 24 * 60 * 60 + 1)),
        "hypothetical_hidden_merge_and_no_merge_have_same_empty_polls": no_merge_world == expired_merge_world,
        "hidden_merge_world_is_invented_not_observed": True,
        "note": "Receipt hashes and short gaps do not prove provider completeness, timeliness or identity.",
    }


if __name__ == "__main__":
    print(json.dumps(run_demo(), ensure_ascii=False, sort_keys=True, indent=2))
