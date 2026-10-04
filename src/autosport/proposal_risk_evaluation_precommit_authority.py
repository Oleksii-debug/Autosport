from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .decision_ledger import (
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .economic_goal import EconomicGoalContract
from .economic_goal_store import EconomicGoalContractError, EconomicGoalStore
from .integrity import ensure_durable_file
from .proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    resolve_product_proposal_risk_target,
)
from .risk import PaperRiskPolicy
from .risk_evaluation_precommit_authority import (
    ProductFixedNRiskEvaluationPrecommitAuthority,
    ProductRiskEvaluationPrecommitError,
    resolve_product_fixed_n_risk_evaluation_precommit,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA = "autosport.proposal-risk-evaluation-precommit.v1"
_ACTION = "PROPOSAL_RISK_EVALUATION_PRECOMMIT"
_AGENT = "autosport.proposal-risk-evaluation-precommit-authority.v1"
_ACTION_PREFIX = "proposal-risk-evaluation-precommit-v1:"
_HEX = frozenset("0123456789abcdef")
_MAX_DECIMAL_TEXT = 256
_PROPOSAL_EVALUATION_SCOPE = (
    "EXACT_PROPOSAL_TARGET_FIXED_STAKE_VECTOR_COUNTERFACTUAL_V1"
)
_PROTOCOL_CONSTANTS = (
    _SCHEMA,
    _ACTION,
    _AGENT,
    _ACTION_PREFIX,
    _PROPOSAL_EVALUATION_SCOPE,
)
_PROTOCOL_CONSTANTS_EXPECTED = (
    "autosport.proposal-risk-evaluation-precommit.v1",
    "PROPOSAL_RISK_EVALUATION_PRECOMMIT",
    "autosport.proposal-risk-evaluation-precommit-authority.v1",
    "proposal-risk-evaluation-precommit-v1:",
    "EXACT_PROPOSAL_TARGET_FIXED_STAKE_VECTOR_COUNTERFACTUAL_V1",
)

_TARGET_TYPE = ProductProposalRiskTarget
_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_SCIENCE_TYPE = ProductFixedNRiskEvaluationPrecommitAuthority
_SCIENCE_RESOLVER = resolve_product_fixed_n_risk_evaluation_precommit
_SCIENCE_RESOLVER_CODE = getattr(_SCIENCE_RESOLVER, "__code__", None)
_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_GOAL_TYPE = EconomicGoalContract
_GOAL_STORE_TYPE = EconomicGoalStore
_GOAL_LOAD = EconomicGoalStore.load
_GOAL_LOAD_CODE = getattr(_GOAL_LOAD, "__code__", None)
_POLICY_TYPE = PaperRiskPolicy
_LEDGER_TYPE = JsonlDecisionLedger
_LEDGER_APPEND = JsonlDecisionLedger.append_economic
_LEDGER_APPEND_CODE = getattr(_LEDGER_APPEND, "__code__", None)
_LEDGER_RESOLVE = JsonlDecisionLedger.verified_economic_decision_for_material_action
_LEDGER_RESOLVE_CODE = getattr(_LEDGER_RESOLVE, "__code__", None)
_LEDGER_VERIFY = JsonlDecisionLedger.verify_integrity
_LEDGER_VERIFY_CODE = getattr(_LEDGER_VERIFY, "__code__", None)
_RECORD_TYPE = DecisionRecord
_LOCK_TYPE = WorkspaceEconomicLock
_ENSURE_DURABLE_FILE = ensure_durable_file
_ENSURE_DURABLE_FILE_CODE = getattr(_ENSURE_DURABLE_FILE, "__code__", None)

_JSON_DUMPS = json.dumps
_HASHLIB_SHA256 = hashlib.sha256
_LEDGER_INTERNAL_METHOD_WITNESSES = tuple(
    (
        name,
        JsonlDecisionLedger.__dict__[name],
        getattr(
            getattr(
                JsonlDecisionLedger.__dict__[name],
                "__func__",
                JsonlDecisionLedger.__dict__[name],
            ),
            "__code__",
            None,
        ),
    )
    for name in (
        "_require_utf8_text",
        "_validate_json_value",
        "_canonical_record",
        "_validate_record",
        "_json_object_without_duplicate_keys",
        "_reject_non_finite_json",
        "_append_validated",
        "_verify_bytes",
        "verified_snapshot",
        "verify_integrity",
    )
)
_DECISION_RECORD_METHOD_WITNESSES = tuple(
    (
        name,
        DecisionRecord.__dict__[name],
        getattr(DecisionRecord.__dict__[name], "__code__", None),
    )
    for name in ("__post_init__", "to_dict")
)


class ProductProposalRiskEvaluationPrecommitError(RuntimeError):
    """The target/scientific evaluation join cannot be product-resolved safely."""


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_proposal_risk_evaluation_precommit_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(
            instance,
            "_proposal_risk_evaluation_precommit_capability",
            token,
        )

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskEvaluationPrecommit:
    """Durable join between one exact proposal target and fixed-N science.

    This is deliberately not a ruin result. It freezes the exact proposal target
    together with the already product-owned statistical protocol/data/cohort contract
    that a downstream counterfactual evaluator must consume. Positive risk authority
    remains closed until later target-specific execution/result authority exists.
    """

    workspace_instance_id: str
    decision_id: str
    target_sha256: str
    target_decision_ts: str
    bankroll_id: str
    currency: str
    base_portfolio_sha256: str
    risk_policy_sha256: str
    candidate_vector_sha256: str
    evaluated_stakes: tuple[Decimal, ...]
    experiment_id: str
    research_protocol_id: str
    protocol_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    membership_design_sha256: str
    sampling_manifest_sha256: str
    planned_member_ids: tuple[str, ...]
    membership_protocol_record_sha256: str
    membership_dataset_record_sha256: str
    membership_causal_cutoff: str
    membership_precommitted_at: str
    membership_outcome_reveal_after: str
    confidence_level: Decimal
    ruin_threshold: Decimal
    scientific_risk_target_scope: str
    proposal_evaluation_scope: str
    scientific_initial_capital_state_sha256: str
    scientific_stake_policy_sha256: str
    scientific_precommit_sha256: str
    binding_sha256: str
    _proposal_risk_evaluation_precommit_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalRiskEvaluationPrecommit":
        raise TypeError(
            "ProductProposalRiskEvaluationPrecommit is product-issued; use "
            "issue_product_proposal_risk_evaluation_precommit or "
            "resolve_product_proposal_risk_evaluation_precommit"
        )

    @property
    def binding_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def proposal_target_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def scientific_precommit_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def scientific_preoutcome_chronology_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def proposal_target_bound_after_scientific_precommit(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def scientific_precommit_proves_proposal_execution_scope(self) -> bool:
        return False

    @property
    def proposal_target_counterfactual_execution_proven(self) -> bool:
        return False

    @property
    def risk_upper_bound_for_target(self) -> bool:
        return False

    @property
    def grants_ticket_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


_BINDING_TYPE = ProductProposalRiskEvaluationPrecommit
_BINDING_FIELDS = (
    "workspace_instance_id",
    "decision_id",
    "target_sha256",
    "target_decision_ts",
    "bankroll_id",
    "currency",
    "base_portfolio_sha256",
    "risk_policy_sha256",
    "candidate_vector_sha256",
    "evaluated_stakes",
    "experiment_id",
    "research_protocol_id",
    "protocol_sha256",
    "dataset_snapshot_id",
    "dataset_manifest_sha256",
    "membership_design_sha256",
    "sampling_manifest_sha256",
    "planned_member_ids",
    "membership_protocol_record_sha256",
    "membership_dataset_record_sha256",
    "membership_causal_cutoff",
    "membership_precommitted_at",
    "membership_outcome_reveal_after",
    "confidence_level",
    "ruin_threshold",
    "scientific_risk_target_scope",
    "proposal_evaluation_scope",
    "scientific_initial_capital_state_sha256",
    "scientific_stake_policy_sha256",
    "scientific_precommit_sha256",
    "binding_sha256",
)
_BINDING_FIELDS_CANONICAL = _BINDING_FIELDS


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 64
        or text != text.lower()
        or any(ch not in _HEX for ch in text)
    ):
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _decimal_text(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} must be a finite exact Decimal"
        )
    if value.is_zero():
        return "0"
    parts = value.as_tuple()
    exponent = int(parts.exponent)
    digits = len(parts.digits)
    sign = 1 if parts.sign else 0
    if exponent >= 0:
        length = sign + digits + exponent
    elif digits + exponent > 0:
        length = sign + digits + 1
    else:
        length = sign + 2 - exponent
    if length > _MAX_DECIMAL_TEXT:
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} exceeds supported canonical size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProposalRiskEvaluationPrecommitError(
            f"{name} must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _workspace_path(value: object) -> Path:
    if type(value) is not Path:
        raise ProductProposalRiskEvaluationPrecommitError(
            "workspace must be an exact pathlib.Path"
        )
    expanded = value.expanduser()
    if not expanded.is_absolute():
        raise ProductProposalRiskEvaluationPrecommitError(
            "workspace must be an absolute path"
        )
    return expanded


def _require_dispatch() -> None:
    if (
        _PROTOCOL_CONSTANTS != _PROTOCOL_CONSTANTS_EXPECTED
        or _SCHEMA != _PROTOCOL_CONSTANTS_EXPECTED[0]
        or _ACTION != _PROTOCOL_CONSTANTS_EXPECTED[1]
        or _AGENT != _PROTOCOL_CONSTANTS_EXPECTED[2]
        or _ACTION_PREFIX != _PROTOCOL_CONSTANTS_EXPECTED[3]
        or _PROPOSAL_EVALUATION_SCOPE != _PROTOCOL_CONSTANTS_EXPECTED[4]
        or ProductProposalRiskTarget is not _TARGET_TYPE
        or resolve_product_proposal_risk_target is not _TARGET_RESOLVER
        or getattr(_TARGET_RESOLVER, "__code__", None) is not _TARGET_RESOLVER_CODE
        or ProductFixedNRiskEvaluationPrecommitAuthority is not _SCIENCE_TYPE
        or resolve_product_fixed_n_risk_evaluation_precommit is not _SCIENCE_RESOLVER
        or getattr(_SCIENCE_RESOLVER, "__code__", None) is not _SCIENCE_RESOLVER_CODE
        or ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or EconomicGoalContract is not _GOAL_TYPE
        or EconomicGoalStore is not _GOAL_STORE_TYPE
        or EconomicGoalStore.load is not _GOAL_LOAD
        or getattr(_GOAL_LOAD, "__code__", None) is not _GOAL_LOAD_CODE
        or PaperRiskPolicy is not _POLICY_TYPE
        or JsonlDecisionLedger is not _LEDGER_TYPE
        or JsonlDecisionLedger.append_economic is not _LEDGER_APPEND
        or getattr(_LEDGER_APPEND, "__code__", None) is not _LEDGER_APPEND_CODE
        or JsonlDecisionLedger.verified_economic_decision_for_material_action
        is not _LEDGER_RESOLVE
        or getattr(_LEDGER_RESOLVE, "__code__", None) is not _LEDGER_RESOLVE_CODE
        or JsonlDecisionLedger.verify_integrity is not _LEDGER_VERIFY
        or getattr(_LEDGER_VERIFY, "__code__", None) is not _LEDGER_VERIFY_CODE
        or DecisionRecord is not _RECORD_TYPE
        or WorkspaceEconomicLock is not _LOCK_TYPE
        or ensure_durable_file is not _ENSURE_DURABLE_FILE
        or getattr(_ENSURE_DURABLE_FILE, "__code__", None)
        is not _ENSURE_DURABLE_FILE_CODE
        or _JSON_DUMPS is not json.dumps
        or _HASHLIB_SHA256 is not hashlib.sha256
        or ProductProposalRiskEvaluationPrecommit is not _BINDING_TYPE
        or _BINDING_FIELDS is not _BINDING_FIELDS_CANONICAL
    ):
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit authority dispatch changed"
        )

    for name, expected, code in _LEDGER_INTERNAL_METHOD_WITNESSES:
        current = JsonlDecisionLedger.__dict__.get(name)
        current_function = getattr(current, "__func__", current)
        if (
            current is not expected
            or getattr(current_function, "__code__", None) is not code
        ):
            raise ProductProposalRiskEvaluationPrecommitError(
                "proposal risk evaluation precommit Decision Ledger authority "
                f"changed: {name}"
            )
    for name, expected, code in _DECISION_RECORD_METHOD_WITNESSES:
        current = DecisionRecord.__dict__.get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
        ):
            raise ProductProposalRiskEvaluationPrecommitError(
                "proposal risk evaluation precommit DecisionRecord authority "
                f"changed: {name}"
            )

    helper_witnesses = globals().get(
        "_PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES"
    )
    expected_helper_witnesses = globals().get(
        "_PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES_EXPECTED"
    )
    if helper_witnesses is not expected_helper_witnesses:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit internal helper witness root changed"
        )
    if type(helper_witnesses) is not tuple:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit internal helper witness set is unavailable"
        )
    for name, expected, code in expected_helper_witnesses:
        current = globals().get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
        ):
            raise ProductProposalRiskEvaluationPrecommitError(
                "proposal risk evaluation precommit internal helper authority "
                f"changed: {name}"
            )


