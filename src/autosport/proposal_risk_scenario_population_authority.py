from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .domain import utc_now_iso
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .economic_goal_store import EconomicGoalContractError, EconomicGoalStore
from .integrity import ensure_durable_file
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
    RecoveryDisposition,
)
from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from .proposal_target_terminal_population_authority import (
    ProductProposalTargetTerminalPopulation,
)
from .risk import PaperRiskPolicy
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA = "autosport.proposal-risk-scenario-population-precommit.v1"
_ACTION = "PROPOSAL_RISK_SCENARIO_POPULATION_PRECOMMIT"
_AGENT = "autosport.proposal-risk-scenario-population-authority.v1"
_BINDING_SCOPE = "EXACT_PROPOSAL_TARGET_MEMBER_SCENARIO_MAPPING_V1"
_ACTION_PREFIX = "proposal-risk-scenario-population-v1:"
_TARGET_AUTHORITY_DOMAIN = "proposal-risk-target-precommit-v1"
_TARGET_WORKSPACE_KEY = "workspace-binding-v1"
_TARGET_CHAIN_KEY = "current-target-chain-v1"
_POPULATION_AUTHORITY_DOMAIN = "proposal-risk-scenario-population-precommit-v1"
_POPULATION_AUTHORITY_PREFIX = "population-v1:"
_PARENT_PRECOMMIT_SCHEMA = "autosport.proposal-risk-evaluation-precommit.v1"
_PARENT_PRECOMMIT_ACTION = "PROPOSAL_RISK_EVALUATION_PRECOMMIT"
_PARENT_PRECOMMIT_AGENT = "autosport.proposal-risk-evaluation-precommit-authority.v1"
_TARGET_SCHEMA = "autosport.proposal-risk-target-precommit.v1"
_TARGET_ACTION = "PROPOSAL_RISK_TARGET_PRECOMMIT"
_TARGET_AGENT = "autosport.proposal-risk-target-authority.v1"
_TARGET_ACTION_PREFIX = "target-v1:"
_TERMINAL_PARENT_SCHEMA = "autosport.proposal-target-terminal-population-precommit.v1"
_TERMINAL_PARENT_ACTION = "PROPOSAL_TARGET_TERMINAL_POPULATION_PRECOMMIT"
_TERMINAL_PARENT_AGENT = "autosport.proposal-target-terminal-population-authority.v1"
_TERMINAL_PARENT_ACTION_PREFIX = "proposal-target-terminal-population-v1:"
_HEX = frozenset("0123456789abcdef")
_MAX_DECIMAL_TEXT = 256
_PATH_TYPE = type(Path("."))

_PRECOMMIT_TYPE = ProductProposalRiskEvaluationPrecommit
_GOAL_STORE_TYPE = EconomicGoalStore
_POLICY_TYPE = PaperRiskPolicy
_LEDGER_TYPE = JsonlDecisionLedger
_RECORD_TYPE = DecisionRecord
_LOCK_TYPE = WorkspaceEconomicLock
_AUTHORITY_TYPE = MonotonicWorkspaceAuthority
_GOAL_LOAD = EconomicGoalStore.load
_LEDGER_APPEND = JsonlDecisionLedger.append_economic
_LEDGER_RESOLVE = JsonlDecisionLedger.verified_economic_decision_for_material_action
_LEDGER_VERIFY = JsonlDecisionLedger.verify_integrity
_ENSURE_DURABLE_FILE = ensure_durable_file
_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256
_UTC_NOW_ISO = utc_now_iso
_TERMINAL_POPULATION_TYPE = ProductProposalTargetTerminalPopulation
_TERMINAL_POPULATION_PROPERTY_NAMES = (
    "population_identity_proven",
    "provider_terminal_authority_proven",
    "terminal_space_exhaustive",
    "probability_model_bound",
    "scientific_precommit_bound",
    "iid_member_mapping_proven",
    "proposal_target_counterfactual_execution_proven",
    "risk_upper_bound_for_target",
    "grants_ticket_authority",
    "grants_real_money_authority",
)
_TERMINAL_POPULATION_PROPERTY_WITNESSES = tuple(
    (
        name,
        _TERMINAL_POPULATION_TYPE.__dict__[name],
        _TERMINAL_POPULATION_TYPE.__dict__[name].fget,
        getattr(_TERMINAL_POPULATION_TYPE.__dict__[name].fget, "__code__", None),
    )
    for name in _TERMINAL_POPULATION_PROPERTY_NAMES
)
_TERMINAL_POPULATION_PROPERTY_WITNESSES_EXPECTED = (
    _TERMINAL_POPULATION_PROPERTY_WITNESSES
)


