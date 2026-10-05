from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import paper as _paper_module
from . import portfolio as _portfolio_module
from .domain import PaperTicket, TicketLeg, TicketStatus
from .market_outcomes import (
    MarketSettlementOutcomeAuthority,
    MarketTerminalState,
    SettlementResult,
)
from .paper import PaperBook
from .portfolio import PortfolioEngine
from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from .proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    resolve_product_proposal_risk_target,
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


_SCHEMA = "autosport.proposal-risk-terminal-payoff-evaluation.v1"
_EVALUATION_ALGORITHM = (
    "portfolio-engine.scenario-profit-settlements+"
    "paperbook.settlement-result.v1"
)
_SCHEMA_EXPECTED = _SCHEMA
_EVALUATION_ALGORITHM_EXPECTED = _EVALUATION_ALGORITHM
_HEX = frozenset("0123456789abcdef")
_PATH_TYPE = type(Path("."))

_PRECOMMIT_TYPE = ProductProposalRiskEvaluationPrecommit
_TARGET_TYPE = ProductProposalRiskTarget
_TERMINAL_POPULATION_TYPE = ProductProposalTargetTerminalPopulation
_MAPPING_TYPE = ProductProposalRiskTerminalStateMapping
_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_TERMINAL_STATE_TYPE = MarketTerminalState
_SETTLEMENT_RESULT_TYPE = SettlementResult
_TICKET_LEG_TYPE = TicketLeg
_PAPER_TICKET_TYPE = PaperTicket
_TICKET_STATUS_TYPE = TicketStatus
_PORTFOLIO_ENGINE_TYPE = PortfolioEngine
_PAPER_BOOK_TYPE = PaperBook
_DECIMAL_TYPE = Decimal

_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_TERMINAL_POPULATION_RESOLVER = (
    resolve_product_proposal_target_terminal_population
)
_TERMINAL_POPULATION_RESOLVER_CODE = getattr(
    _TERMINAL_POPULATION_RESOLVER,
    "__code__",
    None,
)
_MAPPING_RESOLVER = resolve_product_proposal_risk_terminal_state_mapping
_MAPPING_RESOLVER_CODE = getattr(_MAPPING_RESOLVER, "__code__", None)

_AUTHORITY_SHA_GETTER = MarketSettlementOutcomeAuthority.authority_sha256.fget
_AUTHORITY_SHA_GETTER_CODE = getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
_AUTHORITY_TERMINAL_STATES_GETTER = (
    MarketSettlementOutcomeAuthority.terminal_states.fget
)
_AUTHORITY_TERMINAL_STATES_GETTER_CODE = getattr(
    _AUTHORITY_TERMINAL_STATES_GETTER,
    "__code__",
    None,
)
_TERMINAL_STATE_TO_DICT = MarketTerminalState.to_dict
_TERMINAL_STATE_TO_DICT_CODE = getattr(
    _TERMINAL_STATE_TO_DICT,
    "__code__",
    None,
)

_TICKET_LEG_INIT = TicketLeg.__init__
_TICKET_LEG_INIT_CODE = getattr(_TICKET_LEG_INIT, "__code__", None)
_TICKET_LEG_POST_INIT = TicketLeg.__post_init__
_TICKET_LEG_POST_INIT_CODE = getattr(
    _TICKET_LEG_POST_INIT,
    "__code__",
    None,
)
_TICKET_LEG_QUOTE_KEY = TicketLeg.__dict__["quote_key"]
_TICKET_LEG_QUOTE_KEY_GETTER = _TICKET_LEG_QUOTE_KEY.fget
_TICKET_LEG_QUOTE_KEY_GETTER_CODE = getattr(
    _TICKET_LEG_QUOTE_KEY_GETTER,
    "__code__",
    None,
)
_PAPER_TICKET_INIT = PaperTicket.__init__
_PAPER_TICKET_INIT_CODE = getattr(_PAPER_TICKET_INIT, "__code__", None)

_PORTFOLIO_SCENARIO_PROFIT = PortfolioEngine.scenario_profit_settlements
_PORTFOLIO_SCENARIO_PROFIT_CODE = getattr(
    _PORTFOLIO_SCENARIO_PROFIT,
    "__code__",
    None,
)
_PAPER_SETTLEMENT_DESCRIPTOR = PaperBook.__dict__["_settlement_result"]
_PAPER_SETTLEMENT_FUNCTION = _PAPER_SETTLEMENT_DESCRIPTOR.__func__
_PAPER_SETTLEMENT_FUNCTION_CODE = getattr(
    _PAPER_SETTLEMENT_FUNCTION,
    "__code__",
    None,
)
_PAPER_VALIDATE_LEG_DESCRIPTOR = PaperBook.__dict__["_validate_ticket_leg"]
_PAPER_VALIDATE_LEG_FUNCTION = getattr(
    _PAPER_VALIDATE_LEG_DESCRIPTOR,
    "__func__",
    _PAPER_VALIDATE_LEG_DESCRIPTOR,
)
_PAPER_VALIDATE_LEG_FUNCTION_CODE = getattr(
    _PAPER_VALIDATE_LEG_FUNCTION,
    "__code__",
    None,
)
_PAPER_REQUIRE_FINITE_DESCRIPTOR = PaperBook.__dict__["_require_finite"]
_PAPER_REQUIRE_FINITE_FUNCTION = getattr(
    _PAPER_REQUIRE_FINITE_DESCRIPTOR,
    "__func__",
    _PAPER_REQUIRE_FINITE_DESCRIPTOR,
)
_PAPER_REQUIRE_FINITE_FUNCTION_CODE = getattr(
    _PAPER_REQUIRE_FINITE_FUNCTION,
    "__code__",
    None,
)
_PAPER_REQUIRE_CANONICAL_TEXT_DESCRIPTOR = (
    PaperBook.__dict__["_require_canonical_text"]
)
_PAPER_REQUIRE_CANONICAL_TEXT_FUNCTION = getattr(
    _PAPER_REQUIRE_CANONICAL_TEXT_DESCRIPTOR,
    "__func__",
    _PAPER_REQUIRE_CANONICAL_TEXT_DESCRIPTOR,
)
_PAPER_REQUIRE_CANONICAL_TEXT_FUNCTION_CODE = getattr(
    _PAPER_REQUIRE_CANONICAL_TEXT_FUNCTION,
    "__code__",
    None,
)
_PAPER_REQUIRE_UTF8_DESCRIPTOR = PaperBook.__dict__["_require_utf8_string"]
_PAPER_REQUIRE_UTF8_FUNCTION = getattr(
    _PAPER_REQUIRE_UTF8_DESCRIPTOR,
    "__func__",
    _PAPER_REQUIRE_UTF8_DESCRIPTOR,
)
_PAPER_REQUIRE_UTF8_FUNCTION_CODE = getattr(
    _PAPER_REQUIRE_UTF8_FUNCTION,
    "__code__",
    None,
)

