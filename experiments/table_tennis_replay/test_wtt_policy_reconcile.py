import hashlib
import json

import pytest

import wtt_policy_reconcile as policy
from test_wtt_archive_inventory import COMMON, archive, csv_bytes, row


FIELDS = COMMON + ["a_id_raw", "x_id_raw", "event_name", "round", "best_of"]


def item(key, rnd, *, day="2023-03-07", event=policy.EVENT, best="7", score="11-3,11-4,11-5"):
    value = row(key, day)
    value.update(event_name=event, round=rnd, best_of=best, score=score)
    return value


def make_archive(men, women=None):
    return archive(csv_bytes(men, FIELDS), csv_bytes(women or [item("female", "R64")], FIELDS))


@pytest.fixture
def policy_document(monkeypatch):
    raw = b"test-only policy document, not the WTT PDF"
    monkeypatch.setattr(policy, "POLICY_SHA256", hashlib.sha256(raw).hexdigest())
    return raw


def inspect(raw, policy_document):
    return policy.reconcile(raw, policy_document, expected_archive_sha256=hashlib.sha256(raw).hexdigest())


def test_official_early_and_late_assumptions_do_not_rewrite_source(policy_document):
    early = item("x", "R64")
    late = item("y", "QFNL", score="11-3,11-4,11-5,11-6")
    original = make_archive([early, late])
    before = hashlib.sha256(original).hexdigest()
    result = inspect(original, policy_document)
    rounds = result["tables"][0]["rounds"]
    assert rounds["R64"]["declared_differs_policy"] == 1
    assert rounds["R64"]["score_shape_declared"] == {"nonterminal_for_declared_best_of": 1}
    assert rounds["R64"]["score_shape_policy"] == {"terminal_and_winner_consistent": 1}
    assert rounds["QFNL"]["declared_matches_policy"] == 1
    assert rounds["QFNL"]["score_shape_policy"] == {"terminal_and_winner_consistent": 1}
    assert result["retrospective"] is True and result["data_modified"] is False
    assert result["training_authorized"] is False and result["independent_match_verification"] is False
    assert hashlib.sha256(original).hexdigest() == before


def test_unknown_rounds_wrong_dates_and_other_events_not_used(policy_document):
    records = [item("a", "8FNL"), item("b", "RND1"), item("c", "arbitrary secret label"),
               item("d", "R64", day="2024-03-07"), item("e", "R64", day="2023-03-07T10:00:00Z"),
               item("f", "R64", event="Singapore Smash 2024")]
    result = inspect(make_archive(records), policy_document)
    table = result["tables"][0]
    assert table["event_name_label_rows"] == 5
    assert table["excluded_invalid_or_outside_2023_date_label"] == 2
    assert table["excluded_round_labels"] == {"8FNL": 1, "RND1": 1, "<other_or_missing>": 1}
    assert sum(x["selected"] for x in table["rounds"].values()) == 0
    assert "arbitrary secret label" not in json.dumps(result)


def test_missing_and_invalid_scores_stay_nonterminal_or_unknown(policy_document):
    records = [item("a", "R32", best="", score="11-3,11-4,11-5"),
               item("b", "R32", best="7", score="11-10"),
               item("c", "FNL", best="5", score="11-3,11-4,11-5")]
    groups = inspect(make_archive(records), policy_document)["tables"][0]["rounds"]
    assert groups["R32"]["declared_missing"] == 1
    assert groups["R32"]["score_shape_policy"] == {"invalid_set_score": 1, "terminal_and_winner_consistent": 1}
    assert groups["FNL"]["declared_differs_policy"] == 1
    assert groups["FNL"]["score_shape_policy"] == {"nonterminal_for_declared_best_of": 1}


def test_pinned_policy_and_archive_reject_tampering(policy_document):
    raw = make_archive([item("x", "R64")]); digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="policy document SHA-256"):
        policy.reconcile(raw, policy_document + b"x", expected_archive_sha256=digest)
    with pytest.raises(ValueError, match="source archive SHA-256"):
        policy.reconcile(raw, policy_document, expected_archive_sha256="0" * 64)


def test_no_fixture_ids_or_names_are_published(policy_document):
    a = item("private fixture string", "R32"); a["player1_name"] = "private player string"
    out = json.dumps(inspect(make_archive([a]), policy_document))
    assert "private fixture string" not in out and "private player string" not in out