class ProductProposalRiskScenarioPopulationError(RuntimeError):
    """An exact-target scenario population cannot be precommitted safely."""


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskScenarioPopulationError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProductProposalRiskScenarioPopulationError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskScenarioPopulationError(
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
        raise ProductProposalRiskScenarioPopulationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductProposalRiskScenarioPopulationError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProposalRiskScenarioPopulationError(
            f"{name} must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _decimal_text(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskScenarioPopulationError(
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
        raise ProductProposalRiskScenarioPopulationError(
            f"{name} exceeds supported canonical decimal size"
        )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_json(value: object) -> bytes:
    if (
        _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS_EXPECTED, "__code__", None) is not _JSON_DUMPS_CODE
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority dispatch changed"
        )
    try:
        return _JSON_DUMPS(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    if (
        _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority dispatch changed"
        )
    return _HASHLIB_SHA256(_canonical_json(value)).hexdigest()


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE or not value.is_absolute():
        raise ProductProposalRiskScenarioPopulationError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != value or not value.is_dir() or value.is_symlink():
        raise ProductProposalRiskScenarioPopulationError(
            "workspace must be the canonical non-symlink directory path"
        )
    return value


@dataclass(frozen=True, slots=True)
class CounterfactualScenarioMemberBinding:
    """Caller-declared member→scenario mapping identity.

    The upstream terminal state population is product/provider verified separately.
    This object identifies only the fixed-N member's selected scenario and mapping
    material; it never proves that the selection belongs to, or was sampled from,
    that population.
    """

    member_id: str
    scenario_id: str
    mapping_sha256: str

    def __post_init__(self) -> None:
        _text(self.member_id, "member_id")
        _text(self.scenario_id, "scenario_id")
        _sha(self.mapping_sha256, "mapping_sha256")


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return getattr(instance, "_scenario_population_capability", None) is token

    def bind(instance: object) -> None:
        object.__setattr__(instance, "_scenario_population_capability", token)

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskScenarioPopulation:
    """Durable exact-member scenario identity precommit.

    This proves only that one exact scenario identity vector was durably fixed for
    the exact proposal/scientific precommit. Scenario references remain caller-
    declared identifiers, so this object does not prove lawful/product-owned source
    provenance, terminal mapping truth, execution, a target risk bound, ticket
    authority, or money-moving authority.
    """

    workspace_instance_id: str
    decision_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    evaluated_stakes: tuple[Decimal, ...]
    planned_member_ids: tuple[str, ...]
    terminal_population_sha256: str
    terminal_market_group_sha256s: tuple[str, ...]
    terminal_market_count: int
    terminal_state_count: int
    terminal_space_exact: bool
    bound_at: str
    member_scenario_ids: tuple[str, ...]
    member_mapping_sha256s: tuple[str, ...]
    binding_scope: str
    population_sha256: str
    _scenario_population_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalRiskScenarioPopulation":
        raise TypeError(
            "ProductProposalRiskScenarioPopulation is product-issued; use "
            "issue_product_proposal_risk_scenario_population or "
            "resolve_product_proposal_risk_scenario_population"
        )

    @property
    def population_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def fixed_n_member_mapping_complete(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def provider_terminal_population_proven(
        self,
        _proven=_IDENTITY_PROVEN,
    ) -> bool:
        return _proven(self)

    @property
    def product_scenario_source_provenance_proven(self) -> bool:
        return False

    @property
    def terminal_mapping_proven(self) -> bool:
        return False

    @property
    def scenario_execution_proven(self) -> bool:
        return False

    @property
    def proposal_target_counterfactual_execution_proven(self) -> bool:
        return False

    @property
    def risk_upper_bound_for_target(self) -> bool:
        return False

    @property
    def grants_risk_approval_authority(self) -> bool:
        return False

    @property
    def grants_ticket_authority(self) -> bool:
        return False

    @property
    def grants_broker_execution_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False

    @property
    def grants_state_mutation_authority(self) -> bool:
        return False


_RESULT_TYPE = ProductProposalRiskScenarioPopulation
_RESULT_TYPE_EXPECTED = _RESULT_TYPE
_RESULT_AUTHORITY_PROPERTY_NAMES = (
    "population_identity_proven",
    "fixed_n_member_mapping_complete",
    "provider_terminal_population_proven",
    "product_scenario_source_provenance_proven",
    "terminal_mapping_proven",
    "scenario_execution_proven",
    "proposal_target_counterfactual_execution_proven",
    "risk_upper_bound_for_target",
    "grants_risk_approval_authority",
    "grants_ticket_authority",
    "grants_broker_execution_authority",
    "grants_real_money_authority",
    "grants_state_mutation_authority",
)
_RESULT_AUTHORITY_PROPERTY_WITNESSES = tuple(
    (
        name,
        _RESULT_TYPE.__dict__[name],
        _RESULT_TYPE.__dict__[name].fget,
        getattr(_RESULT_TYPE.__dict__[name].fget, "__code__", None),
    )
    for name in _RESULT_AUTHORITY_PROPERTY_NAMES
)
_RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED = (
    _RESULT_AUTHORITY_PROPERTY_WITNESSES
)


_RESULT_FIELDS = (
    "workspace_instance_id",
    "decision_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "evaluated_stakes",
    "planned_member_ids",
    "terminal_population_sha256",
    "terminal_market_group_sha256s",
    "terminal_market_count",
    "terminal_state_count",
    "terminal_space_exact",
    "bound_at",
    "member_scenario_ids",
    "member_mapping_sha256s",
    "binding_scope",
    "population_sha256",
)


_DISPATCH_ROOT = (
    _PRECOMMIT_TYPE,
    _GOAL_STORE_TYPE,
    _POLICY_TYPE,
    _LEDGER_TYPE,
    _RECORD_TYPE,
    _LOCK_TYPE,
    _AUTHORITY_TYPE,
    _GOAL_LOAD,
    _LEDGER_APPEND,
    _LEDGER_RESOLVE,
    _LEDGER_VERIFY,
    _ENSURE_DURABLE_FILE,
    _JSON_DUMPS,
    _HASHLIB_SHA256,
    _UTC_NOW_ISO,
    _TERMINAL_POPULATION_TYPE,
)


def _require_dispatch(
    _expected=_DISPATCH_ROOT,
) -> None:
    current = (
        ProductProposalRiskEvaluationPrecommit,
        EconomicGoalStore,
        PaperRiskPolicy,
        JsonlDecisionLedger,
        DecisionRecord,
        WorkspaceEconomicLock,
        MonotonicWorkspaceAuthority,
        EconomicGoalStore.load,
        JsonlDecisionLedger.append_economic,
        JsonlDecisionLedger.verified_economic_decision_for_material_action,
        JsonlDecisionLedger.verify_integrity,
        ensure_durable_file,
        json.dumps,
        hashlib.sha256,
        utc_now_iso,
        ProductProposalTargetTerminalPopulation,
    )
    aliases = (
        _PRECOMMIT_TYPE,
        _GOAL_STORE_TYPE,
        _POLICY_TYPE,
        _LEDGER_TYPE,
        _RECORD_TYPE,
        _LOCK_TYPE,
        _AUTHORITY_TYPE,
        _GOAL_LOAD,
        _LEDGER_APPEND,
        _LEDGER_RESOLVE,
        _LEDGER_VERIFY,
        _ENSURE_DURABLE_FILE,
        _JSON_DUMPS,
        _HASHLIB_SHA256,
        _UTC_NOW_ISO,
        _TERMINAL_POPULATION_TYPE,
    )
    if (
        type(_expected) is not tuple
        or len(current) != len(_expected)
        or any(actual is not expected for actual, expected in zip(current, _expected))
        or any(actual is not expected for actual, expected in zip(aliases, _expected))
        or _TERMINAL_POPULATION_PROPERTY_WITNESSES
        is not _TERMINAL_POPULATION_PROPERTY_WITNESSES_EXPECTED
        or any(
            ProductProposalTargetTerminalPopulation.__dict__.get(name)
            is not descriptor
            or getattr(
                ProductProposalTargetTerminalPopulation.__dict__.get(name),
                "fget",
                None,
            )
            is not getter
            or getattr(getter, "__code__", None) is not code
            for name, descriptor, getter, code
            in _TERMINAL_POPULATION_PROPERTY_WITNESSES_EXPECTED
        )
        or ProductProposalRiskScenarioPopulation is not _RESULT_TYPE_EXPECTED
        or _RESULT_TYPE is not _RESULT_TYPE_EXPECTED
        or _RESULT_AUTHORITY_PROPERTY_WITNESSES
        is not _RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
        or any(
            ProductProposalRiskScenarioPopulation.__dict__.get(name)
            is not descriptor
            or getattr(
                ProductProposalRiskScenarioPopulation.__dict__.get(name),
                "fget",
                None,
            )
            is not getter
            or getattr(getter, "__code__", None) is not code
            for name, descriptor, getter, code
            in _RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
        )
        or _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS_EXPECTED, "__code__", None) is not _JSON_DUMPS_CODE
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority dispatch changed"
        )

    helper_witnesses = globals().get("_HELPER_WITNESSES")
    expected_helper_witnesses = globals().get("_HELPER_WITNESSES_EXPECTED")
    if (
        helper_witnesses is not expected_helper_witnesses
        or type(helper_witnesses) is not tuple
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority dispatch changed"
        )
    for name, expected_function, code in expected_helper_witnesses:
        current_function = globals().get(name)
        if (
            current_function is not expected_function
            or getattr(current_function, "__code__", None) is not code
        ):
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population authority dispatch changed"
            )


_REQUIRE_DISPATCH_ORIGINAL = _require_dispatch


def _require_precommit(
    precommit: object,
) -> ProductProposalRiskEvaluationPrecommit:
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductProposalRiskScenarioPopulationError(
            "precommit must be exact ProductProposalRiskEvaluationPrecommit"
        )
    if (
        precommit.binding_identity_proven is not True
        or precommit.proposal_target_identity_proven is not True
        or precommit.scientific_precommit_proven is not True
        or precommit.scientific_preoutcome_chronology_proven is not True
        or precommit.proposal_target_counterfactual_execution_proven is not False
        or precommit.risk_upper_bound_for_target is not False
        or precommit.grants_ticket_authority is not False
        or precommit.grants_real_money_authority is not False
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "proposal risk evaluation precommit truth boundary is inconsistent"
        )
    return precommit


def _require_terminal_population(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: object,
) -> ProductProposalTargetTerminalPopulation:
    if type(terminal_population) is not _TERMINAL_POPULATION_TYPE:
        raise ProductProposalRiskScenarioPopulationError(
            "terminal_population must be exact ProductProposalTargetTerminalPopulation"
        )
    if (
        terminal_population.population_identity_proven is not True
        or terminal_population.provider_terminal_authority_proven is not True
        or terminal_population.terminal_space_exhaustive is not True
        or terminal_population.probability_model_bound is not False
        or terminal_population.scientific_precommit_bound is not False
        or terminal_population.iid_member_mapping_proven is not False
        or terminal_population.proposal_target_counterfactual_execution_proven
        is not False
        or terminal_population.risk_upper_bound_for_target is not False
        or terminal_population.grants_ticket_authority is not False
        or terminal_population.grants_real_money_authority is not False
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "terminal population truth boundary is inconsistent"
        )
    if (
        terminal_population.workspace_instance_id != precommit.workspace_instance_id
        or terminal_population.target_sha256 != precommit.target_sha256
        or terminal_population.candidate_vector_sha256
        != precommit.candidate_vector_sha256
        or terminal_population.target_decision_ts != precommit.target_decision_ts
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "terminal population does not bind the exact proposal target"
        )
    _sha(terminal_population.population_sha256, "terminal_population_sha256")
    return terminal_population


def _validate_members(
    precommit: ProductProposalRiskEvaluationPrecommit,
    members: object,
) -> tuple[CounterfactualScenarioMemberBinding, ...]:
    if type(members) is not tuple or not members:
        raise ProductProposalRiskScenarioPopulationError(
            "members must be a non-empty exact tuple"
        )
    if len(members) != len(precommit.planned_member_ids):
        raise ProductProposalRiskScenarioPopulationError(
            "member scenario mapping must contain the exact fixed-N cohort"
        )
    validated: list[CounterfactualScenarioMemberBinding] = []
    for index, member in enumerate(members):
        if type(member) is not CounterfactualScenarioMemberBinding:
            raise ProductProposalRiskScenarioPopulationError(
                f"members[{index}] must be exact CounterfactualScenarioMemberBinding"
            )
        validated.append(member)
    result = tuple(validated)
    member_ids = tuple(member.member_id for member in result)
    if member_ids != precommit.planned_member_ids:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario member order must exactly match the precommitted fixed-N cohort"
        )
    if len(set(member_ids)) != len(member_ids):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario member ids must be unique"
        )
    # The scientific sampler is IID with replacement. Repeated scenario or mapping
    # identities remain valid multiplicity; this layer freezes them but does not
    # claim a product-owned draw law.
    return result


