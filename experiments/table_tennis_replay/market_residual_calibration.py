"""Research-only, regularized market residual learning; no execution authority.

Fit on causal earlier predictions, then freeze before reading test outcomes.
Day-bootstrap bands describe sensitivity to this training sample, NOT guaranteed
confidence intervals for true match probabilities or financial returns.
"""
from collections import Counter, defaultdict
from decimal import Decimal
import hashlib
import json
import math
import random

from h2h_exploratory import instant
from market_anchor_screen import action

GRID = (0.0, .25, .5, .75, 1.0)
RIDGE = .01
BOOTSTRAPS = 200
SEED = 20261110


def logit(p):
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 < p < 1:
        raise ValueError('finite probability strictly inside (0, 1) required')
    return math.log(p) - math.log1p(-p)


def sigmoid(z):
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def features(row):
    offset = logit(row['market'])
    return offset, logit(row['elo_anchor']) - offset, logit(row['h2h_anchor']) - offset


def prediction(row, weights):
    if len(weights) != 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in weights):
        raise ValueError('two nonnegative bounded weights required')
    offset, elo, h2h = features(row)
    return sigmoid(offset + weights[0] * elo + weights[1] * h2h)


def _loss(z, y):
    return max(z, 0) - y * z + math.log1p(math.exp(-abs(z)))


def _fit(prepared, multiplicities):
    total = sum(multiplicities)
    if total <= 0:
        raise ValueError('empty effective training sample')
    candidates = []
    for a in GRID:
        for b in GRID:
            loss = sum(w * _loss(o + a * x + b * h, y)
                       for (o, x, h, y), w in zip(prepared, multiplicities)) / total
            candidates.append((loss + RIDGE * (a * a + b * b), a + b, a, b))
    best = min(candidates)
    return [best[2], best[3]], best[0]


def fit(rows, *, cutoff, source_sha256):
    """Reject temporal overlap instead of silently dropping leaked training rows."""
    if not rows or len(source_sha256) != 64 or any(c not in '0123456789abcdef' for c in source_sha256):
        raise ValueError('training data and its exact source digest required')
    seen, days, prepared = set(), defaultdict(list), []
    for row in rows:
        ident = row['fixture_id']
        if ident in seen:
            raise ValueError('duplicate training event')
        seen.add(ident)
        if (not instant(row['kickoff']) < instant(row['result_updated_at']) < instant(cutoff)
                or row['day'] != instant(row['kickoff']).date().isoformat()):
            raise ValueError('training chronology crosses cutoff')
        y = row['outcome']
        if type(y) is not int or y not in (0, 1):
            raise ValueError('binary result required')
        days[row['day']].append(len(prepared))
        prepared.append((*features(row), y))
    if len(rows) < 30 or len(days) < 5:
        raise ValueError('at least 30 earlier observations over five days required')
    weights, objective = _fit(prepared, [1] * len(rows))
    rng = random.Random(SEED)
    calendar = sorted(days)
    draws = []
    for _ in range(BOOTSTRAPS):
        counts = Counter(rng.choice(calendar) for _ in calendar)
        multiplicities = [0] * len(rows)
        for day, indices in days.items():
            for index in indices:
                multiplicities[index] = counts[day]
        draws.append(_fit(prepared, multiplicities)[0])
    return {'schema': 'research.market_residual_calibrator.v1', 'cutoff': cutoff,
            'source_sha256': source_sha256, 'training_ids': sorted(seen),
            'training_count': len(rows), 'training_days': calendar,
            'weights': weights, 'penalized_training_log_loss': objective,
            'grid': list(GRID), 'ridge': RIDGE, 'bootstrap_seed': SEED,
            'bootstrap_weights': draws, 'bootstrap_unit': 'UTC prediction day',
            'cross_day_dependence_resolved': False, 'probability_coverage_verified': False,
            'production_promotion': False, 'real_money_execution': False}


def forecast(row, model):
    # Outcome fields are deliberately not accessed.
    if (row['fixture_id'] in model['training_ids']
            or instant(row['kickoff']) < instant(model['cutoff'])):
        raise ValueError('test event overlaps calibration period')
    p = prediction(row, model['weights'])
    samples = [prediction(row, w) for w in model['bootstrap_weights']]
    if len(samples) != BOOTSTRAPS:
        raise ValueError('incomplete fixed bootstrap ensemble')
    lower = [sorted(samples)[int(.05 * (len(samples) - 1))],
             sorted(1 - x for x in samples)[int(.05 * (len(samples) - 1))]]
    odds = tuple(Decimal(v) for v in row['odds'])
    if len(odds) != 2 or any(not v.is_finite() or v <= 1 for v in odds):
        raise ValueError('valid two-sided decimal odds required')
    side = action(p, odds)
    guarded = side
    if side is not None:
        pessimistic_ev = Decimal(str(lower[side])) * odds[side] * Decimal('.98') - Decimal('1.01')
        if pessimistic_ev < Decimal('.03'):
            guarded = None
    return {'calibrated_anchor': p, 'bootstrap_lower_selected_side_probabilities': lower,
            'calibrated_action': side, 'bootstrap_guarded_action': guarded}


def digest(model):
    return hashlib.sha256(json.dumps(model, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def fixed_action_economics(rows, policy):
    """Score precomputed actions; an abstention is never encoded as a fake p."""
    total = stress = peak = drawdown = Decimal(0)
    bets = wins = 0
    days = defaultdict(lambda: {'bets': 0, 'wins': 0, 'pnl': Decimal(0)})
    for row in sorted(rows, key=lambda r: (r['kickoff'], r['fixture_id'])):
        side = row['actions'][policy]
        if side is None:
            continue
        if type(side) is not int or side not in (0, 1):
            raise ValueError('invalid frozen action')
        outcome = row['outcome']
        if type(outcome) is not int or outcome not in (0, 1):
            raise ValueError('invalid result')
        price = Decimal(row['odds'][side])
        if not price.is_finite() or price <= 1:
            raise ValueError('invalid settlement price')
        won = outcome == 1 - side
        pnl = (price * Decimal('.98') if won else 0) - Decimal('1.01')
        stress += (price * Decimal('.95') if won else 0) - Decimal('1.01')
        total += pnl
        bets += 1
        wins += won
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
        d = days[row['day']]
        d['bets'] += 1
        d['wins'] += won
        d['pnl'] += pnl
    return {'bets': bets, 'wins': wins, 'stake': str(bets), 'pnl': str(total),
            'stress_pnl': str(stress), 'roi': str(total / bets) if bets else None,
            'max_drawdown_in_kickoff_order': str(drawdown),
            'daily': {d: {**v, 'pnl': str(v['pnl'])} for d, v in sorted(days.items())}}
