from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .decision_ledger import (
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
    RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from .economic_goal import EconomicGoalContract
from .economic_goal_store import EconomicGoalContractError, EconomicGoalStore
from .integrity import ensure_durable_file
from .market_implied_baseline import (
    MarketImpliedBaselineError,
    build_market_implied_baseline_evidence,
)
from .market_outcomes import MarketSettlementOutcomeAuthority
from .proposal_risk_target_authority import (
    ProductProposalRiskTarget,
    ProductProposalRiskTargetError,
    resolve_product_proposal_risk_target,
)
from .risk import PaperRiskPolicy
from .storage import SQLiteMarketStore
from .workspace_lock import WorkspaceEconomicLock


_SCHEMA = "autosport.proposal-risk-outcome-input-mapping.v1"
_ACTION = "PROPOSAL_RISK_OUTCOME_INPUT_MAPPING"
_AGENT = "autosport.proposal-risk-outcome-input-mapping-authority.v1"
_ACTION_PREFIX = "proposal-risk-outcome-input-mapping-v1:"
_HEX = frozenset("0123456789abcdef")
_PATH_TYPE = type(Path("."))

_TARGET_TYPE = ProductProposalRiskTarget
_TARGET_RESOLVER = resolve_product_proposal_risk_target
_TARGET_RESOLVER_CODE = getattr(_TARGET_RESOLVER, "__code__", None)
_BASELINE_BUILD = build_market_implied_baseline_evidence
_BASELINE_BUILD_CODE = getattr(_BASELINE_BUILD, "__code__", None)
_OUTCOME_TYPE = MarketSettlementOutcomeAuthority
_STORE_TYPE = SQLiteMarketStore
_GOAL_TYPE = EconomicGoalContract
_GOAL_STORE_TYPE = EconomicGoalStore
_GOAL_LOAD = EconomicGoalStore.load
_GOAL_LOAD_CODE = getattr(_GOAL_LOAD, "__code__", None)
_POLICY_TYPE = PaperRiskPolicy
_LEDGER_TYPE = JsonlDecisionLedger
_LEDGER_APPEND = JsonlDecisionLedger.append_economic
_LEDGER_APPEND_CODE = getattr(_LEDGER_APPEND, "__code__", None)
_LEDGER_RESOLVE = JsonlDecisionLedger.verified_economic_decision_for_material_action
_LEDGER_RESOLVE_CODE = getattr(_LEDGER_RESOLVE, "__code__", None)
_LEDGER_VERIFY = JsonlDecisionLedger.verify_integrity
_LEDGER_VERIFY_CODE = getattr(_LEDGER_VERIFY, "__code__", None)
_RECORD_TYPE = DecisionRecord
_LOCK_TYPE = WorkspaceEconomicLock
_ENSURE_DURABLE_FILE = ensure_durable_file
_ENSURE_DURABLE_FILE_CODE = getattr(_ENSURE_DURABLE_FILE, "__code__", None)
_JSON_DUMPS = json.dumps
_JSON_LOADS = json.loads
_HASHLIB_SHA256 = hashlib.sha256


class ProductProposalRiskOutcomeInputMappingError(RuntimeError):
    """Exact proposal markets cannot be mapped to canonical decision-time inputs."""


@dataclass(frozen=True, slots=True)
class ProposalRiskMarketInput:
    """Non-authorizing handle to canonical market state used to rebuild one input row."""

    store: SQLiteMarketStore
    outcome_authority: MarketSettlementOutcomeAuthority
    max_age: timedelta

    def __post_init__(self) -> None:
        if type(self.store) is not _STORE_TYPE:
            raise TypeError("store must be exact SQLiteMarketStore")
        if type(self.outcome_authority) is not _OUTCOME_TYPE:
            raise TypeError(
                "outcome_authority must be exact MarketSettlementOutcomeAuthority"
            )
        if type(self.max_age) is not timedelta or self.max_age < timedelta(0):
            raise TypeError("max_age must be a non-negative exact timedelta")


def _make_identity_capability():
    token = object()

    def proven(instance: object) -> bool:
        return getattr(instance, "_outcome_input_mapping_capability", None) is token

    def bind(instance: object) -> None:
        object.__setattr__(instance, "_outcome_input_mapping_capability", token)

    return proven, bind


_IDENTITY_PROVEN, _BIND_IDENTITY = _make_identity_capability()
del _make_identity_capability


@dataclass(frozen=True, slots=True, init=False)
class ProductProposalRiskOutcomeInputMapping:
    """Durable target-to-market-input mapping with deliberately narrow truth.

    This authority proves only that exact proposal quote identities were covered by
    product-built decision-time market-implied rows tied to typed terminal-outcome
    inputs. It does not qualify a joint/dependence model and therefore cannot prove
    target counterfactual execution or issue a target risk-of-ruin bound.
    """

    workspace_instance_id: str
    decision_id: str
    target_sha256: str
    target_decision_ts: str
    candidate_vector_sha256: str
    candidate_context_sha256s: tuple[str, ...]
    market_identity_json: tuple[str, ...]
    outcome_authority_sha256s: tuple[str, ...]
    baseline_evidence_sha256s: tuple[str, ...]
    probability_vector_json: tuple[str, ...]
    mapping_sha256: str
    _outcome_input_mapping_capability: object = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "ProductProposalRiskOutcomeInputMapping":
        raise TypeError(
            "ProductProposalRiskOutcomeInputMapping is product-issued; use "
            "issue_product_proposal_risk_outcome_input_mapping or "
            "resolve_product_proposal_risk_outcome_input_mapping"
        )

    @property
    def mapping_identity_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def exact_target_market_coverage_proven(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def decision_time_market_inputs_rebuilt(self, _proven=_IDENTITY_PROVEN) -> bool:
        return _proven(self)

    @property
    def provider_outcome_origin_independently_proven(self) -> bool:
        return False

    @property
    def joint_probability_model_proven(self) -> bool:
        return False

    @property
    def proposal_target_counterfactual_execution_proven(self) -> bool:
        return False

    @property
    def risk_upper_bound_for_target(self) -> bool:
        return False

    @property
    def proposal_target_risk_qualified(self) -> bool:
        return False

    @property
    def grants_risk_approval_authority(self) -> bool:
        return False

    @property
    def grants_ticket_authority(self) -> bool:
        return False

    @property
    def grants_broker_execution_authority(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False

    @property
    def grants_state_mutation_authority(self) -> bool:
        return False


_RESULT_FIELDS = (
    "workspace_instance_id",
    "decision_id",
    "target_sha256",
    "target_decision_ts",
    "candidate_vector_sha256",
    "candidate_context_sha256s",
    "market_identity_json",
    "outcome_authority_sha256s",
    "baseline_evidence_sha256s",
    "probability_vector_json",
    "mapping_sha256",
)
_RESULT_FIELDS_EXPECTED = _RESULT_FIELDS


def _text(value: object, name: str, *, max_length: int = 1024) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProductProposalRiskOutcomeInputMappingError(
            f"{name} must be non-empty canonical text"
        )
    if (
        len(value) > max_length
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or any(ord(ch) < 32 for ch in value)
    ):
        raise ProductProposalRiskOutcomeInputMappingError(
            f"{name} contains unsupported characters"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            f"{name} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if (
        len(text) != 64
        or text != text.lower()
        or any(ch not in _HEX for ch in text)
    ):
        raise ProductProposalRiskOutcomeInputMappingError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            f"{name} must be valid ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProductProposalRiskOutcomeInputMappingError(
            f"{name} must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_json_text(value: object) -> str:
    try:
        return _JSON_DUMPS(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            "outcome input mapping is outside canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return _HASHLIB_SHA256(_canonical_json_text(value).encode("utf-8")).hexdigest()


def _workspace_path(value: object) -> Path:
    if type(value) is not _PATH_TYPE or not value.is_absolute():
        raise ProductProposalRiskOutcomeInputMappingError(
            "workspace must be an exact absolute pathlib.Path"
        )
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            "workspace must resolve to an existing canonical directory"
        ) from exc
    if resolved != value or not value.is_dir() or value.is_symlink():
        raise ProductProposalRiskOutcomeInputMappingError(
            "workspace must be the canonical non-symlink directory path"
        )
    return value


def _require_dispatch() -> None:
    if (
        ProductProposalRiskTarget is not _TARGET_TYPE
        or resolve_product_proposal_risk_target is not _TARGET_RESOLVER
        or getattr(_TARGET_RESOLVER, "__code__", None) is not _TARGET_RESOLVER_CODE
        or build_market_implied_baseline_evidence is not _BASELINE_BUILD
        or getattr(_BASELINE_BUILD, "__code__", None) is not _BASELINE_BUILD_CODE
        or MarketSettlementOutcomeAuthority is not _OUTCOME_TYPE
        or SQLiteMarketStore is not _STORE_TYPE
        or EconomicGoalContract is not _GOAL_TYPE
        or EconomicGoalStore is not _GOAL_STORE_TYPE
        or EconomicGoalStore.load is not _GOAL_LOAD
        or getattr(_GOAL_LOAD, "__code__", None) is not _GOAL_LOAD_CODE
        or PaperRiskPolicy is not _POLICY_TYPE
        or JsonlDecisionLedger is not _LEDGER_TYPE
        or JsonlDecisionLedger.append_economic is not _LEDGER_APPEND
        or getattr(_LEDGER_APPEND, "__code__", None) is not _LEDGER_APPEND_CODE
        or JsonlDecisionLedger.verified_economic_decision_for_material_action
        is not _LEDGER_RESOLVE
        or getattr(_LEDGER_RESOLVE, "__code__", None) is not _LEDGER_RESOLVE_CODE
        or JsonlDecisionLedger.verify_integrity is not _LEDGER_VERIFY
        or getattr(_LEDGER_VERIFY, "__code__", None) is not _LEDGER_VERIFY_CODE
        or DecisionRecord is not _RECORD_TYPE
        or WorkspaceEconomicLock is not _LOCK_TYPE
        or ensure_durable_file is not _ENSURE_DURABLE_FILE
        or getattr(_ENSURE_DURABLE_FILE, "__code__", None)
        is not _ENSURE_DURABLE_FILE_CODE
        or json.dumps is not _JSON_DUMPS
        or json.loads is not _JSON_LOADS
        or hashlib.sha256 is not _HASHLIB_SHA256
        or _RESULT_FIELDS is not _RESULT_FIELDS_EXPECTED
    ):
        raise ProductProposalRiskOutcomeInputMappingError(
            "proposal risk outcome-input mapping dispatch authority changed"
        )


def _current_economic_state(
    workspace: Path,
) -> tuple[EconomicGoalContract, PaperRiskPolicy, JsonlDecisionLedger]:
    try:
        goal = _GOAL_LOAD(_GOAL_STORE_TYPE(workspace))
    except (EconomicGoalContractError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            "current product EconomicGoal cannot be resolved"
        ) from exc
    if type(goal) is not _GOAL_TYPE:
        raise ProductProposalRiskOutcomeInputMappingError(
            "current product EconomicGoal has non-canonical type"
        )
    policy = _POLICY_TYPE(economic_goal=goal)
    if type(policy) is not _POLICY_TYPE:
        raise ProductProposalRiskOutcomeInputMappingError(
            "current PaperRiskPolicy has non-canonical type"
        )
    ledger = _LEDGER_TYPE(workspace / "decisions.jsonl")
    try:
        _ENSURE_DURABLE_FILE(ledger.path)
        _LEDGER_VERIFY(ledger)
    except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            "canonical Decision Ledger cannot be verified"
        ) from exc
    return goal, policy, ledger


def _resolve_target(workspace: Path, target_sha256: str) -> ProductProposalRiskTarget:
    try:
        target = _TARGET_RESOLVER(workspace, target_sha256)
    except (
        ProductProposalRiskTargetError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProductProposalRiskOutcomeInputMappingError(
            "proposal target cannot be re-resolved"
        ) from exc
    _require_dispatch()
    if type(target) is not _TARGET_TYPE or not target.proposal_target_identity_proven:
        raise ProductProposalRiskOutcomeInputMappingError(
            "proposal target identity is not product-proven"
        )
    if (
        target.proposal_target_counterfactual_execution_proven
        or target.risk_upper_bound_for_target
        or target.grants_ticket_authority
        or target.grants_real_money_authority
    ):
        raise ProductProposalRiskOutcomeInputMappingError(
            "proposal target authority boundary is inconsistent"
        )
    return target


def _target_market_requirements(
    target: ProductProposalRiskTarget,
) -> tuple[tuple[tuple[str, str, str, str, str], tuple[str, ...]], ...]:
    requirements: dict[tuple[str, str, str, str, str], set[str]] = {}
    for context_index, raw_text in enumerate(target.candidate_context_json):
        if type(raw_text) is not str or not raw_text:
            raise ProductProposalRiskOutcomeInputMappingError(
                f"candidate_context_json[{context_index}] is invalid"
            )
        try:
            raw = _JSON_LOADS(raw_text)
        except (TypeError, ValueError) as exc:
            raise ProductProposalRiskOutcomeInputMappingError(
                f"candidate_context_json[{context_index}] is not valid JSON"
            ) from exc
        if type(raw) is not dict or _canonical_json_text(raw) != raw_text:
            raise ProductProposalRiskOutcomeInputMappingError(
                f"candidate_context_json[{context_index}] is not canonical JSON"
            )
        legs = raw.get("legs")
        quotes = raw.get("quotes")
        if type(legs) is not list or not legs or type(quotes) is not list or not quotes:
            raise ProductProposalRiskOutcomeInputMappingError(
                f"candidate_context_json[{context_index}] lacks canonical legs/quotes"
            )
        for leg_index, leg in enumerate(legs):
            if type(leg) is not dict:
                raise ProductProposalRiskOutcomeInputMappingError(
                    f"candidate leg {context_index}:{leg_index} is invalid"
                )
            matching = [
                quote
                for quote in quotes
                if type(quote) is dict
                and quote.get("event_id") == leg.get("event_id")
                and quote.get("market_id") == leg.get("market_id")
                and quote.get("selection_id") == leg.get("selection_id")
                and quote.get("sport") == leg.get("sport")
            ]
            if not matching:
                raise ProductProposalRiskOutcomeInputMappingError(
                    "proposal target leg has no exact quote identity"
                )
            for quote in matching:
                sport = _text(quote.get("sport"), "quote sport")
                event_id = _text(quote.get("event_id"), "quote event_id")
                market_id = _text(quote.get("market_id"), "quote market_id")
                selection_id = _text(quote.get("selection_id"), "quote selection_id")
                source_id = _text(quote.get("source_id"), "quote source_id")
                market_type = _text(quote.get("market_type"), "quote market_type")
                if market_type != "winner":
                    raise ProductProposalRiskOutcomeInputMappingError(
                        "proposal risk outcome mapping currently supports winner markets only"
                    )
                key = (sport, event_id, market_id, source_id, market_type)
                requirements.setdefault(key, set()).add(selection_id)
    if not requirements:
        raise ProductProposalRiskOutcomeInputMappingError(
            "proposal target contains no mappable market identities"
        )
    return tuple(
        (key, tuple(sorted(selections)))
        for key, selections in sorted(requirements.items())
    )


def _market_rows(
    target: ProductProposalRiskTarget,
    inputs: tuple[ProposalRiskMarketInput, ...],
) -> tuple[dict[str, object], ...]:
    if type(inputs) is not tuple or not inputs:
        raise ProductProposalRiskOutcomeInputMappingError(
            "market_inputs must be a non-empty exact tuple"
        )
    if any(type(item) is not ProposalRiskMarketInput for item in inputs):
        raise ProductProposalRiskOutcomeInputMappingError(
            "market_inputs must contain exact ProposalRiskMarketInput values"
        )
    requirements = dict(_target_market_requirements(target))
    expected_keys = set(requirements)
    supplied: dict[tuple[str, str, str, str, str], ProposalRiskMarketInput] = {}
    for item in inputs:
        identity = item.outcome_authority.identity
        key = identity.identity_key
        if key in supplied:
            raise ProductProposalRiskOutcomeInputMappingError(
                "market_inputs contain duplicate market authority identity"
            )
        supplied[key] = item
    if set(supplied) != expected_keys:
        missing = sorted(expected_keys.difference(supplied))
        extra = sorted(set(supplied).difference(expected_keys))
        raise ProductProposalRiskOutcomeInputMappingError(
            f"market_inputs must exactly cover target markets; missing={missing!r} extra={extra!r}"
        )

    decision_time = _instant(target.decision_ts, "target_decision_ts")
    rows: list[dict[str, object]] = []
    for ordinal, key in enumerate(sorted(expected_keys)):
        item = supplied[key]
        authority = item.outcome_authority
        selected = requirements[key]
        if not set(selected).issubset(set(authority.selection_ids)):
            raise ProductProposalRiskOutcomeInputMappingError(
                "target selection is absent from terminal-outcome roster"
            )
        cohort_key = (
            "proposal-risk-target:"
            + target.target_sha256[:16]
            + ":"
            + str(ordinal)
        )
        try:
            evidence = _BASELINE_BUILD(
                cohort_key=cohort_key,
                store=item.store,
                outcome_authority=authority,
                decision_cutoff=decision_time,
                max_age=item.max_age,
            )
        except (MarketImpliedBaselineError, OSError, TypeError, ValueError) as exc:
            raise ProductProposalRiskOutcomeInputMappingError(
                "decision-time market-implied evidence cannot be rebuilt"
            ) from exc
        evidence_key = (
            evidence.sport,
            evidence.event_id,
            evidence.market_id,
            evidence.source_id,
            evidence.market_type,
        )
        if evidence_key != key:
            raise ProductProposalRiskOutcomeInputMappingError(
                "market-implied evidence identity differs from target market"
            )
        probability_ids = tuple(item.selection_id for item in evidence.probabilities)
        if not set(selected).issubset(set(probability_ids)):
            raise ProductProposalRiskOutcomeInputMappingError(
                "target selection is absent from market-implied probability vector"
            )
        truth = evidence.to_dict().get("truth")
        if type(truth) is not dict or (
            truth.get("forecast_comparator_only") is not True
            or truth.get("execution_authority") is not False
            or truth.get("promotion_authority") is not False
            or truth.get("real_money_execution") is not False
        ):
            raise ProductProposalRiskOutcomeInputMappingError(
                "market-implied evidence authority boundary is inconsistent"
            )
        rows.append(
            {
                "market_identity": {
                    "sport": key[0],
                    "event_id": key[1],
                    "market_id": key[2],
                    "source_id": key[3],
                    "market_type": key[4],
                },
                "target_selection_ids": list(selected),
                "outcome_authority_sha256": _sha(
                    authority.authority_sha256,
                    "outcome_authority_sha256",
                ),
                "terminal_space_exact": authority.terminal_space_exact,
                "settlement_semantics": authority.settlement_semantics.value,
                "roster_basis": authority.roster_basis.value,
                "baseline_evidence_sha256": _sha(
                    evidence.evidence_sha256,
                    "baseline_evidence_sha256",
                ),
                "quote_snapshot_sha256": _sha(
                    evidence.quote_snapshot_sha256,
                    "quote_snapshot_sha256",
                ),
                "max_age_microseconds": evidence.max_age_microseconds,
                "probabilities": [
                    {
                        "selection_id": probability.selection_id,
                        "numerator": probability.numerator,
                        "denominator": probability.denominator,
                    }
                    for probability in evidence.probabilities
                ],
                "forecast_comparator_only": True,
                "provider_outcome_origin_independently_proven": False,
                "joint_probability_model_proven": False,
                "execution_authority": False,
                "promotion_authority": False,
                "real_money_execution": False,
            }
        )
    return tuple(rows)


def _material(
    target: ProductProposalRiskTarget,
    rows: tuple[dict[str, object], ...],
) -> dict[str, object]:
    if type(target) is not _TARGET_TYPE or not rows:
        raise ProductProposalRiskOutcomeInputMappingError(
            "mapping material requires exact target and non-empty rows"
        )
    context_sha256s = tuple(
        _HASHLIB_SHA256(value.encode("utf-8")).hexdigest()
        for value in target.candidate_context_json
    )
    return {
        "schema": _SCHEMA,
        "workspace_instance_id": _text(
            target.workspace_instance_id,
            "workspace_instance_id",
            max_length=256,
        ),
        "target_sha256": _sha(target.target_sha256, "target_sha256"),
        "target_decision_ts": _text(target.decision_ts, "target_decision_ts"),
        "candidate_vector_sha256": _sha(
            target.candidate_vector_sha256,
            "candidate_vector_sha256",
        ),
        "candidate_context_sha256s": list(context_sha256s),
        "market_rows": list(rows),
        "mapping_identity_proven": True,
        "exact_target_market_coverage_proven": True,
        "decision_time_market_inputs_rebuilt": True,
        "provider_outcome_origin_independently_proven": False,
        "joint_probability_model_proven": False,
        "proposal_target_counterfactual_execution_proven": False,
        "risk_upper_bound_for_target": False,
        "proposal_target_risk_qualified": False,
        "grants_risk_approval_authority": False,
        "grants_ticket_authority": False,
        "grants_broker_execution_authority": False,
        "grants_real_money_authority": False,
        "grants_state_mutation_authority": False,
    }


def _build(
    *,
    record: DecisionRecord,
    target: ProductProposalRiskTarget,
    rows: tuple[dict[str, object], ...],
    expected_mapping_sha256: str,
    _bind=_BIND_IDENTITY,
) -> ProductProposalRiskOutcomeInputMapping:
    if type(record) is not _RECORD_TYPE:
        raise ProductProposalRiskOutcomeInputMappingError(
            "persisted outcome-input mapping record has unsupported type"
        )
    mapping_sha256 = _sha(expected_mapping_sha256, "expected_mapping_sha256")
    action_id = _ACTION_PREFIX + mapping_sha256
    material = _material(target, rows)
    expected_fields = set(material)
    expected_fields.update(
        {
            "mapping_sha256",
            MATERIAL_ACTION_ID_PAYLOAD_KEY,
            ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
            RISK_POLICY_PROVENANCE_PAYLOAD_KEY,
        }
    )
    payload = record.payload
    if set(payload) != expected_fields:
        raise ProductProposalRiskOutcomeInputMappingError(
            "persisted outcome-input mapping payload schema is invalid"
        )
    if (
        record.action != _ACTION
        or record.agent != _AGENT
        or record.replay_run_id != action_id
        or record.context_hash != mapping_sha256
        or record.decision_id != action_id
        or payload.get(MATERIAL_ACTION_ID_PAYLOAD_KEY) != action_id
        or payload.get("mapping_sha256") != mapping_sha256
    ):
        raise ProductProposalRiskOutcomeInputMappingError(
            "persisted outcome-input mapping envelope is invalid"
        )
    for key, value in material.items():
        if payload.get(key) != value:
            raise ProductProposalRiskOutcomeInputMappingError(
                f"persisted outcome-input mapping field {key!r} does not re-resolve"
            )
    if _digest(material) != mapping_sha256:
        raise ProductProposalRiskOutcomeInputMappingError(
            "outcome-input mapping digest does not re-derive"
        )

    instance = object.__new__(ProductProposalRiskOutcomeInputMapping)
    values: dict[str, object] = {
        "workspace_instance_id": target.workspace_instance_id,
        "decision_id": action_id,
        "target_sha256": target.target_sha256,
        "target_decision_ts": target.decision_ts,
        "candidate_vector_sha256": target.candidate_vector_sha256,
        "candidate_context_sha256s": tuple(material["candidate_context_sha256s"]),
        "market_identity_json": tuple(
            _canonical_json_text(row["market_identity"]) for row in rows
        ),
        "outcome_authority_sha256s": tuple(
            row["outcome_authority_sha256"] for row in rows
        ),
        "baseline_evidence_sha256s": tuple(
            row["baseline_evidence_sha256"] for row in rows
        ),
        "probability_vector_json": tuple(
            _canonical_json_text(row["probabilities"]) for row in rows
        ),
        "mapping_sha256": mapping_sha256,
    }
    for name in _RESULT_FIELDS_EXPECTED:
        object.__setattr__(instance, name, values[name])
    _bind(instance)
    return instance


def issue_product_proposal_risk_outcome_input_mapping(
    workspace: Path,
    *,
    target_sha256: str,
    market_inputs: tuple[ProposalRiskMarketInput, ...],
) -> ProductProposalRiskOutcomeInputMapping:
    """Persist exact target-to-market input mapping without widening risk authority."""

    _require_dispatch()
    workspace = _workspace_path(workspace)
    target_sha256 = _sha(target_sha256, "target_sha256")
    target = _resolve_target(workspace, target_sha256)
    rows = _market_rows(target, market_inputs)
    material = _material(target, rows)
    mapping_sha256 = _digest(material)
    action_id = _ACTION_PREFIX + mapping_sha256

    with _LOCK_TYPE(workspace):
        _require_dispatch()
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            existing = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskOutcomeInputMappingError(
                "outcome-input mapping Decision Ledger re-resolution failed"
            ) from exc
        if existing is None:
            record = _RECORD_TYPE(
                replay_run_id=action_id,
                agent=_AGENT,
                observed_ts=target.decision_ts,
                action=_ACTION,
                payload={
                    **material,
                    "mapping_sha256": mapping_sha256,
                    MATERIAL_ACTION_ID_PAYLOAD_KEY: action_id,
                },
                context_hash=mapping_sha256,
                decision_id=action_id,
            )
            try:
                _LEDGER_APPEND(
                    ledger,
                    record,
                    goal,
                    risk_policy=policy,
                )
                existing = _LEDGER_RESOLVE(
                    ledger,
                    action_id,
                    goal,
                    risk_policy=policy,
                )
            except (DecisionLedgerIntegrityError, OSError, TypeError, ValueError) as exc:
                raise ProductProposalRiskOutcomeInputMappingError(
                    "outcome-input mapping Decision Ledger append failed"
                ) from exc
        if existing is None:
            raise ProductProposalRiskOutcomeInputMappingError(
                "outcome-input mapping append did not re-resolve"
            )

    fresh_target = _resolve_target(workspace, target_sha256)
    fresh_rows = _market_rows(fresh_target, market_inputs)
    if _digest(_material(fresh_target, fresh_rows)) != mapping_sha256:
        raise ProductProposalRiskOutcomeInputMappingError(
            "proposal target/market inputs changed during mapping issuance"
        )

    with _LOCK_TYPE(workspace):
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            final_record = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskOutcomeInputMappingError(
                "outcome-input mapping final re-resolution failed"
            ) from exc
        if final_record is None:
            raise ProductProposalRiskOutcomeInputMappingError(
                "outcome-input mapping disappeared after durable append"
            )
        return _build(
            record=final_record,
            target=fresh_target,
            rows=fresh_rows,
            expected_mapping_sha256=mapping_sha256,
        )


def resolve_product_proposal_risk_outcome_input_mapping(
    workspace: Path,
    *,
    mapping_sha256: str,
    target_sha256: str,
    market_inputs: tuple[ProposalRiskMarketInput, ...],
) -> ProductProposalRiskOutcomeInputMapping:
    """Rebuild all decision-time inputs and re-resolve the durable mapping."""

    _require_dispatch()
    workspace = _workspace_path(workspace)
    mapping_sha256 = _sha(mapping_sha256, "mapping_sha256")
    target_sha256 = _sha(target_sha256, "target_sha256")
    target = _resolve_target(workspace, target_sha256)
    rows = _market_rows(target, market_inputs)
    if _digest(_material(target, rows)) != mapping_sha256:
        raise ProductProposalRiskOutcomeInputMappingError(
            "requested mapping differs from current target/market input roots"
        )
    action_id = _ACTION_PREFIX + mapping_sha256
    with _LOCK_TYPE(workspace):
        _require_dispatch()
        goal, policy, ledger = _current_economic_state(workspace)
        try:
            record = _LEDGER_RESOLVE(
                ledger,
                action_id,
                goal,
                risk_policy=policy,
            )
        except (DecisionLedgerIntegrityError, TypeError, ValueError) as exc:
            raise ProductProposalRiskOutcomeInputMappingError(
                "outcome-input mapping Decision Ledger re-resolution failed"
            ) from exc
        if record is None:
            raise ProductProposalRiskOutcomeInputMappingError(
                "outcome-input mapping is missing from canonical Decision Ledger"
            )
        return _build(
            record=record,
            target=target,
            rows=rows,
            expected_mapping_sha256=mapping_sha256,
        )


_HELPER_WITNESSES = tuple(
    (
        name,
        globals()[name],
        getattr(globals()[name], "__code__", None),
    )
    for name in (
        "_workspace_path",
        "_current_economic_state",
        "_resolve_target",
        "_target_market_requirements",
        "_market_rows",
        "_material",
        "_build",
        "_text",
        "_sha",
        "_instant",
        "_canonical_json_text",
        "_digest",
    )
)
_HELPER_WITNESSES_EXPECTED = _HELPER_WITNESSES

del _IDENTITY_PROVEN
del _BIND_IDENTITY


__all__ = [
    "ProductProposalRiskOutcomeInputMapping",
    "ProductProposalRiskOutcomeInputMappingError",
    "ProposalRiskMarketInput",
    "issue_product_proposal_risk_outcome_input_mapping",
    "resolve_product_proposal_risk_outcome_input_mapping",
]
