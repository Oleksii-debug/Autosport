from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .market_outcomes import (
    MarketOutcomeIdentity,
    MarketSettlementOutcomeAuthority,
    MarketTerminalState,
)
from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from .proposal_risk_scenario_population_authority import (
    CounterfactualScenarioMemberBinding,
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


_SCENARIO_SCHEMA = "autosport.proposal-risk-terminal-scenario.v1"
_MAPPING_SCHEMA = "autosport.proposal-risk-terminal-state-mapping.v1"
_SCENARIO_PREFIX = "terminal-state-v1:"
_HEX = frozenset("0123456789abcdef")
_PATH_TYPE = type(Path("."))
_PRECOMMIT_TYPE = ProductProposalRiskEvaluationPrecommit
_TARGET_TYPE = ProductProposalRiskTarget
_TERMINAL_TYPE = ProductProposalTargetTerminalPopulation
_SCENARIO_TYPE = ProductProposalRiskScenarioPopulation
_IDENTITY_TYPE = MarketOutcomeIdentity
_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_STATE_TYPE = MarketTerminalState
_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TERMINAL_RESOLVER = resolve_product_proposal_target_terminal_population
_SCENARIO_RESOLVER = resolve_product_proposal_risk_scenario_population
_SETTLEMENT_BY_QUOTE = MarketSettlementOutcomeAuthority.settlement_by_quote
_STATE_IS_DERIVED = MarketSettlementOutcomeAuthority._state_is_derived
_QUOTE_KEY = MarketOutcomeIdentity.quote_key
_MARKET_KEY_GETTER = MarketOutcomeIdentity.market_key.fget
_AUTHORITY_SHA_GETTER = MarketSettlementOutcomeAuthority.authority_sha256.fget
_JSON_DUMPS = json.dumps
_JSON_LOADS = json.loads
_JSON_DECODE_ERROR_TYPE = json.JSONDecodeError
_HASHLIB_SHA256 = hashlib.sha256
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_TERMINAL_RESOLVER_CODE = getattr(_TERMINAL_RESOLVER, "__code__", None)
_SCENARIO_RESOLVER_CODE = getattr(_SCENARIO_RESOLVER, "__code__", None)
_SETTLEMENT_BY_QUOTE_CODE = getattr(_SETTLEMENT_BY_QUOTE, "__code__", None)
_STATE_IS_DERIVED_CODE = getattr(_STATE_IS_DERIVED, "__code__", None)
_QUOTE_KEY_CODE = getattr(_QUOTE_KEY, "__code__", None)
_MARKET_KEY_GETTER_CODE = getattr(_MARKET_KEY_GETTER, "__code__", None)
_AUTHORITY_SHA_GETTER_CODE = getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_JSON_LOADS_CODE = getattr(_JSON_LOADS, "__code__", None)


class ProductProposalRiskTerminalStateMappingError(RuntimeError):
    """Raised when exact target terminal-state mapping cannot be re-derived."""


def _require_dispatch() -> None:
    if (
        ProductProposalRiskEvaluationPrecommit is not _PRECOMMIT_TYPE
        or ProductProposalRiskTarget is not _TARGET_TYPE
        or ProductProposalTargetTerminalPopulation is not _TERMINAL_TYPE
        or ProductProposalRiskScenarioPopulation is not _SCENARIO_TYPE
        or MarketOutcomeIdentity is not _IDENTITY_TYPE
        or MarketSettlementOutcomeAuthority is not _AUTHORITY_TYPE
        or MarketTerminalState is not _STATE_TYPE
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
        or MarketSettlementOutcomeAuthority.settlement_by_quote
        is not _SETTLEMENT_BY_QUOTE
        or getattr(_SETTLEMENT_BY_QUOTE, "__code__", None)
        is not _SETTLEMENT_BY_QUOTE_CODE
        or MarketSettlementOutcomeAuthority._state_is_derived is not _STATE_IS_DERIVED
        or getattr(_STATE_IS_DERIVED, "__code__", None)
        is not _STATE_IS_DERIVED_CODE
        or MarketOutcomeIdentity.quote_key is not _QUOTE_KEY
        or getattr(_QUOTE_KEY, "__code__", None) is not _QUOTE_KEY_CODE
        or MarketOutcomeIdentity.market_key.fget is not _MARKET_KEY_GETTER
        or getattr(_MARKET_KEY_GETTER, "__code__", None)
        is not _MARKET_KEY_GETTER_CODE
        or MarketSettlementOutcomeAuthority.authority_sha256.fget
        is not _AUTHORITY_SHA_GETTER
        or getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
        is not _AUTHORITY_SHA_GETTER_CODE
        or json.dumps is not _JSON_DUMPS
        or getattr(_JSON_DUMPS, "__code__", None) is not _JSON_DUMPS_CODE
        or json.loads is not _JSON_LOADS
        or getattr(_JSON_LOADS, "__code__", None) is not _JSON_LOADS_CODE
        or json.JSONDecodeError is not _JSON_DECODE_ERROR_TYPE
        or hashlib.sha256 is not _HASHLIB_SHA256
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal-state mapping dispatch changed"
        )


_REQUIRE_DISPATCH_ORIGINAL = _require_dispatch


def _text(value: object, name: str, *, max_length: int = 4096) -> str:
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
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name, max_length=64)
    if (
        len(value) != 64
        or value != value.lower()
        or any(ch not in _HEX for ch in value)
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return value


def _decimal_text(value: object, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} must be an exact finite Decimal"
        )
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(child) for child in value]
    return value


