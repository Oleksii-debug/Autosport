"""Bounded blind-policy endurance campaign over ParlayAPI's documented sandbox.

This module is development evidence, not forward evidence.  It deliberately keeps
the decision policy blind to execution mode while the causal audit records every
outcome and reward as ``SIMULATED``.  The real forward-campaign adapters reject
these witnesses, so a sandbox run cannot be relabelled as observed performance.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Final
from urllib.request import Request, urlopen

from .domain import TicketLeg, TicketStatus
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .learning_environment import (
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from .paper import PaperBook
from .transparent_bandit_policy import BanditPolicyState


SANDBOX_URL: Final = (
    "https://parlay-api.com/v1/sandbox/sports/table_tennis/"
    "live/period_markets?period=FT&market=h2h"
)
SIMULATION_MODEL_ID: Final = "parlayapi-sandbox-line-movement-v1"
STATE_SCHEMA: Final = "autosport.account_c.blind_live_sandbox_campaign"
STATE_VERSION: Final = 3
PARLAY_ACTIONS: Final = frozenset(
    {"PARLAY_2_HIGH_ODDS", "PARLAY_2_LOW_ODDS"}
)
SINGLE_ACTIONS: Final = frozenset({"SINGLE_HIGH_ODDS", "SINGLE_LOW_ODDS"})
ACTION_TYPES: Final = frozenset({"WAIT", *PARLAY_ACTIONS, *SINGLE_ACTIONS})
STRATEGY_MIN_OBSERVATIONS: Final = 8
SELECTION_PROTOCOL: Final = "bounded-family-warmup-positive-mean-v3"


class SandboxCampaignError(ValueError):
    """Sandbox data or durable checkpoint is not causally admissible."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SandboxCampaignError("campaign payload is not canonical JSON") from exc


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SandboxCampaignError(f"{field} must be a non-empty canonical string")
    if "\x00" in value:
        raise SandboxCampaignError(f"{field} must not contain NUL")
    value.encode("utf-8", errors="strict")
    return value


