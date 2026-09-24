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
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Final, Mapping
from weakref import WeakKeyDictionary

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

# These values define one durable product authority namespace. Public module
# constants remain import-compatible, but runtime rebinding must not create an
# alternate state file or independent monotonic journal inside one workspace.
_CANONICAL_FILE_NAME: Final = FILE_NAME
_CANONICAL_AUTHORITY_DOMAIN: Final = AUTHORITY_DOMAIN
_CANONICAL_AUTHORITY_KEY: Final = AUTHORITY_KEY

# The origin boundary must remain anchored to the import-time product classes.
# Module globals are writable in Python; using them for "exact canonical" checks
# would let a caller temporarily redefine which classes this authority trusts.
_CANONICAL_OPPORTUNITY_INTENT_CLASS: Final = OpportunityIntent
_CANONICAL_ROUTER_STORE_CLASS: Final = ModelComputeRouterStore
_CANONICAL_ROUTE_REQUEST_CLASS: Final = ComputeRouteRequest
_CANONICAL_ROUTE_REQUEST_FROM_PAYLOAD: Final = ComputeRouteRequest.from_payload
_CANONICAL_ROUTER_GET_REQUEST: Final = ModelComputeRouterStore.get_request


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


def _positive_window(value: object, field: str) -> timedelta:
    if type(value) is not timedelta or value <= timedelta(0):
        raise ModelComputeIntentRouteAuthorityError(
            f"{field} must be an exact positive timedelta"
        )
    return value


def _deadline_from_window(issued_at: object, window: object) -> str:
    issued = _instant(issued_at, "issued_at")
    duration = _positive_window(window, "decision_timeout")
    try:
        deadline = issued + duration
    except OverflowError as exc:
        raise ModelComputeIntentRouteAuthorityError(
            "decision_timeout exceeds supported datetime range"
        ) from exc
    return _time(deadline, "decision_deadline")


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


def _build_product_monotonic_authority_root():
    """Freeze the product root resolver's OS dependencies at import time."""

    os_name = os.name
    path_type = Path
    error_type = ModelComputeIntentRouteAuthorityError

    if os_name == "nt":
        try:
            import ctypes

            create_unicode_buffer = ctypes.create_unicode_buffer
            get_folder_path = (
                ctypes.windll.shell32.SHGetFolderPathW  # type: ignore[attr-defined]
            )
        except (AttributeError, ImportError) as exc:
            raise error_type(
                "cannot resolve product-owned Windows authority root"
            ) from exc

        def resolve() -> Path:
            try:
                buffer = create_unicode_buffer(32768)
                result = get_folder_path(
                    None,
                    0x001C,  # CSIDL_LOCAL_APPDATA
                    None,
                    0,
                    buffer,
                )
            except (AttributeError, OSError, ValueError) as exc:
                raise error_type(
                    "cannot resolve product-owned Windows authority root"
                ) from exc
            if result != 0 or not buffer.value:
                raise error_type(
                    "cannot resolve product-owned Windows authority root"
                )
            base = path_type(buffer.value)
            relative = (
                path_type("Autosport")
                / "application-state"
                / "monotonic-authority-v1"
            )
            if not base.is_absolute():
                raise error_type(
                    "product-owned monotonic authority root must be absolute"
                )
            return base / relative

        return resolve

    try:
        import pwd

        getuid = os.getuid
        getpwuid = pwd.getpwuid
    except (AttributeError, ImportError) as exc:
        raise error_type(
            "cannot resolve product-owned POSIX authority root"
        ) from exc

    def resolve() -> Path:
        try:
            home = getpwuid(getuid()).pw_dir
        except (KeyError, OSError) as exc:
            raise error_type(
                "cannot resolve product-owned POSIX authority root"
            ) from exc
        base = path_type(home) / ".local" / "state"
        relative = path_type("autosport") / "monotonic-authority-v1"
        if not base.is_absolute():
            raise error_type(
                "product-owned monotonic authority root must be absolute"
            )
        return base / relative

    return resolve


_product_monotonic_authority_root = _build_product_monotonic_authority_root()
_CANONICAL_PRODUCT_MONOTONIC_AUTHORITY_ROOT: Final = (
    _product_monotonic_authority_root
)


