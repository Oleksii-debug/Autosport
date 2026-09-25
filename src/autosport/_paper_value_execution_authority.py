from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import _paper_execution_reality_legacy as _paper_impl
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    GENERAL_DECISION_KIND,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .domain import MarketEvent, TicketLeg
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .paper import PaperBook
from .paper_execution_adoption import (
    PaperExecutionAdoptionError,
    PaperExecutionAdoptionRuntime,
    PaperExposureBinding,
    PreparedPaperExecution,
)
from .paper_strategy import PaperDecisionReconciliationRequired, PaperValueAgent
from .price_truth import paper_quote_rejection_reason
from .real_execution_ledger import ExecutionPlan
from .risk import PaperRiskPolicy, ProposedTicketRiskContext


@dataclass(frozen=True, slots=True)
class PaperValueExecutionDescriptor:
    """Non-authoritative description of a legacy paper-value execution plan."""

    execution_plan: ExecutionPlan
    exposure_bindings: tuple[PaperExposureBinding, ...]
    intent_evidence_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_plan, ExecutionPlan):
            raise TypeError("execution_plan must be ExecutionPlan")
        if (
            type(self.exposure_bindings) is not tuple
            or any(
                not isinstance(item, PaperExposureBinding)
                for item in self.exposure_bindings
            )
        ):
            raise TypeError(
                "exposure_bindings must be a tuple of PaperExposureBinding values"
            )
        action_ids = tuple(action.action_id for action in self.execution_plan.actions)
        binding_ids = tuple(item.action_id for item in self.exposure_bindings)
        if action_ids != binding_ids:
            raise ValueError("exposure bindings must exactly match execution action order")
        if type(self.intent_evidence_json) is not str or not self.intent_evidence_json:
            raise ValueError("intent_evidence_json must be non-empty canonical JSON")


_ORIGINAL_PREPARE_PAPER_VALUE_ACTION = (
    PaperExecutionAdoptionRuntime.prepare_paper_value_action
)
_ORIGINAL_EXPECTED_RUN_ID = PaperExecutionAdoptionRuntime.expected_run_id
_ORIGINAL_EXECUTE = PaperExecutionAdoptionRuntime.execute
_ORIGINAL_ON_MARKET_EVENT = PaperValueAgent.on_market_event
_PAPER_VALUE_ACTION = "OPEN_PAPER_VALUE_TICKET"
_FRAME_MISSING = object()
_EXECUTION_AUTHORITY_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "decision_id",
        "quote_id",
        "event",
        "stake",
        "provider_account",
        "bankroll_id",
        "currency",
    }
)

_RISK_ADMISSION_SCHEMA = "autosport.paper_value.general_risk_admission"
_RISK_ADMISSION_SCHEMA_VERSION = 1
_RISK_ADMISSION_DIR = ".paper-value-risk-admissions"
_RISK_ADMISSION_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "decision_id",
        "replay_run_id",
        "observed_ts",
        "context_hash",
        "quote_key",
        "requested_stake",
        "execution_plan_id",
        "execution_plan_fingerprint",
        "execution_run_id",
        "execution_authority_sha256",
        "provider_source_id",
        "account_id",
        "risk_policy_sha256",
        "pre_action_book_sha256",
        "witness_sha256",
    }
)


