import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from capture_closing_subset import capture, selected_ids


class CaptureTest(unittest.TestCase):
    def test_resume_checks_digests_and_never_refetches_completed(self):
        evaluation = {'rows': [{'fixture_id': '2', 'prior_meetings': 1},
                               {'fixture_id': '1', 'prior_meetings': 0}]}
        calls = []
        def fetch(url):
            calls.append(url)
            return json.dumps({'fixture_id': '2', 'selections': []}).encode()
        with TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(capture(evaluation, root, fetch=fetch)['captured'], 1)
            self.assertEqual(capture(evaluation, root, fetch=fetch)['captured'], 1)
            self.assertEqual(len(calls), 1)
            (root / '2.json').write_text('{}')
            with self.assertRaises(ValueError):
                capture(evaluation, root, fetch=fetch)

    def test_no_duplicate_or_unsafe_fixture_ids(self):
        with self.assertRaises(ValueError):
            selected_ids({'rows': [{'fixture_id': '../1', 'prior_meetings': 1}]})
        with self.assertRaises(ValueError):
            selected_ids({'rows': [{'fixture_id': '1', 'prior_meetings': 1}] * 2})


if __name__ == '__main__':
    unittest.main()
