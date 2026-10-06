from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .market_outcomes import MarketSettlementOutcomeAuthority
from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from .proposal_risk_terminal_payoff_authority import (
    ProductProposalRiskTerminalPayoffEvaluation,
    ProductProposalRiskTerminalPayoffEvaluationError,
    resolve_product_proposal_risk_terminal_payoff_evaluation,
)
from .proposal_risk_terminal_state_mapping_authority import (
    ProductProposalRiskTerminalStateMapping,
    ProductProposalRiskTerminalStateMappingError,
    resolve_product_proposal_risk_terminal_state_mapping,
)
from .proposal_target_terminal_population_authority import (
    ProductProposalTargetTerminalPopulation,
    ProductProposalTargetTerminalPopulationError,
    resolve_product_proposal_target_terminal_population,
)


_SCHEMA = "autosport.proposal-risk-terminal-component-provenance.v1"
_SOURCE_PROTOCOL = (
    "provider-terminal-authority-as-of-target+"
    "durable-scenario-precommit+exact-terminal-mapping.v1"
)
_SCHEMA_EXPECTED = _SCHEMA
_SOURCE_PROTOCOL_EXPECTED = _SOURCE_PROTOCOL
_HEX = frozenset("0123456789abcdef")
_PATH_TYPE = type(Path("."))

_PRECOMMIT_TYPE = ProductProposalRiskEvaluationPrecommit
_POPULATION_TYPE = ProductProposalTargetTerminalPopulation
_MAPPING_TYPE = ProductProposalRiskTerminalStateMapping
_PAYOFF_TYPE = ProductProposalRiskTerminalPayoffEvaluation
_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority

_POPULATION_RESOLVER = resolve_product_proposal_target_terminal_population
_POPULATION_RESOLVER_CODE = getattr(_POPULATION_RESOLVER, "__code__", None)
_MAPPING_RESOLVER = resolve_product_proposal_risk_terminal_state_mapping
_MAPPING_RESOLVER_CODE = getattr(_MAPPING_RESOLVER, "__code__", None)
_PAYOFF_RESOLVER = resolve_product_proposal_risk_terminal_payoff_evaluation
_PAYOFF_RESOLVER_CODE = getattr(_PAYOFF_RESOLVER, "__code__", None)
_AUTHORITY_SHA_GETTER = MarketSettlementOutcomeAuthority.authority_sha256.fget
_AUTHORITY_SHA_GETTER_CODE = getattr(_AUTHORITY_SHA_GETTER, "__code__", None)

_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256


class ProductProposalRiskTerminalComponentProvenanceError(RuntimeError):
    """Exact proposal terminal-component provenance cannot be proven safely."""


def _text(value: object, name: str, *, max_length: int = 2048) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskTerminalComponentProvenanceError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskTerminalComponentProvenanceError(
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
        raise ProductProposalRiskTerminalComponentProvenanceError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _canonical_bytes(
    value: object,
    _json_dumps_code=_JSON_DUMPS_CODE,
) -> bytes:
    if (
        _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS, "__code__", None) is not _json_dumps_code
        or getattr(json.dumps, "__code__", None) is not _json_dumps_code
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal component provenance canonical dispatch changed"
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
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal component provenance material is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return _HASHLIB_SHA256(_canonical_bytes(value)).hexdigest()


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE or not value.is_absolute():
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != value or not value.is_dir() or value.is_symlink():
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "workspace must be the canonical non-symlink directory path"
        )
    return value


def _require_precommit(
    precommit: object,
) -> ProductProposalRiskEvaluationPrecommit:
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "precommit must be exact ProductProposalRiskEvaluationPrecommit"
        )
    if (
        precommit.binding_identity_proven is not True
        or precommit.proposal_target_identity_proven is not True
        or precommit.scientific_precommit_proven is not True
        or precommit.scientific_preoutcome_chronology_proven is not True
        or precommit.proposal_target_bound_after_scientific_precommit is not True
        or precommit.scientific_precommit_proves_proposal_execution_scope
        is not False
        or precommit.proposal_target_counterfactual_execution_proven is not False
        or precommit.risk_upper_bound_for_target is not False
        or precommit.grants_ticket_authority is not False
        or precommit.grants_real_money_authority is not False
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "proposal risk precommit truth boundary is inconsistent"
        )
    return precommit


