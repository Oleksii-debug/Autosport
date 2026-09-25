from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Iterable

from .domain import PaperTicket, TicketStatus
from .paper import PaperBook
from .portfolio import PortfolioEngine, _scenario_profit_in_context
from .risk import PaperRiskPolicy
from .scenario_search import ScenarioGroup


def _canonical_text(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _canonical_decimal_identity(value: Decimal) -> tuple[int, tuple[int, ...], int]:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("joint scenario probability must be a finite Decimal")
    parts = value.as_tuple()
    if not isinstance(parts.exponent, int):
        raise ValueError("joint scenario probability exponent must be an integer")
    digits = list(parts.digits)
    exponent = parts.exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if not any(digits):
        return (0, (0,), 0)
    return (parts.sign, tuple(digits), exponent)


def _fraction_to_decimal(value: Fraction) -> Decimal:
    """Convert an exactly Decimal-derived Fraction back to Decimal without rounding."""

    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("joint scenario arithmetic is not exactly Decimal-representable")
    scale = max(twos, fives)
    coefficient = value.numerator
    if twos < scale:
        coefficient *= 2 ** (scale - twos)
    if fives < scale:
        coefficient *= 5 ** (scale - fives)
    if coefficient == 0:
        return Decimal("0")
    digits = tuple(int(item) for item in str(abs(coefficient)))
    return Decimal((int(coefficient < 0), digits, -scale))


@dataclass(frozen=True, slots=True)
class JointScenarioState:
    """One complete state in a caller-supplied explicit joint distribution."""

    state_id: str
    selected_quote_keys: tuple[str, ...]
    probability: Decimal

    def __post_init__(self) -> None:
        _canonical_text(self.state_id, field_name="joint scenario state_id")
        if type(self.selected_quote_keys) is not tuple or not self.selected_quote_keys:
            raise ValueError("joint scenario selected_quote_keys must be a non-empty tuple")
        normalized = tuple(
            _canonical_text(item, field_name="joint scenario quote key")
            for item in self.selected_quote_keys
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("joint scenario state must not repeat a quote key")
        if (
            not isinstance(self.probability, Decimal)
            or not self.probability.is_finite()
            or self.probability <= 0
            or self.probability > 1
        ):
            raise ValueError(
                "joint scenario probability must be a positive finite Decimal at most 1"
            )


@dataclass(frozen=True, slots=True)
class JointScenarioReport:
    """Conditional portfolio mathematics over an explicit joint distribution.

    probability_authority_proven is deliberately non-constructible and always
    false. This report proves deterministic arithmetic and exact input binding
    only. It does not prove that supplied probabilities are scientifically
    estimated, provider-authoritative, executable, or economically profitable.
    """

    mode: str
    scenario_count: int
    observed_worst: Decimal
    observed_best: Decimal
    expected_case: Decimal
    portfolio_sha256: str
    distribution_sha256: str
    probability_authority_proven: bool = field(init=False, default=False)


@dataclass(frozen=True, slots=True)
class _GroupSnapshot:
    group_id: str
    quote_keys: tuple[str, ...]
    declared_probabilities: tuple[tuple[str, Decimal], ...]


def _snapshot_groups(groups: Iterable[ScenarioGroup]) -> tuple[_GroupSnapshot, ...]:
    try:
        materialized = tuple(groups)
    except TypeError as exc:
        raise ValueError("joint scenario groups must be iterable") from exc
    if not materialized:
        raise ValueError("joint scenario groups are required")

    seen_group_ids: set[str] = set()
    seen_quote_keys: set[str] = set()
    snapshots: list[_GroupSnapshot] = []
    for group in materialized:
        if not isinstance(group, ScenarioGroup):
            raise ValueError("joint scenario groups must contain ScenarioGroup values")
        group_id = _canonical_text(group.group_id, field_name="joint scenario group_id")
        if group_id in seen_group_ids:
            raise ValueError("joint scenario group_id values must be unique")
        seen_group_ids.add(group_id)

        if type(group.outcomes) is not tuple or len(group.outcomes) < 2:
            raise ValueError("joint scenario group requires at least two outcomes")
        quote_keys: list[str] = []
        declared: list[tuple[str, Decimal]] = []
        for outcome in group.outcomes:
            quote_key = _canonical_text(
                outcome.quote_key,
                field_name="joint scenario group quote key",
            )
            if quote_key in seen_quote_keys:
                raise ValueError(
                    "joint scenario quote keys must belong to exactly one group"
                )
            seen_quote_keys.add(quote_key)
            quote_keys.append(quote_key)
            probability = outcome.probability
            if probability is not None:
                if (
                    not isinstance(probability, Decimal)
                    or not probability.is_finite()
                    or probability < 0
                    or probability > 1
                ):
                    raise ValueError(
                        "declared marginal probabilities must be finite Decimals between 0 and 1"
                    )
                declared.append((quote_key, probability))

        if declared and len(declared) != len(quote_keys):
            raise ValueError(
                "joint scenario group must declare either all or no marginal probabilities"
            )
        snapshots.append(
            _GroupSnapshot(
                group_id=group_id,
                quote_keys=tuple(sorted(quote_keys)),
                declared_probabilities=tuple(sorted(declared)),
            )
        )
    return tuple(sorted(snapshots, key=lambda item: item.group_id))


def _distribution_digest(
    groups: tuple[_GroupSnapshot, ...],
    states: tuple[JointScenarioState, ...],
) -> str:
    payload = {
        "schema": "autosport.explicit-joint-scenario-distribution.v1",
        "groups": [
            {
                "group_id": group.group_id,
                "quote_keys": list(group.quote_keys),
                "declared_probabilities": [
                    [quote_key, list(_canonical_decimal_identity(probability))]
                    for quote_key, probability in group.declared_probabilities
                ],
            }
            for group in groups
        ],
        "states": [
            {
                "state_id": state.state_id,
                "selected_quote_keys": sorted(state.selected_quote_keys),
                "probability": list(_canonical_decimal_identity(state.probability)),
            }
            for state in sorted(states, key=lambda item: item.state_id)
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def analyse_joint_distribution(
    book: PaperBook,
    groups: Iterable[ScenarioGroup],
    states: Iterable[JointScenarioState],
) -> JointScenarioReport:
    """Evaluate exact portfolio P&L under an explicitly supplied joint distribution.

    This additive analysis exists specifically for correlated/dependent outcome
    structures. It never derives or endorses the probabilities. Positive use of
    this result by scientific promotion or executable risk authority therefore
    requires a separate product-owned evidence contract.
    """

    if not isinstance(book, PaperBook):
        raise ValueError("joint scenario analysis requires a canonical PaperBook")
    before_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if before_sha256 is None:
        raise ValueError("joint scenario analysis requires a valid canonical PaperBook")

    # Bind all scenario economics to one detached canonical PaperBook cut. A live
    # PaperTicket is mutable during settlement, so a tuple of object references is
    # not a snapshot: an ABA mutation can affect one scenario and be restored before
    # the final live-book hash. The detached book must independently hash to the
    # exact pre-analysis commitment, and the live source must still match immediately
    # after capture.
    try:
        book_snapshot = copy.deepcopy(book)
    except Exception as exc:
        raise ValueError("cannot capture canonical PaperBook snapshot") from exc
    snapshot_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book_snapshot)
    capture_after_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if (
        snapshot_sha256 is None
        or snapshot_sha256 != before_sha256
        or capture_after_sha256 != before_sha256
    ):
        raise ValueError("PaperBook changed during joint scenario analysis")

    group_snapshots = _snapshot_groups(groups)
    try:
        state_values = tuple(states)
    except TypeError as exc:
        raise ValueError("joint scenario states must be iterable") from exc
    if not state_values:
        raise ValueError("joint scenario states are required")
    if any(not isinstance(state, JointScenarioState) for state in state_values):
        raise ValueError("joint scenario states must contain JointScenarioState values")

    state_ids = [state.state_id for state in state_values]
    if len(state_ids) != len(set(state_ids)):
        raise ValueError("joint scenario state_id values must be unique")

    quote_to_group: dict[str, str] = {}
    for group in group_snapshots:
        for quote_key in group.quote_keys:
            quote_to_group[quote_key] = group.group_id

    assignment_keys: set[tuple[str, ...]] = set()
    probability_total = Fraction(0, 1)
    marginal_mass = {quote_key: Fraction(0, 1) for quote_key in quote_to_group}
    normalized_states: list[JointScenarioState] = []

    for state in state_values:
        selected = tuple(sorted(state.selected_quote_keys))
        selected_set = set(selected)
        unknown = selected_set.difference(quote_to_group)
        if unknown:
            raise ValueError("joint scenario state contains an unknown quote key")

        group_counts = {group.group_id: 0 for group in group_snapshots}
        for quote_key in selected:
            group_counts[quote_to_group[quote_key]] += 1
        if any(count != 1 for count in group_counts.values()):
            raise ValueError(
                "joint scenario state must select exactly one outcome from every group"
            )

        if selected in assignment_keys:
            raise ValueError(
                "joint scenario distribution must not duplicate an outcome assignment"
            )
        assignment_keys.add(selected)

        probability = Fraction(state.probability)
        probability_total += probability
        for quote_key in selected:
            marginal_mass[quote_key] += probability
        normalized_states.append(state)

    if probability_total != Fraction(1, 1):
        raise ValueError("joint scenario probabilities must sum exactly to 1")

    for group in group_snapshots:
        for quote_key, declared_probability in group.declared_probabilities:
            if marginal_mass[quote_key] != Fraction(declared_probability):
                raise ValueError(
                    "joint scenario distribution contradicts declared marginal probabilities"
                )

    tickets: tuple[PaperTicket, ...] = tuple(
        ticket
        for ticket in book_snapshot.tickets.values()
        if ticket.status is TicketStatus.OPEN
    )
    uncovered_quote_keys = {
        leg.quote_key
        for ticket in tickets
        for leg in ticket.legs
        if leg.quote_key not in quote_to_group
    }
    if uncovered_quote_keys:
        raise ValueError(
            "open portfolio contains quote keys outside the joint scenario distribution"
        )

    weighted_profit = Fraction(0, 1)
    profits: list[Decimal] = []
    for state in normalized_states:
        winners = set(state.selected_quote_keys)
        public_profit = PortfolioEngine.scenario_profit(list(tickets), winners)
        canonical_profit = _scenario_profit_in_context(list(tickets), winners)
        if public_profit != canonical_profit:
            raise ValueError(
                "portfolio scenario profit changed during joint scenario analysis"
            )
        snapshot_after_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(
            book_snapshot
        )
        if snapshot_after_sha256 != snapshot_sha256:
            raise ValueError("PaperBook snapshot changed during joint scenario analysis")
        profits.append(canonical_profit)
        weighted_profit += Fraction(state.probability) * Fraction(canonical_profit)

    after_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if after_sha256 is None or after_sha256 != before_sha256:
        raise ValueError("PaperBook changed during joint scenario analysis")

    return JointScenarioReport(
        mode="explicit-joint-distribution",
        scenario_count=len(normalized_states),
        observed_worst=min(profits),
        observed_best=max(profits),
        expected_case=_fraction_to_decimal(weighted_profit),
        portfolio_sha256=before_sha256,
        distribution_sha256=_distribution_digest(
            group_snapshots,
            tuple(normalized_states),
        ),
    )
