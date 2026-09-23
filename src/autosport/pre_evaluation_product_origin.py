"""Product-owned origin gate for pre-evaluation semantic authority.

The structural semantic derivation in :mod:`pre_evaluation_semantics` intentionally
accepts typed domain objects.  Typed objects alone are not provenance: a caller can
construct a mutually consistent object graph in memory.  This module adds the
production-facing origin gate.  Positive pre-evaluation semantics are admissible only
when the same inputs can be re-resolved from three independent product-owned roots:

* a runtime-issued complete provider board;
* the durable economic Decision Ledger record for the exact portfolio plan/intents;
* a durable product cost-contract selection bound to the exact economic Decision Ledger record.

The returned wrapper is deliberately a distinct type.  Downstream denominator code
must consume ``ProductOwnedPreEvaluationSemanticSession`` rather than treating a bare
``PreEvaluationSemanticSession`` as product-origin proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Iterable, Mapping
import weakref

from .decision_ledger import DecisionRecord, JsonlDecisionLedger
from .paper import PaperBook
from .portfolio_plan import OpportunityIntent, PortfolioDependencyGraph
from .pre_evaluation_binding import BoundPreEvaluationSession
from .pre_evaluation_semantics import (
    PreEvaluationCostContract,
    PreEvaluationSemanticAuthority,
    PreEvaluationSemanticSession,
    ProviderSelectionBinding,
)
from .provider_observation_authority import (
    CompleteGameBoardSnapshot,
    assert_complete_game_board_authoritative,
)
from .risk import PaperRiskPolicy


_SCHEMA = "autosport.pre_evaluation_product_origin"
_SCHEMA_VERSION = 1
_COST_SCHEMA = "autosport.pre_evaluation_cost_contract"
_COST_SELECTION_SCHEMA = "autosport.pre_evaluation_cost_contract_selection"
_COST_SELECTION_SCHEMA_VERSION = 2
_COST_SELECTION_AGENT = "pre-evaluation-cost-contract"
_COST_SELECTION_ACTION = "SELECT_PRE_EVALUATION_COST_CONTRACT"
_COST_SELECTION_MATERIAL_ACTION_KEY = "pre_evaluation_material_action_id"
_COST_SELECTION_CONTRACT_KEY = "cost_contract"
_COST_SELECTION_PREDECESSOR_SHA_KEY = "selection_predecessor_prefix_sha256"
_COST_SELECTION_PREDECESSOR_COUNT_KEY = "selection_predecessor_record_count"
_COST_SELECTION_AVAILABLE_COUNT_KEY = "selection_available_record_count"
_HEX = frozenset("0123456789abcdef")
_ISSUED: dict[int, tuple[weakref.ReferenceType["PreEvaluationProductOrigin"], str]] = {}


class PreEvaluationProductOriginError(RuntimeError):
    """Product-owned provenance cannot be re-resolved exactly."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PreEvaluationProductOriginError(
            "product-origin evidence must be finite canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise PreEvaluationProductOriginError(f"{name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _sha(name: str, value: object) -> str:
    raw = _text(name, value)
    if len(raw) != 64 or raw != raw.lower() or any(ch not in _HEX for ch in raw):
        raise PreEvaluationProductOriginError(
            f"{name} must be canonical lowercase SHA-256 hex"
        )
    return raw


def _load_json_object(name: str, raw: object) -> Mapping[str, object]:
    if type(raw) is not str:
        raise PreEvaluationProductOriginError(f"{name} must be canonical JSON text")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreEvaluationProductOriginError(f"{name} is not valid JSON") from exc
    if type(parsed) is not dict:
        raise PreEvaluationProductOriginError(f"{name} must contain one JSON object")
    if _canonical_json(parsed) != raw:
        raise PreEvaluationProductOriginError(f"{name} must be canonical JSON")
    return parsed


def _cost_contract_payload(contract: PreEvaluationCostContract) -> dict[str, object]:
    if not isinstance(contract, PreEvaluationCostContract):
        raise TypeError("contract must be PreEvaluationCostContract")
    return {
        "schema": _COST_SCHEMA,
        "schema_version": 1,
        "contract_id": contract.contract_id,
        "max_cost_micros": contract.max_cost_micros,
        "contract_sha256": contract.contract_sha256,
    }


def _cost_contract_from_payload(raw: object) -> PreEvaluationCostContract:
    expected = {
        "schema",
        "schema_version",
        "contract_id",
        "max_cost_micros",
        "contract_sha256",
    }
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract authority has unexpected fields"
        )
    if raw["schema"] != _COST_SCHEMA or raw["schema_version"] != 1:
        raise PreEvaluationProductOriginError(
            "unsupported durable product cost-contract authority schema"
        )
    try:
        contract = PreEvaluationCostContract(
            contract_id=raw["contract_id"],
            max_cost_micros=raw["max_cost_micros"],
        )
    except (TypeError, ValueError) as exc:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract authority is invalid"
        ) from exc
    if raw["contract_sha256"] != contract.contract_sha256:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract digest does not bind exact configuration"
        )
    return contract