def _population_material(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    members: tuple[CounterfactualScenarioMemberBinding, ...],
    *,
    bound_at: str,
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "workspace_instance_id": _text(
            precommit.workspace_instance_id,
            "workspace_instance_id",
            max_length=256,
        ),
        "precommit_binding_sha256": _sha(
            precommit.binding_sha256,
            "precommit_binding_sha256",
        ),
        "target_sha256": _sha(precommit.target_sha256, "target_sha256"),
        "candidate_vector_sha256": _sha(
            precommit.candidate_vector_sha256,
            "candidate_vector_sha256",
        ),
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake")
            for value in precommit.evaluated_stakes
        ],
        "planned_member_ids": list(precommit.planned_member_ids),
        "membership_design_sha256": _sha(
            precommit.membership_design_sha256,
            "membership_design_sha256",
        ),
        "sampling_manifest_sha256": _sha(
            precommit.sampling_manifest_sha256,
            "sampling_manifest_sha256",
        ),
        "membership_causal_cutoff": _text(
            precommit.membership_causal_cutoff,
            "membership_causal_cutoff",
        ),
        "scientific_precommit_sha256": _sha(
            precommit.scientific_precommit_sha256,
            "scientific_precommit_sha256",
        ),
        "terminal_population_sha256": _sha(
            terminal_population.population_sha256,
            "terminal_population_sha256",
        ),
        "terminal_market_group_sha256s": list(
            terminal_population.market_group_sha256s
        ),
        "terminal_market_count": terminal_population.terminal_market_count,
        "terminal_state_count": terminal_population.terminal_state_count,
        "terminal_space_exact": terminal_population.terminal_space_exact,
        "bound_at": _text(bound_at, "bound_at"),
        "binding_scope": _BINDING_SCOPE,
        "members": [
            {
                "member_id": member.member_id,
                "scenario_id": member.scenario_id,
                "mapping_sha256": member.mapping_sha256,
            }
            for member in members
        ],
        "provider_terminal_population_proven": True,
        "product_scenario_source_provenance_proven": False,
        "terminal_mapping_proven": False,
        "scenario_execution_proven": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_ticket_authority": False,
        "grants_real_money_authority": False,
    }


