from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .market_outcomes import (
    MarketSettlementOutcomeAuthority,
    MarketTerminalState,
)
from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from .proposal_risk_scenario_population_authority import (
    ProductProposalRiskScenarioPopulation,
    ProductProposalRiskScenarioPopulationError,
    resolve_product_proposal_risk_scenario_population,
)
from .proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    resolve_product_proposal_risk_target,
)
from .proposal_target_terminal_population_authority import (
    ProductProposalTargetTerminalPopulation,
    ProductProposalTargetTerminalPopulationError,
    resolve_product_proposal_target_terminal_population,
)


_SCENARIO_ID_SCHEMA = "autosport.proposal-target-terminal-state-vector-id.v1"
_MAPPING_SCHEMA = "autosport.proposal-target-terminal-state-mapping.v1"
_RESULT_SCHEMA = "autosport.proposal-risk-terminal-state-mapping-result.v1"
_SCENARIO_ID_PREFIX = "terminal-vector-v1:"
_HEX = frozenset("0123456789abcdef")
_PATH_TYPE = type(Path("."))
_MAX_SCENARIO_ID_LENGTH = 512

_PRECOMMIT_TYPE = ProductProposalRiskEvaluationPrecommit
_TARGET_TYPE = ProductProposalRiskTarget
_TERMINAL_POPULATION_TYPE = ProductProposalTargetTerminalPopulation
_SCENARIO_POPULATION_TYPE = ProductProposalRiskScenarioPopulation
_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_TERMINAL_STATE_TYPE = MarketTerminalState

_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_TERMINAL_RESOLVER = resolve_product_proposal_target_terminal_population
_TERMINAL_RESOLVER_CODE = getattr(_TERMINAL_RESOLVER, "__code__", None)
_SCENARIO_RESOLVER = resolve_product_proposal_risk_scenario_population
_SCENARIO_RESOLVER_CODE = getattr(_SCENARIO_RESOLVER, "__code__", None)

_AUTHORITY_SHA_GETTER = MarketSettlementOutcomeAuthority.authority_sha256.fget
_AUTHORITY_SHA_GETTER_CODE = getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
_AUTHORITY_TERMINAL_STATES_GETTER = MarketSettlementOutcomeAuthority.terminal_states.fget
_AUTHORITY_TERMINAL_STATES_GETTER_CODE = getattr(
    _AUTHORITY_TERMINAL_STATES_GETTER, "__code__", None
)
_AUTHORITY_TO_DICT = MarketSettlementOutcomeAuthority.to_dict
_AUTHORITY_TO_DICT_CODE = getattr(_AUTHORITY_TO_DICT, "__code__", None)

_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_LOADS = json.loads
_JSON_LOADS_EXPECTED = _JSON_LOADS
_JSON_DECODE_ERROR = json.JSONDecodeError
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256
_B64_ENCODE = base64.urlsafe_b64encode
_B64_ENCODE_EXPECTED = _B64_ENCODE
_B64_DECODE = base64.urlsafe_b64decode
_B64_DECODE_EXPECTED = _B64_DECODE


class ProductProposalRiskTerminalStateMappingError(RuntimeError):
    """A precommitted scenario cannot be mapped to provider terminal states safely."""


def _text(value: object, name: str, *, max_length: int = 512) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 64
        or text != text.lower()
        or any(character not in _HEX for character in text)
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _decimal_text(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be a finite exact Decimal"
        )
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _canonical_bytes(value: object) -> bytes:
    if (
        _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping canonical dispatch changed"
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
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping material is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return _HASHLIB_SHA256(_canonical_bytes(value)).hexdigest()


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal mapping JSON contains duplicate keys"
            )
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise ProductProposalRiskTerminalStateMappingError(
        "terminal mapping JSON contains non-finite numbers"
    )


def _canonical_json_object(value: object, name: str) -> dict[str, object]:
    if (
        _JSON_LOADS is not _JSON_LOADS_EXPECTED
        or json.loads is not _JSON_LOADS_EXPECTED
        or json.JSONDecodeError is not _JSON_DECODE_ERROR
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping JSON dispatch changed"
        )
    if type(value) is not str or not value:
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be canonical JSON text"
        )
    try:
        parsed = _JSON_LOADS(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (_JSON_DECODE_ERROR, UnicodeError) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} is invalid JSON"
        ) from exc
    if type(parsed) is not dict or _canonical_bytes(parsed).decode("utf-8") != value:
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} is not canonical JSON"
        )
    return parsed


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE or not value.is_absolute():
        raise ProductProposalRiskTerminalStateMappingError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != value or not value.is_dir() or value.is_symlink():
        raise ProductProposalRiskTerminalStateMappingError(
            "workspace must be the canonical non-symlink directory path"
        )
    return value


