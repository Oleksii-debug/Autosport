"""Reuse canonical shadow memory on a labelled retrospective simulation clock.

No production checkpoint is touched. A selected rule is fixed for a whole UTC
day using only source updates reported before that day, never its own outcomes.
"""
import argparse
from collections import Counter
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from autosport.shadow_strategy_memory import ShadowMemoryPlan, ShadowStrategyMemory
from canonical_history_bridge import validated_rows
from h2h_exploratory import instant
from market_anchor_screen import POLICIES, COST, HAIRCUT, action, economics


def hypothetical_return(row, policy):
    odds = tuple(Decimal(v) for v in row['odds'])
    side = action(row[policy], odds)
    if side is None:
        return Decimal(0)
    won = row['outcome'] == 1 - side
    return (odds[side] * (1 - HAIRCUT) if won else 0) - 1 - COST


def replay(report, source, source_sha256):
    if report.get('source_sha256') != source_sha256:
        raise ValueError('different result source')
    source_valid, _ = validated_rows(source)
    source_rows = {r['fixture_id']: r for r, _ in source_valid}
    source_winners = {r['fixture_id']: winner for r, winner in source_valid}
    plan = ShadowMemoryPlan(actions=tuple(sorted(POLICIES)))
    memory = ShadowStrategyMemory(plan)
    seen, feedback_seen, selected = set(), set(), []
    rows = report['rows']
    for row in rows:
        if row['fixture_id'] in seen or row['fixture_id'] not in source_rows:
            raise ValueError('duplicate or unknown report event')
        original = source_rows[row['fixture_id']]
        if (any(row[key] != original[key] for key in ('league', 'home', 'away'))
                or row['outcome'] != int(source_winners[row['fixture_id']] == 0)
                or row['day'] != str(instant(original['finished_at']).date())
                or not instant(row['kickoff']) < instant(original['finished_at'])):
            raise ValueError('report/source result mismatch')
        seen.add(row['fixture_id'])
    daily = []
    for day in sorted(report['coverage_by_day']):
        now = datetime.combine(datetime.fromisoformat(day).date(), time.min, tzinfo=timezone.utc)
        available = [r for r in rows if r['fixture_id'] not in feedback_seen and r['day'] < day
            and instant(source_rows[r['fixture_id']]['updated_at']) < now]
        available.sort(key=lambda r: (instant(source_rows[r['fixture_id']]['updated_at']), r['fixture_id']))
        for row in available:
            result_at = source_rows[row['fixture_id']]['updated_at']
            memory.observe(event_id=row['fixture_id'], decision_at=row['kickoff'],
                result_available_at=result_at, now=now.isoformat(),
                net_returns={policy: hypothetical_return(row, policy) for policy in plan.actions},
                source_sha256=source_sha256, availability_basis='retrospective_assumed_delay')
            feedback_seen.add(row['fixture_id'])
        chosen = memory.choose(as_of=now.isoformat(), admissible_actions=frozenset((*plan.actions, 'WAIT')))
        day_rows = [r for r in rows if r['day'] == day]
        adapted = [{**r, 'adaptive': r[chosen]} for r in day_rows] if chosen != 'WAIT' else []
        selected.extend(adapted)
        e = economics(adapted, 'adaptive')
        daily.append({'day': day, 'policy': chosen, 'feedback_events': len(feedback_seen),
            'window_opportunities': min(len(feedback_seen), plan.window),
            'eligible_events': len(day_rows), 'bets': e['bets'], 'wins': e['wins'],
            'pnl': e['pnl'], 'stress_pnl': e['stress_pnl']})
    e = economics(selected, 'adaptive')
    # The final memory includes only feedback actually used by the last day's
    # choice. Do not ingest that day's outcomes merely to improve its result.
    return {'schema': 'research.market_anchor_memory.v1', 'clock': 'SIMULATED_RETROSPECTIVE',
        'source_sha256': source_sha256, 'plan': plan.payload(), 'days': daily,
        'choices': dict(Counter(d['policy'] for d in daily)),
        'economics': {k: v for k, v in e.items() if k != 'daily'},
        'feedback_events_in_checkpoint': len(feedback_seen),
        'positive_days': sum(Decimal(d['pnl']) > 0 for d in daily),
        'negative_days': sum(Decimal(d['pnl']) < 0 for d in daily),
        'identity_verified': False, 'historical_local_availability_verified': False,
        'execution_authorized': False, 'promotion_authorized': False,
        'profitability_proven': False}, memory


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report', type=Path)
    p.add_argument('source', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('checkpoint', type=Path)
    args = p.parse_args()
    raw = args.source.read_bytes()
    report_raw = args.report.read_bytes()
    result, memory = replay(json.loads(report_raw), json.loads(raw), hashlib.sha256(raw).hexdigest())
    result['input_report_sha256'] = hashlib.sha256(report_raw).hexdigest()
    with args.output.open('x') as f:
        json.dump(result, f, indent=2)
        f.write('\n')
    if args.checkpoint.exists():
        raise ValueError('new research checkpoint path required')
    memory.save(args.checkpoint)
    restored = ShadowStrategyMemory.load(args.checkpoint)
    if restored.payload() != memory.payload():
        raise ValueError('checkpoint readback mismatch')
    print(json.dumps({k: result[k] for k in ('choices', 'economics', 'feedback_events_in_checkpoint')}))


if __name__ == '__main__':
    main()
