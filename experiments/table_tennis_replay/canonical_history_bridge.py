"""Read-only archive admission audit against Autosport identity contracts.

Does not create identities, write opponent stores, authorize training or wagers.
Receipt timestamps are capture evidence, not historical availability proof.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from autosport.json_integrity import strict_json_loads
from autosport.learning_environment import EvidenceTruth
from autosport.opponent_intelligence import ObservedPerformance
from autosport.participant_identity import (
    EntityKind, ParticipantIdentityError, ParticipantIdentityRegistry,
)
from h2h_exploratory import instant, result


def validated_rows(archive):
    if archive.get('sport') != 'table_tennis' or not isinstance(archive.get('results'), list):
        raise ValueError('table-tennis results archive required')
    seen, valid, excluded = set(), [], Counter()
    for row in archive['results']:
        ident = row.get('fixture_id')
        if not isinstance(ident, str) or not ident.strip() or ident in seen:
            raise ValueError('missing or duplicate fixture ID')
        seen.add(ident)
        names = [row.get(k) for k in ('home', 'away', 'league')]
        reason = None
        if row.get('status') != 'finished':
            reason = 'not_finished'
        elif row.get('sport', 'table_tennis') != 'table_tennis':
            reason = 'wrong_sport'
        elif not all(isinstance(n, str) and n and n == n.strip() for n in names) or names[0] == names[1]:
            reason = 'ambiguous_names'
        else:
            parsed = result({'result': {'observed_at': row.get('updated_at'), 'periods': row.get('periods')}})
            try:
                chronology = instant(row.get('finished_at')) <= instant(row.get('updated_at'))
            except ValueError:
                chronology = False
            if parsed is None or not chronology:
                reason = 'invalid_result_or_time'
            elif row.get('final_score') is not None and row['final_score'] != ','.join(p['score'] for p in row['periods']):
                reason = 'conflicting_final_score'
        if reason:
            excluded[reason] += 1
        else:
            valid.append((row, parsed[0]))
    return valid, dict(excluded)


def audit(raw, receipt, registry, *, recorded_at):
    """Return structurally resolvable observations, never evidence approval."""
    digest = hashlib.sha256(raw).hexdigest()
    if receipt.get('sha256') != digest:
        raise ValueError('capture receipt digest mismatch')
    if receipt.get('url', '').split('?')[0] != 'https://moviodds.com/results':
        raise ValueError('unsupported receipt source')
    available = receipt['received_at']
    if instant(recorded_at) < instant(available):
        raise ValueError('recorded before capture')
    archive = strict_json_loads(raw.decode('utf8'))
    valid, excluded = validated_rows(archive)
    blocked, candidates = Counter(), []
    for row, winner in valid:
        if instant(row['updated_at']) > instant(available):
            blocked['source_time_after_capture'] += 1
            continue
        try:
            entities = [registry.resolve_alias('moviodds.com', row[k], as_of=available)
                        for k in ('home', 'away', 'league')]
        except ParticipantIdentityError:
            blocked['unresolved_canonical_identity'] += 1
            continue
        if (entities[0].kind is not EntityKind.PARTICIPANT or
                entities[1].kind is not EntityKind.PARTICIPANT or
                entities[2].kind is not EntityKind.LEAGUE or
                entities[0].entity_id == entities[1].entity_id):
            blocked['incompatible_identity'] += 1
            continue
        observation = ObservedPerformance(
            event_id=row['fixture_id'], source_id='moviodds.com',
            subject_alias=row['home'], opponent_alias=row['away'],
            sport_id='table_tennis', league_alias=row['league'],
            market_context_id='match-outcome', score='1' if winner == 0 else '0',
            observed_at=row['updated_at'], available_at=available,
            recorded_at=recorded_at, evidence_sha256=digest, truth=EvidenceTruth.OBSERVED,
        )
        candidates.append(observation.payload())
    return {'schema': 'research.canonical_history_audit.v1', 'source_sha256': digest,
            'source_rows': len(archive['results']), 'valid_results': len(valid),
            'excluded': excluded, 'blocked': dict(blocked), 'candidates': candidates,
            'candidate_count': len(candidates), 'available_at': available,
            'recorded_at': recorded_at, 'rights_verified': False,
            'training_authorized': False, 'real_money_authority': False,
            'note': 'Read-only structural audit; no canonical store mutation. Capture cannot backdate knowledge.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('archive', type=Path)
    p.add_argument('receipt', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--identity', type=Path, required=True)
    p.add_argument('--recorded-at', required=True)
    args = p.parse_args()
    if not args.identity.is_file():
        p.error('--identity must name an existing canonical registry')
    report = audit(args.archive.read_bytes(), strict_json_loads(args.receipt.read_text()),
                   ParticipantIdentityRegistry(args.identity), recorded_at=args.recorded_at)
    with args.output.open('x', encoding='utf8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')


if __name__ == '__main__':
    main()