def _require_precommit(
    precommit: object,
) -> ProductProposalRiskEvaluationPrecommit:
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
            "precommit must be exact ProductProposalRiskEvaluationPrecommit"
        )
    if (
        precommit.binding_identity_proven is not True
        or precommit.proposal_target_identity_proven is not True
        or precommit.scientific_precommit_proven is not True
        or precommit.scientific_preoutcome_chronology_proven is not True
        or precommit.proposal_target_bound_after_scientific_precommit is not True
        or precommit.scientific_precommit_proves_proposal_execution_scope is not False
        or precommit.proposal_target_counterfactual_execution_proven is not False
        or precommit.risk_upper_bound_for_target is not False
        or precommit.grants_ticket_authority is not False
        or precommit.grants_real_money_authority is not False
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "proposal risk precommit truth boundary is inconsistent"
        )
    return precommit


def _require_target(
    precommit: ProductProposalRiskEvaluationPrecommit,
    target: object,
) -> ProductProposalRiskTarget:
    if type(target) is not _TARGET_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
            "proposal target resolver returned a non-canonical type"
        )
    if (
        target.proposal_target_identity_proven is not True
        or target.proposal_target_counterfactual_execution_proven is not False
        or target.risk_upper_bound_for_target is not False
        or target.grants_ticket_authority is not False
        or target.grants_real_money_authority is not False
        or target.workspace_instance_id != precommit.workspace_instance_id
        or target.target_sha256 != precommit.target_sha256
        or target.decision_ts != precommit.target_decision_ts
        or target.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or tuple(target.evaluated_stakes) != precommit.evaluated_stakes
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "proposal target no longer matches the exact risk precommit"
        )
    return target


def _require_terminal_population(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: object,
) -> ProductProposalTargetTerminalPopulation:
    if type(terminal_population) is not _TERMINAL_POPULATION_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal population resolver returned a non-canonical type"
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
        or terminal_population.workspace_instance_id != precommit.workspace_instance_id
        or terminal_population.target_sha256 != precommit.target_sha256
        or terminal_population.target_decision_ts != precommit.target_decision_ts
        or terminal_population.candidate_vector_sha256
        != precommit.candidate_vector_sha256
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal population truth boundary does not match the exact precommit"
        )
    return terminal_population


def _require_scenario_population(
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    scenario_population: object,
) -> ProductProposalRiskScenarioPopulation:
    if type(scenario_population) is not _SCENARIO_POPULATION_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario population resolver returned a non-canonical type"
        )
    if (
        scenario_population.population_identity_proven is not True
        or scenario_population.fixed_n_member_mapping_complete is not True
        or scenario_population.provider_terminal_population_proven is not True
        or scenario_population.product_scenario_source_provenance_proven is not False
        or scenario_population.terminal_mapping_proven is not False
        or scenario_population.scenario_execution_proven is not False
        or scenario_population.proposal_target_counterfactual_execution_proven
        is not False
        or scenario_population.risk_upper_bound_for_target is not False
        or scenario_population.grants_risk_approval_authority is not False
        or scenario_population.grants_ticket_authority is not False
        or scenario_population.grants_broker_execution_authority is not False
        or scenario_population.grants_real_money_authority is not False
        or scenario_population.grants_state_mutation_authority is not False
        or scenario_population.workspace_instance_id != precommit.workspace_instance_id
        or scenario_population.precommit_binding_sha256 != precommit.binding_sha256
        or scenario_population.target_sha256 != precommit.target_sha256
        or scenario_population.candidate_vector_sha256
        != precommit.candidate_vector_sha256
        or scenario_population.evaluated_stakes != precommit.evaluated_stakes
        or scenario_population.planned_member_ids != precommit.planned_member_ids
        or scenario_population.terminal_population_sha256
        != terminal_population.population_sha256
        or scenario_population.terminal_market_group_sha256s
        != terminal_population.market_group_sha256s
        or scenario_population.terminal_market_count
        != terminal_population.terminal_market_count
        or scenario_population.terminal_state_count
        != terminal_population.terminal_state_count
        or scenario_population.terminal_space_exact
        is not terminal_population.terminal_space_exact
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario population does not match its exact durable parents"
        )
    return scenario_population