@dataclass(frozen=True, slots=True)
class _CostSelectionRecord:
    record: DecisionRecord
    record_count: int
    predecessor_prefix_sha256: str


def _verified_ledger_records_with_prefixes(
    ledger: JsonlDecisionLedger,
) -> tuple[_CostSelectionRecord, ...]:
    try:
        snapshot = ledger.verified_snapshot()
    except Exception as exc:
        raise PreEvaluationProductOriginError(
            "durable Decision Ledger cannot be verified"
        ) from exc
    lines = snapshot.payload.splitlines(keepends=True)
    if len(lines) != snapshot.record_count:
        raise PreEvaluationProductOriginError(
            "durable Decision Ledger record count is inconsistent"
        )
    prefix = bytearray()
    records: list[_CostSelectionRecord] = []
    try:
        for record_count, line in enumerate(lines, start=1):
            predecessor_sha256 = sha256(bytes(prefix)).hexdigest()
            envelope = json.loads(line.decode("utf-8"))
            record = DecisionRecord(**envelope["record"])
            records.append(
                _CostSelectionRecord(
                    record=record,
                    record_count=record_count,
                    predecessor_prefix_sha256=predecessor_sha256,
                )
            )
            prefix.extend(line)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreEvaluationProductOriginError(
            "durable Decision Ledger snapshot cannot be replayed"
        ) from exc
    if sha256(bytes(prefix)).hexdigest() != snapshot.sha256:
        raise PreEvaluationProductOriginError(
            "durable Decision Ledger prefix digest is inconsistent"
        )
    return tuple(records)


def _cost_selection_payload(
    *,
    material_action_id: str,
    economic_record: DecisionRecord,
    contract: PreEvaluationCostContract,
    predecessor_prefix_sha256: str,
    predecessor_record_count: int,
) -> dict[str, object]:
    predecessor_prefix_sha256 = _sha(
        _COST_SELECTION_PREDECESSOR_SHA_KEY, predecessor_prefix_sha256
    )
    if type(predecessor_record_count) is not int or predecessor_record_count < 0:
        raise PreEvaluationProductOriginError(
            "selection predecessor record count must be a non-negative integer"
        )
    return {
        "schema": _COST_SELECTION_SCHEMA,
        "schema_version": _COST_SELECTION_SCHEMA_VERSION,
        _COST_SELECTION_MATERIAL_ACTION_KEY: _text(
            _COST_SELECTION_MATERIAL_ACTION_KEY, material_action_id
        ),
        "economic_decision_id": _text(
            "economic_decision_id", economic_record.decision_id
        ),
        "economic_decision_context_hash": _sha(
            "economic_decision_context_hash", economic_record.context_hash
        ),
        _COST_SELECTION_PREDECESSOR_SHA_KEY: predecessor_prefix_sha256,
        _COST_SELECTION_PREDECESSOR_COUNT_KEY: predecessor_record_count,
        _COST_SELECTION_AVAILABLE_COUNT_KEY: predecessor_record_count + 1,
        _COST_SELECTION_CONTRACT_KEY: _cost_contract_payload(contract),
    }


