from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from dated_result_audit import audit
from prospective_capture import collect


def tick_clock():
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    while True:
        yield now
        now += timedelta(seconds=1)


def setup_session(tmp_path):
    ticks = tick_clock()
    def fetch(url):
        if '/odds?' in url:
            return json.dumps({'matches': [{'id': 'fixture-1', 'in_play': True}]}).encode()
        return b'{"sport":"table_tennis","results":[]}'
    collect(tmp_path, cycles=1, fetch=fetch, clock=lambda: next(ticks), sleep=lambda _: None)


def result_row(identity='fixture-1', finished='2026-10-29T00:00:02Z', updated='2026-10-29T00:00:04Z'):
    return {'fixture_id': identity, 'sport': 'table_tennis', 'home': 'A', 'away': 'B',
            'league': 'L', 'status': 'finished', 'periods': [{'score': '11-2'}] * 3,
            'final_score': '11-2,11-2,11-2', 'finished_at': finished, 'updated_at': updated}


def payload(rows):
    raw = json.dumps({'sport': 'table_tennis', 'date': '2026-10-29', 'results': rows}).encode()
    receipt = {'schema': 'autosport.tt_dated_followup.v1',
               'url': 'https://moviodds.com/results?sport=table_tennis&date=2026-10-29',
               'requested_at': '2026-10-29T00:00:05Z', 'received_at': '2026-10-29T00:00:06Z',
               'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
               'rights_verified': False, 'identity_verified': False, 'executable_price_verified': False}
    return raw, receipt


def test_exact_id_and_later_receipt_only(tmp_path):
    setup_session(tmp_path)
    raw, receipt = payload([result_row(), result_row('different-id')])
    report = audit(tmp_path, raw, receipt)
    assert report['prior_distinct_live_fixture_ids'] == 1
    assert report['same_fixture_id'] == 1
    assert report['matching_last_live_at_or_after_observation'] == 1
    assert report['training_authorized'] is False


def test_future_update_and_old_last_live_do_not_count_as_later_outcome(tmp_path):
    setup_session(tmp_path)
    for row, key in ((result_row(updated='2026-10-29T00:00:07Z'), 'matching_source_update_after_receipt'),
                     (result_row(finished='2026-10-29T00:00:00Z'), 'matching_last_live_before_observation')):
        raw, receipt = payload([row]); report = audit(tmp_path, raw, receipt)
        assert report[key] == 1
        assert report['matching_last_live_at_or_after_observation'] == 0


def test_rejects_tampering_source_change_and_backdated_followup(tmp_path):
    setup_session(tmp_path)
    raw, receipt = payload([result_row()])
    with pytest.raises(ValueError, match='receipt mismatch'):
        audit(tmp_path, raw+b' ', receipt)
    bad = dict(receipt, url='https://other.example/results?sport=table_tennis&date=2026-10-29')
    with pytest.raises(ValueError, match='source'):
        audit(tmp_path, raw, bad)
    bad = dict(receipt, requested_at='2026-10-29T00:00:03Z')
    with pytest.raises(ValueError, match='after prior capture'):
        audit(tmp_path, raw, bad)


def test_duplicate_result_fixture_ids_are_rejected(tmp_path):
    setup_session(tmp_path)
    raw, receipt = payload([result_row(), result_row()])
    with pytest.raises(ValueError, match='duplicate fixture ID'):
        audit(tmp_path, raw, receipt)
