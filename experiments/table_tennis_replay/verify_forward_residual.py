"""Independent offline proof of population, prior H2H, frozen actions and PAPER results.

Uses only the standard library and frozen source/commitment artifacts. This
does not certify a source clock, true player identity, or accepted prices.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path

LEAGUES = {'Setka Cup', 'Setka Cup Women', 'TT Cup', 'TT Elite Series', 'Czech Liga Pro'}
ANCHORS = ('market', 'elo_anchor', 'h2h_anchor', 'combined_anchor')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stamp(text):
    result = datetime.fromisoformat(text.replace('Z', '+00:00'))
    require(result.utcoffset() is not None, 'timezone missing')
    return result


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def read(root, name):
    return json.loads((root / f'{name}.json').read_bytes())


def capture(root, name):
    raw = (root / f'{name}.body.json').read_bytes()
    receipt = read(root, f'{name}.receipt')
    require(sha(raw) == receipt['sha256'] and len(raw) == receipt['bytes'], 'capture digest mismatch')
    require(stamp(receipt['requested_at']) <= stamp(receipt['received_at']), 'inverted receipt')
    return json.loads(raw, parse_float=str), receipt


def labels(row):
    return tuple(row.get(k) for k in ('league', 'home', 'away'))


def valid_label(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def winner(row):
    league, a, b = labels(row)
    if (row.get('status') != 'finished' or row.get('sport', 'table_tennis') != 'table_tennis'
            or not all(map(valid_label, (league, a, b))) or a == b):
        return None
    try:
        if stamp(row['finished_at']) > stamp(row['updated_at']):
            return None
        periods = [p['score'] for p in row['periods']]
        if row.get('final_score') is not None and row['final_score'] != ','.join(periods):
            return None
        won = [0, 0]
        for game in periods:
            a, b = map(int, game.split('-'))
            hi, lo = max(a, b), min(a, b)
            if 3 in won or lo < 0 or not ((hi == 11 and lo <= 9) or (hi >= 12 and hi - lo == 2)):
                return None
            won[int(b > a)] += 1
        return won.index(3) if 3 in won else None
    except (KeyError, AttributeError, TypeError, ValueError):
        return None


def same_number(a, b):
    return abs(a - b) < 1e-13


def settlement(rows, policy):
    total = stress = peak = dd = Fraction(0)
    bets = wins = 0
    for r in sorted(rows, key=lambda r: (r['kickoff'], r['fixture_id'])):
        side = r['actions'][policy]
        if side is None:
            continue
        bets += 1
        won = r['outcome'] == 1 - side
        wins += won
        odds = Fraction(r['odds'][side]) if won else Fraction(0)
        total += odds * Fraction(98, 100) - Fraction(101, 100)
        stress += odds * Fraction(95, 100) - Fraction(101, 100)
        peak = max(peak, total)
        dd = max(dd, peak - total)
    return bets, wins, total, stress, dd


def verify(root, expected, followups):
    raw = (root / 'forecasts.json').read_bytes()
    require(sha(raw) == expected, 'forecast does not match published hash')
    forecast = json.loads(raw)
    history, hr = capture(root, 'history')
    board, br = capture(root, 'market')
    cutoff, decision = stamp(hr['received_at']), stamp(br['received_at'])
    server = stamp(board['server_time'])
    require(cutoff < stamp(br['requested_at']) <= decision == stamp(forecast['decision_at'])
            and abs(server - decision) <= timedelta(seconds=30), 'forecast clock mismatch')
    require(hr['sha256'] == forecast['history_sha256'] and br['sha256'] == forecast['market_sha256'], 'forecast source mismatch')
    known = {r['fixture_id'] for r in history['results']}
    require(len(known) == len(history['results']), 'duplicate historical fixture')
    models = {}
    for key, digest in forecast['models'].items():
        raw = (root / f'{key}-model.json').read_bytes()
        require(sha(raw) == digest, 'model digest mismatch')
        models[key] = json.loads(raw)
        known.update(models[key]['training_ids'])
    candidates = []
    ids = Counter(m.get('id') for m in board['matches'])
    for m in board['matches']:
        league, home, away = labels(m)
        ident = m.get('id')
        if (league not in LEAGUES or m.get('sport') != 'table_tennis' or not isinstance(ident, str)
                or not ident.isascii() or not ident.isdecimal() or ident in known or ids[ident] != 1
                or m.get('in_play') is not False or m.get('stale') or m.get('score')
                or not all(map(valid_label, (home, away))) or home == away):
            continue
        group = m.get('group_status', {}).get('920000', {})
        try:
            kickoff, checked = stamp(m.get('kickoff_utc')), stamp(group.get('polled_at'))
        except (AttributeError, ValueError):
            continue
        if (kickoff.date() != decision.date() or not timedelta(minutes=4) <= kickoff - decision <= timedelta(minutes=22)
                or group.get('stale') or not timedelta() <= server - checked <= timedelta(seconds=120)):
            continue
        quotes = [q for q in m['quotes'] if q.get('group_id') == '920000' and q.get('line') == 'To Win']
        if len(quotes) != 2 or {q.get('side') for q in quotes} != {home, away}:
            continue
        quotes.sort(key=lambda q: q['side'] != home)
        if (any(q.get('fixture_id') != ident or q.get('stale') or q.get('suspended')
                or not all(isinstance(q.get(k), str) and q[k] for k in ('market_id', 'sel_id')) for q in quotes)
                or quotes[0]['market_id'] != quotes[1]['market_id'] or quotes[0]['sel_id'] == quotes[1]['sel_id']):
            continue
        try:
            if any(Fraction(q['odds_dec']) <= 1 or stamp(q['ts']) > checked for q in quotes):
                continue
        except (AttributeError, KeyError, ValueError, ZeroDivisionError):
            continue
        candidates.append((kickoff, ident, (league, home, away), quotes, checked))
    candidates.sort(key=lambda c: (c[0], c[1]))
    rows = forecast['rows']
    require([c[1] for c in candidates[:32]] == [r['fixture_id'] for r in rows], 'population mismatch')
    require(bool(rows), 'empty trial')

    meetings = defaultdict(Counter)
    for r in history['results']:
        w = winner(r)
        league, a, b = labels(r)
        if (w is not None and cutoff - timedelta(days=30) <= stamp(r['finished_at'])
                <= stamp(r['updated_at']) < cutoff and league in LEAGUES):
            meetings[(league, *sorted((a, b)))][(a, b)[w]] += 1

    def residual(r, weights):
        odds = lambda p: p / (1 - p)
        m = odds(r['market'])
        ratio = m * (odds(r['elo_anchor']) / m) ** weights[0] * (odds(r['h2h_anchor']) / m) ** weights[1]
        return ratio / (1 + ratio)

    def edge(p, odds):
        return Fraction(str(p)) * odds * Fraction(98, 100) - Fraction(101, 100)

    for r, (kickoff, _, pair, quotes, checked) in zip(rows, candidates):
        odds = [Fraction(q['odds_dec']) for q in quotes]
        require(stamp(r['kickoff']) == kickoff and stamp(r['market_checked_at']) == checked and labels(r) == pair
                and [Fraction(v) for v in r['odds']] == odds and r['market_id'] == quotes[0]['market_id']
                and r['selection_ids'] == [q['sel_id'] for q in quotes], 'quote provenance mismatch')
        league, a, b = pair
        counts = meetings[(league, *sorted((a, b)))]; n = sum(counts.values())
        m = float(odds[1] / sum(odds))
        h = (counts[a] + 20 * m) / (n + 20) if n >= 3 else m
        require(r['prior_meetings'] == n and r['prior_home_wins'] == counts[a]
                and same_number(m, r['market']) and same_number(h, r['h2h_anchor']), 'market/H2H mismatch')
        require(same_number((r['elo_anchor'] + h) / 2, r['combined_anchor']), 'combination mismatch')
        for key, model in models.items():
            require(r['fixture_id'] not in model['training_ids'] and kickoff >= stamp(model['cutoff']), 'model overlap')
            samples = [residual(r, w) for w in model['bootstrap_weights']]
            require(len(samples) == 200 and same_number(residual(r, model['weights']), r[f'{key}_mean']), 'model mismatch')
            lower = [sorted(samples)[9], sorted(1 - p for p in samples)[9]]
            require(all(same_number(a, b) for a, b in zip(lower, r[f'{key}_lower'], strict=True)), 'sensitivity mismatch')
        for key, side in r['actions'].items():
            guard = key.endswith('_guard')
            prob = r[key.replace('_guard', '_mean')] if guard else r[key]
            e = [edge(prob, odds[0]), edge(1 - prob, odds[1])]
            chosen = int(e[1] > e[0]); chosen = chosen if e[chosen] >= Fraction(3, 100) else None
            if guard and chosen is not None and edge(r[key.replace('_guard', '_lower')][chosen], odds[chosen]) < Fraction(3, 100):
                chosen = None
            require(side == chosen, 'frozen action mismatch')

    if followups == 0:
        return {'forecast_sha256': expected, 'fixtures': len(rows), 'frozen_population_and_actions_verified': True}
    observed = defaultdict(list)
    for i in range(1, followups + 1):
        results, rec = capture(root, f'results-{i}')
        require(results.get('date') == decision.date().isoformat(), 'result date mismatch')
        require(stamp(rec['requested_at']) >= candidates[len(rows)-1][0] + timedelta(minutes=20 if i == 1 else 35), 'early outcome request')
        by_id = {q['fixture_id']: q for q in results['results']}
        require(len(by_id) == len(results['results']), 'duplicate result fixture')
        for r in rows:
            q = by_id.get(r['fixture_id'])
            if q is None or labels(q) != labels(r):
                continue
            w = winner(q)
            if w is not None and stamp(r['kickoff']) < stamp(q['finished_at']) <= stamp(q['updated_at']) <= stamp(rec['received_at']):
                observed[r['fixture_id']].append((q.get('final_score'), tuple(p['score'] for p in q['periods']), w))
    settled, pending, conflicts = [], [], []
    for r in rows:
        ident = r['fixture_id']; unique = set(observed[ident])
        if len(unique) == 1:
            settled.append({**r, 'outcome': int(next(iter(unique))[2] == 0)})
        elif not unique:
            pending.append(ident)
        else:
            conflicts.append(ident)
    result = read(root, f'evaluation-{followups}')
    require(result['forecast_sha256'] == expected and settled == result['settled_rows']
            and pending == result['pending_ids'] and conflicts == result['conflicting_ids'], 'settlement population mismatch')
    for p, e in result['economics'].items():
        b, w, pnl, stress, dd = settlement(settled, p)
        require((b, w) == (e['bets'], e['wins']) and all(abs(v - Fraction(e[k])) < Fraction(1, 10**23)
                for v, k in ((pnl, 'pnl'), (stress, 'stress_pnl'), (dd, 'max_drawdown_in_kickoff_order'))), 'PAPER arithmetic mismatch')
    for p, metrics in result['scores'].items():
        if not settled:
            require(metrics is None, 'empty scores expected')
            continue
        brier = sum((r[p] - r['outcome']) ** 2 for r in settled) / len(settled)
        logloss = -sum(math.log(r[p] if r['outcome'] else 1-r[p]) for r in settled) / len(settled)
        require(same_number(brier, metrics['brier']) and same_number(logloss, metrics['log_loss']), 'score mismatch')
    return {'forecast_sha256': expected, 'fixtures': len(rows), 'resolved': len(settled), 'pending': len(pending),
            'conflicting': len(conflicts), 'frozen_population_and_actions_verified': True,
            'independent_h2h_and_settlement_verified': True}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', type=Path)
    p.add_argument('--hash', required=True)
    p.add_argument('--followup', type=int, choices=(0, 1, 2), default=0)
    args = p.parse_args()
    print(json.dumps(verify(args.root, args.hash, args.followup), indent=2))
