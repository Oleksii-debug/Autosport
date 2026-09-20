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

from .integrity import atomic_write_json
from .model_compute_router import ModelComputeRouterError, ModelComputeRouterStore
from .voc_evaluation import PairedVOCEvaluation, VOCEvaluationStore
from .workspace_lock import WorkspaceEconomicLock

_RECEIPT_SCHEMA = "autosport.voc_production_receipts"
_RECEIPT_VERSION = 1
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
    ) -> None:
        if not isinstance(router_store, ModelComputeRouterStore):
            raise TypeError("router_store must be ModelComputeRouterStore")
        self.router_store = router_store
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
    def _body(receipts: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema": _RECEIPT_SCHEMA,
            "version": _RECEIPT_VERSION,
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
        if raw["schema"] != _RECEIPT_SCHEMA or raw["version"] != _RECEIPT_VERSION:
            raise ModelComputeRouterError("VOC production receipt version mismatch")
        receipts = raw["receipts"]
        if type(receipts) is not list:
            raise ModelComputeRouterError("VOC production receipts must be a list")
        body = self._body(receipts)
        if _sha("state_sha256", raw["state_sha256"]) != _digest(body):
            raise ModelComputeRouterError("VOC production receipt state SHA-256 mismatch")
        loaded: dict[tuple[str, str], dict[str, Any]] = {}
        for item in receipts:
            if type(item) is not dict:
                raise ModelComputeRouterError("VOC production receipt must be an object")
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
            loaded[key] = dict(item)
        return loaded

    def _persist_map(self, values: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
        receipts = [dict(values[key]) for key in sorted(values)]
        self._write(receipts)

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
    ) -> dict[str, Any]:
        """Invoke one role at most once, then publish canonical shadow evidence."""

        if not callable(invoke):
            raise TypeError("invoke must be callable")
        precompute, identity = self._precompute(request_id, role)
        key = (request_id, role)

        already = self.router_store.get_voc_shadow_execution(request_id, role)
        if already is not None:
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
                if current is None or current["state"] != "STARTED":
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
            if current is None or current["state"] not in {"SUCCEEDED", "PUBLISHED"}:
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
        baseline = self.run_role(
            request_id=request_id,
            role="baseline",
            invoke=baseline_invoke,
        )
        challenger = self.run_role(
            request_id=request_id,
            role="challenger",
            invoke=challenger_invoke,
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