def _cost_selection_records(
    ledger: JsonlDecisionLedger,
    *,
    material_action_id: str,
) -> tuple[_CostSelectionRecord, ...]:
    material_action_id = _text("material_action_id", material_action_id)
    matches: list[_CostSelectionRecord] = []
    expected_fields = {
        "schema",
        "schema_version",
        _COST_SELECTION_MATERIAL_ACTION_KEY,
        "economic_decision_id",
        "economic_decision_context_hash",
        _COST_SELECTION_PREDECESSOR_SHA_KEY,
        _COST_SELECTION_PREDECESSOR_COUNT_KEY,
        _COST_SELECTION_AVAILABLE_COUNT_KEY,
        _COST_SELECTION_CONTRACT_KEY,
    }
    for located in _verified_ledger_records_with_prefixes(ledger):
        record = located.record
        payload = record.payload
        is_selection = (
            record.agent == _COST_SELECTION_AGENT
            or record.action == _COST_SELECTION_ACTION
            or payload.get("schema") == _COST_SELECTION_SCHEMA
        )
        if not is_selection:
            continue
        if (
            record.agent != _COST_SELECTION_AGENT
            or record.action != _COST_SELECTION_ACTION
            or set(payload) != expected_fields
            or payload.get("schema") != _COST_SELECTION_SCHEMA
            or payload.get("schema_version") != _COST_SELECTION_SCHEMA_VERSION
        ):
            raise PreEvaluationProductOriginError(
                "durable product cost-contract selection record is malformed"
            )
        predecessor_count = payload.get(_COST_SELECTION_PREDECESSOR_COUNT_KEY)
        available_count = payload.get(_COST_SELECTION_AVAILABLE_COUNT_KEY)
        predecessor_sha256 = payload.get(_COST_SELECTION_PREDECESSOR_SHA_KEY)
        if (
            type(predecessor_count) is not int
            or predecessor_count < 0
            or type(available_count) is not int
            or available_count < 1
            or predecessor_count != located.record_count - 1
            or available_count != located.record_count
            or _sha(_COST_SELECTION_PREDECESSOR_SHA_KEY, predecessor_sha256)
            != located.predecessor_prefix_sha256
        ):
            raise PreEvaluationProductOriginError(
                "durable product cost-contract selection append-order fence mismatch"
            )
        selected_action = _text(
            _COST_SELECTION_MATERIAL_ACTION_KEY,
            payload.get(_COST_SELECTION_MATERIAL_ACTION_KEY),
        )
        if selected_action == material_action_id:
            matches.append(located)
    if len(matches) > 1:
        raise PreEvaluationProductOriginError(
            "multiple durable product cost-contract selections exist for material action"
        )
    return tuple(matches)


