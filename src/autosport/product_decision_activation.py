from __future__ import annotations

import hashlib
import json
import os
import uuid
import weakref
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Final

from .economic_goal import EconomicGoalContract, EconomicGoalContractError
from .economic_goal_provenance import provenance_for
from .economic_goal_store import EconomicGoalStore, economic_goal_to_payload
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .monotonic_workspace_authority import (
    AUTHORITY_ID as MONOTONIC_AUTHORITY_ID,
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .monotonic_workspace_binding import WorkspaceIdentityBinding
from .paper_execution_reality import PaperExecutionModelConfig
from .risk import PaperRiskPolicy
from .scientific_registry import ScientificRegistry
from .workspace_lock import WorkspaceEconomicLock


ACTIVATION_SCHEMA: Final = "autosport.product_decision_activation"
ACTIVATION_SCHEMA_VERSION: Final = 1
DECISION_CYCLE_CONTRACT: Final = "autosport.product-paper-decision-cycle.v1"
PAPER_EXECUTION_MODE: Final = "PAPER"
_ACTIVATION_AUTHORITY_DOMAIN: Final = "autosport.product-decision-activation.v1"
_PRODUCT_AUTHORITY_ROOT_NAME: Final = "product-decision-activation-authority-v1"

# Freeze the exact product-import-time fingerprint authority. Exact instance type alone
# is insufficient because Python permits replacing a class property at runtime. START
# evidence must never dispatch through a caller-rebound live class descriptor.
_CANONICAL_EXECUTION_CONFIG_CLASS: Final = PaperExecutionModelConfig
_CANONICAL_EXECUTION_FINGERPRINT_PROPERTY: Final = (
    _CANONICAL_EXECUTION_CONFIG_CLASS.fingerprint
)
_CANONICAL_EXECUTION_FINGERPRINT_GETTER: Final = (
    _CANONICAL_EXECUTION_FINGERPRINT_PROPERTY.fget
    if isinstance(_CANONICAL_EXECUTION_FINGERPRINT_PROPERTY, property)
    else None
)
_CANONICAL_EXECUTION_FINGERPRINT_GETTER_CODE: Final = getattr(
    _CANONICAL_EXECUTION_FINGERPRINT_GETTER,
    "__code__",
    None,
)

# Supported START must bind the exact executable risk policy rather than trusting a
# caller-rebindable provenance property. Freeze the import-time class, provenance
# descriptor/getter and slot descriptors needed to reconstruct its canonical
# executable identity without virtual attribute dispatch.
_CANONICAL_RISK_POLICY_CLASS: Final = PaperRiskPolicy
_CANONICAL_RISK_POLICY_PROVENANCE_PROPERTY: Final = (
    _CANONICAL_RISK_POLICY_CLASS.provenance_sha256
)
_CANONICAL_RISK_POLICY_PROVENANCE_GETTER: Final = (
    _CANONICAL_RISK_POLICY_PROVENANCE_PROPERTY.fget
    if isinstance(_CANONICAL_RISK_POLICY_PROVENANCE_PROPERTY, property)
    else None
)
_CANONICAL_RISK_POLICY_PROVENANCE_GETTER_CODE: Final = getattr(
    _CANONICAL_RISK_POLICY_PROVENANCE_GETTER,
    "__code__",
    None,
)
_CANONICAL_RISK_POLICY_FIELD_DESCRIPTORS: Final = tuple(
    (name, getattr(_CANONICAL_RISK_POLICY_CLASS, name))
    for name in (
        "max_ticket_fraction",
        "max_committed_fraction",
        "minimum_cash_reserve_fraction",
        "economic_goal",
    )
)

# START's durable owner-goal re-resolution must not dispatch through a caller-rebound
# module class or class method. Capture the canonical EconomicGoalStore construction
# and read authority at product-module composition and invoke those callables
# non-virtually after verifying their identity/code is still intact.
_CANONICAL_ECONOMIC_GOAL_STORE_CLASS: Final = EconomicGoalStore
_CANONICAL_ECONOMIC_GOAL_STORE_INIT: Final = EconomicGoalStore.__init__
_CANONICAL_ECONOMIC_GOAL_STORE_INIT_CODE: Final = getattr(
    _CANONICAL_ECONOMIC_GOAL_STORE_INIT,
    "__code__",
    None,
)
_CANONICAL_ECONOMIC_GOAL_STORE_LOAD: Final = EconomicGoalStore.load
_CANONICAL_ECONOMIC_GOAL_STORE_LOAD_CODE: Final = getattr(
    _CANONICAL_ECONOMIC_GOAL_STORE_LOAD,
    "__code__",
    None,
)
_CANONICAL_ECONOMIC_GOAL_STORE_FILE_NAME: Final = (
    _CANONICAL_ECONOMIC_GOAL_STORE_CLASS.FILE_NAME
)
_CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD: Final = economic_goal_to_payload
_CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD_CODE: Final = getattr(
    _CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD,
    "__code__",
    None,
)


# Supported START must derive scientific identity from exact durable registry bytes.
# A live ScientificRegistry.get() method is application code and can be rebound at
# runtime without changing the registry file, so it is never positive START authority.
_CANONICAL_SCIENTIFIC_REGISTRY_CLASS: Final = ScientificRegistry
_CANONICAL_SCIENTIFIC_REGISTRY_SCHEMA_VERSION: Final = ScientificRegistry.SCHEMA_VERSION
_CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY: Final = ScientificRegistry._validate_entry
_CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY_CODE: Final = getattr(
    _CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY,
    "__code__",
    None,
)
_SCIENTIFIC_REGISTRY_ENTRY_FIELDS: Final = frozenset(
    {
        "record_type",
        "record_id",
        "available_at",
        "payload",
        "record_sha256",
    }
)


def _product_machine_state_base() -> Path:
    """Resolve machine state without caller/process trust-root overrides."""

    if os.name == "nt":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            # CSIDL_LOCAL_APPDATA. Query the Windows shell rather than trusting
            # the caller-editable LOCALAPPDATA environment variable.
            result = ctypes.windll.shell32.SHGetFolderPathW(  # type: ignore[attr-defined]
                None,
                0x001C,
                None,
                0,
                buffer,
            )
        except (AttributeError, OSError, ValueError) as exc:
            raise ProductDecisionActivationError(
                "cannot resolve product-owned Windows machine-state root"
            ) from exc
        if result != 0 or not buffer.value:
            raise ProductDecisionActivationError(
                "cannot resolve product-owned Windows machine-state root"
            )
        base = Path(buffer.value)
    else:
        try:
            import pwd

            home = pwd.getpwuid(os.getuid()).pw_dir
        except (ImportError, KeyError, OSError) as exc:
            raise ProductDecisionActivationError(
                "cannot resolve product-owned POSIX machine-state root"
            ) from exc
        base = Path(home) / ".local" / "state"

    if not base.is_absolute():
        raise ProductDecisionActivationError(
            "product-owned machine-state root must be absolute"
        )
    return base


def _product_activation_authority_root() -> Path:
    """Return the fixed product-owned root for durable START activation."""

    return (
        _product_machine_state_base()
        / "autosport"
        / _PRODUCT_AUTHORITY_ROOT_NAME
    )


class ProductDecisionActivationError(ValueError):
    """Durable supported-START activation evidence is invalid or has drifted."""


class BuiltInIntentProducer(StrEnum):
    """Closed product-owned intent producer registry; never an import path."""

    REGISTERED_STRATEGY = "canonical-registered-strategy-v1"


# START authority must not depend on the caller-rebindable module global used to
# name this closed registry. Freeze the exact import-time enum class, member
# identities and their product-owned values, then consume those frozen objects.
_CANONICAL_INTENT_PRODUCER_CLASS: Final = BuiltInIntentProducer
_CANONICAL_INTENT_PRODUCER_MEMBERS: Final = tuple(
    (member, member.value) for member in _CANONICAL_INTENT_PRODUCER_CLASS
)
_CANONICAL_INTENT_PRODUCER_VALUES: Final = frozenset(
    value for _, value in _CANONICAL_INTENT_PRODUCER_MEMBERS
)


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


def _durable_json_bytes(value: object) -> bytes:
    """Exact byte encoding emitted by integrity.atomic_write_json()."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        return (text + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductDecisionActivationError(
            "activation evidence is not durably serializable"
        ) from exc


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


def _canonical_risk_policy_authority(
    risk_policy: PaperRiskPolicy,
    *,
    durable_economic_goal: EconomicGoalContract,
    goal_sha256: str,
) -> tuple[str, dict[str, object]]:
    if (
        PaperRiskPolicy is not _CANONICAL_RISK_POLICY_CLASS
        or type(risk_policy) is not _CANONICAL_RISK_POLICY_CLASS
    ):
        raise ProductDecisionActivationError(
            "risk_policy must be the exact canonical PaperRiskPolicy"
        )

    live_provenance_property = getattr(
        _CANONICAL_RISK_POLICY_CLASS,
        "provenance_sha256",
        None,
    )
    if (
        not isinstance(_CANONICAL_RISK_POLICY_PROVENANCE_PROPERTY, property)
        or _CANONICAL_RISK_POLICY_PROVENANCE_GETTER is None
        or _CANONICAL_RISK_POLICY_PROVENANCE_GETTER_CODE is None
        or live_provenance_property
        is not _CANONICAL_RISK_POLICY_PROVENANCE_PROPERTY
        or live_provenance_property.fget
        is not _CANONICAL_RISK_POLICY_PROVENANCE_GETTER
        or getattr(
            _CANONICAL_RISK_POLICY_PROVENANCE_GETTER,
            "__code__",
            None,
        )
        is not _CANONICAL_RISK_POLICY_PROVENANCE_GETTER_CODE
    ):
        raise ProductDecisionActivationError(
            "canonical PaperRiskPolicy provenance authority changed"
        )

    values: dict[str, object] = {}
    for name, descriptor in _CANONICAL_RISK_POLICY_FIELD_DESCRIPTORS:
        if getattr(_CANONICAL_RISK_POLICY_CLASS, name, None) is not descriptor:
            raise ProductDecisionActivationError(
                "canonical PaperRiskPolicy field authority changed"
            )
        values[name] = descriptor.__get__(
            risk_policy,
            _CANONICAL_RISK_POLICY_CLASS,
        )

    bound_goal = values["economic_goal"]
    if (
        type(bound_goal) is not EconomicGoalContract
        or bound_goal != durable_economic_goal
    ):
        raise ProductDecisionActivationError(
            "risk policy must be bound to the exact EconomicGoalContract"
        )

    fractions: dict[str, Decimal] = {}
    for name in (
        "max_ticket_fraction",
        "max_committed_fraction",
        "minimum_cash_reserve_fraction",
    ):
        value = values[name]
        if (
            type(value) is not Decimal
            or not value.is_finite()
            or value < Decimal("0")
            or value > Decimal("1")
        ):
            raise ProductDecisionActivationError(
                "risk policy executable fractions are invalid"
            )
        fractions[name] = value

    policy_payload: dict[str, object] = {
        "max_ticket_fraction": str(fractions["max_ticket_fraction"]),
        "max_committed_fraction": str(fractions["max_committed_fraction"]),
        "minimum_cash_reserve_fraction": str(
            fractions["minimum_cash_reserve_fraction"]
        ),
        "economic_goal_contract_sha256": goal_sha256,
    }
    provenance_payload = {
        "schema": "autosport.paper_risk_policy_provenance",
        "schema_version": 1,
        **policy_payload,
    }
    return _digest(provenance_payload), policy_payload


def _risk_policy_file_digest(
    workspace: Path,
    *,
    expected_policy: dict[str, object],
    expected_provenance_sha256: str,
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
    if payload.get("policy_provenance_sha256") != expected_provenance_sha256:
        raise ProductDecisionActivationError(
            "paper risk policy file does not match executable risk provenance"
        )
    policy = payload.get("policy")
    if type(policy) is not dict:
        raise ProductDecisionActivationError("paper risk policy body is invalid")
    if policy != expected_policy:
        raise ProductDecisionActivationError(
            "paper risk policy file does not match exact executable risk policy"
        )
    return hashlib.sha256(raw).hexdigest()


def _validated_scientific_registry_records(
    path: Path,
) -> list[dict[str, object]]:
    """Read START scientific authority directly from exact durable registry bytes."""

    live_validate_entry = getattr(
        _CANONICAL_SCIENTIFIC_REGISTRY_CLASS,
        "_validate_entry",
        None,
    )
    if (
        ScientificRegistry is not _CANONICAL_SCIENTIFIC_REGISTRY_CLASS
        or _CANONICAL_SCIENTIFIC_REGISTRY_CLASS.SCHEMA_VERSION
        != _CANONICAL_SCIENTIFIC_REGISTRY_SCHEMA_VERSION
        or _CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY_CODE is None
        or live_validate_entry is not _CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY
        or getattr(
            live_validate_entry,
            "__code__",
            None,
        )
        is not _CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY_CODE
    ):
        raise ProductDecisionActivationError(
            "canonical ScientificRegistry authority changed"
        )

    _, state = _strict_json_file(path, "scientific registry")
    if (
        type(state) is not dict
        or state.get("schema_version")
        != _CANONICAL_SCIENTIFIC_REGISTRY_SCHEMA_VERSION
        or type(state.get("records")) is not list
    ):
        raise ProductDecisionActivationError("scientific registry schema mismatch")

    records: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for raw in state["records"]:
        try:
            _CANONICAL_SCIENTIFIC_REGISTRY_VALIDATE_ENTRY(raw)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProductDecisionActivationError(
                "scientific registry entry failed canonical validation"
            ) from exc
        if type(raw) is not dict or frozenset(raw) != _SCIENTIFIC_REGISTRY_ENTRY_FIELDS:
            raise ProductDecisionActivationError(
                "scientific registry entry fields mismatch"
            )
        record_type = _text(raw.get("record_type"), "scientific record_type")
        record_id = _text(raw.get("record_id"), "scientific record_id")
        available_at = _text(
            raw.get("available_at"),
            "scientific available_at",
        )
        payload = raw.get("payload")
        if type(payload) is not dict:
            raise ProductDecisionActivationError(
                "scientific registry payload must be an object"
            )
        declared = _sha256(
            raw.get("record_sha256"),
            "scientific record_sha256",
        )
        expected = _digest(
            {
                "record_type": record_type,
                "record_id": record_id,
                "available_at": available_at,
                "payload": payload,
            }
        )
        if declared != expected:
            raise ProductDecisionActivationError(
                "scientific registry record digest mismatch"
            )
        key = (record_type, record_id)
        if key in seen:
            raise ProductDecisionActivationError(
                "scientific registry contains duplicate record identity"
            )
        seen.add(key)
        records.append(raw)
    return records



def _validated_strategy_version_payload(
    entry: dict[str, object],
    wanted_strategy_version_id: str,
) -> dict[str, object]:
    payload = entry.get("payload")
    required = frozenset(
        {
            "strategy_version_id",
            "canonical_strategy_id",
            "source_sha256",
            "environment_sha256",
            "config_sha256",
            "created_at",
            "model_version_id",
            "predecessor_strategy_version_id",
        }
    )
    if type(payload) is not dict or frozenset(payload) != required:
        raise ProductDecisionActivationError(
            "activation StrategyVersion typed payload fields mismatch"
        )
    envelope_id = _text(
        entry.get("record_id"),
        "activation StrategyVersion envelope identity",
    )
    payload_id = _text(
        payload.get("strategy_version_id"),
        "activation StrategyVersion payload identity",
    )
    wanted = _text(
        wanted_strategy_version_id,
        "strategy_version_id",
    )
    if envelope_id != wanted or payload_id != wanted:
        raise ProductDecisionActivationError(
            "activation StrategyVersion payload identity does not match durable envelope"
        )
    envelope_available_at = _text(
        entry.get("available_at"),
        "activation StrategyVersion envelope available_at",
    )
    payload_created_at = _text(
        payload.get("created_at"),
        "activation StrategyVersion payload created_at",
    )
    if payload_created_at != envelope_available_at:
        raise ProductDecisionActivationError(
            "activation StrategyVersion payload timestamp does not match durable envelope"
        )
    _text(
        payload.get("canonical_strategy_id"),
        "activation canonical_strategy_id",
    )
    for field in ("source_sha256", "environment_sha256", "config_sha256"):
        _sha256(payload.get(field), f"activation StrategyVersion {field}")
    for field in ("model_version_id", "predecessor_strategy_version_id"):
        value = payload.get(field)
        if value is not None:
            _text(value, f"activation StrategyVersion {field}")
    return dict(payload)


def _validated_model_version_payload(
    entry: dict[str, object],
    wanted_model_version_id: str,
) -> dict[str, object]:
    payload = entry.get("payload")
    required = frozenset(
        {
            "model_version_id",
            "model_family",
            "artifact_sha256",
            "source_sha256",
            "environment_sha256",
            "dataset_snapshot_id",
            "feature_set_id",
            "research_protocol_id",
            "seed",
            "config_sha256",
            "created_at",
            "predecessor_model_version_id",
        }
    )
    if type(payload) is not dict or frozenset(payload) != required:
        raise ProductDecisionActivationError(
            "activation ModelVersion typed payload fields mismatch"
        )
    envelope_id = _text(
        entry.get("record_id"),
        "activation ModelVersion envelope identity",
    )
    payload_id = _text(
        payload.get("model_version_id"),
        "activation ModelVersion payload identity",
    )
    wanted = _text(
        wanted_model_version_id,
        "strategy model_version_id",
    )
    if envelope_id != wanted or payload_id != wanted:
        raise ProductDecisionActivationError(
            "activation ModelVersion payload identity does not match durable envelope"
        )
    envelope_available_at = _text(
        entry.get("available_at"),
        "activation ModelVersion envelope available_at",
    )
    payload_created_at = _text(
        payload.get("created_at"),
        "activation ModelVersion payload created_at",
    )
    if payload_created_at != envelope_available_at:
        raise ProductDecisionActivationError(
            "activation ModelVersion payload timestamp does not match durable envelope"
        )
    for field in (
        "model_family",
        "dataset_snapshot_id",
        "feature_set_id",
        "research_protocol_id",
    ):
        _text(payload.get(field), f"activation ModelVersion {field}")
    for field in (
        "artifact_sha256",
        "source_sha256",
        "environment_sha256",
        "config_sha256",
    ):
        _sha256(payload.get(field), f"activation ModelVersion {field}")
    if type(payload.get("seed")) is not int:
        raise ProductDecisionActivationError(
            "activation ModelVersion seed must be an integer"
        )
    predecessor = payload.get("predecessor_model_version_id")
    if predecessor is not None:
        _text(
            predecessor,
            "activation ModelVersion predecessor_model_version_id",
        )
    return dict(payload)

def _registry_strategy_prefix(
    workspace: Path,
    registry: ScientificRegistry,
    strategy_version_id: str,
) -> tuple[dict[str, object], int, str, str, str | None]:
    if (
        ScientificRegistry is not _CANONICAL_SCIENTIFIC_REGISTRY_CLASS
        or type(registry) is not _CANONICAL_SCIENTIFIC_REGISTRY_CLASS
    ):
        raise ProductDecisionActivationError(
            "scientific_registry must be the exact canonical ScientificRegistry"
        )
    path = _require_workspace_path(
        workspace,
        Path(registry.path),
        "scientific_registry.json",
        "scientific registry",
    )
    wanted = _text(strategy_version_id, "strategy_version_id")
    records = _validated_scientific_registry_records(path)
    index = next(
        (
            i
            for i, raw in enumerate(records)
            if raw["record_type"] == "StrategyVersion"
            and raw["record_id"] == wanted
        ),
        None,
    )
    if index is None:
        raise ProductDecisionActivationError(
            "activation StrategyVersion is absent from registry sequence"
        )

    strategy_entry = records[index]
    strategy_payload = _validated_strategy_version_payload(
        strategy_entry,
        wanted,
    )
    strategy_record_sha256 = _sha256(
        strategy_entry["record_sha256"],
        "strategy record_sha256",
    )

    model_record_sha256: str | None = None
    model_version_id = strategy_payload.get("model_version_id")
    if model_version_id is not None:
        model_version_id = _text(
            model_version_id,
            "strategy model_version_id",
        )
        model_index = next(
            (
                i
                for i, raw in enumerate(records)
                if raw["record_type"] == "ModelVersion"
                and raw["record_id"] == model_version_id
            ),
            None,
        )
        if model_index is None:
            raise ProductDecisionActivationError(
                "activation StrategyVersion references a missing ModelVersion"
            )
        if model_index >= index:
            raise ProductDecisionActivationError(
                "activation ModelVersion was not durable before StrategyVersion"
            )
        model_entry = records[model_index]
        _validated_model_version_payload(
            model_entry,
            model_version_id,
        )
        model_record_sha256 = _sha256(
            model_entry["record_sha256"],
            "strategy model record_sha256",
        )

    prefix = {
        "schema_version": _CANONICAL_SCIENTIFIC_REGISTRY_SCHEMA_VERSION,
        "records": records[: index + 1],
    }
    return (
        strategy_payload,
        index + 1,
        _digest(prefix),
        strategy_record_sha256,
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
        if self.intent_producer_id not in _CANONICAL_INTENT_PRODUCER_VALUES:
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

    __slots__ = ("workspace", "path", "_authority", "__weakref__")
    FILE_NAME: Final = "product_decision_activation.json"

    def __setattr__(self, name: str, value: object) -> None:
        if name in ("workspace", "path", "_authority"):
            try:
                object.__getattribute__(self, name)
            except AttributeError:
                pass
            else:
                raise ProductDecisionActivationError(
                    "product decision activation store construction state is immutable"
                )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in ("workspace", "path", "_authority"):
            raise ProductDecisionActivationError(
                "product decision activation store construction state is immutable"
            )
        object.__delattr__(self, name)

    def __init__(self, workspace: str | Path) -> None:
        if type(self) is not __class__:
            raise ProductDecisionActivationError(
                "product decision activation store must be the exact canonical class"
            )
        if type(self).FILE_NAME != "product_decision_activation.json":
            raise ProductDecisionActivationError(
                "canonical product decision activation store namespace changed"
            )
        self.workspace = Path(workspace).absolute().resolve(strict=False)
        self.path = self.workspace / "product_decision_activation.json"
        try:
            self._authority = MonotonicWorkspaceAuthority(
                workspace=self.workspace,
                domain=_ACTIVATION_AUTHORITY_DOMAIN,
                key="product_decision_activation.json",
                authority_root=_product_activation_authority_root(),
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductDecisionActivationError(
                "cannot bind product decision activation anti-rollback authority"
            ) from exc

    def _observed_state_sha256(self) -> str | None:
        if self.path.is_symlink():
            raise ProductDecisionActivationError(
                "product decision activation must be a regular file"
            )
        if not self.path.exists():
            return None
        raw = _read_regular_file(self.path, "product decision activation")
        return hashlib.sha256(raw).hexdigest()

    def _recover_authority(self) -> str | None:
        observed = self._observed_state_sha256()
        try:
            history = self._authority.read_history()
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if pending is None:
                self._authority.recover(observed_state_sha256=observed)
            else:
                self._authority.recover(
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductDecisionActivationError(
                "product decision activation anti-rollback authority rejected workspace state"
            ) from exc
        return observed

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
        if type(economic_goal) is not EconomicGoalContract:
            raise ProductDecisionActivationError(
                "economic_goal must be the exact canonical EconomicGoalContract"
            )
        if EconomicGoalStore is not _CANONICAL_ECONOMIC_GOAL_STORE_CLASS:
            raise ProductDecisionActivationError(
                "canonical EconomicGoalStore authority changed"
            )
        live_store_init = getattr(
            _CANONICAL_ECONOMIC_GOAL_STORE_CLASS,
            "__init__",
            None,
        )
        live_store_load = getattr(
            _CANONICAL_ECONOMIC_GOAL_STORE_CLASS,
            "load",
            None,
        )
        if (
            _CANONICAL_ECONOMIC_GOAL_STORE_INIT_CODE is None
            or _CANONICAL_ECONOMIC_GOAL_STORE_LOAD_CODE is None
            or live_store_init is not _CANONICAL_ECONOMIC_GOAL_STORE_INIT
            or getattr(live_store_init, "__code__", None)
            is not _CANONICAL_ECONOMIC_GOAL_STORE_INIT_CODE
            or live_store_load is not _CANONICAL_ECONOMIC_GOAL_STORE_LOAD
            or getattr(live_store_load, "__code__", None)
            is not _CANONICAL_ECONOMIC_GOAL_STORE_LOAD_CODE
            or _CANONICAL_ECONOMIC_GOAL_STORE_CLASS.FILE_NAME
            != _CANONICAL_ECONOMIC_GOAL_STORE_FILE_NAME
            or economic_goal_to_payload is not _CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD
            or _CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD_CODE is None
            or getattr(_CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD, "__code__", None)
            is not _CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD_CODE
        ):
            raise ProductDecisionActivationError(
                "canonical EconomicGoalStore dispatch changed"
            )

        durable_goal_store = object.__new__(
            _CANONICAL_ECONOMIC_GOAL_STORE_CLASS
        )
        # Do not call the otherwise-canonical constructor here: its implementation
        # resolves self.FILE_NAME dynamically. Build the two canonical store fields
        # from the already-owned workspace and the import-time frozen filename so a
        # transient class-attribute rebind cannot redirect START authority.
        durable_goal_store.workspace = self.workspace
        durable_goal_store.path = (
            self.workspace / _CANONICAL_ECONOMIC_GOAL_STORE_FILE_NAME
        )
        bound_store_load = getattr(durable_goal_store, "load", None)
        if (
            getattr(bound_store_load, "__self__", None) is not durable_goal_store
            or getattr(bound_store_load, "__func__", None)
            is not _CANONICAL_ECONOMIC_GOAL_STORE_LOAD
        ):
            raise ProductDecisionActivationError(
                "canonical EconomicGoalStore instance dispatch changed"
            )
        # Do not let EconomicGoalStore.load() or its mutable decoder graph grant
        # positive START authority. The canonical owner writer uses atomic_write_json,
        # whose exact deterministic encoding is mirrored by _durable_json_bytes().
        # Bind the exact on-disk bytes to the exact caller-supplied canonical contract
        # before that contract can participate in risk/provenance activation.
        try:
            expected_goal_bytes = _durable_json_bytes(
                _CANONICAL_ECONOMIC_GOAL_TO_PAYLOAD(economic_goal)
            )
        except EconomicGoalContractError as exc:
            raise ProductDecisionActivationError(
                "cannot serialize canonical owner EconomicGoalContract"
            ) from exc
        durable_goal_bytes = _read_regular_file(
            durable_goal_store.path,
            "durable owner EconomicGoalContract",
        )
        if durable_goal_bytes != expected_goal_bytes:
            raise ProductDecisionActivationError(
                "economic_goal no longer matches exact durable authority owner bytes"
            )
        durable_economic_goal = economic_goal
        goal = provenance_for(durable_economic_goal)
        (
            risk_policy_provenance_sha256,
            risk_policy_payload,
        ) = _canonical_risk_policy_authority(
            risk_policy,
            durable_economic_goal=durable_economic_goal,
            goal_sha256=goal.contract_sha256,
        )
        if (
            PaperExecutionModelConfig is not _CANONICAL_EXECUTION_CONFIG_CLASS
            or type(execution_config) is not _CANONICAL_EXECUTION_CONFIG_CLASS
        ):
            raise ProductDecisionActivationError(
                "execution_config must be the exact canonical PaperExecutionModelConfig"
            )
        # Never dispatch START authority through the live class descriptor. Even an
        # exact frozen dataclass instance can be relabelled if caller code temporarily
        # replaces PaperExecutionModelConfig.fingerprint. Require the original
        # import-time property/getter/code identity and invoke that captured getter
        # directly, so transient class-descriptor substitution fails closed.
        live_fingerprint_property = _CANONICAL_EXECUTION_CONFIG_CLASS.fingerprint
        if (
            not isinstance(_CANONICAL_EXECUTION_FINGERPRINT_PROPERTY, property)
            or _CANONICAL_EXECUTION_FINGERPRINT_GETTER is None
            or _CANONICAL_EXECUTION_FINGERPRINT_GETTER_CODE is None
            or live_fingerprint_property
            is not _CANONICAL_EXECUTION_FINGERPRINT_PROPERTY
            or live_fingerprint_property.fget
            is not _CANONICAL_EXECUTION_FINGERPRINT_GETTER
            or getattr(
                _CANONICAL_EXECUTION_FINGERPRINT_GETTER,
                "__code__",
                None,
            )
            is not _CANONICAL_EXECUTION_FINGERPRINT_GETTER_CODE
        ):
            raise ProductDecisionActivationError(
                "canonical PaperExecutionModelConfig fingerprint authority changed"
            )
        execution_model_fingerprint = _sha256(
            _CANONICAL_EXECUTION_FINGERPRINT_GETTER(execution_config),
            "execution_model_fingerprint",
        )
        if (
            BuiltInIntentProducer is not _CANONICAL_INTENT_PRODUCER_CLASS
            or type(intent_producer) is not _CANONICAL_INTENT_PRODUCER_CLASS
        ):
            raise ProductDecisionActivationError(
                "intent producer must come from the closed product registry"
            )
        intent_producer_id = next(
            (
                value
                for member, value in _CANONICAL_INTENT_PRODUCER_MEMBERS
                if intent_producer is member
            ),
            None,
        )
        if intent_producer_id is None:
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

        composition_sha, source_id, initial_bankroll = _product_composition(
            self.workspace
        )
        risk_file_sha = _risk_policy_file_digest(
            self.workspace,
            expected_policy=risk_policy_payload,
            expected_provenance_sha256=risk_policy_provenance_sha256,
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
            goal_id=durable_economic_goal.goal_id,
            goal_revision=durable_economic_goal.revision,
            bankroll_id=durable_economic_goal.bankroll_id,
            currency=durable_economic_goal.currency,
            risk_policy_provenance_sha256=_sha256(
                risk_policy_provenance_sha256,
                "risk_policy_provenance_sha256",
            ),
            risk_policy_file_sha256=risk_file_sha,
            intent_producer_id=intent_producer_id,
            execution_mode=PAPER_EXECUTION_MODE,
            execution_model_fingerprint=execution_model_fingerprint,
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
        """Create once with an independent freshness witness; exact retry is idempotent."""

        if type(self) is not __class__:
            raise ProductDecisionActivationError(
                "product decision activation store must be the exact canonical class"
            )
        with WorkspaceEconomicLock(self.workspace):
            observed = self._recover_authority()
            expected = self._derive(
                scientific_registry=scientific_registry,
                strategy_version_id=strategy_version_id,
                economic_goal=economic_goal,
                risk_policy=risk_policy,
                execution_config=execution_config,
                intent_producer=intent_producer,
            )
            if self.path.exists():
                existing = self._load_local()
                if existing != expected:
                    raise ProductDecisionActivationError(
                        "durable product decision activation conflicts with requested START"
                    )
                return existing
            if observed is not None:
                raise ProductDecisionActivationError(
                    "activation state disappeared after anti-rollback verification"
                )

            root = self._root(expected)
            intended = hashlib.sha256(_durable_json_bytes(root)).hexdigest()
            semantic_binding = _digest(
                {
                    "kind": "PRODUCT_DECISION_ACTIVATION_CREATE",
                    "activation_filename": "product_decision_activation.json",
                    "binding_sha256": expected.binding_sha256,
                    "intended_state_sha256": intended,
                }
            )
            tx_id = f"activation-{uuid.uuid4().hex}"
            try:
                self._authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=semantic_binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise ProductDecisionActivationError(
                    "cannot prepare product decision activation anti-rollback witness"
                ) from exc

            # A failure here deliberately leaves PREPARE durable. On restart,
            # _recover_authority() aborts if the local file is still absent or
            # commits only if the exact prepared bytes were published.
            atomic_write_json(self.path, root)
            published = self._observed_state_sha256()
            if published != intended:
                raise ProductDecisionActivationError(
                    "published product decision activation differs from prepared bytes"
                )
            persisted = self._load_local()
            if persisted != expected:
                raise ProductDecisionActivationError(
                    "persisted product decision activation does not match requested START"
                )
            try:
                self._authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=published,
                    semantic_binding_sha256=semantic_binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise ProductDecisionActivationError(
                    "cannot commit product decision activation anti-rollback witness"
                ) from exc
            return persisted

    def _load_local(self) -> ProductDecisionActivationBinding:
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

    def load(self) -> ProductDecisionActivationBinding:
        """Load only bytes that still match the independent monotonic witness."""

        if type(self) is not __class__:
            raise ProductDecisionActivationError(
                "product decision activation store must be the exact canonical class"
            )
        with WorkspaceEconomicLock(self.workspace):
            # Preserve precise malformed-state diagnostics before freshness
            # adjudication. Structurally valid old/rebound bytes are then rejected
            # by the independent authority below.
            if self.path.exists() or self.path.is_symlink():
                binding = self._load_local()
                self._recover_authority()
                return binding
            self._recover_authority()
            return self._load_local()

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

        if type(self) is not __class__:
            raise ProductDecisionActivationError(
                "product decision activation store must be the exact canonical class"
            )
        with WorkspaceEconomicLock(self.workspace):
            if self.path.exists() or self.path.is_symlink():
                persisted = self._load_local()
                self._recover_authority()
            else:
                self._recover_authority()
                persisted = self._load_local()
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

def _seal_product_decision_activation_derive_dispatch() -> None:
    """Seal positive START derivation and its nested anti-rollback authority."""

    store_class = ProductDecisionActivationStore
    authority_type = MonotonicWorkspaceAuthority
    workspace_binding_type = WorkspaceIdentityBinding
    path_type = Path
    authority_domain = _ACTIVATION_AUTHORITY_DOMAIN
    authority_id = MONOTONIC_AUTHORITY_ID
    activation_filename = "product_decision_activation.json"

    canonical_authority_root = _product_activation_authority_root
    canonical_authority_root_code = getattr(canonical_authority_root, "__code__", None)
    canonical_init = store_class.__dict__.get("__init__")
    canonical_init_code = getattr(canonical_init, "__code__", None)
    constructor_authorities: weakref.WeakKeyDictionary[
        ProductDecisionActivationStore, MonotonicWorkspaceAuthority
    ] = weakref.WeakKeyDictionary()

    canonical_derive = store_class.__dict__.get("_derive")
    canonical_derive_code = getattr(canonical_derive, "__code__", None)
    canonical_load_local = store_class.__dict__.get("_load_local")
    canonical_load_local_code = getattr(canonical_load_local, "__code__", None)
    canonical_observed_state = store_class.__dict__.get("_observed_state_sha256")
    canonical_observed_state_code = getattr(
        canonical_observed_state, "__code__", None
    )

    canonical_authority_methods = {
        name: authority_type.__dict__.get(name)
        for name in ("read_history", "recover", "prepare", "commit")
    }
    canonical_authority_codes = {
        name: getattr(method, "__code__", None)
        for name, method in canonical_authority_methods.items()
    }
    # The public MWA entrypoints above still virtual-dispatch through these lower
    # class helpers. Freezing only read_history/recover/prepare/commit therefore
    # leaves a transient helper substitution able to forge history or suppress a
    # durable append while the checked top-level methods remain unchanged.
    canonical_authority_helper_names = (
        "_load_bound_history",
        "_validate_workspace_binding",
        "_ensure_workspace_bound",
        "_load_history",
        "_decode_record",
        "_latest_record_for_tx",
        "_require_same_transaction",
        "_validate_prepare_retry",
        "_new_record",
        "_new_terminal_record",
        "_append_record",
        "_payload",
        "_namespace_payload",
        "_ensure_namespace_marker",
        "_validate_namespace_marker",
    )
    canonical_authority_helpers = {
        name: getattr(authority_type, name, None)
        for name in canonical_authority_helper_names
    }
    canonical_authority_helper_codes = {
        name: getattr(method, "__code__", None)
        for name, method in canonical_authority_helpers.items()
    }

    if (
        canonical_authority_root_code is None
        or canonical_init is None
        or canonical_init_code is None
        or canonical_derive is None
        or canonical_derive_code is None
        or canonical_load_local is None
        or canonical_load_local_code is None
        or canonical_observed_state is None
        or canonical_observed_state_code is None
        or any(method is None for method in canonical_authority_methods.values())
        or any(code is None for code in canonical_authority_codes.values())
        or any(method is None for method in canonical_authority_helpers.values())
        or any(code is None for code in canonical_authority_helper_codes.values())
    ):
        raise RuntimeError(
            "canonical product decision activation authority composition is unavailable"
        )

    def require_store_helper(
        name: str,
        canonical: object,
        canonical_code: object,
    ) -> None:
        live = store_class.__dict__.get(name)
        if live is not canonical or getattr(live, "__code__", None) is not canonical_code:
            raise ProductDecisionActivationError(
                f"canonical product decision activation {name} authority changed"
            )

    def require_authority_dispatch(authority: MonotonicWorkspaceAuthority) -> None:
        instance_state = getattr(authority, "__dict__", None)
        if type(instance_state) is not dict:
            raise ProductDecisionActivationError(
                "nested anti-rollback authority instance state is unavailable"
            )
        for name, canonical in canonical_authority_methods.items():
            live = authority_type.__dict__.get(name)
            if (
                live is not canonical
                or getattr(live, "__code__", None)
                is not canonical_authority_codes[name]
                or name in instance_state
            ):
                raise ProductDecisionActivationError(
                    "nested anti-rollback authority dispatch changed"
                )
        for name, canonical in canonical_authority_helpers.items():
            live = getattr(authority_type, name, None)
            if (
                live is not canonical
                or getattr(live, "__code__", None)
                is not canonical_authority_helper_codes[name]
                or name in instance_state
            ):
                raise ProductDecisionActivationError(
                    "nested anti-rollback authority lower dispatch changed"
                )

    def require_store_state(store: ProductDecisionActivationStore) -> None:
        if type(store) is not store_class:
            raise ProductDecisionActivationError(
                "product decision activation store must be the exact canonical class"
            )
        if (
            _product_activation_authority_root is not canonical_authority_root
            or getattr(_product_activation_authority_root, "__code__", None)
            is not canonical_authority_root_code
        ):
            raise ProductDecisionActivationError(
                "product decision activation authority root resolver changed"
            )
        try:
            workspace = store.workspace
            path = store.path
            authority = store._authority
            constructor_authority = constructor_authorities.get(store)
        except (AttributeError, TypeError) as exc:
            raise ProductDecisionActivationError(
                "product decision activation store construction state changed"
            ) from exc
        if constructor_authority is None or authority is not constructor_authority:
            raise ProductDecisionActivationError(
                "product decision activation constructor authority identity changed"
            )

        try:
            expected_root = canonical_authority_root()
        except (OSError, RuntimeError) as exc:
            raise ProductDecisionActivationError(
                "cannot resolve product decision activation authority root"
            ) from exc

        if (
            not isinstance(workspace, path_type)
            or not isinstance(path, path_type)
            or path != workspace / activation_filename
            or type(authority) is not authority_type
            or authority.workspace != workspace
            or authority.domain != authority_domain
            or authority.key != activation_filename
            or authority.authority_root != expected_root
            or type(authority.workspace_binding) is not workspace_binding_type
        ):
            raise ProductDecisionActivationError(
                "product decision activation store construction state changed"
            )

        binding = authority.workspace_binding
        expected_namespace = hashlib.sha256(
            "\0".join(
                (
                    authority_id,
                    authority.workspace_instance_id,
                    authority_domain,
                    activation_filename,
                )
            ).encode("utf-8")
        ).hexdigest()
        expected_journal = (
            expected_root
            / "journals"
            / expected_namespace[:2]
            / expected_namespace
        )
        expected_records = expected_journal / "records"
        expected_namespace_marker = (
            expected_root
            / "namespace-bindings"
            / expected_namespace[:2]
            / f"{expected_namespace}.json"
        )
        expected_workspace_marker = (
            workspace / ".autosport" / "monotonic-workspace-binding.json"
        )
        expected_path_binding = (
            expected_root
            / "workspace-bindings"
            / binding.workspace_locator_sha256[:2]
            / f"{binding.workspace_locator_sha256}.json"
        )
        if (
            binding.workspace != workspace
            or binding.authority_root != expected_root
            or binding.workspace_instance_id != authority.workspace_instance_id
            or binding.workspace_marker_path != expected_workspace_marker
            or binding.path_binding_path != expected_path_binding
            or hashlib.sha256(
                binding.workspace_locator.encode("utf-8")
            ).hexdigest()
            != binding.workspace_locator_sha256
            or authority.workspace_binding_path != expected_workspace_marker
            or authority.namespace_sha256 != expected_namespace
            or authority.journal_dir != expected_journal
            or authority.records_dir != expected_records
            or authority.namespace_marker_path != expected_namespace_marker
        ):
            raise ProductDecisionActivationError(
                "nested anti-rollback authority construction state changed"
            )
        require_authority_dispatch(authority)

    def observed_state_canonically(
        store: ProductDecisionActivationStore,
    ) -> str | None:
        require_store_state(store)
        require_store_helper(
            "_observed_state_sha256",
            canonical_observed_state,
            canonical_observed_state_code,
        )
        return canonical_observed_state(store)

    def load_local_canonically(
        store: ProductDecisionActivationStore,
    ) -> ProductDecisionActivationBinding:
        require_store_state(store)
        require_store_helper(
            "_load_local",
            canonical_load_local,
            canonical_load_local_code,
        )
        return canonical_load_local(store)

    def recover_authority_canonically(
        store: ProductDecisionActivationStore,
    ) -> str | None:
        require_store_state(store)
        authority = store._authority
        observed = observed_state_canonically(store)
        read_history = canonical_authority_methods["read_history"]
        recover = canonical_authority_methods["recover"]
        try:
            history = read_history(authority)
            pending = (
                history[-1]
                if history and history[-1].phase is AuthorityPhase.PREPARE
                else None
            )
            if pending is None:
                recover(authority, observed_state_sha256=observed)
            else:
                recover(
                    authority,
                    observed_state_sha256=observed,
                    tx_id=pending.tx_id,
                    semantic_binding_sha256=pending.semantic_binding_sha256,
                )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductDecisionActivationError(
                "product decision activation anti-rollback authority rejected workspace state"
            ) from exc
        require_store_state(store)
        return observed

    def prepare_authority_canonically(
        store: ProductDecisionActivationStore,
        *,
        tx_id: str,
        intended_state_sha256: str,
        semantic_binding_sha256: str,
    ) -> None:
        require_store_state(store)
        prepare = canonical_authority_methods["prepare"]
        try:
            prepare(
                store._authority,
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=intended_state_sha256,
                semantic_binding_sha256=semantic_binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductDecisionActivationError(
                "cannot prepare product decision activation anti-rollback witness"
            ) from exc
        require_store_state(store)

    def commit_authority_canonically(
        store: ProductDecisionActivationStore,
        *,
        tx_id: str,
        observed_state_sha256: str,
        semantic_binding_sha256: str,
    ) -> None:
        require_store_state(store)
        commit = canonical_authority_methods["commit"]
        try:
            commit(
                store._authority,
                tx_id=tx_id,
                observed_state_sha256=observed_state_sha256,
                semantic_binding_sha256=semantic_binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductDecisionActivationError(
                "cannot commit product decision activation anti-rollback witness"
            ) from exc
        require_store_state(store)

    def derive_canonically(
        store: ProductDecisionActivationStore,
        *,
        scientific_registry: ScientificRegistry,
        strategy_version_id: str,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        execution_config: PaperExecutionModelConfig,
        intent_producer: BuiltInIntentProducer,
    ) -> ProductDecisionActivationBinding:
        require_store_state(store)
        live_derive = store_class.__dict__.get("_derive")
        if (
            live_derive is not canonical_derive
            or getattr(live_derive, "__code__", None) is not canonical_derive_code
        ):
            raise ProductDecisionActivationError(
                "canonical product decision activation derivation authority changed"
            )
        return canonical_derive(
            store,
            scientific_registry=scientific_registry,
            strategy_version_id=strategy_version_id,
            economic_goal=economic_goal,
            risk_policy=risk_policy,
            execution_config=execution_config,
            intent_producer=intent_producer,
        )

    def initialize_owner(
        self: ProductDecisionActivationStore,
        *,
        scientific_registry: ScientificRegistry,
        strategy_version_id: str,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        execution_config: PaperExecutionModelConfig,
        intent_producer: BuiltInIntentProducer = BuiltInIntentProducer.REGISTERED_STRATEGY,
    ) -> ProductDecisionActivationBinding:
        """Create once with an independent freshness witness; exact retry is idempotent."""

        require_store_state(self)
        with WorkspaceEconomicLock(self.workspace):
            observed = recover_authority_canonically(self)
            expected = derive_canonically(
                self,
                scientific_registry=scientific_registry,
                strategy_version_id=strategy_version_id,
                economic_goal=economic_goal,
                risk_policy=risk_policy,
                execution_config=execution_config,
                intent_producer=intent_producer,
            )
            if self.path.exists():
                existing = load_local_canonically(self)
                if existing != expected:
                    raise ProductDecisionActivationError(
                        "durable product decision activation conflicts with requested START"
                    )
                return existing
            if observed is not None:
                raise ProductDecisionActivationError(
                    "activation state disappeared after anti-rollback verification"
                )

            root = self._root(expected)
            intended = hashlib.sha256(_durable_json_bytes(root)).hexdigest()
            semantic_binding = _digest(
                {
                    "kind": "PRODUCT_DECISION_ACTIVATION_CREATE",
                    "activation_filename": activation_filename,
                    "binding_sha256": expected.binding_sha256,
                    "intended_state_sha256": intended,
                }
            )
            tx_id = f"activation-{uuid.uuid4().hex}"
            prepare_authority_canonically(
                self,
                tx_id=tx_id,
                intended_state_sha256=intended,
                semantic_binding_sha256=semantic_binding,
            )

            atomic_write_json(self.path, root)
            published = observed_state_canonically(self)
            if published != intended:
                raise ProductDecisionActivationError(
                    "published product decision activation differs from prepared bytes"
                )
            persisted = load_local_canonically(self)
            if persisted != expected:
                raise ProductDecisionActivationError(
                    "persisted product decision activation does not match requested START"
                )
            commit_authority_canonically(
                self,
                tx_id=tx_id,
                observed_state_sha256=published,
                semantic_binding_sha256=semantic_binding,
            )
            return persisted

    def verify(
        self: ProductDecisionActivationStore,
        *,
        scientific_registry: ScientificRegistry,
        strategy_version_id: str,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        execution_config: PaperExecutionModelConfig,
        intent_producer: BuiltInIntentProducer = BuiltInIntentProducer.REGISTERED_STRATEGY,
    ) -> ProductDecisionActivationBinding:
        """Re-resolve every child authority and reject START-time drift."""

        require_store_state(self)
        with WorkspaceEconomicLock(self.workspace):
            if self.path.exists() or self.path.is_symlink():
                persisted = load_local_canonically(self)
                recover_authority_canonically(self)
            else:
                recover_authority_canonically(self)
                persisted = load_local_canonically(self)
            expected = derive_canonically(
                self,
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

    def load(self: ProductDecisionActivationStore) -> ProductDecisionActivationBinding:
        """Load only bytes that match the sealed nested monotonic authority."""

        require_store_state(self)
        with WorkspaceEconomicLock(self.workspace):
            if self.path.exists() or self.path.is_symlink():
                binding = load_local_canonically(self)
                recover_authority_canonically(self)
                return binding
            recover_authority_canonically(self)
            return load_local_canonically(self)

    def init(
        self: ProductDecisionActivationStore,
        workspace: str | Path,
    ) -> None:
        """Construct one store and bind its exact nested authority in closure state."""

        live_init = canonical_init
        if getattr(live_init, "__code__", None) is not canonical_init_code:
            raise ProductDecisionActivationError(
                "canonical product decision activation constructor authority changed"
            )
        canonical_init(self, workspace)
        constructor_authorities[self] = self._authority

    setattr(store_class, "__init__", init)
    setattr(store_class, "initialize_owner", initialize_owner)
    setattr(store_class, "verify", verify)
    setattr(store_class, "load", load)


_seal_product_decision_activation_derive_dispatch()
del _seal_product_decision_activation_derive_dispatch

