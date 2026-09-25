from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import autosport.monotonic_workspace_authority as monotonic_authority_module
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.paper_execution_reality import EvidenceGrade, PaperExecutionModelConfig
from autosport.product_decision_activation import (
    ProductDecisionActivationError,
    ProductDecisionActivationStore,
)
from autosport.risk import PaperRiskPolicy
from autosport.scientific_registry import ScientificRegistry, StrategyVersion


class ProductDecisionActivationCommitReturnGuardTests(unittest.TestCase):
    STRATEGY_ID = "paper-strategy-v1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name) / "workspace"
        self.workspace.mkdir()

        self.registry = ScientificRegistry.initialize_pristine(
            self.workspace / "scientific_registry.json"
        )
        self.registry.append(
            StrategyVersion(
                strategy_version_id=self.STRATEGY_ID,
                canonical_strategy_id="paper-strategy",
                source_sha256="1" * 64,
                environment_sha256="2" * 64,
                config_sha256="3" * 64,
                created_at="2026-09-25T00:00:00+00:00",
            )
        )
        self.goal = EconomicGoalContract(
            goal_id="owner-goal",
            revision=1,
            bankroll_id="bankroll-main",
            currency="EUR",
        )
        EconomicGoalStore(self.workspace).initialize_owner(self.goal)
        self.risk = PaperRiskPolicy(economic_goal=self.goal)
        self.execution = PaperExecutionModelConfig(
            model_id="paper-execution",
            model_version="1",
            evidence_grade=EvidenceGrade.CONFIGURED,
            evidence_source="owner-config",
            seed="stable-seed",
            max_quote_age_ms=5000,
        )
        self._write_json(
            self.workspace / "product_composition.json",
            {
                "schema": "autosport.autonomous_product_composition",
                "schema_version": 2,
                "source_id": "provider-a",
                "initial_bankroll": "1000",
                "settlement_authority_identity": None,
            },
        )
        self._write_json(
            self.workspace / "paper_risk_policy.json",
            {
                "schema": "autosport.paper_risk_policy",
                "schema_version": 1,
                "policy": {
                    "max_ticket_fraction": str(self.risk.max_ticket_fraction),
                    "max_committed_fraction": str(
                        self.risk.max_committed_fraction
                    ),
                    "minimum_cash_reserve_fraction": str(
                        self.risk.minimum_cash_reserve_fraction
                    ),
                    "economic_goal_contract_sha256": provenance_for(
                        self.goal
                    ).contract_sha256,
                },
                "policy_provenance_sha256": self.risk.provenance_sha256,
            },
        )
        self.store = ProductDecisionActivationStore(self.workspace)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )

    def _initialize(self):
        return self.store.initialize_owner(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
        )

    @unittest.skipIf(
        os.name == "nt" or not Path("/proc/self/fd").exists(),
        "deterministic post-COMMIT substitution harness requires POSIX /proc fd paths",
    )
    def test_post_commit_substitution_cannot_escape_as_positive_start(self) -> None:
        canonical_fsync = os.fsync
        records_dir = self.store._authority.records_dir.resolve(strict=False)
        tampered = False

        def tamper_after_commit_record_fsync(fd: int) -> None:
            nonlocal tampered
            canonical_fsync(fd)
            if tampered:
                return
            try:
                target = Path(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:
                return
            if (
                target.parent.resolve(strict=False) == records_dir
                and target.name.startswith("00000000000000000002-")
                and target.name.endswith(".json")
            ):
                # Simulate the non-cooperating filesystem mutation from the exact
                # review falsifier after the COMMIT record itself is durable.
                self.store.path.write_bytes(b"{}\n")
                tampered = True

        with mock.patch.object(
            monotonic_authority_module.os,
            "fsync",
            tamper_after_commit_record_fsync,
        ):
            with self.assertRaises(ProductDecisionActivationError):
                self._initialize()

        self.assertTrue(tampered)
        history = self.store._authority.read_history()
        self.assertEqual(history[-1].phase.value, "COMMIT")
        self.assertNotEqual(
            hashlib.sha256(self.store.path.read_bytes()).hexdigest(),
            history[-1].intended_state_sha256,
        )

    def test_unchanged_committed_activation_still_returns_and_reloads(self) -> None:
        initialized = self._initialize()
        self.assertEqual(self.store.load(), initialized)
        self.assertEqual(
            self.store._authority.read_history()[-1].phase.value,
            "COMMIT",
        )


if __name__ == "__main__":
    unittest.main()