@dataclass(frozen=True, slots=True)
class CanonicalTerminalScenarioBinding:
    """Deterministic strings that may be precommitted by the fixed-N parent layer.

    This value is intentionally non-authorizing. It identifies one exact vector of
    separately provider-verified per-market terminal states and the exact target-leg
    settlement mapping that vector implies.
    """

    terminal_population_sha256: str
    market_group_sha256s: tuple[str, ...]
    market_state_ids: tuple[str, ...]
    scenario_id: str
    state_vector_sha256: str
    mapping_sha256: str

    def __post_init__(self) -> None:
        _sha(self.terminal_population_sha256, "terminal_population_sha256")
        if type(self.market_group_sha256s) is not tuple or not self.market_group_sha256s:
            raise ProductProposalRiskTerminalStateMappingError(
                "market_group_sha256s must be a non-empty exact tuple"
            )
        if type(self.market_state_ids) is not tuple or (
            len(self.market_state_ids) != len(self.market_group_sha256s)
        ):
            raise ProductProposalRiskTerminalStateMappingError(
                "market_state_ids must exactly match the market-group vector"
            )
        for index, value in enumerate(self.market_group_sha256s):
            _sha(value, f"market_group_sha256s[{index}]")
        for index, value in enumerate(self.market_state_ids):
            _text(value, f"market_state_ids[{index}]")
        _text(
            self.scenario_id,
            "scenario_id",
            max_length=_MAX_SCENARIO_ID_LENGTH,
        )
        _sha(self.state_vector_sha256, "state_vector_sha256")
        _sha(self.mapping_sha256, "mapping_sha256")


def _make_mapping_capability():
    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_terminal_mapping_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(instance, "_terminal_mapping_capability", token)

    return proven, bind


_MAPPING_PROVEN, _BIND_MAPPING = _make_mapping_capability()
del _make_mapping_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskTerminalStateMapping:
    """Deterministic positive proof of only terminal settlement mapping.

    The result proves that every fixed-N member's precommitted scenario identifier
    decodes to exact provider-verified per-market terminal states and that its
    mapping digest re-derives from the exact proposal target, stake vector, leg/quote
    material and terminal settlements. It does not prove the scenario was sampled
    from a lawful joint distribution, that the vector is in joint support, that any
    counterfactual ticket was executed, or that risk/ticket/money authority exists.
    """

    workspace_instance_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    terminal_population_sha256: str
    scenario_population_sha256: str
    market_group_sha256s: tuple[str, ...]
    joint_terminal_space_exact: bool
    planned_member_ids: tuple[str, ...]
    member_scenario_ids: tuple[str, ...]
    member_mapping_sha256s: tuple[str, ...]
    member_state_vector_sha256s: tuple[str, ...]
    resolution_sha256: str
    _terminal_mapping_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalRiskTerminalStateMapping":
        raise TypeError(
            "ProductProposalRiskTerminalStateMapping is product-issued; use "
            "resolve_product_proposal_risk_terminal_state_mapping"
        )

    @property
    def mapping_identity_proven(self, _proven=_MAPPING_PROVEN) -> bool:
        return _proven(self)

    @property
    def provider_terminal_population_proven(
        self, _proven=_MAPPING_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def fixed_n_member_mapping_complete(
        self, _proven=_MAPPING_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def per_market_terminal_states_exact(
        self, _proven=_MAPPING_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def terminal_mapping_proven(self, _proven=_MAPPING_PROVEN) -> bool:
        return _proven(self)

    @property
    def product_scenario_source_provenance_proven(self) -> bool:
        return False

    @property
    def iid_member_mapping_proven(self) -> bool:
        return False

    @property
    def joint_scenario_support_proven(self) -> bool:
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


_RESULT_TYPE = ProductProposalRiskTerminalStateMapping
_RESULT_FIELDS = (
    "workspace_instance_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "terminal_population_sha256",
    "scenario_population_sha256",
    "market_group_sha256s",
    "joint_terminal_space_exact",
    "planned_member_ids",
    "member_scenario_ids",
    "member_mapping_sha256s",
    "member_state_vector_sha256s",
    "resolution_sha256",
)


@dataclass(frozen=True, slots=True)
class _TerminalGroupModel:
    market_group_sha256: str
    authority_sha256s: tuple[str, ...]
    terminal_space_exact: bool
    state_by_id: dict[str, MarketTerminalState]


def _authority_map(
    authorities: object,
) -> dict[str, MarketSettlementOutcomeAuthority]:
    if type(authorities) is not tuple or not authorities:
        raise ProductProposalRiskTerminalStateMappingError(
            "authorities must be a non-empty exact tuple"
        )
    result: dict[str, MarketSettlementOutcomeAuthority] = {}
    for index, authority in enumerate(authorities):
        if type(authority) is not _AUTHORITY_TYPE:
            raise ProductProposalRiskTerminalStateMappingError(
                f"authorities[{index}] must be exact MarketSettlementOutcomeAuthority"
            )
        digest = _sha(_AUTHORITY_SHA_GETTER(authority), "market_authority_sha256")
        if digest in result:
            raise ProductProposalRiskTerminalStateMappingError(
                "authorities contain a duplicate market authority digest"
            )
        result[digest] = authority
    return result


def _terminal_groups(
    terminal_population: ProductProposalTargetTerminalPopulation,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> tuple[_TerminalGroupModel, ...]:
    by_sha = _authority_map(authorities)
    if set(by_sha) != set(terminal_population.market_authority_sha256s):
        raise ProductProposalRiskTerminalStateMappingError(
            "verified market authority set differs from the terminal population"
        )
    if len(terminal_population.market_group_json) != len(
        terminal_population.market_group_sha256s
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal population market-group serialization is inconsistent"
        )

    groups: list[_TerminalGroupModel] = []
    seen_authorities: set[str] = set()
    for index, raw in enumerate(terminal_population.market_group_json):
        parsed = _canonical_json_object(raw, f"market_group_json[{index}]")
        required = {
            "market_key",
            "authority_sha256s",
            "terminal_state_count",
            "terminal_space_exact",
            "market_group_sha256",
        }
        if set(parsed) != required:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal population market-group schema changed"
            )
        group_sha = _sha(
            parsed["market_group_sha256"],
            f"market_group_json[{index}].market_group_sha256",
        )
        if group_sha != terminal_population.market_group_sha256s[index]:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal population market-group order/digest changed"
            )
        material_without_sha = {
            key: parsed[key]
            for key in (
                "market_key",
                "authority_sha256s",
                "terminal_state_count",
                "terminal_space_exact",
            )
        }
        if _digest(material_without_sha) != group_sha:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal population market-group digest does not re-derive"
            )
        authority_values = parsed["authority_sha256s"]
        if type(authority_values) is not list or not authority_values:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal population market group has no authorities"
            )
        group_authority_sha256s = tuple(
            _sha(value, "group authority sha256") for value in authority_values
        )
        if tuple(sorted(group_authority_sha256s)) != group_authority_sha256s:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal population market authority order is not canonical"
            )
        if any(value in seen_authorities for value in group_authority_sha256s):
            raise ProductProposalRiskTerminalStateMappingError(
                "market authority appears in more than one terminal group"
            )
        seen_authorities.update(group_authority_sha256s)

        terminal_space_exact = parsed["terminal_space_exact"]
        if type(terminal_space_exact) is not bool:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal market-group exactness is not canonical bool"
            )
        if terminal_space_exact is not True:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal mapping requires exact per-market terminal semantics"
            )

        providers = [by_sha[digest] for digest in group_authority_sha256s]
        baseline_states = _AUTHORITY_TERMINAL_STATES_GETTER(providers[0])
        if type(baseline_states) is not tuple or not baseline_states:
            raise ProductProposalRiskTerminalStateMappingError(
                "verified market authority produced no terminal states"
            )
        baseline_payload = tuple(state.to_dict() for state in baseline_states)
        if len(baseline_states) != parsed["terminal_state_count"]:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal market-group state count changed"
            )
        for provider in providers[1:]:
            states = _AUTHORITY_TERMINAL_STATES_GETTER(provider)
            if tuple(state.to_dict() for state in states) != baseline_payload:
                raise ProductProposalRiskTerminalStateMappingError(
                    "provider authorities disagree on terminal state vectors"
                )
        state_by_id: dict[str, MarketTerminalState] = {}
        for state in baseline_states:
            if type(state) is not _TERMINAL_STATE_TYPE:
                raise ProductProposalRiskTerminalStateMappingError(
                    "terminal authority returned a non-canonical state type"
                )
            state_id = _text(state.state_id, "terminal state_id")
            if state_id in state_by_id:
                raise ProductProposalRiskTerminalStateMappingError(
                    "terminal authority produced duplicate state_id"
                )
            state_by_id[state_id] = state
        groups.append(
            _TerminalGroupModel(
                market_group_sha256=group_sha,
                authority_sha256s=group_authority_sha256s,
                terminal_space_exact=True,
                state_by_id=state_by_id,
            )
        )
    if seen_authorities != set(by_sha):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal market groups do not cover the verified authority set"
        )
    return tuple(groups)


