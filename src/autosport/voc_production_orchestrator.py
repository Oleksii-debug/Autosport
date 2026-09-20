"""Production orchestration for paired realized-VOC shadow evidence.

This module deliberately does not score VOC.  It closes the execution seam between
router-owned precompute admission and the existing canonical outcome/VOC authority:
backend invocation is fenced by a durable receipt, successful output is published
through ``ModelComputeRouterStore.record_voc_shadow_execution``, and restart after an
uncertain invocation never blindly resends paid work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from .decision_ledger import JsonlDecisionLedger
from .integrity import atomic_write_json
from .model_compute_router import ModelComputeRouterError, ModelComputeRouterStore
from .voc_evaluation import PairedVOCEvaluation, VOCEvaluationStore
from .voc_outcome_scoring import append_paired_voc_admission
from .workspace_lock import WorkspaceEconomicLock

_RECEIPT_SCHEMA = "autosport.voc_production_receipts"
_RECEIPT_VERSION = 2
_RECEIPT_STATES = {"STARTED", "SUCCEEDED", "PUBLISHED"}
_ROLES = {"baseline", "challenger"}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ModelComputeRouterError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8", errors="strict")
    return value


def _sha(name: str, value: object) -> str:
    text = _text(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ModelComputeRouterError(f"{name} must be SHA-256 hex")
    return text


def _instant(name: str, value: object) -> datetime:
    text = _text(name, value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelComputeRouterError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelComputeRouterError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ModelComputeRouterError("VOC compute identity must be an object")
    expected = {"candidate_id", "backend_id", "model_id", "config_sha256"}
    if set(value) != expected:
        raise ModelComputeRouterError("VOC compute identity fields are invalid")
    return {
        "candidate_id": _text("candidate_id", value["candidate_id"]),
        "backend_id": _text("backend_id", value["backend_id"]),
        "model_id": _text("model_id", value["model_id"]),
        "config_sha256": _sha("config_sha256", value["config_sha256"]),
    }


@dataclass(frozen=True, slots=True)
class VOCBackendResult:
    """Measured result returned by one already-authorized backend invocation."""

    output_sha256: str
    action: str
    abstained: bool
    completed_at: str
    available_at: str
    actual_cost: Decimal
    evidence_sha256: str

    def __post_init__(self) -> None:
        _sha("output_sha256", self.output_sha256)
        _text("action", self.action)
        if type(self.abstained) is not bool:
            raise ModelComputeRouterError("abstained must be bool")
        completed = _instant("completed_at", self.completed_at)
        available = _instant("available_at", self.available_at)
        if available < completed:
            raise ModelComputeRouterError("available_at cannot predate completed_at")
        if not isinstance(self.actual_cost, Decimal) or not self.actual_cost.is_finite():
            raise ModelComputeRouterError("actual_cost must be a finite Decimal")
        if self.actual_cost < Decimal("0"):
            raise ModelComputeRouterError("actual_cost must be non-negative")
        _sha("evidence_sha256", self.evidence_sha256)

    def payload(self) -> dict[str, Any]:
        return {
            "output_sha256": self.output_sha256,
            "action": self.action,
            "abstained": self.abstained,
            "completed_at": self.completed_at,
            "available_at": self.available_at,
            "actual_cost": str(self.actual_cost),
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "VOCBackendResult":
        try:
            return cls(
                output_sha256=raw["output_sha256"],
                action=raw["action"],
                abstained=raw["abstained"],
                completed_at=raw["completed_at"],
                available_at=raw["available_at"],
                actual_cost=Decimal(raw["actual_cost"]),
                evidence_sha256=raw["evidence_sha256"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ModelComputeRouterError):
                raise
            raise ModelComputeRouterError("invalid VOC backend result") from exc


class VOCProductionOrchestrator:
    """Exactly-once-attempt producer for router-owned paired shadow evidence.

    ``STARTED`` is durable before the backend callable is entered.  Therefore an
    interrupted/uncertain paid invocation is never retried automatically.  A
    ``SUCCEEDED`` receipt can be publication-retried without invoking the backend
    again, while ``PUBLISHED`` is fully idempotent.
    """

    def __init__(
        self,
        router_store: ModelComputeRouterStore,
        receipt_path: str | Path | None = None,
        *,
        decision_ledger: JsonlDecisionLedger | None = None,
    ) -> None:
        if not isinstance(router_store, ModelComputeRouterStore):
            raise TypeError("router_store must be ModelComputeRouterStore")
        if decision_ledger is not None and not isinstance(
            decision_ledger, JsonlDecisionLedger
        ):
            raise TypeError("decision_ledger must be JsonlDecisionLedger or None")
        self.router_store = router_store
        self.decision_ledger = decision_ledger
        self.path = (
            Path(receipt_path)
            if receipt_path is not None
            else router_store.path.with_name(f"{router_store.path.name}.voc-production.json")
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])
        self._load()

    @staticmethod
    def _body(
        receipts: list[dict[str, Any]],
        *,
        version: int = _RECEIPT_VERSION,
    ) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "version": version,
            "receipts": receipts,
        }

    def _write(self, receipts: list[dict[str, Any]]) -> None:
        body = self._body(receipts)
        atomic_write_json(self.path, {**body, "state_sha256": _digest(body)})

    def _load(self) -> dict[tuple[str, str], dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelComputeRouterError("VOC production receipt store is unreadable") from exc
        if type(raw) is not dict or set(raw) != {
            "schema", "version", "receipts", "state_sha256"
        }:
            raise ModelComputeRouterError("VOC production receipt schema is invalid")
        version = raw["version"]
        if (
            raw["schema"] != _RECEIPT_SCHEMA
            or type(version) is not int
            or version not in {1, _RECEIPT_VERSION}
        ):
            raise ModelComputeRouterError("VOC production receipt version mismatch")
        receipts = raw["receipts"]
        if type(receipts) is not list:
            raise ModelComputeRouterError("VOC production receipts must be a list")
        body = self._body(receipts, version=version)
        if _sha("state_sha256", raw["state_sha256"]) != _digest(body):
            raise ModelComputeRouterError("VOC production receipt state SHA-256 mismatch")
        loaded: dict[tuple[str, str], dict[str, Any]] = {}
        for item in receipts:
            if type(item) is not dict:
                raise ModelComputeRouterError("VOC production receipt must be an object")
            normalized = dict(item)
            request_id = _text("request_id", item.get("request_id"))
            role = _text("role", item.get("role"))
            if role not in _ROLES:
                raise ModelComputeRouterError("VOC production receipt role is invalid")
            state = item.get("state")
            if state not in _RECEIPT_STATES:
                raise ModelComputeRouterError("VOC production receipt state is invalid")
            _sha("route_record_sha256", item.get("route_record_sha256"))
            _identity(item.get("candidate_identity"))
            _instant("started_at", item.get("started_at"))
            if version == 1:
                if "admission_sha256" in item:
                    raise ModelComputeRouterError(
                        "legacy VOC production receipt contains future admission authority"
                    )
                normalized["admission_sha256"] = None
            else:
                if "admission_sha256" not in item:
                    raise ModelComputeRouterError(
                        "VOC production receipt lacks paired admission authority"
                    )
                admission_sha = item.get("admission_sha256")
                if admission_sha is not None:
                    _sha("admission_sha256", admission_sha)
            result = item.get("result")
            if state == "STARTED":
                if result is not None or item.get("authority_sha256") is not None:
                    raise ModelComputeRouterError("STARTED VOC receipt contains terminal data")
            else:
                if not isinstance(result, Mapping):
                    raise ModelComputeRouterError("terminal VOC receipt lacks backend result")
                VOCBackendResult.from_payload(result)
                if state == "PUBLISHED":
                    _sha("authority_sha256", item.get("authority_sha256"))
                elif item.get("authority_sha256") is not None:
                    raise ModelComputeRouterError("SUCCEEDED VOC receipt has publication authority")
            key = (request_id, role)
            if key in loaded:
                raise ModelComputeRouterError("duplicate VOC production receipt identity")
            loaded[key] = normalized
        return loaded

    def _persist_map(self, values: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
        receipts = [dict(values[key]) for key in sorted(values)]
        self._write(receipts)

    @staticmethod
    def _admission_payload(precompute: Mapping[str, Any]) -> dict[str, Any]:
        version = precompute.get("schema_version")
        if version not in {1, 2}:
            raise ModelComputeRouterError("VOC precompute admission version is unsupported")
        # Router schema v2 adds immutable ResearchProtocol/cohort publication
        # witnesses.  The DecisionLedger admission intentionally remains schema v1;
        # project only its common fields here without mutating/downgrading the
        # router-owned v2 authority that terminal-aware scoring later revalidates.
        scope = precompute.get("scope")
        expected_scope = {
            "sport_id",
            "league_id",
            "regime_id",
            "urgency_id",
            "contradiction_state",
        }
        if not isinstance(scope, Mapping) or set(scope) != expected_scope:
            raise ModelComputeRouterError("VOC precompute admission scope is invalid")
        return {
            "schema_version": 1,
            "admission_id": _text("admission_id", precompute.get("admission_id")),
            "decision_input_sha256": _sha(
                "decision_input_sha256", precompute.get("decision_input_sha256")
            ),
            "decision_context_sha256": _sha(
                "decision_context_sha256", precompute.get("decision_context_sha256")
            ),
            "decision_deadline": _text(
                "decision_deadline", precompute.get("decision_deadline")
            ),
            "research_protocol_id": _text(
                "research_protocol_id", precompute.get("research_protocol_id")
            ),
            "cohort_id": _text("cohort_id", precompute.get("cohort_id")),
            "task_class": _text("task_class", precompute.get("task_class")),
            "scope": {
                field: _text(field, scope.get(field))
                for field in sorted(expected_scope)
            },
            "baseline_compute_identity": _identity(
                precompute.get("baseline_compute_identity")
            ),
            "challenger_compute_identity": _identity(
                precompute.get("challenger_compute_identity")
            ),
        }

    def _ensure_pair_admission(self, request_id: str) -> str:
        request = _text("request_id", request_id)
        ledger = self.decision_ledger
        if ledger is None:
            raise ModelComputeRouterError(
                "paired VOC production requires canonical DecisionLedger authority"
            )
        precompute = self.router_store.get_voc_precompute_admission(request)
        if precompute is None:
            raise ModelComputeRouterError(
                "paired VOC production requires canonical precompute admission"
            )
        admission = self._admission_payload(precompute)
        recorded_at = _text(
            "VOC precompute authority_recorded_at",
            precompute.get("authority_recorded_at"),
        )
        _instant("VOC precompute authority_recorded_at", recorded_at)
        replay_run_id = f"voc-production:{request}"
        agent = "voc-production-orchestrator"
        decision_id = f"voc-admission:{admission['admission_id']}"
        expected_record = {
            "replay_run_id": replay_run_id,
            "agent": agent,
            "observed_ts": recorded_at,
            "action": "VOC_PAIRED_ADMISSION",
            "payload": {"voc_paired_admission": admission},
            "context_hash": admission["decision_input_sha256"],
            "decision_id": decision_id,
            "recorded_at": recorded_at,
        }

        with WorkspaceEconomicLock(self.path.parent):
            receipts = self._load()
            request_receipts = [
                item
                for (stored_request, _), item in receipts.items()
                if stored_request == request
            ]
            if any(item.get("admission_sha256") is None for item in request_receipts):
                raise ModelComputeRouterError(
                    "existing VOC production work lacks pre-execution paired admission; "
                    "refusing post-hoc backfill"
                )
            records = ledger.verified_records()
            matches = [
                record
                for record in records
                if getattr(record, "decision_id", None) == decision_id
            ]
            if len(matches) > 1:
                raise ModelComputeRouterError(
                    "canonical paired VOC admission identity is ambiguous"
                )
            if matches:
                record = matches[0]
                if record.to_dict() != expected_record:
                    raise ModelComputeRouterError(
                        "canonical paired VOC admission conflicts with router precompute authority"
                    )
                admission_sha = _digest(expected_record)
                if any(
                    item.get("admission_sha256") != admission_sha
                    for item in request_receipts
                ):
                    raise ModelComputeRouterError(
                        "VOC production receipt conflicts with paired admission authority"
                    )
                return admission_sha
            if request_receipts:
                raise ModelComputeRouterError(
                    "VOC production receipt references missing paired admission authority"
                )
            admission_sha = append_paired_voc_admission(
                ledger,
                admission_id=admission["admission_id"],
                decision_context_sha256=admission["decision_context_sha256"],
                decision_input_sha256=admission["decision_input_sha256"],
                decision_deadline=admission["decision_deadline"],
                research_protocol_id=admission["research_protocol_id"],
                cohort_id=admission["cohort_id"],
                task_class=admission["task_class"],
                scope=admission["scope"],
                baseline_compute_identity=admission["baseline_compute_identity"],
                challenger_compute_identity=admission["challenger_compute_identity"],
                replay_run_id=replay_run_id,
                agent=agent,
                recorded_at=recorded_at,
            )
            if admission_sha != _digest(expected_record):
                raise ModelComputeRouterError(
                    "paired VOC admission digest is not deterministic"
                )
            ledger.verified_snapshot()
            return admission_sha

    def _precompute(self, request_id: str, role: str) -> tuple[dict[str, Any], dict[str, str]]:
        request = _text("request_id", request_id)
        role_text = _text("role", role)
        if role_text not in _ROLES:
            raise ModelComputeRouterError("VOC production role is invalid")
        precompute = self.router_store.get_voc_precompute_admission(request)
        if precompute is None:
            raise ModelComputeRouterError("VOC production requires canonical precompute admission")
        identity = _identity(precompute.get(f"{role_text}_compute_identity"))
        return precompute, identity

    def run_role(
        self,
        *,
        request_id: str,
        role: str,
        invoke: Callable[[], VOCBackendResult],
        _admission_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Invoke one role at most once, then publish canonical shadow evidence."""

        if not callable(invoke):
            raise TypeError("invoke must be callable")
        admission_sha = (
            None
            if _admission_sha256 is None
            else _sha("admission_sha256", _admission_sha256)
        )
        precompute, identity = self._precompute(request_id, role)
        key = (request_id, role)

        already = self.router_store.get_voc_shadow_execution(request_id, role)
        if already is not None:
            with WorkspaceEconomicLock(self.path.parent):
                receipts = self._load()
                existing = receipts.get(key)
                if existing is None:
                    if admission_sha is not None:
                        raise ModelComputeRouterError(
                            "paired VOC publication lacks producer receipt admission binding"
                        )
                    return already
                if (
                    existing["route_record_sha256"] != precompute["route_record_sha256"]
                    or existing["candidate_identity"] != identity
                    or existing.get("admission_sha256") != admission_sha
                ):
                    raise ModelComputeRouterError(
                        "VOC production receipt no longer matches canonical route authority"
                    )
                if existing["state"] == "STARTED":
                    raise ModelComputeRouterError(
                        "canonical VOC publication exists without terminal producer result"
                    )
                result = VOCBackendResult.from_payload(existing["result"])
                authority_result = VOCBackendResult.from_payload(already)
                if result != authority_result:
                    raise ModelComputeRouterError(
                        "VOC publication authority conflicts with producer result"
                    )
                expected_authority = _sha(
                    "authority_sha256", already.get("authority_sha256")
                )
                if existing["state"] == "PUBLISHED":
                    if existing["authority_sha256"] != expected_authority:
                        raise ModelComputeRouterError(
                            "VOC publication authority conflicts with receipt"
                        )
                else:
                    receipts[key] = {
                        **existing,
                        "state": "PUBLISHED",
                        "authority_sha256": expected_authority,
                    }
                    self._persist_map(receipts)
            return already

        with WorkspaceEconomicLock(self.path.parent):
            receipts = self._load()
            existing = receipts.get(key)
            if existing is None:
                receipts[key] = {
                    "request_id": request_id,
                    "role": role,
                    "route_record_sha256": precompute["route_record_sha256"],
                    "candidate_identity": identity,
                    "admission_sha256": admission_sha,
                    "state": "STARTED",
                    "started_at": _now(),
                    "result": None,
                    "authority_sha256": None,
                }
                self._persist_map(receipts)
                result = None
            else:
                if (
                    existing["route_record_sha256"] != precompute["route_record_sha256"]
                    or existing["candidate_identity"] != identity
                    or existing.get("admission_sha256") != admission_sha
                ):
                    raise ModelComputeRouterError(
                        "VOC production receipt no longer matches canonical route authority"
                    )
                if existing["state"] == "STARTED":
                    raise ModelComputeRouterError(
                        "VOC backend invocation is uncertain; refusing blind resend"
                    )
                result = VOCBackendResult.from_payload(existing["result"])

        if result is None:
            produced = invoke()
            if not isinstance(produced, VOCBackendResult):
                raise ModelComputeRouterError("VOC backend returned invalid result type")
            result = produced
            with WorkspaceEconomicLock(self.path.parent):
                receipts = self._load()
                current = receipts.get(key)
                if (
                    current is None
                    or current["state"] != "STARTED"
                    or current.get("admission_sha256") != admission_sha
                ):
                    raise ModelComputeRouterError("VOC production receipt changed during invocation")
                receipts[key] = {
                    **current,
                    "state": "SUCCEEDED",
                    "result": result.payload(),
                }
                self._persist_map(receipts)

        authority = self.router_store.record_voc_shadow_execution(
            request_id=request_id,
            role=role,
            output_sha256=result.output_sha256,
            action=result.action,
            abstained=result.abstained,
            completed_at=result.completed_at,
            available_at=result.available_at,
            actual_cost=result.actual_cost,
            evidence_sha256=result.evidence_sha256,
        )
        with WorkspaceEconomicLock(self.path.parent):
            receipts = self._load()
            current = receipts.get(key)
            if (
                current is None
                or current["state"] not in {"SUCCEEDED", "PUBLISHED"}
                or current.get("admission_sha256") != admission_sha
            ):
                raise ModelComputeRouterError("VOC production receipt lost before publication")
            expected_authority = _sha("authority_sha256", authority.get("authority_sha256"))
            if current["state"] == "PUBLISHED":
                if current["authority_sha256"] != expected_authority:
                    raise ModelComputeRouterError("VOC publication authority conflicts with receipt")
            else:
                receipts[key] = {
                    **current,
                    "state": "PUBLISHED",
                    "authority_sha256": expected_authority,
                }
                self._persist_map(receipts)
        return authority

    def run_pair(
        self,
        *,
        request_id: str,
        baseline_invoke: Callable[[], VOCBackendResult],
        challenger_invoke: Callable[[], VOCBackendResult],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        # Materialize the scorer-recognized denominator before either backend can
        # run. Successful scoring later supplies the scored terminal; negative
        # terminals remain owned by canonical execution authority.
        admission_sha = self._ensure_pair_admission(request_id)
        baseline = self.run_role(
            request_id=request_id,
            role="baseline",
            invoke=baseline_invoke,
            _admission_sha256=admission_sha,
        )
        challenger = self.run_role(
            request_id=request_id,
            role="challenger",
            invoke=challenger_invoke,
            _admission_sha256=admission_sha,
        )
        return baseline, challenger


def causal_matching_voc_history(
    evaluation_store: VOCEvaluationStore,
    *,
    precompute_admission: Mapping[str, Any],
    as_of: str,
    require_canonical: bool = True,
) -> tuple[PairedVOCEvaluation, ...]:
    """Return only earlier, exact-context VOC history for a later route.

    No sign/action filter is applied: positive, null, negative and harmful outcomes
    remain in memory.  Exact task/scope/compute/protocol matching prevents regime
    mixing.  With the default canonical fence, every selected item must resolve via
    the existing scientific/outcome authority before it can enter route history.
    """

    if not isinstance(evaluation_store, VOCEvaluationStore):
        raise TypeError("evaluation_store must be VOCEvaluationStore")
    if not isinstance(precompute_admission, Mapping):
        raise TypeError("precompute_admission must be a mapping")
    if type(require_canonical) is not bool:
        raise TypeError("require_canonical must be bool")
    cutoff = _instant("as_of", as_of)
    scope = precompute_admission.get("scope")
    if not isinstance(scope, Mapping):
        raise ModelComputeRouterError("VOC precompute scope is invalid")
    baseline = _identity(precompute_admission.get("baseline_compute_identity"))
    challenger = _identity(precompute_admission.get("challenger_compute_identity"))
    wanted = {
        "task_class": _text("task_class", precompute_admission.get("task_class")),
        "sport_id": _text("sport_id", scope.get("sport_id")),
        "league_id": _text("league_id", scope.get("league_id")),
        "regime_id": _text("regime_id", scope.get("regime_id")),
        "urgency_id": _text("urgency_id", scope.get("urgency_id")),
        "contradiction_state": _text(
            "contradiction_state", scope.get("contradiction_state")
        ),
        "research_protocol_id": _text(
            "research_protocol_id", precompute_admission.get("research_protocol_id")
        ),
    }
    selected: list[PairedVOCEvaluation] = []
    for value in evaluation_store.values():
        if _instant("evaluated_at", value.evaluated_at) >= cutoff:
            continue
        if any(getattr(value, field) != expected for field, expected in wanted.items()):
            continue
        if (
            value.baseline_candidate_id != baseline["candidate_id"]
            or value.baseline_backend_id != baseline["backend_id"]
            or value.baseline_model_id != baseline["model_id"]
            or value.baseline_config_sha256 != baseline["config_sha256"]
            or value.challenger_candidate_id != challenger["candidate_id"]
            or value.challenger_backend_id != challenger["backend_id"]
            or value.challenger_model_id != challenger["model_id"]
            or value.challenger_config_sha256 != challenger["config_sha256"]
        ):
            continue
        if require_canonical:
            value = evaluation_store.require(
                value.evaluation_id,
                evaluation_sha256=value.evaluation_sha256,
                as_of=as_of,
            )
        selected.append(value)
    selected.sort(key=lambda item: (_instant("evaluated_at", item.evaluated_at), item.evaluation_id))
    return tuple(selected)