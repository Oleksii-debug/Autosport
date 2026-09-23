from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final

from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .paper_execution_reality import PaperExecutionModelConfig
from .risk import PaperRiskPolicy
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


ACTIVATION_SCHEMA: Final = "autosport.product_decision_activation"
ACTIVATION_SCHEMA_VERSION: Final = 1
DECISION_CYCLE_CONTRACT: Final = "autosport.product-paper-decision-cycle.v1"
PAPER_EXECUTION_MODE: Final = "PAPER"


class ProductDecisionActivationError(ValueError):
    """Durable supported-START activation evidence is invalid or has drifted."""


class BuiltInIntentProducer(StrEnum):
    """Closed product-owned intent producer registry; never an import path."""

    REGISTERED_STRATEGY = "canonical-registered-strategy-v1"


_BINDING_FIELDS: Final = frozenset(
    {
        "strategy_version_id",
        "strategy_record_sha256",
        "strategy_source_sha256",
        "strategy_environment_sha256",
        "strategy_config_sha256",
        "strategy_model_version_id",
        "strategy_model_record_sha256",
        "registry_prefix_record_count",
        "registry_prefix_sha256",
        "economic_goal_contract_sha256",
        "goal_id",
        "goal_revision",
        "bankroll_id",
        "currency",
        "risk_policy_provenance_sha256",
        "risk_policy_file_sha256",
        "intent_producer_id",
        "execution_mode",
        "execution_model_fingerprint",
        "product_composition_sha256",
        "product_source_id",
        "initial_bankroll",
        "decision_cycle_contract",
    }
)
_ROOT_FIELDS: Final = frozenset(
    {"schema", "schema_version", "binding", "binding_sha256"}
)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductDecisionActivationError(
            "activation evidence is not canonically serializable"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        raise ProductDecisionActivationError(
            f"{name} must be non-empty canonical text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductDecisionActivationError(f"{name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 64
        or text != text.lower()
        or any(ch not in "0123456789abcdef" for ch in text)
    ):
        raise ProductDecisionActivationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProductDecisionActivationError(f"{name} must be a positive integer")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _read_regular_file(path: Path, label: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise ProductDecisionActivationError(f"{label} must be a regular file")
        return path.read_bytes()
    except OSError as exc:
        raise ProductDecisionActivationError(f"cannot read {label}") from exc


def _require_workspace_path(
    workspace: Path,
    path: Path,
    filename: str,
    label: str,
) -> Path:
    expected = workspace / filename
    try:
        if path.resolve(strict=False) != expected.resolve(strict=False):
            raise ProductDecisionActivationError(
                f"{label} must use the canonical workspace path"
            )
    except OSError as exc:
        raise ProductDecisionActivationError(f"cannot resolve {label} path") from exc
    return expected


def _strict_json_file(path: Path, label: str) -> tuple[bytes, object]:
    raw = _read_regular_file(path, label)
    try:
        return raw, strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ProductDecisionActivationError(
            f"{label} must be strict UTF-8 JSON"
        ) from exc


def _product_composition(workspace: Path) -> tuple[str, str, str]:
    path = _require_workspace_path(
        workspace,
        workspace / "product_composition.json",
        "product_composition.json",
        "product composition",
    )
    raw, payload = _strict_json_file(path, "product composition")
    if type(payload) is not dict:
        raise ProductDecisionActivationError("product composition must be an object")
    if payload.get("schema") != "autosport.autonomous_product_composition":
        raise ProductDecisionActivationError("product composition schema mismatch")
    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise ProductDecisionActivationError(
            "product composition schema version is invalid"
        )
    source_id = _text(payload.get("source_id"), "product source_id")
    initial_bankroll = _text(payload.get("initial_bankroll"), "initial_bankroll")
    try:
        amount = Decimal(initial_bankroll)
    except InvalidOperation as exc:
        raise ProductDecisionActivationError(
            "initial_bankroll must be an exact Decimal"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise ProductDecisionActivationError(
            "initial_bankroll must be positive and finite"
        )
    return hashlib.sha256(raw).hexdigest(), source_id, initial_bankroll


def _risk_policy_file_digest(
    workspace: Path,
    *,
    risk_policy: PaperRiskPolicy,
    goal_sha256: str,
) -> str:
    path = _require_workspace_path(
        workspace,
        workspace / "paper_risk_policy.json",
        "paper_risk_policy.json",
        "paper risk policy",
    )
    raw, payload = _strict_json_file(path, "paper risk policy")
    if type(payload) is not dict:
        raise ProductDecisionActivationError("paper risk policy must be an object")
    if (
        payload.get("schema") != "autosport.paper_risk_policy"
        or payload.get("schema_version") != 1
    ):
        raise ProductDecisionActivationError("paper risk policy schema mismatch")
    if payload.get("policy_provenance_sha256") != risk_policy.provenance_sha256:
        raise ProductDecisionActivationError(
            "paper risk policy file does not match executable risk provenance"
        )
    policy = payload.get("policy")
    if type(policy) is not dict:
        raise ProductDecisionActivationError("paper risk policy body is invalid")
    if policy.get("economic_goal_contract_sha256") != goal_sha256:
        raise ProductDecisionActivationError(
            "paper risk policy file is not bound to the exact EconomicGoalContract"
        )
    return hashlib.sha256(raw).hexdigest()


def _registry_strategy_prefix(
    workspace: Path,
    registry: ScientificRegistry,
    strategy_version_id: str,
) -> tuple[dict[str, object], int, str, str, str | None]:
    if type(registry) is not ScientificRegistry:
        raise ProductDecisionActivationError(
            "scientific_registry must be the canonical ScientificRegistry"
        )
    path = _require_workspace_path(
        workspace,
        Path(registry.path),
        "scientific_registry.json",
        "scientific registry",
    )
    canonical = ScientificRegistry(path)
    wanted = _text(strategy_version_id, "strategy_version_id")
    entry = canonical.get("StrategyVersion", wanted)
    if entry is None:
        raise ProductDecisionActivationError(
            "activation StrategyVersion is missing from ScientificRegistry"
        )
    _, state = _strict_json_file(path, "scientific registry")
    if (
        type(state) is not dict
        or state.get("schema_version") != ScientificRegistry.SCHEMA_VERSION
        or type(state.get("records")) is not list
    ):
        raise ProductDecisionActivationError("scientific registry schema mismatch")
    records = state["records"]
    index = next(
        (
            i
            for i, raw in enumerate(records)
            if type(raw) is dict
            and raw.get("record_type") == "StrategyVersion"
            and raw.get("record_id") == wanted
        ),
        None,
    )
    if index is None:
        raise ProductDecisionActivationError(
            "activation StrategyVersion is absent from registry sequence"
        )
    strategy_payload = dict(entry.payload)
    model_record_sha256: str | None = None
    model_version_id = strategy_payload.get("model_version_id")
    if model_version_id is not None:
        model_version_id = _text(
            model_version_id,
            "strategy model_version_id",
        )
        model_entry = canonical.get("ModelVersion", model_version_id)
        if model_entry is None:
            raise ProductDecisionActivationError(
                "activation StrategyVersion references a missing ModelVersion"
            )
        model_index = next(
            (
                i
                for i, raw in enumerate(records)
                if type(raw) is dict
                and raw.get("record_type") == "ModelVersion"
                and raw.get("record_id") == model_version_id
            ),
            None,
        )
        if model_index is None or model_index >= index:
            raise ProductDecisionActivationError(
                "activation ModelVersion was not durable before StrategyVersion"
            )
        model_record_sha256 = _sha256(
            model_entry.record_sha256,
            "strategy model record_sha256",
        )
    prefix = {
        "schema_version": ScientificRegistry.SCHEMA_VERSION,
        "records": records[: index + 1],
    }
    return (
        strategy_payload,
        index + 1,
        _digest(prefix),
        entry.record_sha256,
        model_record_sha256,
    )


@dataclass(frozen=True, slots=True)
class ProductDecisionActivationBinding:
    strategy_version_id: str
    strategy_record_sha256: str
    strategy_source_sha256: str
    strategy_environment_sha256: str
    strategy_config_sha256: str
    strategy_model_version_id: str | None
    strategy_model_record_sha256: str | None
    registry_prefix_record_count: int
    registry_prefix_sha256: str
    economic_goal_contract_sha256: str
    goal_id: str
    goal_revision: int
    bankroll_id: str
    currency: str
    risk_policy_provenance_sha256: str
    risk_policy_file_sha256: str
    intent_producer_id: str
    execution_mode: str
    execution_model_fingerprint: str
    product_composition_sha256: str
    product_source_id: str
    initial_bankroll: str
    decision_cycle_contract: str

    def __post_init__(self) -> None:
        for name in (
            "strategy_version_id",
            "goal_id",
            "bankroll_id",
            "currency",
            "intent_producer_id",
            "execution_mode",
            "product_source_id",
            "initial_bankroll",
            "decision_cycle_contract",
        ):
            _text(getattr(self, name), name)
        for name in (
            "strategy_record_sha256",
            "strategy_source_sha256",
            "strategy_environment_sha256",
            "strategy_config_sha256",
            "registry_prefix_sha256",
            "economic_goal_contract_sha256",
            "risk_policy_provenance_sha256",
            "risk_policy_file_sha256",
            "execution_model_fingerprint",
            "product_composition_sha256",
        ):
            _sha256(getattr(self, name), name)
        _optional_text(self.strategy_model_version_id, "strategy_model_version_id")
        if self.strategy_model_record_sha256 is not None:
            _sha256(
                self.strategy_model_record_sha256,
                "strategy_model_record_sha256",
            )
        if (self.strategy_model_version_id is None) != (
            self.strategy_model_record_sha256 is None
        ):
            raise ProductDecisionActivationError(
                "model version and model record provenance must be bound together"
            )
        _positive_int(self.registry_prefix_record_count, "registry_prefix_record_count")
        _positive_int(self.goal_revision, "goal_revision")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ProductDecisionActivationError(
                "currency must be uppercase three-letter ASCII"
            )
        if self.intent_producer_id not in {
            member.value for member in BuiltInIntentProducer
        }:
            raise ProductDecisionActivationError(
                "intent producer is not product-owned"
            )
        if self.execution_mode != PAPER_EXECUTION_MODE:
            raise ProductDecisionActivationError(
                "supported activation is PAPER-only"
            )
        if self.decision_cycle_contract != DECISION_CYCLE_CONTRACT:
            raise ProductDecisionActivationError(
                "decision-cycle contract is unsupported"
            )

    @property
    def binding_sha256(self) -> str:
        return _digest(asdict(self))

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(
        cls, payload: object
    ) -> "ProductDecisionActivationBinding":
        if type(payload) is not dict or frozenset(payload) != _BINDING_FIELDS:
            raise ProductDecisionActivationError(
                "activation binding fields mismatch"
            )
        try:
            return cls(**payload)
        except TypeError as exc:
            raise ProductDecisionActivationError(
                "activation binding schema is invalid"
            ) from exc


class ProductDecisionActivationStore:
    """Creation-only supported-START binding; it grants no execution authority."""

    FILE_NAME: Final = "product_decision_activation.json"

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.path = self.workspace / self.FILE_NAME

    def _derive(
        self,
        *,
        scientific_registry: ScientificRegistry,
        strategy_version_id: str,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        execution_config: PaperExecutionModelConfig,
        intent_producer: BuiltInIntentProducer,
    ) -> ProductDecisionActivationBinding:
        if not isinstance(economic_goal, EconomicGoalContract):
            raise ProductDecisionActivationError(
                "economic_goal must be the canonical EconomicGoalContract"
            )
        if type(risk_policy) is not PaperRiskPolicy:
            raise ProductDecisionActivationError(
                "risk_policy must be the canonical PaperRiskPolicy"
            )
        if risk_policy.economic_goal != economic_goal:
            raise ProductDecisionActivationError(
                "risk policy must be bound to the exact EconomicGoalContract"
            )
        if not isinstance(execution_config, PaperExecutionModelConfig):
            raise ProductDecisionActivationError(
                "execution_config must be PaperExecutionModelConfig"
            )
        if not isinstance(intent_producer, BuiltInIntentProducer):
            raise ProductDecisionActivationError(
                "intent producer must come from the closed product registry"
            )

        (
            strategy,
            prefix_count,
            prefix_sha,
            record_sha,
            model_record_sha,
        ) = _registry_strategy_prefix(
            self.workspace, scientific_registry, strategy_version_id
        )
        source_sha = _sha256(
            strategy.get("source_sha256"), "strategy source_sha256"
        )
        environment_sha = _sha256(
            strategy.get("environment_sha256"),
            "strategy environment_sha256",
        )
        config_sha = _sha256(
            strategy.get("config_sha256"), "strategy config_sha256"
        )
        _text(strategy.get("canonical_strategy_id"), "canonical_strategy_id")
        model_version_id = _optional_text(
            strategy.get("model_version_id"), "strategy model_version_id"
        )

        goal = provenance_for(economic_goal)
        composition_sha, source_id, initial_bankroll = _product_composition(
            self.workspace
        )
        risk_file_sha = _risk_policy_file_digest(
            self.workspace,
            risk_policy=risk_policy,
            goal_sha256=goal.contract_sha256,
        )

        return ProductDecisionActivationBinding(
            strategy_version_id=_text(
                strategy_version_id, "strategy_version_id"
            ),
            strategy_record_sha256=_sha256(
                record_sha, "strategy_record_sha256"
            ),
            strategy_source_sha256=source_sha,
            strategy_environment_sha256=environment_sha,
            strategy_config_sha256=config_sha,
            strategy_model_version_id=model_version_id,
            strategy_model_record_sha256=model_record_sha,
            registry_prefix_record_count=prefix_count,
            registry_prefix_sha256=prefix_sha,
            economic_goal_contract_sha256=goal.contract_sha256,
            goal_id=economic_goal.goal_id,
            goal_revision=economic_goal.revision,
            bankroll_id=economic_goal.bankroll_id,
            currency=economic_goal.currency,
            risk_policy_provenance_sha256=_sha256(
                risk_policy.provenance_sha256,
                "risk_policy_provenance_sha256",
            ),
            risk_policy_file_sha256=risk_file_sha,
            intent_producer_id=intent_producer.value,
            execution_mode=PAPER_EXECUTION_MODE,
            execution_model_fingerprint=_sha256(
                execution_config.fingerprint,
                "execution_model_fingerprint",
            ),
            product_composition_sha256=composition_sha,
            product_source_id=source_id,
            initial_bankroll=initial_bankroll,
            decision_cycle_contract=DECISION_CYCLE_CONTRACT,
        )

    @staticmethod
    def _root(
        binding: ProductDecisionActivationBinding,
    ) -> dict[str, object]:
        return {
            "schema": ACTIVATION_SCHEMA,
            "schema_version": ACTIVATION_SCHEMA_VERSION,
            "binding": binding.to_payload(),
            "binding_sha256": binding.binding_sha256,
        }

    def initialize_owner(
        self,
        *,
        scientific_registry: ScientificRegistry,
        strategy_version_id: str,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        execution_config: PaperExecutionModelConfig,
        intent_producer: BuiltInIntentProducer = BuiltInIntentProducer.REGISTERED_STRATEGY,
    ) -> ProductDecisionActivationBinding:
        """Create once; exact retry is idempotent and semantic drift fails closed."""

        with WorkspaceEconomicLock(self.workspace):
            expected = self._derive(
                scientific_registry=scientific_registry,
                strategy_version_id=strategy_version_id,
                economic_goal=economic_goal,
                risk_policy=risk_policy,
                execution_config=execution_config,
                intent_producer=intent_producer,
            )
            if self.path.exists():
                existing = self.load()
                if existing != expected:
                    raise ProductDecisionActivationError(
                        "durable product decision activation conflicts with requested START"
                    )
                return existing
            atomic_write_json(self.path, self._root(expected))
            persisted = self.load()
            if persisted != expected:
                raise ProductDecisionActivationError(
                    "persisted product decision activation does not match requested START"
                )
            return persisted

    def load(self) -> ProductDecisionActivationBinding:
        _, root = _strict_json_file(
            self.path, "product decision activation"
        )
        if type(root) is not dict or frozenset(root) != _ROOT_FIELDS:
            raise ProductDecisionActivationError(
                "activation root fields mismatch"
            )
        if root.get("schema") != ACTIVATION_SCHEMA:
            raise ProductDecisionActivationError("activation schema mismatch")
        if root.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
            raise ProductDecisionActivationError(
                "activation schema version mismatch"
            )
        binding = ProductDecisionActivationBinding.from_payload(
            root.get("binding")
        )
        declared = _sha256(root.get("binding_sha256"), "binding_sha256")
        if declared != binding.binding_sha256:
            raise ProductDecisionActivationError(
                "activation binding digest mismatch"
            )
        return binding

    def verify(
        self,
        *,
        scientific_registry: ScientificRegistry,
        strategy_version_id: str,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        execution_config: PaperExecutionModelConfig,
        intent_producer: BuiltInIntentProducer = BuiltInIntentProducer.REGISTERED_STRATEGY,
    ) -> ProductDecisionActivationBinding:
        """Re-resolve every child authority and reject START-time drift."""

        persisted = self.load()
        expected = self._derive(
            scientific_registry=scientific_registry,
            strategy_version_id=strategy_version_id,
            economic_goal=economic_goal,
            risk_policy=risk_policy,
            execution_config=execution_config,
            intent_producer=intent_producer,
        )
        if persisted != expected:
            raise ProductDecisionActivationError(
                "supported START activation evidence no longer matches durable authority"
            )
        return persisted