def _canonical_json(value: object) -> str:
    _require_dispatch()
    try:
        return _JSON_DUMPS(
            _plain(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal-state mapping is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    _require_dispatch()
    return _HASHLIB_SHA256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProductProposalRiskTerminalStateMappingError(
                "canonical material contains duplicate JSON keys"
            )
        result[key] = value
    return result


def _parse_canonical_json(value: object, name: str) -> dict[str, object]:
    value = _text(value, name, max_length=1_000_000)
    try:
        parsed = _JSON_LOADS(value, object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError, _JSON_DECODE_ERROR_TYPE) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} is invalid JSON"
        ) from exc
    if type(parsed) is not dict or _canonical_json(parsed) != value:
        raise ProductProposalRiskTerminalStateMappingError(
            f"{name} is not canonical JSON"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class CounterfactualMemberTerminalStateSelection:
    """Exact per-market terminal states selected for one fixed-N member."""

    member_id: str
    market_states: tuple[MarketTerminalState, ...]

    def __post_init__(self) -> None:
        _text(self.member_id, "member_id", max_length=256)
        if type(self.market_states) is not tuple or not self.market_states:
            raise ProductProposalRiskTerminalStateMappingError(
                "market_states must be a non-empty exact tuple"
            )
        if any(type(state) is not _STATE_TYPE for state in self.market_states):
            raise ProductProposalRiskTerminalStateMappingError(
                "market_states must contain exact MarketTerminalState values"
            )


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return getattr(instance, "_terminal_mapping_capability", None) is token

    def bind(instance: object) -> None:
        object.__setattr__(instance, "_terminal_mapping_capability", token)

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskTerminalStateMapping:
    """Deterministic target settlement mapping for precommitted member states."""

    workspace_instance_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    terminal_population_sha256: str
    terminal_state_count: int
    terminal_space_exact: bool
    scenario_population_sha256: str
    member_ids: tuple[str, ...]
    member_scenario_ids: tuple[str, ...]
    member_mapping_sha256s: tuple[str, ...]
    member_settlement_json: tuple[str, ...]
    mapping_vector_sha256: str
    _terminal_mapping_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalRiskTerminalStateMapping":
        raise TypeError(
            "ProductProposalRiskTerminalStateMapping is product-derived; use "
            "resolve_product_proposal_risk_terminal_state_mapping"
        )

    @property
    def mapping_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def provider_terminal_population_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def fixed_n_scenario_precommit_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def terminal_mapping_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def product_scenario_source_provenance_proven(self) -> bool:
        return False

    @property
    def iid_member_sampling_proven(self) -> bool:
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


_RESULT_FIELDS = (
    "workspace_instance_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "terminal_population_sha256",
    "terminal_state_count",
    "terminal_space_exact",
    "scenario_population_sha256",
    "member_ids",
    "member_scenario_ids",
    "member_mapping_sha256s",
    "member_settlement_json",
    "mapping_vector_sha256",
)


def _require_precommit(precommit: object) -> ProductProposalRiskEvaluationPrecommit:
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
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
        raise ProductProposalRiskTerminalStateMappingError(
            "proposal risk precommit truth boundary is inconsistent"
        )
    return precommit


def _require_authorities(
    authorities: object,
) -> tuple[MarketSettlementOutcomeAuthority, ...]:
    if type(authorities) is not tuple or not authorities:
        raise ProductProposalRiskTerminalStateMappingError(
            "authorities must be a non-empty exact tuple"
        )
    if any(type(item) is not _AUTHORITY_TYPE for item in authorities):
        raise ProductProposalRiskTerminalStateMappingError(
            "authorities must contain exact MarketSettlementOutcomeAuthority values"
        )
    return authorities


def _resolve_target_and_terminal(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: object,
    authorities: object,
) -> tuple[
    ProductProposalRiskTarget,
    ProductProposalTargetTerminalPopulation,
    tuple[MarketSettlementOutcomeAuthority, ...],
]:
    _REQUIRE_DISPATCH_ORIGINAL()
    precommit = _require_precommit(precommit)
    if type(terminal_population) is not _TERMINAL_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal_population must be exact ProductProposalTargetTerminalPopulation"
        )
    authorities = _require_authorities(authorities)
    try:
        target = _TARGET_RESOLVER(workspace, precommit.target_sha256)
        fresh_terminal = _TERMINAL_RESOLVER(
            workspace,
            target_sha256=precommit.target_sha256,
            authorities=authorities,
        )
    except (
        ProductProposalRiskTargetError,
        ProductProposalTargetTerminalPopulationError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalStateMappingError(
            "target/provider terminal population cannot be re-resolved"
        ) from exc
    if (
        target.workspace_instance_id != precommit.workspace_instance_id
        or target.target_sha256 != precommit.target_sha256
        or target.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or target.evaluated_stakes != precommit.evaluated_stakes
        or fresh_terminal != terminal_population
        or fresh_terminal.population_identity_proven is not True
        or fresh_terminal.provider_terminal_authority_proven is not True
        or fresh_terminal.terminal_space_exhaustive is not True
        or fresh_terminal.probability_model_bound is not False
        or fresh_terminal.iid_member_mapping_proven is not False
        or fresh_terminal.proposal_target_counterfactual_execution_proven is not False
        or fresh_terminal.risk_upper_bound_for_target is not False
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "target/provider terminal parent identity changed"
        )
    return target, fresh_terminal, authorities


def _resolve_scenario_parent(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    scenario_population: object,
) -> ProductProposalRiskScenarioPopulation:
    if type(scenario_population) is not _SCENARIO_TYPE:
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario_population must be exact ProductProposalRiskScenarioPopulation"
        )
    try:
        fresh = _SCENARIO_RESOLVER(
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
            "scenario population cannot be re-resolved"
        ) from exc
    if (
        fresh != scenario_population
        or fresh.population_identity_proven is not True
        or fresh.fixed_n_member_mapping_complete is not True
        or fresh.provider_terminal_population_proven is not True
        or fresh.product_scenario_source_provenance_proven is not False
        or fresh.terminal_mapping_proven is not False
        or fresh.scenario_execution_proven is not False
        or fresh.proposal_target_counterfactual_execution_proven is not False
        or fresh.risk_upper_bound_for_target is not False
        or fresh.precommit_binding_sha256 != precommit.binding_sha256
        or fresh.terminal_population_sha256 != terminal_population.population_sha256
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "scenario population parent identity changed"
        )
    return fresh


