"""Combine two independent, disjoint recordings without altering their frames."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

from additional_capture import fixture_key
import record

ORIGINAL_URL = ('https://raw.githubusercontent.com/Oleksii-debug/Autosport/'
                'data/table-tennis-capture-20261028/experiments/table_tennis_replay/recording.json')


def combine(original, extra):
    batches = (original, extra)
    if any(x.get('schema') != 'autosport.table_tennis_recording.v1' for x in batches):
        raise ValueError('unsupported recording schema')
    if [len(x['matches']) for x in batches] != [20, 30]:
        raise ValueError('expected 20 original and 30 additional matches')
    seen_ids, seen_keys = set(), set()
    matches = []
    for batch in batches:
        for m in batch['matches']:
            aliases = {str(m['id'])}
            if m.get('live_id'):
                aliases.add(str(m['live_id']))
            key = fixture_key(m)
            if seen_ids & aliases or key in seen_keys:
                raise ValueError('overlapping fixture in recorded batches')
            seen_ids.update(aliases)
            seen_keys.add(key)
            matches.append(deepcopy(m))
    result = deepcopy(original)
    result['matches'] = matches
    result['errors'] = [{'batch': i, **e} for i, b in enumerate(batches) for e in b.get('errors', [])]
    result['recording_complete'] = all(m.get('result') for m in matches)
    result['coverage_complete'] = result['recording_complete'] and all(b.get('coverage_complete', False) for b in batches)
    result['batch_observations'] = [
        {'matches': len(b['matches']), 'last_checked_at': b.get('last_checked_at'),
         'sha256': hashlib.sha256(json.dumps(b, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
        for b in batches]
    times = [b['last_checked_at'] for b in batches if b.get('last_checked_at')]
    result['last_checked_at'] = max(times, key=record.instant) if times else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extra', type=Path, required=True)
    parser.add_argument('--original', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.original:
        original = json.loads(args.original.read_text('utf-8'))
    else:
        with urlopen(Request(ORIGINAL_URL, headers={'User-Agent': 'Autosport-research/1.0'}), timeout=30) as response:
            original = json.load(response)
    extra = json.loads(args.extra.read_text('utf-8'))
    record.save(args.output, combine(original, extra))


if __name__ == '__main__':
    main()
