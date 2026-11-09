from decimal import Decimal

import pytest

from live_point_total_economics import (
    AmbiguousPriceHistory, _total_at, game_probability, implied_point_skill, over_probability,
)
from record import instant
from test_early_set_economics import at


def quote(side, seconds, *, odds=1.9, suspended=False, group="240", line="76.5"):
    return {"group_id": group, "group_name": "Match", "side": side, "line": line,
            "handicap": line, "ts": at(seconds), "market_id": "same",
            "sel_id": side, "odds_dec": odds, "suspended": suspended}


def test_point_model_handles_deuce_and_matches_the_live_winner_anchor():
    assert game_probability(.6, 10, 10) == pytest.approx(.36 / .52)
    assert implied_point_skill(.5, (0, 0), (0, 0)) == pytest.approx(.5)
    assert implied_point_skill(.5, (1, 0), (0, 0)) < .5
    assert implied_point_skill(.5, (0, 1), (0, 0)) > .5


def test_total_probability_counts_the_match_ending_point():
    assert over_probability(.6, (2, 2), (10, 8), 1) == 1
    assert over_probability(.6, (2, 2), (10, 8), 2) == pytest.approx(.4)
    assert over_probability(.6, (2, 2), (10, 8), 3) == pytest.approx(.16)


def test_require_same_line_both_sides_at_same_tick_without_later_suspension():
    cutoff = instant(at(20))
    pair = [quote("Over", 15), quote("Under", 15)]
    assert _total_at(pair + [quote("Over", 25, odds=5)], cutoff) == (
        Decimal("76.5"), (Decimal("1.9"), Decimal("1.9")))
    assert _total_at(pair + [quote("Under", 16, suspended=True)], cutoff) is None
    assert _total_at([quote("Over", 15), quote("Under", 14)], cutoff) is None
    assert _total_at([quote("Over", 4), quote("Under", 4)], cutoff) is None
    assert _total_at([quote("Over", 15, group="238"), quote("Under", 15)], cutoff) is None
    assert _total_at([quote("Over", 15), quote("Under", 15, line="77.5")], cutoff) is None


def test_conflicting_same_tick_never_selects_the_better_price():
    pair = [quote("Over", 15), quote("Under", 15)]
    with pytest.raises(AmbiguousPriceHistory):
        _total_at(pair + [quote("Over", 15, odds=2.8)], instant(at(20)))