def _leg_key(value: Mapping[str, object]) -> tuple[object, ...]:
    return (
        value.get("event_id"),
        value.get("market_id"),
        value.get("selection_id"),
        value.get("sport"),
        value.get("exchange_side"),
    )


def _state_vector_payload(
    terminal_population: ProductProposalTargetTerminalPopulation,
    groups: tuple[_TerminalGroupModel, ...],
    state_ids: tuple[str, ...],
) -> dict[str, object]:
    if type(state_ids) is not tuple or len(state_ids) != len(groups):
        raise ProductProposalRiskTerminalStateMappingError(
            "state_ids must exactly match the terminal market-group vector"
        )
    states: list[dict[str, str]] = []
    for index, (group, state_id_raw) in enumerate(zip(groups, state_ids)):
        state_id = _text(state_id_raw, f"state_ids[{index}]")
        if state_id not in group.state_by_id:
            raise ProductProposalRiskTerminalStateMappingError(
                f"state_ids[{index}] is not a verified terminal state"
            )
        states.append(
            {
                "market_group_sha256": group.market_group_sha256,
                "state_id": state_id,
            }
        )
    return {
        "schema": _SCENARIO_ID_SCHEMA,
        "terminal_population_sha256": terminal_population.population_sha256,
        "states": states,
    }


