"""Build a checked, explicitly scoped list of already observed fixture IDs.

This only compares exact provider fixture IDs. It cannot resolve pre-match/live
ID aliases, people, other providers, omitted observations or independence.
It is for private research PAPER experiments, never financial authority.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from forward_portfolio_journal import _excluded_sha, _read_record
from forward_portfolio_revisions import evaluate_history
from causal_h2h_revision_history import _read_history
from prospective_capture import verify_session
from record import instant


def _ids(body, field, key):
    obj = strict_json_loads(body.decode("utf-8"))
    if not isinstance(obj, dict) or not isinstance(obj.get(field), list):
        raise ValueError("invalid prior source collection")
    ids = set()
    for row in obj[field]:
        value = row.get(key) if isinstance(row, dict) else None
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("invalid prior source fixture ID")
        ids.add(value)
    return ids


def build_scope(session, journal, base_ids, base_head, history, history_head, *, before):
    """Validate frozen prior actions and all dated links; union their source IDs.

    ``before`` is the next observation's requested-at time or an earlier time.
    The supplied base set is bound by the prior frozen journal; callers are
    responsible for reconstructing that set from its own pinned history.
    """
    if not isinstance(base_ids, (set, frozenset)) or any(
        not isinstance(x, str) or not x or x != x.strip() for x in base_ids
    ):
        raise ValueError("invalid prior base IDs")
    cutoff = instant(before)
    report = evaluate_history(session, journal, base_ids, base_head, history, history_head)
    verified = verify_session(session)
    expected = {(n, kind) for n in (1, 2, 3) for kind in ("odds", "results")}
    if set(verified) != expected:
        raise ValueError("prior session must contain exactly three complete cycles")
    _, action = _read_record(journal, "action", 2)
    links = _read_history(history, history_head, action["sha256"])
    as_of = max([instant(r["received_at"]) for r in verified.values()] +
                [instant(receipt["received_at"]) for _, _, receipt, _ in links])
    if as_of >= cutoff:
        raise ValueError("prior evidence must be received strictly before new observation")

    odds_ids, result_ids, dated_ids = set(), set(), set()
    for (seq, kind), receipt in verified.items():
        raw = (Path(session) / f"{seq:04d}.{kind}.body.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("prior session changed after verification")
        if kind == "odds":
            odds_ids.update(_ids(raw, "matches", "id"))
        else:
            result_ids.update(_ids(raw, "results", "fixture_id"))
    for _, raw, _, _ in links:
        dated_ids.update(_ids(raw, "results", "fixture_id"))
    ids = set(base_ids) | odds_ids | result_ids | dated_ids
    return {"schema": "research.tt_forward_prior_scope.v1", "base_head": base_head,
            "history_head": history_head, "prior_as_of": as_of.isoformat(),
            "id_set_sha256": _excluded_sha(ids), "ids": sorted(ids),
            "counts": {"base_ids": len(base_ids), "prior_session_odds_ids": len(odds_ids),
                       "prior_session_result_ids": len(result_ids), "dated_result_ids": len(dated_ids),
                       "union_ids": len(ids), "dated_receipts": len(report["snapshots"])},
            "exact_provider_ids_only": True, "complete_prior_universe": False,
            "player_identity_verified": False, "independent_holdout": False}