_PORTFOLIO_SNAPSHOT = _portfolio_module._snapshot_open_tickets_for_analysis
_PORTFOLIO_SNAPSHOT_CODE = getattr(_PORTFOLIO_SNAPSHOT, "__code__", None)
_PORTFOLIO_REQUIRE_FINITE = _portfolio_module._require_finite_decimal
_PORTFOLIO_REQUIRE_FINITE_CODE = getattr(
    _PORTFOLIO_REQUIRE_FINITE,
    "__code__",
    None,
)
_PORTFOLIO_ARITHMETIC_ERROR = _portfolio_module._portfolio_arithmetic_error
_PORTFOLIO_ARITHMETIC_ERROR_CODE = getattr(
    _PORTFOLIO_ARITHMETIC_ERROR,
    "__code__",
    None,
)
_PORTFOLIO_CONTEXT = _portfolio_module._PORTFOLIO_DECIMAL_CONTEXT
_PORTFOLIO_LOCALCONTEXT = _portfolio_module.localcontext
_PORTFOLIO_DECIMAL_EXCEPTION = _portfolio_module.DecimalException

_PAPER_DECIMAL_CONTEXT_FACTORY = _paper_module._paper_decimal_context
_PAPER_DECIMAL_CONTEXT_FACTORY_CODE = getattr(
    _PAPER_DECIMAL_CONTEXT_FACTORY,
    "__code__",
    None,
)
_PAPER_DECIMAL_PRECISION = _paper_module._PAPER_DECIMAL_PRECISION
_PAPER_DECIMAL_EMIN = _paper_module._PAPER_DECIMAL_EMIN
_PAPER_DECIMAL_EMAX = _paper_module._PAPER_DECIMAL_EMAX
_PAPER_ROUNDING = _paper_module.ROUND_HALF_EVEN
_PAPER_LOCALCONTEXT = _paper_module.localcontext
_PAPER_CONTEXT_TYPE = _paper_module.Context
_PAPER_INVALID_OPERATION = _paper_module.InvalidOperation
_PAPER_OVERFLOW = _paper_module.Overflow
_PAPER_UNDERFLOW = _paper_module.Underflow

_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_JSON_LOADS = json.loads
_JSON_LOADS_EXPECTED = _JSON_LOADS
_JSON_LOADS_CODE = getattr(_JSON_LOADS, "__code__", None)
_JSON_DECODE_ERROR = json.JSONDecodeError
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256


class ProductProposalRiskTerminalPayoffEvaluationError(RuntimeError):
    """Exact mapped terminal states cannot be evaluated economically safely."""


def _text(value: object, name: str, *, max_length: int = 2048) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _decimal_context_state(context: object) -> tuple[object, ...]:
    return (
        getattr(context, "prec", None),
        getattr(context, "rounding", None),
        getattr(context, "Emin", None),
        getattr(context, "Emax", None),
        getattr(context, "capitals", None),
        getattr(context, "clamp", None),
        tuple(
            sorted(
                (
                    getattr(signal, "__name__", repr(signal)),
                    bool(enabled),
                )
                for signal, enabled in getattr(context, "traps", {}).items()
            )
        ),
    )


_PORTFOLIO_CONTEXT_STATE = _decimal_context_state(_PORTFOLIO_CONTEXT)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is Decimal:
        result = value
    elif type(value) is str:
        text = _text(value, name, max_length=1024)
        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                f"{name} must be canonical Decimal text"
            ) from exc
    else:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} must be an exact Decimal or canonical Decimal text"
        )
    if type(result) is not Decimal or not result.is_finite():
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} must be finite"
        )
    return result


def _decimal_text(value: object, name: str) -> str:
    decimal = _decimal(value, name)
    if decimal.is_zero():
        return "0"
    text = format(decimal, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff canonical dispatch changed"
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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff material is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return _HASHLIB_SHA256(_canonical_bytes(value)).hexdigest()


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal payoff JSON contains duplicate keys"
            )
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise ProductProposalRiskTerminalPayoffEvaluationError(
        "terminal payoff JSON contains non-finite numbers"
    )


def _canonical_json_object(
    value: object,
    name: str,
    _json_loads_code=_JSON_LOADS_CODE,
) -> dict[str, object]:
    if (
        _JSON_LOADS is not _JSON_LOADS_EXPECTED
        or json.loads is not _JSON_LOADS_EXPECTED
        or getattr(_JSON_LOADS, "__code__", None) is not _json_loads_code
        or getattr(json.loads, "__code__", None) is not _json_loads_code
        or json.JSONDecodeError is not _JSON_DECODE_ERROR
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff JSON dispatch changed"
        )
    if type(value) is not str or not value:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} must be canonical JSON text"
        )
    try:
        parsed = _JSON_LOADS(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except (_JSON_DECODE_ERROR, UnicodeError) as exc:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} is invalid JSON"
        ) from exc
    if type(parsed) is not dict or _canonical_bytes(parsed).decode("utf-8") != value:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            f"{name} is not canonical JSON"
        )
    return parsed


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE or not value.is_absolute():
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != value or not value.is_dir() or value.is_symlink():
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "workspace must be the canonical non-symlink directory path"
        )
    return value


