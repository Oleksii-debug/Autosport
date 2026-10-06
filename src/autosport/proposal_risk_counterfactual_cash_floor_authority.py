from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import paper as _paper_module
from . import risk as _risk_module
from .market_outcomes import MarketSettlementOutcomeAuthority
from .paper import PaperBook
from .proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)
from .proposal_risk_terminal_component_provenance_authority import (
    ProductProposalRiskTerminalComponentProvenance,
    ProductProposalRiskTerminalComponentProvenanceError,
    resolve_product_proposal_risk_terminal_component_provenance,
)
from .proposal_risk_terminal_payoff_authority import (
    ProductProposalRiskTerminalPayoffEvaluation,
    ProductProposalRiskTerminalPayoffEvaluationError,
    _candidate_tickets,
    resolve_product_proposal_risk_terminal_payoff_evaluation,
)
from .proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    resolve_product_proposal_risk_target,
)
from .risk import PaperRiskPolicy


_SCHEMA = "autosport.proposal-risk-counterfactual-cash-floor.v1"
_PATH_PROTOCOL = "paperbook.cash-floor.open-all-before-settlement.v1"
_SCHEMA_EXPECTED = _SCHEMA
_PATH_PROTOCOL_EXPECTED = _PATH_PROTOCOL
_HEX = frozenset("0123456789abcdef")
_PATH_TYPE = type(Path("."))

_PRECOMMIT_TYPE = ProductProposalRiskEvaluationPrecommit
_COMPONENT_TYPE = ProductProposalRiskTerminalComponentProvenance
_PAYOFF_TYPE = ProductProposalRiskTerminalPayoffEvaluation
_TARGET_TYPE = ProductProposalRiskTarget
_AUTHORITY_TYPE = MarketSettlementOutcomeAuthority
_BOOK_TYPE = PaperBook
_POLICY_TYPE = PaperRiskPolicy
_DECIMAL_TYPE = Decimal

_COMPONENT_RESOLVER = resolve_product_proposal_risk_terminal_component_provenance
_COMPONENT_RESOLVER_CODE = getattr(_COMPONENT_RESOLVER, "__code__", None)
_PAYOFF_RESOLVER = resolve_product_proposal_risk_terminal_payoff_evaluation
_PAYOFF_RESOLVER_CODE = getattr(_PAYOFF_RESOLVER, "__code__", None)
_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_TARGET_TICKET_RESOLVER = _candidate_tickets
_TARGET_TICKET_RESOLVER_CODE = getattr(_TARGET_TICKET_RESOLVER, "__code__", None)
_RISK_LOCKED_CAPITAL_FOR_PROPOSAL = (
    _risk_module._CANONICAL_LOCKED_CAPITAL_FOR_PROPOSAL
)
_RISK_LOCKED_CAPITAL_FOR_PROPOSAL_CODE = getattr(
    _RISK_LOCKED_CAPITAL_FOR_PROPOSAL,
    "__code__",
    None,
)

_BOOK_LOAD_DESCRIPTOR = PaperBook.__dict__["load"]
_BOOK_LOAD_FUNCTION = _BOOK_LOAD_DESCRIPTOR.__func__
_BOOK_LOAD_CODE = getattr(_BOOK_LOAD_FUNCTION, "__code__", None)
_BOOK_DEBIT_DESCRIPTOR = PaperBook.__dict__["_debit_balance"]
_BOOK_DEBIT_FUNCTION = _BOOK_DEBIT_DESCRIPTOR.__func__
_BOOK_DEBIT_CODE = getattr(_BOOK_DEBIT_FUNCTION, "__code__", None)
_PORTFOLIO_SHA_DESCRIPTOR = PaperRiskPolicy.__dict__[
    "risk_of_ruin_portfolio_sha256"
]
_PORTFOLIO_SHA_FUNCTION = _PORTFOLIO_SHA_DESCRIPTOR.__func__
_PORTFOLIO_SHA_CODE = getattr(_PORTFOLIO_SHA_FUNCTION, "__code__", None)
_PORTFOLIO_VALIDATE_STATE_DESCRIPTOR = PaperBook.__dict__["_validate_loaded_state"]
_PORTFOLIO_VALIDATE_STATE_FUNCTION = _PORTFOLIO_VALIDATE_STATE_DESCRIPTOR.__func__
_PORTFOLIO_VALIDATE_STATE_CODE = getattr(
    _PORTFOLIO_VALIDATE_STATE_FUNCTION,
    "__code__",
    None,
)
_PORTFOLIO_VALIDATE_LIFECYCLE_DESCRIPTOR = PaperBook.__dict__[
    "_validate_lifecycle_entry"
]
_PORTFOLIO_VALIDATE_LIFECYCLE_FUNCTION = (
    _PORTFOLIO_VALIDATE_LIFECYCLE_DESCRIPTOR.__func__
)
_PORTFOLIO_VALIDATE_LIFECYCLE_CODE = getattr(
    _PORTFOLIO_VALIDATE_LIFECYCLE_FUNCTION,
    "__code__",
    None,
)
_RISK_SHA256_PAYLOAD = _risk_module._sha256_payload
_RISK_SHA256_PAYLOAD_CODE = getattr(_RISK_SHA256_PAYLOAD, "__code__", None)
_RISK_JSON_MODULE = _risk_module.json
_RISK_HASHLIB_MODULE = _risk_module.hashlib

