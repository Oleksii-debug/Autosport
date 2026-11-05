import copy
import unittest

from h2h_archive_probe import compare
from test_h2h_exploratory import fixture


class ArchiveProbeTests(unittest.TestCase):
    def setUp(self):
        self.recording = {"matches": [fixture("new", "A", "B", "2026-01-02T10:00:00Z", "2026-01-02T10:20:00Z")]}
        self.item = {"fixture_id": "old", "home": "B", "away": "A", "league": "L",
                     "status": "finished", "finished_at": "2026-01-01T10:00:00Z",
                     "updated_at": "2026-01-01T10:01:00Z",
                     "periods": [{"score": "8-11"}] * 3}
        self.archive = {"sport": "table_tennis", "results": [self.item]}

    def test_reverse_order_and_assumed_availability(self):
        r = compare(self.recording, self.archive, "2026-01-02T09:00:00Z")
        self.assertEqual(r["matched_records"], 1)
        self.assertEqual(r["rows"][0]["archive_prior_home_wins"], 1)
        self.assertFalse(r["available_at_verified"])
        self.assertFalse(r["holdout"])
        self.assertEqual(compare(self.recording, self.archive, "2026-01-02T10:00:00Z")["matched_records"], 0)

    def test_future_archive_update_and_sport(self):
        self.item["updated_at"] = "2026-01-02T09:01:00Z"
        self.assertEqual(compare(self.recording, self.archive, "2026-01-02T09:00:00Z")["matched_records"], 0)
        self.archive["sport"] = "football"
        with self.assertRaises(ValueError):
            compare(self.recording, self.archive, "2026-01-02T09:00:00Z")

    def test_duplicate_and_overlapping_ids_rejected(self):
        self.archive["results"].append(copy.deepcopy(self.item))
        with self.assertRaises(ValueError):
            compare(self.recording, self.archive, "2026-01-02T09:00:00Z")
        self.archive["results"] = [self.item]
        self.item["fixture_id"] = "new"
        with self.assertRaises(ValueError):
            compare(self.recording, self.archive, "2026-01-02T09:00:00Z")


if __name__ == "__main__":
    unittest.main()