def persist_pre_evaluation_cost_contract_authority(
    *,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    risk_policy: PaperRiskPolicy,
    contract: PreEvaluationCostContract,
) -> DecisionRecord:
    """Bind one immutable cost contract to an existing durable economic decision.

    Selection is append-only and keyed by the same material action as the canonical
    PortfolioPlan decision.  Its payload also binds the exact verified ledger prefix
    that existed immediately before the append, so restart resolution cannot backdate
    later selection availability by copying an older economic-decision timestamp.
    """

    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise TypeError("risk_policy must be PaperRiskPolicy")
    if not isinstance(contract, PreEvaluationCostContract):
        raise TypeError("contract must be PreEvaluationCostContract")
    material_action_id = _text("material_action_id", material_action_id)
    goal = risk_policy.economic_goal
    if goal is None:
        raise PreEvaluationProductOriginError(
            "cost-contract selection requires canonical EconomicGoal authority"
        )
    try:
        economic_record = ledger.verified_economic_decision_for_material_action(
            material_action_id,
            goal,
            risk_policy=risk_policy,
        )
    except Exception as exc:
        raise PreEvaluationProductOriginError(
            "durable economic decision cannot authorize cost-contract selection"
        ) from exc
    if economic_record is None:
        raise PreEvaluationProductOriginError(
            "cost-contract selection requires an existing durable economic decision"
        )

    existing = _cost_selection_records(
        ledger,
        material_action_id=material_action_id,
    )
    if existing:
        located = existing[0]
        record = located.record
        selected = _cost_contract_from_payload(
            record.payload.get(_COST_SELECTION_CONTRACT_KEY)
        )
        expected_payload = _cost_selection_payload(
            material_action_id=material_action_id,
            economic_record=economic_record,
            contract=contract,
            predecessor_prefix_sha256=located.predecessor_prefix_sha256,
            predecessor_record_count=located.record_count - 1,
        )
        if (
            record.context_hash != _digest(expected_payload)
            or record.payload.get("economic_decision_id") != economic_record.decision_id
            or record.payload.get("economic_decision_context_hash")
            != economic_record.context_hash
            or selected != contract
        ):
            raise PreEvaluationProductOriginError(
                "durable product cost-contract selection conflicts with requested contract"
            )
        return record

    prefix = ledger.verified_snapshot()
    expected_payload = _cost_selection_payload(
        material_action_id=material_action_id,
        economic_record=economic_record,
        contract=contract,
        predecessor_prefix_sha256=prefix.sha256,
        predecessor_record_count=prefix.record_count,
    )
    expected_context_hash = _digest(expected_payload)
    decision_id = f"pre-eval-cost:{expected_context_hash[:32]}"
    record = DecisionRecord(
        replay_run_id=economic_record.replay_run_id,
        agent=_COST_SELECTION_AGENT,
        observed_ts=economic_record.observed_ts,
        action=_COST_SELECTION_ACTION,
        payload=expected_payload,
        context_hash=expected_context_hash,
        decision_id=decision_id,
    )
    try:
        ledger.append(record)
    except Exception as exc:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract selection could not be persisted"
        ) from exc
    written = _cost_selection_records(
        ledger,
        material_action_id=material_action_id,
    )
    if len(written) != 1 or written[0].record.decision_id != decision_id:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract selection append could not be re-resolved"
        )
    return written[0].record


def _validated_bound_ledger_prefix(
    *,
    ledger: JsonlDecisionLedger,
    bound: BoundPreEvaluationSession,
) -> int:
    prefix_sha256 = bound.decision_ledger_prefix_sha256
    prefix_record_count = bound.decision_ledger_prefix_record_count
    if prefix_sha256 is None or prefix_record_count is None:
        raise PreEvaluationProductOriginError(
            "pre-evaluation freeze does not bind a durable Decision Ledger prefix"
        )
    try:
        snapshot = ledger.verified_snapshot()
    except Exception as exc:
        raise PreEvaluationProductOriginError(
            "pre-evaluation freeze Decision Ledger cannot be verified"
        ) from exc
    if prefix_record_count > snapshot.record_count:
        raise PreEvaluationProductOriginError(
            "pre-evaluation freeze ledger prefix is longer than durable ledger"
        )
    lines = snapshot.payload.splitlines(keepends=True)
    prefix_payload = b"".join(lines[:prefix_record_count])
    if sha256(prefix_payload).hexdigest() != prefix_sha256:
        raise PreEvaluationProductOriginError(
            "pre-evaluation freeze ledger prefix no longer matches durable history"
        )
    return prefix_record_count


def _validated_durable_cost_contract(
    *,
    ledger: JsonlDecisionLedger,
    bound: BoundPreEvaluationSession,
    material_action_id: str,
    economic_record: DecisionRecord,
    expected_cost_contract_sha256: str | None,
) -> PreEvaluationCostContract:
    matches = _cost_selection_records(
        ledger,
        material_action_id=material_action_id,
    )
    if not matches:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract selection is absent"
        )
    located = matches[0]
    record = located.record
    contract = _cost_contract_from_payload(
        record.payload.get(_COST_SELECTION_CONTRACT_KEY)
    )
    expected_payload = _cost_selection_payload(
        material_action_id=material_action_id,
        economic_record=economic_record,
        contract=contract,
        predecessor_prefix_sha256=located.predecessor_prefix_sha256,
        predecessor_record_count=located.record_count - 1,
    )
    if (
        record.context_hash != _digest(expected_payload)
        or record.payload.get("economic_decision_id") != economic_record.decision_id
        or record.payload.get("economic_decision_context_hash")
        != economic_record.context_hash
    ):
        raise PreEvaluationProductOriginError(
            "durable product cost-contract selection is detached from economic decision"
        )
    freeze_record_count = _validated_bound_ledger_prefix(ledger=ledger, bound=bound)
    if located.record_count > freeze_record_count:
        raise PreEvaluationProductOriginError(
            "durable product cost-contract selection was not durable before pre-evaluation freeze"
        )
    if expected_cost_contract_sha256 is not None:
        expected_digest = _sha(
            "expected_cost_contract_sha256", expected_cost_contract_sha256
        )
        if expected_digest != contract.contract_sha256:
            raise PreEvaluationProductOriginError(
                "expected cost contract does not match durable product selection"
            )
    return contract


