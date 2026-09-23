"""Safety-only joint-tail stress projection for portfolio exposure.

This module does not create settlement, execution, optimization, risk-policy, or
risk-of-ruin authority.  It derives structural concentration from canonical
PaperTicket identities and evaluates explicit adverse terminal assumptions by
delegating monetary P&L to PortfolioEngine.scenario_profit_settlements().

The result is deliberately monotone toward safety: it can expose concentration
or a worse observed stress P&L, but it can never authorize diversification
credit, risk reduction, permission expansion, or real-money execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any

from .domain import PaperTicket, TicketLeg, TicketStatus
from .portfolio import PortfolioEngine


_HEX = frozenset("0123456789abcdef")
_ALLOWED_SETTLEMENTS = frozenset({"win", "loss", "void"})
_RESULT_ISSUANCE_TOKEN = object()


class JointDependenceGrade(str, Enum):
    """Truth grade for non-structural dependence evidence."""

    EMPIRICAL_DEPENDENCE = "empirical_dependence"
    STRESS_ASSUMPTION = "stress_assumption"
    UNKNOWN_DEPENDENCE = "unknown_dependence"


class StructuralDependencyKind(str, Enum):
    """Mechanically derived shared economic identity."""

    SHARED_QUOTE = "shared_quote"
    SHARED_MARKET = "shared_market"
    SHARED_EVENT = "shared_event"


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be canonical non-empty text")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    return value


def _sha256_text(value: object, field_name: str) -> str:
    digest = _text(value, field_name)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in _HEX for character in digest)
    ):
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")
    return digest


def _timestamp(value: object, field_name: str) -> str:
    raw = _text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return raw


def _parsed_timestamp(value: object, field_name: str) -> datetime:
    raw = _timestamp(value, field_name)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _finite_decimal(value: object, field_name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite exact Decimal")
    return value


def _decimal_text(value: Decimal) -> str:
    """Context-independent canonical finite Decimal representation."""

    value = _finite_decimal(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    if not digits or all(digit == 0 for digit in digits):
        return "0"
    coefficient = "".join(str(digit) for digit in digits)
    exponent = int(exponent)
    if exponent >= 0:
        body = coefficient + ("0" * exponent)
    else:
        places = -exponent
        if len(coefficient) > places:
            body = coefficient[:-places] + "." + coefficient[-places:]
        else:
            body = "0." + ("0" * (places - len(coefficient))) + coefficient
        body = body.rstrip("0").rstrip(".")
    return ("-" if sign else "") + body


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: object) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class JointDependenceRelation:
    """Descriptive/stress relation that can never grant diversification credit.

    Structural shared quote/market/event relationships are not accepted from
    callers here; they are derived mechanically from PaperTicket legs.
    """

    relation_id: str
    member_ticket_ids: tuple[str, ...]
    grade: JointDependenceGrade
    reason: str
    evidence_sha256: str | None = None
    evidence_available_at: str | None = None
    empirical_coefficient: Decimal | None = None

    def __post_init__(self) -> None:
        _text(self.relation_id, "relation_id")
        if type(self.member_ticket_ids) is not tuple or len(self.member_ticket_ids) < 2:
            raise ValueError("dependence relation requires at least two ticket identities")
        canonical_members = tuple(
            sorted(_text(ticket_id, "member_ticket_id") for ticket_id in self.member_ticket_ids)
        )
        if len(canonical_members) != len(set(canonical_members)):
            raise ValueError("dependence relation ticket identities must be unique")
        object.__setattr__(self, "member_ticket_ids", canonical_members)

        if type(self.grade) is not JointDependenceGrade:
            raise ValueError("grade must be exact JointDependenceGrade")
        _text(self.reason, "dependence reason")

        if self.grade is JointDependenceGrade.UNKNOWN_DEPENDENCE:
            if (
                self.evidence_sha256 is not None
                or self.evidence_available_at is not None
                or self.empirical_coefficient is not None
            ):
                raise ValueError(
                    "unknown dependence must not carry fabricated evidence or coefficient"
                )
        elif self.grade is JointDependenceGrade.EMPIRICAL_DEPENDENCE:
            _sha256_text(self.evidence_sha256, "empirical evidence_sha256")
            _timestamp(self.evidence_available_at, "empirical evidence_available_at")
            coefficient = _finite_decimal(
                self.empirical_coefficient,
                "empirical_coefficient",
            )
            if coefficient < Decimal("-1") or coefficient > Decimal("1"):
                raise ValueError("empirical_coefficient must be between -1 and 1")
        else:
            _sha256_text(self.evidence_sha256, "stress evidence_sha256")
            _timestamp(self.evidence_available_at, "stress evidence_available_at")
            if self.empirical_coefficient is not None:
                raise ValueError("stress assumptions must not masquerade as empirical correlation")

    @property
    def relation_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.joint_dependence_relation",
                "schema_version": 1,
                "relation_id": self.relation_id,
                "member_ticket_ids": list(self.member_ticket_ids),
                "grade": self.grade.value,
                "reason": self.reason,
                "evidence_sha256": self.evidence_sha256,
                "evidence_available_at": self.evidence_available_at,
                "empirical_coefficient": (
                    None
                    if self.empirical_coefficient is None
                    else _decimal_text(self.empirical_coefficient)
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class JointStressScenario:
    """One fully specified adverse settlement assumption.

    A scenario is intentionally not terminal-state authority.  Even a structurally
    plausible scenario remains a safety stress unless a separate canonical outcome
    authority proves exhaustiveness/exactness elsewhere.
    """

    scenario_id: str
    settlements: tuple[tuple[str, str], ...]
    relation_ids: tuple[str, ...]
    committed_at: str
    assumption_sha256: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.scenario_id, "scenario_id")
        if type(self.settlements) is not tuple or not self.settlements:
            raise ValueError("stress scenario settlements must be a non-empty tuple")
        canonical_rows: list[tuple[str, str]] = []
        seen_quotes: set[str] = set()
        for item in self.settlements:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("stress settlements must contain (quote_key, result) tuples")
            quote_key = _text(item[0], "stress quote_key")
            result = item[1]
            if type(result) is not str or result not in _ALLOWED_SETTLEMENTS:
                raise ValueError("stress result must be win, loss, or void")
            if quote_key in seen_quotes:
                raise ValueError("stress scenario contains duplicate quote_key")
            seen_quotes.add(quote_key)
            canonical_rows.append((quote_key, result))
        object.__setattr__(self, "settlements", tuple(sorted(canonical_rows)))

        if type(self.relation_ids) is not tuple:
            raise ValueError("stress scenario relation_ids must be a tuple")
        canonical_relations = tuple(
            sorted(_text(relation_id, "stress relation_id") for relation_id in self.relation_ids)
        )
        if len(canonical_relations) != len(set(canonical_relations)):
            raise ValueError("stress scenario relation_ids must be unique")
        object.__setattr__(self, "relation_ids", canonical_relations)
        _timestamp(self.committed_at, "stress scenario committed_at")
        _sha256_text(self.assumption_sha256, "assumption_sha256")
        _text(self.reason, "stress scenario reason")

    @property
    def scenario_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.joint_stress_scenario",
                "schema_version": 1,
                "scenario_id": self.scenario_id,
                "settlements": [
                    {"quote_key": quote_key, "result": result}
                    for quote_key, result in self.settlements
                ],
                "relation_ids": list(self.relation_ids),
                "committed_at": self.committed_at,
                "assumption_sha256": self.assumption_sha256,
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class JointStressProtocol:
    """Frozen safety-stress inputs; never a positive financial permission source."""

    protocol_id: str
    protocol_version: str
    causal_cutoff: str
    relations: tuple[JointDependenceRelation, ...]
    scenarios: tuple[JointStressScenario, ...]

    def __post_init__(self) -> None:
        _text(self.protocol_id, "protocol_id")
        _text(self.protocol_version, "protocol_version")
        cutoff = _parsed_timestamp(self.causal_cutoff, "causal_cutoff")

        if type(self.relations) is not tuple:
            raise ValueError("relations must be a tuple")
        if any(type(relation) is not JointDependenceRelation for relation in self.relations):
            raise ValueError("relations must contain exact JointDependenceRelation values")
        relations = tuple(sorted(self.relations, key=lambda item: item.relation_id))
        relation_ids = tuple(item.relation_id for item in relations)
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("relation_id values must be unique")
        object.__setattr__(self, "relations", relations)
        for relation in relations:
            if relation.evidence_available_at is None:
                continue
            available = _parsed_timestamp(
                relation.evidence_available_at,
                "relation evidence_available_at",
            )
            if available > cutoff:
                raise ValueError(
                    "dependence evidence cannot be available after causal_cutoff"
                )

        if type(self.scenarios) is not tuple or not self.scenarios:
            raise ValueError("scenarios must be a non-empty tuple")
        if any(type(scenario) is not JointStressScenario for scenario in self.scenarios):
            raise ValueError("scenarios must contain exact JointStressScenario values")
        scenarios = tuple(sorted(self.scenarios, key=lambda item: item.scenario_id))
        scenario_ids = tuple(item.scenario_id for item in scenarios)
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("scenario_id values must be unique")
        object.__setattr__(self, "scenarios", scenarios)
        for scenario in scenarios:
            committed = _parsed_timestamp(
                scenario.committed_at,
                "stress scenario committed_at",
            )
            if committed > cutoff:
                raise ValueError(
                    "stress scenario must be committed no later than causal_cutoff"
                )

        known_relations = set(relation_ids)
        referenced: set[str] = set()
        for scenario in scenarios:
            unknown = set(scenario.relation_ids).difference(known_relations)
            if unknown:
                raise ValueError("stress scenario references unknown dependence relation")
            referenced.update(scenario.relation_ids)

        required_references = {
            relation.relation_id
            for relation in relations
            if relation.grade is not JointDependenceGrade.UNKNOWN_DEPENDENCE
        }
        if not required_references.issubset(referenced):
            raise ValueError(
                "every empirical/stress relation must be exercised by a stress scenario"
            )

    @property
    def protocol_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.joint_stress_protocol",
                "schema_version": 1,
                "protocol_id": self.protocol_id,
                "protocol_version": self.protocol_version,
                "causal_cutoff": self.causal_cutoff,
                "relations": [
                    {
                        "relation_id": relation.relation_id,
                        "relation_sha256": relation.relation_sha256,
                    }
                    for relation in self.relations
                ],
                "scenarios": [
                    {
                        "scenario_id": scenario.scenario_id,
                        "scenario_sha256": scenario.scenario_sha256,
                    }
                    for scenario in self.scenarios
                ],
            }
        )

    @property
    def financial_permission_authority(self) -> bool:
        return False

    @property
    def exact_terminal_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class StructuralDependencyGroup:
    kind: StructuralDependencyKind
    member_ticket_ids: tuple[str, ...]
    scope_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not StructuralDependencyKind:
            raise ValueError("structural dependency kind is invalid")
        if type(self.member_ticket_ids) is not tuple or len(self.member_ticket_ids) < 2:
            raise ValueError("structural dependency group requires at least two tickets")
        if self.member_ticket_ids != tuple(sorted(self.member_ticket_ids)):
            raise ValueError("structural dependency ticket ids must be sorted")
        if len(self.member_ticket_ids) != len(set(self.member_ticket_ids)):
            raise ValueError("structural dependency ticket ids must be unique")
        for ticket_id in self.member_ticket_ids:
            _text(ticket_id, "structural dependency ticket_id")
        _sha256_text(self.scope_sha256, "structural dependency scope_sha256")

    @property
    def group_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.structural_dependency_group",
                "schema_version": 1,
                "kind": self.kind.value,
                "member_ticket_ids": list(self.member_ticket_ids),
                "scope_sha256": self.scope_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class JointScenarioEvaluation:
    scenario_id: str
    scenario_sha256: str
    profit: Decimal

    def __post_init__(self) -> None:
        _text(self.scenario_id, "scenario evaluation id")
        _sha256_text(self.scenario_sha256, "scenario evaluation sha256")
        _finite_decimal(self.profit, "scenario profit")


@dataclass(frozen=True, slots=True)
class JointStressResult:
    """Product-issued safety projection.

    Direct construction is rejected.  Even product-issued results remain
    non-authoritative for diversification/risk reduction/permission expansion.
    """

    protocol_sha256: str
    portfolio_scope_sha256: str
    currency: str
    ticket_ids: tuple[str, ...]
    quote_keys: tuple[str, ...]
    structural_groups: tuple[StructuralDependencyGroup, ...]
    evaluations: tuple[JointScenarioEvaluation, ...]
    unresolved_relation_ids: tuple[str, ...]
    worst_observed_profit: Decimal
    best_observed_profit: Decimal
    _issuance_token: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuance_token is not _RESULT_ISSUANCE_TOKEN:
            raise TypeError("JointStressResult must be issued by evaluate_joint_stress")
        _sha256_text(self.protocol_sha256, "protocol_sha256")
        _sha256_text(self.portfolio_scope_sha256, "portfolio_scope_sha256")
        currency = _text(self.currency, "currency")
        if (
            len(currency) != 3
            or not currency.isascii()
            or not currency.isalpha()
            or currency != currency.upper()
        ):
            raise ValueError("currency must be 3-letter uppercase ASCII")
        if type(self.ticket_ids) is not tuple or not self.ticket_ids:
            raise ValueError("result ticket_ids must be a non-empty tuple")
        if self.ticket_ids != tuple(sorted(self.ticket_ids)):
            raise ValueError("result ticket_ids must be sorted")
        if len(self.ticket_ids) != len(set(self.ticket_ids)):
            raise ValueError("result ticket_ids must be unique")
        if type(self.quote_keys) is not tuple or not self.quote_keys:
            raise ValueError("result quote_keys must be a non-empty tuple")
        if self.quote_keys != tuple(sorted(self.quote_keys)):
            raise ValueError("result quote_keys must be sorted")
        if len(self.quote_keys) != len(set(self.quote_keys)):
            raise ValueError("result quote_keys must be unique")
        if type(self.structural_groups) is not tuple or any(
            type(group) is not StructuralDependencyGroup
            for group in self.structural_groups
        ):
            raise ValueError(
                "result structural_groups must contain exact StructuralDependencyGroup values"
            )
        if type(self.evaluations) is not tuple or not self.evaluations:
            raise ValueError("result evaluations must be a non-empty tuple")
        if any(
            type(item) is not JointScenarioEvaluation
            for item in self.evaluations
        ):
            raise ValueError(
                "result evaluations must contain exact JointScenarioEvaluation values"
            )
        evaluation_ids = tuple(item.scenario_id for item in self.evaluations)
        if evaluation_ids != tuple(sorted(evaluation_ids)):
            raise ValueError("result evaluations must be sorted by scenario_id")
        if len(evaluation_ids) != len(set(evaluation_ids)):
            raise ValueError("result scenario evaluations must be unique")
        if type(self.unresolved_relation_ids) is not tuple:
            raise ValueError("result unresolved_relation_ids must be a tuple")
        if self.unresolved_relation_ids != tuple(sorted(self.unresolved_relation_ids)):
            raise ValueError("result unresolved_relation_ids must be sorted")
        if len(self.unresolved_relation_ids) != len(set(self.unresolved_relation_ids)):
            raise ValueError("result unresolved_relation_ids must be unique")
        _finite_decimal(self.worst_observed_profit, "worst_observed_profit")
        _finite_decimal(self.best_observed_profit, "best_observed_profit")
        if self.worst_observed_profit > self.best_observed_profit:
            raise ValueError("worst_observed_profit cannot exceed best_observed_profit")
        evaluation_profits = tuple(item.profit for item in self.evaluations)
        if (
            self.worst_observed_profit != min(evaluation_profits)
            or self.best_observed_profit != max(evaluation_profits)
        ):
            raise ValueError(
                "result extrema must exactly match scenario evaluation profits"
            )

        # The constructor token is a one-shot issuance capability, not durable
        # result state.  Retaining it would let dataclasses.replace() copy the
        # product-issued marker into caller-modified fields.
        object.__setattr__(self, "_issuance_token", None)

    @property
    def diversification_credit_authorized(self) -> bool:
        return False

    @property
    def risk_reduction_authorized(self) -> bool:
        return False

    @property
    def financial_permission_expansion_authorized(self) -> bool:
        return False

    @property
    def exact_terminal_authority(self) -> bool:
        return False

    @property
    def result_sha256(self) -> str:
        return _digest(
            {
                "schema": "autosport.joint_stress_result",
                "schema_version": 1,
                "protocol_sha256": self.protocol_sha256,
                "portfolio_scope_sha256": self.portfolio_scope_sha256,
                "currency": self.currency,
                "ticket_ids": list(self.ticket_ids),
                "quote_keys": list(self.quote_keys),
                "structural_group_sha256s": [
                    group.group_sha256 for group in self.structural_groups
                ],
                "evaluations": [
                    {
                        "scenario_id": item.scenario_id,
                        "scenario_sha256": item.scenario_sha256,
                        "profit": _decimal_text(item.profit),
                    }
                    for item in self.evaluations
                ],
                "unresolved_relation_ids": list(self.unresolved_relation_ids),
                "worst_observed_profit": _decimal_text(self.worst_observed_profit),
                "best_observed_profit": _decimal_text(self.best_observed_profit),
                "diversification_credit_authorized": False,
                "risk_reduction_authorized": False,
                "financial_permission_expansion_authorized": False,
                "exact_terminal_authority": False,
            }
        )


def _leg_payload(leg: TicketLeg) -> dict[str, object]:
    if type(leg) is not TicketLeg:
        raise ValueError("joint stress requires exact TicketLeg values")
    quote_key = _text(leg.quote_key, "ticket leg quote_key")
    odds = _finite_decimal(leg.locked_odds, "ticket leg locked_odds")
    exchange_side = leg.exchange_side
    if exchange_side == "lay":
        raise ValueError(
            "joint stress LAY ticket economics require canonical LAY settlement support"
        )
    return {
        "quote_key": quote_key,
        "event_id": _text(leg.event_id, "ticket leg event_id"),
        "market_id": _text(leg.market_id, "ticket leg market_id"),
        "selection_id": _text(leg.selection_id, "ticket leg selection_id"),
        "sport": leg.sport,
        "exchange_side": exchange_side,
        "locked_odds": _decimal_text(odds),
    }


def _ticket_scope_payload(ticket: PaperTicket) -> dict[str, object]:
    if type(ticket) is not PaperTicket:
        raise ValueError("joint stress requires exact PaperTicket values")
    if ticket.status is not TicketStatus.OPEN:
        raise ValueError("joint stress scope must contain only OPEN tickets")
    ticket_id = _text(ticket.ticket_id, "ticket_id")
    stake = _finite_decimal(ticket.stake, "ticket stake")
    if stake < 0:
        raise ValueError("ticket stake must be non-negative")
    if type(ticket.legs) is not tuple or not ticket.legs:
        raise ValueError("joint stress ticket requires a non-empty leg tuple")
    legs = tuple(
        sorted(
            (_leg_payload(leg) for leg in ticket.legs),
            key=lambda payload: (
                payload["quote_key"],
                payload["locked_odds"],
            ),
        )
    )
    if type(ticket.provider_source_ids) is not tuple:
        raise ValueError("provider_source_ids must be a tuple")
    provider_source_ids = tuple(
        sorted(_text(item, "provider_source_id") for item in ticket.provider_source_ids)
    )
    if len(provider_source_ids) != len(set(provider_source_ids)):
        raise ValueError("provider_source_ids must be unique")
    if type(ticket.provider_accounts) is not tuple:
        raise ValueError("provider_accounts must be a tuple")
    provider_accounts: list[tuple[str, str]] = []
    for item in ticket.provider_accounts:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("provider_accounts must contain (source_id, account_id) tuples")
        provider_accounts.append(
            (
                _text(item[0], "provider account source_id"),
                _text(item[1], "provider account_id"),
            )
        )
    if len(provider_accounts) != len(set(provider_accounts)):
        raise ValueError("provider_accounts must be unique")
    currency = _text(ticket.currency, "ticket currency")
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or currency != currency.upper()
    ):
        raise ValueError("ticket currency must be 3-letter uppercase ASCII")
    _timestamp(ticket.placed_at, "ticket placed_at")
    return {
        "ticket_id": ticket_id,
        "stake": _decimal_text(stake),
        "placed_at": ticket.placed_at,
        "currency": currency,
        "bankroll_id": ticket.bankroll_id,
        "provider_source_ids": list(provider_source_ids),
        "provider_accounts": [
            {"source_id": source_id, "account_id": account_id}
            for source_id, account_id in sorted(provider_accounts)
        ],
        "legs": list(legs),
    }


def _structural_groups(
    tickets: tuple[PaperTicket, ...],
) -> tuple[StructuralDependencyGroup, ...]:
    quote_members: dict[str, set[str]] = {}
    market_members: dict[tuple[str | None, str, str], set[str]] = {}
    event_members: dict[tuple[str | None, str], set[str]] = {}

    for ticket in tickets:
        for leg in ticket.legs:
            quote_members.setdefault(leg.quote_key, set()).add(ticket.ticket_id)
            market_members.setdefault(
                (leg.sport, leg.event_id, leg.market_id),
                set(),
            ).add(ticket.ticket_id)
            event_members.setdefault(
                (leg.sport, leg.event_id),
                set(),
            ).add(ticket.ticket_id)

    groups: list[StructuralDependencyGroup] = []
    for kind, mapping in (
        (StructuralDependencyKind.SHARED_QUOTE, quote_members),
        (StructuralDependencyKind.SHARED_MARKET, market_members),
        (StructuralDependencyKind.SHARED_EVENT, event_members),
    ):
        for scope_key, members in mapping.items():
            if len(members) < 2:
                continue
            scope_payload: Any
            if kind is StructuralDependencyKind.SHARED_QUOTE:
                scope_payload = {"quote_key": scope_key}
            elif kind is StructuralDependencyKind.SHARED_MARKET:
                sport, event_id, market_id = scope_key
                scope_payload = {
                    "sport": sport,
                    "event_id": event_id,
                    "market_id": market_id,
                }
            else:
                sport, event_id = scope_key
                scope_payload = {
                    "sport": sport,
                    "event_id": event_id,
                }
            groups.append(
                StructuralDependencyGroup(
                    kind=kind,
                    member_ticket_ids=tuple(sorted(members)),
                    scope_sha256=_digest(
                        {
                            "kind": kind.value,
                            "scope": scope_payload,
                        }
                    ),
                )
            )
    return tuple(
        sorted(
            groups,
            key=lambda item: (
                item.kind.value,
                item.scope_sha256,
                item.member_ticket_ids,
            ),
        )
    )


def evaluate_joint_stress(
    *,
    tickets: tuple[PaperTicket, ...],
    protocol: JointStressProtocol,
) -> JointStressResult:
    """Evaluate explicit joint-tail stress without granting diversification authority."""

    if type(protocol) is not JointStressProtocol:
        raise ValueError("protocol must be exact JointStressProtocol")
    if type(tickets) is not tuple or not tickets:
        raise ValueError("tickets must be a non-empty tuple")
    if any(type(ticket) is not PaperTicket for ticket in tickets):
        raise ValueError("tickets must contain exact PaperTicket values")

    ordered_tickets = tuple(sorted(tickets, key=lambda ticket: ticket.ticket_id))
    ticket_payloads = tuple(_ticket_scope_payload(ticket) for ticket in ordered_tickets)
    ticket_ids = tuple(payload["ticket_id"] for payload in ticket_payloads)
    if len(ticket_ids) != len(set(ticket_ids)):
        raise ValueError("joint stress ticket identities must be unique")

    cutoff = _parsed_timestamp(protocol.causal_cutoff, "causal_cutoff")
    for payload in ticket_payloads:
        if _parsed_timestamp(payload["placed_at"], "ticket placed_at") > cutoff:
            raise ValueError(
                "joint stress ticket cannot be placed after protocol causal_cutoff"
            )

    currencies = {payload["currency"] for payload in ticket_payloads}
    if len(currencies) != 1:
        raise ValueError(
            "cross-currency joint monetary stress requires separate canonical FX authority"
        )
    currency = next(iter(currencies))

    quote_keys = tuple(
        sorted(
            {
                leg.quote_key
                for ticket in ordered_tickets
                for leg in ticket.legs
            }
        )
    )
    if not quote_keys:
        raise ValueError("joint stress scope requires at least one quote")

    ticket_id_set = set(ticket_ids)
    for relation in protocol.relations:
        if not set(relation.member_ticket_ids).issubset(ticket_id_set):
            raise ValueError("dependence relation references ticket outside stress scope")

    quote_key_set = set(quote_keys)
    evaluations: list[JointScenarioEvaluation] = []
    for scenario in protocol.scenarios:
        scenario_keys = {quote_key for quote_key, _ in scenario.settlements}
        if scenario_keys != quote_key_set:
            raise ValueError(
                "every stress scenario must specify the exact portfolio quote-key set"
            )
        profit = PortfolioEngine.scenario_profit_settlements(
            list(ordered_tickets),
            dict(scenario.settlements),
        )
        evaluations.append(
            JointScenarioEvaluation(
                scenario_id=scenario.scenario_id,
                scenario_sha256=scenario.scenario_sha256,
                profit=profit,
            )
        )

    evaluations_tuple = tuple(sorted(evaluations, key=lambda item: item.scenario_id))
    profits = tuple(item.profit for item in evaluations_tuple)
    unresolved = tuple(
        relation.relation_id
        for relation in protocol.relations
        if relation.grade is JointDependenceGrade.UNKNOWN_DEPENDENCE
    )

    portfolio_scope_sha256 = _digest(
        {
            "schema": "autosport.joint_stress_portfolio_scope",
            "schema_version": 1,
            "tickets": list(ticket_payloads),
        }
    )
    return JointStressResult(
        protocol_sha256=protocol.protocol_sha256,
        portfolio_scope_sha256=portfolio_scope_sha256,
        currency=currency,
        ticket_ids=ticket_ids,
        quote_keys=quote_keys,
        structural_groups=_structural_groups(ordered_tickets),
        evaluations=evaluations_tuple,
        unresolved_relation_ids=unresolved,
        worst_observed_profit=min(profits),
        best_observed_profit=max(profits),
        _issuance_token=_RESULT_ISSUANCE_TOKEN,
    )