def _require_precommit(
    precommit: object,
) -> ProductProposalRiskEvaluationPrecommit:
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "precommit must be exact ProductProposalRiskEvaluationPrecommit"
        )
    if (
        precommit.binding_identity_proven is not True
        or precommit.proposal_target_identity_proven is not True
        or precommit.scientific_precommit_proven is not True
        or precommit.proposal_target_counterfactual_execution_proven is not False
        or precommit.risk_upper_bound_for_target is not False
        or precommit.grants_ticket_authority is not False
        or precommit.grants_real_money_authority is not False
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "proposal risk precommit truth boundary is inconsistent"
        )
    return precommit


def _require_mapping(
    precommit: ProductProposalRiskEvaluationPrecommit,
    mapping: object,
) -> ProductProposalRiskTerminalStateMapping:
    if type(mapping) is not _MAPPING_TYPE:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal mapping truth boundary does not match the exact precommit"
        )
    return mapping


def _require_target(
    precommit: ProductProposalRiskEvaluationPrecommit,
    target: object,
) -> ProductProposalRiskTarget:
    if type(target) is not _TARGET_TYPE:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "target resolver returned a non-canonical type"
        )
    if (
        target.proposal_target_identity_proven is not True
        or target.proposal_target_counterfactual_execution_proven is not False
        or target.risk_upper_bound_for_target is not False
        or target.grants_ticket_authority is not False
        or target.grants_real_money_authority is not False
        or target.workspace_instance_id != precommit.workspace_instance_id
        or target.target_sha256 != precommit.target_sha256
        or target.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or target.evaluated_stakes != precommit.evaluated_stakes
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "proposal target no longer matches the exact precommit"
        )
    return target


def _require_terminal_population(
    precommit: ProductProposalRiskEvaluationPrecommit,
    mapping: ProductProposalRiskTerminalStateMapping,
    population: object,
) -> ProductProposalTargetTerminalPopulation:
    if type(population) is not _TERMINAL_POPULATION_TYPE:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
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
        or population.candidate_vector_sha256
        != precommit.candidate_vector_sha256
        or population.population_sha256 != mapping.terminal_population_sha256
        or population.market_group_sha256s != mapping.market_group_sha256s
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal population does not match the exact mapped target"
        )
    return population


@dataclass(frozen=True, slots=True)
class _TerminalGroup:
    market_group_sha256: str
    authority_sha256s: tuple[str, ...]
    terminal_space_exact: bool


def _authority_map(
    authorities: object,
) -> dict[str, MarketSettlementOutcomeAuthority]:
    if type(authorities) is not tuple or not authorities:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "authorities must be a non-empty exact tuple"
        )
    result: dict[str, MarketSettlementOutcomeAuthority] = {}
    for index, authority in enumerate(authorities):
        if type(authority) is not _AUTHORITY_TYPE:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                f"authorities[{index}] must be exact "
                "MarketSettlementOutcomeAuthority"
            )
        digest = _sha(
            _AUTHORITY_SHA_GETTER(authority),
            f"authorities[{index}].authority_sha256",
        )
        if digest in result:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "authorities contain a duplicate authority digest"
            )
        result[digest] = authority
    return result


def _terminal_groups(
    population: ProductProposalTargetTerminalPopulation,
    authority_by_sha: dict[str, MarketSettlementOutcomeAuthority],
) -> tuple[_TerminalGroup, ...]:
    if set(authority_by_sha) != set(population.market_authority_sha256s):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "reverified authority set differs from terminal population"
        )
    if len(population.market_group_json) != len(
        population.market_group_sha256s
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal population group serialization is inconsistent"
        )

    groups: list[_TerminalGroup] = []
    seen: set[str] = set()
    for index, raw in enumerate(population.market_group_json):
        parsed = _canonical_json_object(
            raw,
            f"market_group_json[{index}]",
        )
        expected = {
            "market_key",
            "authority_sha256s",
            "terminal_state_count",
            "terminal_space_exact",
            "market_group_sha256",
        }
        if set(parsed) != expected:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal population market-group schema changed"
            )
        group_sha = _sha(
            parsed["market_group_sha256"],
            f"market_group_json[{index}].market_group_sha256",
        )
        if group_sha != population.market_group_sha256s[index]:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal population market-group order changed"
            )
        authorities_raw = parsed["authority_sha256s"]
        if type(authorities_raw) is not list or not authorities_raw:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal market group has no provider authorities"
            )
        authority_sha256s = tuple(
            _sha(value, "market-group authority sha256")
            for value in authorities_raw
        )
        if tuple(sorted(authority_sha256s)) != authority_sha256s:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal market-group authority order is non-canonical"
            )
        if any(value in seen for value in authority_sha256s):
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal authority appears in more than one market group"
            )
        if any(value not in authority_by_sha for value in authority_sha256s):
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal market group references an unverified authority"
            )
        terminal_space_exact = parsed["terminal_space_exact"]
        if type(terminal_space_exact) is not bool:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal market-group exactness is not canonical bool"
            )
        material = {
            "market_key": parsed["market_key"],
            "authority_sha256s": authorities_raw,
            "terminal_state_count": parsed["terminal_state_count"],
            "terminal_space_exact": parsed["terminal_space_exact"],
        }
        if _digest(material) != group_sha:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal market-group digest does not re-derive"
            )
        seen.update(authority_sha256s)
        groups.append(
            _TerminalGroup(
                market_group_sha256=group_sha,
                authority_sha256s=authority_sha256s,
                terminal_space_exact=terminal_space_exact,
            )
        )
    if seen != set(authority_by_sha):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal groups do not cover the reverified authority set"
        )
    return tuple(groups)


def _state_for_authority(
    authority: MarketSettlementOutcomeAuthority,
    state_id: str,
) -> MarketTerminalState:
    states = _AUTHORITY_TERMINAL_STATES_GETTER(authority)
    if type(states) is not tuple or not states:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal authority returned no canonical states"
        )
    selected: MarketTerminalState | None = None
    for state in states:
        if type(state) is not _TERMINAL_STATE_TYPE:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal authority returned a non-canonical state type"
            )
        if state.state_id == state_id:
            if selected is not None:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "terminal authority returned duplicate requested state_id"
                )
            selected = state
    if selected is None:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "mapped terminal state no longer exists in provider authority"
        )
    return selected


