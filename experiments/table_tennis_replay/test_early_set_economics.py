"""Adversarial checks on the early-set replay; no real wagers."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from early_set_economics import _score, _set_skill, analyze, decide, match_probability


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat()


def prices(home, away, checked):
    return {
        side: {"odds": price, "market_checked_at": at(checked),
               "stale": False, "suspended": False}
        for side, price in (("home", home), ("away", away))
    }


def frame(seen, score, *, checked=None, source="live"):
    return {"kind": "live", "source_id": source, "source_stale": False,
            "observed_at": at(seen), "source_last_live_seen": at(seen),
            "score": score, "odds": prices(2.4, 1.6, seen if checked is None else checked)}


def prematch():
    return {"captured_at": at(0), "odds": prices(1.9, 1.9, -1)}


def recording():
    return {
        "schema": "autosport.table_tennis_recording.v1", "source": "https://moviodds.com",
        "matches": [{"id": "pre", "live_id": "live", "league": "Test",
                     "prematch": prematch(), "first_live_at": at(5),
                     "frames": [frame(5, "1-1"), frame(15, "11-5,2-2")],
                     "result": {"source_fixture_id": "live", "observed_at": at(51),
                                "source_finished_at": at(50), "final_score": "11-5,11-7,11-8",
                                "periods": [{"score": s} for s in ("11-5", "11-7", "11-8")]}}],
    }


def evaluate(data, leagues=("Test",)):
    raw = json.dumps(data).encode()
    return analyze(raw, expected_sha256=hashlib.sha256(raw).hexdigest(),
                   assumed_best_of_five_leagues=leagues)


def test_equal_prematch_anchor_and_set_update_are_distinct():
    for weight in (None, 8):
        q = _set_skill(.67, prior_sets=weight)
        assert match_probability(q, prior_sets=weight) == pytest.approx(.67)
        assert match_probability(q, 1, 0, prior_sets=weight) == pytest.approx(
            1 - match_probability(1 - q, 0, 1, prior_sets=weight))
    assert match_probability(.5, 1, 0) == pytest.approx(.6875)
    assert match_probability(.5, 1, 0, prior_sets=8) > .6875


@pytest.mark.parametrize("raw", ("12-9", "11-10,0-0", "11-9,11-9,11-9,0-0", "11:4"))
def test_nonterminal_prior_sets_and_impossible_scores_do_not_qualify(raw):
    assert _score(raw) is None


def test_first_early_snapshot_requires_fresh_pair_and_no_future_observations():
    snapshots = [frame(5, "11-7,5-3"), frame(10, "11-7,0-0", checked=11),
                 frame(15, "12-10,0-0")]
    snapshots[1]["odds"]["away"]["market_checked_at"] = at(10)
    category, first = decide(prematch(), snapshots, "live", first_live_at=at(5))
    assert category == "selected_frame" and first["time"].isoformat() == at(15)
    # An after-decision record cannot disqualify an earlier trade.
    assert decide(prematch(), snapshots + [frame(90, "bad", source="later")],
                  "live", first_live_at=at(5)) == (category, first)
    assert decide(prematch(), [frame(5, "11-7", source="wrong"), *snapshots],
                  "live", first_live_at=at(5))[0] == "live_id_or_chronology_conflict"


def test_no_result_is_never_treated_as_a_loss_or_a_gain():
    data = recording()
    tally = evaluate(data)["policies"]["adaptive_skill"]
    assert tally["paper_selected"] == tally["paper_settled"] == tally["paper_won"] == 1
    assert Decimal(tally["paper_net_units"]) == Decimal("1.342")
    assert Decimal(tally["stress_net_units"]) == Decimal("1.27")
    data["matches"][0]["frames"][1]["score"] = "11-6,2-2"
    assert evaluate(data)["policies"]["adaptive_skill"]["paper_unresolved"] == 1
    data["matches"][0]["frames"][1]["score"] = "11-5,2-2"
    data["matches"][0]["result"]["source_fixture_id"] = "foreign"
    tally = evaluate(data)["policies"]["adaptive_skill"]
    assert tally["paper_selected"] == tally["paper_unresolved"] == 1
    assert tally["paper_settled"] == tally["scored_matches"] == 0
    assert tally["brier"] is None and Decimal(tally["paper_net_units"]) == 0
    assert Decimal(tally["paper_net_if_unresolved_lose_units"]) == Decimal("-1.01")


def test_outcome_cannot_create_format_scope_or_exposure():
    data = recording()
    assert evaluate(data, ("Other league",))["counts"] == {"format_out_of_scope": 1}
    data["matches"][0]["result"]["source_finished_at"] = at(13)
    assert evaluate(data)["policies"]["fixed_skill"]["paper_unresolved"] == 1
    data["matches"].append({**data["matches"][0], "id": "another"})
    with pytest.raises(ValueError, match="duplicate"):
        evaluate(data)
