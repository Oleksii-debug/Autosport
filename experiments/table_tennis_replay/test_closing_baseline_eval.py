from datetime import datetime, timezone
import unittest

from closing_baseline_eval import market_probability


def quote(side, odds, ts='2026-01-03T09:59:00Z'):
    return {'group_id': '920000', 'group_name': 'To Win Match', 'market_id': '920000',
            'line': 'To Win', 'side': side,
            'pre_close': {'ts': ts, 'odds_dec': odds, 'suspended': False}}


def data():
    return {'fixture_id': '42', 'closed': True, 'kickoff_known': True, 'kickoff_estimated': False,
            'pre_close_basis': 'kickoff', 'kickoff_utc': '2026-01-03T10:00:00Z',
            'selections': [quote('A', 2), quote('B', 4)]}


class ClosingTest(unittest.TestCase):
    def setUp(self):
        self.row = {'fixture_id': '42', 'home': 'A', 'away': 'B'}
        self.cutoff = datetime(2026, 1, 3, tzinfo=timezone.utc)

    def test_fresh_pair_uses_normalized_implied_probability(self):
        p, reason = market_probability(data(), self.row, self.cutoff)
        self.assertIsNone(reason); self.assertAlmostEqual(p, 2 / 3)

    def test_provisional_or_unknown_closing_is_rejected(self):
        for value in (False, None):
            d = data(); d['closed'] = value
            self.assertEqual(market_probability(d, self.row, self.cutoff)[1], 'closing_not_final')

    def test_stale_suspended_and_same_instant_excluded(self):
        for change in ('stale', 'suspended', 'same'):
            d = data()
            if change == 'stale': d['selections'][0]['pre_close']['ts'] = '2026-01-03T09:00:00Z'
            if change == 'suspended': d['selections'][0]['pre_close']['suspended'] = True
            if change == 'same': d['selections'][0]['pre_close']['ts'] = d['kickoff_utc']
            self.assertEqual(market_probability(d, self.row, self.cutoff)[1], 'missing_fresh_pair')

    def test_ambiguous_market_or_leaking_kickoff_excluded(self):
        d = data(); d['selections'].append(quote('A', 3))
        self.assertEqual(market_probability(d, self.row, self.cutoff)[1], 'ambiguous_market')
        d = data(); d['kickoff_utc'] = '2026-01-02T23:50:00Z'
        self.assertEqual(market_probability(d, self.row, self.cutoff)[1], 'kickoff_before_training_cutoff')
        d = data(); d['kickoff_utc'] = '2026-01-03T00:05:00Z'
        d['selections'][0]['pre_close']['ts'] = '2026-01-02T23:59:00Z'
        d['selections'][1]['pre_close']['ts'] = '2026-01-03T00:04:00Z'
        self.assertEqual(market_probability(d, self.row, self.cutoff)[1], 'missing_fresh_pair')


if __name__ == '__main__':
    unittest.main()
