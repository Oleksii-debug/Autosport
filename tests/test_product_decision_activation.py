from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionModelConfig,
)
from autosport.product_decision_activation import (
    BuiltInIntentProducer,
    ProductDecisionActivationBinding,
    ProductDecisionActivationError,
    ProductDecisionActivationStore,
)
from autosport.risk import PaperRiskPolicy
from autosport.scientific_registry import (
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)


class ProductDecisionActivationTests(unittest.TestCase):
    STRATEGY_ID = "paper-strategy-v1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.test_root = Path(self._tmp.name)
        self.workspace = self.test_root / "workspace"
        self.workspace.mkdir()
        self.authority_root = self.test_root / "machine-authority"
        self._authority_env = mock.patch.dict(
            os.environ,
            {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(self.authority_root)},
        )
        self._authority_env.start()
        self.addCleanup(self._authority_env.stop)
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
                created_at="2026-09-23T12:00:00+00:00",
            )
        )
        self.goal = EconomicGoalContract(
            goal_id="owner-goal",
            revision=1,
            bankroll_id="bankroll-main",
            currency="EUR",
        )
        self.risk = PaperRiskPolicy(economic_goal=self.goal)
        self.execution = PaperExecutionModelConfig(
            model_id="paper-execution",
            model_version="1",
            evidence_grade=EvidenceGrade.CONFIGURED,
            evidence_source="owner-config",
            seed="stable-seed",
            max_quote_age_ms=5000,
        )
        self._write_composition(source_id="provider-a", bankroll="1000")
        self._write_risk(self.risk, self.goal)
        self.store = ProductDecisionActivationStore(self.workspace)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _json(payload: object, *, pretty: bool = False) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )

    def _write_json(
        self, path: Path, payload: object, *, pretty: bool = False
    ) -> None:
        path.write_text(self._json(payload, pretty=pretty), encoding="utf-8")

    def _write_composition(self, *, source_id: str, bankroll: str) -> None:
        self._write_json(
            self.workspace / "product_composition.json",
            {
                "schema": "autosport.autonomous_product_composition",
                "schema_version": 2,
                "source_id": source_id,
                "initial_bankroll": bankroll,
                "settlement_authority_identity": None,
            },
        )

    def _write_risk(
        self,
        policy: PaperRiskPolicy,
        goal: EconomicGoalContract,
        *,
        pretty: bool = False,
    ) -> None:
        self._write_json(
            self.workspace / "paper_risk_policy.json",
            {
                "schema": "autosport.paper_risk_policy",
                "schema_version": 1,
                "policy": {
                    "max_ticket_fraction": str(policy.max_ticket_fraction),
                    "max_committed_fraction": str(
                        policy.max_committed_fraction
                    ),
                    "minimum_cash_reserve_fraction": str(
                        policy.minimum_cash_reserve_fraction
                    ),
                    "economic_goal_contract_sha256": provenance_for(
                        goal
                    ).contract_sha256,
                },
                "policy_provenance_sha256": policy.provenance_sha256,
            },
            pretty=pretty,
        )

    def _initialize(self):
        return self.store.initialize_owner(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
        )

    def _verify(self, **overrides):
        arguments = {
            "scientific_registry": self.registry,
            "strategy_version_id": self.STRATEGY_ID,
            "economic_goal": self.goal,
            "risk_policy": self.risk,
            "execution_config": self.execution,
        }
        arguments.update(overrides)
        return self.store.verify(**arguments)

    def test_create_reload_verify_and_exact_retry_are_idempotent(self) -> None:
        first = self._initialize()
        second = self._initialize()
        verified = self._verify()

        self.assertEqual(first, second)
        self.assertEqual(first, verified)
        self.assertEqual(self.store.load().binding_sha256, first.binding_sha256)
        self.assertEqual(first.execution_mode, "PAPER")
        self.assertEqual(
            first.intent_producer_id,
            BuiltInIntentProducer.REGISTERED_STRATEGY.value,
        )

    def test_committed_activation_deletion_cannot_reinitialize(self) -> None:
        committed = self._initialize()
        self.store.path.unlink()

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "anti-rollback authority rejected",
        ):
            self._initialize()

        self.assertFalse(self.store.path.exists())
        self.assertEqual(committed.strategy_version_id, self.STRATEGY_ID)

    def test_structurally_valid_rebound_activation_is_rejected(self) -> None:
        self._initialize()
        root = json.loads(self.store.path.read_text(encoding="utf-8"))
        root["binding"]["product_source_id"] = "provider-b"
        encoded = json.dumps(
            root["binding"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        root["binding_sha256"] = hashlib.sha256(encoded).hexdigest()
        self._write_json(self.store.path, root)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "anti-rollback authority rejected",
        ):
            self.store.load()

    def test_prepare_without_publish_recovers_then_creates_once(self) -> None:
        with mock.patch(
            "autosport.product_decision_activation.atomic_write_json",
            side_effect=RuntimeError("synthetic pre-publish crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "pre-publish"):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        recovered = self._initialize()
        self.assertEqual(recovered.strategy_version_id, self.STRATEGY_ID)
        self.assertEqual(self.store.load(), recovered)

    def test_publish_without_commit_recovers_exact_prepared_activation(self) -> None:
        with mock.patch.object(
            self.store._authority,
            "commit",
            side_effect=RuntimeError("synthetic post-publish crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "post-publish"):
                self._initialize()

        self.assertTrue(self.store.path.exists())
        reopened = ProductDecisionActivationStore(self.workspace)
        recovered = reopened.initialize_owner(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
        )
        self.assertEqual(reopened.load(), recovered)

    def test_registry_append_after_strategy_keeps_frozen_prefix_valid(self) -> None:
        frozen = self._initialize()
        self.registry.append(
            ResearchQuestion(
                question_id="later-question",
                statement="Append-only growth must not invalidate activation.",
                source_sha256="4" * 64,
                created_at="2026-09-23T13:00:00+00:00",
            )
        )

        verified = self._verify()

        self.assertEqual(
            verified.registry_prefix_sha256,
            frozen.registry_prefix_sha256,
        )
        self.assertEqual(
            verified.registry_prefix_record_count,
            frozen.registry_prefix_record_count,
        )

    def test_registry_strategy_tamper_is_rejected(self) -> None:
        self._initialize()
        path = self.workspace / "scientific_registry.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][0]["payload"]["config_sha256"] = "9" * 64
        self._write_json(path, payload)

        with self.assertRaises((ProductDecisionActivationError, ValueError)):
            self._verify()

    def test_same_goal_labels_with_changed_semantics_are_rejected(self) -> None:
        self._initialize()
        changed_goal = replace(
            self.goal, max_stake_fraction=Decimal("0.01")
        )
        changed_risk = PaperRiskPolicy(economic_goal=changed_goal)
        self._write_risk(changed_risk, changed_goal)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches durable authority",
        ):
            self._verify(
                economic_goal=changed_goal,
                risk_policy=changed_risk,
            )

    def test_changed_risk_policy_is_rejected_even_with_same_goal(self) -> None:
        self._initialize()
        changed_risk = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.01"),
            economic_goal=self.goal,
        )
        self._write_risk(changed_risk, self.goal)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches durable authority",
        ):
            self._verify(risk_policy=changed_risk)

    def test_risk_file_must_match_executable_policy_and_goal(self) -> None:
        payload = json.loads(
            (self.workspace / "paper_risk_policy.json").read_text(
                encoding="utf-8"
            )
        )
        payload["policy_provenance_sha256"] = "a" * 64
        self._write_json(self.workspace / "paper_risk_policy.json", payload)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "does not match executable risk provenance",
        ):
            self._initialize()

    def test_exact_risk_evidence_bytes_are_bound(self) -> None:
        self._initialize()
        self._write_risk(self.risk, self.goal, pretty=True)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches durable authority",
        ):
            self._verify()

    def test_execution_model_drift_is_rejected(self) -> None:
        self._initialize()
        changed = replace(self.execution, max_slippage_bps=1)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches durable authority",
        ):
            self._verify(execution_config=changed)

    def test_provider_manifest_change_cannot_relabel_activation(self) -> None:
        self._initialize()
        self._write_composition(source_id="provider-b", bankroll="1000")

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches durable authority",
        ):
            self._verify()

    def test_bankroll_manifest_change_is_rejected(self) -> None:
        self._initialize()
        self._write_composition(source_id="provider-a", bankroll="1001")

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches durable authority",
        ):
            self._verify()

    def test_registry_path_substitution_is_rejected(self) -> None:
        self._initialize()
        other_path = self.workspace / "other_registry.json"
        other_path.write_bytes(
            (self.workspace / "scientific_registry.json").read_bytes()
        )
        other = ScientificRegistry(other_path)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "canonical workspace path",
        ):
            self.store.verify(
                scientific_registry=other,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=self.goal,
                risk_policy=self.risk,
                execution_config=self.execution,
            )

    def test_arbitrary_intent_producer_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "closed product registry",
        ):
            self.store.initialize_owner(
                scientific_registry=self.registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=self.goal,
                risk_policy=self.risk,
                execution_config=self.execution,
                intent_producer="module:function",  # type: ignore[arg-type]
            )

    def test_binding_digest_tamper_is_rejected(self) -> None:
        self._initialize()
        root = json.loads(self.store.path.read_text(encoding="utf-8"))
        root["binding_sha256"] = "0" * 64
        self._write_json(self.store.path, root)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "binding digest mismatch",
        ):
            self.store.load()

    def test_duplicate_activation_json_key_is_rejected(self) -> None:
        self._initialize()
        text = self.store.path.read_text(encoding="utf-8")
        self.store.path.write_text(
            text[:-1]
            + ',"schema":"autosport.product_decision_activation"}',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "strict UTF-8 JSON",
        ):
            self.store.load()

    def test_non_paper_binding_cannot_be_made_valid_by_rehashing(self) -> None:
        self._initialize()
        root = json.loads(self.store.path.read_text(encoding="utf-8"))
        root["binding"]["execution_mode"] = "REAL"
        encoded = json.dumps(
            root["binding"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        root["binding_sha256"] = hashlib.sha256(encoded).hexdigest()
        self._write_json(self.store.path, root)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "PAPER-only",
        ):
            self.store.load()

    def test_binding_payload_rejects_authority_widening_fields(self) -> None:
        binding = self._initialize()
        payload = binding.to_payload()
        payload["provider_write_enabled"] = True

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "fields mismatch",
        ):
            ProductDecisionActivationBinding.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
