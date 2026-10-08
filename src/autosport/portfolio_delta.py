"""Read-only PAPER stake-vector delta bound to a canonical PortfolioPlan and PaperBook.

This projection never opens tickets or turns a plan, model score, market quote or
bookmaker ACK into accepted execution evidence. Scenario/terminal payout and
provider liabilities remain the separate authorities of their owning modules.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .paper import PaperBook
from .portfolio_plan import PortfolioDependencyEvidence, PortfolioDependencyGraph, PortfolioPlan
from .risk import PaperRiskPolicy


def _canonical_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                   allow_nan=False).encode("utf-8")
    ).hexdigest()


def _same_canonical_wire(received: object, expected: object) -> bool:
    """Compare JSON payload types and values without Python bool/int coercion."""
    if type(received) is not type(expected):
        return False
    if type(expected) is dict:
        if any(type(key) is not str for key in received):
            return False
        return received.keys() == expected.keys() and all(
            _same_canonical_wire(received[key], value)
            for key, value in expected.items()
        )
    if type(expected) is list:
        return len(received) == len(expected) and all(
            _same_canonical_wire(left, right)
            for left, right in zip(received, expected, strict=True)
        )
    return received == expected


@dataclass(frozen=True, slots=True)
class PortfolioDelta:
    """Full *stake-vector* cash/commitment projection, not a realized P&L delta.

    Every candidate, including ZERO, is retained in order. The source book,
    owner contract, risk policy and proposal identity are frozen by hashes.
    All amounts are in `currency` and all cash effects are hypothetical PAPER
    debits. Lay liability/partial real fills and terminal payoff are NOT
    inferred from PAPER stake; the caller must keep their own independent proof.
    """

    plan_sha256: str
    source_portfolio_sha256: str
    risk_policy_sha256: str
    economic_goal_contract_sha256: str
    bankroll_id: str
    currency: str
    intent_ids: tuple[str, ...]
    intent_sha256s: tuple[str, ...]
    stake_vector: tuple[Decimal, ...]
    available_cash_before: Decimal
    committed_stake_before: Decimal
    proposed_stake_total: Decimal
    hypothetical_cash_after: Decimal
    hypothetical_committed_stake_after: Decimal
    terminal_economics_proof_sha256: str | None = None

    EFFECT_STATE = "PROPOSED_PAPER_ONLY"

    @classmethod
    def derive(
        cls, book: PaperBook, plan: PortfolioPlan, risk_policy: PaperRiskPolicy
    ) -> "PortfolioDelta":
        if type(book) is not PaperBook:
            raise TypeError("delta requires exact canonical PaperBook")
        if type(plan) is not PortfolioPlan:
            raise TypeError("delta requires exact canonical PortfolioPlan")
        if type(risk_policy) is not PaperRiskPolicy:
            raise TypeError("delta requires exact canonical PaperRiskPolicy")
        # A frozen dataclass is not a use-boundary trust boundary: callers can
        # change it (or its graph) with object.__setattr__ after construction.
        # Validate exact monetary values before invoking class-owned validators.
        if type(plan.stakes) is not tuple or any(type(stake) is not Decimal for stake in plan.stakes):
            raise ValueError("proposal stakes must be exact canonical Decimals")
        if plan.dependency_graph is not None:
            if type(plan.dependency_graph) is not PortfolioDependencyGraph:
                raise ValueError("proposal dependency graph must be exact canonical type")
            PortfolioDependencyGraph.__post_init__(plan.dependency_graph)
        # DependencyEvidence is frozen, but object.__setattr__ can still mutate
        # nested risk/correlation inputs after PortfolioPlan construction.
        # Reject noncanonical numeric subclasses before the validators touch them.
        if plan.dependency_evidence is not None:
            evidence = plan.dependency_evidence
            if type(evidence) is not PortfolioDependencyEvidence:
                raise ValueError("proposal dependency evidence must be exact canonical type")
            fractions = (
                evidence.uncertainty_fraction,
                evidence.fee_fraction,
                evidence.partial_fill_stress_fraction,
            )
            if any(type(value) is not Decimal for value in fractions):
                raise ValueError("proposal dependency fractions must be exact Decimals")
            pairs = evidence.pairwise_dependency_upper_bounds
            if type(pairs) is not tuple or any(
                type(pair) is not tuple or len(pair) != 3 or type(pair[2]) is not Decimal
                for pair in pairs
            ):
                raise ValueError("proposal dependency pair bounds must be exact Decimals")
            PortfolioDependencyEvidence.__post_init__(evidence)
        PortfolioPlan.__post_init__(plan)
        goal = risk_policy.economic_goal
        if type(goal) is not EconomicGoalContract:
            raise ValueError("delta requires an exact owner EconomicGoal")
        EconomicGoalContract.__post_init__(goal)
        PaperBook._validate_loaded_state(book)
        # No caller-provided portfolio hash is trusted for source identity.
        book_hash = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        if book_hash is None or plan.portfolio_sha256 != book_hash:
            raise ValueError("stale or missing exact source portfolio identity")
        goal_sha = provenance_for(goal).contract_sha256
        if plan.economic_goal_contract_sha256 != goal_sha:
            raise ValueError("EconomicGoal source authority mismatch")
        policy_sha = risk_policy.provenance_sha256
        if plan.risk_policy_sha256 != policy_sha:
            raise ValueError("RiskPolicy source authority mismatch")
        if len(plan.stakes) != len(plan.intent_ids) or len(plan.stakes) != len(plan.intent_sha256s):
            raise ValueError("incomplete proposal stake/intent vector")
        if len(plan.stakes) != len(plan.opportunity_classes):
            raise ValueError("incomplete proposal strategy vector")
        if any(type(stake) is not Decimal or not stake.is_finite() or stake < 0
               for stake in plan.stakes):
            raise ValueError("stake vector must contain exact finite nonnegative Decimals")
        if any(stake > 0 for stake in plan.stakes):
            if plan.dependency_graph is None:
                raise ValueError("positive stake requires bound dependency graph")
        total = PaperRiskPolicy._exact_positive_sum(plan.stakes)
        before = book.balance
        committed = book.committed_stake
        after = before if not total else PaperBook._debit_balance(before, total)
        committed_after = PaperRiskPolicy._exact_positive_sum((committed, total))
        return cls(
            plan_sha256=plan.plan_sha256,
            source_portfolio_sha256=book_hash,
            risk_policy_sha256=policy_sha,
            economic_goal_contract_sha256=goal_sha,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            intent_ids=plan.intent_ids,
            intent_sha256s=plan.intent_sha256s,
            stake_vector=plan.stakes,
            available_cash_before=before,
            committed_stake_before=committed,
            proposed_stake_total=total,
            hypothetical_cash_after=after,
            hypothetical_committed_stake_after=committed_after,
            terminal_economics_proof_sha256=(
                None if plan.terminal_economics is None
                else plan.terminal_economics.proof_sha256
            ),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": "autosport.proposed_paper_portfolio_delta",
            "schema_version": 1,
            "effect_state": self.EFFECT_STATE,
            "plan_sha256": self.plan_sha256,
            "source_portfolio_sha256": self.source_portfolio_sha256,
            "risk_policy_sha256": self.risk_policy_sha256,
            "economic_goal_contract_sha256": self.economic_goal_contract_sha256,
            "bankroll_id": self.bankroll_id,
            "currency": self.currency,
            "intent_ids": list(self.intent_ids),
            "intent_sha256s": list(self.intent_sha256s),
            "stake_vector": [str(value) for value in self.stake_vector],
            "available_cash_before": str(self.available_cash_before),
            "committed_stake_before": str(self.committed_stake_before),
            "proposed_stake_total": str(self.proposed_stake_total),
            "hypothetical_cash_after": str(self.hypothetical_cash_after),
            "hypothetical_committed_stake_after": str(self.hypothetical_committed_stake_after),
            "terminal_economics_proof_sha256": self.terminal_economics_proof_sha256,
        }
        return {**payload, "delta_sha256": _canonical_hash(payload)}

    @classmethod
    def readback(
        cls, raw: object, *, book: PaperBook, plan: PortfolioPlan,
        risk_policy: PaperRiskPolicy
    ) -> "PortfolioDelta":
        """Re-derive from current canonical authorities; never trust a serialized delta."""
        expected = cls.derive(book, plan, risk_policy)
        if not _same_canonical_wire(raw, expected.to_dict()):
            raise ValueError("delta payload is stale, tampered or noncanonical")
        return expected