_PAPER_CONTEXT_FACTORY = _paper_module._paper_decimal_context
_PAPER_CONTEXT_FACTORY_CODE = getattr(_PAPER_CONTEXT_FACTORY, "__code__", None)
_PAPER_LOCALCONTEXT = _paper_module.localcontext
_PAPER_DECIMAL_EXCEPTION = _paper_module.DecimalException
_PAPER_INEXACT = _paper_module.Inexact
_PAPER_CONTEXT_TYPE = _paper_module.Context
_PAPER_ROUND_HALF_EVEN = _paper_module.ROUND_HALF_EVEN
_PAPER_INVALID_OPERATION = _paper_module.InvalidOperation
_PAPER_OVERFLOW = _paper_module.Overflow
_PAPER_UNDERFLOW = _paper_module.Underflow
_PAPER_DECIMAL_PRECISION = _paper_module._PAPER_DECIMAL_PRECISION
_PAPER_DECIMAL_EMIN = _paper_module._PAPER_DECIMAL_EMIN
_PAPER_DECIMAL_EMAX = _paper_module._PAPER_DECIMAL_EMAX

_JSON_DUMPS = json.dumps
_JSON_DUMPS_EXPECTED = _JSON_DUMPS
_JSON_DUMPS_CODE = getattr(_JSON_DUMPS, "__code__", None)
_JSON_ENCODER = json.JSONEncoder
_JSON_ENCODER_EXPECTED = _JSON_ENCODER
_HASHLIB_SHA256 = hashlib.sha256
_HASHLIB_SHA256_EXPECTED = _HASHLIB_SHA256


class ProductProposalRiskCounterfactualCashFloorError(RuntimeError):
    """The proposal cash-floor path cannot be derived safely."""


def _text(value: object, name: str, *, max_length: int = 2048) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskCounterfactualCashFloorError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
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
        raise ProductProposalRiskCounterfactualCashFloorError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not _DECIMAL_TYPE or not value.is_finite():
        raise ProductProposalRiskCounterfactualCashFloorError(
            f"{name} must be a finite exact Decimal"
        )
    return value


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
        raise ProductProposalRiskCounterfactualCashFloorError(
            "cash-floor canonical dispatch changed"
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
        raise ProductProposalRiskCounterfactualCashFloorError(
            "cash-floor material is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return _HASHLIB_SHA256(_canonical_bytes(value)).hexdigest()


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE or not value.is_absolute():
        raise ProductProposalRiskCounterfactualCashFloorError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != value or not value.is_dir() or value.is_symlink():
        raise ProductProposalRiskCounterfactualCashFloorError(
            "workspace must be the canonical non-symlink directory path"
        )
    return value


def _exact_add(left: object, right: object, name: str) -> Decimal:
    left_value = _decimal(left, f"{name}.left")
    right_value = _decimal(right, f"{name}.right")
    try:
        with _PAPER_LOCALCONTEXT(_PAPER_CONTEXT_FACTORY()) as context:
            result = left_value + right_value
            if context.flags[_PAPER_INEXACT]:
                raise ProductProposalRiskCounterfactualCashFloorError(
                    f"{name} loses Decimal precision"
                )
    except _PAPER_DECIMAL_EXCEPTION as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            f"{name} is not representable"
        ) from exc
    return _decimal(result, name)


def _require_precommit(
    precommit: object,
) -> ProductProposalRiskEvaluationPrecommit:
    if type(precommit) is not _PRECOMMIT_TYPE:
        raise ProductProposalRiskCounterfactualCashFloorError(
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
        raise ProductProposalRiskCounterfactualCashFloorError(
            "proposal risk precommit truth boundary is inconsistent"
        )
    return precommit


def _require_component(
    precommit: ProductProposalRiskEvaluationPrecommit,
    component: object,
) -> ProductProposalRiskTerminalComponentProvenance:
    if type(component) is not _COMPONENT_TYPE:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "terminal component resolver returned a non-canonical type"
        )
    if (
        component.provenance_identity_proven is not True
        or component.provider_terminal_population_proven is not True
        or component.terminal_mapping_proven is not True
        or component.target_terminal_payoff_evaluation_proven is not True
        or component.provider_terminal_component_provenance_proven is not True
        or type(component.per_market_terminal_space_exact) is not bool
        or type(component.joint_terminal_space_exact) is not bool
        or component.product_scenario_source_provenance_proven is not False
        or component.iid_member_mapping_proven is not False
        or component.joint_scenario_support_proven is not False
        or component.scenario_selection_law_proven is not False
        or component.minimum_equity_path_proven is not False
        or component.execution_costs_proven is not False
        or component.slippage_realization_proven is not False
        or component.net_execution_pnl_proven is not False
        or component.cashflow_chronology_proven is not False
        or component.scenario_execution_proven is not False
        or component.proposal_target_counterfactual_execution_proven is not False
        or component.risk_upper_bound_for_target is not False
        or component.grants_risk_approval_authority is not False
        or component.grants_ticket_authority is not False
        or component.grants_broker_execution_authority is not False
        or component.grants_real_money_authority is not False
        or component.grants_state_mutation_authority is not False
        or component.workspace_instance_id != precommit.workspace_instance_id
        or component.precommit_binding_sha256 != precommit.binding_sha256
        or component.target_sha256 != precommit.target_sha256
        or component.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or component.target_decision_ts != precommit.target_decision_ts
        or component.planned_member_ids != precommit.planned_member_ids
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "terminal component provenance does not match the exact precommit"
        )
    return component


def _require_payoff(
    precommit: ProductProposalRiskEvaluationPrecommit,
    component: ProductProposalRiskTerminalComponentProvenance,
    payoff: object,
) -> ProductProposalRiskTerminalPayoffEvaluation:
    if type(payoff) is not _PAYOFF_TYPE:
        raise ProductProposalRiskCounterfactualCashFloorError(
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
        is not component.per_market_terminal_space_exact
        or payoff.joint_terminal_space_exact
        is not component.joint_terminal_space_exact
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
        or payoff.evaluation_sha256
        != component.terminal_payoff_evaluation_sha256
        or payoff.planned_member_ids != component.planned_member_ids
        or payoff.member_scenario_ids != component.member_scenario_ids
        or payoff.member_mapping_sha256s != component.member_mapping_sha256s
        or payoff.member_state_vector_sha256s
        != component.member_state_vector_sha256s
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "terminal payoff does not match terminal component provenance"
        )
    return payoff


def _require_target(
    precommit: ProductProposalRiskEvaluationPrecommit,
    payoff: ProductProposalRiskTerminalPayoffEvaluation,
    target: object,
) -> ProductProposalRiskTarget:
    if type(target) is not _TARGET_TYPE:
        raise ProductProposalRiskCounterfactualCashFloorError(
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
        or target.candidate_vector_sha256 != precommit.candidate_vector_sha256
        or target.candidate_sha256s != payoff.candidate_sha256s
        or target.evaluated_stakes != payoff.evaluated_stakes
        or target.evaluated_stakes != precommit.evaluated_stakes
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "proposal target does not match the exact payoff/precommit"
        )
    return target


def _current_base_book(
    workspace: Path,
    target: ProductProposalRiskTarget,
) -> PaperBook:
    book_path = workspace / "paper_book.json"
    if not book_path.exists() or book_path.is_symlink():
        raise ProductProposalRiskCounterfactualCashFloorError(
            "canonical paper_book.json is required"
        )
    try:
        book = _BOOK_LOAD_FUNCTION(_BOOK_TYPE, book_path)
    except (ArithmeticError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "canonical base PaperBook cannot be loaded"
        ) from exc
    if type(book) is not _BOOK_TYPE:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "PaperBook loader returned a non-canonical type"
        )
    try:
        portfolio_sha = _PORTFOLIO_SHA_FUNCTION(_POLICY_TYPE, book)
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "base portfolio identity cannot be re-derived"
        ) from exc
    if portfolio_sha != target.base_portfolio_sha256:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "current PaperBook no longer matches target base portfolio"
        )
    _decimal(book.balance, "base cash balance")
    if book.balance < 0:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "base cash balance cannot be negative"
        )
    return book


