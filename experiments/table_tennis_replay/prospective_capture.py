"""Bounded read-only raw table-tennis evidence capture. No execution authority.

Receipt times are local observation times, not a trusted historical clock or a
claim that a bookmaker would accept a price. Unverified data rights and player
identities remain outside product training and authority.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from autosport.json_integrity import strict_json_loads
from record import instant

URLS = {
    "odds": "https://moviodds.com/odds?sport=table_tennis&in_play=1",
    "results": "https://moviodds.com/results?sport=table_tennis",
}
MAX_BYTES = 30 * 1024 * 1024
NAME = re.compile(r"^(\d{4})\.(odds|results)\.(body|receipt)\.json$")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect denied", headers, fp)


def _utc_now():
    return datetime.now(timezone.utc)


def _parse(body, kind):
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BYTES:
        raise ValueError("empty or oversized response")
    value = strict_json_loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("response must be an object")
    field = "matches" if kind == "odds" else "results"
    if not isinstance(value.get(field), list):
        raise ValueError("missing source collection")
    if kind == "results" and value.get("sport") != "table_tennis":
        raise ValueError("wrong results sport")
    return value


def _get(url):
    # Fixed URL only, no secrets, no redirects, no retries.
    request = Request(url, headers={"User-Agent": "Autosport-table-tennis-research/1.0"})
    with build_opener(NoRedirect()).open(request, timeout=25) as response:
        if response.status != 200:
            raise ValueError("unexpected HTTP status")
        return response.read(MAX_BYTES + 1)


def _names(seq, kind):
    stem = f"{seq:04d}.{kind}"
    return stem + ".body.json", stem + ".receipt.json"


def _write_exclusive(path, payload):
    # If interrupted between body and receipt, retain the incomplete evidence;
    # verify_session rejects it instead of silently overwriting it.
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def capture_one(directory, seq, kind, *, fetch=_get, clock=_utc_now):
    if type(seq) is not int or not 1 <= seq <= 9999 or kind not in URLS:
        raise ValueError("invalid capture sequence or kind")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    body_name, receipt_name = _names(seq, kind)
    body_path, receipt_path = directory / body_name, directory / receipt_name
    if body_path.exists() or receipt_path.exists():
        raise ValueError("capture path exists; verify before resuming")
    start = clock()
    raw = fetch(URLS[kind])
    received = clock()
    if (start.tzinfo is None or received.tzinfo is None or received < start):
        raise ValueError("local receipt clock moved backwards or lacks timezone")
    value = _parse(raw, kind)
    receipt = {"schema": "autosport.tt_capture_receipt.v1", "sequence": seq,
               "kind": kind, "url": URLS[kind], "requested_at": start.isoformat(),
               "received_at": received.isoformat(), "sha256": hashlib.sha256(raw).hexdigest(),
               "bytes": len(raw), "source_server_time": value.get("server_time"),
               "rights_verified": False, "identity_verified": False,
               "executable_price_verified": False}
    _write_exclusive(body_path, raw)
    _write_exclusive(receipt_path, (json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return receipt


def verify_session(directory):
    directory = Path(directory)
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise ValueError("capture destination must be a directory")
    groups = {}
    for path in directory.iterdir():
        match = NAME.fullmatch(path.name)
        if path.is_symlink() or not path.is_file() or not match:
            raise ValueError("unexpected evidence entry")
        seq, kind, piece = match.groups()
        groups.setdefault((int(seq), kind), set()).add(piece)
    verified = {}
    for (seq, kind), pieces in sorted(groups.items()):
        if pieces != {"body", "receipt"}:
            raise ValueError("incomplete body/receipt pair; preserve for review")
        body_name, receipt_name = _names(seq, kind)
        body = (directory / body_name).read_bytes()
        receipt = strict_json_loads((directory / receipt_name).read_text("utf-8"))
        if not isinstance(receipt, dict):
            raise ValueError("invalid receipt")
        if (receipt.get("schema") != "autosport.tt_capture_receipt.v1" or
                type(receipt.get("sequence")) is not int or receipt["sequence"] != seq or
                receipt.get("kind") != kind or receipt.get("url") != URLS[kind] or
                type(receipt.get("bytes")) is not int or receipt["bytes"] != len(body) or
                receipt.get("sha256") != hashlib.sha256(body).hexdigest() or
                any(receipt.get(k) is not False for k in ("rights_verified", "identity_verified", "executable_price_verified"))):
            raise ValueError("receipt does not bind response and scope")
        try:
            requested = instant(receipt.get("requested_at"))
            received = instant(receipt.get("received_at"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("invalid receipt time") from exc
        if requested > received:
            raise ValueError("receipt clock order is invalid")
        value = _parse(body, kind)
        if receipt.get("source_server_time") != value.get("server_time"):
            raise ValueError("receipt source time mismatch")
        verified[(seq, kind)] = receipt
    # Each cycle is ordered odds then results; resumed evidence cannot claim
    # chronological continuity if a prior receipt is missing or reordered.
    ordered = sorted(verified, key=lambda key: (key[0], 0 if key[1] == "odds" else 1))
    for idx, key in enumerate(ordered):
        expected = (idx // 2 + 1, "odds" if idx % 2 == 0 else "results")
        if key != expected:
            raise ValueError("capture sequence gap")
        if idx and instant(verified[ordered[idx - 1]]["received_at"]) > instant(verified[key]["requested_at"]):
            raise ValueError("overlapping or out-of-order receipts")
    return verified


def iter_visible_before(directory, cutoff):
    """Yield only snapshots received strictly before cutoff, after integrity checks.

    This constrains local receipt chronology; it does not independently validate
    wall-clock accuracy, historical availability, source rights, or fills.
    """
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be a timezone-aware datetime")
    verified = verify_session(directory)
    ordered = sorted(verified, key=lambda key: (key[0], 0 if key[1] == "odds" else 1))
    for seq, kind in ordered:
        receipt = verified[(seq, kind)]
        if instant(receipt["received_at"]) >= cutoff:
            continue
        body_name, _ = _names(seq, kind)
        # Revalidate the bytes at the read boundary; do not rely on an earlier
        # directory scan if the file changed between verification and use.
        raw = (Path(directory) / body_name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("snapshot changed after verification")
        yield receipt, _parse(raw, kind)


def collect(directory, cycles=6, interval=30, *, fetch=_get, clock=_utc_now, sleep=time.sleep):
    if type(cycles) is not int or not 1 <= cycles <= 6 or type(interval) is not int or interval < 30:
        raise ValueError("cycles must be 1..6 and interval at least 30 seconds")
    verified = verify_session(directory)
    initial = set(verified)
    if any(seq > cycles for seq, _ in verified):
        raise ValueError("existing session exceeds requested cycle count")
    for seq in range(1, cycles + 1):
        for kind in ("odds", "results"):
            if (seq, kind) in verified:
                continue
            receipt = capture_one(directory, seq, kind, fetch=fetch, clock=clock)
            verified = verify_session(directory)
            print(json.dumps({"sequence": seq, "kind": kind, "received_at": receipt["received_at"]}), flush=True)
        if seq < cycles:
            # On resumption, skip waiting for already completed cycles, but wait
            # after any newly completed cycle before starting the next one.
            if any((s, k) not in initial for s in (seq,) for k in ("odds", "results")):
                sleep(interval)
    return verified


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--cycles", type=int, default=6)
    p.add_argument("--interval", type=int, default=30)
    args = p.parse_args()
    try:
        result = collect(args.directory, cycles=args.cycles, interval=args.interval)
        print(json.dumps({"verified_snapshots": len(result), "complete": len(result) == 2 * args.cycles}))
    except (OSError, ValueError, UnicodeError, KeyError) as exc:
        print(json.dumps({"capture_stopped": type(exc).__name__, "completed_pairs_preserved": True}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