def _group_authorities(
    terminal_population: ProductProposalTargetTerminalPopulation,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
) -> tuple[tuple[str, tuple[str, str, str, str], MarketSettlementOutcomeAuthority], ...]:
    if len(terminal_population.market_group_json) != len(
        terminal_population.market_group_sha256s
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal market-group material cardinality changed"
        )
    by_key: dict[
        tuple[str, str, str, str],
        list[MarketSettlementOutcomeAuthority],
    ] = {}
    for authority in authorities:
        by_key.setdefault(authority.identity.market_key, []).append(authority)

    result: list[
        tuple[str, tuple[str, str, str, str], MarketSettlementOutcomeAuthority]
    ] = []
    used: set[tuple[str, str, str, str]] = set()
    for index, raw_json in enumerate(terminal_population.market_group_json):
        raw = _parse_canonical_json(raw_json, f"market_group_json[{index}]")
        expected_keys = {
            "market_key",
            "authority_sha256s",
            "terminal_state_count",
            "terminal_space_exact",
            "market_group_sha256",
        }
        if set(raw) != expected_keys:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal market-group schema changed"
            )
        market_key_raw = raw["market_key"]
        if (
            type(market_key_raw) is not list
            or len(market_key_raw) != 4
            or any(type(value) is not str or not value for value in market_key_raw)
        ):
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal market-group key is invalid"
            )
        market_key = tuple(market_key_raw)
        providers = sorted(
            by_key.get(market_key, ()),
            key=lambda item: (item.identity.source_id, item.authority_sha256),
        )
        if not providers:
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal market group lacks reverified provider authority"
            )
        baseline = providers[0]
        expected_shas = [item.authority_sha256 for item in providers]
        if (
            raw["authority_sha256s"] != expected_shas
            or raw["terminal_state_count"] != baseline.terminal_state_count
            or raw["terminal_space_exact"] is not baseline.terminal_space_exact
            or raw["market_group_sha256"]
            != terminal_population.market_group_sha256s[index]
        ):
            raise ProductProposalRiskTerminalStateMappingError(
                "terminal market-group material changed"
            )
        used.add(market_key)
        result.append(
            (
                _sha(raw["market_group_sha256"], "market_group_sha256"),
                market_key,
                baseline,
            )
        )
    if used != set(by_key):
        raise ProductProposalRiskTerminalStateMappingError(
            "reverified authority set does not match terminal market groups"
        )
    return tuple(result)


