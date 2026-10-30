"""Hash-linked, receipt-ordered research history for frozen in-play forecasts.

The caller must retain and externally pin the latest link SHA. This can detect
missing or changed *recorded* links relative to that pin; it cannot prove that
every source read was recorded, the pin is latest, clocks are trusted, or the
provider's results are true. This module never trains or places a bet.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from autosport.json_integrity import strict_json_loads
from causal_h2h_longitudinal import _signature
from causal_h2h_observer import verify_journal
from causal_h2h_score import evaluate as score_followup
from next_day_player_eval import _scores
from prospective_capture import _write_exclusive
from record import instant

_NAME = re.compile(r"^(\d{4})\.(body|receipt|link)\.json$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {"schema", "sequence", "forecast_sha256", "previous_link_sha256",
           "body_sha256", "receipt_sha256"}
MAX_RECEIPTS = 256


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_history(directory, expected_head, forecast_sha):
    """Check complete triplets and the externally supplied last-link digest."""
    directory = Path(directory)
    if not isinstance(expected_head, str) or not _SHA.fullmatch(expected_head):
        raise ValueError("expected latest link SHA-256 is required")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("history must be a non-symlink directory")
    entries = {}
    for path in directory.iterdir():
        match = _NAME.fullmatch(path.name)
        if path.is_symlink() or not path.is_file() or not match:
            raise ValueError("unexpected history entry")
        sequence, kind = match.groups()
        sequence = int(sequence)
        if not 1 <= sequence <= MAX_RECEIPTS:
            raise ValueError("history exceeds supported sequence")
        entries.setdefault(sequence, set()).add(kind)
    if not entries or sorted(entries) != list(range(1, len(entries) + 1)):
        raise ValueError("history is empty or has a sequence gap")
    if any(parts != {"body", "receipt", "link"} for parts in entries.values()):
        raise ValueError("incomplete history triplet")
    checked, previous = [], None
    for sequence in sorted(entries):
        stem = f"{sequence:04d}"
        body = (directory / f"{stem}.body.json").read_bytes()
        receipt_bytes = (directory / f"{stem}.receipt.json").read_bytes()
        link_bytes = (directory / f"{stem}.link.json").read_bytes()
        link = strict_json_loads(link_bytes.decode("utf-8"))
        if (not isinstance(link, dict) or set(link) != _FIELDS or
                link["schema"] != "research.tt_result_history_link.v1" or
                type(link["sequence"]) is not int or link["sequence"] != sequence or
                link["forecast_sha256"] != forecast_sha or
                link["previous_link_sha256"] != previous or
                link["body_sha256"] != _digest(body) or
                link["receipt_sha256"] != _digest(receipt_bytes)):
            raise ValueError("history link or evidence hash mismatch")
        receipt = strict_json_loads(receipt_bytes.decode("utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("invalid history receipt")
        previous = _digest(link_bytes)
        checked.append((sequence, body, receipt, previous))
    if previous != expected_head:
        raise ValueError("history head differs from externally pinned SHA")
    return checked


def evaluate_history(root, directory, expected_head):
    """Score each as-of state, retaining quarantine through all later receipts."""
    journal, envelope, _ = verify_journal(Path(root))
    checked = _read_history(directory, expected_head, envelope["sha256"])
    forecasts = journal["forecasts"]
    ids = [row["fixture_id"] for row in forecasts]
    states = {fid: {"state": "unresolved", "unqualified_finished_seen": False,
                    "signature": None, "score": None, "first_eligible_at": None,
                    "quarantined_at": None, "reason": None} for fid in ids}
    snapshots, previous_receipt, previous_date = [], None, None
    for sequence, body, receipt, link_sha in checked:
        if previous_receipt is not None and (
                instant(previous_receipt["received_at"]) >= instant(receipt.get("requested_at"))):
            raise ValueError("later request must start strictly after prior receipt")
        scored = score_followup(root, body, receipt)
        current_date = scored["followup_audit"]["source_date"]
        if previous_date is not None and current_date != previous_date:
            raise ValueError("history contains different source dates")
        source = strict_json_loads(body.decode("utf-8"))
        reported = {row["fixture_id"]: row for row in source["results"]}
        eligible = {row["fixture_id"]: row for row in scored["rows"]}
        as_of = receipt["received_at"]
        for fid in ids:
            state = states[fid]
            if state["state"] == "quarantined":
                continue
            if state["state"] == "eligible":
                if fid not in reported:
                    reason = "previously_eligible_missing"
                elif fid not in eligible:
                    reason = "previously_eligible_now_invalid"
                elif (_signature(reported[fid]) != state["signature"] or
                      eligible[fid] != state["score"]):
                    reason = "previously_eligible_changed"
                else:
                    reason = None
                if reason:
                    state.update(state="quarantined", reason=reason, quarantined_at=as_of)
            elif fid in eligible:
                if state["unqualified_finished_seen"]:
                    state.update(state="quarantined", reason="earlier_finished_ineligible",
                                 quarantined_at=as_of)
                else:
                    state.update(state="eligible", signature=_signature(reported[fid]),
                                 score=eligible[fid], first_eligible_at=as_of)
            elif fid in reported and reported[fid].get("status") == "finished":
                state["unqualified_finished_seen"] = True
        counts = Counter(state["state"] for state in states.values())
        admitted = [eligible[fid] for fid in ids if states[fid]["state"] == "eligible"]
        # Copy values at this receipt: future evidence cannot rewrite the past.
        records = [{"fixture_id": fid, "state": states[fid]["state"],
                    "first_eligible_at": states[fid]["first_eligible_at"],
                    "quarantined_at": states[fid]["quarantined_at"],
                    "reason": states[fid]["reason"]} for fid in ids]
        snapshots.append({"sequence": sequence, "link_sha256": link_sha,
                          "source_sha256": receipt["sha256"], "as_of": as_of,
                          "source_audit": scored["followup_audit"],
                          "source_scorer_counts": scored["counts"],
                          "counts": {k: counts[k] for k in ("eligible", "quarantined", "unresolved")},
                          "market": _scores(admitted, "market"),
                          "neutral": _scores(admitted, "neutral"),
                          "rows": admitted, "records": records})
        previous_receipt, previous_date = receipt, current_date
    return {"schema": "research.tt_result_revision_history.v1", "forecast_sha256": envelope["sha256"],
            "expected_head_sha256": expected_head, "frozen_candidates": len(ids),
            "snapshots": snapshots, "identity_verified": False, "rights_verified": False,
            "source_results_independently_verified": False, "independent_holdout": False,
            "bookmaker_acceptance_verified": False, "model_training_authorized": False,
            "real_money_execution": False, "profitability_claim": False,
            "note": "Checks recorded receipts against supplied pin, not unknown or omitted source reads; "
                    "pin freshness, source clocks and source truth need independent evidence."}


def append_followup(root, directory, body, receipt_bytes, *, expected_head):
    """Append a verified response without overwriting any earlier evidence.

    expected_head must be None for a fresh empty directory, otherwise it must
    equal the latest externally retained pin. Interrupted partial writes stay
    visible and all later verification fails closed until reviewed.
    """
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("history must be a non-symlink directory")
    journal, envelope, _ = verify_journal(Path(root))
    if list(directory.iterdir()):
        previous = evaluate_history(root, directory, expected_head)
        previous_snapshot = previous["snapshots"][-1]
        sequence = previous_snapshot["sequence"] + 1
        previous_at = instant(previous_snapshot["as_of"])
        previous_date = previous_snapshot["source_audit"]["source_date"]
    else:
        if expected_head is not None:
            raise ValueError("empty history cannot match an existing head")
        sequence, previous_at, previous_date = 1, None, None
    if sequence > MAX_RECEIPTS:
        raise ValueError("history exceeds supported sequence")
    receipt = strict_json_loads(receipt_bytes.decode("utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("invalid history receipt")
    scored = score_followup(root, body, receipt)
    if previous_at is not None and previous_at >= instant(receipt.get("requested_at")):
        raise ValueError("later request must start strictly after prior receipt")
    if previous_date is not None and previous_date != scored["followup_audit"]["source_date"]:
        raise ValueError("history contains different source dates")
    link = {"schema": "research.tt_result_history_link.v1", "sequence": sequence,
            "forecast_sha256": envelope["sha256"], "previous_link_sha256": expected_head,
            "body_sha256": _digest(body), "receipt_sha256": _digest(receipt_bytes)}
    link_bytes = (json.dumps(link, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    stem = f"{sequence:04d}"
    _write_exclusive(directory / f"{stem}.body.json", body)
    _write_exclusive(directory / f"{stem}.receipt.json", receipt_bytes)
    _write_exclusive(directory / f"{stem}.link.json", link_bytes)
    return _digest(link_bytes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observation_directory", type=Path)
    parser.add_argument("history_directory", type=Path)
    parser.add_argument("expected_head_sha256")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = evaluate_history(args.observation_directory, args.history_directory,
                              args.expected_head_sha256)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