def _require_mapping(
    precommit: ProductProposalRiskEvaluationPrecommit,
    mapping: object,
) -> ProductProposalRiskTerminalStateMapping:
    if type(mapping) is not _MAPPING_TYPE:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal mapping resolver returned a non-canonical type"
        )
    if (
        mapping.mapping_identity_proven is not True
        or mapping.provider_terminal_population_proven is not True
        or mapping.fixed_n_member_mapping_complete is not True
        or type(mapping.per_market_terminal_space_exact) is not bool
        or mapping.per_market_terminal_states_exact
        is not mapping.per_market_terminal_space_exact
        or mapping.terminal_mapping_proven is not True
        or mapping.product_scenario_source_provenance_proven is not False
        or mapping.iid_member_mapping_proven is not False
        or mapping.joint_scenario_support_proven is not False
        or mapping.scenario_execution_proven is not False
        or mapping.proposal_target_counterfactual_execution_proven is not False
        or mapping.risk_upper_bound_for_target is not False
        or mapping.grants_risk_approval_authority is not False
        or mapping.grants_ticket_authority is not False
        or mapping.grants_broker_execution_authority is not False
        or mapping.grants_real_money_authority is not False
        or mapping.grants_state_mutation_authority is not False
        or mapping.workspace_instance_id != precommit.workspace_instance_id
        or mapping.precommit_binding_sha256 != precommit.binding_sha256
        or mapping.target_sha256 != precommit.target_sha256
        or mapping.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or mapping.planned_member_ids != precommit.planned_member_ids
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal mapping truth boundary does not match the exact precommit"
        )
    return mapping


def _require_payoff(
    precommit: ProductProposalRiskEvaluationPrecommit,
    mapping: ProductProposalRiskTerminalStateMapping,
    payoff: object,
) -> ProductProposalRiskTerminalPayoffEvaluation:
    if type(payoff) is not _PAYOFF_TYPE:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal payoff resolver returned a non-canonical type"
        )
    if (
        payoff.evaluation_identity_proven is not True
        or payoff.terminal_mapping_consumed_proven is not True
        or payoff.paperbook_settlement_arithmetic_proven is not True
        or payoff.fixed_n_member_payoff_complete is not True
        or payoff.target_terminal_payoff_evaluation_proven is not True
        or type(payoff.per_market_terminal_space_exact) is not bool
        or payoff.per_market_terminal_space_exact
        is not mapping.per_market_terminal_space_exact
        or payoff.joint_terminal_space_exact
        is not mapping.joint_terminal_space_exact
        or payoff.product_scenario_source_provenance_proven is not False
        or payoff.iid_member_mapping_proven is not False
        or payoff.joint_scenario_support_proven is not False
        or payoff.minimum_equity_path_proven is not False
        or payoff.execution_costs_proven is not False
        or payoff.slippage_realization_proven is not False
        or payoff.net_execution_pnl_proven is not False
        or payoff.cashflow_chronology_proven is not False
        or payoff.scenario_execution_proven is not False
        or payoff.proposal_target_counterfactual_execution_proven is not False
        or payoff.risk_upper_bound_for_target is not False
        or payoff.grants_risk_approval_authority is not False
        or payoff.grants_ticket_authority is not False
        or payoff.grants_broker_execution_authority is not False
        or payoff.grants_real_money_authority is not False
        or payoff.grants_state_mutation_authority is not False
        or payoff.workspace_instance_id != precommit.workspace_instance_id
        or payoff.precommit_binding_sha256 != precommit.binding_sha256
        or payoff.target_sha256 != precommit.target_sha256
        or payoff.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or payoff.terminal_population_sha256 != mapping.terminal_population_sha256
        or payoff.scenario_population_sha256 != mapping.scenario_population_sha256
        or payoff.terminal_mapping_resolution_sha256 != mapping.resolution_sha256
        or payoff.planned_member_ids != mapping.planned_member_ids
        or payoff.member_scenario_ids != mapping.member_scenario_ids
        or payoff.member_mapping_sha256s != mapping.member_mapping_sha256s
        or payoff.member_state_vector_sha256s != mapping.member_state_vector_sha256s
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal payoff truth boundary does not match the exact mapping"
        )
    return payoff


