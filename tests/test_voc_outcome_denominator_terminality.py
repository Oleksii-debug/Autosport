from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from autosport.decision_ledger import DecisionRecord
from autosport.voc_evaluation import VOCEvaluationError


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
            if isinstance(record.payload, dict)
            and isinstance(record.payload.get("voc_binding"), dict)
        )
        source_context = next(
            record
            for record in records
            if isinstance(record.payload, dict)
            and isinstance(record.payload.get("voc_current_context"), dict)
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
            "eligible VOC decision lacks terminal scoring evidence",
        ):
            fixture._authority().resolve(target.evaluation_id, as_of=_FIXTURE.T_AS_OF)


if __name__ == "__main__":
    unittest.main()
