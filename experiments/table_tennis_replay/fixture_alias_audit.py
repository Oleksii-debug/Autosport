"""Audit provider-claimed fixture aliases as locally available at a cutoff.

An alias group is a claim by one provider, not verified event or person
identity. A non-overlap against a scoped set is not proof of independence.
This module only produces research diagnostics; it does not change decisions,
train models, authorize money, or call a bookmaker.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError

from autosport.json_integrity import strict_json_loads
from forward_prior_scope import _ids
from prospective_capture import _get, _write_exclusive, verify_session
from record import instant

PREFIX = "https://moviodds.com/closing?fi="
MAX_ALIASES = 128


def _id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 32 or not value.isascii() or not value.isdigit():
        raise ValueError("fixture ID must be ASCII digits")
    return value


def session_ids(session):
    """Read all captured odds IDs only after the existing session verifier."""
    session = Path(session)
    verified = verify_session(session)
    if set(verified) != {(n, kind) for n in (1, 2, 3) for kind in ("odds", "results")}:
        raise ValueError("expected a complete three-cycle source session")
    ids = set()
    for n in (1, 2, 3):
        raw = (session / f"{n:04d}.odds.body.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != verified[(n, "odds")]["sha256"]:
            raise ValueError("source odds changed after verification")
        ids.update(_ids(raw, "matches", "id"))
    for fid in ids:
        _id(fid)
    return ids


def _now():
    return datetime.now(timezone.utc)


def capture_one(directory, sequence, fixture_id, *, fetch=_get, clock=_now):
    """One bounded request; persist response before parsing; no retry.

    An exception is recorded by class and optional HTTP status, never its text
    or headers. The caller decides whether to stop collecting further IDs.
    """
    fixture_id = _id(fixture_id)
    if type(sequence) is not int or not 1 <= sequence <= 9999:
        raise ValueError("invalid capture sequence")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("capture directory must be a non-symlink directory")
    body_path = directory / f"{sequence:04d}.body.json"
    receipt_path = directory / f"{sequence:04d}.receipt.json"
    if body_path.exists() or receipt_path.exists():
        raise ValueError("capture destination exists")
    started = clock()
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("request clock must be timezone-aware")
    url = PREFIX + fixture_id
    try:
        raw = fetch(url)
    except Exception as exc:
        ended = clock()
        if ended.tzinfo is None or ended.utcoffset() is None or ended < started:
            raise ValueError("invalid error receipt clock") from exc
        receipt = {"schema": "research.tt_fixture_alias_receipt.v1", "sequence": sequence,
                   "fixture_id": fixture_id, "url": url, "requested_at": started.isoformat(),
                   "received_at": ended.isoformat(), "outcome": "request_error",
                   "error_class": type(exc).__name__,
                   "http_status": exc.code if isinstance(exc, HTTPError) else None,
                   "rights_verified": False, "identity_verified": False}
        _write_exclusive(receipt_path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
        return receipt
    ended = clock()
    if ended.tzinfo is None or ended.utcoffset() is None or ended < started:
        raise ValueError("invalid source receipt clock")
    if not isinstance(raw, bytes):
        raise ValueError("source response must be bytes")
    receipt = {"schema": "research.tt_fixture_alias_receipt.v1", "sequence": sequence,
               "fixture_id": fixture_id, "url": url, "requested_at": started.isoformat(),
               "received_at": ended.isoformat(), "outcome": "response", "bytes": len(raw),
               "sha256": hashlib.sha256(raw).hexdigest(),
               "rights_verified": False, "identity_verified": False}
    _write_exclusive(body_path, raw)
    _write_exclusive(receipt_path, (json.dumps(receipt, sort_keys=True) + "\n").encode())
    return receipt


def read_captures(directory):
    """Validate a complete recorded directory, including later entries.

    This integrity check is not an as-of loader: a later incomplete/tampered
    entry may stop it. ``audit_as_of`` filters already loaded observations.
    """
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid capture directory")
    groups = {}
    for path in directory.iterdir():
        parts = path.name.split(".")
        if (path.is_symlink() or not path.is_file() or len(parts) != 3 or
                len(parts[0]) != 4 or not parts[0].isascii() or not parts[0].isdigit() or
                parts[1] not in ("body", "receipt") or parts[2] != "json"):
            raise ValueError("unexpected capture entry")
        groups.setdefault(int(parts[0]), set()).add(parts[1])
    if sorted(groups) != list(range(1, len(groups) + 1)):
        raise ValueError("capture sequence gap")
    observations = []
    for seq, pieces in sorted(groups.items()):
        if "receipt" not in pieces:
            raise ValueError("incomplete alias capture")
        receipt = strict_json_loads((directory / f"{seq:04d}.receipt.json").read_text("utf-8"))
        if not isinstance(receipt, dict) or type(receipt.get("sequence")) is not int or receipt["sequence"] != seq:
            raise ValueError("alias receipt sequence mismatch")
        expected = {"body", "receipt"} if receipt.get("outcome") == "response" else {"receipt"}
        if pieces != expected:
            raise ValueError("incomplete alias capture")
        body = (directory / f"{seq:04d}.body.json").read_bytes() if "body" in pieces else None
        observations.append((receipt, body))
    return observations


def _aliases(raw, fixture_id):
    """Validate only explicit /closing linkage fields, never names or prices."""
    obj = strict_json_loads(raw.decode("utf-8"))
    if (not isinstance(obj, dict) or obj.get("fixture_id") != fixture_id or
            not isinstance(obj.get("fixture_ids"), list) or
            not isinstance(obj.get("selections"), list)):
        raise ValueError("invalid provider linkage response")
    aliases = obj["fixture_ids"]
    if not 1 <= len(aliases) <= MAX_ALIASES:
        raise ValueError("invalid provider alias count")
    aliases = [_id(value) for value in aliases]
    if len(set(aliases)) != len(aliases) or fixture_id not in aliases:
        raise ValueError("duplicate or missing requested fixture ID")
    prematch = obj.get("prematch_id")
    if prematch is not None and _id(prematch) not in aliases:
        raise ValueError("pre-match ID outside provider alias group")
    return frozenset(aliases)


def audit_as_of(candidates, prior_ids, observations, *, cutoff):
    """Conservatively classify candidate overlap using only receipts before cutoff.

    ``prior_ids`` is a caller-supplied, already frozen scoped set. Evidence
    received exactly at or after cutoff has no effect, including malformed body
    bytes. Multiple visible observations of one candidate remain ambiguous.
    """
    if not isinstance(candidates, (set, frozenset)) or not isinstance(prior_ids, (set, frozenset)):
        raise ValueError("candidates and scoped prior IDs must be sets")
    for fid in candidates | prior_ids:
        _id(fid)
    at = instant(cutoff)
    visible = {}
    for receipt, raw in observations:
        if not isinstance(receipt, dict):
            raise ValueError("invalid alias receipt")
        received = instant(receipt.get("received_at"))
        if received >= at:
            continue
        fid = _id(receipt.get("fixture_id"))
        if fid not in candidates:
            raise ValueError("receipt references an unrequested fixture")
        if (receipt.get("schema") != "research.tt_fixture_alias_receipt.v1" or
                receipt.get("url") != PREFIX + fid or
                receipt.get("rights_verified") is not False or receipt.get("identity_verified") is not False or
                instant(receipt.get("requested_at")) > received):
            raise ValueError("alias receipt metadata mismatch")
        outcome = receipt.get("outcome")
        if outcome == "response":
            if (not isinstance(raw, bytes) or type(receipt.get("bytes")) is not int or
                    receipt["bytes"] != len(raw) or receipt.get("sha256") != hashlib.sha256(raw).hexdigest()):
                raise ValueError("alias response hash mismatch")
            try:
                aliases = _aliases(raw, fid)
                value = ("valid", aliases)
            except (ValueError, TypeError, UnicodeError):
                value = ("invalid", None)
        elif outcome == "request_error" and raw is None:
            value = ("request_error", None)
        else:
            raise ValueError("invalid alias capture outcome")
        visible.setdefault(fid, []).append(value)

    groups = {fid: values[0][1] for fid, values in visible.items()
              if len(values) == 1 and values[0][0] == "valid"}
    all_claims = [(fid, value[1]) for fid, values in visible.items() for value in values if value[0] == "valid"]
    rows = []
    for fid in sorted(candidates):
        evidence = visible.get(fid, [])
        group = groups.get(fid)
        if fid in prior_ids:
            status = "prior_overlap"
        elif len(evidence) > 1:
            status = "ambiguous_multiple_receipts"
        elif not evidence:
            status = "unavailable"
        elif evidence[0][0] != "valid":
            status = evidence[0][0]
        elif group & prior_ids:
            status = "prior_overlap"
        elif (group & (candidates - {fid}) or
              any(group & other for other_id, other in all_claims if other_id != fid)):
            status = "candidate_overlap"
        else:
            status = "no_scoped_overlap"
        rows.append({"fixture_id": fid, "status": status,
                     "provider_claimed_alias_count": len(group) if group is not None else None})
    counts = Counter(row["status"] for row in rows)
    return {"schema": "research.tt_fixture_alias_asof.v1", "cutoff": at.isoformat(),
            "candidates": len(candidates), "prior_scoped_ids": len(prior_ids),
            "counts": dict(sorted(counts.items())), "rows": rows,
            "prior_scope_complete": False, "provider_identity_independently_verified": False,
            "player_identity_verified": False, "rights_verified": False,
            "independent_holdout": False, "model_training_authorized": False,
            "real_money_execution": False,
            "note": "No scoped overlap is not proof of unique events; future receipts are invisible."}
