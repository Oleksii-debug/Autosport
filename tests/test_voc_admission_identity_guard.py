from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from autosport import _voc_outcome_scoring_base as _base
from autosport.model_compute_router import ModelComputeRouterError, ModelComputeRouterStore
from autosport.voc_evaluation import CanonicalVOCAuthorityResolver, VOCEvaluationError
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

    def test_router_precompute_binds_published_protocol_semantics_and_restart(self) -> None:
        fixture, target = self._fixture()
        router = fixture._precommit_router(target)
        authority = router.get_voc_precompute_admission(f"source:{target.evaluation_id}")
        self.assertIsNotNone(authority)
        assert authority is not None
        protocol = fixture.registry.get("ResearchProtocol", target.research_protocol_id)
        self.assertIsNotNone(protocol)
        assert protocol is not None
        self.assertEqual(authority["schema_version"], 2)
        self.assertEqual(
            authority["research_protocol_sha256"],
            target.research_protocol_sha256,
        )
        self.assertEqual(
            authority["research_protocol_record_sha256"],
            protocol.record_sha256,
        )
        for field in (
            "research_protocol_registry_prefix_sha256",
            "evaluation_design_sha256",
            "cohort_eligibility_sha256",
        ):
            self.assertEqual(len(authority[field]), 64)
        self.assertGreaterEqual(authority["research_protocol_registry_index"], 0)

        reopened = ModelComputeRouterStore(router.path)
        self.assertEqual(
            reopened.get_voc_precompute_admission(f"source:{target.evaluation_id}"),
            authority,
        )

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

    def test_missing_protocol_cannot_be_physically_preadmitted_then_backdated(self) -> None:
        fixture, target = self._fixture()
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "ResearchProtocol must exist before VOC precompute admission",
        ):
            fixture._precommit_router(
                target,
                protocol_id="post-outcome-backdated-protocol",
                cohort_id="voc-cohort-derived",
            )

    def test_foreign_protocol_cannot_enter_target_scored_denominator(self) -> None:
        fixture, target = self._fixture()
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "ResearchProtocol must exist before VOC precompute admission",
        ):
            self._append_explicit_admission(
                fixture,
                target,
                protocol_id="foreign-research-protocol",
                cohort_id="voc-cohort-derived",
            )

    def test_foreign_cohort_cannot_enter_target_scored_denominator(self) -> None:
        fixture, target = self._fixture()
        with self.assertRaisesRegex(
            ModelComputeRouterError,
            "cohort does not match canonical ResearchProtocol",
        ):
            self._append_explicit_admission(
                fixture,
                target,
                protocol_id=target.research_protocol_id,
                cohort_id="foreign-voc-cohort",
            )

    def test_unfenced_base_scorer_is_not_a_positive_authority(self) -> None:
        fixture, target = self._fixture()
        scorer = _base.CanonicalOutcomeDerivedVOCScoreAuthority(
            decision_ledger=fixture.ledger,
            scientific_registry=fixture.registry,
            outcome_authority=fixture.outcome_authority,
            outcome_source_root=fixture.root,
            source_record_file=fixture.outcome_file,
            source_record_sha256=fixture.outcome_sha256,
        )
        with self.assertRaisesRegex(
            VOCEvaluationError,
            "unfenced legacy VOC scorer is disabled",
        ):
            scorer.resolve(target.evaluation_id, as_of=_FIXTURE.T_AS_OF)

        with self.assertRaisesRegex(
            TypeError,
            "exact terminal-aware scorer authority",
        ):
            CanonicalVOCAuthorityResolver(
                fixture.ledger,
                fixture.registry,
                fixture.outcome_authority,
                scorer,
            )


if __name__ == "__main__":
    unittest.main()
