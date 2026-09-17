import pytest

from autosport.research_supervisor import (
    ResearchPhase,
    ResearchSupervisorError,
    SupervisorStatus,
)
from autosport.research_supervisor_actions import stage_factory_evaluation
from autosport.strategy_model_factory import ExperimentRunner
from test_research_supervisor_actions import _advance_to, _supervisor_for_factory
from test_strategy_model_factory import (
    T7,
    _candidate_points,
    _candidate_spec,
    _factory_foundation,
)


def test_stage_factory_stopped_run_has_no_scientific_registry_side_effect(tmp_path):
    registry, _, rule, store, _, _ = _factory_foundation(tmp_path)
    runner = ExperimentRunner(registry, store)
    supervisor, run_id = _supervisor_for_factory(tmp_path, registry)
    _advance_to(supervisor, run_id, ResearchPhase.EXPERIMENT)
    supervisor.stop(run_id, at=T7, reason="operator cancellation")
    before = registry.path.read_bytes()

    with pytest.raises(ResearchSupervisorError, match="run is not active: STOPPED"):
        stage_factory_evaluation(
            supervisor,
            run_id,
            runner=runner,
            spec=_candidate_spec(),
            points=_candidate_points(),
            rule=rule,
            at=T7,
        )

    assert registry.path.read_bytes() == before
    stopped = supervisor.status(run_id)
    assert stopped.status is SupervisorStatus.STOPPED
    assert stopped.phase is ResearchPhase.EXPERIMENT
    assert stopped.stop_reason == "operator cancellation"
