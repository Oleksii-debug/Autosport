"""Causal research preferences from settled, including declined, opportunities.

This memory never posts to PaperBook or the executed-reward learner. Its returns
are hypothetical per-unit results. Risk, settlement and promotion remain owned
by their existing authorities. A source timestamp is not independently verified
merely because it passes the chronology checks here.
"""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from .integrity import atomic_write_json, durable_path_lock
from .json_integrity import strict_json_loads


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("shadow timestamp requires timezone")
    return result.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text(value):
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError("canonical nonempty text required")
    return value


@dataclass(frozen=True)
class ShadowMemoryPlan:
    actions: tuple[str, ...]
    window: int = 100
    minimum_observations: int = 30
    minimum_mean_return: str = "0.02"

    def __post_init__(self):
        if type(self.actions) is not tuple or not self.actions or tuple(sorted(set(self.actions))) != self.actions:
            raise ValueError("shadow actions must be sorted and distinct")
        for action in self.actions:
            if text(action) == "WAIT":
                raise ValueError("WAIT cannot carry hypothetical wager feedback")
        if type(self.window) is not int or not 1 <= self.window <= 10000:
            raise ValueError("invalid shadow window")
        if type(self.minimum_observations) is not int or not 1 <= self.minimum_observations <= self.window:
            raise ValueError("invalid minimum shadow support")
        if type(self.minimum_mean_return) is not str:
            raise ValueError("minimum return requires decimal text")
        minimum = Decimal(self.minimum_mean_return)
        if not minimum.is_finite() or not 0 <= minimum <= 1:
            raise ValueError("invalid minimum mean return")

    def payload(self):
        return {**vars(self), "actions": list(self.actions)}


class ShadowStrategyMemory:
    def __init__(self, plan: ShadowMemoryPlan):
        self.plan = plan
        self._records = []
        self._identities = {}
        self._persisted_sha = None
        self._window_sums = {action: Fraction() for action in plan.actions}

    def observe(self, *, event_id: str, decision_at: str, result_available_at: str,
                now: str, net_returns: dict[str, Decimal], source_sha256: str,
                availability_basis: str):
        text(event_id)
        if availability_basis not in {"recorded_source_timestamp_unverified", "retrospective_assumed_delay"}:
            raise ValueError("explicit source availability basis required")
        if type(source_sha256) is not str or len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
            raise ValueError("source digest required")
        if not instant(decision_at) < instant(result_available_at) <= instant(now):
            raise ValueError("shadow feedback must be revealed after decision and by now")
        if type(net_returns) is not dict or set(net_returns) != set(self.plan.actions):
            raise ValueError("complete frozen action feedback required")
        if any(type(value) is not Decimal or not value.is_finite() for value in net_returns.values()):
            raise ValueError("finite exact Decimal shadow returns required")
        record = {"event_id": event_id, "decision_at": decision_at,
                  "result_available_at": result_available_at, "ingested_at": now,
                  "net_returns": {key: str(net_returns[key]) for key in self.plan.actions},
                  "source_sha256": source_sha256, "availability_basis": availability_basis}
        # Arrival time is not a new observation. An identical retry cannot train twice.
        identity = digest({key: value for key, value in record.items() if key != "ingested_at"})
        if event_id in self._identities:
            if self._identities[event_id] != identity:
                raise ValueError("conflicting feedback for an existing shadow event")
            return False
        if self._records and instant(now) < instant(self._records[-1]["ingested_at"]):
            raise ValueError("shadow ingestion time cannot go backwards")
        self._records.append(record)
        self._identities[event_id] = identity
        for action in self.plan.actions:
            self._window_sums[action] += Fraction(net_returns[action])
            if len(self._records) > self.plan.window:
                self._window_sums[action] -= Fraction(Decimal(self._records[-self.plan.window - 1]["net_returns"][action]))
        return True

    def choose(self, *, as_of: str, admissible_actions: frozenset[str]):
        decision_time = instant(as_of)
        if type(admissible_actions) is not frozenset or not admissible_actions or not admissible_actions <= frozenset((*self.plan.actions, "WAIT")):
            raise ValueError("invalid externally admissible actions")
        if "WAIT" not in admissible_actions:
            raise ValueError("research preference requires externally admissible WAIT")
        if self._records and decision_time < instant(self._records[-1]["ingested_at"]):
            raise ValueError("cannot choose in the past using future memory")
        support = min(len(self._records), self.plan.window)
        if support < self.plan.minimum_observations:
            return "WAIT"
        means = {action: self._window_sums[action] / support
                 for action in self.plan.actions if action in admissible_actions}
        profitable = [action for action, mean in means.items()
                      if mean > Fraction(Decimal(self.plan.minimum_mean_return))]
        return min(profitable, key=lambda action: (-means[action], action)) if profitable else "WAIT"

    def payload(self):
        return {"schema": "autosport.shadow_strategy_memory", "schema_version": 1,
                "plan": self.plan.payload(), "records": deepcopy(self._records),
                "reward_basis": "hypothetical_net_return_per_unit",
                "execution_authorized": False, "promotion_authorized": False}

    def save(self, path: Path):
        with durable_path_lock(path):
            if path.exists():
                previous = strict_json_loads(path.read_text("utf-8"))
                if digest(previous) != self._persisted_sha:
                    raise ValueError("stale shadow writer or changed checkpoint")
            elif self._persisted_sha is not None:
                raise ValueError("previous shadow checkpoint disappeared")
            payload = self.payload()
            checkpoint = {"payload": payload, "sha256": digest(payload)}
            atomic_write_json(path, checkpoint)
            self._persisted_sha = digest(checkpoint)

    @classmethod
    def load(cls, path: Path):
        with durable_path_lock(path):
            checkpoint = strict_json_loads(path.read_text("utf-8"))
            if type(checkpoint) is not dict or set(checkpoint) != {"payload", "sha256"} or digest(checkpoint["payload"]) != checkpoint["sha256"]:
                raise ValueError("shadow checkpoint integrity mismatch")
            payload = checkpoint["payload"]
            raw_plan = payload["plan"]
            memory = cls(ShadowMemoryPlan(**{**raw_plan, "actions": tuple(raw_plan["actions"])}))
            for row in payload["records"]:
                if not memory.observe(event_id=row["event_id"], decision_at=row["decision_at"],
                                      result_available_at=row["result_available_at"], now=row["ingested_at"],
                                      net_returns={key: Decimal(value) for key, value in row["net_returns"].items()},
                                      source_sha256=row["source_sha256"], availability_basis=row["availability_basis"]):
                    raise ValueError("duplicate shadow checkpoint record")
            if memory.payload() != payload:
                raise ValueError("shadow checkpoint is not canonical")
            memory._persisted_sha = digest(checkpoint)
            return memory
