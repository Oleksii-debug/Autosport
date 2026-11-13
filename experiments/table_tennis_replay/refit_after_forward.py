"""Append only verified prospective rows to the last research model's inputs."""
import argparse
from collections import Counter
import json
from pathlib import Path

from forward_residual_trial import ACTIONS, MODELS, RESULTS, load_capture, now, sha, write
from h2h_exploratory import instant
from market_residual_calibration import fit
from verify_forward_residual import verify, settlement


def refit(training, previous_model, cohorts, cutoff):
    model_raw = previous_model.read_bytes()
    if sha(model_raw) != MODELS['adaptive']:
        raise ValueError('wrong previous model')
    old = json.loads(model_raw)
    prior_raw = training.read_bytes()
    if sha(prior_raw) != old['source_sha256']:
        raise ValueError('prior training data do not bind to the model')
    prior = json.loads(prior_raw)
    new, all_decisions, metadata = [], [], []
    used = set(old['training_ids'])
    if used != {r['fixture_id'] for r in prior} or len(used) != len(prior):
        raise ValueError('prior training population mismatch')
    for root, digest, count in cohorts:
        root = Path(root)
        audit = verify(root, digest, count)
        evaluation_raw = (root / f'evaluation-{count}.json').read_bytes()
        evaluation = json.loads(evaluation_raw)
        frozen = json.loads((root / 'forecasts.json').read_bytes())
        for row in frozen['rows']:
            if row['fixture_id'] in used:
                raise ValueError('overlapping forward cohort or training')
            used.add(row['fixture_id'])
        seen_results = set()
        date = instant(frozen['decision_at']).date().isoformat()
        for n in range(1, count + 1):
            src, rec = load_capture(root, f'results-{n}', 'results', RESULTS+'&date='+date)
            at = instant(rec['received_at'])
            if at >= instant(cutoff):
                raise ValueError('receipt beyond next-model cutoff')
            seen_results.update(q['fixture_id'] for q in src['results'])
        for r in evaluation['settled_rows']:
            # Local availability is deliberately conservative: even when an ID
            # was present earlier, we use the last audited snapshot's receipt.
            if r['fixture_id'] not in seen_results:
                raise ValueError('settled result missing in source')
            new.append({**r, 'result_updated_at': rec['received_at'], 'availability_basis': 'final_audited_local_receipt'})
        all_decisions.extend(evaluation['settled_rows'])
        metadata.append({'forecast_sha256': digest, 'evaluation_sha256': sha(evaluation_raw), **audit})
    if not new:
        raise ValueError('no new verified training rows')
    payload = prior + new
    raw = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
    model = fit(payload, cutoff=cutoff, source_sha256=sha(raw))
    model.update({'prior_model_sha256': MODELS['adaptive'], 'new_forward_rows': len(new),
                  'fitted_at': now().isoformat(), 'evaluation_is_in_sample_after_refit': True,
                  'new_holdout_needed': True})
    returns = {p: settlement(all_decisions, p) for p in ACTIONS}
    summary = {'schema': 'research.residual_forward_memory.v1', 'cohorts': metadata,
               'previous_training_rows': len(prior), 'new_training_rows': len(new),
               'training_sha256': model['source_sha256'], 'old_weights': old['weights'],
               'new_weights': model['weights'], 'zero_weight_bootstraps': Counter(map(tuple, model['bootstrap_weights']))[(0., 0.)],
               'frozen_paper_returns': {p: dict(zip(('bets','wins','net_fraction','stress_fraction','drawdown_fraction'),
                                      (str(x) for x in values), strict=True)) for p, values in returns.items()},
               'new_model_tested_on_untouched_results': False, 'profitability_proven': False,
               'production_promotion': False, 'real_money_execution': False}
    return payload, model, summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('output', type=Path)
    p.add_argument('prior_training', type=Path)
    p.add_argument('prior_model', type=Path)
    p.add_argument('--cohort', action='append', nargs=3, metavar=('DIR','HASH','FOLLOWUPS'), required=True)
    p.add_argument('--cutoff', required=True)
    a = p.parse_args()
    payload, model, report = refit(a.prior_training, a.prior_model, [(r, h, int(n)) for r,h,n in a.cohort], a.cutoff)
    a.output.mkdir(mode=0o700)
    write(a.output, 'training.json', payload)
    if sha((a.output/'training.json').read_bytes()) != model['source_sha256']:
        raise ValueError('written training checksum mismatch')
    write(a.output, 'model.json', model)
    report['next_model_sha256'] = sha((a.output/'model.json').read_bytes())
    write(a.output, 'memory.json', report)
    print(json.dumps(report))
