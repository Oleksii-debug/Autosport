"""Product-owned causal issuance for model-compute requests tied to OpportunityIntent.

The model-compute router owns immutable request/decision records, but a matching
digest inside a caller-created request is not proof that the product issued that
request for one exact OpportunityIntent.  This companion authority closes only
that origin relation.

A new request is constructed here from the exact canonical intent.  The product
clock supplies created_at, the intent proposal timestamp supplies the deadline,
and the intent/evidence digests are inserted mechanically.  The issuance is
persisted before routing and protected by the independent monotonic workspace
authority.  Resolution after restart re-derives the intent identity and
exact-matches the immutable request from the bound router store.

This module does not assign monetary value, select a tariff, execute a provider
action, or authorize real-money behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Mapping

from .integrity import atomic_write_json, sha256_file
from .json_integrity import strict_json_loads
from .model_compute_router import (
    ComputeRouteRequest,
    DataClassification,
    ModelComputeRouterStore,
)
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicWorkspaceAuthority,
)
from .portfolio_plan import OpportunityIntent
from .workspace_lock import WorkspaceEconomicLock


SCHEMA: Final = "autosport.model_compute_intent_route_authority"
SCHEMA_VERSION: Final = 1
FILE_NAME: Final = "model-compute-intent-route-authority.json"
AUTHORITY_DOMAIN: Final = "autosport.model-compute-intent-route-authority.v1"
AUTHORITY_KEY: Final = "model-compute-intent-route-issuance"
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class ModelComputeIntentRouteAuthorityError(ValueError):
    """The intent-to-request authority is missing, ambiguous, or malformed."""


def _text(value: object, field: str, *, limit: int = 512) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > limit
    ):
        raise ModelComputeIntentRouteAuthorityError(
            f"{field} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ModelComputeIntentRouteAuthorityError(
            f"{field} must be valid UTF-8"
        ) from exc
    return value


def _sha(value: object, field: str) -> str:
    digest = _text(value, field, limit=64)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ModelComputeIntentRouteAuthorityError(
            f"{field} must be lowercase SHA-256"
        )
    return digest


def _instant(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        raw = _text(value, field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ModelComputeIntentRouteAuthorityError(
                f"{field} must be ISO-8601"
            ) from exc
    else:
        raise ModelComputeIntentRouteAuthorityError(
            f"{field} must be timezone-aware datetime/ISO-8601"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelComputeIntentRouteAuthorityError(
            f"{field} must include timezone"
        )
    return parsed.astimezone(timezone.utc)


def _time(value: object, field: str) -> str:
    return _instant(value, field).isoformat().replace("+00:00", "Z")


def _digest(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ModelComputeIntentRouteAuthorityError(
            "authority payload is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _state_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ModelComputeIntentRouteAuthorityError(
            "authority state is outside canonical JSON domain"
        ) from exc


def _authority_now() -> str:
    """Product clock seam. Tests may patch this private function."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _intent_identity(intent: OpportunityIntent) -> dict[str, str]:
    if type(intent) is not OpportunityIntent:
        raise ModelComputeIntentRouteAuthorityError(
            "intent must be the exact canonical OpportunityIntent type"
        )
    proposal_ts = getattr(intent.risk_context, "proposal_ts", None)
    if proposal_ts is None:
        raise ModelComputeIntentRouteAuthorityError(
            "OpportunityIntent lacks canonical proposal_ts"
        )
    audit_payload = intent.audit_payload()
    return {
        "intent_id": _text(intent.intent_id, "intent_id"),
        "intent_sha256": _sha(intent.intent_sha256, "intent_sha256"),
        "intent_audit_sha256": _digest(audit_payload),
        "opportunity_id": _text(
            intent.opportunity.opportunity_id,
            "opportunity_id",
        ),
        "opportunity_evidence_sha256": _sha(
            intent.evidence.evidence_sha256,
            "opportunity_evidence_sha256",
        ),
        "candidate_sha256": _sha(
            intent.candidate_sha256,
            "candidate_sha256",
        ),
        "proposal_ts": _time(proposal_ts, "proposal_ts"),
    }