def _candidate_tickets(
    target: ProductProposalRiskTarget,
) -> tuple[PaperTicket, ...]:
    if (
        len(target.candidate_context_json) != len(target.candidate_sha256s)
        or len(target.candidate_context_json) != len(target.evaluated_stakes)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "target candidate/context/stake cardinality changed"
        )

    tickets: list[PaperTicket] = []
    for index, raw in enumerate(target.candidate_context_json):
        context = _canonical_json_object(
            raw,
            f"candidate_context_json[{index}]",
        )
        expected = {
            "legs",
            "quotes",
            "provider_accounts",
            "bankroll_id",
            "currency",
            "measurement_window_start",
            "measurement_window_end",
            "proposal_ts",
        }
        if set(context) != expected:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "proposal target candidate context schema changed"
            )
        legs_raw = context["legs"]
        if type(legs_raw) is not list or not legs_raw:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "proposal target candidate has no canonical legs"
            )
        legs: list[TicketLeg] = []
        for leg_index, leg_raw in enumerate(legs_raw):
            if type(leg_raw) is not dict or set(leg_raw) != {
                "event_id",
                "market_id",
                "selection_id",
                "locked_odds",
                "sport",
                "exchange_side",
            }:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "proposal target TicketLeg schema changed"
                )
            if leg_raw["exchange_side"] is not None:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "terminal payoff does not support exchange-side settlement "
                    "semantics"
                )
            try:
                leg = _TICKET_LEG_TYPE(
                    event_id=_text(
                        leg_raw["event_id"],
                        f"candidate[{index}].leg[{leg_index}].event_id",
                    ),
                    market_id=_text(
                        leg_raw["market_id"],
                        f"candidate[{index}].leg[{leg_index}].market_id",
                    ),
                    selection_id=_text(
                        leg_raw["selection_id"],
                        f"candidate[{index}].leg[{leg_index}].selection_id",
                    ),
                    locked_odds=_decimal(
                        leg_raw["locked_odds"],
                        f"candidate[{index}].leg[{leg_index}].locked_odds",
                    ),
                    sport=leg_raw["sport"],
                    exchange_side=leg_raw["exchange_side"],
                )
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "proposal target TicketLeg cannot be reconstructed"
                ) from exc
            try:
                _PAPER_VALIDATE_LEG_FUNCTION(
                    _PAPER_BOOK_TYPE,
                    leg,
                    ticket_id=(
                        "proposal-terminal-payoff:"
                        + target.candidate_sha256s[index]
                    ),
                )
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "proposal target TicketLeg is outside canonical "
                    "PaperBook settlement economics"
                ) from exc
            legs.append(leg)

        quote_keys = tuple(
            _TICKET_LEG_QUOTE_KEY_GETTER(leg) for leg in legs
        )
        if len(quote_keys) != len(set(quote_keys)):
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "proposal target candidate contains duplicate quote_key legs"
            )
        stake = _decimal(
            target.evaluated_stakes[index],
            f"evaluated_stakes[{index}]",
        )
        if stake < 0:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "proposal target evaluated stake cannot be negative"
            )
        try:
            ticket = _PAPER_TICKET_TYPE(
                ticket_id=(
                    "proposal-terminal-payoff:"
                    + target.candidate_sha256s[index]
                ),
                stake=stake,
                legs=tuple(legs),
                placed_at=target.decision_ts,
                status=_TICKET_STATUS_TYPE.OPEN,
                provider_source_ids=(),
                provider_accounts=(),
                bankroll_id=target.bankroll_id,
                currency=target.currency,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "proposal target PaperTicket cannot be reconstructed"
            ) from exc
        tickets.append(ticket)
    return tuple(tickets)


def _authority_group_index(
    groups: tuple[_TerminalGroup, ...],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for group_index, group in enumerate(groups):
        for authority_sha256 in group.authority_sha256s:
            if authority_sha256 in result:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "terminal authority group mapping is duplicated"
                )
            result[authority_sha256] = group_index
    return result


def _settlement_map_for_candidate(
    *,
    candidate_index: int,
    ticket: PaperTicket,
    population: ProductProposalTargetTerminalPopulation,
    authority_by_sha: dict[str, MarketSettlementOutcomeAuthority],
    authority_group_index: dict[str, int],
    member_state_ids: tuple[str, ...],
) -> dict[str, str]:
    authority_vector = population.candidate_market_authority_sha256s[
        candidate_index
    ]
    if (
        type(authority_vector) is not tuple
        or len(authority_vector) != len(ticket.legs)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "candidate terminal-authority vector no longer matches ticket legs"
        )

    settlement_by_quote: dict[str, str] = {}
    for leg_index, (leg, authority_sha_raw) in enumerate(
        zip(ticket.legs, authority_vector)
    ):
        authority_sha = _sha(
            authority_sha_raw,
            (
                f"candidate_market_authority_sha256s[{candidate_index}]"
                f"[{leg_index}]"
            ),
        )
        authority = authority_by_sha.get(authority_sha)
        group_index = authority_group_index.get(authority_sha)
        if authority is None or group_index is None:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "candidate leg terminal authority is not reverified"
            )
        if group_index >= len(member_state_ids):
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "member terminal-state vector lost market-group cardinality"
            )
        state_id = _text(
            member_state_ids[group_index],
            f"member_state_ids[{group_index}]",
        )
        state = _state_for_authority(authority, state_id)
        settlement_by_selection = dict(state.settlements)
        result = settlement_by_selection.get(leg.selection_id)
        if type(result) is not _SETTLEMENT_RESULT_TYPE:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "mapped terminal state does not settle the target selection"
            )
        quote_key = _TICKET_LEG_QUOTE_KEY_GETTER(leg)
        result_text = result.value
        prior = settlement_by_quote.get(quote_key)
        if prior is not None and prior != result_text:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "one target quote_key has conflicting terminal settlements"
            )
        settlement_by_quote[quote_key] = result_text
    return settlement_by_quote


