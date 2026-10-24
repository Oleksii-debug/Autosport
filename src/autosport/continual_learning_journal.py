"""Durable crash-resume journal for provider-free continual-learning cycles.

The journal adapts the useful operational guarantees of AutoTrade's bounded
SQLite learning loop to Autosport's existing causal learning authorities:

* the complete request is stored before work starts;
* every policy update is checkpointed with compare-and-swap semantics;
* a restart resumes from the last committed policy state;
* final results are recomputed by the canonical in-memory cycle and compared
  with the checkpointed update chain;
* a successful cycle emits a candidate-only Factory handoff envelope, never a
  promotion or execution authorization.

No bookmaker, network, stake, risk, promotion, or deployment authority is
owned by this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from .continual_learning_cycle import (
    ContinualLearningCycleError,
    ContinualLearningRecipe,
    ContinualLearningResult,
    FrozenPolicySuite,
    VerifiedLearningExperience,
    run_continual_learning_cycle,
)
from .learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from .policy_evaluation import (
    PolicyEvaluationCase,
    PolicyRewardMode,
    QualifiedCounterfactualAuthority,
)
from .transparent_bandit_policy import BanditPolicyState


_FINAL_STATES: Final = frozenset({"CANDIDATE_READY", "ROLLED_BACK"})
_SCHEMA_VERSION: Final = 1
_ZERO_SHA256: Final = "0" * 64


class ContinualLearningJournalError(RuntimeError):
    """Durable continual-learning state is invalid or unavailable."""


class ContinualLearningJournalConflict(ContinualLearningJournalError):
    """An immutable cycle identity or optimistic checkpoint was changed."""


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
        raise ContinualLearningJournalError(
            "durable learning payload is not canonical JSON"
        ) from exc


def _decode_json(value: object, *, label: str) -> object:
    if type(value) is not str:
        raise ContinualLearningJournalError(f"{label} is not stored JSON text")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContinualLearningJournalError(f"{label} is invalid JSON") from exc
    if _canonical_json(decoded) != value:
        raise ContinualLearningJournalError(f"{label} is not canonically encoded")
    return decoded


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ContinualLearningJournalError(f"{label} must be canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ContinualLearningJournalError(f"{label} must be valid UTF-8") from exc
    return value


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ContinualLearningJournalError("durable decimal must be finite and exact")
    return str(value)


def _decimal(value: object, *, label: str) -> Decimal:
    if type(value) is not str:
        raise ContinualLearningJournalError(f"{label} must be Decimal text")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ContinualLearningJournalError(f"{label} is invalid Decimal text") from exc
    if not parsed.is_finite() or str(parsed) != value:
        raise ContinualLearningJournalError(f"{label} is not canonical Decimal text")
    return parsed


def _pairs_payload(values: tuple[tuple[str, Decimal], ...]) -> list[list[str]]:
    return [[key, _decimal_text(value)] for key, value in values]


def _pairs(value: object, *, label: str) -> tuple[tuple[str, Decimal], ...]:
    if type(value) is not list or not value:
        raise ContinualLearningJournalError(f"{label} must be a non-empty list")
    result: list[tuple[str, Decimal]] = []
    for index, item in enumerate(value):
        if type(item) is not list or len(item) != 2:
            raise ContinualLearningJournalError(
                f"{label}[{index}] must be a two-item list"
            )
        result.append(
            (
                _text(item[0], label=f"{label}[{index}].action"),
                _decimal(item[1], label=f"{label}[{index}].value"),
            )
        )
    return tuple(result)


def _metadata_payload(values: tuple[tuple[str, str], ...]) -> list[list[str]]:
    return [[key, value] for key, value in values]


def _metadata(value: object, *, label: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not list:
        raise ContinualLearningJournalError(f"{label} must be a list")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if type(item) is not list or len(item) != 2:
            raise ContinualLearningJournalError(
                f"{label}[{index}] must be a two-item list"
            )
        result.append(
            (
                _text(item[0], label=f"{label}[{index}].key"),
                _text(item[1], label=f"{label}[{index}].value"),
            )
        )
    return tuple(result)


def _action_payload(value: Action) -> dict[str, object]:
    return {
        "environment_id": value.environment_id,
        "observation_id": value.observation_id,
        "action_type": value.action_type,
        "decided_at": value.decided_at,
        "parameters": _metadata_payload(value.parameters),
    }


def _action(value: object) -> Action:
    fields = {
        "environment_id",
        "observation_id",
        "action_type",
        "decided_at",
        "parameters",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored action fields mismatch")
    return Action(
        environment_id=value["environment_id"],
        observation_id=value["observation_id"],
        action_type=value["action_type"],
        decided_at=value["decided_at"],
        parameters=_metadata(value["parameters"], label="action.parameters"),
    )


def _reward_payload(value: RewardEvidence) -> dict[str, object]:
    return {
        "environment_id": value.environment_id,
        "action_id": value.action_id,
        "outcome_id": value.outcome_id,
        "reward": _decimal_text(value.reward),
        "available_at": value.available_at,
        "truth": value.truth.value,
        "evidence": _metadata_payload(value.evidence),
        "simulation_model_id": value.simulation_model_id,
    }


def _reward(value: object) -> RewardEvidence:
    fields = {
        "environment_id",
        "action_id",
        "outcome_id",
        "reward",
        "available_at",
        "truth",
        "evidence",
        "simulation_model_id",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored reward fields mismatch")
    try:
        truth = EvidenceTruth(value["truth"])
    except (TypeError, ValueError) as exc:
        raise ContinualLearningJournalError("stored reward truth is invalid") from exc
    return RewardEvidence(
        environment_id=value["environment_id"],
        action_id=value["action_id"],
        outcome_id=value["outcome_id"],
        reward=_decimal(value["reward"], label="reward.reward"),
        available_at=value["available_at"],
        truth=truth,
        evidence=_metadata(value["evidence"], label="reward.evidence"),
        simulation_model_id=value["simulation_model_id"],
    )


def _transition_payload(value: Transition) -> dict[str, object]:
    return {
        "environment_id": value.environment_id,
        "episode_id": value.episode_id,
        "step_index": value.step_index,
        "observation_id": value.observation_id,
        "action_id": value.action_id,
        "outcome_id": value.outcome_id,
        "reward_id": value.reward_id,
        "decision_at": value.decision_at,
        "resolved_at": value.resolved_at,
    }


def _transition(value: object) -> Transition:
    fields = {
        "environment_id",
        "episode_id",
        "step_index",
        "observation_id",
        "action_id",
        "outcome_id",
        "reward_id",
        "decision_at",
        "resolved_at",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored transition fields mismatch")
    return Transition(**value)


def _experience_payload(value: VerifiedLearningExperience) -> dict[str, object]:
    return {
        "sample_id": value.sample_id,
        "source_evidence_sha256": value.source_evidence_sha256,
        "action": _action_payload(value.action),
        "reward": _reward_payload(value.reward),
        "transition": _transition_payload(value.transition),
    }


def _experience(value: object) -> VerifiedLearningExperience:
    fields = {
        "sample_id",
        "source_evidence_sha256",
        "action",
        "reward",
        "transition",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored experience fields mismatch")
    return VerifiedLearningExperience(
        sample_id=value["sample_id"],
        source_evidence_sha256=value["source_evidence_sha256"],
        action=_action(value["action"]),
        reward=_reward(value["reward"]),
        transition=_transition(value["transition"]),
    )


def _case(value: object) -> PolicyEvaluationCase:
    fields = {
        "sample_id",
        "observed_at",
        "reward_available_at",
        "admissible_actions",
        "action_rewards",
        "action_costs",
        "behavior_propensities",
        "reward_truth",
        "reward_mode",
        "source_evidence_sha256",
        "regime_id",
        "historical_action",
        "counterfactual_source_id",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored evaluation case fields mismatch")
    if type(value["admissible_actions"]) is not list:
        raise ContinualLearningJournalError("stored admissible actions must be a list")
    try:
        truth = EvidenceTruth(value["reward_truth"])
        mode = PolicyRewardMode(value["reward_mode"])
    except (TypeError, ValueError) as exc:
        raise ContinualLearningJournalError(
            "stored evaluation truth or mode is invalid"
        ) from exc
    return PolicyEvaluationCase(
        sample_id=value["sample_id"],
        observed_at=value["observed_at"],
        reward_available_at=value["reward_available_at"],
        admissible_actions=tuple(value["admissible_actions"]),
        action_rewards=_pairs(value["action_rewards"], label="case.action_rewards"),
        action_costs=_pairs(value["action_costs"], label="case.action_costs"),
        behavior_propensities=_pairs(
            value["behavior_propensities"], label="case.behavior_propensities"
        ),
        reward_truth=truth,
        reward_mode=mode,
        source_evidence_sha256=value["source_evidence_sha256"],
        regime_id=value["regime_id"],
        historical_action=value["historical_action"],
        counterfactual_source_id=value["counterfactual_source_id"],
    )


def _suite_payload(value: FrozenPolicySuite) -> dict[str, object]:
    return {
        "suite_id": value.suite_id,
        "role": value.role,
        "cases": [case.canonical_payload() for case in value.cases],
    }


def _suite(value: object) -> FrozenPolicySuite:
    if type(value) is not dict or set(value) != {"suite_id", "role", "cases"}:
        raise ContinualLearningJournalError("stored suite fields mismatch")
    if type(value["cases"]) is not list:
        raise ContinualLearningJournalError("stored suite cases must be a list")
    return FrozenPolicySuite(
        suite_id=value["suite_id"],
        role=value["role"],
        cases=tuple(_case(case) for case in value["cases"]),
    )


def _recipe_payload(value: ContinualLearningRecipe) -> dict[str, object]:
    return {
        "champion_policy_id": value.champion_policy_id,
        "retention_suite_sha256": value.retention_suite_sha256,
        "holdout_suite_sha256": value.holdout_suite_sha256,
        "max_updates": value.max_updates,
        "min_holdout_improvement": _decimal_text(value.min_holdout_improvement),
        "max_retention_drop": _decimal_text(value.max_retention_drop),
        "min_effective_sample_size": _decimal_text(value.min_effective_sample_size),
        "require_observed_rewards": value.require_observed_rewards,
    }


def _recipe(value: object) -> ContinualLearningRecipe:
    fields = {
        "champion_policy_id",
        "retention_suite_sha256",
        "holdout_suite_sha256",
        "max_updates",
        "min_holdout_improvement",
        "max_retention_drop",
        "min_effective_sample_size",
        "require_observed_rewards",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored recipe fields mismatch")
    return ContinualLearningRecipe(
        champion_policy_id=value["champion_policy_id"],
        retention_suite_sha256=value["retention_suite_sha256"],
        holdout_suite_sha256=value["holdout_suite_sha256"],
        max_updates=value["max_updates"],
        min_holdout_improvement=_decimal(
            value["min_holdout_improvement"], label="recipe.min_holdout_improvement"
        ),
        max_retention_drop=_decimal(
            value["max_retention_drop"], label="recipe.max_retention_drop"
        ),
        min_effective_sample_size=_decimal(
            value["min_effective_sample_size"],
            label="recipe.min_effective_sample_size",
        ),
        require_observed_rewards=value["require_observed_rewards"],
    )


def _request_payload(
    champion: BanditPolicyState,
    *,
    new_experiences: tuple[VerifiedLearningExperience, ...],
    replay_experiences: tuple[VerifiedLearningExperience, ...],
    retention_suite: FrozenPolicySuite,
    holdout_suite: FrozenPolicySuite,
    recipe: ContinualLearningRecipe,
    completed_at: str,
    counterfactual_authority: QualifiedCounterfactualAuthority | None,
) -> dict[str, object]:
    # Run the canonical validator before any durable state is admitted.  Its
    # result is intentionally discarded; enqueue owns no candidate authority.
    run_continual_learning_cycle(
        champion,
        new_experiences=new_experiences,
        replay_experiences=replay_experiences,
        retention_suite=retention_suite,
        holdout_suite=holdout_suite,
        recipe=recipe,
        completed_at=completed_at,
        counterfactual_authority=counterfactual_authority,
    )
    return {
        "schema": "autosport.durable_continual_learning_request",
        "schema_version": 1,
        "champion": champion.to_payload(),
        "new_experiences": [
            _experience_payload(item)
            for item in sorted(new_experiences, key=lambda item: item.sample_id)
        ],
        "replay_experiences": [
            _experience_payload(item)
            for item in sorted(replay_experiences, key=lambda item: item.sample_id)
        ],
        "retention_suite": _suite_payload(retention_suite),
        "holdout_suite": _suite_payload(holdout_suite),
        "recipe": _recipe_payload(recipe),
        "completed_at": _text(completed_at, label="completed_at"),
        "counterfactual_authority": (
            None
            if counterfactual_authority is None
            else counterfactual_authority.canonical_payload()
        ),
    }


@dataclass(frozen=True, slots=True)
class _Request:
    champion: BanditPolicyState
    new_experiences: tuple[VerifiedLearningExperience, ...]
    replay_experiences: tuple[VerifiedLearningExperience, ...]
    retention_suite: FrozenPolicySuite
    holdout_suite: FrozenPolicySuite
    recipe: ContinualLearningRecipe
    completed_at: str
    counterfactual_authority: QualifiedCounterfactualAuthority | None


def _request(value: object) -> _Request:
    fields = {
        "schema",
        "schema_version",
        "champion",
        "new_experiences",
        "replay_experiences",
        "retention_suite",
        "holdout_suite",
        "recipe",
        "completed_at",
        "counterfactual_authority",
    }
    if type(value) is not dict or set(value) != fields:
        raise ContinualLearningJournalError("stored cycle request fields mismatch")
    if (
        value["schema"] != "autosport.durable_continual_learning_request"
        or value["schema_version"] != 1
    ):
        raise ContinualLearningJournalError("stored cycle request schema is unsupported")
    if type(value["new_experiences"]) is not list or type(
        value["replay_experiences"]
    ) is not list:
        raise ContinualLearningJournalError("stored training populations must be lists")
    authority_payload = value["counterfactual_authority"]
    authority = (
        None
        if authority_payload is None
        else QualifiedCounterfactualAuthority.from_payload(authority_payload)
    )
    result = _Request(
        champion=BanditPolicyState.from_payload(value["champion"]),
        new_experiences=tuple(_experience(item) for item in value["new_experiences"]),
        replay_experiences=tuple(
            _experience(item) for item in value["replay_experiences"]
        ),
        retention_suite=_suite(value["retention_suite"]),
        holdout_suite=_suite(value["holdout_suite"]),
        recipe=_recipe(value["recipe"]),
        completed_at=value["completed_at"],
        counterfactual_authority=authority,
    )
    # Revalidate cross-object invariants after reconstruction.
    run_continual_learning_cycle(
        result.champion,
        new_experiences=result.new_experiences,
        replay_experiences=result.replay_experiences,
        retention_suite=result.retention_suite,
        holdout_suite=result.holdout_suite,
        recipe=result.recipe,
        completed_at=result.completed_at,
        counterfactual_authority=result.counterfactual_authority,
    )
    return result


def _result_payload(result: ContinualLearningResult) -> dict[str, object]:
    handoff = None
    if result.state == "CANDIDATE_READY":
        if result.candidate is None or result.candidate_policy_id is None:
            raise ContinualLearningJournalError("ready result lacks exact candidate")
        handoff = {
            "schema": "autosport.continual_factory_candidate_handoff",
            "schema_version": 1,
            "champion_policy_id": result.champion_policy_id,
            "candidate_policy_id": result.candidate_policy_id,
            "candidate_policy": result.candidate.to_payload(),
            "cycle_evidence_sha256": result.evidence_sha256,
            "recipe_sha256": result.recipe_sha256,
            "new_population_sha256": result.new_population_sha256,
            "replay_population_sha256": result.replay_population_sha256,
            "retention_evaluation_sha256": (
                result.retention_evaluation.evaluation_sha256
            ),
            "holdout_evaluation_sha256": result.holdout_evaluation.evaluation_sha256,
            "promotion_authorized": False,
            "execution_authorized": False,
        }
    return {
        "schema": "autosport.durable_continual_learning_result",
        "schema_version": 1,
        "state": result.state,
        "champion_policy_id": result.champion_policy_id,
        "candidate_policy_id": result.candidate_policy_id,
        "evaluated_candidate_policy_id": result.updates[-1].successor_policy_id,
        "recipe_sha256": result.recipe_sha256,
        "new_population_sha256": result.new_population_sha256,
        "replay_population_sha256": result.replay_population_sha256,
        "updates": [
            {
                "index": item.index,
                "population": item.population,
                "sample_id": item.sample_id,
                "experience_id": item.experience_id,
                "update_id": item.update_id,
                "predecessor_policy_id": item.predecessor_policy_id,
                "successor_policy_id": item.successor_policy_id,
            }
            for item in result.updates
        ],
        "retention_evaluation": result.retention_evaluation.canonical_payload(),
        "holdout_evaluation": result.holdout_evaluation.canonical_payload(),
        "rollback_to_policy_id": result.rollback_to_policy_id,
        "qualification_reasons": list(result.qualification_reasons),
        "evidence_sha256": result.evidence_sha256,
        "promotion_authorized": False,
        "execution_authorized": False,
        "factory_handoff": handoff,
    }


@dataclass(frozen=True, slots=True)
class DurableCycleStatus:
    cycle_id: str
    request_sha256: str
    state: str
    completed_updates: int
    total_updates: int
    checkpoint_policy_id: str
    result_evidence_sha256: str | None
    candidate_policy_id: str | None
    promotion_authorized: bool = False
    execution_authorized: bool = False


class ContinualLearningJournal:
    """SQLite owner for restart-safe, candidate-only continual cycles."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connection()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS journal_meta(
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    schema_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS continual_cycles(
                    cycle_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    checkpoint_revision INTEGER NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    result_json TEXT
                );
                CREATE TABLE IF NOT EXISTS continual_cycle_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    predecessor_event_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES continual_cycles(cycle_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS continual_cycle_request_identity
                    ON continual_cycles(request_sha256);
                """
            )
            row = connection.execute(
                "SELECT schema_version FROM journal_meta WHERE id = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO journal_meta(id, schema_version) VALUES(1, ?)",
                    (_SCHEMA_VERSION,),
                )
            elif row[0] != _SCHEMA_VERSION:
                raise ContinualLearningJournalError(
                    "unsupported durable continual-learning journal schema"
                )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _checkpoint_payload(
        policy: BanditPolicyState,
        *,
        next_update_index: int,
        update_witnesses: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "schema": "autosport.continual_learning_checkpoint",
            "schema_version": 1,
            "next_update_index": next_update_index,
            "policy": policy.to_payload(),
            "update_witnesses": update_witnesses,
        }

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        cycle_id: str,
        phase: str,
        payload: dict[str, object],
    ) -> None:
        row = connection.execute(
            """
            SELECT event_sha256 FROM continual_cycle_events
            WHERE cycle_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (cycle_id,),
        ).fetchone()
        predecessor = _ZERO_SHA256 if row is None else row[0]
        event_payload = {
            "cycle_id": cycle_id,
            "phase": phase,
            "payload": payload,
            "predecessor_event_sha256": predecessor,
        }
        event_sha256 = _digest(event_payload)
        connection.execute(
            """
            INSERT INTO continual_cycle_events(
                cycle_id, phase, payload_json,
                predecessor_event_sha256, event_sha256
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                phase,
                _canonical_json(payload),
                predecessor,
                event_sha256,
            ),
        )

    def enqueue(
        self,
        cycle_id: str,
        champion: BanditPolicyState,
        *,
        new_experiences: tuple[VerifiedLearningExperience, ...],
        replay_experiences: tuple[VerifiedLearningExperience, ...],
        retention_suite: FrozenPolicySuite,
        holdout_suite: FrozenPolicySuite,
        recipe: ContinualLearningRecipe,
        completed_at: str,
        counterfactual_authority: QualifiedCounterfactualAuthority | None = None,
    ) -> tuple[DurableCycleStatus, bool]:
        cycle_id = _text(cycle_id, label="cycle_id")
        request_payload = _request_payload(
            champion,
            new_experiences=new_experiences,
            replay_experiences=replay_experiences,
            retention_suite=retention_suite,
            holdout_suite=holdout_suite,
            recipe=recipe,
            completed_at=completed_at,
            counterfactual_authority=counterfactual_authority,
        )
        request_json = _canonical_json(request_payload)
        request_sha256 = _digest(request_payload)
        checkpoint = self._checkpoint_payload(
            champion,
            next_update_index=0,
            update_witnesses=[],
        )
        with closing(self._connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_sha256, request_json FROM continual_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if row is not None:
                connection.execute("COMMIT")
                if row["request_sha256"] != request_sha256 or row["request_json"] != request_json:
                    raise ContinualLearningJournalConflict(
                        "cycle_id is already bound to a different immutable request"
                    )
                return self.status(cycle_id), False
            duplicate = connection.execute(
                "SELECT cycle_id FROM continual_cycles WHERE request_sha256 = ?",
                (request_sha256,),
            ).fetchone()
            if duplicate is not None:
                connection.execute("ROLLBACK")
                raise ContinualLearningJournalConflict(
                    "identical immutable request already has cycle_id "
                    + duplicate["cycle_id"]
                )
            connection.execute(
                """
                INSERT INTO continual_cycles(
                    cycle_id, request_sha256, request_json, state,
                    checkpoint_revision, checkpoint_json, result_json
                ) VALUES(?, ?, ?, 'QUEUED', 0, ?, NULL)
                """,
                (cycle_id, request_sha256, request_json, _canonical_json(checkpoint)),
            )
            self._append_event(
                connection,
                cycle_id=cycle_id,
                phase="QUEUED",
                payload={
                    "request_sha256": request_sha256,
                    "champion_policy_id": champion.policy_id,
                    "recipe_sha256": recipe.recipe_sha256,
                },
            )
            connection.execute("COMMIT")
        return self.status(cycle_id), True

    def _row(self, cycle_id: str) -> sqlite3.Row:
        cycle_id = _text(cycle_id, label="cycle_id")
        with closing(self._connection()) as connection:
            row = connection.execute(
                "SELECT * FROM continual_cycles WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
        if row is None:
            raise ContinualLearningJournalError(f"unknown cycle_id: {cycle_id}")
        return row

    def _validated_row(self, cycle_id: str) -> tuple[sqlite3.Row, _Request, dict[str, object]]:
        row = self._row(cycle_id)
        request_payload = _decode_json(row["request_json"], label="cycle request")
        if _digest(request_payload) != row["request_sha256"]:
            raise ContinualLearningJournalError("cycle request digest mismatch")
        request = _request(request_payload)
        checkpoint = _decode_json(row["checkpoint_json"], label="cycle checkpoint")
        expected_checkpoint_fields = {
            "schema",
            "schema_version",
            "next_update_index",
            "policy",
            "update_witnesses",
        }
        if type(checkpoint) is not dict or set(checkpoint) != expected_checkpoint_fields:
            raise ContinualLearningJournalError("cycle checkpoint fields mismatch")
        if (
            checkpoint["schema"] != "autosport.continual_learning_checkpoint"
            or checkpoint["schema_version"] != 1
            or type(checkpoint["next_update_index"]) is not int
            or checkpoint["next_update_index"] < 0
            or type(checkpoint["update_witnesses"]) is not list
        ):
            raise ContinualLearningJournalError("cycle checkpoint is invalid")
        policy = BanditPolicyState.from_payload(checkpoint["policy"])
        if checkpoint["next_update_index"] == 0:
            if policy != request.champion or checkpoint["update_witnesses"]:
                raise ContinualLearningJournalError(
                    "initial checkpoint does not match immutable champion"
                )
        elif len(checkpoint["update_witnesses"]) != checkpoint["next_update_index"]:
            raise ContinualLearningJournalError(
                "checkpoint witness count does not match update index"
            )
        return row, request, checkpoint

    def status(self, cycle_id: str) -> DurableCycleStatus:
        row, request, checkpoint = self._validated_row(cycle_id)
        result_payload = None
        if row["result_json"] is not None:
            result_payload = _decode_json(row["result_json"], label="cycle result")
            if type(result_payload) is not dict:
                raise ContinualLearningJournalError("cycle result must be an object")
        candidate_policy_id = (
            None if result_payload is None else result_payload.get("candidate_policy_id")
        )
        evidence_sha256 = (
            None if result_payload is None else result_payload.get("evidence_sha256")
        )
        total_updates = len(request.new_experiences) + len(request.replay_experiences)
        policy = BanditPolicyState.from_payload(checkpoint["policy"])
        return DurableCycleStatus(
            cycle_id=cycle_id,
            request_sha256=row["request_sha256"],
            state=row["state"],
            completed_updates=checkpoint["next_update_index"],
            total_updates=total_updates,
            checkpoint_policy_id=policy.policy_id,
            result_evidence_sha256=evidence_sha256,
            candidate_policy_id=candidate_policy_id,
        )

    def _persist_checkpoint(
        self,
        *,
        cycle_id: str,
        expected_revision: int,
        checkpoint: dict[str, object],
        event_payload: dict[str, object],
    ) -> int:
        with closing(self._connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE continual_cycles
                SET state = 'TRAINING', checkpoint_revision = checkpoint_revision + 1,
                    checkpoint_json = ?
                WHERE cycle_id = ? AND checkpoint_revision = ?
                  AND state NOT IN ('CANDIDATE_READY', 'ROLLED_BACK')
                """,
                (_canonical_json(checkpoint), cycle_id, expected_revision),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise ContinualLearningJournalConflict(
                    "checkpoint compare-and-swap failed; reload durable cycle"
                )
            self._append_event(
                connection,
                cycle_id=cycle_id,
                phase="TRAINING_CHECKPOINT",
                payload=event_payload,
            )
            connection.execute("COMMIT")
        return expected_revision + 1

    def run(
        self,
        cycle_id: str,
        *,
        fail_after_updates_for_testing: int | None = None,
    ) -> ContinualLearningResult:
        """Resume one stored cycle and return the canonical deterministic result."""

        row, request, checkpoint = self._validated_row(cycle_id)
        if fail_after_updates_for_testing is not None and (
            type(fail_after_updates_for_testing) is not int
            or fail_after_updates_for_testing < 1
        ):
            raise ValueError("fail_after_updates_for_testing must be a positive integer")
        ordered = tuple(
            ("new", item)
            for item in sorted(request.new_experiences, key=lambda item: item.sample_id)
        ) + tuple(
            ("replay", item)
            for item in sorted(request.replay_experiences, key=lambda item: item.sample_id)
        )
        next_index = checkpoint["next_update_index"]
        if next_index > len(ordered):
            raise ContinualLearningJournalError("checkpoint exceeds frozen update budget")
        policy = BanditPolicyState.from_payload(checkpoint["policy"])
        witnesses = list(checkpoint["update_witnesses"])

        # Rebuild the exact committed prefix from immutable inputs before trusting
        # a persisted policy snapshot.  This detects checkpoint or witness tamper.
        replayed = request.champion
        expected_witnesses: list[dict[str, object]] = []
        for index, (population, item) in enumerate(ordered[:next_index], start=1):
            predecessor = replayed
            replayed, update = replayed.update(
                action=item.action,
                reward=item.reward,
                transition=item.transition,
            )
            expected_witnesses.append(
                {
                    "index": index,
                    "population": population,
                    "sample_id": item.sample_id,
                    "experience_id": item.experience_id,
                    "update_id": update.update_id,
                    "predecessor_policy_id": predecessor.policy_id,
                    "successor_policy_id": replayed.policy_id,
                }
            )
        if replayed != policy or witnesses != expected_witnesses:
            raise ContinualLearningJournalError(
                "checkpoint policy or update witnesses do not replay exactly"
            )

        revision = row["checkpoint_revision"]
        committed_in_this_call = 0
        for index in range(next_index, len(ordered)):
            population, item = ordered[index]
            predecessor = policy
            policy, update = policy.update(
                action=item.action,
                reward=item.reward,
                transition=item.transition,
            )
            witness = {
                "index": index + 1,
                "population": population,
                "sample_id": item.sample_id,
                "experience_id": item.experience_id,
                "update_id": update.update_id,
                "predecessor_policy_id": predecessor.policy_id,
                "successor_policy_id": policy.policy_id,
            }
            witnesses.append(witness)
            checkpoint = self._checkpoint_payload(
                policy,
                next_update_index=index + 1,
                update_witnesses=witnesses,
            )
            revision = self._persist_checkpoint(
                cycle_id=cycle_id,
                expected_revision=revision,
                checkpoint=checkpoint,
                event_payload=witness,
            )
            committed_in_this_call += 1
            if fail_after_updates_for_testing == committed_in_this_call:
                raise RuntimeError("injected crash after durable learning checkpoint")

        result = run_continual_learning_cycle(
            request.champion,
            new_experiences=request.new_experiences,
            replay_experiences=request.replay_experiences,
            retention_suite=request.retention_suite,
            holdout_suite=request.holdout_suite,
            recipe=request.recipe,
            completed_at=request.completed_at,
            counterfactual_authority=request.counterfactual_authority,
        )
        if result.updates[-1].successor_policy_id != policy.policy_id:
            raise ContinualLearningJournalError(
                "canonical final replay differs from durable checkpoint policy"
            )
        result_payload = _result_payload(result)
        result_json = _canonical_json(result_payload)
        if row["state"] in _FINAL_STATES:
            if row["result_json"] != result_json:
                raise ContinualLearningJournalError(
                    "final durable result differs from canonical replay"
                )
            return result

        with closing(self._connection()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state, checkpoint_revision, result_json FROM continual_cycles WHERE cycle_id = ?",
                (cycle_id,),
            ).fetchone()
            if current is None:
                connection.execute("ROLLBACK")
                raise ContinualLearningJournalError("cycle disappeared during finalization")
            if current["state"] in _FINAL_STATES:
                connection.execute("COMMIT")
                if current["result_json"] != result_json:
                    raise ContinualLearningJournalConflict(
                        "concurrent final result differs from canonical replay"
                    )
                return result
            if current["checkpoint_revision"] != revision:
                connection.execute("ROLLBACK")
                raise ContinualLearningJournalConflict(
                    "checkpoint changed before final publication"
                )
            cursor = connection.execute(
                """
                UPDATE continual_cycles
                SET state = ?, result_json = ?
                WHERE cycle_id = ? AND checkpoint_revision = ?
                  AND result_json IS NULL
                """,
                (result.state, result_json, cycle_id, revision),
            )
            if cursor.rowcount != 1:
                connection.execute("ROLLBACK")
                raise ContinualLearningJournalConflict(
                    "final result compare-and-swap failed"
                )
            self._append_event(
                connection,
                cycle_id=cycle_id,
                phase=result.state,
                payload={
                    "evidence_sha256": result.evidence_sha256,
                    "candidate_policy_id": result.candidate_policy_id,
                    "rollback_to_policy_id": result.rollback_to_policy_id,
                    "promotion_authorized": False,
                    "execution_authorized": False,
                },
            )
            connection.execute("COMMIT")
        return result

    def candidate_handoff(self, cycle_id: str) -> dict[str, object]:
        """Return a replay-verified candidate envelope for the canonical Factory."""

        # Recompute the canonical result before exposing anything downstream;
        # this also rejects any altered durable result payload.
        self.run(cycle_id)
        row, _, _ = self._validated_row(cycle_id)
        if row["state"] != "CANDIDATE_READY" or row["result_json"] is None:
            raise ContinualLearningJournalError(
                "cycle has no qualified candidate handoff"
            )
        payload = _decode_json(row["result_json"], label="cycle result")
        if type(payload) is not dict or type(payload.get("factory_handoff")) is not dict:
            raise ContinualLearningJournalError("candidate handoff is missing")
        handoff = payload["factory_handoff"]
        if handoff.get("promotion_authorized") is not False or handoff.get(
            "execution_authorized"
        ) is not False:
            raise ContinualLearningJournalError(
                "candidate handoff cannot contain promotion or execution authority"
            )
        candidate = BanditPolicyState.from_payload(handoff.get("candidate_policy"))
        if candidate.policy_id != handoff.get("candidate_policy_id"):
            raise ContinualLearningJournalError("candidate handoff policy identity mismatch")
        return json.loads(_canonical_json(handoff))

    def verify(self, cycle_id: str) -> bool:
        """Verify the request, checkpoint, event chain, and any final result."""

        row, _, _ = self._validated_row(cycle_id)
        predecessor = _ZERO_SHA256
        phases: list[str] = []
        with closing(self._connection()) as connection:
            events = connection.execute(
                """
                SELECT phase, payload_json, predecessor_event_sha256, event_sha256
                FROM continual_cycle_events WHERE cycle_id = ? ORDER BY sequence
                """,
                (cycle_id,),
            ).fetchall()
        if not events:
            raise ContinualLearningJournalError("cycle event chain is empty")
        for event in events:
            payload = _decode_json(event["payload_json"], label="cycle event")
            phases.append(event["phase"])
            if event["predecessor_event_sha256"] != predecessor:
                raise ContinualLearningJournalError("cycle event predecessor mismatch")
            expected = _digest(
                {
                    "cycle_id": cycle_id,
                    "phase": event["phase"],
                    "payload": payload,
                    "predecessor_event_sha256": predecessor,
                }
            )
            if event["event_sha256"] != expected:
                raise ContinualLearningJournalError("cycle event digest mismatch")
            predecessor = expected
        if phases[0] != "QUEUED":
            raise ContinualLearningJournalError("cycle event chain lacks QUEUED origin")
        status = self.status(cycle_id)
        if phases.count("TRAINING_CHECKPOINT") != status.completed_updates:
            raise ContinualLearningJournalError(
                "cycle event chain does not cover every durable checkpoint"
            )
        if row["state"] in _FINAL_STATES:
            if phases[-1] != row["state"]:
                raise ContinualLearningJournalError(
                    "cycle event chain lacks matching terminal event"
                )
            self.run(cycle_id)
        elif row["result_json"] is not None:
            raise ContinualLearningJournalError(
                "non-final cycle unexpectedly has a result"
            )
        return True


__all__ = [
    "ContinualLearningJournal",
    "ContinualLearningJournalConflict",
    "ContinualLearningJournalError",
    "DurableCycleStatus",
]
