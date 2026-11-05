import copy
import unittest

from h2h_exploratory import analyze


def fixture(ident, home, away, start, result_at, home_wins=True):
    sets = ["11-8" if home_wins else "8-11"] * 3
    quote = {"home": {"odds": 1.5}, "away": {"odds": 3.0}}
    return {"id": ident, "home": home, "away": away, "league": "L",
            "first_live_at": start, "frames": [{"kind": "live", "offset_seconds": 0,
             "observed_at": start, "odds": quote}],
            "result": {"observed_at": result_at, "periods": [{"score": s} for s in sets]}}


class H2HTests(unittest.TestCase):
    def setUp(self):
        self.a = fixture("1", "A", "B", "2026-01-01T10:00:00Z", "2026-01-01T10:20:00Z")
        self.b = fixture("2", "B", "A", "2026-01-01T10:30:00Z", "2026-01-01T10:50:00Z")

    def test_prior_orientation_and_same_cohort(self):
        report = analyze({"matches": [self.a, self.b]})
        self.assertEqual(report["with_prior_meetings"], 1)
        self.assertEqual(report["rows"][1]["prior_home_wins"], 0)
        self.assertLess(report["rows"][1]["h2h_probability"], report["rows"][1]["market_probability"])

    def test_future_and_same_instant_are_excluded(self):
        self.a["result"]["observed_at"] = self.b["first_live_at"]
        self.assertEqual(analyze({"matches": [self.a, self.b]})["with_prior_meetings"], 0)
        self.a["result"]["observed_at"] = "2026-01-01T10:31:00Z"
        self.assertEqual(analyze({"matches": [self.a, self.b]})["with_prior_meetings"], 0)

    def test_suspended_and_different_league_excluded(self):
        self.a["league"] = "other"
        self.assertEqual(analyze({"matches": [self.a, self.b]})["with_prior_meetings"], 0)
        self.b["frames"][0]["odds"]["home"]["suspended"] = True
        self.assertEqual(analyze({"matches": [self.a, self.b]})["scored"], 1)

    def test_duplicate_and_invalid_terminal(self):
        with self.assertRaises(ValueError):
            analyze({"matches": [self.a, copy.deepcopy(self.a)]})
        self.a["result"]["periods"].append({"score": "11-9"})
        self.assertEqual(analyze({"matches": [self.a, self.b]})["with_prior_meetings"], 0)


if __name__ == "__main__":
    unittest.main()
