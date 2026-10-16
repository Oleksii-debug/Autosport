from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from autosport.learning_evidence_projection import verified_learning_projection

NOW = datetime(2026, 10, 16, 0, 15, tzinfo=timezone.utc)


def _entry(identity, kind, value):
    return SimpleNamespace(
        record_id=identity, record_sha256='a'*64,
        available_at='2026-10-15T23:00:00+00:00',
        payload={kind: value},
    )


class Registry:
    def __init__(self, path: Path):
        self.path = path
    def causal_records(self, typ, *, as_of):
        assert as_of == NOW.isoformat()
        if typ == 'Experiment':
            return (_entry('sensitive-secret-experiment-id', 'outcome', 'NEGATIVE'),)
        if typ == 'PromotionDecision':
            return (_entry('promotion-id', 'action', 'ROLLBACK'),)
        return ()


def test_missing_learning_evidence_is_not_success(tmp_path):
    report = verified_learning_projection(tmp_path, as_of=NOW)
    assert report['science']['state'] == 'NOT_PRESENT'
    assert report['research_runs']['state'] == 'NOT_PRESENT'
    assert report['agent_loop']['state'] == 'NOT_PRESENT'
    assert report['knowledge_retention']['status'] == 'NOT_EVALUATED'
    assert report['proven_live_model_learning'] is False
    assert report['economic_improvement_proven_by_this_report'] is False


def test_verified_registry_projects_actual_negative_and_rollback_but_not_secrets(tmp_path):
    (tmp_path/'scientific-registry.json').write_text('{}')
    report = verified_learning_projection(tmp_path, as_of=NOW, registry_factory=Registry)
    assert report['science']['state'] == 'CANONICAL_REGISTRY_VERIFIED'
    assert report['science']['records_total'] == 2
    assert report['science']['experiment_outcomes'] == {'NEGATIVE': 1}
    assert report['science']['promotion_decisions'] == {'ROLLBACK': 1}
    encoded = json.dumps(report)
    assert 'sensitive-secret-experiment-id' not in encoded
    assert 'promotion-id' not in encoded
    assert report['proven_live_model_learning'] is False


def test_research_and_agent_restart_truth_is_read_only_and_nonpromoting(tmp_path):
    for name in ('scientific-registry.json', 'research-supervisor.json', 'agent-loop.json'):
        (tmp_path/name).write_text('{}')
    def supervisor_factory(path, registry):
        assert isinstance(registry, Registry)
        assert path.name == 'research-supervisor.json'
        run = SimpleNamespace(
            run_id='secret-token-like-id',
            phase=SimpleNamespace(value='COMPLETE'),
            status=SimpleNamespace(value='SUCCEEDED'),
            checkpoint_sha256='b'*64,
            consumed_budget_units=7,
            updated_at='2026-10-15T23:00:00Z',
        )
        return SimpleNamespace(list_runs=lambda: (run,))
    def agent_loop_factory(path):
        return SimpleNamespace(snapshot=lambda: SimpleNamespace(
            phase=SimpleNamespace(value='RESEARCH'),
            external_effect_state=SimpleNamespace(value='NONE'),
            sequence=19, updated_at='2026-10-15T23:30:00Z', state_sha256='c'*64,
            postmortem_id='failure-42', research_run_id='run-55',
            economic_goal_fingerprint='d'*64,
        ))
    report = verified_learning_projection(
        tmp_path, as_of=NOW, registry_factory=Registry,
        supervisor_factory=supervisor_factory, agent_loop_factory=agent_loop_factory,
    )
    assert report['research_runs']['status_counts']['SUCCEEDED'] == 1
    assert report['agent_loop']['postmortem_recorded'] is True
    assert report['agent_loop']['research_handoff_recorded'] is True
    assert report['knowledge_retention']['status'] == 'NOT_EVALUATED'
    assert report['proven_live_model_learning'] is False
    assert 'secret-token-like-id' not in json.dumps(report)


def test_ambiguous_scientific_registry_fails_closed(tmp_path):
    (tmp_path/'scientific-registry.json').write_text('{}')
    (tmp_path/'scientific_registry.json').write_text('{}')
    with pytest.raises(ValueError, match='ambiguous'):
        verified_learning_projection(tmp_path, as_of=NOW, registry_factory=Registry)


def test_agent_without_canonical_scientific_registry_is_not_falsely_reported(tmp_path):
    (tmp_path/'research-supervisor.json').write_text('{}')
    with pytest.raises(ValueError, match='requires'):
        verified_learning_projection(tmp_path, as_of=NOW)


def test_symlink_registry_is_not_imported(tmp_path):
    outside = tmp_path.parent/'some-outside-file.txt'
    outside.write_text('{}')
    (tmp_path/'scientific-registry.json').symlink_to(outside)
    with pytest.raises(ValueError, match='ordinary'):
        verified_learning_projection(tmp_path, as_of=NOW)


def test_registry_uses_actual_positive_null_harmful_and_retain_taxonomy(tmp_path):
    (tmp_path/'scientific-registry.json').write_text('{}')
    class FullTaxonomyRegistry(Registry):
        def causal_records(self, typ, *, as_of):
            if typ == 'Experiment':
                return tuple(_entry('exp-'+v, 'outcome', v) for v in ('POSITIVE','NULL','HARMFUL'))
            if typ == 'PromotionDecision':
                return (_entry('retain-1','action','RETAIN'),)
            if typ == 'PromotionEvidence':
                return (SimpleNamespace(
                    record_id='evidence-1', record_sha256='e'*64,
                    available_at='2026-10-15T23:00:00+00:00',
                    payload={'validity':'INCONCLUSIVE','guardrails_passed':False,'holdout_consumed':False},
                ),)
            return ()
    report = verified_learning_projection(tmp_path,as_of=NOW,registry_factory=FullTaxonomyRegistry)
    info=report['science']
    assert info['experiment_outcomes'] == {'HARMFUL':1,'NULL':1,'POSITIVE':1}
    assert info['promotion_decisions']=={'RETAIN':1}
    assert info['promotion_evidence_validity']=={'INCONCLUSIVE':1}
    assert info['promotion_evidence_guardrail_failures']==1
    assert info['promotion_evidence_holdout_missing']==1
    assert report['proven_live_model_learning'] is False