_REQUIRE_DISPATCH_ORIGINAL = _require_dispatch


def _current_economic_state(
    workspace: Path,
) -> tuple[EconomicGoalContract, PaperRiskPolicy, JsonlDecisionLedger]:
    try:
        goal = _GOAL_LOAD(_GOAL_STORE_TYPE(workspace))
    except (EconomicGoalContractError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskEvaluationPrecommitError(
            "current product EconomicGoal cannot be resolved"
        ) from exc
    if type(goal) is not _GOAL_TYPE:
        raise ProductProposalRiskEvaluationPrecommitError(
            "current product EconomicGoal has non-canonical type"
        )
    policy = _POLICY_TYPE(economic_goal=goal)
    if type(policy) is not _POLICY_TYPE:
        raise ProductProposalRiskEvaluationPrecommitError(
            "current PaperRiskPolicy has non-canonical type"
        )
    ledger = _LEDGER_TYPE(workspace / "decisions.jsonl")
    try:
        _ENSURE_DURABLE_FILE(ledger.path)
        _LEDGER_VERIFY(ledger)
    except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskEvaluationPrecommitError(
            "canonical Decision Ledger cannot be verified"
        ) from exc
    return goal, policy, ledger


def _resolve_inputs(
    *,
    workspace: Path,
    target_sha256: str,
    membership: ResolvedFixedNRiskMembership,
    registry_path: str | Path,
    sampling_manifest_json: str,
    authority_root: str | Path | None,
) -> tuple[ProductProposalRiskTarget, ProductFixedNRiskEvaluationPrecommitAuthority]:
    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError("membership must be exact ResolvedFixedNRiskMembership")
    try:
        target = _TARGET_RESOLVER(workspace, target_sha256)
        science = _SCIENCE_RESOLVER(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
    except (
        ProductProposalRiskTargetError,
        ProductRiskEvaluationPrecommitError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal target/scientific precommit cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if type(target) is not _TARGET_TYPE or type(science) is not _SCIENCE_TYPE:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal target/scientific precommit returned unsupported type"
        )
    if (
        target.proposal_target_identity_proven is not True
        or target.proposal_target_counterfactual_execution_proven is not False
        or target.risk_upper_bound_for_target is not False
        or target.grants_ticket_authority is not False
        or target.grants_real_money_authority is not False
        or science.product_preoutcome_chronology_proven is not True
        or science.statistical_policy_precommitted is not True
        or science.target_execution_proven is not False
        or science.iid_qualified is not False
        or science.risk_upper_bound_issued is not False
        or science.grants_real_money_authority is not False
    ):
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal target/scientific precommit truth flags are inconsistent"
        )
    if target.workspace_instance_id != science.workspace_instance_id:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal target and scientific precommit belong to different workspace instances"
        )
    return target, science


