"""Fixed historical replication of a set-count H2H correction; no execution."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError

from canonical_history_bridge import validated_rows
from forward_residual_trial import capture, load_capture, RESULTS, sha, write
from h2h_exploratory import instant
from h2h_set_margin_probe import set_anchor, set_history
from market_anchor_screen import LEAGUES, action, closing_prices, history_state, probabilities
from market_residual_calibration import fixed_action_economics
from next_day_player_eval import _paired_interval, _scores

DATES = ('2026-11-11', '2026-11-12')
RULES = ('market', 'h2h_anchor', 'h2h_set_anchor')
QUARANTINE = {'204173406', '204188383'}
ARCHIVE = 'e9e6cd0aa5df570d627c3165dfa3f37d3fb9b8bf6205c15b46a12e546383f459'


def manifest(src):
    groups = defaultdict(list)
    known = set()
    for row in src['results']:
        ident = row['fixture_id']
        if not isinstance(ident, str) or not ident.isascii() or not ident.isdecimal() or ident in known:
            raise ValueError('invalid or duplicate archive fixture')
        known.add(ident)
        if row.get('league') not in LEAGUES or ident in QUARANTINE:
            continue
        finished = instant(row['finished_at'])
        date = finished.date().isoformat()
        if date in DATES and 12 <= finished.hour < 20:
            groups[(date, finished.hour)].append(ident)
    return [{'date': day, 'finish_hour_utc': hour, 'population': len(ids),
             'ids': sorted(ids, key=lambda i: sha(('autosport-set-replication-v1|' + i).encode()))[:8]}
            for (day, hour), ids in sorted(groups.items())]


def collect(root, selected):
    attempts = []
    stop = False
    for group in selected:
        for ident in group['ids']:
            if stop:
                attempts.append({'fixture_id': ident, 'status': 'unattempted_after_source_pause'})
                continue
            status, code = 'captured', None
            try:
                capture(root, ident, 'https://moviodds.com/closing?fi=' + ident)
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
                status, code = type(exc).__name__, getattr(exc, 'code', None)
                if code in (429, 503):
                    stop = True
            attempts.append({'fixture_id': ident, 'status': status, 'http_status': code})
            if not stop:
                time.sleep(1.05)
        print(group['date'], group['finish_hour_utc'], Counter(a['status'] for a in attempts), flush=True)
    write(root, 'attempts.json', attempts)


def report(rows):
    return {'n': len(rows), 'scores': {p: _scores(rows, p) for p in RULES},
            'economics': {p: fixed_action_economics(rows, p) for p in RULES},
            'paired_brier': {p: _paired_interval(rows, 'h2h_set_anchor', p) for p in RULES[:2]}}


def forecasts(src, receipt, root, selected, attempts):
    admitted, _ = validated_rows(src)
    by_id = {r['fixture_id']: r for r, _ in admitted}
    history = [(r, w) for r, w in admitted if r['fixture_id'] not in QUARANTINE]
    known = frozenset(r['fixture_id'] for r in src['results'])
    states = {day: (history_state(history, instant(day+'T00:00:00Z'), exclude_id=None),
                    *set_history(history, day+'T00:00:00Z')) for day in DATES}
    ids = [i for g in selected for i in g['ids']]
    if ids != [a['fixture_id'] for a in attempts] or len(set(ids)) != len(ids):
        raise ValueError('missing or repeated request accounting')
    status = {a['fixture_id']: a['status'] for a in attempts}
    rows, excluded = [], []
    for group in selected:
        day = group['date']
        state, sets, meetings = states[day]
        for ident in group['ids']:
            r = by_id.get(ident)
            if status[ident] != 'captured':
                reason = 'capture_' + status[ident]
            elif r is None or instant(r['updated_at']) > instant(receipt['received_at']):
                reason = 'invalid_or_unavailable_result'
            else:
                raw = (root / (ident+'.body.json')).read_bytes()
                rec = json.loads((root / (ident+'.receipt.json')).read_bytes())
                if rec['sha256'] != sha(raw) or rec['bytes'] != len(raw) or rec['url'] != 'https://moviodds.com/closing?fi='+ident:
                    raise ValueError('closing receipt mismatch')
                try:
                    close, reason = closing_prices(json.loads(raw, parse_float=Decimal), r, day, result_ids=known)
                except (AttributeError, KeyError, TypeError, ValueError):
                    close, reason = None, 'invalid_closing_schema'
                if close and not 12 <= instant(close['kickoff']).hour < 20:
                    close, reason = None, 'kickoff_outside_window'
                if close:
                    odds = close['odds']
                    row = {'fixture_id': ident, 'day': day, 'kickoff': close['kickoff'], 'odds': list(map(str, odds)),
                           **{k: r[k] for k in ('home', 'away', 'league')}}
                    row.update(probabilities(row, float(odds[1] / sum(odds)), state))
                    row['h2h_set_anchor'], row['prior_home_sets'], row['prior_away_sets'] = set_anchor(row, sets, meetings)
                    row['actions'] = {p: action(row[p], odds) for p in RULES}
                    rows.append(row)
                    continue
            excluded.append({'fixture_id': ident, 'day': day, 'reason': reason})
    return {'archive_sha256': receipt['sha256'], 'selected': len(ids), 'exclusions': excluded, 'rows': rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('history', type=Path)
    p.add_argument('phase', choices=('select', 'collect', 'forecast', 'evaluate'))
    p.add_argument('--selection-hash')
    args = p.parse_args()
    src, receipt = load_capture(args.history, 'history', 'results', RESULTS)
    if receipt['sha256'] != ARCHIVE:
        raise ValueError('archive differs from the protocol')
    selection = manifest(src)
    selection_path = args.root / 'selection.json'
    if args.phase == 'select':
        args.root.mkdir(mode=0o700)
        write(args.root, selection_path.name, selection)
        print(json.dumps({'selected': sum(len(g['ids']) for g in selection), 'selection_sha256': sha(selection_path.read_bytes())}))
        return
    raw = selection_path.read_bytes()
    if sha(raw) != args.selection_hash or json.loads(raw) != selection:
        raise ValueError('selection differs from published commitment')
    if args.phase == 'collect':
        if (args.root / 'attempts.json').exists() or list(args.root.glob('*.receipt.json')):
            raise ValueError('collection already started')
        collect(args.root, selection)
        return
    attempts = json.loads((args.root / 'attempts.json').read_bytes())
    f = forecasts(src, receipt, args.root, selection, attempts)
    f['selection_sha256'] = args.selection_hash
    target = args.root / 'forecasts.json'
    if args.phase == 'forecast':
        write(args.root, target.name, f)
        print(json.dumps({'forecast_sha256': sha(target.read_bytes()), 'selected': f['selected'], 'eligible': len(f['rows']),
                          'exclusions': Counter(r['reason'] for r in f['exclusions'])}))
        return
    if json.loads(target.read_bytes()) != f:
        raise ValueError('forecasts differ from recorded calculation')
    valid, _ = validated_rows(src)
    labels = {r['fixture_id']: int(w == 0) for r, w in valid}
    rows = [{**r, 'outcome': labels[r['fixture_id']]} for r in f['rows']]
    result = {'forecast_sha256': sha(target.read_bytes()), 'selected': f['selected'], 'eligible': len(rows),
              'by_day': {d: report([r for r in rows if r['day'] == d]) for d in DATES}, 'pooled': report(rows),
              'prior_results_snapshot_contains_test_outcomes': True, 'retrospective': True,
              'real_money_execution': False, 'profitability_proven': False, 'rows': rows}
    write(args.root, 'evaluation.json', result)
    print(json.dumps({k:v for k,v in result.items() if k != 'rows'}))


if __name__ == '__main__':
    main()