def _current_economic_state(
    workspace: Path,
) -> tuple[object, PaperRiskPolicy, JsonlDecisionLedger]:
    try:
        goal = _GOAL_LOAD(_GOAL_STORE_TYPE(workspace))
    except (EconomicGoalContractError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "current product EconomicGoal cannot be resolved"
        ) from exc
    policy = _POLICY_TYPE(economic_goal=goal)
    ledger = _LEDGER_TYPE(workspace / "decisions.jsonl")
    try:
        _ENSURE_DURABLE_FILE(ledger.path)
        _LEDGER_VERIFY(ledger)
    except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "canonical Decision Ledger cannot be verified"
        ) from exc
    return goal, policy, ledger


def _require_parent_ledger_roots(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    ledger: JsonlDecisionLedger,
    goal: object,
    policy: PaperRiskPolicy,
) -> None:
    terminal_action_id = _TERMINAL_PARENT_ACTION_PREFIX + precommit.target_sha256
    try:
        parent = _LEDGER_RESOLVE(
            ledger,
            precommit.decision_id,
            goal,
            risk_policy=policy,
        )
        target_action_id = _TARGET_ACTION_PREFIX + precommit.target_sha256
        target = _LEDGER_RESOLVE(
            ledger,
            target_action_id,
            goal,
            risk_policy=policy,
        )
        terminal = _LEDGER_RESOLVE(
            ledger,
            terminal_action_id,
            goal,
            risk_policy=policy,
        )
    except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "proposal scenario population parent ledger roots cannot be re-resolved"
        ) from exc
    if parent is None:
        raise ProductProposalRiskScenarioPopulationError(
            "proposal risk evaluation precommit is missing from the canonical Decision Ledger"
        )
    if (
        parent.decision_id != precommit.decision_id
        or parent.replay_run_id != precommit.decision_id
        or parent.action != _PARENT_PRECOMMIT_ACTION
        or parent.agent != _PARENT_PRECOMMIT_AGENT
        or parent.context_hash != precommit.binding_sha256
        or parent.payload.get("schema") != _PARENT_PRECOMMIT_SCHEMA
        or parent.payload.get("binding_sha256") != precommit.binding_sha256
        or parent.payload.get("target_sha256") != precommit.target_sha256
        or parent.payload.get("candidate_vector_sha256")
        != precommit.candidate_vector_sha256
        or tuple(parent.payload.get("planned_member_ids", ()))
        != precommit.planned_member_ids
        or parent.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
        != precommit.decision_id
        or parent.payload.get("proposal_target_counterfactual_execution_proven")
        is not False
        or parent.payload.get("risk_upper_bound_for_target") is not False
        or parent.payload.get("grants_ticket_authority") is not False
        or parent.payload.get("grants_real_money_authority") is not False
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "proposal risk evaluation precommit ledger identity changed"
        )
    if target is None:
        raise ProductProposalRiskScenarioPopulationError(
            "proposal risk target is missing from the canonical Decision Ledger"
        )
    if (
        target.decision_id != target_action_id
        or target.replay_run_id != target_action_id
        or target.action != _TARGET_ACTION
        or target.agent != _TARGET_AGENT
        or target.context_hash != precommit.target_sha256
        or target.payload.get("schema") != _TARGET_SCHEMA
        or target.payload.get("target_sha256") != precommit.target_sha256
        or target.payload.get("workspace_instance_id")
        != precommit.workspace_instance_id
        or target.payload.get("candidate_vector_sha256")
        != precommit.candidate_vector_sha256
        or target.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
        != target_action_id
        or target.payload.get("proposal_target_counterfactual_execution_proven")
        is not False
        or target.payload.get("risk_upper_bound_for_target") is not False
        or target.payload.get("grants_ticket_authority") is not False
        or target.payload.get("grants_real_money_authority") is not False
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "proposal risk target ledger identity changed"
        )
    if terminal is None:
        raise ProductProposalRiskScenarioPopulationError(
            "provider terminal population is missing from the canonical Decision Ledger"
        )
    if (
        terminal_population.decision_id != terminal_action_id
        or terminal.decision_id != terminal_action_id
        or terminal.replay_run_id != terminal_action_id
        or terminal.action != _TERMINAL_PARENT_ACTION
        or terminal.agent != _TERMINAL_PARENT_AGENT
        or terminal.context_hash != terminal_population.population_sha256
        or terminal.payload.get("schema") != _TERMINAL_PARENT_SCHEMA
        or terminal.payload.get("population_sha256")
        != terminal_population.population_sha256
        or terminal.payload.get("workspace_instance_id")
        != precommit.workspace_instance_id
        or terminal.payload.get("target_sha256") != precommit.target_sha256
        or terminal.payload.get("candidate_vector_sha256")
        != precommit.candidate_vector_sha256
        or tuple(terminal.payload.get("market_group_sha256s", ()))
        != terminal_population.market_group_sha256s
        or terminal.payload.get("terminal_market_count")
        != terminal_population.terminal_market_count
        or terminal.payload.get("terminal_state_count")
        != terminal_population.terminal_state_count
        or terminal.payload.get("terminal_space_exact")
        is not terminal_population.terminal_space_exact
        or terminal.payload.get("terminal_space_exhaustive") is not True
        or terminal.payload.get("probability_model_bound") is not False
        or terminal.payload.get("scientific_precommit_bound") is not False
        or terminal.payload.get("iid_member_mapping_proven") is not False
        or terminal.payload.get("proposal_target_counterfactual_execution_proven")
        is not False
        or terminal.payload.get("risk_upper_bound_for_target") is not False
        or terminal.payload.get("grants_ticket_authority") is not False
        or terminal.payload.get("grants_real_money_authority") is not False
        or terminal.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
        != terminal_action_id
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "provider terminal population ledger identity changed"
        )