def _target_capital_vector(
    target: ProductProposalRiskTarget,
) -> tuple[Decimal, ...]:
    try:
        tickets = _TARGET_TICKET_RESOLVER(target)
    except (ArithmeticError, RuntimeError, TypeError, ValueError) as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "target tickets cannot be reconstructed for capital reservation"
        ) from exc
    if (
        type(tickets) is not tuple
        or len(tickets) != len(target.evaluated_stakes)
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "target ticket vector lost evaluated-stake cardinality"
        )
    values: list[Decimal] = []
    for index, (ticket, stake_raw) in enumerate(
        zip(tickets, target.evaluated_stakes)
    ):
        stake = _decimal(stake_raw, f"evaluated_stakes[{index}]")
        if stake < 0:
            raise ProductProposalRiskCounterfactualCashFloorError(
                "evaluated target stake cannot be negative"
            )
        if stake.is_zero():
            capital = Decimal("0")
        else:
            try:
                capital = _RISK_LOCKED_CAPITAL_FOR_PROPOSAL(
                    stake,
                    ticket.legs,
                )
            except (ArithmeticError, AttributeError, TypeError, ValueError) as exc:
                raise ProductProposalRiskCounterfactualCashFloorError(
                    "target capital-at-risk vector is invalid"
                ) from exc
        capital = _decimal(capital, f"evaluated_capital_at_risk[{index}]")
        if capital < 0:
            raise ProductProposalRiskCounterfactualCashFloorError(
                "target capital at risk cannot be negative"
            )
        values.append(capital)
    return tuple(values)