def _scenario_id(payload: dict[str, object]) -> str:
    if (
        _B64_ENCODE is not _B64_ENCODE_EXPECTED
        or base64.urlsafe_b64encode is not _B64_ENCODE_EXPECTED
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping base64 dispatch changed"
        )
    encoded = _B64_ENCODE(_canonical_bytes(payload)).decode("ascii").rstrip("=")
    scenario_id = _SCENARIO_ID_PREFIX + encoded
    return _text(
        scenario_id,
        "scenario_id",
        max_length=_MAX_SCENARIO_ID_LENGTH,
    )


def _decode_scenario_id(
    value: object,
    terminal_population: ProductProposalTargetTerminalPopulation,
    groups: tuple[_TerminalGroupModel, ...],
) -> tuple[str, ...]:
    scenario_id = _text(
        value,
        "scenario_id",
        max_length=_MAX_SCENARIO_ID_LENGTH,
    )
    if not scenario_id.startswith(_SCENARIO_ID_PREFIX):
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario_id is not a canonical terminal-vector identifier"
        )
    encoded = scenario_id[len(_SCENARIO_ID_PREFIX) :]
    if not encoded:
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario_id terminal-vector payload is empty"
        )
    if (
        _B64_DECODE is not _B64_DECODE_EXPECTED
        or base64.urlsafe_b64decode is not _B64_DECODE_EXPECTED
        or _B64_ENCODE is not _B64_ENCODE_EXPECTED
        or base64.urlsafe_b64encode is not _B64_ENCODE_EXPECTED
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping base64 dispatch changed"
        )
    try:
        raw = _B64_DECODE(encoded + "=" * (-len(encoded) % 4))
        raw_text = raw.decode("utf-8", errors="strict")
        parsed = _JSON_LOADS(
            raw_text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (ValueError, UnicodeError, _JSON_DECODE_ERROR) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario_id terminal-vector payload is invalid"
        ) from exc
    if (
        _B64_ENCODE(raw).decode("ascii").rstrip("=") != encoded
        or type(parsed) is not dict
        or _canonical_bytes(parsed) != raw
        or set(parsed) != {"schema", "terminal_population_sha256", "states"}
        or parsed.get("schema") != _SCENARIO_ID_SCHEMA
        or parsed.get("terminal_population_sha256")
        != terminal_population.population_sha256
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario_id terminal-vector payload is non-canonical or stale"
        )
    states = parsed.get("states")
    if type(states) is not list or len(states) != len(groups):
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario_id terminal state vector cardinality changed"
        )
    state_ids: list[str] = []
    for index, (raw_state, group) in enumerate(zip(states, groups)):
        if (
            type(raw_state) is not dict
            or set(raw_state) != {"market_group_sha256", "state_id"}
            or raw_state.get("market_group_sha256")
            != group.market_group_sha256
        ):
            raise ProductProposalRiskTerminalStateMappingError(
                "scenario_id terminal market-group binding changed"
            )
        state_id = _text(raw_state.get("state_id"), f"scenario state[{index}]")
        if state_id not in group.state_by_id:
            raise ProductProposalRiskTerminalStateMappingError(
                "scenario_id names an unverified terminal state"
            )
        state_ids.append(state_id)
    return tuple(state_ids)