def _require_current_precommit_economics(
    precommit: ProductProposalRiskEvaluationPrecommit,
    goal: object,
    policy: PaperRiskPolicy,
) -> None:
    if policy.provenance_sha256 != precommit.risk_policy_sha256:
        raise ProductProposalRiskScenarioPopulationError(
            "current PaperRiskPolicy changed after proposal risk precommit"
        )
    if getattr(goal, "bankroll_id", None) != precommit.bankroll_id:
        raise ProductProposalRiskScenarioPopulationError(
            "current bankroll changed after proposal risk precommit"
        )
    if getattr(goal, "currency", None) != precommit.currency:
        raise ProductProposalRiskScenarioPopulationError(
            "current currency changed after proposal risk precommit"
        )


def _require_current_target_authority(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
) -> str:
    try:
        workspace_authority = _AUTHORITY_TYPE(
            workspace=workspace,
            domain=_TARGET_AUTHORITY_DOMAIN,
            key=_TARGET_WORKSPACE_KEY,
        )
        workspace_instance_id = workspace_authority.workspace_instance_id
        if workspace_instance_id != precommit.workspace_instance_id:
            raise ProductProposalRiskScenarioPopulationError(
                "proposal risk precommit belongs to a different workspace instance"
            )
        target_authority = _AUTHORITY_TYPE(
            workspace=workspace,
            workspace_instance_id=workspace_instance_id,
            domain=_TARGET_AUTHORITY_DOMAIN,
            key=_TARGET_CHAIN_KEY,
        )
        history = target_authority.read_history()
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "proposal target independent authority is unavailable"
        ) from exc
    if not history or history[-1].phase is AuthorityPhase.PREPARE:
        raise ProductProposalRiskScenarioPopulationError(
            "proposal target authority is not at a stable committed tip"
        )
    latest_commit = next(
        (
            record
            for record in reversed(history)
            if record.phase is AuthorityPhase.COMMIT
        ),
        None,
    )
    if (
        latest_commit is None
        or latest_commit.intended_state_sha256 != precommit.target_sha256
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "proposal risk precommit target is no longer the current target"
        )
    return workspace_instance_id