def _selection_labels(market_key: str) -> tuple[str, str]:
    if market_key in {"h2h", "spreads"}:
        return ("home", "away")
    if market_key == "totals":
        return ("over", "under")
    raise PreEvaluationProductOriginError(
        "complete provider board contains an unsupported market"
    )


def _provider_selection_set(
    snapshot: CompleteGameBoardSnapshot,
) -> set[tuple[str, str, str]]:
    frame = snapshot.frame
    rows = frame.get("data")
    if not isinstance(rows, list):
        raise PreEvaluationProductOriginError(
            "authoritative complete provider board has invalid data"
        )
    selections: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise PreEvaluationProductOriginError(
                "authoritative complete provider board row is invalid"
            )
        event_id = _text("provider event_id", row.get("event_id"))
        bookmaker = _text("provider bookmaker", row.get("bookmaker"))
        market_key = _text("provider market_key", row.get("market_key"))
        market_id = f"{bookmaker}:{market_key}"
        for label in _selection_labels(market_key):
            selections.add((event_id, market_id, f"{market_id}:{label}"))
    return selections


def _provider_bindings_payload(
    bindings: tuple[ProviderSelectionBinding, ...],
) -> list[dict[str, object]]:
    return [binding.authority_payload() for binding in bindings]


def _validate_provider_origin(
    *,
    snapshot: CompleteGameBoardSnapshot,
    bound: BoundPreEvaluationSession,
    provider_selections: tuple[ProviderSelectionBinding, ...],
) -> None:
    try:
        assert_complete_game_board_authoritative(snapshot)
    except Exception as exc:
        raise PreEvaluationProductOriginError(
            "provider origin is not a runtime-issued complete-board authority"
        ) from exc
    if bound.context.provider_evidence_sha256 != snapshot.evidence_sha256:
        raise PreEvaluationProductOriginError(
            "denominator context does not bind the authoritative provider snapshot"
        )
    expected_member_ids = {
        member.row_key: member.member_sha256 for member in bound.members
    }
    actual_member_ids = {
        binding.row_key: binding.member_sha256 for binding in provider_selections
    }
    if actual_member_ids != expected_member_ids:
        raise PreEvaluationProductOriginError(
            "provider bindings do not equal denominator-bound member identities"
        )
    if len(actual_member_ids) != len(provider_selections):
        raise PreEvaluationProductOriginError("provider bindings contain duplicate row keys")

    available = _provider_selection_set(snapshot)
    if not available:
        if len(provider_selections) != 1 or provider_selections[0].event_id is not None:
            raise PreEvaluationProductOriginError(
                "empty complete provider board requires one explicit empty member"
            )
    for binding in provider_selections:
        if binding.source_id != snapshot.request.source_id:
            raise PreEvaluationProductOriginError(
                "provider binding source_id is not owned by the complete-board origin"
            )
        if binding.event_id is None:
            if available:
                raise PreEvaluationProductOriginError(
                    "non-empty complete provider board cannot authorize an empty member"
                )
            continue
        selection = (binding.event_id, binding.market_id, binding.selection_id)
        if selection not in available:
            raise PreEvaluationProductOriginError(
                "provider binding selection is absent from the authoritative complete board"
            )