def _candidate_mapping_material(
    target: ProductProposalRiskTarget,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    groups: tuple[_TerminalGroupModel, ...],
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    state_ids: tuple[str, ...],
) -> list[dict[str, object]]:
    by_sha = _authority_map(authorities)
    authority_to_group: dict[str, int] = {}
    for group_index, group in enumerate(groups):
        for digest in group.authority_sha256s:
            authority_to_group[digest] = group_index

    if (
        len(target.candidate_context_json) != len(target.candidate_sha256s)
        or len(target.candidate_context_json) != len(precommit.evaluated_stakes)
        or len(terminal_population.candidate_market_authority_sha256s)
        != len(target.candidate_context_json)
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "proposal candidate/terminal mapping cardinality changed"
        )

    candidates: list[dict[str, object]] = []
    for candidate_index, raw_context in enumerate(target.candidate_context_json):
        context = _canonical_json_object(
            raw_context,
            f"candidate_context_json[{candidate_index}]",
        )
        expected_context_keys = {
            "legs",
            "quotes",
            "provider_accounts",
            "bankroll_id",
            "currency",
            "measurement_window_start",
            "measurement_window_end",
            "proposal_ts",
        }
        if set(context) != expected_context_keys:
            raise ProductProposalRiskTerminalStateMappingError(
                "proposal candidate context schema changed"
            )
        legs = context["legs"]
        quotes = context["quotes"]
        if type(legs) is not list or not legs or type(quotes) is not list or not quotes:
            raise ProductProposalRiskTerminalStateMappingError(
                "proposal candidate context legs/quotes are invalid"
            )
        persisted_authorities = terminal_population.candidate_market_authority_sha256s[
            candidate_index
        ]
        if (
            type(persisted_authorities) is not tuple
            or len(persisted_authorities) != len(legs)
        ):
            raise ProductProposalRiskTerminalStateMappingError(
                "candidate terminal-authority vector no longer matches its legs"
            )

        quotes_by_key: dict[tuple[object, ...], Mapping[str, object]] = {}
        for quote in quotes:
            if not isinstance(quote, Mapping):
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate quote is not a mapping"
                )
            key = _leg_key(quote)
            if key in quotes_by_key:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate quote identity is duplicated"
                )
            quotes_by_key[key] = quote

        mapped_legs: list[dict[str, object]] = []
        for leg_index, (leg, authority_sha_raw) in enumerate(
            zip(legs, persisted_authorities)
        ):
            if not isinstance(leg, Mapping):
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg is not a mapping"
                )
            authority_sha = _sha(
                authority_sha_raw,
                f"candidate authority[{candidate_index}][{leg_index}]",
            )
            authority = by_sha.get(authority_sha)
            group_index = authority_to_group.get(authority_sha)
            if authority is None or group_index is None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg terminal authority is not reverified"
                )
            quote = quotes_by_key.get(_leg_key(leg))
            if quote is None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg lacks its exact locked quote"
                )
            if leg.get("exchange_side") is not None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "terminal mapping does not support exchange-side settlement semantics"
                )
            selection_id = _text(
                leg.get("selection_id"),
                f"candidate[{candidate_index}].leg[{leg_index}].selection_id",
            )
            if (
                authority.identity.sport != leg.get("sport")
                or authority.identity.event_id != leg.get("event_id")
                or authority.identity.market_id != leg.get("market_id")
                or authority.identity.source_id != quote.get("source_id")
                or authority.identity.market_type.value != quote.get("market_type")
                or selection_id not in authority.selection_ids
            ):
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg no longer matches its persisted provider terminal authority"
                )

            state = groups[group_index].state_by_id[state_ids[group_index]]
            settlement_by_selection = dict(state.settlements)
            result = settlement_by_selection.get(selection_id)
            if result is None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "verified terminal state does not settle the proposal selection"
                )
            mapped_legs.append(
                {
                    "leg_index": leg_index,
                    "leg_sha256": _digest(dict(leg)),
                    "quote_sha256": _digest(dict(quote)),
                    "authority_sha256": authority_sha,
                    "market_group_sha256": groups[
                        group_index
                    ].market_group_sha256,
                    "state_id": state.state_id,
                    "selection_id": selection_id,
                    "settlement_result": result.value,
                }
            )
        candidates.append(
            {
                "candidate_index": candidate_index,
                "candidate_sha256": target.candidate_sha256s[candidate_index],
                "candidate_context_sha256": _digest(context),
                "evaluated_stake": _decimal_text(
                    precommit.evaluated_stakes[candidate_index],
                    f"evaluated_stakes[{candidate_index}]",
                ),
                "legs": mapped_legs,
            }
        )
    return candidates


def _derive_binding_from_parents(
    *,
    precommit: ProductProposalRiskEvaluationPrecommit,
    target: ProductProposalRiskTarget,
    terminal_population: ProductProposalTargetTerminalPopulation,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    groups: tuple[_TerminalGroupModel, ...],
    state_ids: tuple[str, ...],
) -> CanonicalTerminalScenarioBinding:
    state_vector = _state_vector_payload(terminal_population, groups, state_ids)
    scenario_id = _scenario_id(state_vector)
    state_vector_sha256 = _digest(state_vector)
    candidates = _candidate_mapping_material(
        target,
        precommit,
        terminal_population,
        groups,
        authorities,
        state_ids,
    )
    mapping_material = {
        "schema": _MAPPING_SCHEMA,
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake")
            for value in precommit.evaluated_stakes
        ],
        "terminal_population_sha256": terminal_population.population_sha256,
        "state_vector_sha256": state_vector_sha256,
        "state_vector": state_vector["states"],
        "candidates": candidates,
        "terminal_mapping_proven": True,
        "product_scenario_source_provenance_proven": False,
        "iid_member_mapping_proven": False,
        "joint_scenario_support_proven": False,
        "scenario_execution_proven": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_ticket_authority": False,
        "grants_real_money_authority": False,
    }
    return CanonicalTerminalScenarioBinding(
        terminal_population_sha256=terminal_population.population_sha256,
        market_group_sha256s=terminal_population.market_group_sha256s,
        market_state_ids=state_ids,
        scenario_id=scenario_id,
        state_vector_sha256=state_vector_sha256,
        mapping_sha256=_digest(mapping_material),
    )


