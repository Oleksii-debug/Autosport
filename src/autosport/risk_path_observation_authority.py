from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .agent_loop import AgentLoopError, AgentLoopRuntime
from .decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    verify_economic_goal_binding,
)
from .domain import PaperTicket, TicketStatus
from .paper import PaperBook
from .paper_settlement_learning import (
    PaperSettlementLearningBridge,
    PaperSettlementLearningBridgeError,
    PaperSettlementLearningWitness,
)
from .risk_path_equity_replay import (
    RiskPathEquityReplayError,
    replay_paper_book_equity_path,
)
from .risk_sampling_dependence import (
    inspect_fixed_n_iid_sampling_structure,
    resolve_fixed_n_iid_precommit_authority,
)
from .risk_sampling_membership import ResolvedFixedNRiskMembership
from .risk_sampling_occurrence_authority import (
    ProductIidDrawPlanError,
    ProductIidExpectedDrawPlan,
    ProductIidRunAdmissionReceipt,
    ProductIidRunExecutionReceipt,
    expected_replay_consumed_payload_multiset_sha256,
    expected_replay_input_payload_sequence_sha256,
    resolve_product_iid_expected_draw_plan,
    resolve_product_iid_run_admission,
    resolve_product_iid_run_execution,
)
from .run_registry import ReconciliationError, RunRegistry
from .run_transaction import RunTransaction, RunTransactionError


_SCHEMA = "AUTOSPORT_PRODUCT_RUN_CAPITAL_PATH_EVIDENCE_V1"
_EFFECTS_SCHEMA = "AUTOSPORT_PRODUCT_RUN_SETTLEMENT_EFFECTS_V1"
_MAX_DECIMAL_TEXT = 512

_DECISION_TYPE = DecisionRecord
_VERIFY_GOAL = verify_economic_goal_binding
_VERIFY_GOAL_CODE = getattr(_VERIFY_GOAL, "__code__", None)
_TICKET_TYPE = PaperTicket
_TICKET_STATUS_TYPE = TicketStatus
_OPEN_STATUS = TicketStatus.OPEN
_WITNESS_TYPE = PaperSettlementLearningWitness
_MEMBERSHIP_TYPE = ResolvedFixedNRiskMembership
_PAPER_TYPE = PaperBook
_PAPER_LOAD = PaperBook.load_bytes
_PAPER_LOAD_CODE = getattr(_PAPER_LOAD.__func__, "__code__", None)
_PAPER_DEBIT = PaperBook._debit_balance
_PAPER_DEBIT_CODE = getattr(_PAPER_DEBIT.__func__, "__code__", None)
_REPLAY = replay_paper_book_equity_path
_REPLAY_CODE = getattr(_REPLAY, "__code__", None)
_BRIDGE_TYPE = PaperSettlementLearningBridge
_BRIDGE_WITNESS = PaperSettlementLearningBridge.resolution_witness
_BRIDGE_WITNESS_CODE = getattr(_BRIDGE_WITNESS, "__code__", None)
_BRIDGE_DECISION_MATCHES = PaperSettlementLearningBridge._decision_matches
_BRIDGE_DECISION_MATCHES_CODE = getattr(
    _BRIDGE_DECISION_MATCHES,
    "__code__",
    None,
)
_LOOP_TYPE = AgentLoopRuntime
_LOOP_READ = AgentLoopRuntime._read
_LOOP_READ_CODE = getattr(_LOOP_READ, "__code__", None)
_TX_TYPE = RunTransaction
_TX_BASE = RunTransaction.verified_base_paper_book_snapshot
_TX_TERMINAL = RunTransaction.verified_terminal_paper_book_snapshot
_TX_COMPLETED = RunTransaction._completed_registry_item
_TX_LEDGER = RunTransaction._verified_canonical_decision_ledger
_TX_REQUIRE_RUN = RunTransaction._require_run_decision_identity
_TX_DECODE = RunTransaction._decode_strict_json
_TX_BASE_CODE = getattr(_TX_BASE, "__code__", None)
_TX_TERMINAL_CODE = getattr(_TX_TERMINAL, "__code__", None)
_TX_COMPLETED_CODE = getattr(_TX_COMPLETED, "__code__", None)
_TX_LEDGER_CODE = getattr(_TX_LEDGER.__func__, "__code__", None)
_TX_REQUIRE_RUN_CODE = getattr(_TX_REQUIRE_RUN.__func__, "__code__", None)
_TX_DECODE_CODE = getattr(_TX_DECODE.__func__, "__code__", None)
_PRECOMMIT = resolve_fixed_n_iid_precommit_authority
_PRECOMMIT_CODE = getattr(_PRECOMMIT, "__code__", None)
_STRUCTURE = inspect_fixed_n_iid_sampling_structure
_STRUCTURE_CODE = getattr(_STRUCTURE, "__code__", None)
_DRAW_PLAN_TYPE = ProductIidExpectedDrawPlan
_DRAW_PLAN = resolve_product_iid_expected_draw_plan
_DRAW_PLAN_CODE = getattr(_DRAW_PLAN, "__code__", None)
_RUN_ADMISSION_TYPE = ProductIidRunAdmissionReceipt
_RUN_ADMISSION = resolve_product_iid_run_admission
_RUN_ADMISSION_CODE = getattr(_RUN_ADMISSION, "__code__", None)
_RUN_EXECUTION_TYPE = ProductIidRunExecutionReceipt
_RUN_EXECUTION = resolve_product_iid_run_execution
_RUN_EXECUTION_CODE = getattr(_RUN_EXECUTION, "__code__", None)
_EXPECTED_REPLAY_SEQUENCE = expected_replay_input_payload_sequence_sha256
_EXPECTED_REPLAY_SEQUENCE_CODE = getattr(_EXPECTED_REPLAY_SEQUENCE, "__code__", None)
_EXPECTED_REPLAY_MULTISET = expected_replay_consumed_payload_multiset_sha256
_EXPECTED_REPLAY_MULTISET_CODE = getattr(_EXPECTED_REPLAY_MULTISET, "__code__", None)
_REGISTRY_TYPE = RunRegistry
_REGISTRY_COMPLETED_SUMMARY = RunRegistry.verified_completed_summary_for_run
_REGISTRY_COMPLETED_SUMMARY_CODE = getattr(
    _REGISTRY_COMPLETED_SUMMARY,
    "__code__",
    None,
)