@dataclass(frozen=True, slots=True)
class ModelComputeIntentRouteRecord:
    """Durable product-issued origin binding for one router request."""

    router_store_relpath: str
    intent_id: str
    intent_sha256: str
    intent_audit_sha256: str
    opportunity_id: str
    opportunity_evidence_sha256: str
    candidate_sha256: str
    proposal_ts: str
    issued_at: str
    request: dict[str, object]
    request_sha256: str

    def __post_init__(self) -> None:
        _text(self.router_store_relpath, "router_store_relpath")
        if self.router_store_relpath.startswith("/") or ".." in Path(
            self.router_store_relpath
        ).parts:
            raise ModelComputeIntentRouteAuthorityError(
                "router_store_relpath must remain inside the bound workspace"
            )
        _text(self.intent_id, "intent_id")
        _sha(self.intent_sha256, "intent_sha256")
        _sha(self.intent_audit_sha256, "intent_audit_sha256")
        _text(self.opportunity_id, "opportunity_id")
        _sha(
            self.opportunity_evidence_sha256,
            "opportunity_evidence_sha256",
        )
        _sha(self.candidate_sha256, "candidate_sha256")
        proposal = _instant(self.proposal_ts, "proposal_ts")
        issued = _instant(self.issued_at, "issued_at")
        if issued >= proposal:
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route issuance must occur before proposal cutoff"
            )
        if type(self.request) is not dict:
            raise ModelComputeIntentRouteAuthorityError(
                "request must be a canonical object"
            )
        try:
            request = ComputeRouteRequest.from_payload(self.request)
        except Exception as exc:
            raise ModelComputeIntentRouteAuthorityError(
                "persisted router request payload is invalid"
            ) from exc
        canonical_request = request.payload()
        if canonical_request != self.request:
            raise ModelComputeIntentRouteAuthorityError(
                "persisted router request payload is not canonical"
            )
        if _instant(request.created_at, "request.created_at") != issued:
            raise ModelComputeIntentRouteAuthorityError(
                "router request created_at must equal product issuance time"
            )
        if _instant(request.decision_deadline, "request.decision_deadline") != proposal:
            raise ModelComputeIntentRouteAuthorityError(
                "router request deadline must equal intent proposal cutoff"
            )
        if request.decision_input_sha256 != self.intent_sha256:
            raise ModelComputeIntentRouteAuthorityError(
                "router request is not mechanically bound to intent identity"
            )
        if request.decision_evidence_sha256 != self.opportunity_evidence_sha256:
            raise ModelComputeIntentRouteAuthorityError(
                "router request is not mechanically bound to opportunity evidence"
            )
        if _sha(self.request_sha256, "request_sha256") != _digest(
            canonical_request
        ):
            raise ModelComputeIntentRouteAuthorityError(
                "router request digest mismatch"
            )

    @property
    def request_id(self) -> str:
        return _text(self.request["request_id"], "request_id")

    def payload(self) -> dict[str, object]:
        return {
            "router_store_relpath": self.router_store_relpath,
            "intent_id": self.intent_id,
            "intent_sha256": self.intent_sha256,
            "intent_audit_sha256": self.intent_audit_sha256,
            "opportunity_id": self.opportunity_id,
            "opportunity_evidence_sha256": self.opportunity_evidence_sha256,
            "candidate_sha256": self.candidate_sha256,
            "proposal_ts": _time(self.proposal_ts, "proposal_ts"),
            "issued_at": _time(self.issued_at, "issued_at"),
            "request": dict(self.request),
            "request_sha256": self.request_sha256,
        }

    @property
    def authority_sha256(self) -> str:
        return _digest(self.payload())

    def to_dict(self) -> dict[str, object]:
        return {
            **self.payload(),
            "authority_sha256": self.authority_sha256,
        }

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, object],
    ) -> "ModelComputeIntentRouteRecord":
        expected = {
            "router_store_relpath",
            "intent_id",
            "intent_sha256",
            "intent_audit_sha256",
            "opportunity_id",
            "opportunity_evidence_sha256",
            "candidate_sha256",
            "proposal_ts",
            "issued_at",
            "request",
            "request_sha256",
            "authority_sha256",
        }
        if set(raw) != expected:
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route record fields do not match schema"
            )
        request = raw["request"]
        if type(request) is not dict:
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route request must be an object"
            )
        item = cls(
            router_store_relpath=raw["router_store_relpath"],  # type: ignore[arg-type]
            intent_id=raw["intent_id"],  # type: ignore[arg-type]
            intent_sha256=raw["intent_sha256"],  # type: ignore[arg-type]
            intent_audit_sha256=raw["intent_audit_sha256"],  # type: ignore[arg-type]
            opportunity_id=raw["opportunity_id"],  # type: ignore[arg-type]
            opportunity_evidence_sha256=raw["opportunity_evidence_sha256"],  # type: ignore[arg-type]
            candidate_sha256=raw["candidate_sha256"],  # type: ignore[arg-type]
            proposal_ts=raw["proposal_ts"],  # type: ignore[arg-type]
            issued_at=raw["issued_at"],  # type: ignore[arg-type]
            request=dict(request),
            request_sha256=raw["request_sha256"],  # type: ignore[arg-type]
        )
        if raw["authority_sha256"] != item.authority_sha256:
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route authority digest mismatch"
            )
        return item