def _resolve_terminal_parents(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> tuple[
    ProductProposalRiskTarget,
    ProductProposalTargetTerminalPopulation,
    tuple[_TerminalGroupModel, ...],
]:
    try:
        target = _TARGET_RESOLVER(workspace, precommit.target_sha256)
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "proposal target cannot be re-resolved"
        ) from exc
    target = _require_target(precommit, target)
    try:
        terminal_population = _TERMINAL_RESOLVER(
            workspace,
            target_sha256=precommit.target_sha256,
            authorities=authorities,
        )
    except (
        ProductProposalTargetTerminalPopulationError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "provider terminal population cannot be re-resolved"
        ) from exc
    terminal_population = _require_terminal_population(
        precommit,
        terminal_population,
    )
    groups = _terminal_groups(terminal_population, authorities)
    return target, terminal_population, groups


def derive_product_proposal_terminal_scenario_binding(
    workspace: Path,
    *,
    precommit: ProductProposalRiskEvaluationPrecommit,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    market_state_ids: tuple[str, ...],
) -> CanonicalTerminalScenarioBinding:
    """Derive non-authorizing strings for one exact provider terminal-state vector.

    This helper is intended to run before the fixed-N scenario precommit is issued.
    The returned strings may be stored by that parent layer, but they do not by
    themselves prove product-owned sampling, joint support, execution, risk, tickets,
    provider writes, or money movement.
    """

    _require_dispatch()
    workspace = _workspace_path(workspace)
    precommit = _require_precommit(precommit)
    target, terminal_population, groups = _resolve_terminal_parents(
        workspace,
        precommit,
        authorities,
    )
    return _derive_binding_from_parents(
        precommit=precommit,
        target=target,
        terminal_population=terminal_population,
        authorities=authorities,
        groups=groups,
        state_ids=market_state_ids,
    )


def resolve_product_proposal_risk_terminal_state_mapping(
    workspace: Path,
    *,
    precommit: ProductProposalRiskEvaluationPrecommit,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> ProductProposalRiskTerminalStateMapping:
    """Prove only that the durable fixed-N scenario strings map to terminal states."""

    _require_dispatch()
    workspace = _workspace_path(workspace)
    precommit = _require_precommit(precommit)
    target, terminal_population, groups = _resolve_terminal_parents(
        workspace,
        precommit,
        authorities,
    )
    try:
        scenario_population = _SCENARIO_RESOLVER(
            workspace,
            precommit,
            terminal_population,
        )
    except (
        ProductProposalRiskScenarioPopulationError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "fixed-N scenario population cannot be re-resolved"
        ) from exc
    scenario_population = _require_scenario_population(
        precommit,
        terminal_population,
        scenario_population,
    )

    member_state_vector_sha256s: list[str] = []
    for index, (scenario_id, mapping_sha256) in enumerate(
        zip(
            scenario_population.member_scenario_ids,
            scenario_population.member_mapping_sha256s,
        )
    ):
        state_ids = _decode_scenario_id(
            scenario_id,
            terminal_population,
            groups,
        )
        derived = _derive_binding_from_parents(
            precommit=precommit,
            target=target,
            terminal_population=terminal_population,
            authorities=authorities,
            groups=groups,
            state_ids=state_ids,
        )
        if (
            derived.scenario_id != scenario_id
            or derived.mapping_sha256 != mapping_sha256
        ):
            raise ProductProposalRiskTerminalStateMappingError(
                f"member scenario mapping[{index}] does not re-derive from "
                "verified terminal states and exact proposal material"
            )
        member_state_vector_sha256s.append(derived.state_vector_sha256)

    if len(member_state_vector_sha256s) != len(precommit.planned_member_ids):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping lost fixed-N member cardinality"
        )

    result_material = {
        "schema": _RESULT_SCHEMA,
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "terminal_population_sha256": terminal_population.population_sha256,
        "scenario_population_sha256": scenario_population.population_sha256,
        "market_group_sha256s": list(terminal_population.market_group_sha256s),
        "joint_terminal_space_exact": terminal_population.terminal_space_exact,
        "planned_member_ids": list(precommit.planned_member_ids),
        "members": [
            {
                "member_id": member_id,
                "scenario_id": scenario_id,
                "mapping_sha256": mapping_sha256,
                "state_vector_sha256": state_vector_sha256,
            }
            for (
                member_id,
                scenario_id,
                mapping_sha256,
                state_vector_sha256,
            ) in zip(
                precommit.planned_member_ids,
                scenario_population.member_scenario_ids,
                scenario_population.member_mapping_sha256s,
                member_state_vector_sha256s,
            )
        ],
        "provider_terminal_population_proven": True,
        "fixed_n_member_mapping_complete": True,
        "per_market_terminal_states_exact": True,
        "terminal_mapping_proven": True,
        "product_scenario_source_provenance_proven": False,
        "iid_member_mapping_proven": False,
        "joint_scenario_support_proven": False,
        "scenario_execution_proven": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_risk_approval_authority": False,
        "grants_ticket_authority": False,
        "grants_broker_execution_authority": False,
        "grants_real_money_authority": False,
        "grants_state_mutation_authority": False,
    }
    resolution_sha256 = _digest(result_material)
    instance = object.__new__(_RESULT_TYPE)
    values = {
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "terminal_population_sha256": terminal_population.population_sha256,
        "scenario_population_sha256": scenario_population.population_sha256,
        "market_group_sha256s": terminal_population.market_group_sha256s,
        "joint_terminal_space_exact": terminal_population.terminal_space_exact,
        "planned_member_ids": precommit.planned_member_ids,
        "member_scenario_ids": scenario_population.member_scenario_ids,
        "member_mapping_sha256s": scenario_population.member_mapping_sha256s,
        "member_state_vector_sha256s": tuple(member_state_vector_sha256s),
        "resolution_sha256": resolution_sha256,
    }
    for name in _RESULT_FIELDS:
        object.__setattr__(instance, name, values[name])
    _BIND_MAPPING(instance)
    return instance