def _require_population(
    precommit: ProductProposalRiskEvaluationPrecommit,
    mapping: ProductProposalRiskTerminalStateMapping,
    population: object,
) -> ProductProposalTargetTerminalPopulation:
    if type(population) is not _POPULATION_TYPE:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal population resolver returned a non-canonical type"
        )
    if (
        population.population_identity_proven is not True
        or population.provider_terminal_authority_proven is not True
        or population.terminal_space_exhaustive is not True
        or population.probability_model_bound is not False
        or population.scientific_precommit_bound is not False
        or population.iid_member_mapping_proven is not False
        or population.proposal_target_counterfactual_execution_proven is not False
        or population.risk_upper_bound_for_target is not False
        or population.grants_ticket_authority is not False
        or population.grants_real_money_authority is not False
        or population.workspace_instance_id != precommit.workspace_instance_id
        or population.target_sha256 != precommit.target_sha256
        or population.target_decision_ts != precommit.target_decision_ts
        or population.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or population.population_sha256 != mapping.terminal_population_sha256
        or population.market_group_sha256s != mapping.market_group_sha256s
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal population truth boundary does not match the exact mapping"
        )
    return population


def _authority_sha256s(
    population: ProductProposalTargetTerminalPopulation,
    authorities: object,
) -> tuple[str, ...]:
    if type(authorities) is not tuple or not authorities:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "authorities must be a non-empty exact tuple"
        )
    values: list[str] = []
    seen: set[str] = set()
    for index, authority in enumerate(authorities):
        if type(authority) is not _AUTHORITY_TYPE:
            raise ProductProposalRiskTerminalComponentProvenanceError(
                f"authorities[{index}] must be exact MarketSettlementOutcomeAuthority"
            )
        value = _sha(
            _AUTHORITY_SHA_GETTER(authority),
            f"authorities[{index}].authority_sha256",
        )
        if value in seen:
            raise ProductProposalRiskTerminalComponentProvenanceError(
                "authorities contain a duplicate authority digest"
            )
        seen.add(value)
        values.append(value)
    expected = tuple(population.market_authority_sha256s)
    if tuple(sorted(values)) != expected:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "reverified provider authority set differs from terminal population"
        )
    return expected


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_terminal_component_provenance_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(
            instance,
            "_terminal_component_provenance_capability",
            token,
        )

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskTerminalComponentProvenance:
    """Provider/target provenance for precommitted terminal-vector components.

    Positive truth is limited to provenance of each already-fixed vector component:
    every accepted terminal state re-resolves through the exact target, target-time
    provider terminal authority population, deterministic terminal mapping, and
    canonical terminal payoff evaluation. The vector choice itself remains a
    caller-precommitted assertion. This does not prove product scenario-source
    provenance, a scenario-selection law, IID sampling, exact cross-market joint
    support, path execution, or a target risk bound.
    """

    workspace_instance_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    target_decision_ts: str
    source_protocol: str
    terminal_population_sha256: str
    market_authority_sha256s: tuple[str, ...]
    market_group_sha256s: tuple[str, ...]
    per_market_terminal_space_exact: bool
    joint_terminal_space_exact: bool
    scenario_population_sha256: str
    terminal_mapping_resolution_sha256: str
    terminal_payoff_evaluation_sha256: str
    planned_member_ids: tuple[str, ...]
    member_scenario_ids: tuple[str, ...]
    member_mapping_sha256s: tuple[str, ...]
    member_state_vector_sha256s: tuple[str, ...]
    provenance_sha256: str
    _terminal_component_provenance_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductProposalRiskTerminalComponentProvenance":
        raise TypeError(
            "ProductProposalRiskTerminalComponentProvenance is product-resolved; "
            "use resolve_product_proposal_risk_terminal_component_provenance"
        )

    @property
    def provenance_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def provider_terminal_population_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def terminal_mapping_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def target_terminal_payoff_evaluation_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def provider_terminal_component_provenance_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
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
    def scenario_selection_law_proven(self) -> bool:
        return False

    @property
    def minimum_equity_path_proven(self) -> bool:
        return False

    @property
    def execution_costs_proven(self) -> bool:
        return False

    @property
    def slippage_realization_proven(self) -> bool:
        return False

    @property
    def net_execution_pnl_proven(self) -> bool:
        return False

    @property
    def cashflow_chronology_proven(self) -> bool:
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


