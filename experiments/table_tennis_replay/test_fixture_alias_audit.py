from copy import deepcopy
import hashlib
import json
from urllib.error import HTTPError

import pytest

from fixture_alias_audit import PREFIX, audit_as_of, capture_one, read_captures, session_ids
from test_forward_portfolio_journal import capture, clock, setup
from test_quote_delay_audit import match, stamp


def observation(fid, aliases, *, requested=10, received=11, prematch=None):
    body = json.dumps({"fixture_id": fid, "fixture_ids": aliases, "prematch_id": prematch,
                       "selections": []}).encode()
    receipt = {"schema": "research.tt_fixture_alias_receipt.v1", "sequence": 1,
               "fixture_id": fid, "url": PREFIX + fid, "requested_at": stamp(requested),
               "received_at": stamp(received), "outcome": "response", "bytes": len(body),
               "sha256": hashlib.sha256(body).hexdigest(),
               "rights_verified": False, "identity_verified": False}
    return receipt, body


def audit(candidates, prior, observations, *, cutoff=20):
    return audit_as_of(set(candidates), set(prior), observations, cutoff=stamp(cutoff))


def by_id(report):
    return {row["fixture_id"]: row["status"] for row in report["rows"]}


def test_source_alias_intersecting_prior_is_flagged_even_when_id_differs():
    result = audit(["100"], ["9"], [observation("100", ["100", "9"], prematch="9")])
    assert by_id(result) == {"100": "prior_overlap"}
    assert result["provider_identity_independently_verified"] is False
    assert result["model_training_authorized"] is result["real_money_execution"] is False


def test_overlap_between_candidates_is_flagged_without_name_matching():
    result = audit(["100", "200", "300"], [],
                   [observation("100", ["100", "900"]), observation("200", ["200", "900"]),
                    observation("300", ["300"])])
    assert by_id(result) == {"100": "candidate_overlap", "200": "candidate_overlap", "300": "no_scoped_overlap"}
    assert result["prior_scope_complete"] is result["independent_holdout"] is False


def test_exact_prior_id_is_flagged_even_without_response():
    assert by_id(audit(["100"], ["100"], [])) == {"100": "prior_overlap"}


@pytest.mark.parametrize("case", ["missing_list", "missing_self", "duplicate", "bad_prematch", "wrong_fixture", "unsafe_alias", "no_selections"])
def test_invalid_provider_linkage_is_not_admitted(case):
    receipt, raw = observation("100", ["100", "9"], prematch="9")
    obj = json.loads(raw)
    if case == "missing_list": obj.pop("fixture_ids")
    elif case == "missing_self": obj["fixture_ids"] = ["9"]
    elif case == "duplicate": obj["fixture_ids"] = ["100", "100"]
    elif case == "bad_prematch": obj["prematch_id"] = "8"
    elif case == "wrong_fixture": obj["fixture_id"] = "101"
    elif case == "unsafe_alias": obj["fixture_ids"] = ["100", "../9"]
    else: obj.pop("selections")
    raw = json.dumps(obj).encode(); receipt["bytes"] = len(raw); receipt["sha256"] = hashlib.sha256(raw).hexdigest()
    assert by_id(audit(["100"], [], [(receipt, raw)])) == {"100": "invalid"}


def test_strict_cutoff_ignores_future_body_even_if_it_is_corrupted():
    future = observation("100", ["100", "9"], requested=19, received=20)
    baseline = audit(["100"], ["9"], [], cutoff=20)
    assert audit(["100"], ["9"], [future], cutoff=20) == baseline
    assert audit(["100"], ["9"], [(future[0], b"tampered")], cutoff=20) == baseline
    assert by_id(audit(["100"], ["9"], [future], cutoff=21)) == {"100": "prior_overlap"}


def test_visible_body_tampering_and_receipt_binding_fail_closed():
    receipt, body = observation("100", ["100"])
    with pytest.raises(ValueError, match="hash mismatch"):
        audit(["100"], [], [(receipt, body + b" ")])
    receipt = deepcopy(receipt); receipt["url"] += "&secret=untrusted"
    with pytest.raises(ValueError, match="metadata mismatch"):
        audit(["100"], [], [(receipt, body)])


def test_duplicate_visible_receipts_remain_ambiguous():
    a = observation("100", ["100"])
    b = observation("100", ["100", "8"], requested=12, received=13)
    assert by_id(audit(["100"], [], [a, b])) == {"100": "ambiguous_multiple_receipts"}


def test_one_ambiguous_candidate_cannot_hide_a_claim_against_another():
    observations = [observation("100", ["100"]), observation("200", ["200", "100"]),
                    observation("200", ["200"], requested=12, received=13)]
    assert by_id(audit(["100", "200"], [], observations)) == {
        "100": "candidate_overlap", "200": "ambiguous_multiple_receipts"}


def test_capture_retains_malformed_body_before_any_parsing(tmp_path):
    calls = []
    def fetch(url):
        calls.append(url); return b"not json"
    capture_one(tmp_path, 1, "100", fetch=fetch, clock=clock(10, 11))
    assert calls == [PREFIX + "100"]
    assert (tmp_path / "0001.body.json").read_bytes() == b"not json"
    assert by_id(audit(["100"], [], read_captures(tmp_path))) == {"100": "invalid"}
    with pytest.raises(ValueError, match="destination exists"):
        capture_one(tmp_path, 1, "100", fetch=fetch, clock=clock(12, 13))
    assert len(calls) == 1


def test_request_error_records_only_class_and_status_and_is_unavailable(tmp_path):
    def fetch(url):
        raise HTTPError(url, 503, "do not persist error text", {}, None)
    receipt = capture_one(tmp_path, 1, "100", fetch=fetch, clock=clock(10, 11))
    assert receipt["http_status"] == 503 and "do not persist" not in json.dumps(receipt)
    assert not (tmp_path / "0001.body.json").exists()
    assert by_id(audit(["100"], [], read_captures(tmp_path))) == {"100": "request_error"}


def test_incomplete_or_extra_capture_entries_fail_closed(tmp_path):
    (tmp_path / "0001.body.json").write_bytes(b"partial")
    with pytest.raises(ValueError, match="incomplete"):
        read_captures(tmp_path)
    (tmp_path / "other.txt").write_text("unexpected")
    with pytest.raises(ValueError, match="unexpected"):
        read_captures(tmp_path)


def test_source_ids_include_every_odds_snapshot_after_session_verification(tmp_path):
    session, _ = setup(tmp_path)
    for n, at, identities in [(1, 0, ["1", "2"]), (2, 40, ["2", "3"]), (3, 80, ["3", "4"])]:
        capture(session, n, "odds", at, [match(x, at) for x in identities])
        capture(session, n, "results", at + 2, [])
    assert session_ids(session) == {"1", "2", "3", "4"}
    path = session / "0001.odds.body.json";path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="receipt"):
        session_ids(session)


def test_unsafe_ids_and_unrequested_visible_evidence_fail_closed():
    with pytest.raises(ValueError, match="ASCII digits"):
        audit(["../1"], [], [])
    with pytest.raises(ValueError, match="unrequested"):
        audit(["100"], [], [observation("200", ["200"])])