def _material(
    target: ProductProposalRiskTarget,
    science: ProductFixedNRiskEvaluationPrecommitAuthority,
    membership: ResolvedFixedNRiskMembership,
) -> dict[str, object]:
    if (
        type(target) is not _TARGET_TYPE
        or type(science) is not _SCIENCE_TYPE
        or type(membership) is not _MEMBERSHIP_TYPE
    ):
        raise TypeError("binding inputs must be exact product authority values")
    target_time = _instant(target.decision_ts, "target_decision_ts")
    precommitted_at = _instant(
        membership.precommitted_at, "membership_precommitted_at"
    )
    _instant(
        membership.outcome_reveal_after, "membership_outcome_reveal_after"
    )
    if target_time < precommitted_at:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal target predates the product scientific membership precommit"
        )
    return {
        "schema": _SCHEMA,
        "workspace_instance_id": _text(
            target.workspace_instance_id,
            "workspace_instance_id",
            max_length=256,
        ),
        "target_sha256": _sha(target.target_sha256, "target_sha256"),
        "target_decision_ts": _text(target.decision_ts, "target_decision_ts"),
        "bankroll_id": _text(target.bankroll_id, "bankroll_id"),
        "currency": _text(target.currency, "currency"),
        "base_portfolio_sha256": _sha(
            target.base_portfolio_sha256, "base_portfolio_sha256"
        ),
        "risk_policy_sha256": _sha(target.risk_policy_sha256, "risk_policy_sha256"),
        "candidate_vector_sha256": _sha(
            target.candidate_vector_sha256, "candidate_vector_sha256"
        ),
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake")
            for value in target.evaluated_stakes
        ],
        "experiment_id": _text(science.experiment_id, "experiment_id"),
        "research_protocol_id": _text(
            science.research_protocol_id, "research_protocol_id"
        ),
        "protocol_sha256": _sha(science.protocol_sha256, "protocol_sha256"),
        "dataset_snapshot_id": _text(
            science.dataset_snapshot_id, "dataset_snapshot_id"
        ),
        "dataset_manifest_sha256": _sha(
            science.dataset_manifest_sha256, "dataset_manifest_sha256"
        ),
        "membership_design_sha256": _sha(
            science.membership_design_sha256, "membership_design_sha256"
        ),
        "sampling_manifest_sha256": _sha(
            science.sampling_manifest_sha256, "sampling_manifest_sha256"
        ),
        "planned_member_ids": [
            _text(value, "planned_member_id") for value in science.planned_member_ids
        ],
        "membership_protocol_record_sha256": _sha(
            membership.protocol_record_sha256,
            "membership_protocol_record_sha256",
        ),
        "membership_dataset_record_sha256": _sha(
            membership.dataset_record_sha256,
            "membership_dataset_record_sha256",
        ),
        "membership_causal_cutoff": _text(
            membership.causal_cutoff, "membership_causal_cutoff"
        ),
        "membership_precommitted_at": _text(
            membership.precommitted_at, "membership_precommitted_at"
        ),
        "membership_outcome_reveal_after": _text(
            membership.outcome_reveal_after, "membership_outcome_reveal_after"
        ),
        "confidence_level": _decimal_text(
            science.confidence_level, "confidence_level"
        ),
        "ruin_threshold": _decimal_text(science.ruin_threshold, "ruin_threshold"),
        "scientific_risk_target_scope": _text(
            science.risk_target_scope, "scientific_risk_target_scope"
        ),
        "proposal_evaluation_scope": _PROPOSAL_EVALUATION_SCOPE,
        "scientific_initial_capital_state_sha256": _sha(
            science.initial_capital_state_sha256,
            "scientific_initial_capital_state_sha256",
        ),
        "scientific_stake_policy_sha256": _sha(
            science.stake_policy_sha256, "scientific_stake_policy_sha256"
        ),
        "scientific_precommit_sha256": _sha(
            science.authority_sha256, "scientific_precommit_sha256"
        ),
        "proposal_target_identity_proven": True,
        "scientific_precommit_proven": True,
        "scientific_preoutcome_chronology_proven": True,
        "proposal_target_bound_after_scientific_precommit": True,
        "scientific_precommit_proves_proposal_execution_scope": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_ticket_authority": False,
        "grants_real_money_authority": False,
    }