_RESULT_TYPE = ProductProposalRiskTerminalComponentProvenance
_RESULT_TYPE_EXPECTED = _RESULT_TYPE
_RESULT_FIELDS = (
    "workspace_instance_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "target_decision_ts",
    "source_protocol",
    "terminal_population_sha256",
    "market_authority_sha256s",
    "market_group_sha256s",
    "per_market_terminal_space_exact",
    "joint_terminal_space_exact",
    "scenario_population_sha256",
    "terminal_mapping_resolution_sha256",
    "terminal_payoff_evaluation_sha256",
    "planned_member_ids",
    "member_scenario_ids",
    "member_mapping_sha256s",
    "member_state_vector_sha256s",
    "provenance_sha256",
)
_RESULT_FIELDS_EXPECTED = _RESULT_FIELDS
_RESULT_FIELD_DESCRIPTOR_WITNESSES = tuple(
    (name, _RESULT_TYPE.__dict__[name])
    for name in _RESULT_FIELDS
)
_RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED = (
    _RESULT_FIELD_DESCRIPTOR_WITNESSES
)
_RESULT_AUTHORITY_PROPERTY_NAMES = (
    "provenance_identity_proven",
    "provider_terminal_population_proven",
    "terminal_mapping_proven",
    "target_terminal_payoff_evaluation_proven",
    "provider_terminal_component_provenance_proven",
    "product_scenario_source_provenance_proven",
    "iid_member_mapping_proven",
    "joint_scenario_support_proven",
    "scenario_selection_law_proven",
    "minimum_equity_path_proven",
    "execution_costs_proven",
    "slippage_realization_proven",
    "net_execution_pnl_proven",
    "cashflow_chronology_proven",
    "scenario_execution_proven",
    "proposal_target_counterfactual_execution_proven",
    "risk_upper_bound_for_target",
    "grants_risk_approval_authority",
    "grants_ticket_authority",
    "grants_broker_execution_authority",
    "grants_real_money_authority",
    "grants_state_mutation_authority",
)
_RESULT_AUTHORITY_PROPERTY_NAMES_EXPECTED = _RESULT_AUTHORITY_PROPERTY_NAMES
_RESULT_AUTHORITY_PROPERTY_WITNESSES = tuple(
    (
        name,
        _RESULT_TYPE.__dict__[name],
        _RESULT_TYPE.__dict__[name].fget,
        getattr(_RESULT_TYPE.__dict__[name].fget, "__code__", None),
        getattr(_RESULT_TYPE.__dict__[name].fget, "__defaults__", None),
        tuple(
            (
                default,
                getattr(default, "__code__", None),
                getattr(default, "__closure__", None),
                tuple(
                    (cell, cell.cell_contents)
                    for cell in (getattr(default, "__closure__", None) or ())
                ),
            )
            for default in (
                getattr(_RESULT_TYPE.__dict__[name].fget, "__defaults__", None)
                or ()
            )
        ),
    )
    for name in _RESULT_AUTHORITY_PROPERTY_NAMES
)
_RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED = (
    _RESULT_AUTHORITY_PROPERTY_WITNESSES
)