def _scenario_payoff(
    *,
    tickets: tuple[PaperTicket, ...],
    population: ProductProposalTargetTerminalPopulation,
    groups: tuple[_TerminalGroup, ...],
    authority_by_sha: dict[str, MarketSettlementOutcomeAuthority],
    member_state_ids: tuple[str, ...],
) -> tuple[tuple[Decimal, ...], Decimal]:
    authority_vectors = population.candidate_market_authority_sha256s
    if (
        type(tickets) is not tuple
        or type(authority_vectors) is not tuple
        or not tickets
        or len(tickets) != len(authority_vectors)
        or any(type(ticket) is not _PAPER_TICKET_TYPE for ticket in tickets)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff candidate vector no longer matches "
            "the provider-authority population"
        )
    if (
        type(member_state_ids) is not tuple
        or len(member_state_ids) != len(groups)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "member terminal-state vector must match market-group cardinality"
        )
    group_index = _authority_group_index(groups)
    candidate_profits: list[Decimal] = []
    combined_settlements: dict[str, str] = {}
    economic_tickets: list[PaperTicket] = []

    for candidate_index, ticket in enumerate(tickets):
        settlement_map = _settlement_map_for_candidate(
            candidate_index=candidate_index,
            ticket=ticket,
            population=population,
            authority_by_sha=authority_by_sha,
            authority_group_index=group_index,
            member_state_ids=member_state_ids,
        )
        if ticket.stake.is_zero():
            candidate_profit = Decimal("0")
        else:
            try:
                candidate_profit = _PORTFOLIO_SCENARIO_PROFIT(
                    [ticket],
                    settlement_map,
                )
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "canonical candidate settlement arithmetic failed"
                ) from exc
            economic_tickets.append(ticket)
        candidate_profits.append(
            _decimal(candidate_profit, "candidate terminal profit")
        )
        for quote_key, result in settlement_map.items():
            prior = combined_settlements.get(quote_key)
            if prior is not None and prior != result:
                raise ProductProposalRiskTerminalPayoffEvaluationError(
                    "target candidates disagree on one terminal settlement"
                )
            combined_settlements[quote_key] = result

    try:
        total_profit = _PORTFOLIO_SCENARIO_PROFIT(
            economic_tickets,
            combined_settlements,
        )
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "canonical target-vector settlement arithmetic failed"
        ) from exc
    return (
        tuple(candidate_profits),
        _decimal(total_profit, "member total terminal profit"),
    )


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_terminal_payoff_identity_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(
            instance,
            "_terminal_payoff_identity_capability",
            token,
        )

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskTerminalPayoffEvaluation:
    """Exact terminal P&L arithmetic for the precommitted proposal members.

    Positive truth is deliberately limited to deterministic terminal settlement
    arithmetic. This artifact does not prove how member scenarios were sampled,
    joint support, path chronology/minimum equity, realized costs/slippage, net
    execution P&L, experiment execution, a risk bound, ticket permission, provider
    execution or money movement.
    """

    workspace_instance_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    candidate_sha256s: tuple[str, ...]
    evaluated_stakes: tuple[Decimal, ...]
    settlement_evaluator_protocol: str
    terminal_population_sha256: str
    scenario_population_sha256: str
    terminal_mapping_resolution_sha256: str
    per_market_terminal_space_exact: bool
    joint_terminal_space_exact: bool
    planned_member_ids: tuple[str, ...]
    member_scenario_ids: tuple[str, ...]
    member_mapping_sha256s: tuple[str, ...]
    member_state_vector_sha256s: tuple[str, ...]
    member_candidate_paper_profit_vectors: tuple[tuple[Decimal, ...], ...]
    member_paper_terminal_profits: tuple[Decimal, ...]
    evaluation_sha256: str
    _terminal_payoff_identity_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductProposalRiskTerminalPayoffEvaluation":
        raise TypeError(
            "ProductProposalRiskTerminalPayoffEvaluation is product-resolved; "
            "use resolve_product_proposal_risk_terminal_payoff_evaluation"
        )

    @property
    def evaluation_identity_proven(
        self,
        _proven=_IDENTITY_PROVEN,
    ) -> bool:
        return _proven(self)

    @property
    def terminal_mapping_consumed_proven(
        self,
        _proven=_IDENTITY_PROVEN,
    ) -> bool:
        return _proven(self)

    @property
    def paperbook_settlement_arithmetic_proven(
        self,
        _proven=_IDENTITY_PROVEN,
    ) -> bool:
        return _proven(self)

    @property
    def fixed_n_member_payoff_complete(
        self,
        _proven=_IDENTITY_PROVEN,
    ) -> bool:
        return _proven(self)

    @property
    def target_terminal_payoff_evaluation_proven(
        self,
        _proven=_IDENTITY_PROVEN,
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


_RESULT_TYPE = ProductProposalRiskTerminalPayoffEvaluation
_RESULT_TYPE_EXPECTED = _RESULT_TYPE
_RESULT_FIELDS = (
    "workspace_instance_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "candidate_sha256s",
    "evaluated_stakes",
    "settlement_evaluator_protocol",
    "terminal_population_sha256",
    "scenario_population_sha256",
    "terminal_mapping_resolution_sha256",
    "per_market_terminal_space_exact",
    "joint_terminal_space_exact",
    "planned_member_ids",
    "member_scenario_ids",
    "member_mapping_sha256s",
    "member_state_vector_sha256s",
    "member_candidate_paper_profit_vectors",
    "member_paper_terminal_profits",
    "evaluation_sha256",
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
    "evaluation_identity_proven",
    "terminal_mapping_consumed_proven",
    "paperbook_settlement_arithmetic_proven",
    "fixed_n_member_payoff_complete",
    "target_terminal_payoff_evaluation_proven",
    "product_scenario_source_provenance_proven",
    "iid_member_mapping_proven",
    "joint_scenario_support_proven",
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
_RESULT_AUTHORITY_PROPERTY_NAMES_EXPECTED = (
    _RESULT_AUTHORITY_PROPERTY_NAMES
)
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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff dispatch guard root changed"
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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal mapping cannot be re-resolved"
        ) from exc
    mapping = _require_mapping(precommit, mapping)

    try:
        target = _TARGET_RESOLVER(
            workspace,
            precommit.target_sha256,
        )
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "proposal target cannot be re-resolved"
        ) from exc
    target = _require_target(precommit, target)

    try:
        population = _TERMINAL_POPULATION_RESOLVER(
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
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal population cannot be re-resolved"
        ) from exc
    population = _require_terminal_population(
        precommit,
        mapping,
        population,
    )

    authority_by_sha = _authority_map(authorities)
    groups = _terminal_groups(population, authority_by_sha)
    per_market_terminal_space_exact = all(
        group.terminal_space_exact for group in groups
    )
    if per_market_terminal_space_exact is not mapping.per_market_terminal_space_exact:
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff exactness differs from mapped terminal authority"
        )
    tickets = _candidate_tickets(target)

    if (
        type(member_market_state_ids) is not tuple
        or len(member_market_state_ids) != len(mapping.planned_member_ids)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "member terminal-state vectors must contain the exact fixed-N cohort"
        )

    candidate_profit_vectors: list[tuple[Decimal, ...]] = []
    total_profits: list[Decimal] = []
    for member_index, state_ids in enumerate(member_market_state_ids):
        if type(state_ids) is not tuple:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                f"member_market_state_ids[{member_index}] "
                "must be an exact tuple"
            )
        candidate_profits, total_profit = _scenario_payoff(
            tickets=tickets,
            population=population,
            groups=groups,
            authority_by_sha=authority_by_sha,
            member_state_ids=state_ids,
        )
        if len(candidate_profits) != len(target.candidate_sha256s):
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "candidate payoff vector lost target cardinality"
            )
        candidate_profit_vectors.append(candidate_profits)
        total_profits.append(total_profit)

    if (
        len(candidate_profit_vectors) != len(mapping.planned_member_ids)
        or len(total_profits) != len(mapping.planned_member_ids)
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff evaluation lost fixed-N member cardinality"
        )

    material = {
        "schema": _SCHEMA,
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": target.target_sha256,
        "candidate_vector_sha256": target.candidate_vector_sha256,
        "candidate_sha256s": list(target.candidate_sha256s),
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake")
            for value in target.evaluated_stakes
        ],
        "settlement_evaluator_protocol": _EVALUATION_ALGORITHM,
        "terminal_population_sha256": population.population_sha256,
        "scenario_population_sha256": mapping.scenario_population_sha256,
        "terminal_mapping_resolution_sha256": mapping.resolution_sha256,
        "per_market_terminal_space_exact": per_market_terminal_space_exact,
        "joint_terminal_space_exact": mapping.joint_terminal_space_exact,
        "planned_member_ids": list(mapping.planned_member_ids),
        "members": [
            {
                "member_id": member_id,
                "scenario_id": scenario_id,
                "mapping_sha256": mapping_sha256,
                "state_vector_sha256": state_vector_sha256,
                "candidate_paper_terminal_profits": [
                    _decimal_text(
                        profit,
                        "candidate terminal profit",
                    )
                    for profit in candidate_profits
                ],
                "paper_terminal_profit": _decimal_text(
                    total_profit,
                    "member terminal total profit",
                ),
            }
            for (
                member_id,
                scenario_id,
                mapping_sha256,
                state_vector_sha256,
                candidate_profits,
                total_profit,
            ) in zip(
                mapping.planned_member_ids,
                mapping.member_scenario_ids,
                mapping.member_mapping_sha256s,
                mapping.member_state_vector_sha256s,
                candidate_profit_vectors,
                total_profits,
            )
        ],
        "terminal_mapping_consumed_proven": True,
        "paperbook_settlement_arithmetic_proven": True,
        "fixed_n_member_payoff_complete": True,
        "target_terminal_payoff_evaluation_proven": True,
        "product_scenario_source_provenance_proven": False,
        "iid_member_mapping_proven": False,
        "joint_scenario_support_proven": False,
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
        "target_sha256": target.target_sha256,
        "candidate_vector_sha256": target.candidate_vector_sha256,
        "candidate_sha256s": target.candidate_sha256s,
        "evaluated_stakes": target.evaluated_stakes,
        "settlement_evaluator_protocol": _EVALUATION_ALGORITHM,
        "terminal_population_sha256": population.population_sha256,
        "scenario_population_sha256": mapping.scenario_population_sha256,
        "terminal_mapping_resolution_sha256": mapping.resolution_sha256,
        "per_market_terminal_space_exact": per_market_terminal_space_exact,
        "joint_terminal_space_exact": mapping.joint_terminal_space_exact,
        "planned_member_ids": mapping.planned_member_ids,
        "member_scenario_ids": mapping.member_scenario_ids,
        "member_mapping_sha256s": mapping.member_mapping_sha256s,
        "member_state_vector_sha256s": mapping.member_state_vector_sha256s,
        "member_candidate_paper_profit_vectors": tuple(candidate_profit_vectors),
        "member_paper_terminal_profits": tuple(total_profits),
        "evaluation_sha256": _digest(material),
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
    ) -> ProductProposalRiskTerminalPayoffEvaluation:
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
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal payoff public resolver closure changed"
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
        "resolve_product_proposal_risk_terminal_payoff_evaluation"
    )
    resolver.__qualname__ = resolver.__name__
    resolver.__doc__ = (
        "Resolve exact terminal target payoffs without granting experiment "
        "execution or risk authority."
    )
    return resolver


