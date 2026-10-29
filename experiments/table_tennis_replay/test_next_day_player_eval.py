import copy
import unittest

from next_day_player_eval import evaluate
from test_canonical_history_bridge import fixture


def row(ident, home, away, day, home_wins=True):
    data = fixture()
    scores = ['11-8' if home_wins else '8-11'] * 3
    data.update(fixture_id=ident, home=home, away=away,
                periods=[{'score': x} for x in scores], final_score=','.join(scores),
                finished_at=f'2026-01-0{day}T10:00:00Z', updated_at=f'2026-01-0{day}T10:01:00Z')
    return data


def archive(day, rows):
    return {'sport': 'table_tennis', 'date': f'2026-01-0{day}', 'results': rows}


class NextDayTest(unittest.TestCase):
    def test_reverse_orientation_and_test_outcomes_do_not_train(self):
        train = archive(2, [row('1', 'A', 'B', 2)])
        test = archive(3, [row('2', 'B', 'A', 3), row('3', 'B', 'A', 3)])
        result = evaluate(train, test)
        self.assertEqual(result['pair_known']['matches'], 2)
        self.assertEqual(result['rows'][0]['prior_home_wins'], 0)
        self.assertLess(result['rows'][0]['strength_h2h'], .5)
        changed = copy.deepcopy(test)
        changed['results'][0] = row('2', 'B', 'A', 3, home_wins=False)
        other = evaluate(train, changed)
        self.assertEqual(result['rows'][1]['strength_h2h'], other['rows'][1]['strength_h2h'])
        self.assertFalse(result['availability_verified'])
        self.assertFalse(result['profitability_proven'])

    def test_late_update_never_fits_and_overlapping_ids_rejected(self):
        prior = row('1', 'A', 'B', 2)
        prior['updated_at'] = '2026-01-03T00:01:00Z'
        test = archive(3, [row('2', 'A', 'B', 3)])
        result = evaluate(archive(2, [prior]), test)
        self.assertEqual(result['train_used_before_midnight'], 0)
        self.assertEqual(result['rows'][0]['strength'], .5)
        with self.assertRaises(ValueError):
            evaluate(archive(2, [row('2', 'A', 'B', 2)]), test)
        with self.assertRaises(ValueError):
            evaluate(archive(1, []), test)


if __name__ == '__main__':
    unittest.main()