def _canonical_payload_sha256(payload: dict[str, object]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _risk_admission_paths(
    ledger: JsonlDecisionLedger,
    decision_id: str,
) -> tuple[Path, Path]:
    token = hashlib.sha256(decision_id.encode("utf-8")).hexdigest()
    root = ledger.path.parent / _RISK_ADMISSION_DIR
    return root / f"{token}.json", root / f"{token}.pre-action.json"


def _risk_admission_payload(
    *,
    record: DecisionRecord,
    descriptor: PaperValueExecutionDescriptor,
    risk_policy: PaperRiskPolicy,
    execution_run_id: str,
    pre_action_book_sha256: str,
) -> dict[str, object]:
    if len(descriptor.execution_plan.actions) != 1:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission requires exactly one execution action"
        )
    action = descriptor.execution_plan.actions[0]
    payload: dict[str, object] = {
        "schema": _RISK_ADMISSION_SCHEMA,
        "schema_version": _RISK_ADMISSION_SCHEMA_VERSION,
        "decision_id": record.decision_id,
        "replay_run_id": record.replay_run_id,
        "observed_ts": record.observed_ts,
        "context_hash": record.context_hash,
        "quote_key": record.payload.get("quote_key"),
        "requested_stake": str(action.requested_stake),
        "execution_plan_id": descriptor.execution_plan.plan_id,
        "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
        "execution_run_id": execution_run_id,
        "execution_authority_sha256": hashlib.sha256(
            descriptor.intent_evidence_json.encode("utf-8")
        ).hexdigest(),
        "provider_source_id": action.bookmaker_id,
        "account_id": action.account_id,
        "risk_policy_sha256": risk_policy.provenance_sha256,
        "pre_action_book_sha256": pre_action_book_sha256,
    }
    payload["witness_sha256"] = _canonical_payload_sha256(payload)
    return payload


def _load_general_risk_admission(
    ledger: JsonlDecisionLedger,
    decision_id: str,
) -> tuple[dict[str, object], PaperBook]:
    witness_path, pre_action_path = _risk_admission_paths(ledger, decision_id)
    if witness_path.is_symlink() or pre_action_path.is_symlink():
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness path is not canonical"
        )
    try:
        raw = strict_json_loads(witness_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "durable GENERAL paper-value action lacks canonical risk admission witness"
        ) from exc
    if type(raw) is not dict or set(raw) != _RISK_ADMISSION_FIELDS:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness schema is invalid"
        )
    if (
        raw.get("schema") != _RISK_ADMISSION_SCHEMA
        or raw.get("schema_version") != _RISK_ADMISSION_SCHEMA_VERSION
        or raw.get("decision_id") != decision_id
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness identity is invalid"
        )
    witness_sha256 = raw.get("witness_sha256")
    unsigned = dict(raw)
    unsigned.pop("witness_sha256", None)
    if (
        type(witness_sha256) is not str
        or witness_sha256 != _canonical_payload_sha256(unsigned)
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness digest mismatch"
        )
    try:
        pre_action_book = PaperBook.load(pre_action_path)
    except (OSError, TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission pre-action PaperBook is unreadable"
        ) from exc
    return raw, pre_action_book


def _issue_general_risk_admission(
    *,
    agent: PaperValueAgent,
    context,
    descriptor: PaperValueExecutionDescriptor,
    decision_id: str,
    started_at: str,
) -> None:
    """Persist one canonical goal-less risk pass before #623 can reserve a run."""

    ledger = context.decision_ledger
    runtime = context.paper_execution
    if (
        not isinstance(ledger, JsonlDecisionLedger)
        or not isinstance(runtime, PaperExecutionAdoptionRuntime)
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission requires canonical runtime authority"
        )
    matches = tuple(
        record
        for record in ledger.verified_records()
        if record.decision_id == decision_id
    )
    if len(matches) != 1:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission requires one exact durable decision"
        )
    record = matches[0]
    expected_run_id = runtime.expected_run_id(descriptor, decision_id)
    if (
        record.decision_kind != GENERAL_DECISION_KIND
        or record.replay_run_id != context.replay_run_id
        or record.agent != PaperValueAgent.name
        or record.action != _PAPER_VALUE_ACTION
        or record.observed_ts != started_at
        or record.payload.get("material_action_id") != decision_id
        or record.payload.get("requested_stake")
        != str(descriptor.execution_plan.actions[0].requested_stake)
        or record.payload.get("execution_plan_id") != descriptor.execution_plan.plan_id
        or record.payload.get("execution_plan_fingerprint")
        != descriptor.execution_plan.fingerprint
        or record.payload.get("execution_run_id") != expected_run_id
        or record.payload.get("execution_authority_json")
        != descriptor.intent_evidence_json
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission decision binding is invalid"
        )

    pre_action_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(
        context.paper_book
    )
    if pre_action_sha256 is None:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission cannot validate pre-action PaperBook"
        )
    witness = _risk_admission_payload(
        record=record,
        descriptor=descriptor,
        risk_policy=agent.risk_policy,
        execution_run_id=expected_run_id,
        pre_action_book_sha256=pre_action_sha256,
    )
    witness_path, pre_action_path = _risk_admission_paths(ledger, decision_id)
    witness_path.parent.mkdir(parents=True, exist_ok=True)
    if witness_path.is_symlink() or pre_action_path.is_symlink():
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness path is not canonical"
        )

    if pre_action_path.exists():
        try:
            persisted_pre_action = PaperBook.load(pre_action_path)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission pre-action PaperBook is unreadable"
            ) from exc
        persisted_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(
            persisted_pre_action
        )
        if (
            persisted_sha256 != pre_action_sha256
            or not runtime._same_book_state(context.paper_book, persisted_pre_action)
        ):
            raise PaperExecutionAdoptionError(
                "paper-value risk admission conflicts with existing pre-action state"
            )
    else:
        context.paper_book.save(pre_action_path)
        persisted_pre_action = PaperBook.load(pre_action_path)
        persisted_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(
            persisted_pre_action
        )
        if (
            persisted_sha256 != pre_action_sha256
            or not runtime._same_book_state(context.paper_book, persisted_pre_action)
        ):
            raise PaperExecutionAdoptionError(
                "paper-value risk admission pre-action state did not persist exactly"
            )

    if witness_path.exists():
        existing, _ = _load_general_risk_admission(ledger, decision_id)
        if existing != witness:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission conflicts with existing witness"
            )
        return
    atomic_write_json(witness_path, witness)
    verified, _ = _load_general_risk_admission(ledger, decision_id)
    if verified != witness:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness did not persist exactly"
        )