def _state_payload(
    records: tuple[ModelComputeIntentRouteRecord, ...],
) -> dict[str, object]:
    ordered = tuple(sorted(records, key=lambda item: item.request_id))
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "records": [item.to_dict() for item in ordered],
    }


class ModelComputeIntentRouteAuthorityStore:
    """Creation-only issuance authority with restart-safe exact re-resolution."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        authority_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).absolute().resolve(strict=False)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / FILE_NAME
        self._authority = MonotonicWorkspaceAuthority(
            workspace=self.workspace,
            domain=AUTHORITY_DOMAIN,
            key=AUTHORITY_KEY,
            authority_root=authority_root,
        )
        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()

    def _observed_sha256(self) -> str | None:
        return sha256_file(self.path) if self.path.exists() else None

    def _recover(self) -> None:
        observed = self._observed_sha256()
        history = self._authority.read_history()
        if history and history[-1].phase is AuthorityPhase.PREPARE:
            pending = history[-1]
            self._authority.recover(
                observed_state_sha256=observed,
                tx_id=pending.tx_id,
                semantic_binding_sha256=pending.semantic_binding_sha256,
            )
            return
        self._authority.recover(observed_state_sha256=observed)

    def _load(self) -> tuple[ModelComputeIntentRouteRecord, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = strict_json_loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route authority state is unreadable"
            ) from exc
        if (
            type(raw) is not dict
            or set(raw) != {"schema", "schema_version", "records"}
            or raw["schema"] != SCHEMA
            or raw["schema_version"] != SCHEMA_VERSION
            or type(raw["schema_version"]) is not int
            or type(raw["records"]) is not list
        ):
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route authority state schema is invalid"
            )
        records: list[ModelComputeIntentRouteRecord] = []
        request_ids: set[str] = set()
        authority_ids: set[str] = set()
        for value in raw["records"]:
            if not isinstance(value, Mapping):
                raise ModelComputeIntentRouteAuthorityError(
                    "intent-route authority record must be an object"
                )
            item = ModelComputeIntentRouteRecord.from_dict(value)
            if item.request_id in request_ids:
                raise ModelComputeIntentRouteAuthorityError(
                    "duplicate request_id in intent-route authority"
                )
            if item.authority_sha256 in authority_ids:
                raise ModelComputeIntentRouteAuthorityError(
                    "duplicate intent-route authority identity"
                )
            request_ids.add(item.request_id)
            authority_ids.add(item.authority_sha256)
            records.append(item)
        if tuple(item.request_id for item in records) != tuple(
            sorted(item.request_id for item in records)
        ):
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route authority records must use canonical request order"
            )
        return tuple(records)

    def _router_relpath(
        self,
        router_store: ModelComputeRouterStore,
    ) -> str:
        if type(router_store) is not ModelComputeRouterStore:
            raise ModelComputeIntentRouteAuthorityError(
                "router_store must be the exact canonical ModelComputeRouterStore type"
            )
        path = Path(router_store.path).absolute().resolve(strict=False)
        if not path.is_relative_to(self.workspace) or path == self.workspace:
            raise ModelComputeIntentRouteAuthorityError(
                "router_store must be inside the bound workspace"
            )
        return _text(
            path.relative_to(self.workspace).as_posix(),
            "router_store_relpath",
        )

    @staticmethod
    def _reject_router_method_shadow(
        router_store: ModelComputeRouterStore,
    ) -> None:
        try:
            state = vars(router_store)
        except TypeError as exc:
            raise ModelComputeIntentRouteAuthorityError(
                "router_store instance state is unavailable"
            ) from exc
        if "get_request" in state:
            raise ModelComputeIntentRouteAuthorityError(
                "router_store get_request method shadow is not allowed"
            )

    def _record_for_request(
        self,
        request_id: str,
    ) -> ModelComputeIntentRouteRecord | None:
        matches = [
            item for item in self._records if item.request_id == request_id
        ]
        if len(matches) > 1:
            raise ModelComputeIntentRouteAuthorityError(
                "intent-route authority is ambiguous"
            )
        return None if not matches else matches[0]

    @staticmethod
    def _record_matches_intent(
        record: ModelComputeIntentRouteRecord,
        identity: Mapping[str, str],
    ) -> bool:
        return (
            record.intent_id == identity["intent_id"]
            and record.intent_sha256 == identity["intent_sha256"]
            and record.intent_audit_sha256 == identity["intent_audit_sha256"]
            and record.opportunity_id == identity["opportunity_id"]
            and record.opportunity_evidence_sha256
            == identity["opportunity_evidence_sha256"]
            and record.candidate_sha256 == identity["candidate_sha256"]
            and record.proposal_ts == identity["proposal_ts"]
        )

    @staticmethod
    def _request_static_matches(
        request: ComputeRouteRequest,
        *,
        required_capability: str,
        data_classification: DataClassification,
        allow_cloud: bool,
        max_cost: Decimal,
        response_ttl_seconds: Decimal,
        baseline_candidate_id: str,
        cloud_candidate_id: str | None,
        voc_regime_id: str | None,
        voc_urgency_id: str | None,
        voc_contradiction_state: str | None,
    ) -> bool:
        return (
            request.required_capability == required_capability
            and request.data_classification is data_classification
            and request.allow_cloud is allow_cloud
            and request.max_cost == max_cost
            and request.response_ttl_seconds == response_ttl_seconds
            and request.baseline_candidate_id == baseline_candidate_id
            and request.cloud_candidate_id == cloud_candidate_id
            and request.voc_regime_id == voc_regime_id
            and request.voc_urgency_id == voc_urgency_id
            and request.voc_contradiction_state == voc_contradiction_state
        )

    def issue_request(
        self,
        *,
        intent: OpportunityIntent,
        router_store: ModelComputeRouterStore,
        request_id: str,
        required_capability: str,
        data_classification: DataClassification,
        allow_cloud: bool,
        max_cost: Decimal,
        response_ttl_seconds: Decimal,
        baseline_candidate_id: str,
        cloud_candidate_id: str | None = None,
        voc_regime_id: str | None = None,
        voc_urgency_id: str | None = None,
        voc_contradiction_state: str | None = None,
    ) -> ComputeRouteRequest:
        """Issue or idempotently recover one exact request for one exact intent.

        No caller-provided created_at, decision_deadline, decision-input digest, or
        decision-evidence digest is accepted.
        """

        identity = _intent_identity(intent)
        relpath = self._router_relpath(router_store)
        self._reject_router_method_shadow(router_store)
        canonical_request_id = _text(request_id, "request_id")
        if type(data_classification) is not DataClassification:
            raise ModelComputeIntentRouteAuthorityError(
                "data_classification must be exact DataClassification"
            )
        if type(allow_cloud) is not bool:
            raise ModelComputeIntentRouteAuthorityError(
                "allow_cloud must be bool"
            )

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()
            existing = self._record_for_request(canonical_request_id)
            router_request = ModelComputeRouterStore.get_request(
                router_store,
                canonical_request_id,
            )
            if existing is not None:
                if existing.router_store_relpath != relpath:
                    raise ModelComputeIntentRouteAuthorityError(
                        "request_id is bound to a different router store"
                    )
                if not self._record_matches_intent(existing, identity):
                    raise ModelComputeIntentRouteAuthorityError(
                        "request_id is immutable across intent identity"
                    )
                request = ComputeRouteRequest.from_payload(existing.request)
                if not self._request_static_matches(
                    request,
                    required_capability=required_capability,
                    data_classification=data_classification,
                    allow_cloud=allow_cloud,
                    max_cost=max_cost,
                    response_ttl_seconds=response_ttl_seconds,
                    baseline_candidate_id=baseline_candidate_id,
                    cloud_candidate_id=cloud_candidate_id,
                    voc_regime_id=voc_regime_id,
                    voc_urgency_id=voc_urgency_id,
                    voc_contradiction_state=voc_contradiction_state,
                ):
                    raise ModelComputeIntentRouteAuthorityError(
                        "request_id is immutable across routing request semantics"
                    )
                if (
                    router_request is not None
                    and (
                        type(router_request) is not ComputeRouteRequest
                        or router_request.payload() != request.payload()
                    )
                ):
                    raise ModelComputeIntentRouteAuthorityError(
                        "durable router request conflicts with product issuance"
                    )
                return request

            if router_request is not None:
                raise ModelComputeIntentRouteAuthorityError(
                    "existing router request cannot be backfilled with origin authority"
                )

            issued_at = _time(_authority_now(), "issued_at")
            if _instant(issued_at, "issued_at") >= _instant(
                identity["proposal_ts"],
                "proposal_ts",
            ):
                raise ModelComputeIntentRouteAuthorityError(
                    "new model-compute request must be issued before intent proposal cutoff"
                )
            request = ComputeRouteRequest(
                request_id=canonical_request_id,
                created_at=issued_at,
                decision_deadline=identity["proposal_ts"],
                required_capability=_text(
                    required_capability,
                    "required_capability",
                ),
                data_classification=data_classification,
                allow_cloud=allow_cloud,
                max_cost=max_cost,
                response_ttl_seconds=response_ttl_seconds,
                baseline_candidate_id=_text(
                    baseline_candidate_id,
                    "baseline_candidate_id",
                ),
                cloud_candidate_id=cloud_candidate_id,
                decision_input_sha256=identity["intent_sha256"],
                decision_evidence_sha256=identity[
                    "opportunity_evidence_sha256"
                ],
                voc_regime_id=voc_regime_id,
                voc_urgency_id=voc_urgency_id,
                voc_contradiction_state=voc_contradiction_state,
            )
            request_payload = request.payload()
            record = ModelComputeIntentRouteRecord(
                router_store_relpath=relpath,
                intent_id=identity["intent_id"],
                intent_sha256=identity["intent_sha256"],
                intent_audit_sha256=identity["intent_audit_sha256"],
                opportunity_id=identity["opportunity_id"],
                opportunity_evidence_sha256=identity[
                    "opportunity_evidence_sha256"
                ],
                candidate_sha256=identity["candidate_sha256"],
                proposal_ts=identity["proposal_ts"],
                issued_at=issued_at,
                request=request_payload,
                request_sha256=_digest(request_payload),
            )

            staged = (*self._records, record)
            payload = _state_payload(staged)
            intended = hashlib.sha256(_state_bytes(payload)).hexdigest()
            observed = self._observed_sha256()
            binding = _digest(
                {
                    "kind": "MODEL_COMPUTE_INTENT_ROUTE_ISSUE",
                    "authority_sha256": record.authority_sha256,
                    "router_store_relpath": relpath,
                    "observed_state_sha256": observed,
                    "intended_state_sha256": intended,
                }
            )
            tx_id = f"intent-route-{record.authority_sha256}"
            self._authority.prepare(
                tx_id=tx_id,
                observed_state_sha256=observed,
                intended_state_sha256=intended,
                semantic_binding_sha256=binding,
            )
            try:
                atomic_write_json(self.path, payload)
            except Exception:
                self._authority.abort(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    semantic_binding_sha256=binding,
                )
                raise
            published = self._observed_sha256()
            if published != intended:
                raise ModelComputeIntentRouteAuthorityError(
                    "published intent-route state does not match prepared bytes"
                )
            self._authority.commit(
                tx_id=tx_id,
                observed_state_sha256=published,
                semantic_binding_sha256=binding,
            )
            self._records = tuple(
                sorted(staged, key=lambda item: item.request_id)
            )
            return request

    def resolve(
        self,
        *,
        intent: OpportunityIntent,
        router_store: ModelComputeRouterStore,
        request_id: str,
        decision_at: datetime | str,
    ) -> ModelComputeIntentRouteRecord:
        """Re-resolve exact intent origin against the immutable router request."""

        identity = _intent_identity(intent)
        relpath = self._router_relpath(router_store)
        self._reject_router_method_shadow(router_store)
        canonical_request_id = _text(request_id, "request_id")
        cutoff = _instant(decision_at, "decision_at")
        if cutoff != _instant(identity["proposal_ts"], "proposal_ts"):
            raise ModelComputeIntentRouteAuthorityError(
                "decision_at must equal canonical OpportunityIntent proposal_ts"
            )

        with WorkspaceEconomicLock(self.workspace):
            self._recover()
            self._records = self._load()
            record = self._record_for_request(canonical_request_id)
            if record is None:
                raise ModelComputeIntentRouteAuthorityError(
                    "product-owned intent-route issuance is missing"
                )
            if record.router_store_relpath != relpath:
                raise ModelComputeIntentRouteAuthorityError(
                    "intent-route issuance belongs to a different router store"
                )
            if not self._record_matches_intent(record, identity):
                raise ModelComputeIntentRouteAuthorityError(
                    "intent-route issuance does not match canonical intent"
                )
            if _instant(record.issued_at, "issued_at") >= cutoff:
                raise ModelComputeIntentRouteAuthorityError(
                    "intent-route issuance was not causally available before cutoff"
                )
            router_request = ModelComputeRouterStore.get_request(
                router_store,
                canonical_request_id,
            )
            if router_request is None:
                raise ModelComputeIntentRouteAuthorityError(
                    "issued model-compute request is missing from canonical router"
                )
            if type(router_request) is not ComputeRouteRequest:
                raise ModelComputeIntentRouteAuthorityError(
                    "canonical router returned a non-canonical request"
                )
            router_payload = router_request.payload()
            if (
                router_payload != record.request
                or _digest(router_payload) != record.request_sha256
            ):
                raise ModelComputeIntentRouteAuthorityError(
                    "canonical router request differs from product issuance"
                )
            return record


__all__ = [
    "ModelComputeIntentRouteAuthorityError",
    "ModelComputeIntentRouteAuthorityStore",
    "ModelComputeIntentRouteRecord",
]