def _validated_durable_plan(
    *,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    dependency_graph: PortfolioDependencyGraph,
) -> tuple[DecisionRecord, str]:
    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")
    material_action_id = _text("material_action_id", material_action_id)
    goal = risk_policy.economic_goal
    if goal is None:
        raise PreEvaluationProductOriginError(
            "positive product origin requires canonical EconomicGoal authority"
        )
    try:
        record = ledger.verified_economic_decision_for_material_action(
            material_action_id,
            goal,
            risk_policy=risk_policy,
        )
    except Exception as exc:
        raise PreEvaluationProductOriginError(
            "durable product decision cannot be verified"
        ) from exc
    if record is None:
        raise PreEvaluationProductOriginError(
            "durable product decision is absent for material action"
        )

    payload = record.payload
    if not isinstance(payload, Mapping):
        raise PreEvaluationProductOriginError("durable product decision payload is invalid")
    plan = _load_json_object("durable portfolio_plan_json", payload.get("portfolio_plan_json"))
    intent_evidence = _load_json_object(
        "durable portfolio_intent_evidence_json",
        payload.get("portfolio_intent_evidence_json"),
    )
    if plan.get("schema") != "autosport.portfolio_plan":
        raise PreEvaluationProductOriginError("durable decision is not a PortfolioPlan")
    if intent_evidence.get("schema") != "autosport.portfolio_plan_intent_evidence":
        raise PreEvaluationProductOriginError(
            "durable decision lacks canonical intent evidence"
        )

    portfolio_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
    if portfolio_sha256 is None:
        raise PreEvaluationProductOriginError(
            "current PaperBook portfolio identity cannot be proven"
        )
    if plan.get("portfolio_sha256") != portfolio_sha256:
        raise PreEvaluationProductOriginError(
            "durable PortfolioPlan does not bind the exact current portfolio"
        )
    if plan.get("risk_policy_sha256") != risk_policy.provenance_sha256:
        raise PreEvaluationProductOriginError(
            "durable PortfolioPlan does not bind the exact risk authority"
        )
    if plan.get("dependency_graph_sha256") != dependency_graph.graph_sha256:
        raise PreEvaluationProductOriginError(
            "durable PortfolioPlan does not bind the exact dependency graph"
        )
    intent_sha256s = tuple(intent.intent_sha256 for intent in intents)
    raw_intent_sha256s = plan.get("intent_sha256s")
    if type(raw_intent_sha256s) is not list or tuple(raw_intent_sha256s) != intent_sha256s:
        raise PreEvaluationProductOriginError(
            "durable PortfolioPlan does not bind the exact intent vector"
        )
    durable_intents = intent_evidence.get("intents")
    if type(durable_intents) is not list:
        raise PreEvaluationProductOriginError(
            "durable product intent evidence must be a list"
        )
    expected_intents = [intent.audit_payload() for intent in intents]
    if durable_intents != expected_intents:
        raise PreEvaluationProductOriginError(
            "caller intents do not equal durable product-owned intent evidence"
        )
    return record, portfolio_sha256


@dataclass(frozen=True, slots=True, weakref_slot=True)
class PreEvaluationProductOrigin:
    """Opaque exact-input capability issued only after durable re-resolution."""

    bound_authority_digest: str
    denominator_context_digest: str
    provider_evidence_sha256: str
    provider_bindings_sha256: str
    intent_sha256s: tuple[str, ...]
    durable_decision_context_hash: str
    risk_policy_sha256: str
    portfolio_sha256: str
    dependency_graph_sha256: str
    cost_contract_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "bound_authority_digest",
            "denominator_context_digest",
            "provider_evidence_sha256",
            "provider_bindings_sha256",
            "durable_decision_context_hash",
            "risk_policy_sha256",
            "portfolio_sha256",
            "dependency_graph_sha256",
            "cost_contract_sha256",
        ):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        if type(self.intent_sha256s) is not tuple:
            raise PreEvaluationProductOriginError("intent_sha256s must be a tuple")
        for digest in self.intent_sha256s:
            _sha("intent_sha256", digest)
        if len(self.intent_sha256s) != len(set(self.intent_sha256s)):
            raise PreEvaluationProductOriginError("intent_sha256s must be unique")

    @property
    def origin_digest(self) -> str:
        return _digest(
            {
                "schema": _SCHEMA,
                "schema_version": _SCHEMA_VERSION,
                "bound_authority_digest": self.bound_authority_digest,
                "denominator_context_digest": self.denominator_context_digest,
                "provider_evidence_sha256": self.provider_evidence_sha256,
                "provider_bindings_sha256": self.provider_bindings_sha256,
                "intent_sha256s": list(self.intent_sha256s),
                "durable_decision_context_hash": self.durable_decision_context_hash,
                "risk_policy_sha256": self.risk_policy_sha256,
                "portfolio_sha256": self.portfolio_sha256,
                "dependency_graph_sha256": self.dependency_graph_sha256,
                "cost_contract_sha256": self.cost_contract_sha256,
            }
        )


