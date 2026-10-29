"""Compare later dated results with previously captured live fixture IDs.

This is a source-ID/chronology inventory, not verified person identity,
bookmaker settlement, executable price, outcome training or a betting model.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from autosport.json_integrity import strict_json_loads
from canonical_history_bridge import validated_rows
from prospective_capture import verify_session
from record import instant


def audit(session, raw, receipt):
    verified = verify_session(session)
    if not verified:
        raise ValueError("prior observation session is empty")
    if not isinstance(receipt, dict) or receipt.get("schema") != "autosport.tt_dated_followup.v1":
        raise ValueError("invalid follow-up receipt")
    if (type(receipt.get("bytes")) is not int or receipt["bytes"] != len(raw) or
            receipt.get("sha256") != hashlib.sha256(raw).hexdigest() or
            any(receipt.get(k) is not False for k in ("rights_verified", "identity_verified", "executable_price_verified"))):
        raise ValueError("follow-up receipt mismatch")
    parsed_url = urlsplit(receipt.get("url", ""))
    query = parse_qsl(parsed_url.query, keep_blank_values=True)
    if (parsed_url.scheme != "https" or parsed_url.netloc != "moviodds.com" or
            parsed_url.path != "/results" or parsed_url.fragment or
            len(query) != 2 or dict(query).get("sport") != "table_tennis" or
            set(k for k, _ in query) != {"sport", "date"}):
        raise ValueError("unexpected follow-up source")
    target_date = date.fromisoformat(dict(query)["date"]).isoformat()
    archive = strict_json_loads(raw.decode("utf-8"))
    if not isinstance(archive, dict) or archive.get("date") != target_date:
        raise ValueError("follow-up source date mismatch")
    requested = instant(receipt.get("requested_at"))
    received = instant(receipt.get("received_at"))
    if requested > received or any(instant(r["received_at"]) >= requested for r in verified.values()):
        raise ValueError("follow-up must start after prior capture")
    first = {}
    for (seq, kind), entry in verified.items():
        if kind != "odds":
            continue
        body = (Path(session) / f"{seq:04d}.odds.body.json").read_bytes()
        # verify_session checked earlier; recheck at the use boundary.
        if hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise ValueError("live snapshot changed after verification")
        board = strict_json_loads(body.decode("utf-8"))
        ids = set()
        for match in board["matches"]:
            if match.get("in_play") is not True:
                continue
            identity = match.get("id")
            if not isinstance(identity, str) or not identity.strip() or identity in ids:
                raise ValueError("missing or duplicate live fixture ID")
            ids.add(identity)
            first.setdefault(identity, instant(entry["received_at"]))
    valid, excluded = validated_rows(archive)
    exact = after = future = before = 0
    for row, _ in valid:
        identity = row["fixture_id"]
        if identity not in first:
            continue
        exact += 1
        if instant(row["updated_at"]) > received:
            future += 1
        elif instant(row["finished_at"]) < first[identity]:
            before += 1
        else:
            after += 1
    return {"schema": "research.tt_dated_followup_audit.v1", "source_date": target_date,
            "followup_sha256": receipt["sha256"], "followup_received_at": receipt["received_at"],
            "prior_distinct_live_fixture_ids": len(first), "followup_rows": len(archive["results"]),
            "structurally_valid_results": len(valid), "excluded": excluded,
            "same_fixture_id": exact, "matching_last_live_at_or_after_observation": after,
            "matching_source_update_after_receipt": future,
            "matching_last_live_before_observation": before,
            "player_identity_verified": False, "rights_verified": False,
            "independent_clock_verified": False, "bookmaker_settlement_verified": False,
            "training_authorized": False, "real_money_execution": False,
            "note": "last live seen is not the official finish; result first locally available at follow-up receipt"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("followup_body", type=Path)
    parser.add_argument("followup_receipt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = audit(args.session, args.followup_body.read_bytes(),
                   strict_json_loads(args.followup_receipt.read_text("utf-8")))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
