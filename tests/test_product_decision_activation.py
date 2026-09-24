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

import autosport.economic_goal_store as economic_goal_store_module
import autosport.monotonic_workspace_authority as monotonic_authority_module
import autosport.monotonic_workspace_binding as workspace_binding_module
import autosport.product_decision_activation as activation_module
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
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
    ModelVersion,
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
        self._write_composition(source_id="provider-a", bankroll="1000")
        self._write_risk(self.risk, self.goal)
        self.store = ProductDecisionActivationStore(self.workspace)
        self.authority_root = self.store._authority.authority_root

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

    @staticmethod
    def _reseal_registry_record(record: dict[str, object]) -> None:
        envelope = {
            "record_type": record["record_type"],
            "record_id": record["record_id"],
            "available_at": record["available_at"],
            "payload": record["payload"],
        }
        record["record_sha256"] = hashlib.sha256(
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def _install_model_reference(
        self,
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        path = self.workspace / "scientific_registry.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        strategy = state["records"][0]
        model_id = "model-v1"
        model_entry = ScientificRegistry._entry(
            ModelVersion(
                model_version_id=model_id,
                model_family="paper-model",
                artifact_sha256="4" * 64,
                source_sha256="5" * 64,
                environment_sha256="6" * 64,
                dataset_snapshot_id="dataset-v1",
                feature_set_id="features-v1",
                research_protocol_id="protocol-v1",
                seed=7,
                config_sha256="7" * 64,
                created_at="2026-09-23T11:00:00+00:00",
            )
        )
        strategy["payload"]["model_version_id"] = model_id
        self._reseal_registry_record(strategy)
        state["records"].insert(0, model_entry)
        self._write_json(path, state)
        return path, model_entry, strategy

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

    def _stage_activation_prepare(self, *, publish: bool):
        expected = self.store._derive(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
            intent_producer=BuiltInIntentProducer.REGISTERED_STRATEGY,
        )
        root = self.store._root(expected)
        intended = hashlib.sha256(
            activation_module._durable_json_bytes(root)
        ).hexdigest()
        semantic_binding = activation_module._digest(
            {
                "kind": "PRODUCT_DECISION_ACTIVATION_CREATE",
                "activation_filename": "product_decision_activation.json",
                "binding_sha256": expected.binding_sha256,
                "intended_state_sha256": intended,
            }
        )
        tx_id = "activation-test-crash-prefix"
        self.store._authority.prepare(
            tx_id=tx_id,
            observed_state_sha256=None,
            intended_state_sha256=intended,
            semantic_binding_sha256=semantic_binding,
        )
        if publish:
            activation_module.atomic_write_json(self.store.path, root)
            self.assertEqual(
                hashlib.sha256(self.store.path.read_bytes()).hexdigest(),
                intended,
            )
        return expected

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

    def test_store_subclass_cannot_mint_durable_start_authority(self) -> None:
        canonical_expected = self.store._derive(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
            intent_producer=BuiltInIntentProducer.REGISTERED_STRATEGY,
        )
        forged_binding = replace(
            canonical_expected,
            product_source_id="caller-forged-provider",
        )
        derive_calls: list[bool] = []

        class ForgedStore(ProductDecisionActivationStore):
            def _derive(self, **_kwargs):
                derive_calls.append(True)
                return forged_binding

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "exact canonical class",
        ):
            forged_store = ForgedStore(self.workspace)
            forged_store.initialize_owner(
                scientific_registry=self.registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=self.goal,
                risk_policy=self.risk,
                execution_config=self.execution,
            )

        self.assertEqual(derive_calls, [])
        self.assertFalse(self.store.path.exists())

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(
            ProductDecisionActivationStore(self.workspace).load(),
            canonical,
        )

    def test_store_instance_cannot_shadow_authority_derivation(self) -> None:
        canonical_expected = self.store._derive(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
            intent_producer=BuiltInIntentProducer.REGISTERED_STRATEGY,
        )
        forged_binding = replace(
            canonical_expected,
            product_source_id="caller-forged-provider",
        )

        with self.assertRaises(AttributeError):
            setattr(
                self.store,
                "_derive",
                lambda **_kwargs: forged_binding,
            )

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")

    def test_store_class_derive_rebind_cannot_mint_durable_start_authority(
        self,
    ) -> None:
        canonical_expected = self.store._derive(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
            intent_producer=BuiltInIntentProducer.REGISTERED_STRATEGY,
        )
        forged_binding = replace(
            canonical_expected,
            product_source_id="caller-forged-provider",
        )

        def forged_derive(_store, **_kwargs):
            return forged_binding

        with mock.patch.object(
            ProductDecisionActivationStore,
            "_derive",
            forged_derive,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "derivation authority changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(
            ProductDecisionActivationStore(self.workspace).load(),
            canonical,
        )

    def test_store_class_derive_rebind_cannot_fake_verification(self) -> None:
        canonical = self._initialize()
        forged_binding = replace(
            canonical,
            product_source_id="caller-forged-provider",
        )

        def forged_derive(_store, **_kwargs):
            return forged_binding

        with mock.patch.object(
            ProductDecisionActivationStore,
            "_derive",
            forged_derive,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "derivation authority changed",
            ):
                self._verify()

        self.assertEqual(self._verify(), canonical)
        self.assertEqual(self.store.load(), canonical)

    def test_store_workspace_rebind_cannot_cross_sign_foreign_start_authority(
        self,
    ) -> None:
        alternate_workspace = self.test_root / "workspace-b"
        alternate_workspace.mkdir()
        for source in self.workspace.iterdir():
            if source.is_file():
                (alternate_workspace / source.name).write_bytes(source.read_bytes())

        composition_path = alternate_workspace / "product_composition.json"
        alternate_composition = json.loads(
            composition_path.read_text(encoding="utf-8")
        )
        alternate_composition["source_id"] = "provider-b"
        self._write_json(composition_path, alternate_composition)
        alternate_registry = ScientificRegistry(
            alternate_workspace / "scientific_registry.json"
        )

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "construction state is immutable",
        ):
            self.store.workspace = alternate_workspace

        # Bypass the normal slot write fence to prove the authority-bearing methods
        # independently reject a split-brain object: derive workspace B while path
        # and the genuine monotonic authority still belong to workspace A.
        object.__setattr__(self.store, "workspace", alternate_workspace)
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "construction state changed",
        ):
            self.store.initialize_owner(
                scientific_registry=alternate_registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=self.goal,
                risk_policy=self.risk,
                execution_config=self.execution,
            )

        canonical_path = self.workspace / "product_decision_activation.json"
        self.assertFalse(canonical_path.exists())

        object.__setattr__(self.store, "workspace", self.workspace)
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")

        object.__setattr__(self.store, "workspace", alternate_workspace)
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "construction state changed",
        ):
            self.store.load()
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "construction state changed",
        ):
            self.store.verify(
                scientific_registry=alternate_registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=self.goal,
                risk_policy=self.risk,
                execution_config=self.execution,
            )

        object.__setattr__(self.store, "workspace", self.workspace)
        self.assertEqual(self.store.load(), canonical)

    def test_nested_authority_instance_dispatch_shadow_fails_before_start_publication(
        self,
    ) -> None:
        authority = self.store._authority

        authority.read_history = lambda: ()
        authority.recover = lambda **_kwargs: None
        authority.prepare = lambda **_kwargs: None
        authority.commit = lambda **_kwargs: None
        try:
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested anti-rollback authority dispatch changed",
            ):
                self._initialize()
            self.assertFalse(self.store.path.exists())
        finally:
            for name in ("read_history", "recover", "prepare", "commit"):
                delattr(authority, name)

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_nested_authority_class_dispatch_rebind_fails_before_start_publication(
        self,
    ) -> None:
        authority_type = type(self.store._authority)

        def forged_prepare(_authority, **_kwargs):
            return None

        with mock.patch.object(authority_type, "prepare", forged_prepare):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested anti-rollback authority dispatch changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")

    def test_nested_authority_lower_history_rebind_fails_before_start_publication(
        self,
    ) -> None:
        authority_type = type(self.store._authority)

        def forged_load_bound_history(_authority):
            raise AssertionError("forged lower history dispatch must never execute")

        with mock.patch.object(
            authority_type,
            "_load_bound_history",
            forged_load_bound_history,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested anti-rollback authority lower dispatch changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")

    def test_nested_authority_lower_append_rebind_fails_before_start_publication(
        self,
    ) -> None:
        authority_type = type(self.store._authority)

        def forged_append_record(_authority, _record, _index):
            return None

        with mock.patch.object(
            authority_type,
            "_append_record",
            forged_append_record,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested anti-rollback authority lower dispatch changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_nested_authority_lower_instance_shadow_fails_before_start_publication(
        self,
    ) -> None:
        authority = self.store._authority
        authority._load_bound_history = lambda: None
        try:
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested anti-rollback authority lower dispatch changed",
            ):
                self._initialize()
            self.assertFalse(self.store.path.exists())
        finally:
            delattr(authority, "_load_bound_history")

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")

    def test_workspace_binding_global_parser_rebind_cannot_mint_start(
        self,
    ) -> None:
        binding = self.store._authority.workspace_binding
        workspace_payload = binding._workspace_payload()
        path_payload = binding._path_payload()
        binding.workspace_marker_path.parent.mkdir(parents=True, exist_ok=True)
        binding.path_binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding.workspace_marker_path.write_text("{}\n", encoding="utf-8")
        binding.path_binding_path.write_text("{}\n", encoding="utf-8")

        def forged_read_strict_object(path, _expected_keys):
            if path == binding.workspace_marker_path:
                return dict(workspace_payload)
            if path == binding.path_binding_path:
                return dict(path_payload)
            raise AssertionError("unexpected workspace binding path")

        with mock.patch.object(
            workspace_binding_module,
            "_read_strict_object",
            forged_read_strict_object,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested workspace identity binding global trust changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())

    def test_workspace_binding_entrypoint_rebinds_fail_before_start_publication(
        self,
    ) -> None:
        binding_type = type(self.store._authority.workspace_binding)

        def forged_validate_existing(_binding, **_kwargs):
            return True, True

        def forged_ensure_bound(_binding):
            return None

        with mock.patch.object(
            binding_type,
            "validate_existing",
            forged_validate_existing,
        ), mock.patch.object(
            binding_type,
            "ensure_bound",
            forged_ensure_bound,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested workspace identity binding dispatch changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_workspace_binding_lower_helper_rebind_fails_before_start_publication(
        self,
    ) -> None:
        binding_type = type(self.store._authority.workspace_binding)
        workspace_instance_id = self.store._authority.workspace_instance_id

        def forged_read_workspace_marker_id(_path):
            return workspace_instance_id

        with mock.patch.object(
            binding_type,
            "_read_workspace_marker_id",
            staticmethod(forged_read_workspace_marker_id),
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested workspace identity binding dispatch changed",
            ):
                self._initialize()

        self.assertFalse(self.store.path.exists())
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")

    def test_workspace_binding_resolve_rebind_fails_before_store_construction(
        self,
    ) -> None:
        binding_type = type(self.store._authority.workspace_binding)

        def forged_resolve(_cls, **_kwargs):
            raise AssertionError("forged workspace binding resolve must never execute")

        with mock.patch.object(
            binding_type,
            "resolve",
            classmethod(forged_resolve),
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested workspace identity binding dispatch changed",
            ):
                ProductDecisionActivationStore(self.workspace)

        reopened = ProductDecisionActivationStore(self.workspace)
        self.assertEqual(reopened.path, self.store.path)

    def test_exact_binding_replacement_and_coordinate_retarget_cannot_mint_start(
        self,
    ) -> None:
        authority = self.store._authority
        original_binding = authority.workspace_binding
        original_state = (
            authority.workspace_binding,
            authority.workspace_instance_id,
            authority.workspace_binding_path,
            authority.namespace_sha256,
            authority.journal_dir,
            authority.records_dir,
            authority.namespace_marker_path,
        )
        forged_instance_id = "caller-selected-workspace-instance"
        forged_locator = original_binding.workspace_locator + "::caller-retarget"
        forged_locator_sha = hashlib.sha256(
            forged_locator.encode("utf-8")
        ).hexdigest()
        forged_path_binding = (
            authority.authority_root
            / "workspace-bindings"
            / forged_locator_sha[:2]
            / f"{forged_locator_sha}.json"
        )
        binding_type = type(original_binding)
        forged_binding = binding_type(
            workspace=original_binding.workspace,
            authority_root=original_binding.authority_root,
            workspace_instance_id=forged_instance_id,
            workspace_marker_path=original_binding.workspace_marker_path,
            path_binding_path=forged_path_binding,
            workspace_locator=forged_locator,
            workspace_locator_sha256=forged_locator_sha,
        )
        forged_namespace = hashlib.sha256(
            "\0".join(
                (
                    activation_module.MONOTONIC_AUTHORITY_ID,
                    forged_instance_id,
                    authority.domain,
                    authority.key,
                )
            ).encode("utf-8")
        ).hexdigest()
        forged_journal = (
            authority.authority_root
            / "journals"
            / forged_namespace[:2]
            / forged_namespace
        )

        authority.workspace_binding = forged_binding
        authority.workspace_instance_id = forged_instance_id
        authority.workspace_binding_path = original_binding.workspace_marker_path
        authority.namespace_sha256 = forged_namespace
        authority.journal_dir = forged_journal
        authority.records_dir = forged_journal / "records"
        authority.namespace_marker_path = (
            authority.authority_root
            / "namespace-bindings"
            / forged_namespace[:2]
            / f"{forged_namespace}.json"
        )
        try:
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "workspace identity binding construction state changed",
            ):
                self._initialize()
            self.assertFalse(self.store.path.exists())
            self.assertFalse(forged_journal.exists())
        finally:
            (
                authority.workspace_binding,
                authority.workspace_instance_id,
                authority.workspace_binding_path,
                authority.namespace_sha256,
                authority.journal_dir,
                authority.records_dir,
                authority.namespace_marker_path,
            ) = original_state

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_exact_same_root_mwa_cannot_replace_constructor_authority(
        self,
    ) -> None:
        original_authority = self.store._authority
        authority_type = type(original_authority)
        replacement_authority = authority_type(
            workspace=self.workspace,
            domain=original_authority.domain,
            key=original_authority.key,
            authority_root=original_authority.authority_root,
        )

        object.__setattr__(self.store, "_authority", replacement_authority)
        try:
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "constructor authority identity changed",
            ):
                self._initialize()
            self.assertFalse(self.store.path.exists())
        finally:
            object.__setattr__(self.store, "_authority", original_authority)

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_exact_alternate_mwa_root_cannot_replace_product_authority(
        self,
    ) -> None:
        original_authority = self.store._authority
        authority_type = type(original_authority)
        alternate_root = self.test_root / "caller-selected-authority-root"
        alternate_authority = authority_type(
            workspace=self.workspace,
            domain=original_authority.domain,
            key=original_authority.key,
            authority_root=alternate_root,
        )

        object.__setattr__(self.store, "_authority", alternate_authority)
        try:
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "constructor authority identity changed",
            ):
                self._initialize()
            self.assertFalse(self.store.path.exists())
        finally:
            object.__setattr__(self.store, "_authority", original_authority)

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_nested_authority_records_path_redirect_fails_before_start_publication(
        self,
    ) -> None:
        authority = self.store._authority
        original_records_dir = authority.records_dir
        authority.records_dir = self.test_root / "caller-selected-records"
        try:
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "nested workspace identity binding construction state changed",
            ):
                self._initialize()
            self.assertFalse(self.store.path.exists())
        finally:
            authority.records_dir = original_records_dir

        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)

    def test_activation_filename_rebind_cannot_create_second_namespace(self) -> None:
        committed = self._initialize()
        canonical_path = self.store.path
        alternate_name = "alternate_product_decision_activation.json"
        alternate_path = self.workspace / alternate_name

        with mock.patch.object(
            ProductDecisionActivationStore,
            "FILE_NAME",
            alternate_name,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "canonical product decision activation store namespace changed",
            ):
                ProductDecisionActivationStore(self.workspace)

        self.assertTrue(canonical_path.exists())
        self.assertFalse(alternate_path.exists())
        reopened = ProductDecisionActivationStore(self.workspace)
        self.assertEqual(reopened.load(), committed)

    def test_module_and_class_filename_rebind_cannot_create_second_namespace(
        self,
    ) -> None:
        committed = self._initialize()
        canonical_path = self.store.path
        alternate_name = "alternate_product_decision_activation.json"
        alternate_path = self.workspace / alternate_name

        with mock.patch.object(
            activation_module,
            "_ACTIVATION_FILE_NAME",
            alternate_name,
            create=True,
        ), mock.patch.object(
            ProductDecisionActivationStore,
            "FILE_NAME",
            alternate_name,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "canonical product decision activation store namespace changed",
            ):
                ProductDecisionActivationStore(self.workspace)

        self.assertTrue(canonical_path.exists())
        self.assertFalse(alternate_path.exists())
        reopened = ProductDecisionActivationStore(self.workspace)
        self.assertEqual(reopened.load(), committed)

    def test_mwa_defining_global_rebind_cannot_reinterpret_committed_history(
        self,
    ) -> None:
        committed = self._initialize()
        self.store.path.unlink()
        canonical_json = monotonic_authority_module.strict_json_loads
        canonical_record_hash = monotonic_authority_module._record_hash
        forged_calls: list[str] = []

        def forged_json(text: str) -> object:
            forged_calls.append("strict_json_loads")
            payload = canonical_json(text)
            if isinstance(payload, dict) and payload.get("phase") == "COMMIT":
                payload = dict(payload)
                payload["phase"] = "ABORT"
            return payload

        def forged_record_hash(payload: dict[str, object]) -> str:
            forged_calls.append("_record_hash")
            canonical_payload = payload
            if payload.get("phase") == "ABORT":
                canonical_payload = dict(payload)
                canonical_payload["phase"] = "COMMIT"
            return canonical_record_hash(canonical_payload)

        with (
            mock.patch.object(
                monotonic_authority_module,
                "strict_json_loads",
                forged_json,
            ),
            mock.patch.object(
                monotonic_authority_module,
                "_record_hash",
                forged_record_hash,
            ),
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "defining globals changed",
            ):
                self._initialize()

        self.assertEqual(forged_calls, [])
        self.assertFalse(self.store.path.exists())
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "anti-rollback authority rejected",
        ):
            self._initialize()
        self.assertFalse(self.store.path.exists())
        self.assertEqual(committed.product_source_id, "provider-a")

    def test_mwa_json_member_rebind_cannot_reinterpret_committed_history(
        self,
    ) -> None:
        committed = self._initialize()
        self.store.path.unlink()
        canonical_loads = monotonic_authority_module.json.loads
        canonical_dumps = monotonic_authority_module.json.dumps
        forged_calls: list[str] = []

        def forged_loads(*args: object, **kwargs: object) -> object:
            forged_calls.append("json.loads")
            payload = canonical_loads(*args, **kwargs)
            if isinstance(payload, dict) and payload.get("phase") == "COMMIT":
                payload = dict(payload)
                payload["phase"] = "ABORT"
            return payload

        def forged_dumps(
            payload: object,
            *args: object,
            **kwargs: object,
        ) -> str:
            forged_calls.append("json.dumps")
            canonical_payload = payload
            if isinstance(payload, dict) and payload.get("phase") == "ABORT":
                canonical_payload = dict(payload)
                canonical_payload["phase"] = "COMMIT"
            return canonical_dumps(canonical_payload, *args, **kwargs)

        with (
            mock.patch.object(
                monotonic_authority_module.json,
                "loads",
                forged_loads,
            ),
            mock.patch.object(
                monotonic_authority_module.json,
                "dumps",
                forged_dumps,
            ),
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "defining globals changed",
            ):
                self._initialize()

        self.assertEqual(forged_calls, [])
        self.assertFalse(self.store.path.exists())
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "anti-rollback authority rejected",
        ):
            self._initialize()
        self.assertFalse(self.store.path.exists())
        self.assertEqual(committed.product_source_id, "provider-a")

    def test_hashlib_sha256_member_rebind_cannot_mask_composition_drift(
        self,
    ) -> None:
        committed = self._initialize()
        canonical_sha256 = hashlib.sha256
        committed_state_sha256 = canonical_sha256(
            self.store.path.read_bytes()
        ).hexdigest()

        self._write_composition(source_id="provider-b", bankroll="1000")
        composition_bytes = (
            self.workspace / "product_composition.json"
        ).read_bytes()

        root = json.loads(self.store.path.read_text(encoding="utf-8"))
        binding = root["binding"]
        binding["product_source_id"] = "provider-b"
        binding["product_composition_sha256"] = canonical_sha256(
            composition_bytes
        ).hexdigest()
        binding_bytes = json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        root["binding_sha256"] = canonical_sha256(binding_bytes).hexdigest()
        tampered_activation_bytes = activation_module._durable_json_bytes(root)
        self.store.path.write_bytes(tampered_activation_bytes)

        forged_calls: list[str] = []

        class ForgedDigest:
            def hexdigest(self) -> str:
                return committed_state_sha256

        def forged_sha256(
            data: bytes = b"",
            *args: object,
            **kwargs: object,
        ) -> object:
            if data == tampered_activation_bytes:
                forged_calls.append("activation-state")
                return ForgedDigest()
            return canonical_sha256(data, *args, **kwargs)

        with mock.patch.object(
            monotonic_authority_module.hashlib,
            "sha256",
            forged_sha256,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "defining globals changed",
            ):
                self._verify()

        self.assertEqual(forged_calls, [])
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "anti-rollback authority rejected",
        ):
            self._verify()
        self.assertEqual(committed.product_source_id, "provider-a")

    def test_product_composition_global_rebind_cannot_hide_durable_drift(
        self,
    ) -> None:
        committed = self._initialize()
        self._write_composition(source_id="provider-b", bankroll="1000")
        forged_calls: list[str] = []

        def forged_product_composition(_workspace: Path) -> tuple[str, str, str]:
            forged_calls.append("_product_composition")
            return (
                committed.product_composition_sha256,
                committed.product_source_id,
                committed.initial_bankroll,
            )

        with mock.patch.object(
            activation_module,
            "_product_composition",
            forged_product_composition,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "defining globals changed",
            ):
                self._verify()

        self.assertEqual(forged_calls, [])
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "no longer matches exact durable authority owner bytes",
        ):
            self._verify()

    def test_path_read_bytes_rebind_cannot_cross_sign_tampered_activation(
        self,
    ) -> None:
        committed = self._initialize()
        committed_activation_bytes = self.store.path.read_bytes()
        canonical_sha256 = hashlib.sha256

        self._write_composition(source_id="provider-b", bankroll="1000")
        composition_bytes = (
            self.workspace / "product_composition.json"
        ).read_bytes()

        root = json.loads(self.store.path.read_text(encoding="utf-8"))
        binding = root["binding"]
        binding["product_source_id"] = "provider-b"
        binding["product_composition_sha256"] = canonical_sha256(
            composition_bytes
        ).hexdigest()
        binding_bytes = json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        root["binding_sha256"] = canonical_sha256(binding_bytes).hexdigest()
        tampered_activation_bytes = activation_module._durable_json_bytes(root)
        self.store.path.write_bytes(tampered_activation_bytes)

        canonical_read_bytes = Path.read_bytes
        forged_calls: list[str] = []

        def forged_read_bytes(
            path: Path,
            *args: object,
            **kwargs: object,
        ) -> bytes:
            if path == self.store.path:
                forged_calls.append("activation-read")
                if len(forged_calls) == 2:
                    return committed_activation_bytes
            return canonical_read_bytes(path, *args, **kwargs)

        with mock.patch.object(
            Path,
            "read_bytes",
            forged_read_bytes,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "path dispatch changed",
            ):
                self._verify()

        self.assertEqual(forged_calls, [])
        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "anti-rollback authority rejected",
        ):
            self._verify()
        self.assertEqual(committed.product_source_id, "provider-a")

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

    def test_preconstruction_root_rebind_cannot_select_start_authority(
        self,
    ) -> None:
        forged_root = self.test_root / "forged-preconstruction-root"
        resolver_calls: list[str] = []

        def forged_machine_state_base() -> Path:
            resolver_calls.append("machine-state-base")
            return forged_root

        with mock.patch.object(
            activation_module,
            "_product_machine_state_base",
            forged_machine_state_base,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "authority root trust changed",
            ):
                ProductDecisionActivationStore(self.workspace)

        self.assertEqual(resolver_calls, [])
        self.assertFalse(forged_root.exists())

        with mock.patch.object(
            activation_module,
            "_PRODUCT_AUTHORITY_ROOT_NAME",
            "caller-selected-activation-root",
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "authority root trust changed",
            ):
                ProductDecisionActivationStore(self.workspace)

        self.assertFalse(
            (
                self.authority_root.parent
                / "caller-selected-activation-root"
            ).exists()
        )
        canonical = self._initialize()
        self.assertEqual(canonical.product_source_id, "provider-a")
        self.assertEqual(self.store.load(), canonical)


    def test_process_environment_cannot_repoint_activation_authority_root(self) -> None:
        env_root_a = self.test_root / "operator-root-a"
        env_root_b = self.test_root / "operator-root-b"

        with mock.patch.dict(
            os.environ,
            {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(env_root_a)},
        ):
            first_store = ProductDecisionActivationStore(self.workspace)
            self.store = first_store
            committed = self._initialize()

        self.assertEqual(first_store._authority.authority_root, self.authority_root)
        self.assertFalse(env_root_a.exists())
        first_store.path.unlink()

        with mock.patch.dict(
            os.environ,
            {"AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(env_root_b)},
        ):
            reopened = ProductDecisionActivationStore(self.workspace)
            self.assertEqual(reopened._authority.authority_root, self.authority_root)
            self.assertNotEqual(reopened._authority.authority_root, env_root_b)
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "anti-rollback authority rejected",
            ):
                reopened.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=self.goal,
                    risk_policy=self.risk,
                    execution_config=self.execution,
                )

        self.assertFalse(reopened.path.exists())
        self.assertFalse(env_root_b.exists())
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
        expected = self._stage_activation_prepare(publish=False)

        self.assertFalse(self.store.path.exists())
        self.assertEqual(
            self.store._authority.read_history()[-1].phase.value,
            "PREPARE",
        )
        recovered = self._initialize()
        self.assertEqual(recovered, expected)
        self.assertEqual(recovered.strategy_version_id, self.STRATEGY_ID)
        self.assertEqual(self.store.load(), recovered)
        self.assertEqual(
            self.store._authority.read_history()[-1].phase.value,
            "COMMIT",
        )

    def test_publish_without_commit_recovers_exact_prepared_activation(self) -> None:
        expected = self._stage_activation_prepare(publish=True)

        self.assertTrue(self.store.path.exists())
        self.assertEqual(
            self.store._authority.read_history()[-1].phase.value,
            "PREPARE",
        )
        reopened = ProductDecisionActivationStore(self.workspace)
        recovered = reopened.initialize_owner(
            scientific_registry=self.registry,
            strategy_version_id=self.STRATEGY_ID,
            economic_goal=self.goal,
            risk_policy=self.risk,
            execution_config=self.execution,
        )
        self.assertEqual(recovered, expected)
        self.assertEqual(reopened.load(), recovered)
        self.assertEqual(
            reopened._authority.read_history()[-1].phase.value,
            "COMMIT",
        )

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

    def test_resealed_strategy_payload_identity_must_match_envelope(self) -> None:
        path = self.workspace / "scientific_registry.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        strategy = state["records"][0]
        strategy["payload"]["strategy_version_id"] = "strategy-other"
        self._reseal_registry_record(strategy)
        self._write_json(path, state)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "StrategyVersion payload identity",
        ):
            self._initialize()
        self.assertFalse(self.store.path.exists())

    def test_resealed_strategy_payload_timestamp_must_match_envelope(self) -> None:
        path = self.workspace / "scientific_registry.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        strategy = state["records"][0]
        strategy["payload"]["created_at"] = "2026-09-23T11:59:59+00:00"
        self._reseal_registry_record(strategy)
        self._write_json(path, state)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "StrategyVersion payload timestamp",
        ):
            self._initialize()
        self.assertFalse(self.store.path.exists())

    def test_resealed_model_payload_identity_must_match_envelope(self) -> None:
        path, model_entry, _strategy = self._install_model_reference()
        state = json.loads(path.read_text(encoding="utf-8"))
        model = state["records"][0]
        self.assertEqual(model["record_id"], model_entry["record_id"])
        model["payload"]["model_version_id"] = "model-other"
        self._reseal_registry_record(model)
        self._write_json(path, state)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "ModelVersion payload identity",
        ):
            self._initialize()
        self.assertFalse(self.store.path.exists())

    def test_resealed_model_payload_timestamp_must_match_envelope(self) -> None:
        path, model_entry, _strategy = self._install_model_reference()
        state = json.loads(path.read_text(encoding="utf-8"))
        model = state["records"][0]
        self.assertEqual(model["record_id"], model_entry["record_id"])
        model["payload"]["created_at"] = "2026-09-23T10:59:59+00:00"
        self._reseal_registry_record(model)
        self._write_json(path, state)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "ModelVersion payload timestamp",
        ):
            self._initialize()
        self.assertFalse(self.store.path.exists())

    def test_registry_get_rebind_cannot_forge_durable_strategy_authority(
        self,
    ) -> None:
        durable = ScientificRegistry.get(
            self.registry,
            "StrategyVersion",
            self.STRATEGY_ID,
        )
        self.assertIsNotNone(durable)
        assert durable is not None
        forged_payload = dict(durable.payload)
        forged_payload["source_sha256"] = "9" * 64

        class ForgedEntry:
            pass

        forged = ForgedEntry()
        forged.payload = forged_payload
        forged.record_sha256 = "a" * 64
        original_get = ScientificRegistry.get

        def forged_get(instance, record_type, record_id):
            if (
                record_type == "StrategyVersion"
                and record_id == self.STRATEGY_ID
            ):
                return forged
            return original_get(instance, record_type, record_id)

        with mock.patch.object(ScientificRegistry, "get", forged_get):
            binding = self._initialize()

        durable_state = json.loads(
            (self.workspace / "scientific_registry.json").read_text(
                encoding="utf-8"
            )
        )
        durable_strategy = durable_state["records"][0]
        self.assertEqual(binding.strategy_source_sha256, "1" * 64)
        self.assertEqual(
            binding.strategy_record_sha256,
            durable_strategy["record_sha256"],
        )
        self.assertNotEqual(binding.strategy_source_sha256, "9" * 64)
        self.assertNotEqual(binding.strategy_record_sha256, "a" * 64)

    def test_matching_risk_file_cannot_promote_non_durable_owner_goal(self) -> None:
        forged_goal = replace(
            self.goal,
            revision=self.goal.revision + 1,
            max_stake_fraction=Decimal("0.01"),
        )
        forged_risk = PaperRiskPolicy(economic_goal=forged_goal)
        self._write_risk(forged_risk, forged_goal)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "exact durable authority owner bytes",
        ):
            self.store.initialize_owner(
                scientific_registry=self.registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=forged_goal,
                risk_policy=forged_risk,
                execution_config=self.execution,
            )

        self.assertFalse(self.store.path.exists())
        self.assertEqual(
            EconomicGoalStore(self.workspace).load(),
            self.goal,
        )

    def test_economic_goal_store_module_rebind_cannot_forge_durable_owner(self) -> None:
        forged_goal = replace(
            self.goal,
            revision=self.goal.revision + 1,
            max_stake_fraction=Decimal("0.01"),
        )
        forged_risk = PaperRiskPolicy(economic_goal=forged_goal)
        self._write_risk(forged_risk, forged_goal)
        forged_dispatch_calls: list[str] = []

        class ForgedEconomicGoalStore:
            def __init__(self, workspace):
                forged_dispatch_calls.append("init")
                self.workspace = workspace

            def load(self):
                forged_dispatch_calls.append("load")
                return forged_goal

        with mock.patch.object(
            activation_module,
            "EconomicGoalStore",
            ForgedEconomicGoalStore,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "canonical EconomicGoalStore authority changed",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=forged_goal,
                    risk_policy=forged_risk,
                    execution_config=self.execution,
                )

        self.assertEqual(forged_dispatch_calls, [])
        self.assertFalse(self.store.path.exists())
        self.assertEqual(
            EconomicGoalStore(self.workspace).load(),
            self.goal,
        )

    def test_economic_goal_store_load_rebind_cannot_forge_durable_owner(self) -> None:
        forged_goal = replace(
            self.goal,
            revision=self.goal.revision + 1,
            max_stake_fraction=Decimal("0.01"),
        )
        forged_risk = PaperRiskPolicy(economic_goal=forged_goal)
        self._write_risk(forged_risk, forged_goal)

        with mock.patch.object(
            EconomicGoalStore,
            "load",
            return_value=forged_goal,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "canonical EconomicGoalStore dispatch changed",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=forged_goal,
                    risk_policy=forged_risk,
                    execution_config=self.execution,
                )

        self.assertFalse(self.store.path.exists())
        self.assertEqual(
            EconomicGoalStore(self.workspace).load(),
            self.goal,
        )

    def test_economic_goal_store_filename_rebind_cannot_redirect_durable_owner(self) -> None:
        forged_goal = replace(
            self.goal,
            revision=self.goal.revision + 1,
            max_stake_fraction=Decimal("0.01"),
        )
        forged_risk = PaperRiskPolicy(economic_goal=forged_goal)
        forged_name = "caller_selected_economic_goal.json"

        # Prove the alternate bytes are a structurally valid EconomicGoalStore
        # document. The supported START path must nevertheless remain pinned to
        # the canonical owner filename captured at product composition.
        with mock.patch.object(EconomicGoalStore, "FILE_NAME", forged_name):
            EconomicGoalStore(self.workspace).initialize_owner(forged_goal)
        self.assertTrue((self.workspace / forged_name).is_file())
        self._write_risk(forged_risk, forged_goal)

        with mock.patch.object(EconomicGoalStore, "FILE_NAME", forged_name):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "canonical EconomicGoalStore dispatch changed",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=forged_goal,
                    risk_policy=forged_risk,
                    execution_config=self.execution,
                )

        self.assertFalse(self.store.path.exists())
        self.assertEqual(
            EconomicGoalStore(self.workspace).load(),
            self.goal,
        )

    def test_economic_goal_decoder_rebind_cannot_launder_durable_owner_bytes(self) -> None:
        forged_goal = replace(
            self.goal,
            revision=self.goal.revision + 1,
            max_stake_fraction=Decimal("0.01"),
        )
        forged_risk = PaperRiskPolicy(economic_goal=forged_goal)
        self._write_risk(forged_risk, forged_goal)
        durable_path = self.workspace / "economic_goal_contract.json"
        durable_before = durable_path.read_bytes()

        # The exact store/load callable may remain unchanged while its application
        # decoder global is transiently replaced. START authority must come from
        # exact durable bytes, not the object returned by that mutable decoder.
        with mock.patch.object(
            economic_goal_store_module,
            "economic_goal_from_json",
            return_value=forged_goal,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "exact durable authority owner bytes",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=forged_goal,
                    risk_policy=forged_risk,
                    execution_config=self.execution,
                )

        self.assertFalse(self.store.path.exists())
        self.assertEqual(durable_path.read_bytes(), durable_before)
        self.assertEqual(
            EconomicGoalStore(self.workspace).load(),
            self.goal,
        )

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

    def test_economic_goal_subclass_cannot_forge_start_authority(self) -> None:
        canonical = self.goal

        class MutableEconomicGoal(EconomicGoalContract):
            def __getattribute__(self, name: str):
                if name == "max_stake_fraction":
                    try:
                        expose_canonical = object.__getattribute__(
                            self, "_expose_canonical"
                        )
                    except AttributeError:
                        expose_canonical = False
                    if expose_canonical:
                        return canonical.max_stake_fraction
                return super().__getattribute__(name)

        forged = MutableEconomicGoal(
            goal_id=canonical.goal_id,
            revision=canonical.revision,
            bankroll_id=canonical.bankroll_id,
            currency=canonical.currency,
            max_stake_fraction=Decimal("0.90"),
        )
        object.__setattr__(forged, "_expose_canonical", True)
        forged_risk = PaperRiskPolicy(economic_goal=forged)
        self._write_risk(forged_risk, forged)

        self.assertIsInstance(forged, EconomicGoalContract)
        self.assertIsNot(type(forged), EconomicGoalContract)
        self.assertEqual(
            EconomicGoalContract.max_stake_fraction.__get__(
                forged, EconomicGoalContract
            ),
            Decimal("0.90"),
        )
        self.assertEqual(
            forged.max_stake_fraction,
            canonical.max_stake_fraction,
        )

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "exact canonical EconomicGoalContract",
        ):
            self.store.initialize_owner(
                scientific_registry=self.registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=forged,
                risk_policy=forged_risk,
                execution_config=self.execution,
            )

        object.__setattr__(forged, "_expose_canonical", False)
        self.assertEqual(forged.max_stake_fraction, Decimal("0.90"))
        self.assertFalse(self.store.path.exists())

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

    def test_risk_policy_provenance_descriptor_rebind_cannot_relabel_executable_policy(self) -> None:
        canonical_provenance = self.risk.provenance_sha256
        forged = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.90"),
            economic_goal=self.goal,
        )
        original_descriptor = PaperRiskPolicy.provenance_sha256

        try:
            PaperRiskPolicy.provenance_sha256 = property(  # type: ignore[assignment]
                lambda _self: canonical_provenance
            )
            self.assertEqual(forged.provenance_sha256, canonical_provenance)
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "PaperRiskPolicy provenance authority changed",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=self.goal,
                    risk_policy=forged,
                    execution_config=self.execution,
                )
        finally:
            PaperRiskPolicy.provenance_sha256 = original_descriptor  # type: ignore[assignment]

        self.assertFalse(self.store.path.exists())
        binding = self._initialize()
        self.assertEqual(
            binding.risk_policy_provenance_sha256,
            canonical_provenance,
        )

    def test_risk_policy_module_class_rebind_cannot_replace_authority(self) -> None:
        class ReboundRiskPolicy:
            pass

        forged = ReboundRiskPolicy()
        forged.economic_goal = self.goal
        forged.provenance_sha256 = self.risk.provenance_sha256

        with mock.patch.object(
            activation_module,
            "PaperRiskPolicy",
            ReboundRiskPolicy,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "exact canonical PaperRiskPolicy",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=self.goal,
                    risk_policy=forged,  # type: ignore[arg-type]
                    execution_config=self.execution,
                )

        self.assertFalse(self.store.path.exists())
        binding = self._initialize()
        self.assertEqual(
            binding.risk_policy_provenance_sha256,
            self.risk.provenance_sha256,
        )

    def test_risk_file_policy_body_must_match_exact_executable_policy(self) -> None:
        path = self.workspace / "paper_risk_policy.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["policy"]["max_ticket_fraction"] = "0.90"
        self._write_json(path, payload)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "does not match exact executable risk policy",
        ):
            self._initialize()

        self.assertFalse(self.store.path.exists())

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

    def test_execution_config_subclass_cannot_forge_canonical_fingerprint(self) -> None:
        canonical = self.execution

        class ForgedExecutionConfig(PaperExecutionModelConfig):
            @property
            def fingerprint(self) -> str:
                return canonical.fingerprint

        forged = ForgedExecutionConfig(
            model_id=canonical.model_id,
            model_version=canonical.model_version,
            evidence_grade=canonical.evidence_grade,
            evidence_source=canonical.evidence_source,
            seed="different-execution-seed",
            max_quote_age_ms=canonical.max_quote_age_ms,
            min_delay_ms=canonical.min_delay_ms,
            max_delay_ms=canonical.max_delay_ms,
            rejected_bps=canonical.rejected_bps,
            partial_bps=canonical.partial_bps,
            unknown_bps=canonical.unknown_bps,
            partial_fill_bps=canonical.partial_fill_bps,
            max_slippage_bps=1,
        )

        self.assertNotEqual(forged.seed, canonical.seed)
        self.assertNotEqual(forged.max_slippage_bps, canonical.max_slippage_bps)
        self.assertEqual(forged.fingerprint, canonical.fingerprint)

        with self.assertRaisesRegex(
            ProductDecisionActivationError,
            "exact canonical PaperExecutionModelConfig",
        ):
            self.store.initialize_owner(
                scientific_registry=self.registry,
                strategy_version_id=self.STRATEGY_ID,
                economic_goal=self.goal,
                risk_policy=self.risk,
                execution_config=forged,
            )

        # The exact canonical base-class config remains accepted.
        binding = self._initialize()
        self.assertEqual(binding.execution_model_fingerprint, canonical.fingerprint)

    def test_execution_config_module_class_rebind_cannot_replace_authority(self) -> None:
        canonical_class = PaperExecutionModelConfig
        canonical = self.execution

        class ReboundExecutionConfig:
            fingerprint = canonical_class.fingerprint

            def __init__(self) -> None:
                self.model_id = canonical.model_id
                self.model_version = canonical.model_version
                self.evidence_grade = canonical.evidence_grade
                self.evidence_source = canonical.evidence_source
                self.seed = canonical.seed
                self.max_quote_age_ms = canonical.max_quote_age_ms
                self.min_delay_ms = canonical.min_delay_ms
                self.max_delay_ms = canonical.max_delay_ms
                self.rejected_bps = canonical.rejected_bps
                self.partial_bps = canonical.partial_bps
                self.unknown_bps = canonical.unknown_bps
                self.partial_fill_bps = canonical.partial_fill_bps
                self.max_slippage_bps = canonical.max_slippage_bps

        forged = ReboundExecutionConfig()
        self.assertEqual(forged.fingerprint, canonical.fingerprint)

        with mock.patch.object(
            activation_module,
            "PaperExecutionModelConfig",
            ReboundExecutionConfig,
        ):
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "exact canonical PaperExecutionModelConfig",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=self.goal,
                    risk_policy=self.risk,
                    execution_config=forged,  # type: ignore[arg-type]
                )

        self.assertFalse(self.store.path.exists())
        binding = self._initialize()
        self.assertEqual(
            binding.execution_model_fingerprint,
            canonical.fingerprint,
        )

    def test_execution_config_base_fingerprint_descriptor_rebind_fails_closed(self) -> None:
        canonical = self.execution
        canonical_fingerprint = canonical.fingerprint
        forged = replace(
            canonical,
            seed="descriptor-forged-execution-seed",
            max_slippage_bps=1,
        )
        original_descriptor = PaperExecutionModelConfig.fingerprint

        try:
            PaperExecutionModelConfig.fingerprint = property(  # type: ignore[assignment]
                lambda _self: canonical_fingerprint
            )
            self.assertEqual(forged.fingerprint, canonical_fingerprint)
            with self.assertRaisesRegex(
                ProductDecisionActivationError,
                "fingerprint authority changed",
            ):
                self.store.initialize_owner(
                    scientific_registry=self.registry,
                    strategy_version_id=self.STRATEGY_ID,
                    economic_goal=self.goal,
                    risk_policy=self.risk,
                    execution_config=forged,
                )
        finally:
            PaperExecutionModelConfig.fingerprint = original_descriptor  # type: ignore[assignment]

        self.assertFalse(self.store.path.exists())
        binding = self._initialize()
        self.assertEqual(
            binding.execution_model_fingerprint,
            canonical_fingerprint,
        )

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

    def test_intent_producer_module_class_rebind_cannot_widen_registry(self) -> None:
        class ReboundIntentProducer(activation_module.StrEnum):
            CALLER_DEFINED = "caller-defined-producer"

        forged = ReboundIntentProducer.CALLER_DEFINED
        with mock.patch.object(
            activation_module,
            "BuiltInIntentProducer",
            ReboundIntentProducer,
        ):
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
                    intent_producer=forged,  # type: ignore[arg-type]
                )

        self.assertFalse(self.store.path.exists())
        binding = self._initialize()
        self.assertEqual(
            binding.intent_producer_id,
            BuiltInIntentProducer.REGISTERED_STRATEGY.value,
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