def _forget(origin_id: int, reference: weakref.ReferenceType[PreEvaluationProductOrigin]) -> None:
    current = _ISSUED.get(origin_id)
    if current is not None and current[0] is reference:
        _ISSUED.pop(origin_id, None)


def _remember(origin: PreEvaluationProductOrigin) -> PreEvaluationProductOrigin:
    origin_id = id(origin)
    reference = weakref.ref(
        origin,
        lambda current, origin_id=origin_id: _forget(origin_id, current),
    )
    _ISSUED[origin_id] = (reference, origin.origin_digest)
    return origin


def assert_pre_evaluation_product_origin_authoritative(
    origin: PreEvaluationProductOrigin,
) -> None:
    if not isinstance(origin, PreEvaluationProductOrigin):
        raise PreEvaluationProductOriginError(
            "pre-evaluation origin requires PreEvaluationProductOrigin"
        )
    issued = _ISSUED.get(id(origin))
    if issued is None or issued[0]() is not origin or issued[1] != origin.origin_digest:
        raise PreEvaluationProductOriginError(
            "pre-evaluation origin was not issued by durable product re-resolution"
        )


def resolve_pre_evaluation_product_origin(
    *,
    snapshot: CompleteGameBoardSnapshot,
    bound: BoundPreEvaluationSession,
    provider_selections: Iterable[ProviderSelectionBinding],
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    dependency_graph: PortfolioDependencyGraph,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    expected_cost_contract_sha256: str | None = None,
) -> tuple[PreEvaluationProductOrigin, PreEvaluationCostContract]:
    """Re-resolve every positive semantic input from durable product-owned roots."""

    if not isinstance(bound, BoundPreEvaluationSession):
        raise TypeError("bound must be BoundPreEvaluationSession")
    if type(intents) is not tuple or any(
        not isinstance(intent, OpportunityIntent) for intent in intents
    ):
        raise TypeError("intents must be a tuple of OpportunityIntent values")
    if not isinstance(risk_policy, PaperRiskPolicy):
        raise TypeError("risk_policy must be PaperRiskPolicy")
    if not isinstance(book, PaperBook):
        raise TypeError("book must be PaperBook")
    if not isinstance(dependency_graph, PortfolioDependencyGraph):
        raise TypeError("dependency_graph must be PortfolioDependencyGraph")
    providers = tuple(provider_selections)
    if any(not isinstance(item, ProviderSelectionBinding) for item in providers):
        raise TypeError("provider_selections must contain ProviderSelectionBinding values")
    providers = tuple(sorted(providers, key=lambda item: item.row_key))

    _validate_provider_origin(
        snapshot=snapshot,
        bound=bound,
        provider_selections=providers,
    )
    decision_record, portfolio_sha256 = _validated_durable_plan(
        ledger=ledger,
        material_action_id=material_action_id,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
    )
    cost_contract = _validated_durable_cost_contract(
        ledger=ledger,
        bound=bound,
        material_action_id=material_action_id,
        economic_record=decision_record,
        expected_cost_contract_sha256=expected_cost_contract_sha256,
    )
    bound_digest = PreEvaluationSemanticAuthority._semantic_bound_authority_digest(bound)
    provider_bindings_sha256 = _digest(_provider_bindings_payload(providers))
    origin = PreEvaluationProductOrigin(
        bound_authority_digest=bound_digest,
        denominator_context_digest=bound.context.digest,
        provider_evidence_sha256=snapshot.evidence_sha256,
        provider_bindings_sha256=provider_bindings_sha256,
        intent_sha256s=tuple(intent.intent_sha256 for intent in intents),
        durable_decision_context_hash=_sha(
            "durable_decision_context_hash", decision_record.context_hash
        ),
        risk_policy_sha256=risk_policy.provenance_sha256,
        portfolio_sha256=portfolio_sha256,
        dependency_graph_sha256=dependency_graph.graph_sha256,
        cost_contract_sha256=cost_contract.contract_sha256,
    )
    return _remember(origin), cost_contract