def _resolve_values(
    workspace: Path,
    *,
    precommit: ProductProposalRiskEvaluationPrecommit,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    member_market_state_ids: tuple[tuple[str, ...], ...],
) -> dict[str, object]:
    if (
        _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL
        or getattr(_REQUIRE_DISPATCH_ORIGINAL, "__code__", None)
        is not _REQUIRE_DISPATCH_CODE
        or getattr(_REQUIRE_DISPATCH_ORIGINAL, "__defaults__", None)
        is not _REQUIRE_DISPATCH_DEFAULTS
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal component provenance dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    precommit = _require_precommit(precommit)

    try:
        mapping = _MAPPING_RESOLVER(
            workspace,
            precommit=precommit,
            authorities=authorities,
            member_market_state_ids=member_market_state_ids,
        )
    except (
        ProductProposalRiskTerminalStateMappingError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal mapping cannot be re-resolved"
        ) from exc
    mapping = _require_mapping(precommit, mapping)

    try:
        payoff = _PAYOFF_RESOLVER(
            workspace,
            precommit=precommit,
            authorities=authorities,
            member_market_state_ids=member_market_state_ids,
        )
    except (
        ProductProposalRiskTerminalPayoffEvaluationError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal payoff cannot be re-resolved"
        ) from exc
    payoff = _require_payoff(precommit, mapping, payoff)

    try:
        population = _POPULATION_RESOLVER(
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
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "provider terminal population cannot be re-resolved"
        ) from exc
    population = _require_population(precommit, mapping, population)
    authority_sha256s = _authority_sha256s(population, authorities)

    if (
        len(mapping.planned_member_ids) != len(mapping.member_scenario_ids)
        or len(mapping.planned_member_ids) != len(mapping.member_mapping_sha256s)
        or len(mapping.planned_member_ids)
        != len(mapping.member_state_vector_sha256s)
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "mapped member provenance vectors lost fixed-N cardinality"
        )

    material = {
        "schema": _SCHEMA,
        "source_protocol": _SOURCE_PROTOCOL,
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "target_decision_ts": precommit.target_decision_ts,
        "terminal_population_sha256": population.population_sha256,
        "market_authority_sha256s": list(authority_sha256s),
        "market_group_sha256s": list(population.market_group_sha256s),
        "per_market_terminal_space_exact": mapping.per_market_terminal_space_exact,
        "joint_terminal_space_exact": mapping.joint_terminal_space_exact,
        "scenario_population_sha256": mapping.scenario_population_sha256,
        "terminal_mapping_resolution_sha256": mapping.resolution_sha256,
        "terminal_payoff_evaluation_sha256": payoff.evaluation_sha256,
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
                mapping.planned_member_ids,
                mapping.member_scenario_ids,
                mapping.member_mapping_sha256s,
                mapping.member_state_vector_sha256s,
            )
        ],
        "provider_terminal_population_proven": True,
        "terminal_mapping_proven": True,
        "target_terminal_payoff_evaluation_proven": True,
        "provider_terminal_component_provenance_proven": True,
        "product_scenario_source_provenance_proven": False,
        "iid_member_mapping_proven": False,
        "joint_scenario_support_proven": False,
        "scenario_selection_law_proven": False,
        "minimum_equity_path_proven": False,
        "execution_costs_proven": False,
        "slippage_realization_proven": False,
        "net_execution_pnl_proven": False,
        "cashflow_chronology_proven": False,
        "scenario_execution_proven": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "grants_risk_approval_authority": False,
        "grants_ticket_authority": False,
        "grants_broker_execution_authority": False,
        "grants_real_money_authority": False,
        "grants_state_mutation_authority": False,
    }
    return {
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "target_decision_ts": precommit.target_decision_ts,
        "source_protocol": _SOURCE_PROTOCOL,
        "terminal_population_sha256": population.population_sha256,
        "market_authority_sha256s": authority_sha256s,
        "market_group_sha256s": population.market_group_sha256s,
        "per_market_terminal_space_exact": mapping.per_market_terminal_space_exact,
        "joint_terminal_space_exact": mapping.joint_terminal_space_exact,
        "scenario_population_sha256": mapping.scenario_population_sha256,
        "terminal_mapping_resolution_sha256": mapping.resolution_sha256,
        "terminal_payoff_evaluation_sha256": payoff.evaluation_sha256,
        "planned_member_ids": mapping.planned_member_ids,
        "member_scenario_ids": mapping.member_scenario_ids,
        "member_mapping_sha256s": mapping.member_mapping_sha256s,
        "member_state_vector_sha256s": mapping.member_state_vector_sha256s,
        "provenance_sha256": _digest(material),
    }