def _population_authority(
    workspace: Path,
    workspace_instance_id: str,
    binding_sha256: str,
) -> MonotonicWorkspaceAuthority:
    try:
        return _AUTHORITY_TYPE(
            workspace=workspace,
            workspace_instance_id=workspace_instance_id,
            domain=_POPULATION_AUTHORITY_DOMAIN,
            key=_POPULATION_AUTHORITY_PREFIX + binding_sha256,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population independent authority is unavailable"
        ) from exc


def _authority_tip(
    authority: MonotonicWorkspaceAuthority,
) -> tuple[str | None, object | None]:
    try:
        history = authority.read_history()
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority history is invalid"
        ) from exc
    if not history:
        return None, None
    pending = history[-1] if history[-1].phase is AuthorityPhase.PREPARE else None
    latest_commit = next(
        (
            record
            for record in reversed(history)
            if record.phase is AuthorityPhase.COMMIT
        ),
        None,
    )
    return (
        None if latest_commit is None else latest_commit.intended_state_sha256,
        pending,
    )


def _record_values(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    record: DecisionRecord,
) -> dict[str, object]:
    payload = record.payload
    required = {
        "schema",
        "workspace_instance_id",
        "precommit_binding_sha256",
        "target_sha256",
        "candidate_vector_sha256",
        "evaluated_stakes",
        "planned_member_ids",
        "membership_design_sha256",
        "sampling_manifest_sha256",
        "membership_causal_cutoff",
        "scientific_precommit_sha256",
        "terminal_population_sha256",
        "terminal_market_group_sha256s",
        "terminal_market_count",
        "terminal_state_count",
        "terminal_space_exact",
        "bound_at",
        "binding_scope",
        "members",
        "provider_terminal_population_proven",
        "product_scenario_source_provenance_proven",
        "terminal_mapping_proven",
        "scenario_execution_proven",
        "proposal_target_counterfactual_execution_proven",
        "risk_upper_bound_for_target",
        "grants_ticket_authority",
        "grants_real_money_authority",
        MATERIAL_ACTION_ID_PAYLOAD_KEY,
        "economic_goal_provenance",
        "risk_policy_provenance",
    }
    if set(payload) != required:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population Decision Ledger payload schema changed"
        )
    if payload["schema"] != _SCHEMA or payload["binding_scope"] != _BINDING_SCOPE:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population protocol identity changed"
        )
    if payload["provider_terminal_population_proven"] is not True:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population lost provider terminal-population proof"
        )
    for name in (
        "product_scenario_source_provenance_proven",
        "terminal_mapping_proven",
        "scenario_execution_proven",
        "proposal_target_counterfactual_execution_proven",
        "risk_upper_bound_for_target",
        "grants_ticket_authority",
        "grants_real_money_authority",
    ):
        if payload[name] is not False:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population record contains forbidden positive authority"
            )
    if (
        payload["workspace_instance_id"] != precommit.workspace_instance_id
        or payload["precommit_binding_sha256"] != precommit.binding_sha256
        or payload["target_sha256"] != precommit.target_sha256
        or payload["candidate_vector_sha256"] != precommit.candidate_vector_sha256
        or payload["membership_design_sha256"] != precommit.membership_design_sha256
        or payload["sampling_manifest_sha256"] != precommit.sampling_manifest_sha256
        or payload["membership_causal_cutoff"] != precommit.membership_causal_cutoff
        or payload["scientific_precommit_sha256"]
        != precommit.scientific_precommit_sha256
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population scientific binding changed"
        )
    if (
        payload["terminal_population_sha256"]
        != terminal_population.population_sha256
        or tuple(payload["terminal_market_group_sha256s"])
        != terminal_population.market_group_sha256s
        or payload["terminal_market_count"]
        != terminal_population.terminal_market_count
        or payload["terminal_state_count"]
        != terminal_population.terminal_state_count
        or payload["terminal_space_exact"]
        is not terminal_population.terminal_space_exact
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population provider terminal binding changed"
        )
    expected_stakes = [
        _decimal_text(value, "evaluated_stake") for value in precommit.evaluated_stakes
    ]
    if list(payload["evaluated_stakes"]) != expected_stakes:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population stake vector changed"
        )
    if tuple(payload["planned_member_ids"]) != precommit.planned_member_ids:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population fixed-N membership changed"
        )

    raw_members = payload["members"]
    if not isinstance(raw_members, (tuple, list)) or not raw_members:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population members are missing"
        )
    expected_member_keys = {"member_id", "scenario_id", "mapping_sha256"}
    members: list[CounterfactualScenarioMemberBinding] = []
    for index, raw in enumerate(raw_members):
        if not hasattr(raw, "keys") or set(raw) != expected_member_keys:
            raise ProductProposalRiskScenarioPopulationError(
                f"scenario population member {index} schema changed"
            )
        members.append(
            CounterfactualScenarioMemberBinding(
                member_id=raw["member_id"],
                scenario_id=raw["scenario_id"],
                mapping_sha256=raw["mapping_sha256"],
            )
        )
    bound_at = payload["bound_at"]
    if _instant(bound_at, "bound_at") < _instant(
        precommit.target_decision_ts, "target_decision_ts"
    ):
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population durable timestamp predates the proposal target"
        )
    validated = _validate_members(precommit, tuple(members))
    material = _population_material(
        precommit,
        terminal_population,
        validated,
        bound_at=bound_at,
    )
    return {
        "members": validated,
        "bound_at": bound_at,
        "population_sha256": _digest(material),
    }


