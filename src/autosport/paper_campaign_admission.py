"""Crash-safe composition admission for one PAPER ticket -> learning action.

The existing authorities deliberately stop at separate durable boundaries:
``PaperBook`` owns virtual tickets, ``JsonlDecisionLedger`` owns economic decisions,
and ``PaperCampaignRuntime`` owns AgentLoop/action/settlement-learning binding.  This
module owns only the missing composition journal which makes those boundaries
retryable as one logical PAPER admission.  It never creates a real-money path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from .domain import TicketLeg
from .integrity import atomic_write_json
from .learning_environment import Action, Observation
from .paper import PaperBook
from .paper_campaign_runtime import PaperCampaignRuntime
from .workspace_lock import WorkspaceEconomicLock


SCHEMA = "autosport.paper_campaign_admission"
SCHEMA_VERSION = 1
_PREPARED = "PREPARED"
_COMMITTED = "COMMITTED"
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


@dataclass(frozen=True, slots=True)
class PaperCampaignAdmissionReceipt:
    admission_id: str
    ticket_id: str
    decision_id: str
    baseline_checkpoint_id: str
    action_id: str


class PaperCampaignAdmissionCoordinator:
    """Converge a logical PAPER admission across existing durable authorities.

    A dedicated admission lock serializes retries of this composition journal.  The
    canonical workspace economic lock is acquired only around PaperBook + ledger
    publication and released before ``PaperCampaignRuntime`` is entered, avoiding a
    nested acquisition while still excluding competing economic writers.
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
        with WorkspaceEconomicLock(self._admission_lock_workspace):
            if self.state_path.exists():
                self._read()
            else:
                self._write({})

    def _write(self, admissions: dict[str, object]) -> None:
        bare = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "admissions": admissions,
        }
        atomic_write_json(self.state_path, {**bare, "state_sha256": _digest(bare)})

    def _read(self) -> dict[str, object]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperCampaignAdmissionError("admission state is unreadable") from exc
        if type(state) is not dict or set(state) != {
            "schema",
            "schema_version",
            "admissions",
            "state_sha256",
        }:
            raise PaperCampaignAdmissionError("admission state schema mismatch")
        if (
            state["schema"] != SCHEMA
            or state["schema_version"] != SCHEMA_VERSION
            or type(state["admissions"]) is not dict
        ):
            raise PaperCampaignAdmissionError("unsupported admission state")
        bare = {
            "schema": state["schema"],
            "schema_version": state["schema_version"],
            "admissions": state["admissions"],
        }
        if _sha(state["state_sha256"], "state_sha256") != _digest(bare):
            raise PaperCampaignAdmissionError("admission state digest mismatch")
        expected = {
            "admission_id",
            "intent_sha256",
            "phase",
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
            _sha(record["baseline_checkpoint_id"], "baseline_checkpoint_id")
            if record["phase"] not in {_PREPARED, _COMMITTED}:
                raise PaperCampaignAdmissionError("admission phase is invalid")
            for field in ("ticket_id", "decision_id", "action_id"):
                value = record[field]
                if value is not None:
                    _text(value, field)
            if record["phase"] == _COMMITTED and any(
                record[field] is None for field in ("ticket_id", "decision_id", "action_id")
            ):
                raise PaperCampaignAdmissionError("committed admission lacks durable identities")
        return state

    @staticmethod
    def _marker(admission_id: str, intent_sha256: str, strategy_reason: str) -> str:
        return (
            f"AUTOSPORT_PAPER_ADMISSION_V1:{_digest(admission_id)}:{intent_sha256}:"
            f"{strategy_reason}"
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
    ):
        book = PaperBook.load(self.paper_book_path)
        marker = self._marker(admission_id, intent_sha256, strategy_reason)
        matches = [ticket for ticket in book.tickets.values() if ticket.strategy_reason == marker]
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
        if (
            ticket.stake != stake
            or tuple(ticket.legs) != legs
            or ticket.placed_at != placed_at
            or ticket.provider_source_ids != provider_source_ids
            or ticket.provider_accounts != provider_accounts
            or ticket.bankroll_id != bankroll_id
            or ticket.currency != currency
        ):
            raise PaperCampaignAdmissionError("durable PaperTicket conflicts with admission intent")
        return ticket

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
        else:
            raise TypeError("decision_payload must be a mapping or None")
        if _RESERVED_DECISION_PAYLOAD & set(extra_payload):
            raise PaperCampaignAdmissionError("decision_payload attempts to replace admission authority fields")
        if type(action_parameters) is not tuple:
            raise TypeError("action_parameters must be a canonical tuple")
        parameter_keys = [item[0] for item in action_parameters]
        if (
            any(type(item) is not tuple or len(item) != 2 for item in action_parameters)
            or any(key in _RESERVED_ACTION_PARAMETERS for key in parameter_keys)
            or len(parameter_keys) != len(set(parameter_keys))
        ):
            raise PaperCampaignAdmissionError("action_parameters are malformed or reserved")
        goal = self.runtime.settlement_bridge.economic_goal
        risk = self.runtime.settlement_bridge.risk_policy
        bankroll_id = goal.bankroll_id if bankroll_id is None else _text(bankroll_id, "bankroll_id")
        currency = goal.currency if currency is None else _text(currency, "currency")
        if bankroll_id != goal.bankroll_id or currency != goal.currency:
            raise PaperCampaignAdmissionError("admission bankroll/currency differs from economic authority")

        baseline = self.runtime.environment.checkpoint()
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
            "baseline_checkpoint_id": baseline.checkpoint_id,
        }
        intent_sha256 = _digest(intent)

        with WorkspaceEconomicLock(self._admission_lock_workspace):
            state = self._read()
            record = state["admissions"].get(admission_id)
            if record is None:
                record = {
                    "admission_id": admission_id,
                    "intent_sha256": intent_sha256,
                    "phase": _PREPARED,
                    "baseline_checkpoint_id": baseline.checkpoint_id,
                    "ticket_id": None,
                    "decision_id": None,
                    "action_id": None,
                }
                state["admissions"][admission_id] = record
                self._write(state["admissions"])
            else:
                if (
                    record["intent_sha256"] != intent_sha256
                    or record["baseline_checkpoint_id"] != baseline.checkpoint_id
                ):
                    raise PaperCampaignAdmissionError("admission retry conflicts with durable intent")
                if record["phase"] == _COMMITTED:
                    return PaperCampaignAdmissionReceipt(
                        admission_id=admission_id,
                        ticket_id=record["ticket_id"],
                        decision_id=record["decision_id"],
                        baseline_checkpoint_id=record["baseline_checkpoint_id"],
                        action_id=record["action_id"],
                    )

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
                )
                decision_id = _digest(
                    {
                        "schema": SCHEMA,
                        "admission_id": admission_id,
                        "intent_sha256": intent_sha256,
                        "ticket_id": ticket.ticket_id,
                    }
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
                existing = self.decision_ledger.verified_economic_decision_for_material_action(
                    admission_id,
                    goal,
                    risk_policy=risk,
                )
                if existing is None:
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
                        key: value for key, value in existing.payload.items() if key not in provenance_keys
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
                    (*action_parameters,
                     ("economic_decision_id", decision_id),
                     ("paper_ticket_id", ticket.ticket_id))
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
                raise PaperCampaignAdmissionError("campaign runtime returned no canonical Action")

            state = self._read()
            current = state["admissions"].get(admission_id)
            if current is None or current["intent_sha256"] != intent_sha256:
                raise PaperCampaignAdmissionError("admission journal changed during commit")
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