def _build(
    *,
    record: DecisionRecord,
    target: ProductProposalRiskTarget,
    science: ProductFixedNRiskEvaluationPrecommitAuthority,
    membership: ResolvedFixedNRiskMembership,
    expected_binding_sha256: str,
    _bind=_BIND_IDENTITY,
) -> ProductProposalRiskEvaluationPrecommit:
    if type(record) is not _RECORD_TYPE:
        raise ProductProposalRiskEvaluationPrecommitError(
            "persisted evaluation precommit record has unsupported type"
        )
    binding_sha256 = _sha(expected_binding_sha256, "expected_binding_sha256")
    action_id = _ACTION_PREFIX + binding_sha256
    payload = record.payload
    expected_fields = set(_material(target, science, membership))
    expected_fields.update(
        {
            "binding_sha256",
            MATERIAL_ACTION_ID_PAYLOAD_KEY,
            ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
            RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
        }
    )
    if set(payload) != expected_fields:
        raise ProductProposalRiskEvaluationPrecommitError(
            "persisted evaluation precommit payload schema is invalid"
        )
    if (
        record.action != _ACTION
        or record.agent != _AGENT
        or record.replay_run_id != action_id
        or record.context_hash != binding_sha256
        or record.decision_id != action_id
        or payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY) != action_id
        or payload.get("binding_sha256") != binding_sha256
    ):
        raise ProductProposalRiskEvaluationPrecommitError(
            "persisted evaluation precommit envelope is invalid"
        )
    expected_material = _material(target, science, membership)
    for key, value in expected_material.items():
        if payload.get(key) != value:
            raise ProductProposalRiskEvaluationPrecommitError(
                f"persisted evaluation precommit field {key!r} does not re-resolve"
            )
    if _digest(expected_material) != binding_sha256:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit digest does not re-derive"
        )

    instance = object.__new__(_BINDING_TYPE)
    values = {
        "workspace_instance_id": target.workspace_instance_id,
        "decision_id": action_id,
        "target_sha256": target.target_sha256,
        "target_decision_ts": target.decision_ts,
        "bankroll_id": target.bankroll_id,
        "currency": target.currency,
        "base_portfolio_sha256": target.base_portfolio_sha256,
        "risk_policy_sha256": target.risk_policy_sha256,
        "candidate_vector_sha256": target.candidate_vector_sha256,
        "evaluated_stakes": tuple(target.evaluated_stakes),
        "experiment_id": science.experiment_id,
        "research_protocol_id": science.research_protocol_id,
        "protocol_sha256": science.protocol_sha256,
        "dataset_snapshot_id": science.dataset_snapshot_id,
        "dataset_manifest_sha256": science.dataset_manifest_sha256,
        "membership_design_sha256": science.membership_design_sha256,
        "sampling_manifest_sha256": science.sampling_manifest_sha256,
        "planned_member_ids": tuple(science.planned_member_ids),
        "membership_protocol_record_sha256": membership.protocol_record_sha256,
        "membership_dataset_record_sha256": membership.dataset_record_sha256,
        "membership_causal_cutoff": membership.causal_cutoff,
        "membership_precommitted_at": membership.precommitted_at,
        "membership_outcome_reveal_after": membership.outcome_reveal_after,
        "confidence_level": science.confidence_level,
        "ruin_threshold": science.ruin_threshold,
        "scientific_risk_target_scope": science.risk_target_scope,
        "proposal_evaluation_scope": _PROPOSAL_EVALUATION_SCOPE,
        "scientific_initial_capital_state_sha256": (
            science.initial_capital_state_sha256
        ),
        "scientific_stake_policy_sha256": science.stake_policy_sha256,
        "scientific_precommit_sha256": science.authority_sha256,
        "binding_sha256": binding_sha256,
    }
    for name in _BINDING_FIELDS_CANONICAL:
        object.__setattr__(instance, name, values[name])
    _bind(instance)
    return instance