def _leg_key(value: Mapping[str, object]) -> tuple[object, ...]:
    return (
        value.get("event_id"),
        value.get("market_id"),
        value.get("selection_id"),
        value.get("sport"),
        value.get("exchange_side"),
    )


def _derive_member(
    precommit: ProductProposalRiskEvaluationPrecommit,
    target: ProductProposalRiskTarget,
    terminal_population: ProductProposalTargetTerminalPopulation,
    groups: tuple[
        tuple[str, tuple[str, str, str, str], MarketSettlementOutcomeAuthority],
        ...,
    ],
    selection: CounterfactualMemberTerminalStateSelection,
) -> tuple[CounterfactualScenarioMemberBinding, str]:
    if len(selection.market_states) != len(groups):
        raise ProductProposalRiskTerminalStateMappingError(
            "member terminal-state vector must cover every terminal market group"
        )

    state_material: list[dict[str, object]] = []
    settlement_maps: list[dict[str, str]] = []
    group_index: dict[tuple[str, str, str, str], int] = {}
    for index, ((group_sha, market_key, authority), state) in enumerate(
        zip(groups, selection.market_states)
    ):
        try:
            settlements = _SETTLEMENT_BY_QUOTE(authority, state)
        except (TypeError, ValueError) as exc:
            raise ProductProposalRiskTerminalStateMappingError(
                f"member terminal state {index} is not derived from its provider authority"
            ) from exc
        state_material.append(
            {
                "market_group_sha256": group_sha,
                "state_id": _text(state.state_id, f"market_states[{index}].state_id"),
            }
        )
        settlement_maps.append(settlements)
        group_index[market_key] = index

    scenario_material = {
        "schema": _SCENARIO_SCHEMA,
        "terminal_population_sha256": terminal_population.population_sha256,
        "market_states": state_material,
    }
    scenario_id = _SCENARIO_PREFIX + _digest(scenario_material)

    if len(target.candidate_context_json) != len(target.candidate_sha256s):
        raise ProductProposalRiskTerminalStateMappingError(
            "target candidate context cardinality changed"
        )
    if len(precommit.evaluated_stakes) != len(target.candidate_sha256s):
        raise ProductProposalRiskTerminalStateMappingError(
            "target/precommit stake cardinality changed"
        )

    candidates: list[dict[str, object]] = []
    for candidate_index, raw_context in enumerate(target.candidate_context_json):
        context = _parse_canonical_json(
            raw_context,
            f"candidate_context_json[{candidate_index}]",
        )
        legs = context.get("legs")
        quotes = context.get("quotes")
        if type(legs) is not list or not legs or type(quotes) is not list or not quotes:
            raise ProductProposalRiskTerminalStateMappingError(
                "candidate context legs/quotes are invalid"
            )
        quotes_by_key: dict[tuple[object, ...], Mapping[str, object]] = {}
        for quote in quotes:
            if not isinstance(quote, Mapping):
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate quote is invalid"
                )
            key = _leg_key(quote)
            if key in quotes_by_key:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate quote identity is duplicated"
                )
            quotes_by_key[key] = quote

        mapped_legs: list[dict[str, object]] = []
        for leg_index, leg in enumerate(legs):
            if not isinstance(leg, Mapping):
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg is invalid"
                )
            if leg.get("exchange_side") is not None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "terminal mapping does not yet prove exchange-side settlement semantics"
                )
            quote = quotes_by_key.get(_leg_key(leg))
            if quote is None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg lacks exact quote evidence"
                )
            sport = leg.get("sport")
            event_id = leg.get("event_id")
            market_id = leg.get("market_id")
            selection_id = leg.get("selection_id")
            market_type = quote.get("market_type")
            if not all(
                type(value) is str and bool(value)
                for value in (sport, event_id, market_id, selection_id, market_type)
            ):
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate market identity is incomplete"
                )
            key = (sport, event_id, market_id, market_type)
            group_position = group_index.get(key)
            if group_position is None:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate leg has no terminal market group"
                )
            authority = groups[group_position][2]
            quote_key = authority.identity.quote_key(selection_id)
            result = settlement_maps[group_position].get(quote_key)
            if type(result) is not str or not result:
                raise ProductProposalRiskTerminalStateMappingError(
                    "candidate selection has no terminal settlement"
                )
            mapped_legs.append(
                {
                    "event_id": event_id,
                    "market_id": market_id,
                    "selection_id": selection_id,
                    "sport": sport,
                    "exchange_side": None,
                    "locked_odds": _text(
                        leg.get("locked_odds"),
                        f"candidate[{candidate_index}].leg[{leg_index}].locked_odds",
                    ),
                    "result": result,
                }
            )
        candidates.append(
            {
                "candidate_sha256": _sha(
                    target.candidate_sha256s[candidate_index],
                    "candidate_sha256",
                ),
                "evaluated_stake": _decimal_text(
                    precommit.evaluated_stakes[candidate_index],
                    "evaluated_stake",
                ),
                "legs": mapped_legs,
            }
        )

    mapping_material = {
        "schema": _MAPPING_SCHEMA,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": target.target_sha256,
        "candidate_vector_sha256": target.candidate_vector_sha256,
        "terminal_population_sha256": terminal_population.population_sha256,
        "scenario_id": scenario_id,
        "market_states": state_material,
        "candidates": candidates,
    }
    mapping_sha256 = _digest(mapping_material)
    return (
        CounterfactualScenarioMemberBinding(
            member_id=selection.member_id,
            scenario_id=scenario_id,
            mapping_sha256=mapping_sha256,
        ),
        _canonical_json(mapping_material),
    )