def _verify_general_risk_admission(
    *,
    agent: PaperValueAgent,
    context,
    record: DecisionRecord,
    descriptor: PaperValueExecutionDescriptor,
    expected_run_id: str,
) -> None:
    """Verify the internally-issued risk witness and exact restart book topology."""

    ledger = context.decision_ledger
    runtime = context.paper_execution
    if (
        not isinstance(ledger, JsonlDecisionLedger)
        or not isinstance(runtime, PaperExecutionAdoptionRuntime)
    ):
        raise PaperExecutionAdoptionError(
            "durable GENERAL paper-value recovery lacks canonical runtime authority"
        )
    witness, pre_action_book = _load_general_risk_admission(
        ledger,
        record.decision_id,
    )
    pre_action_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(
        pre_action_book
    )
    if pre_action_sha256 is None:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission pre-action PaperBook is invalid"
        )
    expected = _risk_admission_payload(
        record=record,
        descriptor=descriptor,
        risk_policy=agent.risk_policy,
        execution_run_id=expected_run_id,
        pre_action_book_sha256=pre_action_sha256,
    )
    if witness != expected:
        raise PaperExecutionAdoptionError(
            "durable GENERAL paper-value risk admission binding changed across restart"
        )

    prepared = runtime._mint_prepared(
        PreparedPaperExecution(
            execution_plan=descriptor.execution_plan,
            exposure_bindings=descriptor.exposure_bindings,
            intent_evidence_json=descriptor.intent_evidence_json,
        )
    )
    try:
        validator = getattr(runtime, "assert_recoverable_book_state", None)
        if validator is None:
            if not runtime._same_book_state(runtime.book, pre_action_book):
                raise PaperExecutionAdoptionError(
                    "paper-value recovery PaperBook differs from pre-action witness"
                )
        else:
            validator(
                pre_action_book=pre_action_book,
                prepared=prepared,
                trigger_id=record.decision_id,
                started_at=record.observed_ts,
                materialize_exposure=True,
            )
    finally:
        runtime._prepared_authorities.pop(id(prepared), None)



def _describe_paper_value_action(
    self: PaperExecutionAdoptionRuntime,
    **kwargs,
) -> PaperValueExecutionDescriptor:
    """Return exact audit data without minting positive execution authority."""

    prepared = _ORIGINAL_PREPARE_PAPER_VALUE_ACTION(self, **kwargs)
    self._prepared_authorities.pop(id(prepared), None)
    return PaperValueExecutionDescriptor(
        execution_plan=prepared.execution_plan,
        exposure_bindings=prepared.exposure_bindings,
        intent_evidence_json=prepared.intent_evidence_json,
    )