def _make_public_resolver(
    _bind_identity,
    _resolve_core,
    _result_type,
    _result_fields,
):
    _bind_code = getattr(_bind_identity, "__code__", None)
    _resolve_core_code = getattr(_resolve_core, "__code__", None)

    def resolver(
        workspace: Path,
        *,
        precommit: ProductProposalRiskEvaluationPrecommit,
        authorities: tuple[MarketSettlementOutcomeAuthority, ...],
        member_market_state_ids: tuple[tuple[str, ...], ...],
    ) -> ProductProposalRiskTerminalComponentProvenance:
        bind_identity = _bind_identity
        resolve_core = _resolve_core
        result_type = _result_type
        result_fields = _result_fields
        expected_bind_code = _bind_code
        expected_core_code = _resolve_core_code
        if (
            getattr(bind_identity, "__code__", None) is not expected_bind_code
            or getattr(resolve_core, "__code__", None) is not expected_core_code
        ):
            raise ProductProposalRiskTerminalComponentProvenanceError(
                "terminal component provenance public resolver closure changed"
            )
        values = resolve_core(
            workspace,
            precommit=precommit,
            authorities=authorities,
            member_market_state_ids=member_market_state_ids,
        )
        instance = object.__new__(result_type)
        for name in result_fields:
            object.__setattr__(instance, name, values[name])
        bind_identity(instance)
        return instance

    resolver.__name__ = (
        "resolve_product_proposal_risk_terminal_component_provenance"
    )
    resolver.__qualname__ = resolver.__name__
    resolver.__doc__ = (
        "Resolve provider/target terminal-component provenance without "
        "granting sampling, joint-support, execution, risk, ticket or money "
        "authority."
    )
    return resolver


resolve_product_proposal_risk_terminal_component_provenance = (
    _make_public_resolver(
        _BIND_IDENTITY,
        _resolve_values,
        _RESULT_TYPE,
        _RESULT_FIELDS_EXPECTED,
    )
)
_PUBLIC_RESOLVER = resolve_product_proposal_risk_terminal_component_provenance
_PUBLIC_RESOLVER_CODE = getattr(_PUBLIC_RESOLVER, "__code__", None)
_PUBLIC_RESOLVER_CLOSURE = getattr(_PUBLIC_RESOLVER, "__closure__", None)
_PUBLIC_RESOLVER_CLOSURE_WITNESSES = tuple(
    (
        cell,
        cell.cell_contents,
        getattr(cell.cell_contents, "__code__", None),
        getattr(cell.cell_contents, "__closure__", None),
        tuple(
            (nested_cell, nested_cell.cell_contents)
            for nested_cell in (
                getattr(cell.cell_contents, "__closure__", None) or ()
            )
        ),
    )
    for cell in (_PUBLIC_RESOLVER_CLOSURE or ())
)
_PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED = (
    _PUBLIC_RESOLVER_CLOSURE_WITNESSES
)
del _make_public_resolver