def _instant(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SandboxCampaignError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SandboxCampaignError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SandboxCampaignError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _finite_decimal(value: object, field: str) -> Decimal:
    if type(value) is not str:
        raise SandboxCampaignError(f"{field} must be canonical Decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise SandboxCampaignError(f"{field} must be canonical Decimal text") from exc
    if not parsed.is_finite() or str(parsed) != value:
        raise SandboxCampaignError(f"{field} must be canonical Decimal text")
    return parsed


def _american_to_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise SandboxCampaignError("sandbox price must be a non-zero integer")
    if value > 0:
        return Decimal("1") + (Decimal(value) / Decimal("100"))
    return Decimal("1") + (Decimal("100") / Decimal(abs(value)))


@dataclass(frozen=True, slots=True)
class SandboxLiveQuote:
    match_id: str
    market: str
    side: str
    decimal_odds: Decimal
    source_timestamp_ms: int
    age_seconds: int

    def __post_init__(self) -> None:
        _text(self.match_id, "match_id")
        _text(self.market, "market")
        _text(self.side, "side")
        if not isinstance(self.decimal_odds, Decimal):
            raise SandboxCampaignError("decimal_odds must be exact Decimal")
        if not self.decimal_odds.is_finite() or self.decimal_odds <= Decimal("1"):
            raise SandboxCampaignError("decimal_odds must be finite and greater than one")
        for field in ("source_timestamp_ms", "age_seconds"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SandboxCampaignError(f"{field} must be a non-negative integer")

    @property
    def quote_identity(self) -> str:
        return _sha(
            {
                "match_id": self.match_id,
                "market": self.market,
                "side": self.side,
            }
        )

    def payload(self) -> dict[str, object]:
        return {
            "match_id": self.match_id,
            "market": self.market,
            "side": self.side,
            "decimal_odds": str(self.decimal_odds),
            "source_timestamp_ms": self.source_timestamp_ms,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True, slots=True)
class SandboxLiveSnapshot:
    observed_at: str
    available_at: str
    quotes: tuple[SandboxLiveQuote, ...]

    def __post_init__(self) -> None:
        observed = _instant(self.observed_at, "observed_at")
        available = _instant(self.available_at, "available_at")
        if available < observed:
            raise SandboxCampaignError("snapshot cannot be available before observation")
        if type(self.quotes) is not tuple or not self.quotes:
            raise SandboxCampaignError("snapshot quotes must be a non-empty tuple")
        identities = tuple(item.quote_identity for item in self.quotes)
        if identities != tuple(sorted(identities)):
            raise SandboxCampaignError("snapshot quotes must be identity sorted")
        if len(identities) != len(set(identities)):
            raise SandboxCampaignError("snapshot contains duplicate quote identity")

    @property
    def snapshot_sha256(self) -> str:
        return _sha(
            {
                "observed_at": self.observed_at,
                "quotes": [quote.payload() for quote in self.quotes],
            }
        )

    def by_identity(self) -> dict[str, SandboxLiveQuote]:
        return {quote.quote_identity: quote for quote in self.quotes}


@dataclass(frozen=True, slots=True)
class BlindPaperCampaignPlan:
    campaign_id: str
    started_at: str
    cutoff_at: str
    initial_bankroll: Decimal = Decimal("10000")
    currency: str = "UAH"
    max_stake: Decimal = Decimal("50")
    curriculum_period: int = 10
    parlay_slots: int = 7

    def __post_init__(self) -> None:
        _text(self.campaign_id, "campaign_id")
        started = _instant(self.started_at, "started_at")
        cutoff = _instant(self.cutoff_at, "cutoff_at")
        if cutoff <= started:
            raise SandboxCampaignError("cutoff_at must follow started_at")
        if self.currency != "UAH":
            raise SandboxCampaignError("development campaign currency must be UAH")
        for field in ("initial_bankroll", "max_stake"):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise SandboxCampaignError(f"{field} must be a positive exact Decimal")
        if self.initial_bankroll != Decimal("10000"):
            raise SandboxCampaignError("development campaign bankroll must be 10000 UAH")
        if self.max_stake > self.initial_bankroll * Decimal("0.005"):
            raise SandboxCampaignError("max_stake cannot exceed 0.5% of bankroll")
        if (
            isinstance(self.curriculum_period, bool)
            or not isinstance(self.curriculum_period, int)
            or self.curriculum_period <= 0
        ):
            raise SandboxCampaignError("curriculum_period must be positive")
        if (
            isinstance(self.parlay_slots, bool)
            or not isinstance(self.parlay_slots, int)
            or self.parlay_slots <= 0
            or self.parlay_slots >= self.curriculum_period
        ):
            raise SandboxCampaignError("parlay_slots must be inside curriculum period")

    @property
    def plan_sha256(self) -> str:
        return _sha(self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "started_at": self.started_at,
            "cutoff_at": self.cutoff_at,
            "initial_bankroll": str(self.initial_bankroll),
            "currency": self.currency,
            "max_stake": str(self.max_stake),
            "curriculum_period": self.curriculum_period,
            "parlay_slots": self.parlay_slots,
            "selection_protocol": SELECTION_PROTOCOL,
            "strategy_min_observations": STRATEGY_MIN_OBSERVATIONS,
            "truth": EvidenceTruth.SIMULATED.value,
            "simulation_model_id": SIMULATION_MODEL_ID,
            "paper_only": True,
            "real_money_execution": False,
        }


def parse_sandbox_live_payload(
    payload: object,
    *,
    available_at: str,
) -> SandboxLiveSnapshot:
    """Parse only the documented no-auth sandbox period-market response."""

    available = _instant(available_at, "available_at")
    if type(payload) is not dict or set(payload) != {
        "sport_key",
        "period",
        "count",
        "results",
    }:
        raise SandboxCampaignError("sandbox response fields mismatch")
    if payload["sport_key"] != "table_tennis" or payload["period"] != "FT":
        raise SandboxCampaignError("sandbox response scope mismatch")
    results = payload["results"]
    count = payload["count"]
    if type(results) is not list or isinstance(count, bool) or type(count) is not int:
        raise SandboxCampaignError("sandbox response count/results types mismatch")
    if count != len(results) or count <= 0:
        raise SandboxCampaignError("sandbox response count must match non-empty results")
    quotes: list[SandboxLiveQuote] = []
    timestamps: list[int] = []
    for raw in results:
        if type(raw) is not dict or set(raw) != {
            "match_id",
            "sport_key",
            "source",
            "period_key",
            "market",
            "line",
            "side",
            "price",
            "timestamp_ms",
            "age_seconds",
        }:
            raise SandboxCampaignError("sandbox quote fields mismatch")
        if (
            raw["sport_key"] != "table_tennis"
            or raw["source"] != "pinnacle"
            or raw["period_key"] != "FT"
        ):
            raise SandboxCampaignError("sandbox quote scope mismatch")
        timestamp_ms = raw["timestamp_ms"]
        age_seconds = raw["age_seconds"]
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
            or isinstance(age_seconds, bool)
            or not isinstance(age_seconds, int)
            or age_seconds < 0
        ):
            raise SandboxCampaignError("sandbox quote time fields are invalid")
        timestamps.append(timestamp_ms)
        quotes.append(
            SandboxLiveQuote(
                match_id=_text(raw["match_id"], "match_id"),
                market=_text(raw["market"], "market"),
                side=_text(raw["side"], "side"),
                decimal_odds=_american_to_decimal(raw["price"]),
                source_timestamp_ms=timestamp_ms,
                age_seconds=age_seconds,
            )
        )
    observed = datetime.fromtimestamp(max(timestamps) / 1000, tz=timezone.utc)
    if observed > available + timedelta(seconds=1):
        raise SandboxCampaignError("sandbox source timestamp is in the future")
    ordered = tuple(sorted(quotes, key=lambda item: item.quote_identity))
    return SandboxLiveSnapshot(
        observed_at=_timestamp(observed),
        available_at=_timestamp(available),
        quotes=ordered,
    )


def fetch_sandbox_live_snapshot(
    *,
    timeout_seconds: float = 20.0,
) -> SandboxLiveSnapshot:
    request = Request(
        SANDBOX_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "Autosport/0.1 Account-C sandbox diagnostic",
        },
        method="GET",
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        body = response.read().decode("utf-8")
    payload = strict_json_loads(body)
    return parse_sandbox_live_payload(
        payload,
        available_at=_timestamp(datetime.now(timezone.utc)),
    )


def _checkpoint_payload(checkpoint: EnvironmentCheckpoint) -> dict[str, object]:
    return {
        "environment_id": checkpoint.environment_id,
        "episode_id": checkpoint.episode_id,
        "policy_id": checkpoint.policy_id,
        "step_index": checkpoint.step_index,
        "chain_sha256": checkpoint.chain_sha256,
        "last_transition_id": checkpoint.last_transition_id,
        "committed_action_ids": list(checkpoint.committed_action_ids),
        "committed_decision_intents": [
            list(entry) for entry in checkpoint.committed_decision_intents
        ],
    }


def _restore_checkpoint(payload: object) -> EnvironmentCheckpoint:
    expected = {
        "environment_id",
        "episode_id",
        "policy_id",
        "step_index",
        "chain_sha256",
        "last_transition_id",
        "committed_action_ids",
        "committed_decision_intents",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise SandboxCampaignError("environment checkpoint fields mismatch")
    if type(payload["committed_action_ids"]) is not list:
        raise SandboxCampaignError("committed_action_ids must be a list")
    intents = payload["committed_decision_intents"]
    if type(intents) is not list:
        raise SandboxCampaignError("committed_decision_intents must be a list")
    canonical_intents: list[tuple[str, str]] = []
    for entry in intents:
        if type(entry) is not list or len(entry) != 2:
            raise SandboxCampaignError("decision intent entry must have two fields")
        canonical_intents.append((entry[0], entry[1]))
    return EnvironmentCheckpoint(
        environment_id=payload["environment_id"],
        episode_id=payload["episode_id"],
        policy_id=payload["policy_id"],
        step_index=payload["step_index"],
        chain_sha256=payload["chain_sha256"],
        last_transition_id=payload["last_transition_id"],
        committed_action_ids=tuple(payload["committed_action_ids"]),
        committed_decision_intents=tuple(canonical_intents),
    )


class BlindPaperSandboxCampaign:
    """One restart-safe development campaign with a blind decision policy."""

    def __init__(
        self,
        plan: BlindPaperCampaignPlan,
        *,
        seed: int = 23,
        book: PaperBook | None = None,
        policy: BanditPolicyState | None = None,
        checkpoint: EnvironmentCheckpoint | None = None,
        counters: dict[str, int] | None = None,
        last_snapshot_sha256: str | None = None,
    ) -> None:
        if type(plan) is not BlindPaperCampaignPlan:
            raise TypeError("plan must be exact BlindPaperCampaignPlan")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise SandboxCampaignError("seed must be a non-negative integer")
        self.plan = plan
        self.seed = seed
        self.book = book or PaperBook(plan.initial_bankroll)
        self.identity = EnvironmentIdentity(
            source_id="parlayapi-sandbox:table_tennis",
            config_id=plan.plan_sha256,
            data_id="documented-live-period-sandbox-v1",
            protocol_id="account-c-blind-paper-sandbox-v1",
            cutoff_ts=plan.cutoff_at,
            seed=seed,
        )
        if policy is None:
            policy = BanditPolicyState.initial(
                environment_id=self.identity.environment_id,
                protocol_id=self.identity.protocol_id,
                config_sha256=plan.plan_sha256,
                seed=seed,
                action_types=ACTION_TYPES,
            )
        self.policy = policy
        if checkpoint is None:
            self.environment = CausalLearningEnvironment(
                self.identity,
                episode_key=plan.campaign_id,
                policy_id="blind-paper-policy-v1",
                admissible_actions=ACTION_TYPES,
            )
        else:
            self.environment = CausalLearningEnvironment.resume(
                self.identity,
                episode_key=plan.campaign_id,
                policy_id="blind-paper-policy-v1",
                admissible_actions=ACTION_TYPES,
                checkpoint=checkpoint,
            )
        if self.policy.environment_id != self.environment.environment_id:
            raise SandboxCampaignError("policy/environment identity mismatch")
        base = {
            "PARLAY_2": 0,
            "SINGLE": 0,
            "WAIT": 0,
            "POSITIVE": 0,
            "NEGATIVE": 0,
            "NETWORK_SNAPSHOTS": 0,
            "NETWORK_FAILURES": 0,
            "DEDUP_SKIPS": 0,
        }
        supplied = base if counters is None else counters
        if type(supplied) is not dict or set(supplied) != set(base):
            raise SandboxCampaignError("campaign counters fields mismatch")
        if any(
            isinstance(value, bool) or type(value) is not int or value < 0
            for value in supplied.values()
        ):
            raise SandboxCampaignError("campaign counters must be non-negative integers")
        self.counters = dict(supplied)
        self.last_snapshot_sha256 = last_snapshot_sha256

    def _admissible_actions(self, snapshot: SandboxLiveSnapshot) -> frozenset[str]:
        distinct_events = len({quote.match_id for quote in snapshot.quotes})
        slot = self.policy.generation % self.plan.curriculum_period
        if slot < self.plan.parlay_slots:
            if distinct_events < 2:
                return frozenset({"WAIT"})
            return self._explore_then_admit_family(PARLAY_ACTIONS)
        if slot < self.plan.curriculum_period - 1:
            if not snapshot.quotes:
                return frozenset({"WAIT"})
            return self._explore_then_admit_family(SINGLE_ACTIONS)
        actions: set[str] = set()
        if snapshot.quotes:
            actions.update(self._explore_then_admit_family(SINGLE_ACTIONS))
        if distinct_events >= 2:
            actions.update(self._explore_then_admit_family(PARLAY_ACTIONS))
        return frozenset(actions or {"WAIT"})

    def _explore_then_admit_family(
        self,
        family: frozenset[str],
    ) -> frozenset[str]:
        """Collect a bounded warmup per strategy, then let the bandit exploit it.

        The curriculum still controls SINGLE versus PARLAY exposure.  Inside
        that fixed family, a small deterministic cold-start sample is required
        for every strategy before the existing transparent mean-reward policy
        may choose between them.  No future snapshot enters this decision.
        """

        estimates = {item.action_type: item for item in self.policy.estimates}
        under_sampled = sorted(
            (
                estimates[action_type].observations,
                action_type,
            )
            for action_type in family
            if estimates[action_type].observations < STRATEGY_MIN_OBSERVATIONS
        )
        if under_sampled:
            return frozenset({under_sampled[0][1]})
        # A non-positive historical reward is not a reason to keep forcing
        # exposure. WAIT is a genuine causal action and preserves the book.
        # These empirical means are selection feedback, not an edge estimate.
        positive = frozenset(
            action_type for action_type in family
            if estimates[action_type].exact_mean_reward > 0
        )
        return positive or frozenset({"WAIT"})

    @staticmethod
    def _selected_quotes(
        snapshot: SandboxLiveSnapshot,
        action_type: str,
    ) -> tuple[SandboxLiveQuote, ...]:
        if action_type == "WAIT":
            return ()
        if action_type not in PARLAY_ACTIONS | SINGLE_ACTIONS:
            raise SandboxCampaignError("unsupported sandbox selection action")
        selected: list[SandboxLiveQuote] = []
        seen_events: set[str] = set()
        wanted = 1 if action_type in SINGLE_ACTIONS else 2
        high_odds = action_type.endswith("HIGH_ODDS")
        ranked = sorted(
            snapshot.quotes,
            key=lambda item: (item.decimal_odds, item.quote_identity),
            reverse=high_odds,
        )
        for quote in ranked:
            if quote.match_id in seen_events:
                continue
            selected.append(quote)
            seen_events.add(quote.match_id)
            if len(selected) == wanted:
                return tuple(selected)
        raise SandboxCampaignError("snapshot lacks distinct events for chosen action")

    def step(
        self,
        decision_snapshot: SandboxLiveSnapshot,
        outcome_snapshot: SandboxLiveSnapshot,
    ) -> bool:
        if decision_snapshot.snapshot_sha256 == self.last_snapshot_sha256:
            return False
        decision_available = _instant(decision_snapshot.available_at, "decision available_at")
        outcome_available = _instant(outcome_snapshot.available_at, "outcome available_at")
        if outcome_available <= decision_available:
            raise SandboxCampaignError(
                "outcome snapshot must be available after decision snapshot"
            )
        decision_at = _timestamp(decision_available + timedelta(microseconds=1))
        if _instant(self.plan.cutoff_at, "cutoff_at") < outcome_available:
            raise SandboxCampaignError("outcome snapshot exceeds frozen campaign cutoff")

        observation = Observation(
            environment_id=self.environment.environment_id,
            observed_at=decision_snapshot.observed_at,
            available_at=decision_snapshot.available_at,
            evidence=(
                ("market_scope", "table_tennis_live"),
                ("quote_count", str(len(decision_snapshot.quotes))),
                ("snapshot_sha256", decision_snapshot.snapshot_sha256),
            ),
        )
        admissible = self._admissible_actions(decision_snapshot)
        action_type = self.policy.choose(admissible_actions=admissible)
        chosen = self._selected_quotes(decision_snapshot, action_type)
        # PaperBook rejects any implicit Decimal rounding.  Provider odds such as
        # -110 can legitimately leave a 28-digit fractional balance after payout,
        # so use a whole-UAH risk amount that is both exactly subtractable and no
        # greater than 0.5% of the current virtual balance.
        risk_cap = (self.book.balance * Decimal("0.005")).to_integral_value(
            rounding=ROUND_DOWN
        )
        stake = min(self.plan.max_stake, risk_cap)
        if chosen and stake <= 0:
            raise SandboxCampaignError("virtual bankroll is below minimum safe paper stake")
        parameters = (
            ("candidate_count", str(len(chosen))),
            ("paper_stake", "0" if not chosen else str(stake)),
        )
        action = self.environment.act(
            observation,
            action_type=action_type,
            decision_at=decision_at,
            parameters=parameters,
        )

        balance_before = self.book.balance
        evidence_pairs: list[tuple[str, str]] = [
            ("future_snapshot_sha256", outcome_snapshot.snapshot_sha256),
        ]
        if not chosen:
            reward_value = Decimal("0")
        else:
            legs = tuple(
                TicketLeg(
                    event_id=quote.match_id,
                    market_id=quote.market,
                    selection_id=quote.side,
                    locked_odds=quote.decimal_odds,
                    sport="table_tennis",
                )
                for quote in chosen
            )
            ticket = self.book.open_ticket(
                legs,
                stake,
                reason="blind sandbox curriculum; not forward evidence",
                placed_at=decision_at,
                provider_source_ids=("parlayapi-sandbox:table_tennis",),
                bankroll_id="account-c-sandbox-bankroll",
                currency=self.plan.currency,
            )
            future = outcome_snapshot.by_identity()
            winners: set[str] = set()
            voids: set[str] = set()
            for quote, leg in zip(chosen, legs, strict=True):
                later = future.get(quote.quote_identity)
                if later is None or later.decimal_odds == quote.decimal_odds:
                    voids.add(leg.quote_key)
                elif later.decimal_odds < quote.decimal_odds:
                    winners.add(leg.quote_key)
            self.book.settle(
                ticket.ticket_id,
                winners,
                voids,
                settled_at=outcome_snapshot.available_at,
            )
            reward_value = self.book.balance - balance_before
            evidence_pairs.append(("paper_ticket_id", ticket.ticket_id))

        outcome = Outcome(
            environment_id=self.environment.environment_id,
            action_id=action.action_id,
            revealed_at=outcome_snapshot.available_at,
            truth=EvidenceTruth.SIMULATED,
            evidence=tuple(sorted(evidence_pairs)),
            simulation_model_id=SIMULATION_MODEL_ID,
        )
        reward_at = _timestamp(outcome_available + timedelta(microseconds=1))
        reward = RewardEvidence(
            environment_id=self.environment.environment_id,
            action_id=action.action_id,
            outcome_id=outcome.outcome_id,
            reward=reward_value,
            available_at=reward_at,
            truth=EvidenceTruth.SIMULATED,
            simulation_model_id=SIMULATION_MODEL_ID,
        )
        transition = self.environment.resolve(
            action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at=_timestamp(outcome_available + timedelta(microseconds=2)),
        )
        self.policy, _evidence = self.policy.update(
            action=action,
            reward=reward,
            transition=transition,
        )
        if action_type in PARLAY_ACTIONS:
            self.counters["PARLAY_2"] += 1
        elif action_type in SINGLE_ACTIONS:
            self.counters["SINGLE"] += 1
        else:
            self.counters["WAIT"] += 1
        if reward_value > 0:
            self.counters["POSITIVE"] += 1
        elif reward_value < 0:
            self.counters["NEGATIVE"] += 1
        self.last_snapshot_sha256 = decision_snapshot.snapshot_sha256
        return True

    def summary(self) -> dict[str, object]:
        estimates = {
            item.action_type: {
                "observations": item.observations,
                "reward_sum": str(item.reward_sum),
                "mean_reward": str(item.mean_reward),
            }
            for item in self.policy.estimates
        }
        return {
            "schema": STATE_SCHEMA,
            "schema_version": STATE_VERSION,
            "campaign_id": self.plan.campaign_id,
            "plan_sha256": self.plan.plan_sha256,
            "truth": EvidenceTruth.SIMULATED.value,
            "simulation_model_id": SIMULATION_MODEL_ID,
            "paper_only": True,
            "real_money_execution": False,
            "forward_evidence_eligible": False,
            "promotion_authorized": False,
            "profitability_claim": False,
            "policy_generation": self.policy.generation,
            "policy_id": self.policy.policy_id,
            "environment_checkpoint_id": self.environment.checkpoint().checkpoint_id,
            "initial_bankroll": str(self.book.initial_bankroll),
            "final_balance": str(self.book.balance),
            "open_stake": str(self.book.committed_stake),
            "counters": dict(sorted(self.counters.items())),
            "estimates": estimates,
        }

    def diagnostics(self) -> dict[str, object]:
        """Recompute bounded risk diagnostics from authoritative settled tickets.

        This read-only projection never changes the policy and never upgrades
        simulated evidence.  It reconstructs the bankroll chronology instead of
        trusting aggregate counters, then cross-checks both authorities.
        """

        if self.book.committed_stake != Decimal("0"):
            raise SandboxCampaignError(
                "campaign diagnostics require zero open PAPER stake"
            )

        equity = self.book.initial_bankroll
        peak_equity = equity
        maximum_drawdown = Decimal("0")
        maximum_drawdown_peak = equity
        gross_stake = Decimal("0")
        gross_payout = Decimal("0")
        positive_count = 0
        negative_count = 0
        neutral_count = 0
        current_negative_streak = 0
        maximum_negative_streak = 0
        previous_settled: datetime | None = None
        cohorts = {
            "SINGLE": {
                "tickets": 0,
                "won": 0,
                "lost": 0,
                "void": 0,
                "gross_stake": Decimal("0"),
                "gross_payout": Decimal("0"),
                "net_reward": Decimal("0"),
            },
            "PARLAY_2": {
                "tickets": 0,
                "won": 0,
                "lost": 0,
                "void": 0,
                "gross_stake": Decimal("0"),
                "gross_payout": Decimal("0"),
                "net_reward": Decimal("0"),
            },
        }

        for ticket in self.book.tickets.values():
            if ticket.status is TicketStatus.OPEN or ticket.settled_at is None:
                raise SandboxCampaignError(
                    "campaign diagnostics require fully settled PAPER tickets"
                )
            placed = _instant(ticket.placed_at, "ticket placed_at")
            settled = _instant(ticket.settled_at, "ticket settled_at")
            if settled <= placed:
                raise SandboxCampaignError(
                    "campaign diagnostics found non-causal ticket settlement"
                )
            if previous_settled is not None and placed <= previous_settled:
                raise SandboxCampaignError(
                    "campaign diagnostics found overlapping ticket chronology"
                )
            previous_settled = settled

            cohort_name = "SINGLE" if len(ticket.legs) == 1 else "PARLAY_2"
            if len(ticket.legs) not in {1, 2}:
                raise SandboxCampaignError(
                    "campaign diagnostics found unsupported ticket leg count"
                )
            cohort = cohorts[cohort_name]
            reward = ticket.payout - ticket.stake
            equity += reward
            gross_stake += ticket.stake
            gross_payout += ticket.payout
            cohort["tickets"] += 1
            cohort[ticket.status.value] += 1
            cohort["gross_stake"] += ticket.stake
            cohort["gross_payout"] += ticket.payout
            cohort["net_reward"] += reward

            if reward > 0:
                positive_count += 1
                current_negative_streak = 0
            elif reward < 0:
                negative_count += 1
                current_negative_streak += 1
                maximum_negative_streak = max(
                    maximum_negative_streak, current_negative_streak
                )
            else:
                neutral_count += 1

            if equity > peak_equity:
                peak_equity = equity
            drawdown = peak_equity - equity
            if drawdown > maximum_drawdown:
                maximum_drawdown = drawdown
                maximum_drawdown_peak = peak_equity

        if equity != self.book.balance:
            raise SandboxCampaignError(
                "campaign diagnostics bankroll reconstruction mismatch"
            )
        if positive_count != self.counters["POSITIVE"]:
            raise SandboxCampaignError(
                "campaign diagnostics positive counter mismatch"
            )
        if negative_count != self.counters["NEGATIVE"]:
            raise SandboxCampaignError(
                "campaign diagnostics negative counter mismatch"
            )
        if cohorts["SINGLE"]["tickets"] != self.counters["SINGLE"]:
            raise SandboxCampaignError(
                "campaign diagnostics single counter mismatch"
            )
        if cohorts["PARLAY_2"]["tickets"] != self.counters["PARLAY_2"]:
            raise SandboxCampaignError(
                "campaign diagnostics parlay counter mismatch"
            )

        def _cohort_payload(value: dict[str, int | Decimal]) -> dict[str, object]:
            return {
                key: str(item) if type(item) is Decimal else item
                for key, item in sorted(value.items())
            }

        return {
            "schema": "autosport.account_c.blind_paper_diagnostics",
            "schema_version": 1,
            "campaign_id": self.plan.campaign_id,
            "truth": EvidenceTruth.SIMULATED.value,
            "simulation_model_id": SIMULATION_MODEL_ID,
            "paper_only": True,
            "real_money_execution": False,
            "forward_evidence_eligible": False,
            "promotion_authorized": False,
            "profitability_claim": False,
            "ticket_count": len(self.book.tickets),
            "positive_reward_count": positive_count,
            "negative_reward_count": negative_count,
            "neutral_reward_count": neutral_count,
            "maximum_consecutive_negative_rewards": maximum_negative_streak,
            "initial_bankroll": str(self.book.initial_bankroll),
            "final_balance": str(self.book.balance),
            "gross_stake": str(gross_stake),
            "gross_payout": str(gross_payout),
            "net_reward": str(self.book.balance - self.book.initial_bankroll),
            "maximum_drawdown": str(maximum_drawdown),
            "maximum_drawdown_peak": str(maximum_drawdown_peak),
            "open_stake": str(self.book.committed_stake),
            "cohorts": {
                name: _cohort_payload(value)
                for name, value in sorted(cohorts.items())
            },
        }

    def save(self, workspace: str | Path) -> None:
        root = Path(workspace)
        root.mkdir(parents=True, exist_ok=True)
        self.book.save(root / "paper_book.json")
        checkpoint = self.environment.checkpoint()
        atomic_write_json(
            root / "campaign_state.json",
            {
                "schema": STATE_SCHEMA,
                "schema_version": STATE_VERSION,
                "plan": self.plan.payload(),
                "seed": self.seed,
                "policy": self.policy.to_payload(),
                "environment_checkpoint": _checkpoint_payload(checkpoint),
                "counters": dict(sorted(self.counters.items())),
                "last_snapshot_sha256": self.last_snapshot_sha256,
                "summary": self.summary(),
            },
        )

    @classmethod
    def load(cls, workspace: str | Path) -> "BlindPaperSandboxCampaign":
        root = Path(workspace)
        raw = strict_json_loads((root / "campaign_state.json").read_text("utf-8"))
        expected = {
            "schema",
            "schema_version",
            "plan",
            "seed",
            "policy",
            "environment_checkpoint",
            "counters",
            "last_snapshot_sha256",
            "summary",
        }
        if type(raw) is not dict or set(raw) != expected:
            raise SandboxCampaignError("campaign state fields mismatch")
        if raw["schema"] != STATE_SCHEMA or raw["schema_version"] != STATE_VERSION:
            raise SandboxCampaignError("unsupported campaign state schema")
        plan_raw = raw["plan"]
        if type(plan_raw) is not dict:
            raise SandboxCampaignError("campaign plan must be an object")
        plan = BlindPaperCampaignPlan(
            campaign_id=plan_raw["campaign_id"],
            started_at=plan_raw["started_at"],
            cutoff_at=plan_raw["cutoff_at"],
            initial_bankroll=_finite_decimal(plan_raw["initial_bankroll"], "initial_bankroll"),
            currency=plan_raw["currency"],
            max_stake=_finite_decimal(plan_raw["max_stake"], "max_stake"),
            curriculum_period=plan_raw["curriculum_period"],
            parlay_slots=plan_raw["parlay_slots"],
        )
        if plan.payload() != plan_raw:
            raise SandboxCampaignError("campaign plan payload is not canonical")
        campaign = cls(
            plan,
            seed=raw["seed"],
            book=PaperBook.load(root / "paper_book.json"),
            policy=BanditPolicyState.from_payload(raw["policy"]),
            checkpoint=_restore_checkpoint(raw["environment_checkpoint"]),
            counters=raw["counters"],
            last_snapshot_sha256=raw["last_snapshot_sha256"],
        )
        if campaign.summary() != raw["summary"]:
            raise SandboxCampaignError("campaign summary does not match canonical state")
        return campaign


def run_network_endurance(
    campaign: BlindPaperSandboxCampaign,
    *,
    workspace: str | Path,
    duration_seconds: int,
    poll_interval_seconds: int = 30,
    progress: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Run a bounded sandbox diagnostic; network failure never creates evidence."""

    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
        or duration_seconds <= 0
        or duration_seconds > 10_800
    ):
        raise SandboxCampaignError("duration_seconds must be in 1..10800")
    if (
        isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, int)
        or poll_interval_seconds < 5
        or poll_interval_seconds > 300
    ):
        raise SandboxCampaignError("poll_interval_seconds must be in 5..300")
    deadline = time.monotonic() + duration_seconds
    previous: SandboxLiveSnapshot | None = None
    fetch_failures = 0
    fetched = 0
    while time.monotonic() < deadline:
        try:
            current = fetch_sandbox_live_snapshot()
        except Exception as exc:
            fetch_failures += 1
            campaign.counters["NETWORK_FAILURES"] += 1
            event = {
                "event": "account_c_sandbox_progress",
                "truth": EvidenceTruth.SIMULATED.value,
                "network_failure_type": type(exc).__name__,
                "network_snapshots": campaign.counters["NETWORK_SNAPSHOTS"],
                "network_failures": campaign.counters["NETWORK_FAILURES"],
                "policy_generation": campaign.policy.generation,
                "profitability_claim": False,
            }
        else:
            fetched += 1
            campaign.counters["NETWORK_SNAPSHOTS"] += 1
            if previous is not None:
                advanced = campaign.step(previous, current)
                if not advanced:
                    campaign.counters["DEDUP_SKIPS"] += 1
            previous = current
            event = {
                "event": "account_c_sandbox_progress",
                "truth": EvidenceTruth.SIMULATED.value,
                "network_snapshots": campaign.counters["NETWORK_SNAPSHOTS"],
                "network_failures": campaign.counters["NETWORK_FAILURES"],
                "policy_generation": campaign.policy.generation,
                "paper_balance": str(campaign.book.balance),
                "open_stake": str(campaign.book.committed_stake),
                "profitability_claim": False,
            }
        campaign.save(workspace)
        if progress is not None:
            progress(event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(float(poll_interval_seconds), remaining))
    campaign.save(workspace)
    summary = campaign.summary()
    summary["network_snapshots_segment"] = fetched
    summary["network_failures_segment"] = fetch_failures
    atomic_write_json(Path(workspace) / "endurance_summary.json", summary)
    return summary