def _require_dispatch() -> None:
    if (
        ProductProposalRiskEvaluationPrecommit is not _PRECOMMIT_TYPE
        or ProductProposalRiskTarget is not _TARGET_TYPE
        or ProductProposalTargetTerminalPopulation is not _TERMINAL_POPULATION_TYPE
        or ProductProposalRiskScenarioPopulation is not _SCENARIO_POPULATION_TYPE
        or MarketSettlementOutcomeAuthority is not _AUTHORITY_TYPE
        or MarketTerminalState is not _TERMINAL_STATE_TYPE
        or resolve_product_proposal_risk_target is not _TARGET_RESOLVER
        or getattr(_TARGET_RESOLVER, "__code__", None) is not _TARGET_RESOLVER_CODE
        or resolve_product_proposal_target_terminal_population
        is not _TERMINAL_RESOLVER
        or getattr(_TERMINAL_RESOLVER, "__code__", None)
        is not _TERMINAL_RESOLVER_CODE
        or resolve_product_proposal_risk_scenario_population
        is not _SCENARIO_RESOLVER
        or getattr(_SCENARIO_RESOLVER, "__code__", None)
        is not _SCENARIO_RESOLVER_CODE
        or MarketSettlementOutcomeAuthority.authority_sha256.fget
        is not _AUTHORITY_SHA_GETTER
        or getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
        is not _AUTHORITY_SHA_GETTER_CODE
        or MarketSettlementOutcomeAuthority.terminal_states.fget
        is not _AUTHORITY_TERMINAL_STATES_GETTER
        or getattr(_AUTHORITY_TERMINAL_STATES_GETTER, "__code__", None)
        is not _AUTHORITY_TERMINAL_STATES_GETTER_CODE
        or MarketSettlementOutcomeAuthority.to_dict is not _AUTHORITY_TO_DICT
        or getattr(_AUTHORITY_TO_DICT, "__code__", None)
        is not _AUTHORITY_TO_DICT_CODE
        or _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or _JSON_LOADS is not _JSON_LOADS_EXPECTED
        or json.loads is not _JSON_LOADS_EXPECTED
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
        or _B64_ENCODE is not _B64_ENCODE_EXPECTED
        or base64.urlsafe_b64encode is not _B64_ENCODE_EXPECTED
        or _B64_DECODE is not _B64_DECODE_EXPECTED
        or base64.urlsafe_b64decode is not _B64_DECODE_EXPECTED
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal mapping dispatch root changed"
        )
    for name, expected, code in _HELPER_WITNESSES_EXPECTED:
        current = globals().get(name)
        if current is not expected or getattr(current, "__code__", None) is not code:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal mapping helper root changed"
            )


_HELPER_WITNESSES_EXPECTED = tuple(
    (name, globals()[name], getattr(globals()[name], "__code__", None))
    for name in (
        "_text",
        "_sha",
        "_decimal_text",
        "_canonical_bytes",
        "_digest",
        "_reject_duplicate_keys",
        "_reject_nonfinite",
        "_canonical_json_object",
        "_workspace_path",
        "_require_precommit",
        "_require_target",
        "_require_terminal_population",
        "_require_scenario_population",
        "_authority_map",
        "_terminal_groups",
        "_leg_key",
        "_state_vector_payload",
        "_scenario_id",
        "_decode_scenario_id",
        "_candidate_mapping_material",
        "_derive_binding_from_parents",
        "_resolve_terminal_parents",
    )
)


del _MAPPING_PROVEN
del _BIND_MAPPING


__all__ = [
    "CanonicalTerminalScenarioBinding",
    "ProductProposalRiskTerminalStateMapping",
    "ProductProposalRiskTerminalStateMappingError",
    "derive_product_proposal_terminal_scenario_binding",
    "resolve_product_proposal_risk_terminal_state_mapping",
]