def _intent_identity(intent: OpportunityIntent) -> dict[str, str]:
    if type(intent) is not _CANONICAL_OPPORTUNITY_INTENT_CLASS:
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
        if proposal > issued:
            raise ModelComputeIntentRouteAuthorityError(
                "intent proposal cannot be in the future at product issuance"
            )
        if type(self.request) is not dict:
            raise ModelComputeIntentRouteAuthorityError(
                "request must be a canonical object"
            )
        try:
            request = _CANONICAL_ROUTE_REQUEST_FROM_PAYLOAD(self.request)
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
        if _instant(request.decision_deadline, "request.decision_deadline") <= issued:
            raise ModelComputeIntentRouteAuthorityError(
                "router request deadline must be after product issuance time"
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


# Persisted positive origin authority must not resolve through the writable
# public module binding after import. Capture the exact canonical record
# constructor/parser once, just like the canonical request/store surfaces.
_CANONICAL_INTENT_ROUTE_RECORD_CLASS: Final = ModelComputeIntentRouteRecord
_CANONICAL_INTENT_ROUTE_RECORD_FROM_DICT: Final = (
    ModelComputeIntentRouteRecord.from_dict
)


def _build_store_init():
    """Bind machine-root selection to import-time closure-owned authority."""

    root_resolver = _CANONICAL_PRODUCT_MONOTONIC_AUTHORITY_ROOT
    root_code = getattr(root_resolver, "__code__", None)
    root_closure = getattr(root_resolver, "__closure__", None)
    try:
        root_closure_state = tuple(
            cell.cell_contents for cell in (root_closure or ())
        )
    except ValueError as exc:
        raise ModelComputeIntentRouteAuthorityError(
            "canonical product authority root closure is invalid"
        ) from exc

    path_type = Path
    authority_type = MonotonicWorkspaceAuthority
    lock_type = WorkspaceEconomicLock
    file_name = _CANONICAL_FILE_NAME
    authority_domain = _CANONICAL_AUTHORITY_DOMAIN
    authority_key = _CANONICAL_AUTHORITY_KEY
    error_type = ModelComputeIntentRouteAuthorityError
    runtime_bindings = WeakKeyDictionary()
    authority_methods = tuple(
        (
            name,
            method,
            getattr(method, "__code__", None),
        )
        for name in (
            "read_history",
            "recover",
            "prepare",
            "abort",
            "commit",
        )
        for method in (getattr(authority_type, name),)
    )

    def require_runtime_binding(self) -> None:
        binding = runtime_bindings.get(self)
        if binding is None:
            raise error_type("intent-route authority runtime binding is missing")
        (
            workspace,
            path,
            authority,
            authority_root,
            authority_workspace,
            domain,
            key,
            workspace_binding,
            workspace_instance_id,
            workspace_binding_path,
            namespace_sha256,
            journal_dir,
            records_dir,
            namespace_marker_path,
        ) = binding
        try:
            instance_state = vars(self)
            authority_instance_state = vars(authority)
            authority_dispatch_changed = any(
                name in authority_instance_state
                or getattr(authority_type, name, None) is not method
                or getattr(method, "__code__", None) is not method_code
                for name, method, method_code in authority_methods
            )
            if (
                self.workspace is not workspace
                or self.path is not path
                or self._authority is not authority
                or any(
                    name in instance_state
                    for name in ("_recover", "issue_request", "resolve_current")
                )
                or authority_dispatch_changed
                or type(authority) is not authority_type
                or authority.authority_root != authority_root
                or authority.workspace != authority_workspace
                or authority.domain != domain
                or authority.key != key
                or authority.workspace_binding is not workspace_binding
                or authority.workspace_instance_id != workspace_instance_id
                or authority.workspace_binding_path != workspace_binding_path
                or authority.namespace_sha256 != namespace_sha256
                or authority.journal_dir != journal_dir
                or authority.records_dir != records_dir
                or authority.namespace_marker_path != namespace_marker_path
            ):
                raise error_type("intent-route authority runtime binding changed")
        except AttributeError as exc:
            raise error_type(
                "intent-route authority runtime binding changed"
            ) from exc

    def sealed_init(self, workspace: str | Path) -> None:
        if getattr(root_resolver, "__code__", None) is not root_code:
            raise error_type(
                "canonical product authority root resolver code changed"
            )
        live_closure = getattr(root_resolver, "__closure__", None)
        try:
            live_closure_state = tuple(
                cell.cell_contents for cell in (live_closure or ())
            )
        except ValueError as exc:
            raise error_type(
                "canonical product authority root closure changed"
            ) from exc
        if (
            len(live_closure_state) != len(root_closure_state)
            or any(
                current is not frozen
                for current, frozen in zip(
                    live_closure_state,
                    root_closure_state,
                )
            )
        ):
            raise error_type(
                "canonical product authority root closure changed"
            )

        self.workspace = path_type(workspace).absolute().resolve(strict=False)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / file_name
        self._authority = authority_type(
            workspace=self.workspace,
            domain=authority_domain,
            key=authority_key,
            authority_root=root_resolver(),
        )
        authority = self._authority
        runtime_bindings[self] = (
            self.workspace,
            self.path,
            authority,
            authority.authority_root,
            authority.workspace,
            authority.domain,
            authority.key,
            authority.workspace_binding,
            authority.workspace_instance_id,
            authority.workspace_binding_path,
            authority.namespace_sha256,
            authority.journal_dir,
            authority.records_dir,
            authority.namespace_marker_path,
        )
        with lock_type(self.workspace):
            self._recover()
            self._records = self._load()

    return sealed_init, require_runtime_binding


_STORE_INIT, _STORE_RUNTIME_GUARD = _build_store_init()


class ModelComputeIntentRouteAuthorityStore:
    """Creation-only issuance authority with restart-safe exact re-resolution."""

    __init__ = _STORE_INIT

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
            item = _CANONICAL_INTENT_ROUTE_RECORD_FROM_DICT(value)
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
        if type(router_store) is not _CANONICAL_ROUTER_STORE_CLASS:
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
        decision_timeout: timedelta,
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
            and request.decision_deadline
            == _deadline_from_window(request.created_at, decision_timeout)
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
        decision_timeout: timedelta,
        response_ttl_seconds: Decimal,
        baseline_candidate_id: str,
        cloud_candidate_id: str | None = None,
        voc_regime_id: str | None = None,
        voc_urgency_id: str | None = None,
        voc_contradiction_state: str | None = None,
    ) -> ComputeRouteRequest:
        """Issue or idempotently recover one exact request for one exact intent.

        No caller-provided absolute created_at/deadline, decision-input digest, or
        decision-evidence digest is accepted. The caller supplies only a positive
        routing window; the absolute deadline is derived from the product clock.
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
            router_request = _CANONICAL_ROUTER_GET_REQUEST(
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
                request = _CANONICAL_ROUTE_REQUEST_FROM_PAYLOAD(existing.request)
                if not self._request_static_matches(
                    request,
                    required_capability=required_capability,
                    data_classification=data_classification,
                    allow_cloud=allow_cloud,
                    max_cost=max_cost,
                    decision_timeout=decision_timeout,
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
            if _instant(identity["proposal_ts"], "proposal_ts") > _instant(
                issued_at,
                "issued_at",
            ):
                raise ModelComputeIntentRouteAuthorityError(
                    "intent proposal cannot be in the future at product issuance"
                )
            decision_deadline = _deadline_from_window(
                issued_at,
                decision_timeout,
            )
            request = _CANONICAL_ROUTE_REQUEST_CLASS(
                request_id=canonical_request_id,
                created_at=issued_at,
                decision_deadline=decision_deadline,
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
            record = _CANONICAL_INTENT_ROUTE_RECORD_CLASS(
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
            # Positive origin bytes must be derived directly from the exact
            # mechanically staged record set. Do not dispatch this authority-
            # bearing composition through a mutable module helper.
            ordered_records = tuple(
                sorted(staged, key=lambda item: item.request_id)
            )
            payload = {
                "schema": SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "records": [
                    item.to_dict() for item in ordered_records
                ],
            }
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

    def resolve_current(
        self,
        *,
        intent: OpportunityIntent,
        router_store: ModelComputeRouterStore,
        request_id: str,
    ) -> ModelComputeIntentRouteRecord:
        """Re-resolve exact intent origin for a new decision after this lookup.

        A successful lookup proves only that the exact product-issued origin and
        immutable router request exist now. It does not prove that the issuance
        existed at any caller-selected historical timestamp.
        """

        identity = _intent_identity(intent)
        relpath = self._router_relpath(router_store)
        self._reject_router_method_shadow(router_store)
        canonical_request_id = _text(request_id, "request_id")

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
            router_request = _CANONICAL_ROUTER_GET_REQUEST(
                router_store,
                canonical_request_id,
            )
            if router_request is None:
                raise ModelComputeIntentRouteAuthorityError(
                    "issued model-compute request is missing from canonical router"
                )
            if type(router_request) is not _CANONICAL_ROUTE_REQUEST_CLASS:
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

    def resolve(
        self,
        *,
        intent: OpportunityIntent,
        router_store: ModelComputeRouterStore,
        request_id: str,
        decision_at: datetime | str,
    ) -> ModelComputeIntentRouteRecord:
        """Fail closed for timestamp-only historical origin resolution.

        The issuance wall-clock timestamp is operational metadata, not durable
        proof that the origin existed before a historical decision. Historical
        positive authority requires a separate durable causal decision witness.
        """

        _instant(decision_at, "decision_at")
        raise ModelComputeIntentRouteAuthorityError(
            "timestamp-only historical intent-route resolution requires "
            "durable causal decision authority"
        )


def _install_store_runtime_guards() -> None:
    guard = _STORE_RUNTIME_GUARD
    recover = ModelComputeIntentRouteAuthorityStore._recover
    issue_request = ModelComputeIntentRouteAuthorityStore.issue_request
    resolve_current = ModelComputeIntentRouteAuthorityStore.resolve_current

    @wraps(recover)
    def guarded_recover(self) -> None:
        guard(self)
        recover(self)
        guard(self)

    @wraps(issue_request)
    def guarded_issue_request(self, *args, **kwargs):
        guard(self)
        result = issue_request(self, *args, **kwargs)
        guard(self)
        return result

    @wraps(resolve_current)
    def guarded_resolve_current(self, *args, **kwargs):
        guard(self)
        result = resolve_current(self, *args, **kwargs)
        guard(self)
        return result

    ModelComputeIntentRouteAuthorityStore._recover = guarded_recover
    ModelComputeIntentRouteAuthorityStore.issue_request = guarded_issue_request
    ModelComputeIntentRouteAuthorityStore.resolve_current = guarded_resolve_current


_install_store_runtime_guards()
del _STORE_INIT, _STORE_RUNTIME_GUARD, _install_store_runtime_guards


__all__ = [
    "ModelComputeIntentRouteAuthorityError",
    "ModelComputeIntentRouteAuthorityStore",
    "ModelComputeIntentRouteRecord",
]