def _expected_run_id(
    self: PaperExecutionAdoptionRuntime,
    prepared,
    trigger_id: str,
) -> str:
    if isinstance(prepared, PaperValueExecutionDescriptor):
        return _paper_impl._run_id(
            prepared.execution_plan,
            trigger_id,
            self.config,
        )
    return _ORIGINAL_EXPECTED_RUN_ID(self, prepared, trigger_id)


def _run_reserved(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
    *,
    run_id: str | None = None,
) -> bool:
    """Resolve exact #623 reservation truth without treating it as proposal data."""

    try:
        events = (
            runtime.ledger.events(run_id)
            if run_id is not None
            else runtime.ledger.events()
        )
    except Exception:
        return False
    return any(
        item.get("event_type") == "RUN_RESERVED"
        and item.get("payload", {}).get("trigger_id") == decision_id
        for item in events
    )


def _durable_record_for_call(
    agent: PaperValueAgent,
    event: MarketEvent,
    context,
    decision_id: str,
) -> DecisionRecord | None:
    """Resolve an already-published material decision before fresh proposal gates."""

    ledger = context.decision_ledger
    if not isinstance(ledger, JsonlDecisionLedger):
        return None
    # A fresh strategy call may legitimately point at a ledger path that has not
    # been created yet. Absence is pristine "no durable decision" state; once the
    # path exists, any unreadable/malformed content remains a hard failure.
    if not ledger.path.exists():
        return None
    try:
        matches = tuple(
            item
            for item in ledger.verified_records()
            if item.decision_id == decision_id
        )
    except Exception as exc:
        raise PaperExecutionAdoptionError(
            "durable paper-value decision ledger cannot be verified"
        ) from exc
    if len(matches) > 1:
        raise PaperExecutionAdoptionError(
            "duplicate durable paper-value decision identity"
        )
    if not matches:
        return None

    record = matches[0]
    if (
        record.replay_run_id != context.replay_run_id
        or record.agent != PaperValueAgent.name
        or record.action != _PAPER_VALUE_ACTION
        or record.decision_id != decision_id
        or record.payload.get("material_action_id") != decision_id
        or record.payload.get("quote_key") != event.quote_key
    ):
        raise PaperExecutionAdoptionError(
            "durable paper-value decision identity changed across restart"
        )

    goal = agent.risk_policy.economic_goal
    if goal is None:
        if record.decision_kind != GENERAL_DECISION_KIND:
            raise PaperExecutionAdoptionError(
                "durable paper-value decision kind conflicts with current risk authority"
            )
        return record

    if record.decision_kind != ECONOMIC_DECISION_KIND:
        raise PaperExecutionAdoptionError(
            "durable paper-value economic action lacks economic decision authority"
        )
    try:
        verified = ledger.verified_economic_decision_for_material_action(
            decision_id,
            goal,
            risk_policy=agent.risk_policy,
        )
    except Exception as exc:
        raise PaperExecutionAdoptionError(
            "durable paper-value economic authority cannot be verified"
        ) from exc
    if verified != record:
        raise PaperExecutionAdoptionError(
            "durable paper-value economic authority changed across restart"
        )
    return record


