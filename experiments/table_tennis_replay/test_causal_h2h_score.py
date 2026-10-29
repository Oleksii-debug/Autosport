from datetime import timedelta
import hashlib
import json
import math

import pytest

from causal_h2h_score import evaluate
from prospective_capture import collect
from test_delayed_portfolio_replay import followup, later_result, set_session_results
from test_quote_delay_audit import BASE, match, stamp


def observation(root, ids=("a",), probability="0.75"):
    session = root / "snapshots"
    times = iter(BASE + timedelta(seconds=s) for s in (0, 1, 2, 3))
    def fetch(url):
        if "/odds?" in url:
            return json.dumps({"server_time": stamp(0), "matches": [match(i, 0, 1.5, 4.5) for i in ids]}).encode()
        return b'{"sport":"table_tennis","results":[]}'
    collect(session, cycles=1, fetch=fetch, clock=lambda: next(times), sleep=lambda _: None)
    odds_receipt = json.loads((session / "0001.odds.receipt.json").read_text())
    forecasts = [{"fixture_id": i, "market_home_probability": probability,
                  "h2h_status": "identity_unresolved", "h2h_home_probability": None} for i in ids]
    journal = {"feature_cutoff": stamp(1.25), "odds_sha256": odds_receipt["sha256"],
               "prior_result_sha256": "a" * 64, "counts": {"forecast_fixtures": len(ids)},
               "forecasts": forecasts}
    raw = json.dumps(journal).encode()
    envelope = {"schema": "research.tt_forecast_receipt.v1", "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "odds_sha256": odds_receipt["sha256"],
                "prior_result_sha256": "a" * 64, "computed_at": stamp(1.25), "frozen_at": stamp(1.5)}
    (root / "forecast.body.json").write_bytes(raw)
    (root / "forecast.receipt.json").write_text(json.dumps(envelope))


def test_same_subset_market_and_neutral_brier_and_log_loss(tmp_path):
    observation(tmp_path, ids=("a", "b"))
    raw, receipt = followup([later_result("a", "home"), later_result("b", "away")])
    result = evaluate(tmp_path, raw, receipt)
    assert result["counts"]["eligible"] == 2
    assert result["market"]["brier"] == pytest.approx((0.25**2 + 0.75**2) / 2)
    assert result["neutral"]["brier"] == 0.25
    assert result["market"]["log_loss"] == pytest.approx(-(math.log(0.75) + math.log(0.25)) / 2)
    assert result["h2h_scored"] == 0
    assert result["execution_verified"] is result["profitability_claim"] is False


def test_missing_outcome_yields_null_metrics_not_loss(tmp_path):
    observation(tmp_path)
    report = evaluate(tmp_path, *followup([]))
    assert report["counts"]["not_present_in_followup"] == 1
    assert report["market"] is report["neutral"] is None


@pytest.mark.parametrize("case", ["wrong_name", "last_live_before_journal_receipt", "future_update", "bad_score"])
def test_temporal_or_identity_inconsistency_is_excluded(tmp_path, case):
    observation(tmp_path)
    a = later_result("a")
    if case == "wrong_name": a["home"] = "other"
    elif case == "last_live_before_journal_receipt": a["finished_at"] = stamp(1.75)
    elif case == "future_update": a["updated_at"] = stamp(101)
    else: a["final_score"] = "11-8,11-8,11-8"
    report = evaluate(tmp_path, *followup([a]))
    assert report["counts"]["eligible"] == 0
    assert report["market"] is None


def test_conflicting_intermediate_result_is_quarantined(tmp_path):
    observation(tmp_path)
    earlier = later_result("a", "away", finished=2.5, updated=2.5)
    set_session_results(tmp_path / "snapshots", 1, [earlier])
    report = evaluate(tmp_path, *followup([later_result("a", "home")]))
    assert report["counts"]["identity_time_or_correction_conflict"] == 1
    assert report["market"] is None


def test_invalid_frozen_probability_is_not_scored(tmp_path):
    observation(tmp_path, probability="1")
    report = evaluate(tmp_path, *followup([later_result("a")]))
    assert report["counts"]["invalid_probability"] == 1
    assert report["market"] is None


def test_frozen_probability_must_match_original_source_quotes(tmp_path):
    observation(tmp_path, probability="0.60")
    with pytest.raises(ValueError, match="differs from source quotes"):
        evaluate(tmp_path, *followup([later_result("a")]))


def test_followup_tampering_is_rejected(tmp_path):
    observation(tmp_path)
    raw, receipt = followup([later_result("a")])
    with pytest.raises(ValueError, match="receipt mismatch"):
        evaluate(tmp_path, raw + b" ", receipt)
