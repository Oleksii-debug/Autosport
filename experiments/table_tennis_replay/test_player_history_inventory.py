import copy
import unittest
from player_history_inventory import build


class HistoryTests(unittest.TestCase):
    def test_reversed_names_and_dedup(self):
        a={'fixture_id':'1','home':'A','away':'B','league':'L','status':'finished',
           'periods':[{'score':'11-5'}]*3,'finished_at':'2026-01-01T10:00:00Z',
           'updated_at':'2026-01-01T10:01:00Z'}
        b={**a,'fixture_id':'2','home':'B','away':'A'}
        d={'sport':'table_tennis','results':[a,b]}
        r=build(d)
        self.assertEqual(len(r['pairs']),1)
        self.assertEqual(r['pairs'][0]['a_wins'],1)
        self.assertEqual(r['players'][0]['descriptive_win_fraction'],0.5)
        self.assertFalse(r['identity_verified'])
        self.assertFalse(r['causal_training_eligible'])
        d['results'].append(copy.deepcopy(a))
        with self.assertRaises(ValueError):build(d)

    def test_unknown_status_is_not_a_loss(self):
        r=build({'sport':'table_tennis','results':[{'fixture_id':'1','status':'unknown'}]})
        self.assertEqual(r['players'],[])
        self.assertEqual(sum(r['excluded'].values()),1)


if __name__ == '__main__':unittest.main()
