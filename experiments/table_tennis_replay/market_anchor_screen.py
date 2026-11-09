"""Fixed retrospective market/history hypotheses; never production authority.

See MARKET_ANCHOR_PROTOCOL_20261109.md. Exact league/name strings are research
proxies. Source update clocks are not proof of historical local availability.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from itertools import groupby
import json
import math
from pathlib import Path
import random

from canonical_history_bridge import validated_rows
from h2h_exploratory import instant
from next_day_player_eval import _scores, _paired_interval

POLICIES = ('market', 'elo_anchor', 'h2h_anchor', 'combined_anchor')
LEAGUES = frozenset(('Setka Cup', 'Setka Cup Women', 'TT Cup',
                     'TT Elite Series', 'Czech Liga Pro'))
START, END = '2026-10-23', '2026-11-07'
COST, HAIRCUT, MIN_EDGE = Decimal('.01'), Decimal('.02'), Decimal('.03')


def selected_population(archive):
    """Selection does not read winners, prices, statuses or history support."""
    days = defaultdict(list)
    seen = set()
    for row in archive['results']:
        ident = row.get('fixture_id')
        if not isinstance(ident, str) or not ident.isascii() or not ident.isdigit() or ident in seen:
            raise ValueError('invalid or duplicate fixture ID')
        seen.add(ident)
        day = instant(row['finished_at']).date().isoformat()
        if row.get('league') in LEAGUES and START <= day <= END:
            days[day].append(ident)
    return {day: sorted(ids, key=lambda i: hashlib.sha256(
        ('autosport-market-anchor-v1|' + i).encode()).hexdigest())[:15]
        for day, ids in sorted(days.items())}


def history_state(valid, cutoff, *, exclude_id):
    """Replay only pre-day reports; equal source update times share prior Elo."""
    history = [(r, winner) for r, winner in valid if r['fixture_id'] != exclude_id
               and r['league'] in LEAGUES
               and instant(r['finished_at']) < cutoff
               and instant(r['updated_at']) < cutoff]
    history.sort(key=lambda x: (instant(x[0]['updated_at']), x[0]['fixture_id']))
    ratings = defaultdict(lambda: 1500.0)
    counts, opponents = Counter(), defaultdict(set)
    pairs = defaultdict(lambda: [0, 0])
    for _, batch in groupby(history, key=lambda x: instant(x[0]['updated_at'])):
        changes = defaultdict(float)
        for row, winner in batch:
            league, home, away = (row[k] for k in ('league', 'home', 'away'))
            hk, ak = (league, home), (league, away)
            expected = 1 / (1 + 10 ** ((ratings[ak] - ratings[hk]) / 400))
            delta = 24 * (int(winner == 0) - expected)
            changes[hk] += delta
            changes[ak] -= delta
            counts[hk] += 1
            counts[ak] += 1
            opponents[hk].add(away)
            opponents[ak].add(home)
            if instant(row['finished_at']) >= cutoff - timedelta(days=30):
                a, b = sorted((home, away))
                pair = pairs[(league, a, b)]
                pair[0] += 1
                pair[1] += (home if winner == 0 else away) == a
        for player, change in changes.items():
            ratings[player] += change
    return ratings, counts, opponents, pairs


def probabilities(row, market, state):
    ratings, counts, opponents, pairs = state
    league, home, away = (row[k] for k in ('league', 'home', 'away'))
    hk, ak = (league, home), (league, away)
    # get() avoids mutating a reused state during prediction.
    elo = 1 / (1 + 10 ** ((ratings.get(ak, 1500) - ratings.get(hk, 1500)) / 400))
    elo_ok = min(counts.get(hk, 0), counts.get(ak, 0)) >= 10 and min(
        len(opponents.get(hk, ())), len(opponents.get(ak, ()))) >= 2
    a, b = sorted((home, away))
    n, a_wins = pairs.get((league, a, b), (0, 0))
    wins = a_wins if home == a else n - a_wins
    p_elo = .8 * market + .2 * elo if elo_ok else market
    p_h2h = (wins + 20 * market) / (n + 20) if n >= 3 else market
    return {'market': market, 'elo_anchor': p_elo, 'h2h_anchor': p_h2h,
            'combined_anchor': (p_elo + p_h2h) / 2,
            'elo_unblended_diagnostic': elo,
            'elo_supported': elo_ok, 'h2h_supported': n >= 3,
            'prior_meetings': n, 'prior_home_wins': wins,
            'home_history': counts.get(hk, 0), 'away_history': counts.get(ak, 0)}


def closing_prices(data, row, day, *, result_ids=frozenset()):
    ident = row['fixture_id']
    if data.get('fixture_id') != ident:
        return None, 'fixture_mismatch'
    aliases, pre = data.get('fixture_ids'), data.get('prematch_id')
    if not (aliases == [ident] or isinstance(pre, str) and pre != ident
            and isinstance(aliases, list) and len(aliases) == 2 and set(aliases) == {ident, pre}):
        return None, 'ambiguous_fixture_link'
    # Demonstrated provider defect: /closing can merge repeated meetings of
    # the same pair, including opposite orientations and different results.
    # A separate result for the alleged twin cannot certify one event identity.
    if any(alias != ident and alias in result_ids for alias in aliases):
        return None, 'alias_has_separate_result'
    if data.get('closed') is not True:
        return None, 'closing_not_final'
    if data.get('kickoff_known') is not True or data.get('kickoff_estimated') is not False or data.get('pre_close_basis') != 'kickoff':
        return None, 'kickoff_unconfirmed'
    try:
        kickoff = instant(data['kickoff_utc'])
        finished = instant(row['finished_at'])
    except (KeyError, ValueError):
        return None, 'kickoff_unconfirmed'
    if str(kickoff.date()) != day or kickoff >= finished:
        return None, 'kickoff_date_or_order'
    sides = {}
    for selection in data.get('selections', []):
        if selection.get('group_id') != '920000' or selection.get('line') != 'To Win':
            continue
        side = selection.get('side')
        if side not in (row['home'], row['away']):
            return None, 'foreign_selection_identity'
        if side in sides:
            return None, 'ambiguous_selection'
        quote = selection.get('pre_close')
        if not isinstance(quote, dict) or quote.get('suspended') is not False:
            return None, 'missing_or_suspended_close'
        if not all(isinstance(selection.get(k), str) and selection[k]
                   for k in ('sel_id', 'market_id', 'group_name')):
            return None, 'missing_selection_identity'
        if any(k in quote and quote[k] != selection.get(k) for k in (
                'group_id', 'market_id', 'sel_id', 'group_name', 'side', 'line')):
            return None, 'conflicting_selection_identity'
        if any(obj.get('fixture_id', pre or ident) not in aliases for obj in (selection, quote)):
            return None, 'foreign_quote_fixture'
        try:
            if isinstance(quote.get('odds_dec'), bool):
                raise ValueError('boolean odds')
            price = Decimal(str(quote['odds_dec']))
            stamp = instant(quote['ts'])
        except (KeyError, ValueError, InvalidOperation):
            return None, 'invalid_price_or_timestamp'
        if not price.is_finite() or price <= 1 or not 0 <= (kickoff - stamp).total_seconds() <= 86400:
            return None, 'price_or_time_outside_protocol'
        sides[side] = (price, selection['market_id'], selection['sel_id'], selection['group_name'])
    if set(sides) != {row['home'], row['away']}:
        return None, 'missing_winner_pair'
    home, away = sides[row['home']], sides[row['away']]
    if home[1] != away[1] or home[2] == away[2] or home[3] != away[3]:
        return None, 'different_or_ambiguous_markets'
    return {'odds': (home[0], away[0]), 'kickoff': kickoff.isoformat()}, None


def action(probability, odds):
    p = Decimal(str(probability))
    edges = (p * odds[0] * (1 - HAIRCUT) - 1 - COST,
             (1 - p) * odds[1] * (1 - HAIRCUT) - 1 - COST)
    side = int(edges[1] > edges[0])
    return side if edges[side] >= MIN_EDGE else None


def economics(rows, policy):
    daily = defaultdict(lambda: {'bets': 0, 'wins': 0, 'pnl': Decimal(0), 'stress_pnl': Decimal(0)})
    total = stress = peak = drawdown = predicted = market_expected = Decimal(0)
    bets = wins = 0
    for row in sorted(rows, key=lambda r: (r['kickoff'], r['fixture_id'])):
        odds = tuple(Decimal(x) for x in row['odds'])
        side = action(row[policy], odds)
        day = daily[row['day']]
        if side is None:
            continue
        won = row['outcome'] == (1 - side)
        pnl = (odds[side] * (1 - HAIRCUT) if won else 0) - 1 - COST
        stress_pnl = (odds[side] * Decimal('.95') if won else 0) - 1 - COST
        p = Decimal(str(row[policy]))
        m = Decimal(str(row['market']))
        predicted += (p if side == 0 else 1 - p) * odds[side] * (1 - HAIRCUT) - 1 - COST
        market_expected += (m if side == 0 else 1 - m) * odds[side] * (1 - HAIRCUT) - 1 - COST
        total += pnl
        stress += stress_pnl
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
        bets += 1
        wins += won
        day['bets'] += 1
        day['wins'] += won
        day['pnl'] += pnl
        day['stress_pnl'] += stress_pnl
    return {'bets': bets, 'wins': wins, 'stake': str(bets), 'pnl': str(total),
            'roi': str(total / bets) if bets else None, 'stress_pnl': str(stress),
            'max_drawdown_in_kickoff_order': str(drawdown),
            'own_expected_pnl': str(predicted), 'market_expected_pnl_same_actions': str(market_expected),
            'daily': {d: {k: str(v) if isinstance(v, Decimal) else v for k, v in vals.items()}
                      for d, vals in sorted(daily.items())}}


def day_intervals(rows, policy, all_days):
    groups = defaultdict(list)
    for row in rows:
        groups[row['day']].append(row)
    econ = economics(rows, policy)['daily']
    aggregates = []
    for day in all_days:
        rs = groups[day]
        delta = sum((r[policy] - r['outcome']) ** 2 - (r['market'] - r['outcome']) ** 2 for r in rs)
        aggregates.append((delta, len(rs), float(econ.get(day, {}).get('pnl', 0))))
    if len(aggregates) < 2:
        return None
    rng = random.Random(20261109)
    briers, pnls = [], []
    for _ in range(2000):
        sample = [rng.choice(aggregates) for _ in aggregates]
        n = sum(x[1] for x in sample)
        if n:
            briers.append(sum(x[0] for x in sample) / n)
        pnls.append(sum(x[2] for x in sample) / len(sample))
    def interval(values):
        if not values:
            return None
        values.sort()
        return [values[int(.025 * (len(values) - 1))], values[int(.975 * (len(values) - 1))]]
    return {'day_blocks': len(aggregates), 'resamples': 2000,
            'brier_difference_95pct': interval(briers), 'mean_daily_pnl_95pct': interval(pnls),
            'cross_day_dependence_resolved': False}


def evaluate(archive, selection, directory):
    expected = selected_population(archive)
    if selection['days'] != expected:
        raise ValueError('selection does not match frozen identifier-only protocol')
    valid, history_excluded = validated_rows(archive)
    by_id = {r['fixture_id']: (r, w) for r, w in valid}
    all_result_ids = frozenset(r['fixture_id'] for r in archive['results'])
    included, coverage = [], Counter()
    per_day = {}
    for day, ids in expected.items():
        cutoff = datetime.combine(datetime.fromisoformat(day).date(), time.min, tzinfo=timezone.utc)
        # Every selected result is from this date, hence outside the pre-day
        # history. Still pass the explicit exclusion contract to the builder.
        state = history_state(valid, cutoff, exclude_id=None)
        daily_counts = Counter()
        for ident in ids:
            if ident not in by_id:
                daily_counts['invalid_or_unknown_result'] += 1
                continue
            row, winner = by_id[ident]
            bp, rp = directory / (ident + '.body.json'), directory / (ident + '.receipt.json')
            if not bp.exists() and not rp.exists():
                daily_counts['not_captured'] += 1
                continue
            if not bp.is_file() or not rp.is_file():
                raise ValueError('incomplete capture')
            raw = bp.read_bytes()
            receipt = json.loads(rp.read_text())
            if hashlib.sha256(raw).hexdigest() != receipt.get('sha256'):
                raise ValueError('closing receipt digest mismatch')
            if receipt.get('url') != 'https://moviodds.com/closing?fi=' + ident:
                raise ValueError('closing receipt source mismatch')
            if instant(receipt['received_at']) < instant(receipt['requested_at']):
                raise ValueError('closing capture chronology')
            prices, reason = closing_prices(json.loads(raw, parse_float=Decimal), row, day,
                                             result_ids=all_result_ids)
            if reason:
                daily_counts[reason] += 1
                continue
            odds = prices['odds']
            market = float(odds[1] / sum(odds))
            forecast = probabilities(row, market, state)
            included.append({'fixture_id': ident, 'day': day, 'league': row['league'],
                'home': row['home'], 'away': row['away'], 'outcome': int(winner == 0),
                'kickoff': prices['kickoff'], 'odds': list(map(str, odds)),
                'overround': str(sum(1 / v for v in odds) - 1), **forecast})
            daily_counts['included'] += 1
        coverage.update(daily_counts)
        per_day[day] = dict(daily_counts)
    def summary(rs, days):
        out = {}
        for policy in POLICIES:
            economic = economics(rs, policy)
            for day in days:
                economic['daily'].setdefault(day, {'bets': 0, 'wins': 0, 'pnl': '0', 'stress_pnl': '0'})
            out[policy] = {'scores': _scores(rs, policy), 'economics': economic,
                'day_block_intervals': day_intervals(rs, policy, days),
                'pair_cluster_brier_interval': _paired_interval(rs, policy, 'market')}
        return out
    days = list(expected)
    return {'schema': 'research.market_anchor_screen.v1', 'selected': sum(map(len, expected.values())),
        'included': len(included), 'coverage': dict(coverage), 'coverage_by_day': per_day,
        'source_result_exclusions': history_excluded,
        'support': {'elo': sum(r['elo_supported'] for r in included),
                    'h2h': sum(r['h2h_supported'] for r in included)},
        'all': summary(included, days),
        'early_period': summary([r for r in included if r['day'] <= '2026-10-30'], [d for d in days if d <= '2026-10-30']),
        'later_period': summary([r for r in included if r['day'] > '2026-10-30'], [d for d in days if d > '2026-10-30']),
        'elo_unblended_diagnostic_scores': _scores(included, 'elo_unblended_diagnostic'),
        'identity_verified': False, 'rights_verified': False, 'historical_local_availability_verified': False,
        'independent_forward_holdout': False, 'executable_price_verified': False,
        'profitability_proven': False, 'production_promotion': False, 'real_money_execution': False,
        'rows': included}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('directory', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raw = args.archive.read_bytes()
    selection = json.loads((args.directory / 'selection.json').read_text())
    if hashlib.sha256(raw).hexdigest() != selection['source_sha256']:
        raise ValueError('source archive digest mismatch')
    report = evaluate(json.loads(raw), selection, args.directory)
    report['source_sha256'] = selection['source_sha256']
    report['protocol_commit'] = selection['protocol_commit']
    with args.output.open('x') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps({k: report[k] for k in ('selected', 'included', 'coverage', 'support')}))
    for policy, values in report['all'].items():
        print(policy, values['scores'], {k: v for k, v in values['economics'].items() if k != 'daily'})


if __name__ == '__main__':
    main()