def _recover_population_authority(
    authority: MonotonicWorkspaceAuthority,
    *,
    ledger_record: DecisionRecord | None,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
) -> str | None:
    current, pending = _authority_tip(authority)
    if pending is None:
        return current
    pending_sha = pending.intended_state_sha256
    matches = (
        ledger_record is not None
        and ledger_record.context_hash == pending_sha
        and ledger_record.payload.get("precommit_binding_sha256")
        == precommit.binding_sha256
        and ledger_record.payload.get("terminal_population_sha256")
        == terminal_population.population_sha256
        and ledger_record.payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY)
        == _ACTION_PREFIX + precommit.binding_sha256
    )
    if matches:
        matches = (
            _record_values(
                precommit,
                terminal_population,
                ledger_record,
            )["population_sha256"]
            == pending_sha
        )
    try:
        recovery = authority.recover(
            observed_state_sha256=(
                pending_sha if matches else pending.previous_committed_state_sha256
            ),
            tx_id=pending.tx_id,
            semantic_binding_sha256=pending.semantic_binding_sha256,
        )
    except MonotonicWorkspaceAuthorityError as exc:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority crash recovery failed"
        ) from exc
    if matches:
        if recovery.disposition not in {
            RecoveryDisposition.COMMITTED_PREPARE,
            RecoveryDisposition.CURRENT,
        }:
            raise ProductProposalRiskScenarioPopulationError(
                "durable scenario population did not commit pending authority"
            )
        return pending_sha
    if recovery.disposition not in {
        RecoveryDisposition.ABORTED_PREPARE,
        RecoveryDisposition.CURRENT,
    }:
        raise ProductProposalRiskScenarioPopulationError(
            "missing scenario population append did not abort pending authority"
        )
    current, pending = _authority_tip(authority)
    if pending is not None:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population authority remains pending after recovery"
        )
    return current


def _require_authority_committed(
    authority: MonotonicWorkspaceAuthority,
    expected_sha256: str,
) -> None:
    current, pending = _authority_tip(authority)
    if pending is not None or current != expected_sha256:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population independent authority is not committed"
        )


def _mint(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    record: DecisionRecord,
    values: dict[str, object],
    *,
    _bind_identity=_BIND_IDENTITY,
) -> ProductProposalRiskScenarioPopulation:
    members = values["members"]
    instance = object.__new__(_RESULT_TYPE)
    result = {
        "workspace_instance_id": precommit.workspace_instance_id,
        "decision_id": record.decision_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "evaluated_stakes": precommit.evaluated_stakes,
        "planned_member_ids": precommit.planned_member_ids,
        "terminal_population_sha256": terminal_population.population_sha256,
        "terminal_market_group_sha256s": terminal_population.market_group_sha256s,
        "terminal_market_count": terminal_population.terminal_market_count,
        "terminal_state_count": terminal_population.terminal_state_count,
        "terminal_space_exact": terminal_population.terminal_space_exact,
        "bound_at": values["bound_at"],
        "member_scenario_ids": tuple(member.scenario_id for member in members),
        "member_mapping_sha256s": tuple(member.mapping_sha256 for member in members),
        "binding_scope": _BINDING_SCOPE,
        "population_sha256": values["population_sha256"],
    }
    for name in _RESULT_FIELDS:
        object.__setattr__(instance, name, result[name])
    _bind_identity(instance)
    return instance


_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
    )
    for name in (
        "_text",
        "_sha",
        "_instant",
        "_decimal_text",
        "_canonical_json",
        "_digest",
        "_workspace_path",
        "_require_precommit",
        "_require_terminal_population",
        "_validate_members",
        "_population_material",
        "_current_economic_state",
        "_require_parent_ledger_roots",
        "_require_current_precommit_economics",
        "_require_current_target_authority",
        "_population_authority",
        "_authority_tip",
        "_record_values",
        "_recover_population_authority",
        "_require_authority_committed",
        "_mint",
    )
)
_HELPER_WITNESSES_EXPECTED = _HELPER_WITNESSES