def _open_target_stakes(
    base_balance: Decimal,
    capital_at_risk: tuple[Decimal, ...],
) -> tuple[tuple[Decimal, ...], Decimal]:
    if type(capital_at_risk) is not tuple or not capital_at_risk:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "capital-at-risk vector must be a non-empty exact tuple"
        )
    balance = _decimal(base_balance, "base cash balance")
    if balance < 0:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "base cash balance cannot be negative"
        )
    balances: list[Decimal] = []
    for index, capital_raw in enumerate(capital_at_risk):
        capital = _decimal(
            capital_raw,
            f"evaluated_capital_at_risk[{index}]",
        )
        if capital < 0:
            raise ProductProposalRiskCounterfactualCashFloorError(
                "evaluated target capital at risk cannot be negative"
            )
        if capital.is_zero():
            next_balance = balance
        else:
            try:
                next_balance = _BOOK_DEBIT_FUNCTION(
                    _BOOK_TYPE,
                    balance,
                    capital,
                )
            except (ArithmeticError, TypeError, ValueError) as exc:
                raise ProductProposalRiskCounterfactualCashFloorError(
                    "target stake vector is not cash-fundable from the base PaperBook"
                ) from exc
        balance = _decimal(next_balance, f"candidate_open_balance[{index}]")
        balances.append(balance)
    return tuple(balances), balance


def _member_cash_path(
    *,
    base_balance: Decimal,
    post_open_balance: Decimal,
    capital_at_risk: tuple[Decimal, ...],
    candidate_profits: tuple[Decimal, ...],
    total_profit: Decimal,
    member_index: int,
) -> tuple[tuple[Decimal, ...], Decimal, Decimal]:
    if (
        type(capital_at_risk) is not tuple
        or not capital_at_risk
        or type(candidate_profits) is not tuple
        or len(candidate_profits) != len(capital_at_risk)
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "member candidate payoff vector lost target cardinality"
        )
    if (
        type(member_index) is not int
        or member_index < 0
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "member_index must be a non-negative exact integer"
        )
    base = _decimal(base_balance, "base cash balance")
    post_open = _decimal(post_open_balance, "post-open cash balance")
    if base < 0 or post_open < 0 or post_open > base:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "member cash path has an invalid base/post-open cash boundary"
        )

    payouts: list[Decimal] = []
    balance = post_open
    minimum = post_open
    for candidate_index, (capital_raw, profit_raw) in enumerate(
        zip(capital_at_risk, candidate_profits)
    ):
        capital = _decimal(
            capital_raw,
            f"member[{member_index}].capital_at_risk",
        )
        if capital < 0:
            raise ProductProposalRiskCounterfactualCashFloorError(
                "member target capital at risk cannot be negative"
            )
        profit = _decimal(
            profit_raw,
            f"member[{member_index}].candidate_profit[{candidate_index}]",
        )
        payout = _exact_add(
            capital,
            profit,
            f"member[{member_index}].candidate_payout[{candidate_index}]",
        )
        if payout < 0:
            raise ProductProposalRiskCounterfactualCashFloorError(
                "terminal payoff implies a negative PaperBook payout"
            )
        payouts.append(payout)
        balance = _exact_add(
            balance,
            payout,
            f"member[{member_index}].settlement_balance[{candidate_index}]",
        )
        if balance < minimum:
            minimum = balance

    expected_terminal = _exact_add(
        base,
        _decimal(total_profit, f"member[{member_index}].total_profit"),
        f"member[{member_index}].terminal_balance_from_profit",
    )
    if balance != expected_terminal:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "candidate payouts do not reconcile to terminal target profit"
        )
    if minimum != post_open:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "non-negative settlement payouts violated the conservative cash floor"
        )
    return tuple(payouts), balance, minimum


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return (
            getattr(instance, "_counterfactual_cash_floor_capability", None)
            is token
        )

    def bind(instance: object) -> None:
        object.__setattr__(
            instance,
            "_counterfactual_cash_floor_capability",
            token,
        )

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskCounterfactualCashFloor:
    """Deterministic conservative cash-floor arithmetic for one proposal target.

    The protocol reserves every positive target stake before crediting any terminal
    payout. Because the terminal payoff authority proves canonical BACK-only
    PaperBook settlement arithmetic and every reconstructed payout is non-negative,
    the post-open cash balance is the minimum cash balance under this conservative
    counterfactual ordering.

    This is not market-value equity, actual scenario execution, an IID/joint
    probability claim, execution-cost/slippage evidence, or a target risk bound.
    """

    workspace_instance_id: str
    precommit_binding_sha256: str
    target_sha256: str
    candidate_vector_sha256: str
    base_portfolio_sha256: str
    terminal_component_provenance_sha256: str
    terminal_payoff_evaluation_sha256: str
    per_market_terminal_space_exact: bool
    joint_terminal_space_exact: bool
    path_protocol: str
    candidate_sha256s: tuple[str, ...]
    evaluated_stakes: tuple[Decimal, ...]
    evaluated_capital_at_risk: tuple[Decimal, ...]
    base_cash_balance: Decimal
    candidate_open_cash_balances: tuple[Decimal, ...]
    post_open_cash_balance: Decimal
    planned_member_ids: tuple[str, ...]
    member_scenario_ids: tuple[str, ...]
    member_candidate_payout_vectors: tuple[tuple[Decimal, ...], ...]
    member_terminal_cash_balances: tuple[Decimal, ...]
    member_minimum_cash_floors: tuple[Decimal, ...]
    evaluation_sha256: str
    _counterfactual_cash_floor_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalRiskCounterfactualCashFloor":
        raise TypeError(
            "ProductProposalRiskCounterfactualCashFloor is product-resolved; use "
            "resolve_product_proposal_risk_counterfactual_cash_floor"
        )

    @property
    def evaluation_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def base_portfolio_identity_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def counterfactual_target_stake_reservation_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def counterfactual_target_capital_reservation_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def terminal_payout_reconstruction_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def counterfactual_minimum_cash_floor_proven(
        self, _proven=_IDENTITY_PROVEN
    ) -> bool:
        return _proven(self)

    @property
    def product_scenario_source_provenance_proven(self) -> bool:
        return False

    @property
    def scenario_selection_law_proven(self) -> bool:
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
    def cashflow_chronology_proven(self) -> bool:
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


