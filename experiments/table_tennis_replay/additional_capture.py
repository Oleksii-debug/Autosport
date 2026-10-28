"""Record 30 new upcoming matches without changing the running original 20."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import time

import record


def fixture_key(item):
    return (tuple(sorted((item['home'], item['away']))), item['league'],
            record.instant(item.get('kickoff_utc') or item['scheduled_at']))


def choose(board, original, now, count=30):
    excluded_ids = {str(m['id']) for m in original['matches']}
    excluded_keys = {fixture_key(m) for m in original['matches']}
    chosen = []
    seen_ids = set(excluded_ids)
    seen_keys = set(excluded_keys)
    candidates = []
    for item in board.get('matches', []):
        try:
            start = record.instant(item['kickoff_utc'])
            if item.get('in_play') or not now + timedelta(minutes=3) <= start <= now + timedelta(hours=3):
                continue
            if item.get('sport') != 'table_tennis':
                continue
            prices = record.market(item)
            for side in ('home', 'away'):
                quote = prices[side] or {}
                value = quote.get('odds')
                checked = record.instant(quote['market_checked_at'])
                if (not isinstance(value, (int, float)) or isinstance(value, bool)
                        or not math.isfinite(value) or value <= 1 or quote.get('stale')
                        or quote.get('suspended') or not 0 <= (now - checked).total_seconds() <= 120):
                    break
            else:
                candidates.append(item)
        except (KeyError, TypeError, ValueError):
            continue
    for item in sorted(candidates, key=lambda x: (record.instant(x['kickoff_utc']), str(x['id']))):
        identity, key = str(item['id']), fixture_key(item)
        if identity in seen_ids or key in seen_keys:
            continue
        chosen.append(item)
        seen_ids.add(identity)
        seen_keys.add(key)
        if len(chosen) == count:
            break
    if len(chosen) != count:
        raise ValueError(f'only {len(chosen)} eligible upcoming matches; require {count}')
    return {'schema': 'autosport.table_tennis_study.v1', 'source': record.BASE,
            'captured_at': now.isoformat(), 'source_generated_at': board.get('generated_at'),
            'matches': chosen, 'excluded_original_ids': sorted(excluded_ids)}


def make_state(selection):
    matches = []
    for item in selection['matches']:
        matches.append({'id': str(item['id']), 'home': item['home'], 'away': item['away'],
                        'league': item['league'], 'scheduled_at': item['kickoff_utc'],
                        'prematch': {'captured_at': selection['captured_at'], 'odds': record.market(item)},
                        'first_live_at': None, 'live_id': None, 'status': 'Очікується',
                        'frames': [], 'result': None})
    if len(matches) != 30 or len({m['id'] for m in matches}) != 30:
        raise ValueError('additional batch must have exactly 30 distinct matches')
    return {'schema': 'autosport.table_tennis_recording.v1', 'source': record.BASE,
            'source_documentation': record.BASE + '/doc',
            'selection_captured_at': selection['captured_at'], 'poll_seconds': 20,
            'frame_seconds': 180, 'matches': matches, 'errors': [],
            'recording_complete': False, 'coverage_complete': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original-selection', type=Path, required=True)
    parser.add_argument('--selection-output', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--select-only', action='store_true')
    args = parser.parse_args()
    original = json.loads(args.original_selection.read_text('utf-8'))
    if args.output.exists() or args.selection_output.exists():
        raise ValueError('fresh extra batch requires new output paths; existing evidence is not overwritten')
    for attempt in range(5):
        try:
            board, digest = record.fetch('/odds', {'sport': 'table_tennis'})
            selection = choose(board, original, datetime.now(timezone.utc))
            selection['source_snapshot_sha256'] = digest
            break
        except (OSError, ValueError) as exc:
            print(json.dumps({'selection_attempt': attempt + 1, 'error': str(exc)}), flush=True)
            if attempt == 4:
                raise
            time.sleep(30)
    state = make_state(selection)
    record.save(args.selection_output, selection)
    record.save(args.output, state)
    print(json.dumps({'selected': len(state['matches']), 'first': state['matches'][0]['scheduled_at'],
                      'last': state['matches'][-1]['scheduled_at']}), flush=True)
    if args.select_only:
        return
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        while True:
            try:
                board_job = pool.submit(record.fetch, '/odds', {'sport': 'table_tennis'})
                result_job = pool.submit(record.fetch, '/results', {'sport': 'table_tennis'})
                board, digest = board_job.result()
                outcomes, _ = result_job.result()
                record.cycle(state, board, outcomes, now=datetime.now(timezone.utc), board_sha=digest)
                print(json.dumps({'checked_at': state['last_checked_at'],
                                  'started': sum(bool(m['first_live_at']) for m in state['matches']),
                                  'finished': sum(bool(m['result']) for m in state['matches'])}), flush=True)
            except (OSError, ValueError, KeyError) as exc:
                state['errors'].append({'observed_at': datetime.now(timezone.utc).isoformat(),
                                        'type': type(exc).__name__})
            record.save(args.output, state)
            if state['recording_complete'] or time.monotonic() - start >= 16200:
                break
            time.sleep(20)


if __name__ == '__main__':
    main()