def issue_product_proposal_risk_scenario_population(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    members: tuple[CounterfactualScenarioMemberBinding, ...],
) -> ProductProposalRiskScenarioPopulation:
    """Durably bind one fixed-N member→scenario vector to verified terminal space."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    precommit = _require_precommit(precommit)
    terminal_population = _require_terminal_population(precommit, terminal_population)
    workspace = _workspace_path(workspace)
    validated = _validate_members(precommit, members)
    action_id = _ACTION_PREFIX + precommit.binding_sha256

    with _LOCK_TYPE(workspace):
        workspace_instance_id = _require_current_target_authority(workspace, precommit)
        goal, policy, ledger = _current_economic_state(workspace)
        _require_current_precommit_economics(precommit, goal, policy)
        _require_parent_ledger_roots(
            precommit,
            terminal_population,
            ledger,
            goal,
            policy,
        )
        authority = _population_authority(
            workspace, workspace_instance_id, precommit.binding_sha256
        )
        try:
            existing = _LEDGER_RESOLVE(
                ledger, action_id, goal, risk_policy=policy
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population Decision Ledger re-resolution failed"
            ) from exc

        committed = _recover_population_authority(
            authority,
            ledger_record=existing,
            precommit=precommit,
            terminal_population=terminal_population,
        )
        if committed is not None:
            if existing is None:
                raise ProductProposalRiskScenarioPopulationError(
                    "scenario population authority exists but ledger record is missing"
                )
            values = _record_values(
                precommit,
                terminal_population,
                existing,
            )
            requested_sha256 = _digest(
                _population_material(
                    precommit,
                    terminal_population,
                    validated,
                    bound_at=values["bound_at"],
                )
            )
            if (
                committed != values["population_sha256"]
                or committed != requested_sha256
                or existing.context_hash != committed
            ):
                raise ProductProposalRiskScenarioPopulationError(
                    "a different scenario population is already bound to this precommit"
                )
            _require_authority_committed(authority, committed)
            return _mint(
                precommit,
                terminal_population,
                existing,
                values,
            )
        if existing is not None:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population ledger record exists without independent authority"
            )

        bound_at = _UTC_NOW_ISO()
        if _instant(bound_at, "bound_at") < _instant(
            precommit.target_decision_ts, "target_decision_ts"
        ):
            raise ProductProposalRiskScenarioPopulationError(
                "product clock predates the proposal target"
            )
        material = _population_material(
            precommit,
            terminal_population,
            validated,
            bound_at=bound_at,
        )
        population_sha256 = _digest(material)

        try:
            history = authority.read_history()
            tx_id = f"{precommit.binding_sha256}:{len(history) + 1}"
            authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=None,
                intended_state_sha256=population_sha256,
                semantic_binding_sha256=precommit.binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population authority PREPARE failed"
            ) from exc

        payload = {**material, MATERIAL_ACTION_ID_PAYLOAD_KEY: action_id}
        try:
            record = _RECORD_TYPE(
                replay_run_id=action_id,
                agent=_AGENT,
                observed_ts=bound_at,
                action=_ACTION,
                payload=payload,
                context_hash=population_sha256,
                decision_id=action_id,
                recorded_at=bound_at,
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            _LEDGER_APPEND(ledger, record, goal, risk_policy=policy)
            existing = _LEDGER_RESOLVE(
                ledger, action_id, goal, risk_policy=policy
            )
        except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
            try:
                crossed = _LEDGER_RESOLVE(
                    ledger, action_id, goal, risk_policy=policy
                )
            except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError):
                crossed = None
            try:
                _recover_population_authority(
                    authority,
                    ledger_record=crossed,
                    precommit=precommit,
                    terminal_population=terminal_population,
                )
            except ProductProposalRiskScenarioPopulationError:
                pass
            if crossed is not None:
                raise ProductProposalRiskScenarioPopulationError(
                    "scenario population append crossed durability boundary; "
                    "exact recovery is required"
                ) from exc
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population Decision Ledger append failed"
            ) from exc

        if existing is None:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population append did not re-resolve"
            )
        values = _record_values(
            precommit,
            terminal_population,
            existing,
        )
        if (
            values["population_sha256"] != population_sha256
            or existing.context_hash != population_sha256
        ):
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population changed across its durability boundary"
            )
        try:
            authority.commit(
                tx_id=tx_id,
                observed_state_sha256=population_sha256,
                semantic_binding_sha256=precommit.binding_sha256,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population authority COMMIT failed; exact recovery is required"
            ) from exc
        _require_authority_committed(authority, population_sha256)
        return _mint(
            precommit,
            terminal_population,
            existing,
            values,
        )


def resolve_product_proposal_risk_scenario_population(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
) -> ProductProposalRiskScenarioPopulation:
    """Re-resolve the durable mapping against the same verified terminal population."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskScenarioPopulationError(
            "scenario population dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    precommit = _require_precommit(precommit)
    terminal_population = _require_terminal_population(precommit, terminal_population)
    workspace = _workspace_path(workspace)
    action_id = _ACTION_PREFIX + precommit.binding_sha256
    with _LOCK_TYPE(workspace):
        workspace_instance_id = _require_current_target_authority(workspace, precommit)
        goal, policy, ledger = _current_economic_state(workspace)
        _require_current_precommit_economics(precommit, goal, policy)
        _require_parent_ledger_roots(
            precommit,
            terminal_population,
            ledger,
            goal,
            policy,
        )
        authority = _population_authority(
            workspace, workspace_instance_id, precommit.binding_sha256
        )
        try:
            record = _LEDGER_RESOLVE(
                ledger, action_id, goal, risk_policy=policy
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population Decision Ledger re-resolution failed"
            ) from exc
        committed = _recover_population_authority(
            authority,
            ledger_record=record,
            precommit=precommit,
            terminal_population=terminal_population,
        )
        if committed is None:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population independent authority is missing"
            )
        if record is None:
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population is missing from the canonical Decision Ledger"
            )
        values = _record_values(
            precommit,
            terminal_population,
            record,
        )
        if (
            committed != values["population_sha256"]
            or record.context_hash != committed
        ):
            raise ProductProposalRiskScenarioPopulationError(
                "scenario population durable authorities disagree"
            )
        _require_authority_committed(authority, committed)
        return _mint(
            precommit,
            terminal_population,
            record,
            values,
        )


del _IDENTITY_PROVEN
del _BIND_IDENTITY


__all__ = [
    "CounterfactualScenarioMemberBinding",
    "ProductProposalRiskScenarioPopulation",
    "ProductProposalRiskScenarioPopulationError",
    "issue_product_proposal_risk_scenario_population",
    "resolve_product_proposal_risk_scenario_population",
]