def _validate_selections(
    precommit: ProductProposalRiskEvaluationPrecommit,
    selections: object,
) -> tuple[CounterfactualMemberTerminalStateSelection, ...]:
    if type(selections) is not tuple or not selections:
        raise ProductProposalRiskTerminalStateMappingError(
            "selections must be a non-empty exact tuple"
        )
    if len(selections) != len(precommit.planned_member_ids):
        raise ProductProposalRiskTerminalStateMappingError(
            "member terminal selections must cover the exact fixed-N cohort"
        )
    for index, selection in enumerate(selections):
        if type(selection) is not CounterfactualMemberTerminalStateSelection:
            raise ProductProposalRiskTerminalStateMappingError(
                f"selections[{index}] must be exact CounterfactualMemberTerminalStateSelection"
            )
    member_ids = tuple(item.member_id for item in selections)
    if member_ids != precommit.planned_member_ids:
        raise ProductProposalRiskTerminalStateMappingError(
            "member terminal selections must follow exact precommitted member order"
        )
    return selections


def derive_product_proposal_risk_terminal_member_bindings(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    selections: tuple[CounterfactualMemberTerminalStateSelection, ...],
) -> tuple[CounterfactualScenarioMemberBinding, ...]:
    """Derive exact #2161 bindings from provider-verified terminal states.

    This proves mapping identity only. It does not prove how or why a member's
    terminal state was sampled.
    """

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal-state mapping dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    precommit = _require_precommit(precommit)
    selections = _validate_selections(precommit, selections)
    target, terminal_population, authorities = _resolve_target_and_terminal(
        workspace,
        precommit,
        terminal_population,
        authorities,
    )
    groups = _group_authorities(terminal_population, authorities)
    return tuple(
        _derive_member(
            precommit,
            target,
            terminal_population,
            groups,
            selection,
        )[0]
        for selection in selections
    )


