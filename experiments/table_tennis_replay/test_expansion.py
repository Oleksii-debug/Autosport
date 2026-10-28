"""Check that expansion selects fresh events and preserves the original evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

import additional_capture as capture
from combine_recordings import combine


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 17, tzinfo=timezone.utc)

    def fixture(self, number):
        return {
            'id': str(number), 'home': f'Home {number}', 'away': f'Away {number}',
            'league': 'Table tennis', 'sport': 'table_tennis', 'in_play': False,
            'kickoff_utc': (self.now + timedelta(minutes=5+number)).isoformat(),
            'group_status': {'920000': {'polled_at': self.now.isoformat()}},
            'quotes': [{'group_id': '920000', 'line': 'To Win',
                        'side': f'{side} {number}', 'odds_dec': odds}
                       for side, odds in [('Home', 1.8), ('Away', 2.0)]],
        }

    def test_selection_excludes_original_live_stale_suspended_and_duplicate(self):
        original = {'matches': [self.fixture(0)]}
        items = [self.fixture(i) for i in range(1, 36)]
        items[0]['in_play'] = True
        items[1]['group_status']['920000']['stale'] = True
        items[2]['quotes'][0]['suspended'] = True
        items[3]['group_status']['920000']['polled_at'] = (self.now-timedelta(minutes=3)).isoformat()
        selected = capture.choose({'matches': [self.fixture(0), *items, deepcopy(items[4])]}, original, self.now)
        self.assertEqual(len(selected['matches']), 30)
        self.assertEqual([m['id'] for m in selected['matches']], [str(i) for i in range(5, 35)])
        state = capture.make_state(selected)
        self.assertFalse(state['recording_complete'])
        self.assertTrue(all(not m['frames'] and not m['first_live_at'] for m in state['matches']))

    def test_insufficient_selection_fails_without_filling_with_live_matches(self):
        with self.assertRaises(ValueError):
            capture.choose({'matches': [self.fixture(1)]}, {'matches': []}, self.now)

    def batches(self):
        def batch(start, end):
            matches = []
            for i in range(start, end):
                m = self.fixture(i)
                m.update(scheduled_at=m.pop('kickoff_utc'), result={'final_score': '3-1'},
                         frames=[{'kind': 'live', 'offset_seconds': 0, 'score': '1-2'}])
                matches.append(m)
            return {'schema': 'autosport.table_tennis_recording.v1', 'matches': matches,
                    'errors': [], 'coverage_complete': True, 'last_checked_at': self.now.isoformat()}
        return batch(0, 20), batch(20, 50)

    def test_combine_preserves_frames_and_inputs(self):
        original, extra = self.batches()
        before = deepcopy((original, extra))
        combined = combine(original, extra)
        self.assertEqual(len(combined['matches']), 50)
        self.assertTrue(combined['recording_complete'])
        self.assertEqual(combined['matches'], original['matches']+extra['matches'])
        combined['matches'][0]['frames'][0]['score'] = 'mutated output'
        self.assertEqual((original, extra), before)

    def test_combine_rejects_alias_overlap_and_preserves_incomplete_status(self):
        original, extra = self.batches()
        extra['matches'][0]['live_id'] = original['matches'][0]['id']
        with self.assertRaises(ValueError):
            combine(original, extra)
        del extra['matches'][0]['live_id']
        extra['matches'][0]['result'] = None
        combined = combine(original, extra)
        self.assertFalse(combined['recording_complete'])
        self.assertFalse(combined['coverage_complete'])


if __name__ == '__main__':
    unittest.main()