@dataclass(frozen=True, slots=True)
class ProductOwnedPreEvaluationSemanticSession:
    """Semantic session plus independently re-resolved product-origin capability."""

    session: PreEvaluationSemanticSession
    origin: PreEvaluationProductOrigin

    def __post_init__(self) -> None:
        if not isinstance(self.session, PreEvaluationSemanticSession):
            raise TypeError("session must be PreEvaluationSemanticSession")
        assert_pre_evaluation_product_origin_authoritative(self.origin)
        if self.session.bound_authority_digest != self.origin.bound_authority_digest:
            raise PreEvaluationProductOriginError("semantic session changed bound authority")
        if self.session.denominator_context_digest != self.origin.denominator_context_digest:
            raise PreEvaluationProductOriginError("semantic session changed denominator context")
        if self.session.provider_bindings_sha256 != self.origin.provider_bindings_sha256:
            raise PreEvaluationProductOriginError("semantic session changed provider bindings")
        if self.session.risk_policy_sha256 != self.origin.risk_policy_sha256:
            raise PreEvaluationProductOriginError("semantic session changed risk authority")
        if self.session.portfolio_sha256 != self.origin.portfolio_sha256:
            raise PreEvaluationProductOriginError("semantic session changed portfolio authority")
        if self.session.dependency_graph_sha256 != self.origin.dependency_graph_sha256:
            raise PreEvaluationProductOriginError("semantic session changed dependency authority")
        if self.session.cost_contract_sha256 != self.origin.cost_contract_sha256:
            raise PreEvaluationProductOriginError("semantic session changed cost authority")

    @property
    def authority_digest(self) -> str:
        return _digest(
            {
                "schema": "autosport.product_owned_pre_evaluation_semantics",
                "schema_version": 1,
                "origin_digest": self.origin.origin_digest,
                "semantic_authority_digest": self.session.authority_digest,
            }
        )

    @property
    def authority_id(self) -> str:
        return f"product-pre-evaluation-semantics:{self.authority_digest[:32]}"

    @property
    def slots(self):
        return self.session.slots

    def resolve_slot(self, row_key: str):
        return self.session.resolve_slot(row_key)


def derive_product_owned_pre_evaluation_session(
    *,
    snapshot: CompleteGameBoardSnapshot,
    bound: BoundPreEvaluationSession,
    provider_selections: Iterable[ProviderSelectionBinding],
    intents: tuple[OpportunityIntent, ...],
    risk_policy: PaperRiskPolicy,
    book: PaperBook,
    dependency_graph: PortfolioDependencyGraph,
    ledger: JsonlDecisionLedger,
    material_action_id: str,
    expected_cost_contract_sha256: str | None = None,
) -> ProductOwnedPreEvaluationSemanticSession:
    """Production entrypoint: re-resolve origin first, then derive exact semantics."""

    providers = tuple(provider_selections)
    origin, cost_contract = resolve_pre_evaluation_product_origin(
        snapshot=snapshot,
        bound=bound,
        provider_selections=providers,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
        ledger=ledger,
        material_action_id=material_action_id,
        expected_cost_contract_sha256=expected_cost_contract_sha256,
    )
    session = PreEvaluationSemanticAuthority(cost_contract).derive_session(
        bound=bound,
        provider_selections=providers,
        intents=intents,
        risk_policy=risk_policy,
        book=book,
        dependency_graph=dependency_graph,
    )
    return ProductOwnedPreEvaluationSemanticSession(session=session, origin=origin)