def _require_dispatch() -> None:
    if (
        _SCHEMA != _SCHEMA_EXPECTED
        or _SOURCE_PROTOCOL != _SOURCE_PROTOCOL_EXPECTED
        or ProductProposalRiskEvaluationPrecommit is not _PRECOMMIT_TYPE
        or ProductProposalTargetTerminalPopulation is not _POPULATION_TYPE
        or ProductProposalRiskTerminalStateMapping is not _MAPPING_TYPE
        or ProductProposalRiskTerminalPayoffEvaluation is not _PAYOFF_TYPE
        or MarketSettlementOutcomeAuthority is not _AUTHORITY_TYPE
        or resolve_product_proposal_target_terminal_population
        is not _POPULATION_RESOLVER
        or getattr(_POPULATION_RESOLVER, "__code__", None)
        is not _POPULATION_RESOLVER_CODE
        or resolve_product_proposal_risk_terminal_state_mapping
        is not _MAPPING_RESOLVER
        or getattr(_MAPPING_RESOLVER, "__code__", None)
        is not _MAPPING_RESOLVER_CODE
        or resolve_product_proposal_risk_terminal_payoff_evaluation
        is not _PAYOFF_RESOLVER
        or getattr(_PAYOFF_RESOLVER, "__code__", None)
        is not _PAYOFF_RESOLVER_CODE
        or MarketSettlementOutcomeAuthority.authority_sha256.fget
        is not _AUTHORITY_SHA_GETTER
        or getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
        is not _AUTHORITY_SHA_GETTER_CODE
        or _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS, "__code__", None) is not _JSON_DUMPS_CODE
        or getattr(json.dumps, "__code__", None) is not _JSON_DUMPS_CODE
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
        or ProductProposalRiskTerminalComponentProvenance is not _RESULT_TYPE_EXPECTED
        or _RESULT_TYPE is not _RESULT_TYPE_EXPECTED
        or _RESULT_FIELDS is not _RESULT_FIELDS_EXPECTED
        or _RESULT_FIELD_DESCRIPTOR_WITNESSES
        is not _RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED
        or _RESULT_AUTHORITY_PROPERTY_NAMES
        is not _RESULT_AUTHORITY_PROPERTY_NAMES_EXPECTED
        or _RESULT_AUTHORITY_PROPERTY_WITNESSES
        is not _RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
        or resolve_product_proposal_risk_terminal_component_provenance
        is not _PUBLIC_RESOLVER
        or getattr(
            resolve_product_proposal_risk_terminal_component_provenance,
            "__code__",
            None,
        )
        is not _PUBLIC_RESOLVER_CODE
        or getattr(
            resolve_product_proposal_risk_terminal_component_provenance,
            "__closure__",
            None,
        )
        is not _PUBLIC_RESOLVER_CLOSURE
        or _PUBLIC_RESOLVER_CLOSURE_WITNESSES
        is not _PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED
        or _HELPER_WITNESSES is not _HELPER_WITNESSES_EXPECTED
        or type(_HELPER_WITNESSES_EXPECTED) is not tuple
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal component provenance dispatch root changed"
        )

    for name, expected_descriptor in _RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED:
        if _RESULT_TYPE.__dict__.get(name) is not expected_descriptor:
            raise ProductProposalRiskTerminalComponentProvenanceError(
                "terminal component provenance result field surface changed"
            )

    for name, descriptor, getter, code, defaults, default_witnesses in (
        _RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
    ):
        current_descriptor = _RESULT_TYPE.__dict__.get(name)
        current_getter = getattr(current_descriptor, "fget", None)
        current_defaults = getattr(current_getter, "__defaults__", None)
        if (
            current_descriptor is not descriptor
            or current_getter is not getter
            or getattr(current_getter, "__code__", None) is not code
            or current_defaults is not defaults
            or len(current_defaults or ()) != len(default_witnesses)
            or any(
                current_defaults[index] is not expected_default
                or getattr(current_defaults[index], "__code__", None)
                is not expected_code
                or getattr(current_defaults[index], "__closure__", None)
                is not expected_closure
                or len(expected_closure or ()) != len(expected_nested_cells)
                or any(
                    nested_cell is not expected_cell
                    or nested_cell.cell_contents is not expected_value
                    for nested_cell, (expected_cell, expected_value)
                    in zip(expected_closure or (), expected_nested_cells)
                )
                for index, (
                    expected_default,
                    expected_code,
                    expected_closure,
                    expected_nested_cells,
                )
                in enumerate(default_witnesses)
            )
        ):
            raise ProductProposalRiskTerminalComponentProvenanceError(
                "terminal component provenance result authority surface changed"
            )

    if (
        any(
            cell is not expected_cell
            or cell.cell_contents is not expected_value
            or getattr(expected_value, "__code__", None) is not expected_code
            or getattr(expected_value, "__closure__", None)
            is not expected_nested_closure
            or len(expected_nested_closure or ()) != len(expected_nested_cells)
            or any(
                nested_cell is not expected_nested_cell
                or nested_cell.cell_contents is not expected_nested_value
                for nested_cell, (
                    expected_nested_cell,
                    expected_nested_value,
                )
                in zip(expected_nested_closure or (), expected_nested_cells)
            )
            for cell, (
                expected_cell,
                expected_value,
                expected_code,
                expected_nested_closure,
                expected_nested_cells,
            )
            in zip(
                _PUBLIC_RESOLVER_CLOSURE or (),
                _PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED,
            )
        )
        or len(_PUBLIC_RESOLVER_CLOSURE or ())
        != len(_PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED)
    ):
        raise ProductProposalRiskTerminalComponentProvenanceError(
            "terminal component provenance public resolver root changed"
        )

    for (
        name,
        expected,
        code,
        defaults,
        kwdefaults,
        kwdefault_items,
    ) in _HELPER_WITNESSES_EXPECTED:
        current = globals().get(name)
        current_kwdefaults = getattr(current, "__kwdefaults__", None)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
            or getattr(current, "__defaults__", None) is not defaults
            or current_kwdefaults is not kwdefaults
            or tuple(sorted((current_kwdefaults or {}).items()))
            != kwdefault_items
        ):
            raise ProductProposalRiskTerminalComponentProvenanceError(
                "terminal component provenance helper root changed"
            )