class ProductRunCapitalPathError(RuntimeError):
    """Product-owned run-capital occurrence cannot be re-resolved safely."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductRunCapitalPathError(
            "risk-path occurrence evidence is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ProductRunCapitalPathError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductRunCapitalPathError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ProductRunCapitalPathError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductRunCapitalPathError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductRunCapitalPathError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _decimal_text(value: Decimal, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ProductRunCapitalPathError(
            f"{name} must be a finite exact Decimal"
        )
    sign, digits, exponent = value.as_tuple()
    if type(exponent) is not int:
        raise ProductRunCapitalPathError(
            f"{name} Decimal exponent must be an integer"
        )
    sign_length = 1 if sign else 0
    digit_count = len(digits)
    if value.is_zero():
        length = sign_length + (1 if exponent >= 0 else 2 - exponent)
    elif exponent >= 0:
        length = sign_length + digit_count + exponent
    elif digit_count + exponent > 0:
        length = sign_length + digit_count + 1
    else:
        length = sign_length + 2 - exponent
    if length > _MAX_DECIMAL_TEXT:
        raise ProductRunCapitalPathError(
            f"{name} fixed-point representation exceeds supported evidence size"
        )
    return format(value, "f")


def _pairs(value: object, name: str) -> dict[str, str]:
    if type(value) is not tuple:
        raise ProductRunCapitalPathError(
            f"{name} must be a canonical tuple"
        )
    result: dict[str, str] = {}
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise ProductRunCapitalPathError(
                f"{name} must contain exact key/value pairs"
            )
        key, item_value = item
        key = _text(key, f"{name} key")
        item_value = _text(item_value, f"{name}[{key}]")
        if key in result:
            raise ProductRunCapitalPathError(
                f"{name} contains duplicate key {key!r}"
            )
        result[key] = item_value
    return result


def _opening_ticket_payload(ticket: PaperTicket) -> dict[str, object]:
    if type(ticket) is not _TICKET_TYPE:
        raise ProductRunCapitalPathError(
            "opening ticket evidence must be an exact PaperTicket"
        )
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


def _opening_ticket_sha256(ticket: PaperTicket) -> str:
    return _digest(_opening_ticket_payload(ticket))


def _changed_ticket_ids(
    base_book: PaperBook,
    final_book: PaperBook,
) -> tuple[str, ...]:
    if type(base_book) is not PaperBook or type(final_book) is not PaperBook:
        raise ProductRunCapitalPathError(
            "run snapshots must decode to exact PaperBook values"
        )
    base_ids = set(base_book.tickets)
    final_ids = set(final_book.tickets)
    if not base_ids.issubset(final_ids):
        raise ProductRunCapitalPathError(
            "terminal PaperBook removed a BASE ticket"
        )
    changed: list[str] = []
    for ticket_id in sorted(final_ids):
        current = final_book.tickets[ticket_id]
        prior = base_book.tickets.get(ticket_id)
        if prior is None or prior != current:
            changed.append(ticket_id)
    if not changed:
        raise ProductRunCapitalPathError(
            "no-effect runs lack product-owned outcome availability authority"
        )
    return tuple(changed)


def _conservative_minimum_equity(
    base_book: PaperBook,
    final_book: PaperBook,
) -> Decimal:
    """Lower-bound run equity without inventing event ordering.

    Every newly opened run ticket is debited before any settlement credit. This
    is intentionally at least as pessimistic as any valid PaperBook interleaving.
    If those aggregate debits cannot all precede credits, zero remains the
    conservative floor because canonical PaperBook never admits negative balance.
    """

    balance = base_book.balance
    for ticket_id in sorted(set(final_book.tickets) - set(base_book.tickets)):
        stake = final_book.tickets[ticket_id].stake
        if stake > balance:
            return Decimal("0")
        try:
            balance = _PAPER_DEBIT(balance, stake)
        except ValueError as exc:
            raise ProductRunCapitalPathError(
                "new-ticket debit is not canonical PaperBook arithmetic"
            ) from exc
    return balance


def _require_dispatch() -> None:
    if (
        DecisionRecord is not _DECISION_TYPE
        or verify_economic_goal_binding is not _VERIFY_GOAL
        or getattr(_VERIFY_GOAL, "__code__", None) is not _VERIFY_GOAL_CODE
        or PaperTicket is not _TICKET_TYPE
        or TicketStatus is not _TICKET_STATUS_TYPE
        or _TICKET_STATUS_TYPE.OPEN is not _OPEN_STATUS
        or PaperSettlementLearningWitness is not _WITNESS_TYPE
        or ResolvedFixedNRiskMembership is not _MEMBERSHIP_TYPE
        or PaperBook is not _PAPER_TYPE
        or _PAPER_TYPE.load_bytes.__func__ is not _PAPER_LOAD.__func__
        or getattr(_PAPER_LOAD.__func__, "__code__", None) is not _PAPER_LOAD_CODE
        or _PAPER_TYPE._debit_balance.__func__ is not _PAPER_DEBIT.__func__
        or getattr(_PAPER_DEBIT.__func__, "__code__", None) is not _PAPER_DEBIT_CODE
        or replay_paper_book_equity_path is not _REPLAY
        or getattr(_REPLAY, "__code__", None) is not _REPLAY_CODE
        or PaperSettlementLearningBridge is not _BRIDGE_TYPE
        or _BRIDGE_TYPE.resolution_witness is not _BRIDGE_WITNESS
        or getattr(_BRIDGE_WITNESS, "__code__", None)
        is not _BRIDGE_WITNESS_CODE
        or _BRIDGE_TYPE._decision_matches is not _BRIDGE_DECISION_MATCHES
        or getattr(_BRIDGE_DECISION_MATCHES, "__code__", None)
        is not _BRIDGE_DECISION_MATCHES_CODE
        or AgentLoopRuntime is not _LOOP_TYPE
        or _LOOP_TYPE._read is not _LOOP_READ
        or getattr(_LOOP_READ, "__code__", None) is not _LOOP_READ_CODE
        or RunTransaction is not _TX_TYPE
        or _TX_TYPE.verified_base_paper_book_snapshot is not _TX_BASE
        or _TX_TYPE.verified_terminal_paper_book_snapshot is not _TX_TERMINAL
        or _TX_TYPE._completed_registry_item is not _TX_COMPLETED
        or _TX_TYPE._verified_canonical_decision_ledger.__func__
        is not _TX_LEDGER.__func__
        or _TX_TYPE._require_run_decision_identity.__func__
        is not _TX_REQUIRE_RUN.__func__
        or _TX_TYPE._decode_strict_json.__func__ is not _TX_DECODE.__func__
        or getattr(_TX_BASE, "__code__", None) is not _TX_BASE_CODE
        or getattr(_TX_TERMINAL, "__code__", None) is not _TX_TERMINAL_CODE
        or getattr(_TX_COMPLETED, "__code__", None) is not _TX_COMPLETED_CODE
        or getattr(_TX_LEDGER.__func__, "__code__", None)
        is not _TX_LEDGER_CODE
        or getattr(_TX_REQUIRE_RUN.__func__, "__code__", None)
        is not _TX_REQUIRE_RUN_CODE
        or getattr(_TX_DECODE.__func__, "__code__", None) is not _TX_DECODE_CODE
        or resolve_fixed_n_iid_precommit_authority is not _PRECOMMIT
        or getattr(_PRECOMMIT, "__code__", None) is not _PRECOMMIT_CODE
        or inspect_fixed_n_iid_sampling_structure is not _STRUCTURE
        or getattr(_STRUCTURE, "__code__", None) is not _STRUCTURE_CODE
        or ProductIidExpectedDrawPlan is not _DRAW_PLAN_TYPE
        or resolve_product_iid_expected_draw_plan is not _DRAW_PLAN
        or getattr(_DRAW_PLAN, "__code__", None) is not _DRAW_PLAN_CODE
        or ProductIidRunAdmissionReceipt is not _RUN_ADMISSION_TYPE
        or resolve_product_iid_run_admission is not _RUN_ADMISSION
        or getattr(_RUN_ADMISSION, "__code__", None) is not _RUN_ADMISSION_CODE
        or ProductIidRunExecutionReceipt is not _RUN_EXECUTION_TYPE
        or resolve_product_iid_run_execution is not _RUN_EXECUTION
        or getattr(_RUN_EXECUTION, "__code__", None) is not _RUN_EXECUTION_CODE
        or expected_replay_input_payload_sequence_sha256
        is not _EXPECTED_REPLAY_SEQUENCE
        or getattr(_EXPECTED_REPLAY_SEQUENCE, "__code__", None)
        is not _EXPECTED_REPLAY_SEQUENCE_CODE
        or expected_replay_consumed_payload_multiset_sha256
        is not _EXPECTED_REPLAY_MULTISET
        or getattr(_EXPECTED_REPLAY_MULTISET, "__code__", None)
        is not _EXPECTED_REPLAY_MULTISET_CODE
        or RunRegistry is not _REGISTRY_TYPE
        or _REGISTRY_TYPE.verified_completed_summary_for_run
        is not _REGISTRY_COMPLETED_SUMMARY
        or getattr(_REGISTRY_COMPLETED_SUMMARY, "__code__", None)
        is not _REGISTRY_COMPLETED_SUMMARY_CODE
    ):
        raise ProductRunCapitalPathError(
            "risk-path occurrence authority dispatch changed"
        )


def _committed_run_decisions(
    tx: RunTransaction,
) -> dict[str, DecisionRecord]:
    completed = _TX_COMPLETED(tx)
    expected_new = _sha(
        completed.get("decision_ledger_sha256"),
        "completed decision_ledger_sha256",
    )
    staged = _TX_LEDGER(
        tx.staged_ledger_path,
        "retained combined Decision Ledger",
    )
    if staged.sha256 != expected_new:
        raise ProductRunCapitalPathError(
            "retained combined Decision Ledger differs from committed NEW identity"
        )
    run = _TX_LEDGER(
        tx.run_ledger_path,
        "retained run Decision Ledger",
    )
    _TX_REQUIRE_RUN(
        run,
        expected_run_id=tx.run_id,
        label="retained run Decision Ledger",
    )
    if not run.payload:
        raise ProductRunCapitalPathError(
            "completed risk-path run has no retained run decisions"
        )
    if not staged.payload.endswith(run.payload):
        raise ProductRunCapitalPathError(
            "retained run Decision Ledger is not the committed NEW ledger suffix"
        )

    decisions: dict[str, DecisionRecord] = {}
    for line_number, line in enumerate(
        run.payload.decode("utf-8").splitlines(),
        start=1,
    ):
        envelope = _TX_DECODE(
            line,
            label=f"retained run Decision Ledger line {line_number}",
        )
        raw = envelope.get("record") if type(envelope) is dict else None
        if type(raw) is not dict:
            raise ProductRunCapitalPathError(
                "retained run Decision Ledger record is invalid"
            )
        if raw.get("decision_kind") != ECONOMIC_DECISION_KIND:
            continue
        try:
            record = _DECISION_TYPE(
                replay_run_id=raw["replay_run_id"],
                agent=raw["agent"],
                observed_ts=raw["observed_ts"],
                action=raw["action"],
                payload=dict(raw["payload"]),
                context_hash=raw["context_hash"],
                decision_id=raw["decision_id"],
                recorded_at=raw["recorded_at"],
                decision_kind=ECONOMIC_DECISION_KIND,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductRunCapitalPathError(
                "retained economic run decision cannot be reconstructed"
            ) from exc
        if record.decision_id in decisions:
            raise ProductRunCapitalPathError(
                "retained run Decision Ledger has duplicate decision identity"
            )
        decisions[record.decision_id] = record
    if not decisions:
        raise ProductRunCapitalPathError(
            "completed risk-path run has no retained economic decisions"
        )
    return decisions


def _require_agent_loop_resolution(
    bridge: PaperSettlementLearningBridge,
    witness: PaperSettlementLearningWitness,
) -> None:
    loop = bridge.agent_loop
    if type(loop) is not _LOOP_TYPE:
        raise ProductRunCapitalPathError(
            "settlement witness AgentLoop is not canonical"
        )
    try:
        state = _LOOP_READ(loop)
    except (AgentLoopError, OSError, ValueError) as exc:
        raise ProductRunCapitalPathError(
            "settlement witness AgentLoop resolution cannot be re-resolved"
        ) from exc
    resolutions = state.get("resolutions") if type(state) is dict else None
    if type(resolutions) is not list:
        raise ProductRunCapitalPathError(
            "settlement witness AgentLoop resolution state is invalid"
        )
    matches = [
        item
        for item in resolutions
        if type(item) is dict
        and item.get("transition_id") == witness.transition.transition_id
    ]
    if len(matches) != 1:
        raise ProductRunCapitalPathError(
            "settlement witness lacks exact durable AgentLoop resolution"
        )
    item = matches[0]
    if (
        item.get("action_id") != witness.action.action_id
        or item.get("outcome_id") != witness.outcome.outcome_id
        or item.get("reward_id") != witness.reward.reward_id
        or item.get("truth") != witness.reward.truth.value
        or item.get("simulation_model_id") != witness.reward.simulation_model_id
        or item.get("reward_value") != str(witness.reward.reward)
        or _instant(
            item.get("reward_available_at"),
            "AgentLoop reward_available_at",
        )
        != _instant(
            witness.reward.available_at,
            "settlement reward available_at",
        )
    ):
        raise ProductRunCapitalPathError(
            "settlement witness differs from durable AgentLoop resolution"
        )

def _witness_effect(
    witness: PaperSettlementLearningWitness,
    *,
    ticket: PaperTicket,
    decisions: dict[str, DecisionRecord],
    bridge: PaperSettlementLearningBridge,
) -> dict[str, str]:
    if type(witness) is not _WITNESS_TYPE:
        raise ProductRunCapitalPathError(
            "settlement bridge returned unsupported witness type"
        )
    if witness.ticket_id != ticket.ticket_id:
        raise ProductRunCapitalPathError(
            "settlement witness ticket identity mismatch"
        )
    action = _pairs(witness.action.parameters, "settlement witness action parameters")
    decision_id = action.get("economic_decision_id")
    record = decisions.get(decision_id) if decision_id is not None else None
    if record is None:
        raise ProductRunCapitalPathError(
            "settlement witness decision is not in the committed run suffix"
        )
    try:
        _VERIFY_GOAL(
            record,
            bridge.economic_goal,
            risk_policy=bridge.risk_policy,
        )
        _BRIDGE_DECISION_MATCHES(
            record,
            ticket,
            witness.action,
            witness.observation,
        )
    except (
        DecisionLedgerIntegrityError,
        PaperSettlementLearningBridgeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductRunCapitalPathError(
            "settlement witness differs from committed economic decision"
        ) from exc
    _require_agent_loop_resolution(bridge, witness)
    if action.get("paper_ticket_id") != ticket.ticket_id:
        raise ProductRunCapitalPathError(
            "settlement witness action does not bind the exact PaperTicket"
        )

    outcome = _pairs(witness.outcome.evidence, "settlement witness outcome evidence")
    reward = _pairs(witness.reward.evidence, "settlement witness reward evidence")
    opening_sha = _opening_ticket_sha256(ticket)
    if outcome.get("decision_id") != decision_id:
        raise ProductRunCapitalPathError(
            "settlement outcome decision identity mismatch"
        )
    if outcome.get("ticket_id") != ticket.ticket_id:
        raise ProductRunCapitalPathError(
            "settlement outcome ticket identity mismatch"
        )
    if outcome.get("paper_ticket_sha256") != opening_sha:
        raise ProductRunCapitalPathError(
            "settlement witness opening-ticket digest mismatch"
        )
    if outcome.get("ticket_status") != ticket.status.value:
        raise ProductRunCapitalPathError(
            "settlement witness terminal status mismatch"
        )
    bundle = _sha(
        witness.settlement_bundle_sha256,
        "settlement_bundle_sha256",
    )
    if (
        outcome.get("settlement_bundle_sha256") != bundle
        or reward.get("settlement_bundle_sha256") != bundle
        or reward.get("ticket_id") != ticket.ticket_id
    ):
        raise ProductRunCapitalPathError(
            "settlement witness bundle identity mismatch"
        )
    available = _instant(
        witness.reward.available_at,
        "settlement reward available_at",
    )
    revealed = _instant(
        witness.outcome.revealed_at,
        "settlement outcome revealed_at",
    )
    resolved = _instant(
        witness.transition.resolved_at,
        "settlement transition resolved_at",
    )
    if available != revealed or resolved != revealed:
        raise ProductRunCapitalPathError(
            "settlement witness causal availability identities differ"
        )
    return {
        "action_id": _text(witness.action.action_id, "action_id"),
        "binding_id": _sha(witness.binding_id, "binding_id"),
        "decision_id": decision_id,
        "opening_ticket_sha256": opening_sha,
        "outcome_id": _text(witness.outcome.outcome_id, "outcome_id"),
        "reward_id": _text(witness.reward.reward_id, "reward_id"),
        "settlement_bundle_sha256": bundle,
        "transition_id": _text(
            witness.transition.transition_id,
            "transition_id",
        ),
        "available_at": available.isoformat(),
    }


@dataclass(frozen=True, slots=True, init=False)
class ProductRunCapitalPathEvidence:
    """Product-owned completed PAPER run-capital evidence.

    This proves exact run/transaction/ticket/settlement ancestry, a conservative
    minimum-equity floor, materialization of the frozen IID frame/horizon and the
    pre-run admission of the exact product-derived draw transcript. It deliberately
    does not prove that simulator execution consumed those draws, followed the
    precommitted stake policy, or therefore qualifies as IID evidence.
    """

    member_id: str
    member_index: int
    expected_stream_sha256: str
    base_snapshot_sha256: str
    final_snapshot_sha256: str
    changed_ticket_ids: tuple[str, ...]
    minimum_equity: Decimal
    outcome_available_at: str
    expected_draw_plan_sha256: str
    expected_draw_transcript_sha256: str
    run_admission_receipt_sha256: str
    run_execution_receipt_sha256: str
    expected_replay_input_event_payload_sequence_sha256: str
    completed_replay_input_event_payload_sequence_sha256: str
    expected_replay_consumed_event_payload_multiset_sha256: str
    completed_replay_consumed_event_payload_multiset_sha256: str
    settlement_effects_sha256: str
    replay_source_evidence_sha256: str
    source_evidence_sha256: str
    complete: bool = True

    def __new__(
        cls,
        *args: object,
        **kwargs: object,
    ) -> "ProductRunCapitalPathEvidence":
        raise TypeError(
            "ProductRunCapitalPathEvidence is product-issued; "
            "use resolve_product_run_capital_path_evidence"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ProductRunCapitalPathEvidence must not be subclassed")

    @property
    def product_precommit_bound(self) -> bool:
        return True

    @property
    def run_path_ancestry_proven(self) -> bool:
        return True

    @property
    def sampling_frame_materialized(self) -> bool:
        return True

    @property
    def expected_draw_product_derived(self) -> bool:
        return True

    @property
    def run_admission_bound(self) -> bool:
        return True

    @property
    def replay_input_binding_proven(self) -> bool:
        return True

    @property
    def execution_consumption_proven(self) -> bool:
        return True

    @property
    def sampling_occurrence_ancestry_proven(self) -> bool:
        return True

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


def resolve_product_run_capital_path_evidence(
    *,
    workspace: str | Path,
    run_id: str,
    member_index: int,
    membership: ResolvedFixedNRiskMembership,
    registry_path: str | Path,
    sampling_manifest_json: str,
    sampling_frame_json: str,
    horizon_json: str,
    settlement_bridge: PaperSettlementLearningBridge,
    authority_root: str | Path | None = None,
) -> ProductRunCapitalPathEvidence:
    """Re-resolve one completed fixed-N member from product-owned durable evidence."""

    _require_dispatch()
    run_id = _text(run_id, "run_id")
    if type(member_index) is not int or member_index < 0:
        raise ProductRunCapitalPathError(
            "member_index must be a non-negative exact integer"
        )
    if type(membership) is not _MEMBERSHIP_TYPE:
        raise TypeError("membership must be exact ResolvedFixedNRiskMembership")
    try:
        precommit = _PRECOMMIT(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            authority_root=authority_root,
        )
        structure = _STRUCTURE(
            membership,
            sampling_manifest_json=sampling_manifest_json,
        )
        draw_plan = _DRAW_PLAN(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            authority_root=authority_root,
        )
        admission = _RUN_ADMISSION(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            member_index=member_index,
            authority_root=authority_root,
        )
        execution = _RUN_EXECUTION(
            membership,
            registry_path=registry_path,
            workspace=workspace,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            member_index=member_index,
            authority_root=authority_root,
        )
    except (
        ProductIidDrawPlanError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise ProductRunCapitalPathError(
            "product fixed-N precommit authority cannot be re-resolved"
        ) from exc
    if (
        precommit.product_membership_preoutcome_chronology_proven is not True
        or precommit.product_randomization_root_issued is not True
        or precommit.occurrence_ancestry_proven is not False
        or precommit.iid_qualified is not False
        or precommit.planned_member_ids != structure.planned_member_ids
        or precommit.sampling_manifest_sha256 != structure.manifest_sha256
        or type(draw_plan) is not _DRAW_PLAN_TYPE
        or draw_plan.product_precommit_bound is not True
        or draw_plan.sampling_frame_materialized is not True
        or draw_plan.expected_draws_product_derived is not True
        or draw_plan.occurrence_ancestry_proven is not False
        or draw_plan.iid_qualified is not False
        or draw_plan.sampling_manifest_sha256 != structure.manifest_sha256
        or draw_plan.sampling_frame_sha256 != structure.sampling_frame_sha256
        or draw_plan.horizon_sha256 != structure.horizon_sha256
        or len(draw_plan.member_draws) != structure.planned_n
        or type(admission) is not _RUN_ADMISSION_TYPE
        or admission.run_admission_bound is not True
        or admission.product_precommit_bound is not True
        or admission.execution_consumption_proven is not False
        or admission.occurrence_ancestry_proven is not False
        or admission.iid_qualified is not False
        or admission.grants_real_money_authority is not False
        or admission.member_id != run_id
        or type(admission.member_index) is not int
        or admission.member_index != member_index
        or admission.expected_draw_plan_sha256 != draw_plan.plan_sha256
        or admission.sampling_manifest_sha256 != structure.manifest_sha256
        or admission.sampling_frame_sha256 != structure.sampling_frame_sha256
        or admission.horizon_sha256 != structure.horizon_sha256
        or type(execution) is not _RUN_EXECUTION_TYPE
        or execution.product_precommit_bound is not True
        or execution.run_admission_bound is not True
        or execution.execution_consumption_proven is not True
        or execution.occurrence_ancestry_proven is not True
        or execution.iid_qualified is not False
        or execution.grants_real_money_authority is not False
        or execution.member_id != run_id
        or execution.member_index != member_index
        or execution.expected_draw_plan_sha256 != draw_plan.plan_sha256
        or execution.run_admission_receipt_sha256 != admission.receipt_sha256
    ):
        raise ProductRunCapitalPathError(
            "product fixed-N precommit truth boundary is inconsistent"
        )
    if member_index >= structure.planned_n:
        raise ProductRunCapitalPathError(
            "member_index is outside the frozen IID membership"
        )
    if structure.planned_member_ids[member_index] != run_id:
        raise ProductRunCapitalPathError(
            "run_id is not the frozen member at member_index"
        )
    expected_draw = draw_plan.member_draws[member_index]
    if (
        expected_draw.member_id != run_id
        or type(expected_draw.member_index) is not int
        or expected_draw.member_index != member_index
        or expected_draw.stream_sha256 != structure.member_stream_sha256[member_index]
        or expected_draw.execution_consumption_proven is not False
        or expected_draw.grants_real_money_authority is not False
        or admission.stream_sha256 != expected_draw.stream_sha256
        or admission.expected_draw_transcript_sha256
        != expected_draw.draw_transcript_sha256
    ):
        raise ProductRunCapitalPathError(
            "expected IID draw does not bind the exact fixed-N member"
        )
    try:
        expected_replay_input_sha256 = _EXPECTED_REPLAY_SEQUENCE(expected_draw)
        expected_replay_multiset_sha256 = _EXPECTED_REPLAY_MULTISET(expected_draw)
    except ProductIidDrawPlanError as exc:
        raise ProductRunCapitalPathError(
            "expected IID draw replay identity cannot be derived"
        ) from exc
    _require_dispatch()
    if type(settlement_bridge) is not _BRIDGE_TYPE:
        raise TypeError(
            "settlement_bridge must be exact PaperSettlementLearningBridge"
        )

    root = Path(workspace).expanduser().resolve(strict=False)
    try:
        completed_summary, _completed_summary_sha256 = _REGISTRY_COMPLETED_SUMMARY(
            _REGISTRY_TYPE(root / "run_registry.json"),
            run_id,
        )
    except (ReconciliationError, OSError, ValueError, KeyError) as exc:
        raise ProductRunCapitalPathError(
            "completed replay payload evidence cannot be re-resolved"
        ) from exc
    _require_dispatch()
    completed_replay_input_sha256 = _sha(
        completed_summary.get("replay_input_event_payload_sequence_sha256"),
        "completed replay input event payload sequence sha256",
    )
    completed_replay_multiset_sha256 = _sha(
        completed_summary.get("replay_consumed_event_payload_multiset_sha256"),
        "completed replay consumed event payload multiset sha256",
    )
    completed_event_count = completed_summary.get("event_count")
    if (
        type(completed_event_count) is not int
        or completed_event_count != expected_draw.draw_count
        or completed_replay_input_sha256 != expected_replay_input_sha256
        or completed_replay_multiset_sha256 != expected_replay_multiset_sha256
    ):
        raise ProductRunCapitalPathError(
            "completed replay input does not bind the frozen IID draw payloads"
        )
    if Path(settlement_bridge.state_path).parent.resolve(strict=False) != root:
        raise ProductRunCapitalPathError(
            "settlement bridge state belongs to another workspace"
        )
    if Path(settlement_bridge.paper_book_path).resolve(strict=False) != (
        root / "paper_book.json"
    ).resolve(strict=False):
        raise ProductRunCapitalPathError(
            "settlement bridge PaperBook belongs to another workspace"
        )
    if Path(settlement_bridge.agent_loop.path).parent.resolve(strict=False) != root:
        raise ProductRunCapitalPathError(
            "settlement bridge AgentLoop belongs to another workspace"
        )

    tx = _TX_TYPE(root, run_id)
    if Path(settlement_bridge.decision_ledger.path).resolve(strict=False) != (
        tx.run_ledger_path.resolve(strict=False)
    ):
        raise ProductRunCapitalPathError(
            "settlement bridge Decision Ledger is not this run ledger"
        )
    try:
        base = _TX_BASE(tx)
        final = _TX_TERMINAL(tx)
        decisions = _committed_run_decisions(tx)
        base_book = _PAPER_LOAD(base.payload)
        final_book = _PAPER_LOAD(final.payload)
        changed = _changed_ticket_ids(base_book, final_book)
        if any(ticket_id in base_book.tickets for ticket_id in changed):
            raise ProductRunCapitalPathError(
                "run-capital path authority v1 supports only tickets opened "
                "and completed inside the exact run"
            )
        replay = _REPLAY(
            base.payload,
            final.payload,
            expected_changed_ticket_ids=frozenset(changed),
        )
    except (
        RunTransactionError,
        RiskPathEquityReplayError,
        OSError,
        ValueError,
    ) as exc:
        raise ProductRunCapitalPathError(
            "completed run-capital evidence cannot be re-resolved"
        ) from exc

    effects: list[dict[str, str]] = []
    for ticket_id in changed:
        ticket = final_book.tickets[ticket_id]
        if ticket.status is _OPEN_STATUS:
            raise ProductRunCapitalPathError(
                "open run ticket has no complete settlement occurrence authority"
            )
        try:
            witness = _BRIDGE_WITNESS(settlement_bridge, ticket_id)
        except (
            PaperSettlementLearningBridgeError,
            OSError,
            ValueError,
        ) as exc:
            raise ProductRunCapitalPathError(
                "changed run ticket lacks durable settlement-learning authority"
            ) from exc
        effects.append(
            _witness_effect(
                witness,
                ticket=ticket,
                decisions=decisions,
                bridge=settlement_bridge,
            )
        )

    minimum = _conservative_minimum_equity(base_book, final_book)
    if minimum > replay.minimum_equity:
        raise ProductRunCapitalPathError(
            "conservative run-capital floor exceeds chronological replay minimum"
        )
    latest = max(
        (_instant(item["available_at"], "effect available_at") for item in effects),
    ).isoformat()
    stream = _sha(
        structure.member_stream_sha256[member_index],
        "member_stream_sha256",
    )
    effects_payload = {
        "schema": _EFFECTS_SCHEMA,
        "member_id": run_id,
        "member_index": member_index,
        "expected_stream_sha256": stream,
        "base_snapshot_sha256": base.sha256,
        "final_snapshot_sha256": final.sha256,
        "changed_ticket_ids": list(changed),
        "settlement_effects": effects,
    }
    effects_sha = _digest(effects_payload)
    source_payload = {
        "schema": _SCHEMA,
        "member_id": run_id,
        "member_index": member_index,
        "expected_stream_sha256": stream,
        "base_snapshot_sha256": base.sha256,
        "final_snapshot_sha256": final.sha256,
        "changed_ticket_ids": list(changed),
        "minimum_equity": _decimal_text(minimum, "minimum_equity"),
        "outcome_available_at": latest,
        "expected_draw_plan_sha256": _sha(
            draw_plan.plan_sha256,
            "expected_draw_plan_sha256",
        ),
        "expected_draw_transcript_sha256": _sha(
            expected_draw.draw_transcript_sha256,
            "expected_draw_transcript_sha256",
        ),
        "run_admission_receipt_sha256": _sha(
            admission.receipt_sha256,
            "run_admission_receipt_sha256",
        ),
        "run_execution_receipt_sha256": _sha(
            execution.receipt_sha256,
            "run_execution_receipt_sha256",
        ),
        "expected_replay_input_event_payload_sequence_sha256": (
            expected_replay_input_sha256
        ),
        "completed_replay_input_event_payload_sequence_sha256": (
            completed_replay_input_sha256
        ),
        "expected_replay_consumed_event_payload_multiset_sha256": (
            expected_replay_multiset_sha256
        ),
        "completed_replay_consumed_event_payload_multiset_sha256": (
            completed_replay_multiset_sha256
        ),
        "settlement_effects_sha256": effects_sha,
        "replay_source_evidence_sha256": replay.source_evidence_sha256,
        "product_precommit_bound": True,
        "run_path_ancestry_proven": True,
        "sampling_frame_materialized": True,
        "expected_draw_product_derived": True,
        "run_admission_bound": True,
        "replay_input_binding_proven": True,
        "execution_consumption_proven": True,
        "sampling_occurrence_ancestry_proven": True,
        "iid_qualified": False,
        "grants_real_money_authority": False,
    }
    # Do not expose a public constructor for positive run-path ancestry.  This object
    # is a projection of the durable roots re-resolved above, not caller-mintable
    # bearer authority.  Bypass the rejecting public __new__ only at this exact
    # resolver issuance point.
    result = object.__new__(ProductRunCapitalPathEvidence)
    for field_name, value in (
        ("member_id", run_id),
        ("member_index", member_index),
        ("expected_stream_sha256", stream),
        ("base_snapshot_sha256", _sha(base.sha256, "base_snapshot_sha256")),
        ("final_snapshot_sha256", _sha(final.sha256, "final_snapshot_sha256")),
        ("changed_ticket_ids", changed),
        ("minimum_equity", minimum),
        ("outcome_available_at", latest),
        (
            "expected_draw_plan_sha256",
            _sha(draw_plan.plan_sha256, "expected_draw_plan_sha256"),
        ),
        (
            "expected_draw_transcript_sha256",
            _sha(
                expected_draw.draw_transcript_sha256,
                "expected_draw_transcript_sha256",
            ),
        ),
        (
            "run_admission_receipt_sha256",
            _sha(admission.receipt_sha256, "run_admission_receipt_sha256"),
        ),
        (
            "run_execution_receipt_sha256",
            _sha(execution.receipt_sha256, "run_execution_receipt_sha256"),
        ),
        (
            "expected_replay_input_event_payload_sequence_sha256",
            expected_replay_input_sha256,
        ),
        (
            "completed_replay_input_event_payload_sequence_sha256",
            completed_replay_input_sha256,
        ),
        (
            "expected_replay_consumed_event_payload_multiset_sha256",
            expected_replay_multiset_sha256,
        ),
        (
            "completed_replay_consumed_event_payload_multiset_sha256",
            completed_replay_multiset_sha256,
        ),
        ("settlement_effects_sha256", effects_sha),
        (
            "replay_source_evidence_sha256",
            _sha(
                replay.source_evidence_sha256,
                "replay_source_evidence_sha256",
            ),
        ),
        ("source_evidence_sha256", _digest(source_payload)),
        ("complete", True),
    ):
        object.__setattr__(result, field_name, value)
    _require_dispatch()
    return result


def _seal_product_run_capital_path_resolver() -> None:
    """Seal the public positive resolver to its exact result type and implementation."""

    module_globals = globals()
    evidence_type = ProductRunCapitalPathEvidence
    error_type = ProductRunCapitalPathError
    original_resolver = resolve_product_run_capital_path_evidence
    original_code = original_resolver.__code__

    def sealed_resolver(
        *,
        workspace: str | Path,
        run_id: str,
        member_index: int,
        membership: ResolvedFixedNRiskMembership,
        registry_path: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        settlement_bridge: PaperSettlementLearningBridge,
        authority_root: str | Path | None = None,
    ) -> ProductRunCapitalPathEvidence:
        if (
            module_globals.get("resolve_product_run_capital_path_evidence")
            is not sealed_resolver
            or module_globals.get("ProductRunCapitalPathEvidence")
            is not evidence_type
            or original_resolver.__code__ is not original_code
        ):
            raise error_type(
                "run-capital evidence resolver authority dispatch changed"
            )
        result = original_resolver(
            workspace=workspace,
            run_id=run_id,
            member_index=member_index,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            settlement_bridge=settlement_bridge,
            authority_root=authority_root,
        )
        if (
            module_globals.get("resolve_product_run_capital_path_evidence")
            is not sealed_resolver
            or module_globals.get("ProductRunCapitalPathEvidence")
            is not evidence_type
            or original_resolver.__code__ is not original_code
            or type(result) is not evidence_type
        ):
            raise error_type(
                "run-capital evidence resolver authority dispatch changed"
            )
        return result

    sealed_resolver.__name__ = original_resolver.__name__
    sealed_resolver.__qualname__ = original_resolver.__qualname__
    sealed_resolver.__doc__ = original_resolver.__doc__
    sealed_resolver.__module__ = original_resolver.__module__
    sealed_resolver.__annotations__ = dict(original_resolver.__annotations__)
    module_globals["resolve_product_run_capital_path_evidence"] = sealed_resolver


_seal_product_run_capital_path_resolver()
del _seal_product_run_capital_path_resolver


def _build_product_run_capital_path_evidence_verifier(
    resolver,
    evidence_type: type[ProductRunCapitalPathEvidence],
):
    """Freeze the only resolver/type graph a verifier may trust."""

    module_globals = globals()
    resolver_code = getattr(resolver, "__code__", None)
    field_names = (
        "member_id",
        "member_index",
        "expected_stream_sha256",
        "base_snapshot_sha256",
        "final_snapshot_sha256",
        "changed_ticket_ids",
        "minimum_equity",
        "outcome_available_at",
        "expected_draw_plan_sha256",
        "expected_draw_transcript_sha256",
        "run_admission_receipt_sha256",
        "run_execution_receipt_sha256",
        "expected_replay_input_event_payload_sequence_sha256",
        "completed_replay_input_event_payload_sequence_sha256",
        "expected_replay_consumed_event_payload_multiset_sha256",
        "completed_replay_consumed_event_payload_multiset_sha256",
        "settlement_effects_sha256",
        "replay_source_evidence_sha256",
        "source_evidence_sha256",
        "complete",
    )

    def verifier(
        candidate: ProductRunCapitalPathEvidence,
        *,
        workspace: str | Path,
        run_id: str,
        member_index: int,
        membership: ResolvedFixedNRiskMembership,
        registry_path: str | Path,
        sampling_manifest_json: str,
        sampling_frame_json: str,
        horizon_json: str,
        settlement_bridge: PaperSettlementLearningBridge,
        authority_root: str | Path | None = None,
    ) -> ProductRunCapitalPathEvidence:
        if (
            module_globals.get("resolve_product_run_capital_path_evidence")
            is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get("ProductRunCapitalPathEvidence")
            is not evidence_type
        ):
            raise ProductRunCapitalPathError(
                "run-capital evidence verifier authority dispatch changed"
            )
        if type(candidate) is not evidence_type:
            raise TypeError(
                "candidate must be an exact ProductRunCapitalPathEvidence"
            )
        canonical = resolver(
            workspace=workspace,
            run_id=run_id,
            member_index=member_index,
            membership=membership,
            registry_path=registry_path,
            sampling_manifest_json=sampling_manifest_json,
            sampling_frame_json=sampling_frame_json,
            horizon_json=horizon_json,
            settlement_bridge=settlement_bridge,
            authority_root=authority_root,
        )
        if (
            module_globals.get("resolve_product_run_capital_path_evidence")
            is not resolver
            or getattr(resolver, "__code__", None) is not resolver_code
            or module_globals.get("ProductRunCapitalPathEvidence")
            is not evidence_type
        ):
            raise ProductRunCapitalPathError(
                "run-capital evidence verifier authority dispatch changed"
            )
        for field_name in field_names:
            supplied = getattr(candidate, field_name)
            expected = getattr(canonical, field_name)
            if type(supplied) is not type(expected) or supplied != expected:
                raise ProductRunCapitalPathError(
                    "run-capital evidence does not match canonical durable roots"
                )
        return canonical

    verifier.__name__ = "verify_product_run_capital_path_evidence"
    verifier.__qualname__ = "verify_product_run_capital_path_evidence"
    return verifier


verify_product_run_capital_path_evidence = (
    _build_product_run_capital_path_evidence_verifier(
        resolve_product_run_capital_path_evidence,
        ProductRunCapitalPathEvidence,
    )
)
del _build_product_run_capital_path_evidence_verifier

__all__ = [
    "ProductRunCapitalPathEvidence",
    "ProductRunCapitalPathError",
    "resolve_product_run_capital_path_evidence",
    "verify_product_run_capital_path_evidence",
]