_RESULT_TYPE = ProductProposalRiskCounterfactualCashFloor
_RESULT_TYPE_EXPECTED = _RESULT_TYPE
_RESULT_FIELDS = (
    "workspace_instance_id",
    "precommit_binding_sha256",
    "target_sha256",
    "candidate_vector_sha256",
    "base_portfolio_sha256",
    "terminal_component_provenance_sha256",
    "terminal_payoff_evaluation_sha256",
    "per_market_terminal_space_exact",
    "joint_terminal_space_exact",
    "path_protocol",
    "candidate_sha256s",
    "evaluated_stakes",
    "evaluated_capital_at_risk",
    "base_cash_balance",
    "candidate_open_cash_balances",
    "post_open_cash_balance",
    "planned_member_ids",
    "member_scenario_ids",
    "member_candidate_payout_vectors",
    "member_terminal_cash_balances",
    "member_minimum_cash_floors",
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
    "base_portfolio_identity_proven",
    "counterfactual_target_stake_reservation_proven",
    "counterfactual_target_capital_reservation_proven",
    "terminal_payout_reconstruction_proven",
    "counterfactual_minimum_cash_floor_proven",
    "product_scenario_source_provenance_proven",
    "scenario_selection_law_proven",
    "iid_member_mapping_proven",
    "joint_scenario_support_proven",
    "minimum_equity_path_proven",
    "cashflow_chronology_proven",
    "execution_costs_proven",
    "slippage_realization_proven",
    "net_execution_pnl_proven",
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
        raise ProductProposalRiskCounterfactualCashFloorError(
            "cash-floor dispatch guard root changed"
        )
    _REQUIRE_DISPATCH_ORIGINAL()
    workspace = _workspace_path(workspace)
    precommit = _require_precommit(precommit)

    try:
        component = _COMPONENT_RESOLVER(
            workspace,
            precommit=precommit,
            authorities=authorities,
            member_market_state_ids=member_market_state_ids,
        )
    except (
        ProductProposalRiskTerminalComponentProvenanceError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "terminal component provenance cannot be re-resolved"
        ) from exc
    component = _require_component(precommit, component)

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
        raise ProductProposalRiskCounterfactualCashFloorError(
            "terminal payoff cannot be re-resolved"
        ) from exc
    payoff = _require_payoff(precommit, component, payoff)

    try:
        target = _TARGET_RESOLVER(workspace, precommit.target_sha256)
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskCounterfactualCashFloorError(
            "proposal target cannot be re-resolved"
        ) from exc
    target = _require_target(precommit, payoff, target)

    book = _current_base_book(workspace, target)
    base_balance = _decimal(book.balance, "base cash balance")
    capital_at_risk = _target_capital_vector(target)
    opening_balances, post_open_balance = _open_target_stakes(
        base_balance,
        capital_at_risk,
    )

    if (
        len(payoff.member_candidate_paper_profit_vectors)
        != len(payoff.planned_member_ids)
        or len(payoff.member_paper_terminal_profits)
        != len(payoff.planned_member_ids)
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "terminal payoff lost fixed-N member cardinality"
        )

    payout_vectors: list[tuple[Decimal, ...]] = []
    terminal_balances: list[Decimal] = []
    minimum_floors: list[Decimal] = []
    for member_index, (profits, total_profit) in enumerate(
        zip(
            payoff.member_candidate_paper_profit_vectors,
            payoff.member_paper_terminal_profits,
        )
    ):
        payouts, terminal_balance, minimum_floor = _member_cash_path(
            base_balance=base_balance,
            post_open_balance=post_open_balance,
            capital_at_risk=capital_at_risk,
            candidate_profits=profits,
            total_profit=total_profit,
            member_index=member_index,
        )
        payout_vectors.append(payouts)
        terminal_balances.append(terminal_balance)
        minimum_floors.append(minimum_floor)

    material = {
        "schema": _SCHEMA,
        "path_protocol": _PATH_PROTOCOL,
        "workspace_instance_id": precommit.workspace_instance_id,
        "precommit_binding_sha256": precommit.binding_sha256,
        "target_sha256": target.target_sha256,
        "candidate_vector_sha256": target.candidate_vector_sha256,
        "base_portfolio_sha256": target.base_portfolio_sha256,
        "terminal_component_provenance_sha256": component.provenance_sha256,
        "terminal_payoff_evaluation_sha256": payoff.evaluation_sha256,
        "per_market_terminal_space_exact": component.per_market_terminal_space_exact,
        "joint_terminal_space_exact": component.joint_terminal_space_exact,
        "candidate_sha256s": list(target.candidate_sha256s),
        "evaluated_stakes": [
            _decimal_text(value, "evaluated_stake")
            for value in target.evaluated_stakes
        ],
        "evaluated_capital_at_risk": [
            _decimal_text(value, "evaluated_capital_at_risk")
            for value in capital_at_risk
        ],
        "base_cash_balance": _decimal_text(base_balance, "base_cash_balance"),
        "candidate_open_cash_balances": [
            _decimal_text(value, "candidate_open_cash_balance")
            for value in opening_balances
        ],
        "post_open_cash_balance": _decimal_text(
            post_open_balance,
            "post_open_cash_balance",
        ),
        "members": [
            {
                "member_id": member_id,
                "scenario_id": scenario_id,
                "candidate_payouts": [
                    _decimal_text(value, "candidate_payout")
                    for value in payouts
                ],
                "terminal_cash_balance": _decimal_text(
                    terminal_balance,
                    "terminal_cash_balance",
                ),
                "minimum_cash_floor": _decimal_text(
                    minimum_floor,
                    "minimum_cash_floor",
                ),
            }
            for (
                member_id,
                scenario_id,
                payouts,
                terminal_balance,
                minimum_floor,
            ) in zip(
                payoff.planned_member_ids,
                payoff.member_scenario_ids,
                payout_vectors,
                terminal_balances,
                minimum_floors,
            )
        ],
        "base_portfolio_identity_proven": True,
        "counterfactual_target_stake_reservation_proven": True,
        "counterfactual_target_capital_reservation_proven": True,
        "terminal_payout_reconstruction_proven": True,
        "counterfactual_minimum_cash_floor_proven": True,
        "product_scenario_source_provenance_proven": False,
        "scenario_selection_law_proven": False,
        "iid_member_mapping_proven": False,
        "joint_scenario_support_proven": False,
        "minimum_equity_path_proven": False,
        "cashflow_chronology_proven": False,
        "execution_costs_proven": False,
        "slippage_realization_proven": False,
        "net_execution_pnl_proven": False,
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
        "base_portfolio_sha256": target.base_portfolio_sha256,
        "terminal_component_provenance_sha256": component.provenance_sha256,
        "terminal_payoff_evaluation_sha256": payoff.evaluation_sha256,
        "per_market_terminal_space_exact": component.per_market_terminal_space_exact,
        "joint_terminal_space_exact": component.joint_terminal_space_exact,
        "path_protocol": _PATH_PROTOCOL,
        "candidate_sha256s": target.candidate_sha256s,
        "evaluated_stakes": target.evaluated_stakes,
        "evaluated_capital_at_risk": capital_at_risk,
        "base_cash_balance": base_balance,
        "candidate_open_cash_balances": opening_balances,
        "post_open_cash_balance": post_open_balance,
        "planned_member_ids": payoff.planned_member_ids,
        "member_scenario_ids": payoff.member_scenario_ids,
        "member_candidate_payout_vectors": tuple(payout_vectors),
        "member_terminal_cash_balances": tuple(terminal_balances),
        "member_minimum_cash_floors": tuple(minimum_floors),
        "evaluation_sha256": _digest(material),
    }


def _make_public_resolver(
    _bind_identity,
    _resolve_core,
    _result_type,
    _result_fields,
):
    bind_code = getattr(_bind_identity, "__code__", None)
    core_code = getattr(_resolve_core, "__code__", None)

    def resolver(
        workspace: Path,
        *,
        precommit: ProductProposalRiskEvaluationPrecommit,
        authorities: tuple[MarketSettlementOutcomeAuthority, ...],
        member_market_state_ids: tuple[tuple[str, ...], ...],
    ) -> ProductProposalRiskCounterfactualCashFloor:
        # Snapshot every construction dependency before entering the core resolver.
        # Closure cells are mutable through Python introspection; rereading them after
        # the core dispatch seal would leave a post-core TOCTOU window.
        bind_identity = _bind_identity
        resolve_core = _resolve_core
        result_type = _result_type
        result_fields = _result_fields
        expected_bind_code = bind_code
        expected_core_code = core_code
        if (
            getattr(bind_identity, "__code__", None) is not expected_bind_code
            or getattr(resolve_core, "__code__", None) is not expected_core_code
        ):
            raise ProductProposalRiskCounterfactualCashFloorError(
                "cash-floor public resolver closure changed"
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

    resolver.__name__ = "resolve_product_proposal_risk_counterfactual_cash_floor"
    resolver.__qualname__ = resolver.__name__
    return resolver


resolve_product_proposal_risk_counterfactual_cash_floor = _make_public_resolver(
    _BIND_IDENTITY,
    _resolve_values,
    _RESULT_TYPE,
    _RESULT_FIELDS_EXPECTED,
)
_PUBLIC_RESOLVER = resolve_product_proposal_risk_counterfactual_cash_floor
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


_HELPER_NAMES = (
    "_text",
    "_sha",
    "_decimal",
    "_decimal_text",
    "_canonical_bytes",
    "_digest",
    "_workspace_path",
    "_exact_add",
    "_require_precommit",
    "_require_component",
    "_require_payoff",
    "_require_target",
    "_current_base_book",
    "_target_capital_vector",
    "_open_target_stakes",
    "_member_cash_path",
    "_resolve_values",
)
_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
        getattr(globals()[name], "__defaults__", None),
        tuple(
            sorted(
                (getattr(globals()[name], "__kwdefaults__", None) or {}).items()
            )
        ),
    )
    for name in _HELPER_NAMES
)
_HELPER_WITNESSES_EXPECTED = _HELPER_WITNESSES


