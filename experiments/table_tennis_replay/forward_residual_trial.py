"""Bounded prospective PAPER comparison using the frozen November 10 models."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
from urllib.request import Request, build_opener

from canonical_history_bridge import validated_rows
from h2h_exploratory import instant
from market_anchor_screen import action, history_state, LEAGUES, POLICIES, probabilities
from market_residual_calibration import fixed_action_economics, forecast
from next_day_player_eval import _paired_interval, _scores
from prospective_capture import MAX_BYTES, NoRedirect, _parse, _write_exclusive

MODELS = {
    'fixed': '5a4d8e88eaf760e380d0cef9cbbc9b9660e3789396d3e07e86a84e2786d749ec',
    'adaptive': '01074f8279715996b3a65531afbc5a0997d699d4437c7e87bf50b1b7a0946c22',
}
ACTIONS = (*POLICIES, 'fixed_mean', 'fixed_guard', 'adaptive_mean', 'adaptive_guard')
SCORES = (*POLICIES, 'fixed_mean', 'adaptive_mean')
RESULTS = 'https://moviodds.com/results?sport=table_tennis'
ODDS = 'https://moviodds.com/odds?sport=table_tennis&groups=920000'
START_MINUTES, END_MINUTES, MAX_FIXTURES = 4, 22, 32


def now():
    return datetime.now(timezone.utc)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(root, name, obj):
    _write_exclusive(root / name, (json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode())


def capture(root, name, url):
    if any((root / f'{name}.{suffix}.json').exists() for suffix in ('body', 'receipt')):
        raise ValueError('capture already exists')
    requested = now()
    req = Request(url, headers={'User-Agent': 'Autosport-table-tennis-research/1.0', 'Accept-Encoding': 'gzip'})
    with build_opener(NoRedirect()).open(req, timeout=25) as response:
        encoding = response.headers.get('Content-Encoding', 'identity')
        raw = response.read(MAX_BYTES + 1)
    received = now()
    if not 0 < len(raw) <= MAX_BYTES or encoding not in ('gzip', 'identity'):
        raise ValueError('unexpected transport encoding or size')
    if encoding == 'gzip':
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            raw = stream.read(MAX_BYTES + 1)
    if not 0 < len(raw) <= MAX_BYTES or received < requested:
        raise ValueError('unexpected decoded size or receipt time')
    _write_exclusive(root / f'{name}.body.json', raw)
    write(root, f'{name}.receipt.json', {'url': url, 'requested_at': requested.isoformat(),
          'received_at': received.isoformat(), 'bytes': len(raw), 'sha256': sha(raw)})


def load_capture(root, name, kind, url):
    raw = (root / f'{name}.body.json').read_bytes()
    rec = json.loads((root / f'{name}.receipt.json').read_bytes())
    if (rec['sha256'] != sha(raw) or rec['bytes'] != len(raw) or rec['url'] != url
            or instant(rec['requested_at']) > instant(rec['received_at'])):
        raise ValueError('source receipt mismatch')
    _parse(raw, kind)
    return json.loads(raw, parse_float=Decimal), rec


def quote_pair(match, server):
    group = match.get('group_status', {}).get('920000', {})
    try:
        checked = instant(group.get('polled_at'))
    except ValueError:
        return None
    if group.get('stale') or not 0 <= (server - checked).total_seconds() <= 120:
        return None
    sides = {}
    for q in match.get('quotes', []):
        if q.get('group_id') != '920000' or q.get('line') != 'To Win':
            continue
        side = q.get('side')
        if (side not in (match['home'], match['away']) or side in sides
                or q.get('fixture_id') != match['id'] or q.get('suspended') or q.get('stale')):
            return None
        try:
            price = Decimal(str(q['odds_dec']))
            if not price.is_finite() or price <= 1 or instant(q['ts']) > checked:
                return None
        except (InvalidOperation, ValueError, KeyError):
            return None
        if not all(isinstance(q.get(k), str) and q[k] for k in ('market_id', 'sel_id')):
            return None
        sides[side] = (price, q['market_id'], q['sel_id'])
    if set(sides) != {match['home'], match['away']}:
        return None
    h, a = sides[match['home']], sides[match['away']]
    if h[1] != a[1] or h[2] == a[2]:
        return None
    return (h[0], a[0]), checked, h[1], [h[2], a[2]]


def frozen_forecasts(root):
    history, hr = load_capture(root, 'history', 'results', RESULTS)
    market, mr = load_capture(root, 'market', 'odds', ODDS)
    cutoff = instant(hr['received_at'])
    decision = instant(mr['received_at'])
    server = instant(market['server_time'])
    if not cutoff < instant(mr['requested_at']) <= decision or abs((server - decision).total_seconds()) > 30:
        raise ValueError('history must precede fresh odds')
    valid, reasons = validated_rows(history)
    state = history_state(valid, cutoff, exclude_id=None)
    known = {r['fixture_id'] for r in history['results']}
    models = {}
    for key, expected in MODELS.items():
        raw = (root / f'{key}-model.json').read_bytes()
        if sha(raw) != expected:
            raise ValueError('model bytes changed')
        models[key] = json.loads(raw)
        known.update(models[key]['training_ids'])
    selected, skipped = [], Counter()
    ids = Counter(m.get('id') for m in market['matches'])
    for m in market['matches']:
        ident = m.get('id')
        if m.get('league') not in LEAGUES or m.get('sport') != 'table_tennis':
            continue
        try:
            kickoff = instant(m.get('kickoff_utc'))
        except ValueError:
            continue
        if (kickoff.date() != decision.date() or
                not decision + timedelta(minutes=START_MINUTES) <= kickoff <= decision + timedelta(minutes=END_MINUTES)):
            continue
        if (not isinstance(ident, str) or not ident.isascii() or not ident.isdigit()
                or ids[ident] != 1 or ident in known or m.get('in_play') is not False or m.get('stale')
                or m.get('score') or not all(isinstance(m.get(k), str) and m[k] and m[k] == m[k].strip()
                     for k in ('home', 'away')) or m['home'] == m['away']):
            skipped['identity_state_or_prior_result'] += 1
            continue
        pair = quote_pair(m, server)
        if pair is None:
            skipped['winner_price_absent_stale_or_ambiguous'] += 1
            continue
        odds, checked, mid, selections = pair
        row = {'fixture_id': ident, **{k: m[k] for k in ('league', 'home', 'away')},
               'kickoff': kickoff.isoformat(), 'day': kickoff.date().isoformat(),
               'odds': list(map(str, odds)), 'market_checked_at': checked.isoformat(),
               'market_id': mid, 'selection_ids': selections}
        row.update(probabilities(row, float(odds[1] / sum(odds)), state))
        row['actions'] = {p: action(row[p], odds) for p in POLICIES}
        for key, model in models.items():
            f = forecast(row, model)
            row[f'{key}_mean'] = f['calibrated_anchor']
            row[f'{key}_lower'] = f['bootstrap_lower_selected_side_probabilities']
            row['actions'][f'{key}_mean'] = f['calibrated_action']
            row['actions'][f'{key}_guard'] = f['bootstrap_guarded_action']
        selected.append(row)
    selected.sort(key=lambda r: (r['kickoff'], r['fixture_id']))
    skipped['fixed_cap'] = max(0, len(selected) - MAX_FIXTURES)
    return {'schema': 'research.residual_forward_trial.v1', 'decision_at': mr['received_at'],
            'history_sha256': hr['sha256'], 'market_sha256': mr['sha256'], 'models': MODELS,
            'history_rows': len(history['results']), 'valid_history_before_cutoff': sum(
                instant(r['updated_at']) < cutoff and r['league'] in LEAGUES for r, _ in valid),
            'history_exclusions': reasons, 'source_history_kind': history.get('source'),
            'excluded_market': dict(skipped), 'rows': selected[:MAX_FIXTURES]}


def evaluate(root, expected, followups):
    raw = (root / 'forecasts.json').read_bytes()
    if sha(raw) != expected:
        raise ValueError('forecast hash differs from published commitment')
    frozen = json.loads(raw)
    if frozen != frozen_forecasts(root):
        raise ValueError('frozen forecast differs from recorded input replay')
    decision = instant(frozen['decision_at'])
    observations = defaultdict(list)
    for number in range(1, followups + 1):
        date = decision.date().isoformat()
        src, receipt = load_capture(root, f'results-{number}', 'results', RESULTS + '&date=' + date)
        earliest = max(instant(r['kickoff']) for r in frozen['rows']) + timedelta(minutes=20 if number == 1 else 35)
        if instant(receipt['requested_at']) < earliest:
            raise ValueError('results requested before forecast')
        good, _ = validated_rows(src)
        for row, winner in good:
            observations[row['fixture_id']].append((row, winner, instant(receipt['received_at'])))
    settled, pending, conflicts = [], [], []
    for r in frozen['rows']:
        matches = [(q, w) for q, w, received in observations[r['fixture_id']]
                   if all(r[k] == q[k] for k in ('league', 'home', 'away')) and decision < instant(r['kickoff'])
                   < instant(q['finished_at']) <= instant(q['updated_at']) <= received]
        keys = {(q.get('final_score'), tuple(g['score'] for g in q['periods']), w) for q, w in matches}
        if len(keys) == 1:
            settled.append({**r, 'outcome': int(matches[-1][1] == 0)})
        elif len(keys) > 1:
            conflicts.append(r['fixture_id'])
        else:
            pending.append(r['fixture_id'])
    return {'forecast_sha256': expected, 'followups': followups, 'frozen_count': len(frozen['rows']),
            'settled_count': len(settled), 'pending_ids': pending, 'conflicting_ids': conflicts,
            'pending_actions': {p: sum(r['actions'][p] is not None for r in frozen['rows']
                                if r['fixture_id'] in pending or r['fixture_id'] in conflicts) for p in ACTIONS},
            'economics': {p: fixed_action_economics(settled, p) for p in ACTIONS},
            'scores': {p: _scores(settled, p) for p in SCORES},
            'paired_brier_minus_market': {p: _paired_interval(settled, p, 'market') for p in SCORES[1:]},
            'settled_rows': settled, 'real_money_execution': False, 'profitability_proven': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('phase', choices=('freeze', 'collect', 'verify'))
    p.add_argument('--hash')
    p.add_argument('--followup', type=int, choices=(1, 2))
    args = p.parse_args()
    root = args.root
    if args.phase == 'freeze':
        capture(root, 'history', RESULTS)
        capture(root, 'market', ODDS)
        f = frozen_forecasts(root)
        if f['rows'] and now() >= min(instant(r['kickoff']) for r in f['rows']):
            raise ValueError('first kickoff elapsed before freeze')
        write(root, 'forecasts.json', f)
        print(json.dumps({'sha256': sha((root / 'forecasts.json').read_bytes()), 'decision_at': f['decision_at'],
              'n': len(f['rows']), 'excluded_market': f['excluded_market'],
              'first': min((r['kickoff'] for r in f['rows']), default=None),
              'last': max((r['kickoff'] for r in f['rows']), default=None),
              'actions': {p: sum(r['actions'][p] is not None for r in f['rows']) for p in ACTIONS}}))
    else:
        if args.hash is None or args.followup is None:
            p.error('--hash and --followup required')
        if args.phase == 'collect':
            raw = (root / 'forecasts.json').read_bytes()
            if sha(raw) != args.hash:
                raise ValueError('forecast hash mismatch before request')
            f = json.loads(raw)
            earliest = max(instant(r['kickoff']) for r in f['rows']) + timedelta(minutes=20 if args.followup == 1 else 35)
            if now() < earliest:
                raise ValueError(f'follow-up too early; first allowed at {earliest.isoformat()}')
            capture(root, f'results-{args.followup}', RESULTS + '&date=' + instant(f['decision_at']).date().isoformat())
        result = evaluate(root, args.hash, args.followup)
        if args.phase == 'collect':
            write(root, f'evaluation-{args.followup}.json', result)
        else:
            if json.loads((root / f'evaluation-{args.followup}.json').read_bytes()) != result:
                raise ValueError('evaluation replay mismatch')
        print(json.dumps({k: v for k, v in result.items() if k != 'settled_rows'}))


if __name__ == '__main__':
    main()
