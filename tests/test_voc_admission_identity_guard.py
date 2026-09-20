from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from autosport.voc_evaluation import VOCEvaluationError
from autosport.voc_outcome_scoring import append_paired_voc_admission


def _fixture_module():
    path = Path(__file__).with_name("test_voc_outcome_scoring.py")
    spec = importlib.util.spec_from_file_location(
        "_voc_admission_identity_fixture", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load VOC scoring fixture module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_FIXTURE = _fixture_module()


class VOCAdmissionIdentityGuardTests(unittest.TestCase):
    def _fixture(self):
        fixture = _FIXTURE.CanonicalOutcomeDerivedVOCScoreAuthorityTests(
            methodName="runTest"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        target = fixture._evaluation()
        fixture.registry.append(target)
        return fixture, target

    @staticmethod
    def _scope(target):
        return {
            "sport_id": target.sport_id,
            "league_id": target.league_id,
            "regime_id": target.regime_id,
            "urgency_id": target.urgency_id,
            "contradiction_state": target.contradiction_state,
        }

    @staticmethod
    def _baseline(target):
        return {
            "candidate_id": target.baseline_candidate_id,
            "backend_id": target.baseline_backend_id,
            "model_id": target.baseline_model_id,
            "config_sha256": target.baseline_config_sha256,
        }

    @staticmethod
    def _challenger(target):
        return {
            "candidate_id": target.challenger_candidate_id,
            "backend_id": target.challenger_backend_id,
            "model_id": target.challenger_model_id,
            "config_sha256": target.challenger_config_sha256,
        }

    def _append_explicit_admission(
        self,
        fixture,
        target,
        *,
        protocol_id: str,
        cohort_id: str,
        router=None,
    ):
        if router is None:
            router = fixture._precommit_router(
                target,
                protocol_id=protocol_id,
                cohort_id=cohort_id,
            )
        append_paired_voc_admission(
            fixture.ledger,
            admission_id=f"explicit:{target.evaluation_id}",
            decision_context_sha256=target.decision_context_sha256,
            decision_input_sha256=target.decision_input_sha256,
            decision_deadline=target.decision_deadline,
            research_protocol_id=protocol_id,
            cohort_id=cohort_id,
            task_class=target.task_class,
            scope=self._scope(target),
            baseline_compute_identity=self._baseline(target),
            challenger_compute_identity=self._challenger(target),
            replay_run_id="replay-foreign-voc-admission",
            agent="voc-admission-identity-test",
            recorded_at=_FIXTURE.T_DECISION,
        )
        return router

    def test_router_precompute_episode_cannot_be_omitted_from_explicit_cohort(self) -> None:
        fixture, target = self._fixture()
        router = fixture._precommit_router(
            target,
            include_omitted_request=True,
        )
        self._append_explicit_admission(
            fixture,
            target,
            protocol_id=target.research_protocol_id,
            cohort_id="voc-cohort-derived",
            router=router,
        )

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "canonical router VOC precompute admission is missing from DecisionLedger",
        ):
            fixture._authority(compute_execution_store=router).resolve(
                target.evaluation_id,
                as_of=_FIXTURE.T_AS_OF,
            )

    def test_foreign_protocol_cannot_enter_target_scored_denominator(self) -> None:
        fixture, target = self._fixture()
        router = self._append_explicit_admission(
            fixture,
            target,
            protocol_id="foreign-research-protocol",
            cohort_id="voc-cohort-derived",
        )

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "protocol/cohort does not match canonical target",
        ):
            fixture._authority(compute_execution_store=router).resolve(
                target.evaluation_id,
                as_of=_FIXTURE.T_AS_OF,
            )

    def test_foreign_cohort_cannot_enter_target_scored_denominator(self) -> None:
        fixture, target = self._fixture()
        router = self._append_explicit_admission(
            fixture,
            target,
            protocol_id=target.research_protocol_id,
            cohort_id="foreign-voc-cohort",
        )

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "protocol/cohort does not match canonical target",
        ):
            fixture._authority(compute_execution_store=router).resolve(
                target.evaluation_id,
                as_of=_FIXTURE.T_AS_OF,
            )


if __name__ == "__main__":
    unittest.main()
