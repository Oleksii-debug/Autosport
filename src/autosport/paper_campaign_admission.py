"""Crash-safe composition admission for one PAPER ticket -> learning action.

The existing authorities deliberately stop at separate durable boundaries:
``PaperBook`` owns virtual tickets, ``JsonlDecisionLedger`` owns economic decisions,
and ``PaperCampaignRuntime`` owns AgentLoop/action/settlement-learning binding. This
module owns only the missing composition journal which makes those boundaries
retryable as one logical PAPER admission. It never creates a real-money path.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ._paper_execution_anti_rollback import _authority_root, _sync_authority_directory
from .agent_loop import AgentLoopPhase, ExternalEffectState
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from .domain import PaperTicket, TicketLeg
from .integrity import atomic_write_json
from .learning_environment import Action, EnvironmentCheckpoint, Observation
from .paper import PaperBook
from .paper_campaign_runtime import PaperCampaignRuntime
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.paper_campaign_admission"
SCHEMA_VERSION = 2
_PREPARED = "PREPARED"
_COMMITTED = "COMMITTED"
_WITNESS_SCHEMA_VERSION = 1
_WITNESS_SUFFIX = ".paper-campaign-admission.monotonic-witness.jsonl"
_RESERVED_DECISION_PAYLOAD = frozenset(
    {
        "ticket_id",
        "quote_key",
        "quote_keys",
        "stake",
        MATERIAL_ACTION_ID_PAYLOAD_KEY,
        ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
        RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    }
)
_RESERVED_ACTION_PARAMETERS = frozenset({"economic_decision_id", "paper_ticket_id"})


class PaperCampaignAdmissionError(RuntimeError):
    """PAPER composition admission is missing, corrupt, or conflicts with durable truth."""


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PaperCampaignAdmissionError(f"{name} must be canonical non-empty text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PaperCampaignAdmissionError(f"{name} must be valid UTF-8") from exc
    return value


def _json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PaperCampaignAdmissionError("admission evidence is not canonical JSON") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PaperCampaignAdmissionError(f"{name} must be lowercase SHA-256 hex")
    return text


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PaperCampaignAdmissionError(f"admission JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _state_identity(path: Path) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _leg_payload(leg: TicketLeg) -> dict[str, str]:
    if not isinstance(leg, TicketLeg):
        raise TypeError("legs must contain TicketLeg values")
    return {
        "event_id": leg.event_id,
        "market_id": leg.market_id,
        "selection_id": leg.selection_id,
        "locked_odds": str(leg.locked_odds),
        "sport": leg.sport,
        "quote_key": leg.quote_key,
    }


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
        "legs": [_leg_payload(leg) for leg in ticket.legs],
    }


def _observation_payload(observation: Observation) -> dict[str, object]:
    if not isinstance(observation, Observation):
        raise TypeError("observation must be Observation")
    return {
        "environment_id": observation.environment_id,
        "observation_id": observation.observation_id,
        "observed_at": observation.observed_at,
        "available_at": observation.available_at,
        "evidence": [list(item) for item in observation.evidence],
    }


def _checkpoint_payload(checkpoint: EnvironmentCheckpoint) -> dict[str, object]:
    if not isinstance(checkpoint, EnvironmentCheckpoint):
        raise TypeError("checkpoint must be EnvironmentCheckpoint")
    return {
        "environment_id": checkpoint.environment_id,
        "episode_id": checkpoint.episode_id,
        "policy_id": checkpoint.policy_id,
        "step_index": checkpoint.step_index,
        "chain_sha256": checkpoint.chain_sha256,
        "last_transition_id": checkpoint.last_transition_id,
        "committed_action_ids": list(checkpoint.committed_action_ids),
        "committed_decision_intents": [
            [intent_id, payload_id]
            for intent_id, payload_id in checkpoint.committed_decision_intents
        ],
    }


def _checkpoint(raw: object) -> EnvironmentCheckpoint:
    if type(raw) is not dict or set(raw) != {
        "environment_id",
        "episode_id",
        "policy_id",
        "step_index",
        "chain_sha256",
        "last_transition_id",
        "committed_action_ids",
        "committed_decision_intents",
    }:
        raise PaperCampaignAdmissionError("admission baseline checkpoint fields mismatch")
    try:
        checkpoint = EnvironmentCheckpoint(
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
        raise PaperCampaignAdmissionError("admission baseline checkpoint is invalid") from exc
    return checkpoint


@dataclass(frozen=True, slots=True)
class PaperCampaignAdmissionReceipt:
    admission_id: str
    ticket_id: str
    decision_id: str
    baseline_checkpoint_id: str
    action_id: str


class PaperCampaignAdmissionCoordinator:
    """Converge a logical PAPER admission across existing durable authorities.

    The composition journal is rollback-fenced by the same independent monotonic
    authority root used by the canonical PAPER execution anti-rollback mechanism.
    The witness is published before the mutable workspace state, so a crash between
    those durability barriers fails closed instead of accepting an older snapshot.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        paper_book_path: str | Path,
        decision_ledger: JsonlDecisionLedger,
        runtime: PaperCampaignRuntime,
    ) -> None:
        if not isinstance(decision_ledger, JsonlDecisionLedger):
            raise TypeError("decision_ledger must be JsonlDecisionLedger")
        if not isinstance(runtime, PaperCampaignRuntime):
            raise TypeError("runtime must be PaperCampaignRuntime")
        self.state_path = Path(state_path)
        self.paper_book_path = Path(paper_book_path)
        self.decision_ledger = decision_ledger
        self.runtime = runtime
        self.workspace = self.state_path.parent
        self._admission_lock_workspace = self.workspace / ".paper-campaign-admission-lock"
        self.workspace.mkdir(parents=True, exist_ok=True)
        expected_workspace = self.workspace.resolve(strict=False)
        if self.paper_book_path.parent.resolve(strict=False) != expected_workspace:
            raise PaperCampaignAdmissionError("PaperBook must share the admission workspace")
        if self.decision_ledger.path.parent.resolve(strict=False) != expected_workspace:
            raise PaperCampaignAdmissionError("Decision Ledger must share the admission workspace")
        if self.runtime.settlement_bridge.state_path.parent.resolve(strict=False) != expected_workspace:
            raise PaperCampaignAdmissionError("campaign runtime must share the admission workspace")
        try:
            authority_root = _authority_root(self.state_path)
        except RuntimeError as exc:
            raise PaperCampaignAdmissionError(
                "cannot establish independent admission monotonic authority"
            ) from exc
        identity = _state_identity(self.state_path)
        self._witness_path = authority_root / f"{identity}{_WITNESS_SUFFIX}"
        self._state_identity = identity
        with WorkspaceEconomicLock(self._admission_lock_workspace):
            if self.state_path.exists():
                self._read()
            else:
                if self._read_witnesses():
                    raise PaperCampaignAdmissionError(
                        "admission journal is missing behind monotonic authority"
                    )
                self._write({})

    def _read_witnesses(self) -> list[dict[str, object]]:
        if not self._witness_path.exists():
            return []
        try:
            lines = self._witness_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise PaperCampaignAdmissionError(
                "cannot read admission monotonic witness journal"
            ) from exc
        records: list[dict[str, object]] = []
        previous_witness_sha256: str | None = None
        expected_keys = {
            "witness_schema_version",
            "generation",
            "state_identity",
            "state_name",
            "state_sha256",
            "previous_witness_sha256",
            "witness_sha256",
        }
        for generation, raw in enumerate(lines, start=1):
            if not raw:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness journal contains a blank line"
                )
            try:
                record = json.loads(
                    raw,
                    object_pairs_hook=_pairs,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        PaperCampaignAdmissionError(
                            f"admission witness contains non-finite value {value}"
                        )
                    ),
                )
            except json.JSONDecodeError as exc:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness journal is unreadable"
                ) from exc
            if type(record) is not dict or set(record) != expected_keys:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness schema is invalid"
                )
            if record["witness_schema_version"] != _WITNESS_SCHEMA_VERSION:
                raise PaperCampaignAdmissionError(
                    "unsupported admission monotonic witness schema"
                )
            if record["generation"] != generation:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness generation is not contiguous"
                )
            if record["state_identity"] != self._state_identity:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness belongs to another state path"
                )
            if record["state_name"] != self.state_path.name:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness belongs to another journal"
                )
            _sha(record["state_sha256"], "witness state_sha256")
            if record["previous_witness_sha256"] != previous_witness_sha256:
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness predecessor mismatch"
                )
            body = {
                key: record[key]
                for key in expected_keys
                if key != "witness_sha256"
            }
            if _sha(record["witness_sha256"], "witness_sha256") != _digest(body):
                raise PaperCampaignAdmissionError(
                    "admission monotonic witness digest mismatch"
                )
            records.append(record)
            previous_witness_sha256 = record["witness_sha256"]
        return records

    def _append_witness(self, *, generation: int, state_sha256: str) -> None:
        records = self._read_witnesses()
        if generation != len(records) + 1:
            raise PaperCampaignAdmissionError(
                "admission state did not advance beyond monotonic authority"
            )
        previous = None if not records else records[-1]["witness_sha256"]
        body = {
            "witness_schema_version": _WITNESS_SCHEMA_VERSION,
            "generation": generation,
            "state_identity": self._state_identity,
            "state_name": self.state_path.name,
            "state_sha256": _sha(state_sha256, "state_sha256"),
            "previous_witness_sha256": previous,
        }
        record = {**body, "witness_sha256": _digest(body)}
        existed = self._witness_path.exists()
        try:
            with self._witness_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if not existed:
                _sync_authority_directory(self._witness_path.parent)
        except OSError as exc:
            raise PaperCampaignAdmissionError(
                "admission monotonic witness durability barrier failed"
            ) from exc

    def _read_local(self) -> dict[str, object]:
        try:
            state = json.loads(
                self.state_path.read_text(encoding="utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    PaperCampaignAdmissionError(
                        f"admission JSON contains non-finite value {value}"
                    )
                ),
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperCampaignAdmissionError("admission state is unreadable") from exc
        if type(state) is not dict or set(state) != {
            "schema",
            "schema_version",
            "generation",
            "admissions",
            "state_sha256",
        }:
            raise PaperCampaignAdmissionError("admission state schema mismatch")
        if (
            state["schema"] != SCHEMA
            or state["schema_version"] != SCHEMA_VERSION
            or type(state["generation"]) is not int
            or state["generation"] < 1
            or type(state["admissions"]) is not dict
        ):
            raise PaperCampaignAdmissionError("unsupported admission state")
        bare = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "generation": state["generation"],
            "admissions": state["admissions"],
        }
        if _sha(state["state_sha256"], "state_sha256") != _digest(bare):
            raise PaperCampaignAdmissionError("admission state digest mismatch")
        expected = {
            "admission_id",
            "intent_sha256",
            "phase",
            "baseline_checkpoint",
            "baseline_checkpoint_id",
            "ticket_id",
            "decision_id",
            "action_id",
        }
        for admission_id, record in state["admissions"].items():
            _text(admission_id, "admission_id")
            if type(record) is not dict or set(record) != expected:
                raise PaperCampaignAdmissionError("admission record fields mismatch")
            if record["admission_id"] != admission_id:
                raise PaperCampaignAdmissionError("admission identity mismatch")
            _sha(record["intent_sha256"], "intent_sha256")
            checkpoint = _checkpoint(record["baseline_checkpoint"])
            if (
                _sha(record["baseline_checkpoint_id"], "baseline_checkpoint_id")
                != checkpoint.checkpoint_id
            ):
                raise PaperCampaignAdmissionError(
                    "admission baseline checkpoint identity mismatch"
                )
            if record["phase"] not in {_PREPARED, _COMMITTED}:
                raise PaperCampaignAdmissionError("admission phase is invalid")
            for field in ("ticket_id", "decision_id", "action_id"):
                value = record[field]
                if value is not None:
                    _text(value, field)
            if record["phase"] == _COMMITTED and any(
                record[field] is None for field in ("ticket_id", "decision_id", "action_id")
            ):
                raise PaperCampaignAdmissionError(
                    "committed admission lacks durable identities"
                )
        return state

    def _read(self) -> dict[str, object]:
        state = self._read_local()
        witnesses = self._read_witnesses()
        if not witnesses:
            raise PaperCampaignAdmissionError(
                "admission state is missing independent monotonic authority"
            )
        latest = witnesses[-1]
        if (
            state["generation"] != latest["generation"]
            or state["state_sha256"] != latest["state_sha256"]
        ):
            raise PaperCampaignAdmissionError(
                "admission journal is older than monotonic authority"
            )
        return state

    def _write(self, admissions: dict[str, object]) -> None:
        if self.state_path.exists():
            current = self._read()
            generation = current["generation"] + 1
        else:
            if self._read_witnesses():
                raise PaperCampaignAdmissionError(
                    "cannot recreate admission journal behind monotonic authority"
                )
            generation = 1
        bare = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "generation": generation,
            "admissions": admissions,
        }
        state = {**bare, "state_sha256": _digest(bare)}
        self._append_witness(
            generation=generation,
            state_sha256=state["state_sha256"],
        )
        atomic_write_json(self.state_path, state)

    @staticmethod
    def _marker(admission_id: str, intent_sha256: str, strategy_reason: str) -> str:
        return (
            f"AUTOSPORT_PAPER_ADMISSION_V1:{_digest(admission_id)}:{intent_sha256}:"
            f"{strategy_reason}"
        )

    @staticmethod
    def _verify_ticket(
        ticket: PaperTicket,
        *,
        marker: str,
        legs: tuple[TicketLeg, ...],
        stake: Decimal,
        placed_at: str,
        provider_source_ids: tuple[str, ...],
        provider_accounts: tuple[tuple[str, str], ...],
        bankroll_id: str,
        currency: str,
    ) -> None:
        if (
            ticket.strategy_reason != marker
            or ticket.stake != stake
            or tuple(ticket.legs) != legs
            or ticket.placed_at != placed_at
            or ticket.provider_source_ids != provider_source_ids
            or ticket.provider_accounts != provider_accounts
            or ticket.bankroll_id != bankroll_id
            or ticket.currency != currency
        ):
            raise PaperCampaignAdmissionError(
                "durable PaperTicket conflicts with admission intent"
            )

    def _ticket(
        self,
        *,
        admission_id: str,
        intent_sha256: str,
        legs: tuple[TicketLeg, ...],
        stake: Decimal,
        strategy_reason: str,
        placed_at: str,
        provider_source_ids: tuple[str, ...],
        provider_accounts: tuple[tuple[str, str], ...],
        bankroll_id: str,
        currency: str,
        expected_ticket_id: str | None = None,
    ) -> PaperTicket:
        book = PaperBook.load(self.paper_book_path)
        marker = self._marker(admission_id, intent_sha256, strategy_reason)
        matches = [ticket for ticket in book.tickets.values() if ticket.strategy_reason == marker]
        if expected_ticket_id is not None:
            ticket = book.tickets.get(expected_ticket_id)
            if ticket is None or len(matches) != 1 or matches[0].ticket_id != expected_ticket_id:
                raise PaperCampaignAdmissionError(
                    "committed admission PaperTicket is missing or substituted"
                )
            self._verify_ticket(
                ticket,
                marker=marker,
                legs=legs,
                stake=stake,
                placed_at=placed_at,
                provider_source_ids=provider_source_ids,
                provider_accounts=provider_accounts,
                bankroll_id=bankroll_id,
                currency=currency,
            )
            return ticket
        if len(matches) > 1:
            raise PaperCampaignAdmissionError("admission marker binds multiple PaperTickets")
        if not matches:
            ticket = book.open_ticket(
                legs,
                stake,
                reason=marker,
                placed_at=placed_at,
                provider_source_ids=provider_source_ids,
                provider_accounts=provider_accounts,
                bankroll_id=bankroll_id,
                currency=currency,
            )
            book.save(self.paper_book_path)
            return ticket
        ticket = matches[0]
        self._verify_ticket(
            ticket,
            marker=marker,
            legs=legs,
            stake=stake,
            placed_at=placed_at,
            provider_source_ids=provider_source_ids,
            provider_accounts=provider_accounts,
            bankroll_id=bankroll_id,
            currency=currency,
        )
        return ticket

    def _verify_committed_loop(
        self,
        record: dict[str, object],
        baseline: EnvironmentCheckpoint,
    ) -> None:
        snapshot = self.runtime.agent_loop.snapshot()
        bridge = self.runtime.settlement_bridge
        if (
            snapshot.phase is not AgentLoopPhase.WAIT_OUTCOME
            or snapshot.external_effect_state is not ExternalEffectState.PAPER_ONLY
            or snapshot.economic_goal_fingerprint != bridge.goal_fingerprint
            or snapshot.risk_fingerprint != bridge.risk_fingerprint
            or snapshot.environment_id != baseline.environment_id
            or snapshot.episode_id != baseline.episode_id
            or snapshot.policy_id != baseline.policy_id
            or snapshot.environment_checkpoint_id != baseline.checkpoint_id
            or snapshot.action_id != record["action_id"]
        ):
            raise PaperCampaignAdmissionError(
                "committed admission differs from durable AgentLoop identity"
            )

    def admit(
        self,
        *,
        admission_id: str,
        observation: Observation,
        action_type: str,
        decision_action: str,
        decision_at: str,
        at: str,
        legs: tuple[TicketLeg, ...],
        stake: Decimal | str,
        placed_at: str,
        replay_run_id: str,
        agent: str,
        strategy_reason: str = "",
        decision_payload: Mapping[str, object] | None = None,
        action_parameters: tuple[tuple[str, str], ...] = (),
        provider_source_ids: tuple[str, ...] = (),
        provider_accounts: tuple[tuple[str, str], ...] = (),
        bankroll_id: str | None = None,
        currency: str | None = None,
    ) -> PaperCampaignAdmissionReceipt:
        admission_id = _text(admission_id, "admission_id")
        action_type = _text(action_type, "action_type")
        decision_action = _text(decision_action, "decision_action")
        replay_run_id = _text(replay_run_id, "replay_run_id")
        agent = _text(agent, "agent")
        if not isinstance(observation, Observation):
            raise TypeError("observation must be Observation")
        if type(legs) is not tuple or not legs:
            raise TypeError("legs must be a non-empty tuple")
        leg_payloads = [_leg_payload(leg) for leg in legs]
        amount = Decimal(str(stake))
        if not amount.is_finite() or amount <= 0:
            raise PaperCampaignAdmissionError("stake must be a finite positive Decimal")
        if decision_payload is None:
            extra_payload: dict[str, object] = {}
        elif isinstance(decision_payload, Mapping):
            extra_payload = dict(decision_payload)
            _json(extra_payload)
        else:
            raise TypeError("decision_payload must be a mapping or None")
        if _RESERVED_DECISION_PAYLOAD & set(extra_payload):
            raise PaperCampaignAdmissionError(
                "decision_payload attempts to replace admission authority fields"
            )
        if type(action_parameters) is not tuple:
            raise TypeError("action_parameters must be a canonical tuple")
        normalized_action_parameters: list[tuple[str, str]] = []
        parameter_keys: list[str] = []
        for item in action_parameters:
            if type(item) is not tuple or len(item) != 2:
                raise PaperCampaignAdmissionError("action_parameters are malformed")
            key = _text(item[0], "action parameter key")
            value = _text(item[1], "action parameter value")
            if key in _RESERVED_ACTION_PARAMETERS:
                raise PaperCampaignAdmissionError(
                    "action_parameters replace reserved identity"
                )
            normalized_action_parameters.append((key, value))
            parameter_keys.append(key)
        if len(parameter_keys) != len(set(parameter_keys)):
            raise PaperCampaignAdmissionError("action_parameters contain duplicate keys")
        action_parameters = tuple(normalized_action_parameters)

        goal = self.runtime.settlement_bridge.economic_goal
        risk = self.runtime.settlement_bridge.risk_policy
        goal_fingerprint = self.runtime.settlement_bridge.goal_fingerprint
        risk_fingerprint = self.runtime.settlement_bridge.risk_fingerprint
        bankroll_id = goal.bankroll_id if bankroll_id is None else _text(bankroll_id, "bankroll_id")
        currency = goal.currency if currency is None else _text(currency, "currency")
        if bankroll_id != goal.bankroll_id or currency != goal.currency:
            raise PaperCampaignAdmissionError(
                "admission bankroll/currency differs from economic authority"
            )

        with WorkspaceEconomicLock(self._admission_lock_workspace):
            state = self._read()
            record = state["admissions"].get(admission_id)
            if record is None:
                baseline = self.runtime.environment.checkpoint()
            else:
                baseline = _checkpoint(record["baseline_checkpoint"])

            baseline_payload = _checkpoint_payload(baseline)
            intent = {
                "admission_id": admission_id,
                "observation": _observation_payload(observation),
                "action_type": action_type,
                "decision_action": decision_action,
                "decision_at": decision_at,
                "at": at,
                "legs": leg_payloads,
                "stake": str(amount),
                "placed_at": placed_at,
                "replay_run_id": replay_run_id,
                "agent": agent,
                "strategy_reason": strategy_reason,
                "decision_payload": extra_payload,
                "action_parameters": [list(item) for item in action_parameters],
                "provider_source_ids": list(provider_source_ids),
                "provider_accounts": [list(item) for item in provider_accounts],
                "bankroll_id": bankroll_id,
                "currency": currency,
                "economic_goal_fingerprint": goal_fingerprint,
                "risk_fingerprint": risk_fingerprint,
                "baseline_checkpoint": baseline_payload,
            }
            intent_sha256 = _digest(intent)

            if record is None:
                record = {
                    "admission_id": admission_id,
                    "intent_sha256": intent_sha256,
                    "phase": _PREPARED,
                    "baseline_checkpoint": baseline_payload,
                    "baseline_checkpoint_id": baseline.checkpoint_id,
                    "ticket_id": None,
                    "decision_id": None,
                    "action_id": None,
                }
                state["admissions"][admission_id] = record
                self._write(state["admissions"])
            elif record["intent_sha256"] != intent_sha256:
                raise PaperCampaignAdmissionError(
                    "admission retry conflicts with durable intent"
                )

            committed_retry = record["phase"] == _COMMITTED
            if committed_retry:
                self._verify_committed_loop(record, baseline)

            with WorkspaceEconomicLock(self.workspace):
                ticket = self._ticket(
                    admission_id=admission_id,
                    intent_sha256=intent_sha256,
                    legs=legs,
                    stake=amount,
                    strategy_reason=strategy_reason,
                    placed_at=placed_at,
                    provider_source_ids=provider_source_ids,
                    provider_accounts=provider_accounts,
                    bankroll_id=bankroll_id,
                    currency=currency,
                    expected_ticket_id=(record["ticket_id"] if committed_retry else None),
                )
                decision_id = _digest(
                    {
                        "schema": SCHEMA,
                        "admission_id": admission_id,
                        "intent_sha256": intent_sha256,
                        "ticket_id": ticket.ticket_id,
                    }
                )
                if committed_retry and decision_id != record["decision_id"]:
                    raise PaperCampaignAdmissionError(
                        "committed admission decision identity is inconsistent"
                    )
                payload = {
                    **extra_payload,
                    "ticket_id": ticket.ticket_id,
                    "quote_keys": sorted(leg.quote_key for leg in legs),
                    "stake": str(ticket.stake),
                    MATERIAL_ACTION_ID_PAYLOAD_KEY: admission_id,
                }
                decision = DecisionRecord(
                    replay_run_id=replay_run_id,
                    agent=agent,
                    observed_ts=observation.observed_at,
                    action=decision_action,
                    payload=payload,
                    context_hash=observation.observation_id,
                    decision_id=decision_id,
                    recorded_at=at,
                    decision_kind=ECONOMIC_DECISION_KIND,
                )
                existing = None
                if self.decision_ledger.path.exists():
                    existing = self.decision_ledger.verified_economic_decision_for_material_action(
                        admission_id,
                        goal,
                        risk_policy=risk,
                    )
                if existing is None:
                    if committed_retry:
                        raise PaperCampaignAdmissionError(
                            "committed admission economic decision is missing"
                        )
                    self.decision_ledger.append_economic(
                        decision,
                        EconomicDecisionAuthority(goal, risk),
                    )
                else:
                    provenance_keys = {
                        ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
                        RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
                    }
                    existing_payload = {
                        key: value
                        for key, value in existing.payload.items()
                        if key not in provenance_keys
                    }
                    if (
                        existing.decision_id != decision.decision_id
                        or existing.replay_run_id != decision.replay_run_id
                        or existing.agent != decision.agent
                        or existing.observed_ts != decision.observed_ts
                        or existing.action != decision.action
                        or existing.context_hash != decision.context_hash
                        or existing.recorded_at != decision.recorded_at
                        or existing_payload != dict(decision.payload)
                    ):
                        raise PaperCampaignAdmissionError(
                            "durable economic decision conflicts with admission intent"
                        )

            parameters = tuple(
                sorted(
                    (
                        *action_parameters,
                        ("economic_decision_id", decision_id),
                        ("paper_ticket_id", ticket.ticket_id),
                    )
                )
            )
            action = self.runtime.begin_and_bind_paper_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision_id,
                observation=observation,
                action_type=action_type,
                decision_at=decision_at,
                parameters=parameters,
                at=at,
                baseline_checkpoint=baseline,
            )
            if not isinstance(action, Action):
                raise PaperCampaignAdmissionError(
                    "campaign runtime returned no canonical Action"
                )

            state = self._read()
            current = state["admissions"].get(admission_id)
            if current is None or current["intent_sha256"] != intent_sha256:
                raise PaperCampaignAdmissionError("admission journal changed during commit")
            if committed_retry:
                if (
                    current["phase"] != _COMMITTED
                    or current["ticket_id"] != ticket.ticket_id
                    or current["decision_id"] != decision_id
                    or current["action_id"] != action.action_id
                ):
                    raise PaperCampaignAdmissionError(
                        "committed admission identities differ from canonical authorities"
                    )
                return PaperCampaignAdmissionReceipt(
                    admission_id=admission_id,
                    ticket_id=ticket.ticket_id,
                    decision_id=decision_id,
                    baseline_checkpoint_id=baseline.checkpoint_id,
                    action_id=action.action_id,
                )

            committed = {
                **current,
                "phase": _COMMITTED,
                "ticket_id": ticket.ticket_id,
                "decision_id": decision_id,
                "action_id": action.action_id,
            }
            state["admissions"][admission_id] = committed
            self._write(state["admissions"])
            return PaperCampaignAdmissionReceipt(
                admission_id=admission_id,
                ticket_id=ticket.ticket_id,
                decision_id=decision_id,
                baseline_checkpoint_id=baseline.checkpoint_id,
                action_id=action.action_id,
            )