_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
        getattr(globals()[name], "__defaults__", None),
        getattr(globals()[name], "__kwdefaults__", None),
        tuple(
            sorted(
                (getattr(globals()[name], "__kwdefaults__", None) or {}).items()
            )
        ),
    )
    for name in (
        "_text",
        "_sha",
        "_canonical_bytes",
        "_digest",
        "_workspace_path",
        "_require_precommit",
        "_require_mapping",
        "_require_payoff",
        "_require_population",
        "_authority_sha256s",
        "_resolve_values",
    )
)
_HELPER_WITNESSES_EXPECTED = _HELPER_WITNESSES

_REQUIRE_DISPATCH_ORIGINAL = _require_dispatch
_REQUIRE_DISPATCH_CODE = getattr(_REQUIRE_DISPATCH_ORIGINAL, "__code__", None)
_REQUIRE_DISPATCH_DEFAULTS = getattr(
    _REQUIRE_DISPATCH_ORIGINAL,
    "__defaults__",
    None,
)


del _IDENTITY_PROVEN
del _BIND_IDENTITY


__all__ = [
    "ProductProposalRiskTerminalComponentProvenance",
    "ProductProposalRiskTerminalComponentProvenanceError",
    "resolve_product_proposal_risk_terminal_component_provenance",
]
