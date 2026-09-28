"""Restart-safe PAPER settlement to AgentLoop learning composition.

The bridge owns no economic or settlement truth. It binds canonical DecisionLedger,
PaperBook, settlement evidence and AgentLoop authorities, and persists only the
minimum decision/ticket binding plus learner outbox/ack needed for crash recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, DecimalException, Inexact, InvalidOperation, Overflow, Underflow, localcontext
from pathlib import Path
from typing import Final

from .agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
from .continuous_session import SettlementResolution
from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .domain import PaperTicket, TicketStatus
from .economic_goal import EconomicGoalContract
from .economic_goal_provenance import provenance_for
from .learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
    Transition,
)
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .paper import PaperBook
from .risk import PaperRiskPolicy
from .workspace_lock import WorkspaceEconomicLock


SCHEMA: Final = "autosport.paper_settlement_learning_bridge"
SCHEMA_VERSION: Final = 1
REWARD_RULE: Final = "paper-net-payout-minus-stake-v1"
COST_RULE: Final = "paperbook-no-extra-cost-v1"
OBSERVED_REWARD_REFERENCE_SCHEMA: Final = "autosport.paper_observed_reward_reference"
OBSERVED_REWARD_REFERENCE_VERSION: Final = 1
OBSERVED_REWARD_ISSUER: Final = "autosport.paper_settlement_learning"
BOUND: Final = "BOUND"
OUTBOX: Final = "OUTBOX"
ACKED: Final = "ACKED"
INVALIDATED: Final = "INVALIDATED"
ACKED_REWARD_INVALIDATION_SCHEMA: Final = "autosport.paper_acked_reward_invalidation"
ACKED_REWARD_INVALIDATION_VERSION: Final = 1
ACKED_REWARD_INVALIDATION_REASON: Final = "ACKED_REWARD_CONTRADICTED_BY_LATER_SETTLEMENT"
_HEX: Final = frozenset("0123456789abcdef")
_ABSTAIN: Final = frozenset({"WAIT", "NO_BET", "ABSTAIN"})
_ACTION_DECISION_ID_PARAMETER: Final = "economic_decision_id"
_ACTION_TICKET_ID_PARAMETER: Final = "paper_ticket_id"
_PAPER_DECISION_AGENT_ACTION_BINDINGS: Final = frozenset(
    {
        ("OPEN_PAPER_TICKET", "PAPER_PROPOSAL"),
        ("OPEN_PAPER_VALUE_TICKET", "PAPER_PROPOSAL"),
    }
)


class PaperSettlementLearningBridgeError(RuntimeError):
    """Binding or cross-authority evidence is incomplete or conflicting."""


@dataclass(frozen=True, slots=True)
class PaperSettlementLearningWitness:
    """Read-only typed view of one durable learner outbox.

    ``PaperSettlementLearningBridge`` remains the sole authority that derives a
    PAPER outcome/reward from canonical settlement evidence.  A downstream
    campaign terminalizer needs the resulting typed evidence to perform
    attribution and reflection without re-reading private bridge JSON or
    reimplementing settlement arithmetic.  This object is intentionally a
    witness, not a second persistence or economic authority.
    """

    ticket_id: str
    binding_id: str
    settlement_bundle_sha256: str
    observation: Observation
    action: Action
    outcome: Outcome
    reward: RewardEvidence
    transition: Transition
    baseline_checkpoint: EnvironmentCheckpoint
    next_checkpoint: EnvironmentCheckpoint

    def __post_init__(self) -> None:
        _text(self.ticket_id, "ticket_id")
        _sha(self.binding_id, "binding_id")
        _sha(self.settlement_bundle_sha256, "settlement_bundle_sha256")
        if not isinstance(self.observation, Observation):
            raise TypeError("observation must be Observation")
        if not isinstance(self.action, Action):
            raise TypeError("action must be Action")
        if not isinstance(self.outcome, Outcome):
            raise TypeError("outcome must be Outcome")
        if not isinstance(self.reward, RewardEvidence):
            raise TypeError("reward must be RewardEvidence")
        if not isinstance(self.transition, Transition):
            raise TypeError("transition must be Transition")
        if not isinstance(self.baseline_checkpoint, EnvironmentCheckpoint):
            raise TypeError("baseline_checkpoint must be EnvironmentCheckpoint")
        if not isinstance(self.next_checkpoint, EnvironmentCheckpoint):
            raise TypeError("next_checkpoint must be EnvironmentCheckpoint")
        if (
            self.observation.environment_id != self.action.environment_id
            or self.action.environment_id != self.outcome.environment_id
            or self.outcome.environment_id != self.reward.environment_id
            or self.reward.environment_id != self.transition.environment_id
            or self.transition.environment_id != self.baseline_checkpoint.environment_id
            or self.transition.environment_id != self.next_checkpoint.environment_id
        ):
            raise PaperSettlementLearningBridgeError(
                "witness environment identities do not match"
            )
        if (
            self.observation.observation_id != self.action.observation_id
            or self.action.action_id != self.outcome.action_id
            or self.action.action_id != self.reward.action_id
            or self.action.action_id != self.transition.action_id
            or self.outcome.outcome_id != self.reward.outcome_id
            or self.outcome.outcome_id != self.transition.outcome_id
            or self.reward.reward_id != self.transition.reward_id
            or self.baseline_checkpoint.episode_id != self.transition.episode_id
            or self.next_checkpoint.episode_id != self.transition.episode_id
            or self.next_checkpoint.step_index != self.baseline_checkpoint.step_index + 1
            or self.next_checkpoint.last_transition_id != self.transition.transition_id
        ):
            raise PaperSettlementLearningBridgeError(
                "witness causal identities do not match"
            )


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
        raise PaperSettlementLearningBridgeError("bridge evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PaperSettlementLearningBridgeError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PaperSettlementLearningBridgeError(f"{name} must be valid UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise PaperSettlementLearningBridgeError(f"{name} must be lowercase SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperSettlementLearningBridgeError(f"{name} must be timezone-aware ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PaperSettlementLearningBridgeError(f"{name} must be timezone-aware ISO-8601")
    return parsed


def _instant_id(value: object, name: str) -> str:
    return _instant(value, name).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PaperSettlementLearningBridgeError(f"bridge JSON duplicate key: {key}")
        result[key] = value
    return result


def _decimal_context() -> Context:
    context = Context(prec=28, Emin=-999999, Emax=999999)
    context.traps[Inexact] = True
    context.traps[InvalidOperation] = True
    context.traps[Overflow] = True
    context.traps[Underflow] = True
    context.clear_flags()
    return context


def _exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    if not isinstance(left, Decimal) or not left.is_finite():
        raise PaperSettlementLearningBridgeError("payout must be a finite exact Decimal")
    if not isinstance(right, Decimal) or not right.is_finite():
        raise PaperSettlementLearningBridgeError("stake must be a finite exact Decimal")
    try:
        with localcontext(_decimal_context()):
            result = left - right
    except DecimalException as exc:
        raise PaperSettlementLearningBridgeError("net PAPER reward is not exactly representable") from exc
    return result


def _ticket_payload(ticket: PaperTicket) -> dict[str, object]:
    return {
        "ticket_id": ticket.ticket_id,
        "stake": str(ticket.stake),
        "placed_at": ticket.placed_at,
        "strategy_reason": ticket.strategy_reason,
        "provider_source_ids": list(ticket.provider_source_ids),
        "provider_accounts": [list(value) for value in ticket.provider_accounts],
        "bankroll_id": ticket.bankroll_id,
        "currency": ticket.currency,
        "legs": [
            {
                "event_id": leg.event_id,
                "market_id": leg.market_id,
                "selection_id": leg.selection_id,
                "locked_odds": str(leg.locked_odds),
                "sport": leg.sport,
                "quote_key": leg.quote_key,
            }
            for leg in ticket.legs
        ],
    }


def _checkpoint_payload(checkpoint: EnvironmentCheckpoint) -> dict[str, object]:
    return {
        "environment_id": checkpoint.environment_id,
        "episode_id": checkpoint.episode_id,
        "policy_id": checkpoint.policy_id,
        "step_index": checkpoint.step_index,
        "chain_sha256": checkpoint.chain_sha256,
        "last_transition_id": checkpoint.last_transition_id,
        "committed_action_ids": list(checkpoint.committed_action_ids),
        "committed_decision_intents": [list(value) for value in checkpoint.committed_decision_intents],
    }


def _checkpoint(raw: object) -> EnvironmentCheckpoint:
    if type(raw) is not dict:
        raise PaperSettlementLearningBridgeError("checkpoint payload must be an object")
    try:
        return EnvironmentCheckpoint(
            environment_id=raw["environment_id"],
            episode_id=raw["episode_id"],
            policy_id=raw["policy_id"],
            step_index=raw["step_index"],
            chain_sha256=raw["chain_sha256"],
            last_transition_id=raw["last_transition_id"],
            committed_action_ids=tuple(raw["committed_action_ids"]),
            committed_decision_intents=tuple(
                (item[0], item[1]) for item in raw["committed_decision_intents"]
            ),
        )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise PaperSettlementLearningBridgeError("checkpoint payload is not canonical") from exc


def _decision_sha(record: DecisionRecord) -> str:
    return _digest(record.to_dict())


def _expected_ticket_economics(
    ticket: PaperTicket,
    known: dict[str, str],
) -> tuple[TicketStatus, Decimal] | None:
    leg_keys = {leg.quote_key for leg in ticket.legs}
    if ticket.status is TicketStatus.LOST:
        if "loss" not in known.values():
            return None
        return TicketStatus.LOST, Decimal("0")
    if set(known) != leg_keys:
        return None
    if "loss" in known.values():
        return TicketStatus.LOST, Decimal("0")
    effective = [leg for leg in ticket.legs if known[leg.quote_key] != "void"]
    if not effective:
        return TicketStatus.VOID, ticket.stake
    try:
        with localcontext(_decimal_context()):
            payout = ticket.stake
            for leg in effective:
                if known[leg.quote_key] != "win":
                    raise PaperSettlementLearningBridgeError(
                        "settled winning ticket contains a non-winning effective leg"
                    )
                payout *= leg.locked_odds
    except DecimalException as exc:
        raise PaperSettlementLearningBridgeError("PAPER payout is not exactly representable") from exc
    return TicketStatus.WON, payout


class PaperSettlementLearningBridge:
    """Durable identity bridge from settled PAPER economics to one AgentLoop resolution."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        paper_book_path: str | Path,
        decision_ledger: JsonlDecisionLedger,
        agent_loop: AgentLoopRuntime,
        economic_goal: EconomicGoalContract,
        risk_policy: PaperRiskPolicy,
        monotonic_authority_root: str | Path | None = None,
    ) -> None:
        if not isinstance(decision_ledger, JsonlDecisionLedger):
            raise TypeError("decision_ledger must be JsonlDecisionLedger")
        if not isinstance(agent_loop, AgentLoopRuntime):
            raise TypeError("agent_loop must be AgentLoopRuntime")
        if not isinstance(economic_goal, EconomicGoalContract):
            raise TypeError("economic_goal must be EconomicGoalContract")
        if not isinstance(risk_policy, PaperRiskPolicy):
            raise TypeError("risk_policy must be PaperRiskPolicy")
        if risk_policy.economic_goal != economic_goal:
            raise PaperSettlementLearningBridgeError(
                "risk policy is not bound to supplied EconomicGoalContract"
            )
        self.state_path = Path(state_path)
        self.paper_book_path = Path(paper_book_path)
        self.decision_ledger = decision_ledger
        self.agent_loop = agent_loop
        self.economic_goal = economic_goal
        self.risk_policy = risk_policy
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._invalidation_authority = MonotonicWorkspaceAuthority(
                workspace=self.state_path.parent.resolve(strict=False),
                domain="learning.paper-settlement-learning.invalidation",
                key=self.state_path.name,
                authority_root=monotonic_authority_root,
            )
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperSettlementLearningBridgeError(
                "cannot initialize independent reward-invalidation rollback authority"
            ) from exc
        self._invalidation_semantic_binding_sha256 = _digest(
            {
                "schema": "autosport.paper_acked_reward_invalidation.monotonic",
                "schema_version": 1,
                "bridge_schema": SCHEMA,
                "bridge_schema_version": SCHEMA_VERSION,
                "state_name": self.state_path.name,
            }
        )
        with WorkspaceEconomicLock(self.state_path.parent):
            if self.state_path.exists():
                self._read()
            else:
                self._recover_invalidation_authority(
                    state_sha256=None,
                    invalidated=False,
                )
                self._write({"bindings": {}})

    @property
    def goal_fingerprint(self) -> str:
        return provenance_for(self.economic_goal).contract_sha256

    @property
    def risk_fingerprint(self) -> str:
        return self.risk_policy.provenance_sha256

    @staticmethod
    def _fsync_parent_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _recover_invalidation_authority(
        self,
        *,
        state_sha256: str | None,
        invalidated: bool,
    ) -> None:
        try:
            history = self._invalidation_authority.read_history()
            if not history:
                if invalidated:
                    raise PaperSettlementLearningBridgeError(
                        "durable reward invalidation lacks independent rollback authority"
                    )
                return
            latest = history[-1]
            observed = state_sha256 if invalidated else None
            if latest.phase is AuthorityPhase.PREPARE:
                recovery = self._invalidation_authority.recover(
                    observed_state_sha256=observed,
                    tx_id=(
                        latest.tx_id
                        if invalidated
                        and state_sha256 == latest.intended_state_sha256
                        else None
                    ),
                    semantic_binding_sha256=(
                        latest.semantic_binding_sha256
                        if invalidated
                        and state_sha256 == latest.intended_state_sha256
                        else None
                    ),
                )
            else:
                recovery = self._invalidation_authority.recover(
                    observed_state_sha256=observed,
                )
            if invalidated:
                if (
                    state_sha256 is None
                    or recovery.committed_state_sha256 != state_sha256
                ):
                    raise PaperSettlementLearningBridgeError(
                        "durable reward invalidation is not the committed rollback-authority tip"
                    )
            elif recovery.committed_state_sha256 is not None:
                raise PaperSettlementLearningBridgeError(
                    "bridge state rolled back before committed reward invalidation"
                )
        except PaperSettlementLearningBridgeError:
            raise
        except MonotonicWorkspaceAuthorityError as exc:
            raise PaperSettlementLearningBridgeError(
                "bridge state failed independent reward-invalidation rollback authority"
            ) from exc

    def _write(
        self,
        payload: dict[str, object],
        *,
        protect_invalidation: bool = False,
    ) -> None:
        bare = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "bindings": payload["bindings"],
        }
        state = {**bare, "state_sha256": _digest(bare)}
        authority_tx_id: str | None = None
        if protect_invalidation:
            if not any(
                type(binding) is dict and binding.get("status") == INVALIDATED
                for binding in payload["bindings"].values()
            ):
                raise PaperSettlementLearningBridgeError(
                    "invalidation rollback seal requires terminal INVALIDATED state"
                )
            try:
                history = self._invalidation_authority.read_history()
                authority_tx_id = (
                    f"reward-invalidation:{len(history) + 1}:"
                    f"{state['state_sha256'][:24]}"
                )
                self._invalidation_authority.prepare(
                    tx_id=authority_tx_id,
                    observed_state_sha256=None,
                    intended_state_sha256=state["state_sha256"],
                    semantic_binding_sha256=self._invalidation_semantic_binding_sha256,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise PaperSettlementLearningBridgeError(
                    "cannot prepare independent reward-invalidation rollback fence"
                ) from exc
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                newline="\n",
                dir=self.state_path.parent,
                prefix=f".{self.state_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(
                    state,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            self._fsync_parent_directory(self.state_path.parent)
            if protect_invalidation:
                assert authority_tx_id is not None
                try:
                    self._invalidation_authority.commit(
                        tx_id=authority_tx_id,
                        observed_state_sha256=state["state_sha256"],
                        semantic_binding_sha256=(
                            self._invalidation_semantic_binding_sha256
                        ),
                    )
                except MonotonicWorkspaceAuthorityError as exc:
                    raise PaperSettlementLearningBridgeError(
                        "cannot commit independent reward-invalidation rollback fence"
                    ) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _read(self) -> dict[str, object]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            state = json.loads(
                raw,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    PaperSettlementLearningBridgeError(
                        f"bridge JSON contains non-finite value {value}"
                    )
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperSettlementLearningBridgeError("bridge state is unreadable") from exc
        if type(state) is not dict or set(state) != {
            "schema",
            "schema_version",
            "bindings",
            "state_sha256",
        }:
            raise PaperSettlementLearningBridgeError("bridge state schema mismatch")
        if state["schema"] != SCHEMA or state["schema_version"] != SCHEMA_VERSION:
            raise PaperSettlementLearningBridgeError("unsupported bridge schema")
        if type(state["bindings"]) is not dict:
            raise PaperSettlementLearningBridgeError("bridge bindings must be an object")
        bare = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "bindings": state["bindings"],
        }
        if _sha(state["state_sha256"], "state_sha256") != _digest(bare):
            raise PaperSettlementLearningBridgeError("bridge state digest mismatch")
        for ticket_id, binding in state["bindings"].items():
            _text(ticket_id, "ticket_id")
            if type(binding) is not dict or binding.get("ticket_id") != ticket_id:
                raise PaperSettlementLearningBridgeError("bridge ticket binding mismatch")
            if binding.get("status") not in {BOUND, OUTBOX, ACKED, INVALIDATED}:
                raise PaperSettlementLearningBridgeError("bridge status is invalid")
            _sha(binding.get("binding_id"), "binding_id")
            _sha(binding.get("ticket_identity_sha256"), "ticket_identity_sha256")
            _sha(binding.get("decision_sha256"), "decision_sha256")
            _sha(binding.get("environment_id"), "environment_id")
            _sha(binding.get("episode_id"), "episode_id")
            _sha(binding.get("observation_id"), "observation_id")
            _sha(binding.get("action_id"), "action_id")
            _sha(binding.get("economic_goal_fingerprint"), "economic_goal_fingerprint")
            _sha(binding.get("risk_fingerprint"), "risk_fingerprint")
            _checkpoint(binding.get("baseline_checkpoint"))
            campaign_plan_anchor = binding.get("campaign_plan_anchor")
            if campaign_plan_anchor is not None:
                if (
                    type(campaign_plan_anchor) is not dict
                    or set(campaign_plan_anchor) != {
                        "plan_id",
                        "reflection_available_at",
                    }
                ):
                    raise PaperSettlementLearningBridgeError(
                        "campaign plan anchor fields mismatch"
                    )
                _sha(campaign_plan_anchor["plan_id"], "campaign plan_id")
                canonical_anchor_time = _instant_id(
                    campaign_plan_anchor["reflection_available_at"],
                    "campaign reflection_available_at",
                )
                if (
                    canonical_anchor_time
                    != campaign_plan_anchor["reflection_available_at"]
                ):
                    raise PaperSettlementLearningBridgeError(
                        "campaign reflection_available_at must be canonical UTC"
                    )
            intent = binding.get("settlement_intent")
            if intent is not None:
                self._intent_resolutions(intent)
                if (
                    intent["binding_id"] != binding["binding_id"]
                    or intent["ticket_id"] != ticket_id
                ):
                    raise PaperSettlementLearningBridgeError(
                        "settlement intent belongs to another ticket binding"
                    )
            invalidation = binding.get("invalidation")
            if binding["status"] == INVALIDATED:
                self._validate_acknowledged_invalidation(binding, invalidation)
            elif invalidation is not None:
                raise PaperSettlementLearningBridgeError(
                    "non-invalidated binding carries reward invalidation evidence"
                )
        invalidated = any(
            binding["status"] == INVALIDATED
            for binding in state["bindings"].values()
        )
        self._recover_invalidation_authority(
            state_sha256=state["state_sha256"] if invalidated else None,
            invalidated=invalidated,
        )
        return state

    def _runtime_matches(
        self,
        action: Action,
        baseline_checkpoint: EnvironmentCheckpoint,
    ) -> None:
        snapshot = self.agent_loop.snapshot()
        if snapshot.economic_goal_fingerprint != self.goal_fingerprint:
            raise PaperSettlementLearningBridgeError(
                "AgentLoop economic-goal fingerprint differs from owner contract"
            )
        if snapshot.risk_fingerprint != self.risk_fingerprint:
            raise PaperSettlementLearningBridgeError(
                "AgentLoop risk fingerprint differs from canonical PaperRiskPolicy"
            )
        if snapshot.phase is not AgentLoopPhase.WAIT_OUTCOME:
            raise PaperSettlementLearningBridgeError(
                "learning binding requires AgentLoop WAIT_OUTCOME"
            )
        if (
            snapshot.environment_id != action.environment_id
            or snapshot.observation_id != action.observation_id
            or snapshot.action_id != action.action_id
        ):
            raise PaperSettlementLearningBridgeError(
                "supplied action differs from AgentLoop durable current action"
            )
        if (
            snapshot.environment_checkpoint_id != baseline_checkpoint.checkpoint_id
            or snapshot.environment_id != baseline_checkpoint.environment_id
            or snapshot.episode_id != baseline_checkpoint.episode_id
            or snapshot.policy_id != baseline_checkpoint.policy_id
        ):
            raise PaperSettlementLearningBridgeError(
                "baseline checkpoint differs from AgentLoop durable environment head"
            )
        if snapshot.transition_id is not None:
            raise PaperSettlementLearningBridgeError("AgentLoop action is already resolved")
        if snapshot.external_effect_state is not ExternalEffectState.PAPER_ONLY:
            raise PaperSettlementLearningBridgeError(
                "learning binding requires durable PAPER_ONLY action effect"
            )
        if action.action_type.upper() in _ABSTAIN:
            raise PaperSettlementLearningBridgeError(
                "WAIT/NO_BET/ABSTAIN cannot bind a settled-ticket reward"
            )

    @staticmethod
    def _decision_context_matches(
        record: DecisionRecord,
        observation: Observation,
    ) -> None:
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if record.context_hash != observation.observation_id:
            raise PaperSettlementLearningBridgeError(
                "economic decision context_hash does not bind exact learning Observation"
            )
        if _instant(record.observed_ts, "decision observed_ts") != _instant(
            observation.observed_at, "observation observed_at"
        ):
            raise PaperSettlementLearningBridgeError(
                "economic decision observed_ts differs from exact learning Observation"
            )

    def verify_decision_observation_binding(
        self,
        *,
        decision_id: str,
        observation: Observation,
    ) -> str:
        """Verify immutable economic-decision context before AgentLoop mutation."""

        canonical_decision_id = _text(decision_id, "decision_id")
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        decision = self.decision_ledger.verified_economic_decision(
            canonical_decision_id,
            self.economic_goal,
            risk_policy=self.risk_policy,
        )
        self._decision_context_matches(decision, observation)
        return _decision_sha(decision)

    @staticmethod
    def _decision_matches(
        record: DecisionRecord,
        ticket: PaperTicket,
        action: Action,
        observation: Observation,
    ) -> None:
        PaperSettlementLearningBridge._decision_context_matches(record, observation)
        payload = record.payload
        action_parameters = dict(action.parameters)
        decision_action = _text(record.action, "economic decision action")
        agent_action_type = _text(action.action_type, "AgentLoop action_type")
        if (
            decision_action,
            agent_action_type,
        ) not in _PAPER_DECISION_AGENT_ACTION_BINDINGS:
            raise PaperSettlementLearningBridgeError(
                "economic decision action is not semantically bound to AgentLoop action_type"
            )
        if (
            action_parameters.get(_ACTION_DECISION_ID_PARAMETER) != record.decision_id
            or action_parameters.get(_ACTION_TICKET_ID_PARAMETER) != ticket.ticket_id
        ):
            raise PaperSettlementLearningBridgeError(
                "AgentLoop action is not bound to supplied economic decision and PaperTicket"
            )
        if payload.get("ticket_id") != ticket.ticket_id:
            raise PaperSettlementLearningBridgeError(
                "economic decision does not bind exact PaperTicket"
            )
        if payload.get("stake") != str(ticket.stake):
            raise PaperSettlementLearningBridgeError(
                "economic decision stake differs from PaperTicket"
            )
        keys = tuple(sorted(leg.quote_key for leg in ticket.legs))
        if "quote_keys" in payload:
            if tuple(payload["quote_keys"]) != keys:
                raise PaperSettlementLearningBridgeError(
                    "economic decision quote_keys differ from PaperTicket"
                )
        elif "quote_key" in payload:
            if len(keys) != 1 or payload["quote_key"] != keys[0]:
                raise PaperSettlementLearningBridgeError(
                    "economic decision quote_key differs from PaperTicket"
                )
        else:
            raise PaperSettlementLearningBridgeError(
                "economic decision lacks exact quote identity"
            )
        if _instant(record.observed_ts, "decision observed_ts") > _instant(
            action.decided_at, "action decided_at"
        ):
            raise PaperSettlementLearningBridgeError(
                "economic decision observation occurs after AgentLoop action"
            )

    def bind_ticket(
        self,
        *,
        ticket_id: str,
        decision_id: str,
        environment: CausalLearningEnvironment,
        observation: Observation,
        action: Action,
        baseline_checkpoint: EnvironmentCheckpoint,
    ) -> str:
        """Bind durable decision and action to one still-open durable PaperTicket."""

        _text(ticket_id, "ticket_id")
        _text(decision_id, "decision_id")
        if not isinstance(environment, CausalLearningEnvironment):
            raise TypeError("environment must be CausalLearningEnvironment")
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if not isinstance(action, Action):
            raise TypeError("action must be Action")
        if not isinstance(baseline_checkpoint, EnvironmentCheckpoint):
            raise TypeError("baseline_checkpoint must be EnvironmentCheckpoint")
        if (
            environment.environment_id != action.environment_id
            or observation.environment_id != action.environment_id
            or observation.observation_id != action.observation_id
        ):
            raise PaperSettlementLearningBridgeError(
                "environment/observation/action identities do not match"
            )
        if (
            environment.episode.episode_id != baseline_checkpoint.episode_id
            or environment.episode.policy_id != baseline_checkpoint.policy_id
            or environment.environment_id != baseline_checkpoint.environment_id
        ):
            raise PaperSettlementLearningBridgeError(
                "environment episode differs from baseline checkpoint"
            )
        replay = CausalLearningEnvironment.resume(
            environment.identity,
            episode_key=environment.episode.episode_key,
            policy_id=environment.episode.policy_id,
            admissible_actions=frozenset(environment.episode.admissible_actions),
            checkpoint=baseline_checkpoint,
        )
        replayed_action = replay.act(
            observation,
            action_type=action.action_type,
            decision_at=action.decided_at,
            parameters=action.parameters,
        )
        if replayed_action.action_id != action.action_id:
            raise PaperSettlementLearningBridgeError(
                "action is not reproducible from canonical environment evidence"
            )

        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read()
            book = PaperBook.load(self.paper_book_path)
            ticket = book.tickets.get(ticket_id)
            if ticket is None:
                raise PaperSettlementLearningBridgeError("durable PaperTicket is missing")
            if ticket.status is not TicketStatus.OPEN or ticket.payout != Decimal("0"):
                raise PaperSettlementLearningBridgeError(
                    "ticket must be bound before settlement"
                )
            if _instant(ticket.placed_at, "ticket placed_at") < _instant(
                action.decided_at, "action decided_at"
            ):
                raise PaperSettlementLearningBridgeError(
                    "PaperTicket predates bound AgentLoop action"
                )
            if (
                ticket.bankroll_id != self.economic_goal.bankroll_id
                or ticket.currency != self.economic_goal.currency
            ):
                raise PaperSettlementLearningBridgeError(
                    "PaperTicket bankroll/currency differs from owner contract"
                )
            self._runtime_matches(action, baseline_checkpoint)
            decision = self.decision_ledger.verified_economic_decision(
                decision_id,
                self.economic_goal,
                risk_policy=self.risk_policy,
            )
            self._decision_matches(decision, ticket, action, observation)
            semantic = {
                "ticket_id": ticket.ticket_id,
                "ticket_identity_sha256": _digest(_ticket_payload(ticket)),
                "decision_id": decision.decision_id,
                "decision_sha256": _decision_sha(decision),
                "environment_id": action.environment_id,
                "environment_identity": {
                    "source_id": environment.identity.source_id,
                    "config_id": environment.identity.config_id,
                    "data_id": environment.identity.data_id,
                    "protocol_id": environment.identity.protocol_id,
                    "cutoff_ts": environment.identity.cutoff_ts,
                    "seed": environment.identity.seed,
                },
                "episode_id": baseline_checkpoint.episode_id,
                "episode_key": environment.episode.episode_key,
                "policy_id": baseline_checkpoint.policy_id,
                "admissible_actions": list(environment.episode.admissible_actions),
                "observation_id": action.observation_id,
                "observation": {
                    "environment_id": observation.environment_id,
                    "observed_at": _instant_id(
                        observation.observed_at, "observation observed_at"
                    ),
                    "available_at": _instant_id(
                        observation.available_at, "observation available_at"
                    ),
                    "evidence": [list(item) for item in observation.evidence],
                },
                "action_id": action.action_id,
                "action_type": action.action_type,
                "action_decided_at": _instant_id(action.decided_at, "action decided_at"),
                "action_parameters": [list(item) for item in action.parameters],
                "economic_goal_fingerprint": self.goal_fingerprint,
                "risk_fingerprint": self.risk_fingerprint,
                "baseline_checkpoint": _checkpoint_payload(baseline_checkpoint),
            }
            binding = {
                "binding_id": _digest(semantic),
                **semantic,
                "status": BOUND,
                "settlement_intent": None,
                "outbox": None,
                "ack": None,
                "invalidation": None,
                "campaign_plan_anchor": None,
            }
            existing = state["bindings"].get(ticket_id)
            if existing is not None:
                if existing["binding_id"] != binding["binding_id"]:
                    raise PaperSettlementLearningBridgeError(
                        "PaperTicket already has conflicting learning binding"
                    )
                return binding["binding_id"]
            if any(item["status"] != ACKED for item in state["bindings"].values()):
                raise PaperSettlementLearningBridgeError(
                    "AgentLoop bridge already has unresolved ticket binding"
                )
            state["bindings"][ticket_id] = binding
            self._write(state)
            return binding["binding_id"]

    def campaign_plan_anchor(
        self,
        ticket_id: str,
    ) -> tuple[str, str] | None:
        """Return the immutable campaign-plan anchor for one resolved ticket."""

        canonical_ticket_id = _text(ticket_id, "ticket_id")
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read()
            binding = state["bindings"].get(canonical_ticket_id)
            if binding is None:
                raise PaperSettlementLearningBridgeError(
                    "campaign plan anchor requires an existing ticket binding"
                )
            if binding.get("status") == INVALIDATED:
                self._validate_acknowledged_invalidation(
                    binding,
                    binding.get("invalidation"),
                )
                raise PaperSettlementLearningBridgeError(
                    "campaign plan anchor is unavailable because acknowledged "
                    "learner reward is durably invalidated"
                )
            anchor = binding.get("campaign_plan_anchor")
            if anchor is None:
                return None
            return (
                _sha(anchor["plan_id"], "campaign plan_id"),
                _instant_id(
                    anchor["reflection_available_at"],
                    "campaign reflection_available_at",
                ),
            )

    def bind_campaign_plan_anchor(
        self,
        *,
        ticket_id: str,
        plan_id: str,
        reflection_available_at: str,
    ) -> tuple[str, str]:
        """Bind one campaign plan identity before dependent AgentLoop progress.

        The bridge does not interpret reflection semantics. It only persists the
        plan digest and first causal availability beside the already sealed
        settlement-learning binding, giving campaign sidecar recovery an
        independent durable anchor.
        """

        canonical_ticket_id = _text(ticket_id, "ticket_id")
        canonical_plan_id = _sha(plan_id, "campaign plan_id")
        canonical_available_at = _instant_id(
            reflection_available_at,
            "campaign reflection_available_at",
        )
        expected = {
            "plan_id": canonical_plan_id,
            "reflection_available_at": canonical_available_at,
        }
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read()
            binding = state["bindings"].get(canonical_ticket_id)
            if binding is None:
                raise PaperSettlementLearningBridgeError(
                    "campaign plan anchor requires an existing ticket binding"
                )
            if binding.get("status") == INVALIDATED:
                self._validate_acknowledged_invalidation(
                    binding,
                    binding.get("invalidation"),
                )
                raise PaperSettlementLearningBridgeError(
                    "campaign plan anchor cannot bind because acknowledged "
                    "learner reward is durably invalidated"
                )
            if binding.get("outbox") is None:
                raise PaperSettlementLearningBridgeError(
                    "campaign plan anchor requires sealed resolution evidence"
                )
            existing = binding.get("campaign_plan_anchor")
            if existing is not None:
                if existing != expected:
                    raise PaperSettlementLearningBridgeError(
                        "ticket is already bound to another campaign plan"
                    )
                return canonical_plan_id, canonical_available_at
            binding["campaign_plan_anchor"] = expected
            self._write(state)
            return canonical_plan_id, canonical_available_at

    @staticmethod
    def _bound_ticket(book: PaperBook, binding: dict[str, object]) -> PaperTicket:
        ticket = book.tickets.get(binding["ticket_id"])
        if ticket is None:
            raise PaperSettlementLearningBridgeError("bound PaperTicket disappeared")
        if _digest(_ticket_payload(ticket)) != binding["ticket_identity_sha256"]:
            raise PaperSettlementLearningBridgeError(
                "bound PaperTicket economics changed after binding"
            )
        return ticket

    @staticmethod
    def _collect_evidence(
        ticket: PaperTicket,
        resolutions: tuple[SettlementResolution, ...],
        *,
        at: str,
    ) -> tuple[list[dict[str, object]], dict[str, str]] | None:
        leg_keys = {leg.quote_key for leg in ticket.legs}
        known: dict[str, str] = {}
        used: dict[str, dict[str, object]] = {}
        leg_by_key = {leg.quote_key: leg for leg in ticket.legs}
        for resolution in resolutions:
            if not isinstance(resolution, SettlementResolution):
                raise PaperSettlementLearningBridgeError(
                    "handoff contains non-canonical settlement evidence"
                )
            try:
                resolution.validate(as_of=at)
            except (TypeError, ValueError) as exc:
                raise PaperSettlementLearningBridgeError(
                    "settlement evidence failed causal validation"
                ) from exc
            scoped = {
                key: value
                for key, value in resolution.quote_outcomes.items()
                if key in leg_keys
            }
            if not scoped:
                continue
            identity_parts = {resolution.event_identity}
            if ":" in resolution.event_identity:
                identity_parts.add(resolution.event_identity.split(":", 1)[1])
            for key in scoped:
                if leg_by_key[key].event_id not in identity_parts:
                    raise PaperSettlementLearningBridgeError(
                        "settlement evidence event identity differs from bound ticket leg"
                    )
            for key, value in scoped.items():
                previous = known.get(key)
                if previous is not None and previous != value:
                    raise PaperSettlementLearningBridgeError(
                        "bound quote has conflicting settlement truth"
                    )
                known[key] = value
            evidence = {
                "evidence_id": resolution.evidence_id,
                "evidence_sha256": resolution.evidence_sha256,
                "event_identity": resolution.event_identity,
                "settlement_ref": resolution.settlement_ref,
                "available_at": _instant_id(
                    resolution.available_at, "settlement available_at"
                ),
                "quote_outcomes": dict(sorted(scoped.items())),
            }
            prior = used.get(resolution.evidence_id)
            if prior is not None and prior != evidence:
                raise PaperSettlementLearningBridgeError(
                    "settlement evidence_id has conflicting content"
                )
            used[resolution.evidence_id] = evidence
        if not known:
            return None
        return [used[key] for key in sorted(used)], known

    @staticmethod
    def _evidence_can_settle(ticket: PaperTicket, known: dict[str, str]) -> bool:
        leg_keys = {leg.quote_key for leg in ticket.legs}
        return "loss" in known.values() or set(known) == leg_keys

    @staticmethod
    def _causal_lower_bound_satisfied(
        binding: dict[str, object],
        ticket: PaperTicket,
        bundle: list[dict[str, object]],
    ) -> bool:
        action_decided_at = _instant(
            binding["action_decided_at"], "bound action_decided_at"
        )
        ticket_placed_at = _instant(ticket.placed_at, "bound ticket placed_at")
        for item in bundle:
            evidence_available_at = _instant(
                item["available_at"], "settlement available_at"
            )
            if (
                evidence_available_at < action_decided_at
                or evidence_available_at < ticket_placed_at
            ):
                return False
        return True

    @classmethod
    def _validate_acknowledged_invalidation(
        cls,
        binding: dict[str, object],
        raw: object,
    ) -> dict[str, object]:
        expected_fields = {
            "invalidation_id",
            "schema",
            "schema_version",
            "reason",
            "binding_id",
            "ticket_id",
            "prior_outbox_id",
            "prior_reward_id",
            "prior_settlement_bundle_sha256",
            "replacement_settlement_bundle_sha256",
            "correction_available_at",
            "settlement_evidence",
            "conflicts",
        }
        if type(raw) is not dict or set(raw) != expected_fields:
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation schema mismatch"
            )
        semantic = {key: value for key, value in raw.items() if key != "invalidation_id"}
        if _sha(raw["invalidation_id"], "invalidation_id") != _digest(semantic):
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation digest mismatch"
            )
        if (
            raw["schema"] != ACKED_REWARD_INVALIDATION_SCHEMA
            or raw["schema_version"] != ACKED_REWARD_INVALIDATION_VERSION
            or raw["reason"] != ACKED_REWARD_INVALIDATION_REASON
        ):
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation authority mismatch"
            )
        if (
            raw["binding_id"] != binding["binding_id"]
            or raw["ticket_id"] != binding["ticket_id"]
        ):
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation belongs to another binding"
            )
        outbox = binding.get("outbox")
        ack = binding.get("ack")
        if type(outbox) is not dict or type(ack) is not dict:
            raise PaperSettlementLearningBridgeError(
                "invalidated ACKED binding lacks prior learner authority"
            )
        _outcome, reward, _transition, _checkpoint = cls._outbox_objects(outbox)
        if (
            _sha(raw["prior_outbox_id"], "prior_outbox_id") != outbox.get("outbox_id")
            or _sha(raw["prior_reward_id"], "prior_reward_id") != reward.reward_id
            or ack.get("reward_id") != reward.reward_id
            or _sha(
                raw["prior_settlement_bundle_sha256"],
                "prior_settlement_bundle_sha256",
            )
            != outbox.get("settlement_bundle_sha256")
        ):
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation does not bind prior learner evidence"
            )
        evidence = raw["settlement_evidence"]
        resolutions = cls._outbox_resolutions({"settlement_evidence": evidence})
        correction_available_at = max(
            (resolution.available_at for resolution in resolutions),
            key=lambda value: _instant(value, "correction available_at"),
        )
        canonical_correction_time = _instant_id(
            raw["correction_available_at"],
            "correction_available_at",
        )
        if canonical_correction_time != correction_available_at:
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation chronology mismatch"
            )
        for resolution in resolutions:
            try:
                resolution.validate(as_of=canonical_correction_time)
            except (TypeError, ValueError) as exc:
                raise PaperSettlementLearningBridgeError(
                    "ACKED reward invalidation evidence failed causal validation"
                ) from exc
        if (
            _sha(
                raw["replacement_settlement_bundle_sha256"],
                "replacement_settlement_bundle_sha256",
            )
            != _digest(evidence)
        ):
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation replacement evidence digest mismatch"
            )
        conflicts = raw["conflicts"]
        sealed = outbox.get("known_quote_outcomes")
        if type(conflicts) is not list or not conflicts or type(sealed) is not dict:
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation conflicts are incomplete"
            )
        incoming: dict[str, str] = {}
        for resolution in resolutions:
            for quote_key, outcome in resolution.quote_outcomes.items():
                previous_incoming = incoming.get(quote_key)
                if previous_incoming is not None and previous_incoming != outcome:
                    raise PaperSettlementLearningBridgeError(
                        "ACKED reward invalidation evidence conflicts internally"
                    )
                incoming[quote_key] = outcome
        expected_conflicts = [
            {
                "quote_key": quote_key,
                "previous_outcome": previous,
                "replacement_outcome": incoming[quote_key],
            }
            for quote_key, previous in sorted(sealed.items())
            if quote_key in incoming and incoming[quote_key] != previous
        ]
        if not expected_conflicts or conflicts != expected_conflicts:
            raise PaperSettlementLearningBridgeError(
                "ACKED reward invalidation conflicts differ from source evidence"
            )
        for conflict in conflicts:
            quote_key = _text(conflict["quote_key"], "invalidation quote_key")
            previous = conflict["previous_outcome"]
            replacement_outcome = conflict["replacement_outcome"]
            if (
                previous not in {"win", "loss", "void"}
                or replacement_outcome not in {"win", "loss", "void"}
                or previous == replacement_outcome
                or sealed.get(quote_key) != previous
                or incoming.get(quote_key) != replacement_outcome
            ):
                raise PaperSettlementLearningBridgeError(
                    "ACKED reward invalidation conflict is not canonical"
                )
        return raw

    @classmethod
    def _acknowledged_settlement_invalidation(
        cls,
        binding: dict[str, object],
        ticket: PaperTicket,
        resolutions: tuple[SettlementResolution, ...],
        *,
        at: str,
    ) -> dict[str, object] | None:
        if not resolutions:
            return None
        outbox = binding.get("outbox")
        if type(outbox) is not dict:
            raise PaperSettlementLearningBridgeError(
                "ACKED binding lacks canonical learner outbox"
            )
        sealed = outbox.get("known_quote_outcomes")
        if type(sealed) is not dict:
            raise PaperSettlementLearningBridgeError(
                "ACKED learner outbox lacks canonical quote outcomes"
            )
        leg_keys = {leg.quote_key for leg in ticket.legs}
        for quote_key, outcome in sealed.items():
            if (
                type(quote_key) is not str
                or quote_key not in leg_keys
                or type(outcome) is not str
                or outcome not in {"win", "loss", "void"}
            ):
                raise PaperSettlementLearningBridgeError(
                    "ACKED learner outbox contains invalid quote outcomes"
                )

        collected = cls._collect_evidence(ticket, resolutions, at=at)
        if collected is None:
            return None
        bundle, incoming = collected
        conflicts: list[dict[str, str]] = []
        for quote_key, outcome in sorted(incoming.items()):
            previous = sealed.get(quote_key)
            if previous is not None and previous != outcome:
                conflicts.append(
                    {
                        "quote_key": quote_key,
                        "previous_outcome": previous,
                        "replacement_outcome": outcome,
                    }
                )
        if not conflicts:
            return None

        _outcome, reward, _transition, _checkpoint = cls._outbox_objects(outbox)
        correction_available_at = max(
            (item["available_at"] for item in bundle),
            key=lambda value: _instant(value, "correction available_at"),
        )
        semantic: dict[str, object] = {
            "schema": ACKED_REWARD_INVALIDATION_SCHEMA,
            "schema_version": ACKED_REWARD_INVALIDATION_VERSION,
            "reason": ACKED_REWARD_INVALIDATION_REASON,
            "binding_id": binding["binding_id"],
            "ticket_id": binding["ticket_id"],
            "prior_outbox_id": _sha(outbox["outbox_id"], "prior_outbox_id"),
            "prior_reward_id": reward.reward_id,
            "prior_settlement_bundle_sha256": _sha(
                outbox["settlement_bundle_sha256"],
                "prior_settlement_bundle_sha256",
            ),
            "replacement_settlement_bundle_sha256": _digest(bundle),
            "correction_available_at": correction_available_at,
            "settlement_evidence": bundle,
            "conflicts": conflicts,
        }
        return {"invalidation_id": _digest(semantic), **semantic}

    @classmethod
    def _assert_acknowledged_settlement_consistency(
        cls,
        binding: dict[str, object],
        ticket: PaperTicket,
        resolutions: tuple[SettlementResolution, ...],
        *,
        at: str,
    ) -> None:
        """Reject later settlement truth that would invalidate an ACKED reward."""
        if cls._acknowledged_settlement_invalidation(
            binding,
            ticket,
            resolutions,
            at=at,
        ) is not None:
            raise PaperSettlementLearningBridgeError(
                "later settlement evidence conflicts with acknowledged learner reward; "
                "successor settlement/reward generation required"
            )

    @staticmethod
    def _paper_observed_evidence(
        binding: dict[str, object],
        ticket: PaperTicket,
        *,
        bundle_sha256: str,
        revealed_at: str,
    ) -> tuple[Outcome, RewardEvidence]:
        canonical_bundle_sha = _sha(bundle_sha256, "settlement_bundle_sha256")
        canonical_revealed_at = _instant_id(revealed_at, "reward available_at")
        reward_value = _exact_subtract(ticket.payout, ticket.stake)
        outcome = Outcome(
            environment_id=binding["environment_id"],
            action_id=binding["action_id"],
            revealed_at=canonical_revealed_at,
            truth=EvidenceTruth.OBSERVED,
            evidence=tuple(
                sorted(
                    (
                        ("binding_id", binding["binding_id"]),
                        ("decision_id", binding["decision_id"]),
                        ("paper_ticket_sha256", binding["ticket_identity_sha256"]),
                        ("settlement_bundle_sha256", canonical_bundle_sha),
                        ("ticket_id", binding["ticket_id"]),
                        ("ticket_status", ticket.status.value),
                    )
                )
            ),
        )
        reward = RewardEvidence(
            environment_id=binding["environment_id"],
            action_id=binding["action_id"],
            outcome_id=outcome.outcome_id,
            reward=reward_value,
            available_at=canonical_revealed_at,
            truth=EvidenceTruth.OBSERVED,
            evidence=tuple(
                sorted(
                    (
                        ("cost_rule", COST_RULE),
                        ("reward_rule", REWARD_RULE),
                        ("settlement_bundle_sha256", canonical_bundle_sha),
                        ("ticket_id", binding["ticket_id"]),
                    )
                )
            ),
        )
        return outcome, reward

    @staticmethod
    def _observed_reward_reference(
        binding: dict[str, object],
        ticket: PaperTicket,
        *,
        settlement_bundle_sha256: str,
        outcome: Outcome,
        reward: RewardEvidence,
    ) -> dict[str, object]:
        if outcome.truth is not EvidenceTruth.OBSERVED or reward.truth is not EvidenceTruth.OBSERVED:
            raise PaperSettlementLearningBridgeError(
                "PAPER observed-reward reference requires OBSERVED outcome and reward"
            )
        semantic: dict[str, object] = {
            "schema": OBSERVED_REWARD_REFERENCE_SCHEMA,
            "schema_version": OBSERVED_REWARD_REFERENCE_VERSION,
            "issuer": OBSERVED_REWARD_ISSUER,
            "binding_id": binding["binding_id"],
            "ticket_id": ticket.ticket_id,
            "paper_ticket_sha256": binding["ticket_identity_sha256"],
            "decision_id": binding["decision_id"],
            "decision_sha256": binding["decision_sha256"],
            "environment_id": reward.environment_id,
            "action_id": reward.action_id,
            "outcome_id": outcome.outcome_id,
            "reward_id": reward.reward_id,
            "reward": str(reward.reward),
            "currency": ticket.currency,
            "available_at": _instant_id(reward.available_at, "reward available_at"),
            "reward_rule": REWARD_RULE,
            "cost_rule": COST_RULE,
            "ticket_status": ticket.status.value,
            "ticket_payout": str(ticket.payout),
            "settlement_bundle_sha256": _sha(
                settlement_bundle_sha256,
                "settlement_bundle_sha256",
            ),
            "economic_goal_fingerprint": binding["economic_goal_fingerprint"],
            "risk_fingerprint": binding["risk_fingerprint"],
        }
        return {"reference_id": _digest(semantic), **semantic}

    @classmethod
    def _verify_observed_reward_reference(
        cls,
        binding: dict[str, object],
        ticket: PaperTicket,
        *,
        settlement_bundle_sha256: str,
        outcome: Outcome,
        reward: RewardEvidence,
        revealed_at: str,
        reference: object,
        allow_missing_legacy: bool = False,
    ) -> RewardEvidence:
        canonical_revealed_at = _instant_id(revealed_at, "reward available_at")
        expected_outcome, expected_reward = cls._paper_observed_evidence(
            binding,
            ticket,
            bundle_sha256=settlement_bundle_sha256,
            revealed_at=canonical_revealed_at,
        )
        if outcome != expected_outcome or reward != expected_reward:
            raise PaperSettlementLearningBridgeError(
                "observed PAPER reward differs from canonical settlement economics"
            )
        expected_reference = cls._observed_reward_reference(
            binding,
            ticket,
            settlement_bundle_sha256=settlement_bundle_sha256,
            outcome=expected_outcome,
            reward=expected_reward,
        )
        if reference is None and allow_missing_legacy:
            return expected_reward
        if type(reference) is not dict or reference != expected_reference:
            raise PaperSettlementLearningBridgeError(
                "observed PAPER reward reference differs from product-owned source truth"
            )
        return expected_reward

    @staticmethod
    def _outbox_resolutions(outbox: dict[str, object]) -> tuple[SettlementResolution, ...]:
        raw_bundle = outbox.get("settlement_evidence")
        if type(raw_bundle) is not list or not raw_bundle:
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox settlement evidence is invalid"
            )
        resolutions: list[SettlementResolution] = []
        expected_fields = {
            "evidence_id",
            "evidence_sha256",
            "event_identity",
            "settlement_ref",
            "available_at",
            "quote_outcomes",
        }
        try:
            for item in raw_bundle:
                if type(item) is not dict or set(item) != expected_fields:
                    raise TypeError
                if type(item["quote_outcomes"]) is not dict:
                    raise TypeError
                resolutions.append(
                    SettlementResolution(
                        event_identity=item["event_identity"],
                        settlement_ref=item["settlement_ref"],
                        quote_outcomes=dict(item["quote_outcomes"]),
                        evidence_id=item["evidence_id"],
                        evidence_sha256=item["evidence_sha256"],
                        available_at=item["available_at"],
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox settlement evidence is not canonical"
            ) from exc
        return tuple(resolutions)

    @staticmethod
    def _bound_observation_action(
        binding: dict[str, object],
    ) -> tuple[Observation, Action]:
        try:
            raw_observation = binding["observation"]
            observation = Observation(
                environment_id=raw_observation["environment_id"],
                observed_at=raw_observation["observed_at"],
                available_at=raw_observation["available_at"],
                evidence=tuple(
                    (item[0], item[1])
                    for item in raw_observation["evidence"]
                ),
            )
            action = Action(
                environment_id=binding["environment_id"],
                observation_id=binding["observation_id"],
                action_type=binding["action_type"],
                decided_at=binding["action_decided_at"],
                parameters=tuple(
                    (item[0], item[1])
                    for item in binding["action_parameters"]
                ),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PaperSettlementLearningBridgeError(
                "durable learning binding cannot reconstruct observation/action"
            ) from exc
        if (
            observation.observation_id != binding["observation_id"]
            or action.action_id != binding["action_id"]
        ):
            raise PaperSettlementLearningBridgeError(
                "durable observation/action identity differs from learning binding"
            )
        return observation, action

    def _verified_outbox_objects(
        self,
        binding: dict[str, object],
        outbox: dict[str, object],
        *,
        as_of: str,
    ) -> tuple[Outcome, RewardEvidence, Transition, EnvironmentCheckpoint]:
        if type(outbox) is not dict:
            raise PaperSettlementLearningBridgeError("durable learner outbox must be an object")
        try:
            outbox_id = _sha(outbox["outbox_id"], "outbox_id")
            semantic = {key: value for key, value in outbox.items() if key != "outbox_id"}
            if outbox_id != _digest(semantic):
                raise PaperSettlementLearningBridgeError(
                    "durable learner outbox digest mismatch"
                )
            if (
                outbox["binding_id"] != binding["binding_id"]
                or outbox["ticket_id"] != binding["ticket_id"]
            ):
                raise PaperSettlementLearningBridgeError(
                    "durable learner outbox belongs to another binding"
                )
        except KeyError as exc:
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox identity is incomplete"
            ) from exc

        if (
            binding["economic_goal_fingerprint"] != self.goal_fingerprint
            or binding["risk_fingerprint"] != self.risk_fingerprint
        ):
            raise PaperSettlementLearningBridgeError(
                "durable learner binding differs from current owner economic authority"
            )

        book = PaperBook.load(self.paper_book_path)
        ticket = self._bound_ticket(book, binding)
        decision = self.decision_ledger.verified_economic_decision(
            binding["decision_id"],
            self.economic_goal,
            risk_policy=self.risk_policy,
        )
        if _decision_sha(decision) != binding["decision_sha256"]:
            raise PaperSettlementLearningBridgeError(
                "durable economic decision changed after learning binding"
            )
        observation, action = self._bound_observation_action(binding)
        self._decision_matches(decision, ticket, action, observation)

        intent = binding.get("settlement_intent")
        if intent is not None:
            resolutions = self._intent_resolutions(intent)
        else:
            resolutions = self._outbox_resolutions(outbox)
        rebuilt = self._bundle(ticket, resolutions, at=as_of)
        if rebuilt is None:
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox no longer has complete settlement evidence"
            )
        bundle, known = rebuilt
        bundle_sha = _digest(bundle)
        canonical_known = dict(sorted(known.items()))
        if intent is not None and (
            intent.get("settlement_evidence") != bundle
            or intent.get("settlement_bundle_sha256") != bundle_sha
            or intent.get("known_quote_outcomes") != canonical_known
        ):
            raise PaperSettlementLearningBridgeError(
                "durable pre-settlement intent differs from current PAPER settlement truth"
            )
        if (
            bundle != outbox.get("settlement_evidence")
            or bundle_sha != outbox.get("settlement_bundle_sha256")
            or canonical_known != outbox.get("known_quote_outcomes")
            or ticket.status.value != outbox.get("ticket_status")
            or str(ticket.payout) != outbox.get("ticket_payout")
        ):
            if intent is not None:
                raise PaperSettlementLearningBridgeError(
                    "durable learner outbox settlement evidence differs from pre-settlement intent"
                )
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox differs from current PAPER settlement truth"
            )
        revealed_at = max(
            (item["available_at"] for item in bundle),
            key=lambda value: _instant(value, "settlement available_at"),
        )

        outcome, reward, transition, checkpoint = self._outbox_objects(outbox)
        verified_reward = self._verify_observed_reward_reference(
            binding,
            ticket,
            settlement_bundle_sha256=bundle_sha,
            outcome=outcome,
            reward=reward,
            revealed_at=revealed_at,
            reference=outbox.get("observed_reward_reference"),
            allow_missing_legacy=True,
        )
        if (
            reward != verified_reward
            or outcome.revealed_at != revealed_at
            or verified_reward.available_at != revealed_at
            or transition.resolved_at != revealed_at
            or outbox.get("net_reward") != str(verified_reward.reward)
        ):
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox reward chronology differs from source-resolved settlement truth"
            )
        return outcome, verified_reward, transition, checkpoint

    @classmethod
    def _bundle(
        cls,
        ticket: PaperTicket,
        resolutions: tuple[SettlementResolution, ...],
        *,
        at: str,
    ) -> tuple[list[dict[str, object]], dict[str, str]] | None:
        result = cls._collect_evidence(ticket, resolutions, at=at)
        if result is None:
            return None
        bundle, known = result
        expected = _expected_ticket_economics(ticket, known)
        if expected is None:
            return None
        expected_status, expected_payout = expected
        if ticket.status is not expected_status or ticket.payout != expected_payout:
            raise PaperSettlementLearningBridgeError(
                "durable PaperBook settlement conflicts with authoritative evidence"
            )
        return bundle, known

    @staticmethod
    def _intent_resolutions(intent: object) -> tuple[SettlementResolution, ...]:
        expected_fields = {
            "intent_id",
            "binding_id",
            "ticket_id",
            "settlement_evidence",
            "known_quote_outcomes",
            "settlement_bundle_sha256",
        }
        if type(intent) is not dict or set(intent) != expected_fields:
            raise PaperSettlementLearningBridgeError(
                "durable settlement intent schema mismatch"
            )
        try:
            evidence = intent["settlement_evidence"]
            if type(evidence) is not list or not evidence:
                raise TypeError
            resolutions_list: list[SettlementResolution] = []
            for item in evidence:
                if type(item) is not dict or set(item) != {
                    "evidence_id",
                    "evidence_sha256",
                    "event_identity",
                    "settlement_ref",
                    "available_at",
                    "quote_outcomes",
                }:
                    raise TypeError
                if type(item["quote_outcomes"]) is not dict:
                    raise TypeError
                resolutions_list.append(
                    SettlementResolution(
                        event_identity=item["event_identity"],
                        settlement_ref=item["settlement_ref"],
                        quote_outcomes=item["quote_outcomes"].copy(),
                        evidence_id=item["evidence_id"],
                        evidence_sha256=item["evidence_sha256"],
                        available_at=item["available_at"],
                    )
                )
            resolutions = tuple(resolutions_list)
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperSettlementLearningBridgeError(
                "durable settlement intent evidence is not canonical"
            ) from exc
        semantic = {
            "binding_id": intent["binding_id"],
            "ticket_id": intent["ticket_id"],
            "settlement_evidence": evidence,
            "known_quote_outcomes": intent["known_quote_outcomes"],
            "settlement_bundle_sha256": intent["settlement_bundle_sha256"],
        }
        if (
            _sha(intent["intent_id"], "settlement_intent intent_id")
            != _digest(semantic)
            or _sha(
                intent["settlement_bundle_sha256"],
                "settlement_intent settlement_bundle_sha256",
            )
            != _digest(evidence)
        ):
            raise PaperSettlementLearningBridgeError(
                "durable settlement intent digest mismatch"
            )
        return resolutions

    def prepare_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        at: str,
    ) -> tuple[str, ...]:
        """Durably stage authoritative evidence before PaperBook settlement mutation."""

        if Path(paper_book_path) != self.paper_book_path:
            raise PaperSettlementLearningBridgeError(
                "continuous session uses another PaperBook path"
            )
        if type(resolutions) is not tuple:
            raise TypeError("settlement handoff resolutions must be a tuple")
        _instant(at, "prepare at")

        prepared: list[str] = []
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read()
            book = PaperBook.load(self.paper_book_path)
            changed = False
            for ticket_id, binding in state["bindings"].items():
                if binding["status"] != BOUND:
                    continue
                ticket = self._bound_ticket(book, binding)
                existing = binding.get("settlement_intent")
                if ticket.status is not TicketStatus.OPEN:
                    if existing is not None:
                        self._intent_resolutions(existing)
                    continue
                result = self._collect_evidence(ticket, resolutions, at=at)
                if result is None:
                    continue
                bundle, known = result
                if not self._evidence_can_settle(ticket, known):
                    continue
                if not self._causal_lower_bound_satisfied(binding, ticket, bundle):
                    continue
                semantic = {
                    "binding_id": binding["binding_id"],
                    "ticket_id": ticket_id,
                    "settlement_evidence": bundle,
                    "known_quote_outcomes": dict(sorted(known.items())),
                    "settlement_bundle_sha256": _digest(bundle),
                }
                intent = {"intent_id": _digest(semantic), **semantic}
                if existing is not None:
                    if existing != intent:
                        raise PaperSettlementLearningBridgeError(
                            "settlement intent conflicts with durable prepared evidence"
                        )
                else:
                    binding["settlement_intent"] = intent
                    changed = True
                prepared.append(ticket_id)
            if changed:
                self._write(state)
        return tuple(prepared)

    def _derive_outbox(
        self,
        binding: dict[str, object],
        ticket: PaperTicket,
        resolutions: tuple[SettlementResolution, ...],
        *,
        at: str,
    ) -> dict[str, object] | None:
        if ticket.status is TicketStatus.OPEN:
            return None
        result = self._bundle(ticket, resolutions, at=at)
        if result is None:
            return None
        bundle, known = result
        bundle_sha = _digest(bundle)
        if not self._causal_lower_bound_satisfied(binding, ticket, bundle):
            raise PaperSettlementLearningBridgeError(
                "settlement evidence predates bound action or ticket placement"
            )
        revealed_at = max(
            (item["available_at"] for item in bundle),
            key=lambda value: _instant(value, "settlement available_at"),
        )
        outcome, reward = self._paper_observed_evidence(
            binding,
            ticket,
            bundle_sha256=bundle_sha,
            revealed_at=revealed_at,
        )
        reward_value = reward.reward
        observed_reward_reference = self._observed_reward_reference(
            binding,
            ticket,
            settlement_bundle_sha256=bundle_sha,
            outcome=outcome,
            reward=reward,
        )
        reward = self._verify_observed_reward_reference(
            binding,
            ticket,
            settlement_bundle_sha256=bundle_sha,
            outcome=outcome,
            reward=reward,
            revealed_at=revealed_at,
            reference=observed_reward_reference,
        )
        baseline = _checkpoint(binding["baseline_checkpoint"])
        try:
            identity_payload = binding["environment_identity"]
            observation_payload = binding["observation"]
            identity = EnvironmentIdentity(
                source_id=identity_payload["source_id"],
                config_id=identity_payload["config_id"],
                data_id=identity_payload["data_id"],
                protocol_id=identity_payload["protocol_id"],
                cutoff_ts=identity_payload["cutoff_ts"],
                seed=identity_payload["seed"],
            )
            environment = CausalLearningEnvironment.resume(
                identity,
                episode_key=binding["episode_key"],
                policy_id=binding["policy_id"],
                admissible_actions=frozenset(binding["admissible_actions"]),
                checkpoint=baseline,
            )
            observation = Observation(
                environment_id=observation_payload["environment_id"],
                observed_at=observation_payload["observed_at"],
                available_at=observation_payload["available_at"],
                evidence=tuple(
                    (item[0], item[1])
                    for item in observation_payload["evidence"]
                ),
            )
            replayed_action = environment.act(
                observation,
                action_type=binding["action_type"],
                decision_at=binding["action_decided_at"],
                parameters=tuple(
                    (item[0], item[1])
                    for item in binding["action_parameters"]
                ),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PaperSettlementLearningBridgeError(
                "durable environment binding is not canonically replayable"
            ) from exc
        if (
            identity.environment_id != binding["environment_id"]
            or environment.episode.episode_id != binding["episode_id"]
            or observation.observation_id != binding["observation_id"]
            or replayed_action.action_id != binding["action_id"]
        ):
            raise PaperSettlementLearningBridgeError(
                "durable environment replay identity mismatch"
            )
        transition = environment.resolve(
            replayed_action.action_id,
            outcome=outcome,
            reward=reward,
            resolved_at=revealed_at,
        )
        next_checkpoint = environment.checkpoint()
        semantic = {
            "binding_id": binding["binding_id"],
            "ticket_id": binding["ticket_id"],
            "ticket_status": ticket.status.value,
            "ticket_payout": str(ticket.payout),
            "net_reward": str(reward_value),
            "known_quote_outcomes": dict(sorted(known.items())),
            "settlement_evidence": bundle,
            "settlement_bundle_sha256": bundle_sha,
            "observed_reward_reference": observed_reward_reference,
            "outcome": {
                "environment_id": outcome.environment_id,
                "action_id": outcome.action_id,
                "revealed_at": outcome.revealed_at,
                "truth": outcome.truth.value,
                "evidence": [list(item) for item in outcome.evidence],
                "simulation_model_id": outcome.simulation_model_id,
            },
            "reward": {
                "environment_id": reward.environment_id,
                "action_id": reward.action_id,
                "outcome_id": reward.outcome_id,
                "reward": str(reward.reward),
                "available_at": reward.available_at,
                "truth": reward.truth.value,
                "evidence": [list(item) for item in reward.evidence],
                "simulation_model_id": reward.simulation_model_id,
            },
            "transition": {
                "environment_id": transition.environment_id,
                "episode_id": transition.episode_id,
                "step_index": transition.step_index,
                "observation_id": transition.observation_id,
                "action_id": transition.action_id,
                "outcome_id": transition.outcome_id,
                "reward_id": transition.reward_id,
                "decision_at": transition.decision_at,
                "resolved_at": transition.resolved_at,
            },
            "next_checkpoint": _checkpoint_payload(next_checkpoint),
        }
        return {"outbox_id": _digest(semantic), **semantic}

    @staticmethod
    def _outbox_objects(
        outbox: dict[str, object],
    ) -> tuple[Outcome, RewardEvidence, Transition, EnvironmentCheckpoint]:
        try:
            raw_outcome = outbox["outcome"]
            raw_reward = outbox["reward"]
            raw_transition = outbox["transition"]
            outcome = Outcome(
                environment_id=raw_outcome["environment_id"],
                action_id=raw_outcome["action_id"],
                revealed_at=raw_outcome["revealed_at"],
                truth=EvidenceTruth(raw_outcome["truth"]),
                evidence=tuple((item[0], item[1]) for item in raw_outcome["evidence"]),
                simulation_model_id=raw_outcome["simulation_model_id"],
            )
            reward = RewardEvidence(
                environment_id=raw_reward["environment_id"],
                action_id=raw_reward["action_id"],
                outcome_id=raw_reward["outcome_id"],
                reward=Decimal(raw_reward["reward"]),
                available_at=raw_reward["available_at"],
                truth=EvidenceTruth(raw_reward["truth"]),
                evidence=tuple((item[0], item[1]) for item in raw_reward["evidence"]),
                simulation_model_id=raw_reward["simulation_model_id"],
            )
            transition = Transition(
                environment_id=raw_transition["environment_id"],
                episode_id=raw_transition["episode_id"],
                step_index=raw_transition["step_index"],
                observation_id=raw_transition["observation_id"],
                action_id=raw_transition["action_id"],
                outcome_id=raw_transition["outcome_id"],
                reward_id=raw_transition["reward_id"],
                decision_at=raw_transition["decision_at"],
                resolved_at=raw_transition["resolved_at"],
            )
            checkpoint = _checkpoint(outbox["next_checkpoint"])
        except (KeyError, TypeError, ValueError, IndexError, ArithmeticError) as exc:
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox is not canonical"
            ) from exc
        if (
            transition.outcome_id != outcome.outcome_id
            or transition.reward_id != reward.reward_id
            or reward.outcome_id != outcome.outcome_id
            or checkpoint.last_transition_id != transition.transition_id
        ):
            raise PaperSettlementLearningBridgeError(
                "durable learner outbox identity mismatch"
            )
        return outcome, reward, transition, checkpoint

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path: Path,
        resolutions: tuple[SettlementResolution, ...],
        settled_ticket_ids: tuple[str, ...],
        at: str,
    ) -> tuple[str, ...]:
        """Publish this handoff\'s new outboxes and recover durable OUTBOX acknowledgements."""

        if Path(paper_book_path) != self.paper_book_path:
            raise PaperSettlementLearningBridgeError(
                "continuous session uses another PaperBook path"
            )
        if type(resolutions) is not tuple or type(settled_ticket_ids) is not tuple:
            raise TypeError("settlement handoff collections must be tuples")
        _instant(at, "reconcile at")

        settled_ids: set[str] = set()
        for settled_ticket_id in settled_ticket_ids:
            canonical_ticket_id = _text(settled_ticket_id, "settled_ticket_id")
            if canonical_ticket_id in settled_ids:
                raise PaperSettlementLearningBridgeError(
                    "settled_ticket_ids contains duplicate ticket identity"
                )
            settled_ids.add(canonical_ticket_id)

        pending: list[tuple[str, dict[str, object]]] = []
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read()
            book = PaperBook.load(self.paper_book_path)
            changed = False
            for ticket_id, binding in state["bindings"].items():
                if binding["status"] == INVALIDATED:
                    self._validate_acknowledged_invalidation(
                        binding,
                        binding.get("invalidation"),
                    )
                    raise PaperSettlementLearningBridgeError(
                        "acknowledged learner reward is durably invalidated; "
                        "successor settlement/reward generation required"
                    )
                if binding["status"] == ACKED:
                    if resolutions:
                        ticket = self._bound_ticket(book, binding)
                        invalidation = self._acknowledged_settlement_invalidation(
                            binding,
                            ticket,
                            resolutions,
                            at=at,
                        )
                        if invalidation is not None:
                            binding["invalidation"] = invalidation
                            binding["status"] = INVALIDATED
                            self._write(
                                state,
                                protect_invalidation=True,
                            )
                            raise PaperSettlementLearningBridgeError(
                                "later settlement evidence conflicts with acknowledged learner reward; "
                                "successor settlement/reward generation required; "
                                "stale reward invalidation persisted"
                            )
                    continue
                ticket = self._bound_ticket(book, binding)
                if binding["status"] == BOUND:
                    intent = binding.get("settlement_intent")
                    evidence = resolutions
                    if intent is not None:
                        evidence = self._intent_resolutions(intent) + resolutions
                    if ticket_id not in settled_ids:
                        if intent is None or ticket.status is TicketStatus.OPEN:
                            continue
                    outbox = self._derive_outbox(
                        binding,
                        ticket,
                        evidence,
                        at=at,
                    )
                    if outbox is None:
                        continue
                    binding["outbox"] = outbox
                    binding["status"] = OUTBOX
                    changed = True
                else:
                    outbox = binding["outbox"]
                    if (
                        ticket.status.value != outbox["ticket_status"]
                        or str(ticket.payout) != outbox["ticket_payout"]
                    ):
                        raise PaperSettlementLearningBridgeError(
                            "PaperBook changed after learner outbox publication"
                        )
                pending.append((ticket_id, binding["outbox"]))
            if changed:
                self._write(state)

        acknowledged: list[str] = []
        for ticket_id, outbox in pending:
            with WorkspaceEconomicLock(self.state_path.parent):
                state = self._read()
                binding = state["bindings"].get(ticket_id)
                if binding is None or binding.get("outbox") != outbox:
                    raise PaperSettlementLearningBridgeError(
                        "learner outbox changed before product authority verification"
                    )
                outcome, reward, transition, checkpoint = self._verified_outbox_objects(
                    binding,
                    outbox,
                    as_of=at,
                )
            snapshot = self.agent_loop.record_resolution(
                transition,
                outcome=outcome,
                reward=reward,
                at=at,
            )
            if (
                snapshot.transition_id != transition.transition_id
                or snapshot.outcome_id != outcome.outcome_id
                or snapshot.reward_id != reward.reward_id
            ):
                raise PaperSettlementLearningBridgeError(
                    "AgentLoop acknowledgement differs from learner outbox"
                )
            with WorkspaceEconomicLock(self.state_path.parent):
                state = self._read()
                binding = state["bindings"].get(ticket_id)
                if binding is None or binding["outbox"] != outbox:
                    raise PaperSettlementLearningBridgeError(
                        "learner outbox changed during acknowledgement"
                    )
                ack = {
                    "outbox_id": outbox["outbox_id"],
                    "transition_id": transition.transition_id,
                    "outcome_id": outcome.outcome_id,
                    "reward_id": reward.reward_id,
                    "next_checkpoint_id": checkpoint.checkpoint_id,
                    "acked_at": _instant_id(at, "acked_at"),
                }
                if binding["ack"] is not None and binding["ack"] != ack:
                    raise PaperSettlementLearningBridgeError(
                        "learner acknowledgement conflicts with durable state"
                    )
                binding["ack"] = ack
                binding["status"] = ACKED
                self._write(state)
            acknowledged.append(transition.transition_id)
        return tuple(acknowledged)

    def resolution_witness(
        self,
        ticket_id: str,
    ) -> PaperSettlementLearningWitness:
        """Return the exact typed evidence already sealed in one learner outbox.

        The witness is available once an outbox exists, including the narrow
        restart window after resolution publication but before the bridge's ACK
        write.  Consumers still have to verify the AgentLoop phase before using
        it; this method deliberately does not advance an AgentLoop phase.
        """

        canonical_ticket_id = _text(ticket_id, "ticket_id")
        with WorkspaceEconomicLock(self.state_path.parent):
            state = self._read()
            binding = state["bindings"].get(canonical_ticket_id)
            if binding is None:
                raise PaperSettlementLearningBridgeError(
                    "ticket has no durable learning binding"
                )
            if binding.get("status") == INVALIDATED:
                self._validate_acknowledged_invalidation(
                    binding,
                    binding.get("invalidation"),
                )
                raise PaperSettlementLearningBridgeError(
                    "acknowledged learner reward is durably invalidated; "
                    "successor settlement/reward generation required"
                )
            outbox = binding.get("outbox")
            if outbox is None:
                raise PaperSettlementLearningBridgeError(
                    "ticket has no durable learner outbox"
                )
            reward_payload = outbox.get("reward")
            if type(reward_payload) is not dict:
                raise PaperSettlementLearningBridgeError(
                    "durable learner outbox reward payload is invalid"
                )
            outcome, reward, transition, checkpoint = self._verified_outbox_objects(
                binding,
                outbox,
                as_of=reward_payload.get("available_at"),
            )
            try:
                observation, action = self._bound_observation_action(binding)
                witness = PaperSettlementLearningWitness(
                    ticket_id=canonical_ticket_id,
                    binding_id=binding["binding_id"],
                    settlement_bundle_sha256=outbox[
                        "settlement_bundle_sha256"
                    ],
                    observation=observation,
                    action=action,
                    outcome=outcome,
                    reward=reward,
                    transition=transition,
                    baseline_checkpoint=_checkpoint(binding["baseline_checkpoint"]),
                    next_checkpoint=checkpoint,
                )
            except (
                KeyError,
                TypeError,
                ValueError,
                IndexError,
            ) as exc:
                raise PaperSettlementLearningBridgeError(
                    "durable learner witness is not canonical"
                ) from exc
            if (
                witness.binding_id != binding["binding_id"]
                or witness.action.action_id != binding["action_id"]
                or witness.transition.episode_id != binding["episode_id"]
            ):
                raise PaperSettlementLearningBridgeError(
                    "durable learner witness differs from ticket binding"
                )
            return witness

    def next_checkpoint(self, ticket_id: str) -> EnvironmentCheckpoint:
        """Return the post-resolution checkpoint witness once outbox exists."""

        return self.resolution_witness(ticket_id).next_checkpoint