def _descriptor_from_durable_record(
    runtime: PaperExecutionAdoptionRuntime,
    record: DecisionRecord,
    *,
    current_event: MarketEvent,
) -> tuple[PaperValueExecutionDescriptor, MarketEvent, Decimal, str, dict[str, object]]:
    """Rebuild the exact immutable execution descriptor from durable decision bytes."""

    payload = record.payload
    raw_authority = payload.get("execution_authority_json")
    if type(raw_authority) is not str or not raw_authority:
        raise PaperExecutionAdoptionError(
            "durable paper-value decision lacks execution authority evidence"
        )
    try:
        authority = json.loads(raw_authority)
    except (TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "durable paper-value execution authority JSON is invalid"
        ) from exc
    if not isinstance(authority, dict) or set(authority) != _EXECUTION_AUTHORITY_FIELDS:
        raise PaperExecutionAdoptionError(
            "durable paper-value execution authority schema is invalid"
        )
    if (
        authority.get("schema") != "autosport.paper_value.execution_authority"
        or authority.get("schema_version") != 1
        or authority.get("decision_id") != record.decision_id
    ):
        raise PaperExecutionAdoptionError(
            "durable paper-value execution authority identity is invalid"
        )

    try:
        durable_event = MarketEvent.from_dict(authority["event"])
        stake = Decimal(str(authority["stake"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "durable paper-value execution authority values are invalid"
        ) from exc
    if not stake.is_finite() or stake <= 0:
        raise PaperExecutionAdoptionError(
            "durable paper-value execution stake is invalid"
        )
    if durable_event.quote_key != current_event.quote_key:
        raise PaperExecutionAdoptionError(
            "redelivered quote identity conflicts with durable paper-value action"
        )
    if durable_event.observed_ts != record.observed_ts:
        raise PaperExecutionAdoptionError(
            "durable paper-value execution time conflicts with decision evidence"
        )
    if payload.get("requested_stake") != str(stake):
        raise PaperExecutionAdoptionError(
            "durable paper-value requested stake conflicts with execution authority"
        )

    provider_account = authority.get("provider_account")
    if (
        type(provider_account) is not list
        or len(provider_account) != 2
        or type(provider_account[0]) is not str
        or type(provider_account[1]) is not str
        or provider_account[0] != durable_event.source_id
        or not provider_account[1]
    ):
        raise PaperExecutionAdoptionError(
            "durable paper-value provider account authority is invalid"
        )

    descriptor = runtime.prepare_paper_value_action(
        event=durable_event,
        stake=stake,
        decision_id=record.decision_id,
        account_id=provider_account[1],
        bankroll_id=authority.get("bankroll_id"),
        currency=authority.get("currency"),
    )
    expected_run_id = runtime.expected_run_id(descriptor, record.decision_id)
    expected = {
        "execution_plan_id": descriptor.execution_plan.plan_id,
        "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
        "execution_run_id": expected_run_id,
        "execution_authority_json": descriptor.intent_evidence_json,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise PaperExecutionAdoptionError(
                f"durable paper-value decision conflicts with execution authority: {key}"
            )
    return descriptor, durable_event, stake, expected_run_id, authority


def _first_execution_risk_authority(
    agent: PaperValueAgent,
    context,
    record: DecisionRecord,
    descriptor: PaperValueExecutionDescriptor,
    durable_event: MarketEvent,
    stake: Decimal,
    expected_run_id: str,
    authority: dict[str, object],
) -> str:
    """Prove restart authority without promoting generic ledger bytes to risk truth."""

    goal = agent.risk_policy.economic_goal
    if goal is None:
        _verify_general_risk_admission(
            agent=agent,
            context=context,
            record=record,
            descriptor=descriptor,
            expected_run_id=expected_run_id,
        )
        return "durable-risk-admission-recovery"

    if (
        authority.get("bankroll_id") != goal.bankroll_id
        or authority.get("currency") != goal.currency
    ):
        raise PaperExecutionAdoptionError(
            "durable paper-value execution authority conflicts with economic goal identity"
        )

    runtime = context.paper_execution
    assert isinstance(runtime, PaperExecutionAdoptionRuntime)
    if _run_reserved(
        runtime,
        record.decision_id,
        run_id=expected_run_id,
    ):
        return "durable-execution-recovery"

    if paper_quote_rejection_reason(durable_event, stake) is not None:
        raise PaperExecutionAdoptionError(
            "durable economic paper-value decision no longer proves quote execution safety"
        )
    provider_account = authority["provider_account"]
    assert isinstance(provider_account, list)
    leg = TicketLeg(
        durable_event.event_id,
        durable_event.market_id,
        durable_event.selection_id,
        durable_event.decimal_odds,
        sport=durable_event.sport,
    )
    proposal_context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(durable_event,),
        provider_accounts=((provider_account[0], provider_account[1]),),
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=record.observed_ts,
    )
    risk = agent.risk_policy.evaluate(
        context.paper_book,
        stake,
        context=proposal_context,
    )
    if not risk.allowed:
        raise PaperExecutionAdoptionError(
            "durable economic paper-value decision no longer proves its first execution risk gate"
        )
    return "fresh-risk-evaluation"


def _resume_durable_paper_value(
    agent: PaperValueAgent,
    event: MarketEvent,
    context,
    record: DecisionRecord,
) -> None:
    """Resume immutable durable execution before status/forecast/edge proposal gates."""

    runtime = context.paper_execution
    if not isinstance(runtime, PaperExecutionAdoptionRuntime):
        raise PaperExecutionAdoptionError(
            "durable paper-value recovery lacks canonical #623 runtime"
        )
    (
        descriptor,
        durable_event,
        stake,
        expected_run_id,
        authority,
    ) = _descriptor_from_durable_record(
        runtime,
        record,
        current_event=event,
    )
    risk_authority = _first_execution_risk_authority(
        agent,
        context,
        record,
        descriptor,
        durable_event,
        stake,
        expected_run_id,
        authority,
    )
    result = runtime.execute(
        prepared=descriptor,
        trigger_id=record.decision_id,
        started_at=record.observed_ts,
        materialize_exposure=True,
    )
    if result.run.run_id != expected_run_id:
        raise PaperExecutionAdoptionError(
            "durable paper-value recovery resolved a different #623 run"
        )
    agent._acted.add(event.quote_key)


def _canonical_agent_call(
    runtime: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    decision_id: str,
    started_at: str,
) -> tuple[JsonlDecisionLedger, object, PaperRiskPolicy, str, str]:
    """Resolve authority from the actually executing canonical code path itself."""

    current = inspect.currentframe()
    original_frame = None
    recovery_frame = None
    try:
        frame = current.f_back if current is not None else None
        while frame is not None:
            if original_frame is None and frame.f_code is _ORIGINAL_ON_MARKET_EVENT.__code__:
                original_frame = frame
            elif recovery_frame is None and frame.f_code is _resume_durable_paper_value.__code__:
                recovery_frame = frame
            frame = frame.f_back

        if recovery_frame is not None and original_frame is None:
            local = recovery_frame.f_locals
            agent = local.get("agent")
            context = local.get("context")
            event = local.get("event")
            record = local.get("record")
            if (
                not isinstance(agent, PaperValueAgent)
                or context is None
                or context.paper_execution is not runtime
                or not isinstance(record, DecisionRecord)
                or record.decision_id != decision_id
                or record.observed_ts != started_at
                or event is None
                or agent._material_action_id(context, event) != decision_id
                or local.get("descriptor") is not descriptor
            ):
                raise PaperExecutionAdoptionError(
                    "durable paper-value recovery call identity is invalid"
                )
            ledger = context.decision_ledger
            risk_policy = agent.risk_policy
            risk_authority = local.get("risk_authority")
            expected_run_id = local.get("expected_run_id")
            if (
                not isinstance(ledger, JsonlDecisionLedger)
                or not isinstance(risk_policy, PaperRiskPolicy)
                or risk_authority
                not in {
                    "fresh-risk-evaluation",
                    "durable-execution-recovery",
                    "durable-risk-admission-recovery",
                }
                or expected_run_id
                != runtime.expected_run_id(descriptor, decision_id)
            ):
                raise PaperExecutionAdoptionError(
                    "durable paper-value recovery lacks exact risk/execution authority"
                )
            if (
                risk_authority == "durable-execution-recovery"
                and not _run_reserved(
                    runtime,
                    decision_id,
                    run_id=expected_run_id,
                )
            ):
                raise PaperExecutionAdoptionError(
                    "durable paper-value recovery lost exact #623 reservation authority"
                )
            return (
                ledger,
                risk_policy.economic_goal,
                risk_policy,
                context.replay_run_id,
                risk_authority,
            )

        if original_frame is None:
            raise PaperExecutionAdoptionError(
                "paper-value execution descriptor requires the canonical PaperValueAgent execution path"
            )

        local = original_frame.f_locals
        agent = local.get("self")
        context = local.get("context")
        event = local.get("event")
        if not isinstance(agent, PaperValueAgent):
            raise PaperExecutionAdoptionError(
                "paper-value execution caller is not canonical PaperValueAgent"
            )
        if context is None or context.paper_execution is not runtime:
            raise PaperExecutionAdoptionError(
                "paper-value execution runtime is not bound to canonical AgentContext"
            )
        ledger = context.decision_ledger
        risk_policy = agent.risk_policy
        if not isinstance(ledger, JsonlDecisionLedger):
            raise PaperExecutionAdoptionError(
                "paper-value execution requires canonical JsonlDecisionLedger authority"
            )
        if not isinstance(risk_policy, PaperRiskPolicy):
            raise PaperExecutionAdoptionError(
                "paper-value execution requires canonical PaperRiskPolicy authority"
            )
        if (
            event is None
            or event.observed_ts != started_at
            or local.get("material_action_id") != decision_id
            or agent._material_action_id(context, event) != decision_id
            or local.get("prepared") is not descriptor
        ):
            raise PaperExecutionAdoptionError(
                "paper-value canonical call identity does not match execution descriptor"
            )

        goal = local.get("goal", _FRAME_MISSING)
        chosen_stake = local.get("chosen_stake", _FRAME_MISSING)
        risk = local.get("risk", _FRAME_MISSING)
        if goal is _FRAME_MISSING or goal is not risk_policy.economic_goal:
            raise PaperExecutionAdoptionError(
                "paper-value canonical call risk authority is inconsistent"
            )
        if chosen_stake is _FRAME_MISSING:
            raise PaperExecutionAdoptionError(
                "paper-value canonical call lacks chosen stake authority"
            )
        if risk is _FRAME_MISSING or getattr(risk, "allowed", None) is not True:
            raise PaperExecutionAdoptionError(
                "fresh paper-value execution has not passed canonical risk evaluation"
            )
        if goal is None:
            _issue_general_risk_admission(
                agent=agent,
                context=context,
                descriptor=descriptor,
                decision_id=decision_id,
                started_at=started_at,
            )
        return (
            ledger,
            goal,
            risk_policy,
            context.replay_run_id,
            "fresh-risk-evaluation",
        )
    finally:
        del current
        try:
            del frame
        except UnboundLocalError:
            pass
        del original_frame
        del recovery_frame


def _resolve_durable_paper_value_decision(
    self: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    trigger_id: str,
    started_at: str,
) -> DecisionRecord:
    decision_id = descriptor.execution_plan.decision_id
    if trigger_id != decision_id:
        raise PaperExecutionAdoptionError(
            "paper-value trigger does not match durable decision identity"
        )
    if descriptor.execution_plan.created_at != started_at:
        raise PaperExecutionAdoptionError(
            "paper-value execution time does not match durable decision observation"
        )

    ledger, goal, risk_policy, replay_run_id, risk_authority = _canonical_agent_call(
        self,
        descriptor=descriptor,
        decision_id=decision_id,
        started_at=started_at,
    )

    try:
        matches = tuple(
            record
            for record in ledger.verified_records()
            if record.decision_id == decision_id
        )
        if len(matches) != 1:
            raise PaperExecutionAdoptionError(
                "paper-value execution requires one exact durable decision"
            )
        record = matches[0]
        if goal is None:
            if record.decision_kind != GENERAL_DECISION_KIND:
                raise PaperExecutionAdoptionError(
                    "legacy paper-value execution requires GENERAL decision authority"
                )
        else:
            economic = ledger.verified_economic_decision_for_material_action(
                decision_id,
                goal,
                risk_policy=risk_policy,
            )
            if economic != record:
                raise PaperExecutionAdoptionError(
                    "paper-value execution requires exact durable economic authority"
                )
    except PaperExecutionAdoptionError:
        raise
    except Exception as exc:
        raise PaperExecutionAdoptionError(
            "paper-value durable decision authority could not be verified"
        ) from exc

    if (
        record.replay_run_id != replay_run_id
        or record.agent != PaperValueAgent.name
        or record.action != _PAPER_VALUE_ACTION
        or record.observed_ts != started_at
        or record.decision_id != decision_id
    ):
        raise PaperExecutionAdoptionError(
            "paper-value durable decision identity does not match execution descriptor"
        )

    if len(descriptor.execution_plan.actions) != 1:
        raise PaperExecutionAdoptionError(
            "legacy paper-value descriptor must contain exactly one execution action"
        )
    action = descriptor.execution_plan.actions[0]
    expected_run_id = _paper_impl._run_id(
        descriptor.execution_plan,
        trigger_id,
        self.config,
    )
    payload = record.payload
    expected = {
        "material_action_id": decision_id,
        "requested_stake": str(action.requested_stake),
        "execution_plan_id": descriptor.execution_plan.plan_id,
        "execution_plan_fingerprint": descriptor.execution_plan.fingerprint,
        "execution_run_id": expected_run_id,
        "execution_authority_json": descriptor.intent_evidence_json,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise PaperExecutionAdoptionError(
                f"paper-value durable decision conflicts with execution descriptor: {key}"
            )
    return record


def _authorize_descriptor(
    self: PaperExecutionAdoptionRuntime,
    *,
    descriptor: PaperValueExecutionDescriptor,
    trigger_id: str,
    started_at: str,
) -> PreparedPaperExecution:
    _resolve_durable_paper_value_decision(
        self,
        descriptor=descriptor,
        trigger_id=trigger_id,
        started_at=started_at,
    )
    return self._mint_prepared(
        PreparedPaperExecution(
            execution_plan=descriptor.execution_plan,
            exposure_bindings=descriptor.exposure_bindings,
            intent_evidence_json=descriptor.intent_evidence_json,
        )
    )


def _execute(
    self: PaperExecutionAdoptionRuntime,
    *,
    prepared,
    trigger_id: str,
    started_at: str,
    materialize_exposure: bool,
    observations=None,
    evidence_registry=None,
    suspended_action_ids: frozenset[str] = frozenset(),
):
    if not isinstance(prepared, PaperValueExecutionDescriptor):
        return _ORIGINAL_EXECUTE(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )

    authorized = _authorize_descriptor(
        self,
        descriptor=prepared,
        trigger_id=trigger_id,
        started_at=started_at,
    )
    try:
        return _ORIGINAL_EXECUTE(
            self,
            prepared=authorized,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )
    finally:
        self._prepared_authorities.pop(id(authorized), None)


def _on_market_event(self: PaperValueAgent, event, context) -> None:
    runtime = context.paper_execution
    if runtime is None:
        return _ORIGINAL_ON_MARKET_EVENT(self, event, context)
    if event.quote_key in self._acted:
        return

    ledger = context.decision_ledger
    if ledger is None:
        context.notes.append(
            "paper-value material action withheld: durable decision authority is unavailable"
        )
        return

    decision_id = self._material_action_id(context, event)
    record = _durable_record_for_call(self, event, context, decision_id)
    if record is not None:
        return _resume_durable_paper_value(self, event, context, record)

    if _run_reserved(runtime, decision_id):
        raise PaperDecisionReconciliationRequired(
            "#623 execution history exists without its durable paper-value decision"
        )

    # Only an action with no durable decision and no #623 history may traverse the
    # fresh status/forecast/edge/risk proposal path.
    return _ORIGINAL_ON_MARKET_EVENT(self, event, context)


def _install() -> None:
    if getattr(
        PaperExecutionAdoptionRuntime,
        "_autosport_paper_value_authority_installed",
        False,
    ):
        return
    PaperExecutionAdoptionRuntime.prepare_paper_value_action = (
        _describe_paper_value_action
    )
    PaperExecutionAdoptionRuntime.expected_run_id = _expected_run_id
    PaperExecutionAdoptionRuntime.execute = _execute
    PaperExecutionAdoptionRuntime._autosport_paper_value_authority_installed = True
    PaperValueAgent.on_market_event = _on_market_event


_install()