def resolve_product_proposal_risk_terminal_state_mapping(
    workspace: Path,
    precommit: ProductProposalRiskEvaluationPrecommit,
    terminal_population: ProductProposalTargetTerminalPopulation,
    scenario_population: ProductProposalRiskScenarioPopulation,
    authorities: tuple[MarketSettlementOutcomeAuthority, ...],
    selections: tuple[CounterfactualMemberTerminalStateSelection, ...],
    *,
    _bind_identity=_BIND_IDENTITY,
) -> ProductProposalRiskTerminalStateMapping:
    """Re-derive exact target settlements for the durably precommitted member states."""

    if _require_dispatch is not _REQUIRE_DISPATCH_ORIGINAL:
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal-state mapping dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    precommit = _require_precommit(precommit)
    selections = _validate_selections(precommit, selections)
    target, terminal_population, authorities = _resolve_target_and_terminal(
        workspace,
        precommit,
        terminal_population,
        authorities,
    )
    scenario_population = _resolve_scenario_parent(
        workspace,
        precommit,
        terminal_population,
        scenario_population,
    )
    groups = _group_authorities(terminal_population, authorities)

    derived: list[tuple[CounterfactualScenarioMemberBinding, str]] = []
    for selection in selections:
        derived.append(
            _derive_member(
                precommit,
                target,
                terminal_population,
                groups,
                selection,
            )
        )
    bindings = tuple(item[0] for item in derived)
    if (
        tuple(item.member_id for item in bindings) != scenario_population.planned_member_ids
        or tuple(item.scenario_id for item in bindings)
        != scenario_population.member_scenario_ids
        or tuple(item.mapping_sha256 for item in bindings)
        != scenario_population.member_mapping_sha256s
    ):
        raise ProductProposalRiskTerminalStateMappingError(
            "terminal state vector does not match the precommitted member scenario mapping"
        )

    member_settlement_json = tuple(item[1] for item in derived)
    mapping_vector_sha256 = _digest(
        {
            "schema": _MAPPING_SCHEMA + ".vector",
            "scenario_population_sha256": scenario_population.population_sha256,
            "member_ids": list(precommit.planned_member_ids),
            "member_scenario_ids": list(scenario_population.member_scenario_ids),
            "member_mapping_sha256s": list(
                scenario_population.member_mapping_sha256s
            ),
        }
    )
    instance = object.__new__(ProductProposalRiskTerminalStateMapping)
    values = {
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": precommit.target_sha256,
        "candidate_vector_sha256": precommit.candidate_vector_sha256,
        "terminal_population_sha256": terminal_population.population_sha256,
        "terminal_state_count": terminal_population.terminal_state_count,
        "terminal_space_exact": terminal_population.terminal_space_exact,
        "scenario_population_sha256": scenario_population.population_sha256,
        "member_ids": precommit.planned_member_ids,
        "member_scenario_ids": scenario_population.member_scenario_ids,
        "member_mapping_sha256s": scenario_population.member_mapping_sha256s,
        "member_settlement_json": member_settlement_json,
        "mapping_vector_sha256": mapping_vector_sha256,
    }
    for name in _RESULT_FIELDS:
        object.__setattr__(instance, name, values[name])
    _bind_identity(instance)
    return instance


del _IDENTITY_PROVEN
del _BIND_IDENTITY


__all__ = [
    "CounterfactualMemberTerminalStateSelection",
    "ProductProposalRiskTerminalStateMapping",
    "ProductProposalRiskTerminalStateMappingError",
    "derive_product_proposal_risk_terminal_member_bindings",
    "resolve_product_proposal_risk_terminal_state_mapping",
]
