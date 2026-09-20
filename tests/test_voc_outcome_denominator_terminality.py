from __future__ import annotations

import importlib.util
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path

from autosport.decision_ledger import DecisionRecord
from autosport.voc_evaluation import VOCEvaluationError
from autosport.voc_outcome_scoring import append_paired_voc_admission


def _fixture_module():
    path = Path(__file__).with_name("test_voc_outcome_scoring.py")
    spec = importlib.util.spec_from_file_location("_voc_outcome_scoring_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load VOC scoring fixture module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_FIXTURE = _fixture_module()


class VOCOutcomeDenominatorTerminalityTests(unittest.TestCase):
    def test_matching_preoutput_admission_without_any_terminal_cannot_disappear(self):
        fixture = _FIXTURE.CanonicalOutcomeDerivedVOCScoreAuthorityTests(
            methodName="runTest"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        target = fixture._evaluation()
        fixture.registry.append(target)

        source_context = next(
            record
            for record in fixture.ledger.verified_records()
            if isinstance(record.payload, Mapping)
            and isinstance(record.payload.get("voc_current_context"), Mapping)
        )
        missing_input = _FIXTURE.digest(
            {"episode": "prebinding-failure", "kind": "input"}
        )
        context_payload = dict(source_context.payload["voc_current_context"])
        context_payload["request_id"] = "source:voc-prebinding-failure"
        context_payload["decision_input_sha256"] = missing_input
        fixture.ledger.append(
            DecisionRecord(
                replay_run_id="replay-voc-prebinding-failure-context",
                agent="voc-derived-test",
                observed_ts=_FIXTURE.T_DECISION,
                action="VOC_ROUTE_CONTEXT",
                payload={"voc_current_context": context_payload},
                context_hash=missing_input,
                decision_id="decision-voc-prebinding-failure-context",
                recorded_at=_FIXTURE.T_DECISION,
            )
        )

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "eligible VOC admission lacks terminal paired record",
        ):
            fixture._authority().resolve(target.evaluation_id, as_of=_FIXTURE.T_AS_OF)

    def test_matching_admitted_attempt_without_scoring_cannot_disappear(self):
        fixture = _FIXTURE.CanonicalOutcomeDerivedVOCScoreAuthorityTests(
            methodName="runTest"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        target = fixture._evaluation()
        fixture.registry.append(target)

        records = fixture.ledger.verified_records()
        successful = next(
            record
            for record in records
            if isinstance(record.payload, Mapping)
            and isinstance(record.payload.get("voc_binding"), Mapping)
        )
        source_context = next(
            record
            for record in records
            if isinstance(record.payload, Mapping)
            and isinstance(record.payload.get("voc_current_context"), Mapping)
        )

        missing_input = _FIXTURE.digest(
            {"episode": "missing-terminal-voc", "kind": "input"}
        )
        context_payload = dict(source_context.payload["voc_current_context"])
        context_payload["request_id"] = "source:voc-missing-terminal"
        context_payload["decision_input_sha256"] = missing_input
        context_record = DecisionRecord(
            replay_run_id="replay-voc-missing-terminal-context",
            agent="voc-derived-test",
            observed_ts=_FIXTURE.T_DECISION,
            action="VOC_ROUTE_CONTEXT",
            payload={"voc_current_context": context_payload},
            context_hash=missing_input,
            decision_id="decision-voc-missing-terminal-context",
            recorded_at=_FIXTURE.T_DECISION,
        )
        context_sha = fixture.ledger.append(context_record)

        binding = dict(successful.payload["voc_binding"])
        binding["decision_input_sha256"] = missing_input
        binding["decision_context_sha256"] = context_sha
        binding["baseline_output_sha256"] = _FIXTURE.digest(
            {"episode": "missing-terminal-voc", "kind": "baseline"}
        )
        binding["challenger_output_sha256"] = _FIXTURE.digest(
            {"episode": "missing-terminal-voc", "kind": "challenger"}
        )
        fixture.ledger.append(
            DecisionRecord(
                replay_run_id="replay-voc-missing-terminal",
                agent="voc-derived-test",
                observed_ts=_FIXTURE.T_DECISION,
                action="BASE",
                payload={
                    "voc_binding": binding,
                    "voc_terminal_status": "timeout",
                },
                context_hash=missing_input,
                decision_id="decision-voc-missing-terminal",
                recorded_at=_FIXTURE.T_BINDING,
            )
        )

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "legacy VOC admission terminated without score",
        ):
            fixture._authority().resolve(target.evaluation_id, as_of=_FIXTURE.T_AS_OF)

    def test_legacy_failure_plus_explicit_success_requires_migration_boundary(self):
        fixture = _FIXTURE.CanonicalOutcomeDerivedVOCScoreAuthorityTests(
            methodName="runTest"
        )
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        target = fixture._evaluation()
        fixture.registry.append(target)
        records = fixture.ledger.verified_records()
        successful = next(
            record
            for record in records
            if isinstance(record.payload, Mapping)
            and isinstance(record.payload.get("voc_binding"), Mapping)
        )
        source_context = next(
            record
            for record in records
            if isinstance(record.payload, Mapping)
            and isinstance(record.payload.get("voc_current_context"), Mapping)
        )
        source_context_sha = successful.payload["voc_binding"][
            "decision_context_sha256"
        ]

        # A legacy-format cohort member fails without a canonical score.
        legacy_input = _FIXTURE.digest(
            {"episode": "legacy-failure-before-migration", "kind": "input"}
        )
        legacy_context = dict(source_context.payload["voc_current_context"])
        legacy_context["request_id"] = "source:legacy-failure-before-migration"
        legacy_context["decision_input_sha256"] = legacy_input
        legacy_context_sha = fixture.ledger.append(
            DecisionRecord(
                replay_run_id="replay-legacy-failure-context",
                agent="voc-derived-test",
                observed_ts=_FIXTURE.T_DECISION,
                action="VOC_ROUTE_CONTEXT",
                payload={"voc_current_context": legacy_context},
                context_hash=legacy_input,
                decision_id="decision-legacy-failure-context",
                recorded_at=_FIXTURE.T_DECISION,
            )
        )
        legacy_binding = dict(successful.payload["voc_binding"])
        legacy_binding["decision_input_sha256"] = legacy_input
        legacy_binding["decision_context_sha256"] = legacy_context_sha
        legacy_binding["baseline_output_sha256"] = _FIXTURE.digest(
            {"episode": "legacy-failure-before-migration", "kind": "baseline"}
        )
        legacy_binding["challenger_output_sha256"] = _FIXTURE.digest(
            {"episode": "legacy-failure-before-migration", "kind": "challenger"}
        )
        fixture.ledger.append(
            DecisionRecord(
                replay_run_id="replay-legacy-failure-terminal",
                agent="voc-derived-test",
                observed_ts=_FIXTURE.T_DECISION,
                action="BASE",
                payload={
                    "voc_binding": legacy_binding,
                    "voc_terminal_status": "timeout",
                },
                context_hash=legacy_input,
                decision_id="decision-legacy-failure-terminal",
                recorded_at=_FIXTURE.T_BINDING,
            )
        )

        # The same frozen window then begins writing explicit admissions.  Even
        # with a scored terminal for that explicit member, the older legacy
        # member may not silently disappear from the denominator.
        router = fixture._precommit_router(target)
        admission_sha = append_paired_voc_admission(
            fixture.ledger,
            admission_id=f"explicit:{target.evaluation_id}",
            decision_context_sha256=source_context_sha,
            decision_input_sha256=target.decision_input_sha256,
            decision_deadline=target.decision_deadline,
            research_protocol_id=target.research_protocol_id,
            cohort_id="voc-cohort-derived",
            task_class=target.task_class,
            scope={
                "sport_id": target.sport_id,
                "league_id": target.league_id,
                "regime_id": target.regime_id,
                "urgency_id": target.urgency_id,
                "contradiction_state": target.contradiction_state,
            },
            baseline_compute_identity={
                "candidate_id": target.baseline_candidate_id,
                "backend_id": target.baseline_backend_id,
                "model_id": target.baseline_model_id,
                "config_sha256": target.baseline_config_sha256,
            },
            challenger_compute_identity={
                "candidate_id": target.challenger_candidate_id,
                "backend_id": target.challenger_backend_id,
                "model_id": target.challenger_model_id,
                "config_sha256": target.challenger_config_sha256,
            },
            replay_run_id="replay-explicit-success-admission",
            agent="voc-derived-test",
            recorded_at=_FIXTURE.T_DECISION,
        )
        self.assertEqual(len(admission_sha), 64)
        fixture.ledger.append(
            DecisionRecord(
                replay_run_id="replay-explicit-success-terminal",
                agent="voc-derived-test",
                observed_ts=_FIXTURE.T_BINDING,
                action=successful.action,
                payload=successful.to_dict()["payload"],
                context_hash=target.decision_input_sha256,
                decision_id="decision-explicit-success-terminal",
                recorded_at=_FIXTURE.T_BINDING,
            )
        )

        with self.assertRaisesRegex(
            VOCEvaluationError,
            "mixed legacy and explicit VOC cohort formats require a frozen migration boundary",
        ):
            fixture._authority(compute_execution_store=router).resolve(
                target.evaluation_id,
                as_of=_FIXTURE.T_AS_OF,
            )


if __name__ == "__main__":
    unittest.main()