resolve_product_proposal_risk_terminal_payoff_evaluation = (
    _make_public_resolver(
        _BIND_IDENTITY,
        _resolve_values,
        _RESULT_TYPE,
        _RESULT_FIELDS_EXPECTED,
    )
)
_PUBLIC_RESOLVER = resolve_product_proposal_risk_terminal_payoff_evaluation
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
        or _EVALUATION_ALGORITHM != _EVALUATION_ALGORITHM_EXPECTED
        or ProductProposalRiskEvaluationPrecommit is not _PRECOMMIT_TYPE
        or ProductProposalRiskTarget is not _TARGET_TYPE
        or ProductProposalTargetTerminalPopulation
        is not _TERMINAL_POPULATION_TYPE
        or ProductProposalRiskTerminalStateMapping is not _MAPPING_TYPE
        or MarketSettlementOutcomeAuthority is not _AUTHORITY_TYPE
        or MarketTerminalState is not _TERMINAL_STATE_TYPE
        or SettlementResult is not _SETTLEMENT_RESULT_TYPE
        or TicketLeg is not _TICKET_LEG_TYPE
        or PaperTicket is not _PAPER_TICKET_TYPE
        or TicketStatus is not _TICKET_STATUS_TYPE
        or PortfolioEngine is not _PORTFOLIO_ENGINE_TYPE
        or PaperBook is not _PAPER_BOOK_TYPE
        or resolve_product_proposal_risk_target is not _TARGET_RESOLVER
        or getattr(_TARGET_RESOLVER, "__code__", None)
        is not _TARGET_RESOLVER_CODE
        or resolve_product_proposal_target_terminal_population
        is not _TERMINAL_POPULATION_RESOLVER
        or getattr(_TERMINAL_POPULATION_RESOLVER, "__code__", None)
        is not _TERMINAL_POPULATION_RESOLVER_CODE
        or resolve_product_proposal_risk_terminal_state_mapping
        is not _MAPPING_RESOLVER
        or getattr(_MAPPING_RESOLVER, "__code__", None)
        is not _MAPPING_RESOLVER_CODE
        or MarketSettlementOutcomeAuthority.authority_sha256.fget
        is not _AUTHORITY_SHA_GETTER
        or getattr(_AUTHORITY_SHA_GETTER, "__code__", None)
        is not _AUTHORITY_SHA_GETTER_CODE
        or MarketSettlementOutcomeAuthority.terminal_states.fget
        is not _AUTHORITY_TERMINAL_STATES_GETTER
        or getattr(_AUTHORITY_TERMINAL_STATES_GETTER, "__code__", None)
        is not _AUTHORITY_TERMINAL_STATES_GETTER_CODE
        or MarketTerminalState.to_dict is not _TERMINAL_STATE_TO_DICT
        or getattr(_TERMINAL_STATE_TO_DICT, "__code__", None)
        is not _TERMINAL_STATE_TO_DICT_CODE
        or TicketLeg.__init__ is not _TICKET_LEG_INIT
        or getattr(_TICKET_LEG_INIT, "__code__", None)
        is not _TICKET_LEG_INIT_CODE
        or TicketLeg.__post_init__ is not _TICKET_LEG_POST_INIT
        or getattr(_TICKET_LEG_POST_INIT, "__code__", None)
        is not _TICKET_LEG_POST_INIT_CODE
        or TicketLeg.__dict__.get("quote_key")
        is not _TICKET_LEG_QUOTE_KEY
        or getattr(_TICKET_LEG_QUOTE_KEY.fget, "__code__", None)
        is not _TICKET_LEG_QUOTE_KEY_GETTER_CODE
        or PaperTicket.__init__ is not _PAPER_TICKET_INIT
        or getattr(_PAPER_TICKET_INIT, "__code__", None)
        is not _PAPER_TICKET_INIT_CODE
        or PortfolioEngine.scenario_profit_settlements
        is not _PORTFOLIO_SCENARIO_PROFIT
        or getattr(_PORTFOLIO_SCENARIO_PROFIT, "__code__", None)
        is not _PORTFOLIO_SCENARIO_PROFIT_CODE
        or PaperBook.__dict__.get("_settlement_result")
        is not _PAPER_SETTLEMENT_DESCRIPTOR
        or getattr(
            _PAPER_SETTLEMENT_DESCRIPTOR.__func__,
            "__code__",
            None,
        )
        is not _PAPER_SETTLEMENT_FUNCTION_CODE
        or PaperBook.__dict__.get("_validate_ticket_leg")
        is not _PAPER_VALIDATE_LEG_DESCRIPTOR
        or getattr(
            getattr(
                _PAPER_VALIDATE_LEG_DESCRIPTOR,
                "__func__",
                _PAPER_VALIDATE_LEG_DESCRIPTOR,
            ),
            "__code__",
            None,
        )
        is not _PAPER_VALIDATE_LEG_FUNCTION_CODE
        or PaperBook.__dict__.get("_require_finite")
        is not _PAPER_REQUIRE_FINITE_DESCRIPTOR
        or getattr(
            getattr(
                _PAPER_REQUIRE_FINITE_DESCRIPTOR,
                "__func__",
                _PAPER_REQUIRE_FINITE_DESCRIPTOR,
            ),
            "__code__",
            None,
        )
        is not _PAPER_REQUIRE_FINITE_FUNCTION_CODE
        or PaperBook.__dict__.get("_require_canonical_text")
        is not _PAPER_REQUIRE_CANONICAL_TEXT_DESCRIPTOR
        or getattr(
            getattr(
                _PAPER_REQUIRE_CANONICAL_TEXT_DESCRIPTOR,
                "__func__",
                _PAPER_REQUIRE_CANONICAL_TEXT_DESCRIPTOR,
            ),
            "__code__",
            None,
        )
        is not _PAPER_REQUIRE_CANONICAL_TEXT_FUNCTION_CODE
        or PaperBook.__dict__.get("_require_utf8_string")
        is not _PAPER_REQUIRE_UTF8_DESCRIPTOR
        or getattr(
            getattr(
                _PAPER_REQUIRE_UTF8_DESCRIPTOR,
                "__func__",
                _PAPER_REQUIRE_UTF8_DESCRIPTOR,
            ),
            "__code__",
            None,
        )
        is not _PAPER_REQUIRE_UTF8_FUNCTION_CODE
        or _portfolio_module.PaperBook is not _PAPER_BOOK_TYPE
        or _portfolio_module.PaperTicket is not _PAPER_TICKET_TYPE
        or _portfolio_module.TicketStatus is not _TICKET_STATUS_TYPE
        or _portfolio_module.Decimal is not _DECIMAL_TYPE
        or _portfolio_module.localcontext is not _PORTFOLIO_LOCALCONTEXT
        or _portfolio_module.DecimalException
        is not _PORTFOLIO_DECIMAL_EXCEPTION
        or _portfolio_module._snapshot_open_tickets_for_analysis
        is not _PORTFOLIO_SNAPSHOT
        or getattr(_PORTFOLIO_SNAPSHOT, "__code__", None)
        is not _PORTFOLIO_SNAPSHOT_CODE
        or _portfolio_module._require_finite_decimal
        is not _PORTFOLIO_REQUIRE_FINITE
        or getattr(_PORTFOLIO_REQUIRE_FINITE, "__code__", None)
        is not _PORTFOLIO_REQUIRE_FINITE_CODE
        or _portfolio_module._portfolio_arithmetic_error
        is not _PORTFOLIO_ARITHMETIC_ERROR
        or getattr(_PORTFOLIO_ARITHMETIC_ERROR, "__code__", None)
        is not _PORTFOLIO_ARITHMETIC_ERROR_CODE
        or _portfolio_module._PORTFOLIO_DECIMAL_CONTEXT
        is not _PORTFOLIO_CONTEXT
        or _decimal_context_state(_PORTFOLIO_CONTEXT)
        != _PORTFOLIO_CONTEXT_STATE
        or _paper_module.PaperBook is not _PAPER_BOOK_TYPE
        or _paper_module.PaperTicket is not _PAPER_TICKET_TYPE
        or _paper_module.TicketLeg is not _TICKET_LEG_TYPE
        or _paper_module.TicketStatus is not _TICKET_STATUS_TYPE
        or _paper_module.Decimal is not _DECIMAL_TYPE
        or _paper_module.localcontext is not _PAPER_LOCALCONTEXT
        or _paper_module._paper_decimal_context
        is not _PAPER_DECIMAL_CONTEXT_FACTORY
        or getattr(_PAPER_DECIMAL_CONTEXT_FACTORY, "__code__", None)
        is not _PAPER_DECIMAL_CONTEXT_FACTORY_CODE
        or _paper_module._PAPER_DECIMAL_PRECISION
        != _PAPER_DECIMAL_PRECISION
        or _paper_module._PAPER_DECIMAL_EMIN != _PAPER_DECIMAL_EMIN
        or _paper_module._PAPER_DECIMAL_EMAX != _PAPER_DECIMAL_EMAX
        or _paper_module.ROUND_HALF_EVEN != _PAPER_ROUNDING
        or _paper_module.Context is not _PAPER_CONTEXT_TYPE
        or _paper_module.InvalidOperation is not _PAPER_INVALID_OPERATION
        or _paper_module.Overflow is not _PAPER_OVERFLOW
        or _paper_module.Underflow is not _PAPER_UNDERFLOW
        or _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS, "__code__", None) is not _JSON_DUMPS_CODE
        or getattr(json.dumps, "__code__", None) is not _JSON_DUMPS_CODE
        or _JSON_LOADS is not _JSON_LOADS_EXPECTED
        or json.loads is not _JSON_LOADS_EXPECTED
        or getattr(_JSON_LOADS, "__code__", None) is not _JSON_LOADS_CODE
        or getattr(json.loads, "__code__", None) is not _JSON_LOADS_CODE
        or json.JSONDecodeError is not _JSON_DECODE_ERROR
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
        or _RESULT_TYPE is not _RESULT_TYPE_EXPECTED
        or _RESULT_FIELDS is not _RESULT_FIELDS_EXPECTED
        or _RESULT_FIELD_DESCRIPTOR_WITNESSES
        is not _RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED
        or _RESULT_AUTHORITY_PROPERTY_NAMES
        is not _RESULT_AUTHORITY_PROPERTY_NAMES_EXPECTED
        or ProductProposalRiskTerminalPayoffEvaluation is not _RESULT_TYPE_EXPECTED
        or _PUBLIC_RESOLVER_CLOSURE_WITNESSES
        is not _PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED
        or _HELPER_WITNESSES is not _HELPER_WITNESSES_EXPECTED
        or type(_HELPER_WITNESSES_EXPECTED) is not tuple
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff dispatch root changed"
        )

    for name, expected_descriptor in _RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED:
        if _RESULT_TYPE.__dict__.get(name) is not expected_descriptor:
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal payoff result field surface changed"
            )

    for (
        name,
        descriptor,
        getter,
        code,
        defaults,
        default_witnesses,
    ) in _RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED:
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
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal payoff result authority surface changed"
            )

    if (
        resolve_product_proposal_risk_terminal_payoff_evaluation
        is not _PUBLIC_RESOLVER
        or getattr(
            resolve_product_proposal_risk_terminal_payoff_evaluation,
            "__code__",
            None,
        )
        is not _PUBLIC_RESOLVER_CODE
        or getattr(
            resolve_product_proposal_risk_terminal_payoff_evaluation,
            "__closure__",
            None,
        )
        is not _PUBLIC_RESOLVER_CLOSURE
        or len(_PUBLIC_RESOLVER_CLOSURE or ())
        != len(_PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED)
        or any(
            cell is not expected_cell
            or cell.cell_contents is not expected_value
            or getattr(expected_value, "__code__", None)
            is not expected_code
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
    ):
        raise ProductProposalRiskTerminalPayoffEvaluationError(
            "terminal payoff public resolver root changed"
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
            raise ProductProposalRiskTerminalPayoffEvaluationError(
                "terminal payoff helper root changed"
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
        "_decimal_context_state",
        "_decimal",
        "_decimal_text",
        "_canonical_bytes",
        "_digest",
        "_reject_duplicate_keys",
        "_reject_nonfinite",
        "_canonical_json_object",
        "_workspace_path",
        "_require_precommit",
        "_require_mapping",
        "_require_target",
        "_require_terminal_population",
        "_authority_map",
        "_terminal_groups",
        "_state_for_authority",
        "_candidate_tickets",
        "_authority_group_index",
        "_settlement_map_for_candidate",
        "_scenario_payoff",
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
    "ProductProposalRiskTerminalPayoffEvaluation",
    "ProductProposalRiskTerminalPayoffEvaluationError",
    "resolve_product_proposal_risk_terminal_payoff_evaluation",
]