def issue_product_proposal_risk_evaluation_precommit(
    workspace: Path,
    *,
    target_sha256: str,
    membership: ResolvedFixedNRiskMembership,
    registry_path: str | Path,
    sampling_manifest_json: str,
    authority_root: str | Path | None = None,
) -> ProductProposalRiskEvaluationPrecommit:
    """Persist one exact target/science join in the canonical Decision Ledger."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    target_sha256 = _sha(target_sha256, "target_sha256")
    target, science = _resolve_inputs(
        workspace=workspace,
        target_sha256=target_sha256,
        membership=membership,
        registry_path=registry_path,
        sampling_manifest_json=sampling_manifest_json,
        authority_root=authority_root,
    )
    material = _material(target, science, membership)
    binding_sha256 = _digest(material)
    action_id = _ACTION_PREFIX + binding_sha256

    # Target/scientific resolvers acquire their own locks. Never nest those inside
    # WorkspaceEconomicLock. A concurrent transition can at worst leave one
    # non-authorizing orphan audit record; mandatory post-append re-resolution below
    # then fails closed.
    with _LOCK_TYPE(workspace):
        _require_dispatch()
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            existing = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskEvaluationPrecommitError(
                "evaluation precommit Decision Ledger re-resolution failed"
            ) from exc
        if existing is None:
            payload = {
                **material,
                "binding_sha256": binding_sha256,
                MATERIAL_ACTION_ID_PAYLOAD_KEY: action_id,
            }
            record = _RECORD_TYPE(
                replay_run_id=action_id,
                agent=_AGENT,
                observed_ts=target.decision_ts,
                action=_ACTION,
                payload=payload,
                context_hash=binding_sha256,
                decision_id=action_id,
            )
            try:
                _LEDGER_APPEND(
                    ledger,
                    record,
                    goal,
                    risk_policy=policy,
                )
                existing = _LEDGER_RESOLVE(
                    ledger,
                    action_id,
                    goal,
                    risk_policy=policy,
                )
            except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
                raise ProductProposalRiskEvaluationPrecommitError(
                    "evaluation precommit Decision Ledger append failed"
                ) from exc
        if existing is None:
            raise ProductProposalRiskEvaluationPrecommitError(
                "evaluation precommit append did not re-resolve"
            )

    fresh_target, fresh_science = _resolve_inputs(
        workspace=workspace,
        target_sha256=target_sha256,
        membership=membership,
        registry_path=registry_path,
        sampling_manifest_json=sampling_manifest_json,
        authority_root=authority_root,
    )
    if _digest(_material(fresh_target, fresh_science, membership)) != binding_sha256:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal/scientific authority changed during evaluation precommit"
        )

    with _LOCK_TYPE(workspace):
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            final_record = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskEvaluationPrecommitError(
                "evaluation precommit final re-resolution failed"
            ) from exc
        if final_record is None:
            raise ProductProposalRiskEvaluationPrecommitError(
                "evaluation precommit disappeared after durable append"
            )
        return _build(
            record=final_record,
            target=fresh_target,
            science=fresh_science,
            membership=membership,
            expected_binding_sha256=binding_sha256,
        )


def resolve_product_proposal_risk_evaluation_precommit(
    workspace: Path,
    *,
    binding_sha256: str,
    target_sha256: str,
    membership: ResolvedFixedNRiskMembership,
    registry_path: str | Path,
    sampling_manifest_json: str,
    authority_root: str | Path | None = None,
) -> ProductProposalRiskEvaluationPrecommit:
    """Re-resolve one durable target/science join from current canonical roots."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskEvaluationPrecommitError(
            "proposal risk evaluation precommit dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    binding_sha256 = _sha(binding_sha256, "binding_sha256")
    target_sha256 = _sha(target_sha256, "target_sha256")
    target, science = _resolve_inputs(
        workspace=workspace,
        target_sha256=target_sha256,
        membership=membership,
        registry_path=registry_path,
        sampling_manifest_json=sampling_manifest_json,
        authority_root=authority_root,
    )
    if _digest(_material(target, science, membership)) != binding_sha256:
        raise ProductProposalRiskEvaluationPrecommitError(
            "requested evaluation precommit differs from current target/science roots"
        )
    with _LOCK_TYPE(workspace):
        _require_dispatch()
        goal, policy, ledger = _current_economic_state(workspace)
        action_id = _ACTION_PREFIX + binding_sha256
        try:
            record = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskEvaluationPrecommitError(
                "evaluation precommit Decision Ledger re-resolution failed"
            ) from exc
        if record is None:
            raise ProductProposalRiskEvaluationPrecommitError(
                "evaluation precommit is missing from the canonical Decision Ledger"
            )
        return _build(
            record=record,
            target=target,
            science=science,
            membership=membership,
            expected_binding_sha256=binding_sha256,
        )


_PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
    )
    for name in (
        "_workspace_path",
        "_current_economic_state",
        "_resolve_inputs",
        "_material",
        "_build",
        "_text",
        "_sha",
        "_decimal_text",
        "_instant",
        "_canonical_json",
        "_digest",
    )
)
_PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES_EXPECTED = (
    _PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES
)

# Keep the issuance token out of the mutable module namespace. Public truth
# properties and the canonical builder retain only closure/default references.
del _IDENTITY_PROVEN
del _BIND_IDENTITY


__all__ = [
    "ProductProposalRiskEvaluationPrecommit",
    "ProductProposalRiskEvaluationPrecommitError",
    "issue_product_proposal_risk_evaluation_precommit",
    "resolve_product_proposal_risk_evaluation_precommit",
]
