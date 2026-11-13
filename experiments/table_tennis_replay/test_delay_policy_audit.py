from copy import deepcopy
from decimal import Decimal
import hashlib
import json

import pytest

from delay_policy_audit import _classify, audit
from test_quote_delay_audit import build_session, match


def test_exact_threshold_and_no_rounding():
    d = Decimal
    unchanged = (d("2"), d("2"))
    assert _classify((d("2"), d("1.9599999999999999999999999999999999")), unchanged) == "below_98_percent"
    assert _classify((d("2"), d("1.96")), unchanged) == "from_98_to_below_100_percent"
    assert _classify((d("2"), d("2")), unchanged) == "at_least_100_percent"
    assert _classify((d("2"), d("2.01")), unchanged) == "at_least_100_percent"
    assert _classify(None, unchanged) == "missing_or_invalid"


def test_all_pairs_partition_and_frozen_first_pair(tmp_path):
    # Deliberately shuffled: the first pair is a/b, not the first two list entries
    # and not whichever two retain their prices in the next snapshot.
    initial = [match("c", 0), match("b", 0), match("a", 0)]
    later = [match("a", 40, 1.4), match("c", 40)]
    build_session(tmp_path, [(0, initial), (40, later)])
    report = audit(tmp_path)
    assert report["all_pairs"] == {"initial_pairs": 3, "missing_or_invalid": 2,
                                   "below_98_percent": 1, "from_98_to_below_100_percent": 0,
                                   "at_least_100_percent": 0}
    assert report["first_pair_per_transition"] == {"initial_pairs": 1, "missing_or_invalid": 1,
                                                   "below_98_percent": 0, "from_98_to_below_100_percent": 0,
                                                   "at_least_100_percent": 0}
    assert report["displayed_prices_are_fills"] is False
    assert report["real_money_execution"] is False


def test_initial_favorite_is_not_reselected_from_future(tmp_path):
    initial = [match("a", 0), match("b", 0)]
    # The new favorite is the opposite side for a; compare original selection.
    later = [match("a", 40, 2.0, 1.1), match("b", 40, 1.5, 2.5)]
    build_session(tmp_path, [(0, initial), (40, later)])
    assert audit(tmp_path)["first_pair_per_transition"]["at_least_100_percent"] == 1


def test_original_decimal_precision_controls_frozen_favorite(tmp_path):
    first = [match("a", 0, 1.5, 1.5), match("b", 0)]
    later = [match("a", 40, 1.4, 1.5), match("b", 40)]
    build_session(tmp_path, [(0, first), (40, later)])
    body = tmp_path / "0001.odds.body.json"
    # Both numbers collapse to 1.5 in binary float. Exact JSON has away as the
    # shorter price, so the unchanged away selection must be frozen instead.
    raw = body.read_bytes().replace(b'"odds_dec": 1.5',
                                    b'"odds_dec": 1.500000000000000000000000001', 1)
    body.write_bytes(raw)
    receipt_path = tmp_path / "0001.odds.receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["sha256"], receipt["bytes"] = hashlib.sha256(raw).hexdigest(), len(raw)
    receipt_path.write_text(json.dumps(receipt))
    assert audit(tmp_path)["first_pair_per_transition"]["at_least_100_percent"] == 1


@pytest.mark.parametrize("mutation", ["missing", "suspended", "stale", "selection", "reversed"])
def test_unknown_later_state_never_becomes_fill_or_loss(tmp_path, mutation):
    first = [match("a", 0), match("b", 0)]
    later = [match("a", 40), match("b", 40)]
    if mutation == "missing": later.pop()
    elif mutation == "suspended": later[1]["quotes"][0]["suspended"] = True
    elif mutation == "stale": later[1]["group_status"]["240"]["stale"] = True
    elif mutation == "selection": later[1]["quotes"][1]["sel_id"] = "other"
    else: later[1]["home"], later[1]["away"] = later[1]["away"], later[1]["home"]
    build_session(tmp_path, [(0, first), (40, later)])
    assert audit(tmp_path)["first_pair_per_transition"]["missing_or_invalid"] == 1


def test_out_of_window_and_zero_candidates_are_not_interpolated(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0)]), (61, [match("a", 61)])], interval=61)
    report = audit(tmp_path)
    assert report["intervals_outside_30_60_seconds"] == 1
    assert report["all_pairs"]["initial_pairs"] == 0
    assert not report["transitions"][0]["included"]


def test_tampering_and_duplicate_fixture_fail_closed(tmp_path):
    a = match("a", 0)
    build_session(tmp_path, [(0, [a, deepcopy(a)]), (40, [match("a", 40)])])
    with pytest.raises(ValueError, match="duplicate fixture"):
        audit(tmp_path)
    (tmp_path / "0001.odds.body.json").write_text("{}")
    with pytest.raises(ValueError, match="receipt does not bind"):
        audit(tmp_path)