def _require_dispatch() -> None:
    if (
        _SCHEMA != _SCHEMA_EXPECTED
        or _PATH_PROTOCOL != _PATH_PROTOCOL_EXPECTED
        or ProductProposalRiskEvaluationPrecommit is not _PRECOMMIT_TYPE
        or ProductProposalRiskTerminalComponentProvenance is not _COMPONENT_TYPE
        or ProductProposalRiskTerminalPayoffEvaluation is not _PAYOFF_TYPE
        or ProductProposalRiskTarget is not _TARGET_TYPE
        or MarketSettlementOutcomeAuthority is not _AUTHORITY_TYPE
        or PaperBook is not _BOOK_TYPE
        or PaperRiskPolicy is not _POLICY_TYPE
        or Decimal is not _DECIMAL_TYPE
        or resolve_product_proposal_risk_terminal_component_provenance
        is not _COMPONENT_RESOLVER
        or getattr(_COMPONENT_RESOLVER, "__code__", None)
        is not _COMPONENT_RESOLVER_CODE
        or resolve_product_proposal_risk_terminal_payoff_evaluation
        is not _PAYOFF_RESOLVER
        or getattr(_PAYOFF_RESOLVER, "__code__", None)
        is not _PAYOFF_RESOLVER_CODE
        or resolve_product_proposal_risk_target is not _TARGET_RESOLVER
        or getattr(_TARGET_RESOLVER, "__code__", None) is not _TARGET_RESOLVER_CODE
        or _candidate_tickets is not _TARGET_TICKET_RESOLVER
        or getattr(_TARGET_TICKET_RESOLVER, "__code__", None)
        is not _TARGET_TICKET_RESOLVER_CODE
        or _risk_module._CANONICAL_LOCKED_CAPITAL_FOR_PROPOSAL
        is not _RISK_LOCKED_CAPITAL_FOR_PROPOSAL
        or getattr(_RISK_LOCKED_CAPITAL_FOR_PROPOSAL, "__code__", None)
        is not _RISK_LOCKED_CAPITAL_FOR_PROPOSAL_CODE
        or PaperBook.__dict__.get("load") is not _BOOK_LOAD_DESCRIPTOR
        or _BOOK_LOAD_DESCRIPTOR.__func__ is not _BOOK_LOAD_FUNCTION
        or getattr(_BOOK_LOAD_FUNCTION, "__code__", None) is not _BOOK_LOAD_CODE
        or PaperBook.__dict__.get("_debit_balance") is not _BOOK_DEBIT_DESCRIPTOR
        or _BOOK_DEBIT_DESCRIPTOR.__func__ is not _BOOK_DEBIT_FUNCTION
        or getattr(_BOOK_DEBIT_FUNCTION, "__code__", None) is not _BOOK_DEBIT_CODE
        or PaperRiskPolicy.__dict__.get("risk_of_ruin_portfolio_sha256")
        is not _PORTFOLIO_SHA_DESCRIPTOR
        or _PORTFOLIO_SHA_DESCRIPTOR.__func__ is not _PORTFOLIO_SHA_FUNCTION
        or getattr(_PORTFOLIO_SHA_FUNCTION, "__code__", None)
        is not _PORTFOLIO_SHA_CODE
        or PaperBook.__dict__.get("_validate_loaded_state")
        is not _PORTFOLIO_VALIDATE_STATE_DESCRIPTOR
        or _PORTFOLIO_VALIDATE_STATE_DESCRIPTOR.__func__
        is not _PORTFOLIO_VALIDATE_STATE_FUNCTION
        or getattr(_PORTFOLIO_VALIDATE_STATE_FUNCTION, "__code__", None)
        is not _PORTFOLIO_VALIDATE_STATE_CODE
        or PaperBook.__dict__.get("_validate_lifecycle_entry")
        is not _PORTFOLIO_VALIDATE_LIFECYCLE_DESCRIPTOR
        or _PORTFOLIO_VALIDATE_LIFECYCLE_DESCRIPTOR.__func__
        is not _PORTFOLIO_VALIDATE_LIFECYCLE_FUNCTION
        or getattr(_PORTFOLIO_VALIDATE_LIFECYCLE_FUNCTION, "__code__", None)
        is not _PORTFOLIO_VALIDATE_LIFECYCLE_CODE
        or _risk_module.PaperBook is not _BOOK_TYPE
        or _risk_module._sha256_payload is not _RISK_SHA256_PAYLOAD
        or getattr(_RISK_SHA256_PAYLOAD, "__code__", None)
        is not _RISK_SHA256_PAYLOAD_CODE
        or _risk_module.json is not _RISK_JSON_MODULE
        or _risk_module.hashlib is not _RISK_HASHLIB_MODULE
        or _RISK_JSON_MODULE is not json
        or _RISK_HASHLIB_MODULE is not hashlib
        or _paper_module._paper_decimal_context is not _PAPER_CONTEXT_FACTORY
        or getattr(_PAPER_CONTEXT_FACTORY, "__code__", None)
        is not _PAPER_CONTEXT_FACTORY_CODE
        or _paper_module.localcontext is not _PAPER_LOCALCONTEXT
        or _paper_module.DecimalException is not _PAPER_DECIMAL_EXCEPTION
        or _paper_module.Inexact is not _PAPER_INEXACT
        or _paper_module.Context is not _PAPER_CONTEXT_TYPE
        or _paper_module.ROUND_HALF_EVEN is not _PAPER_ROUND_HALF_EVEN
        or _paper_module.InvalidOperation is not _PAPER_INVALID_OPERATION
        or _paper_module.Overflow is not _PAPER_OVERFLOW
        or _paper_module.Underflow is not _PAPER_UNDERFLOW
        or _paper_module._PAPER_DECIMAL_PRECISION != _PAPER_DECIMAL_PRECISION
        or _paper_module._PAPER_DECIMAL_EMIN != _PAPER_DECIMAL_EMIN
        or _paper_module._PAPER_DECIMAL_EMAX != _PAPER_DECIMAL_EMAX
        or _JSON_DUMPS is not _JSON_DUMPS_EXPECTED
        or json.dumps is not _JSON_DUMPS_EXPECTED
        or getattr(_JSON_DUMPS, "__code__", None) is not _JSON_DUMPS_CODE
        or getattr(json.dumps, "__code__", None) is not _JSON_DUMPS_CODE
        or _JSON_ENCODER is not _JSON_ENCODER_EXPECTED
        or json.JSONEncoder is not _JSON_ENCODER_EXPECTED
        or _HASHLIB_SHA256 is not _HASHLIB_SHA256_EXPECTED
        or hashlib.sha256 is not _HASHLIB_SHA256_EXPECTED
        or ProductProposalRiskCounterfactualCashFloor is not _RESULT_TYPE_EXPECTED
        or _RESULT_TYPE is not _RESULT_TYPE_EXPECTED
        or _RESULT_FIELDS is not _RESULT_FIELDS_EXPECTED
        or _RESULT_FIELD_DESCRIPTOR_WITNESSES
        is not _RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED
        or _RESULT_AUTHORITY_PROPERTY_NAMES
        is not _RESULT_AUTHORITY_PROPERTY_NAMES_EXPECTED
        or _RESULT_AUTHORITY_PROPERTY_WITNESSES
        is not _RESULT_AUTHORITY_PROPERTY_WITNESSES_EXPECTED
        or resolve_product_proposal_risk_counterfactual_cash_floor
        is not _PUBLIC_RESOLVER
        or getattr(
            resolve_product_proposal_risk_counterfactual_cash_floor,
            "__code__",
            None,
        )
        is not _PUBLIC_RESOLVER_CODE
        or getattr(
            resolve_product_proposal_risk_counterfactual_cash_floor,
            "__closure__",
            None,
        )
        is not _PUBLIC_RESOLVER_CLOSURE
        or _PUBLIC_RESOLVER_CLOSURE_WITNESSES
        is not _PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED
        or _HELPER_WITNESSES is not _HELPER_WITNESSES_EXPECTED
        or type(_HELPER_WITNESSES_EXPECTED) is not tuple
    ):
        raise ProductProposalRiskCounterfactualCashFloorError(
            "cash-floor dispatch root changed"
        )

    for name, expected_descriptor in _RESULT_FIELD_DESCRIPTOR_WITNESSES_EXPECTED:
        if _RESULT_TYPE.__dict__.get(name) is not expected_descriptor:
            raise ProductProposalRiskCounterfactualCashFloorError(
                "cash-floor result field surface changed"
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
            raise ProductProposalRiskCounterfactualCashFloorError(
                "cash-floor result authority surface changed"
            )

    if (
        len(_PUBLIC_RESOLVER_CLOSURE or ())
        != len(_PUBLIC_RESOLVER_CLOSURE_WITNESSES_EXPECTED)
        or any(
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
                in zip(
                    expected_nested_closure or (),
                    expected_nested_cells,
                )
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
        raise ProductProposalRiskCounterfactualCashFloorError(
            "cash-floor public resolver root changed"
        )

    for (
        name,
        expected,
        code,
        defaults,
        kwdefault_items,
    ) in _HELPER_WITNESSES_EXPECTED:
        current = globals().get(name)
        if (
            current is not expected
            or getattr(current, "__code__", None) is not code
            or getattr(current, "__defaults__", None) is not defaults
            or tuple(
                sorted(
                    (getattr(current, "__kwdefaults__", None) or {}).items()
                )
            )
            != kwdefault_items
        ):
            raise ProductProposalRiskCounterfactualCashFloorError(
                "cash-floor helper root changed"
            )


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
    "ProductProposalRiskCounterfactualCashFloor",
    "ProductProposalRiskCounterfactualCashFloorError",
    "resolve_product_proposal_risk_counterfactual_cash_floor",
]
