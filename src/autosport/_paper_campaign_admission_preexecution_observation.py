"""Decision-time learning Observation authority for PAPER campaign admission.

The canonical economic decision producer issues one exact learning Observation from
decision-visible source evidence before publishing its DecisionRecord. The
Observation bytes live inside that existing durable record. The merged #727
decision-origin path later copies those already-issued bytes into RUN_RESERVED; it
never reconstructs them from execution-plan or reservation fields.

Existing v1 origins remain readable for generic PAPER recovery, but campaign
admission requires the v2 carrier and exact equality with the DecisionRecord
commitment. No second observation store or decision-origin protocol is introduced.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import Mapping
from weakref import WeakKeyDictionary

from . import _paper_execution_decision_origin as _origin
from . import _paper_execution_decision_origin_instance_guard as _instance_guard
from .learning_environment import (
    CausalLearningEnvironment,
    LearningEnvironmentError,
    Observation,
)
from .paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from .paper_execution_adoption import PaperExecutionAdoptionRuntime

_ORIGIN_SCHEMA = "autosport.paper_execution_decision_origin"
_ORIGIN_SCHEMA_V1 = 1
_ORIGIN_SCHEMA_V2 = 2
_ORIGIN_V1_FIELDS = frozenset(
    {"schema", "schema_version", "decision_id", "record_sha256"}
)
_ORIGIN_V2_FIELDS = frozenset((*_ORIGIN_V1_FIELDS, "learning_observation"))
_OBSERVATION_SCHEMA = "autosport.paper_execution_learning_observation"
_OBSERVATION_SCHEMA_VERSION = 1
_OBSERVATION_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "environment_id",
        "observed_at",
        "available_at",
        "evidence",
        "observation_id",
    }
)
_RUNTIME_INIT_SENTINEL = "_autosport_campaign_observation_pristine_init"
_RUNTIME_GETATTRIBUTE_SENTINEL = (
    "_autosport_campaign_observation_pristine_getattribute"
)
_ISSUER_METHOD = "_autosport_issue_predecision_learning_observation"
_INSTALL_SENTINEL = "_autosport_campaign_preexecution_observation_v4"


class _ProductIssuerDescriptor:
    """Data descriptor that prevents exact-runtime instance shadowing of issuer authority."""

    __slots__ = ("_issuer",)

    def __init__(self, issuer):
        object.__setattr__(self, "_issuer", issuer)

    def __get__(self, instance, owner=None):
        issuer = object.__getattribute__(self, "_issuer")
        if instance is None:
            return issuer
        return issuer.__get__(instance, owner or PaperExecutionAdoptionRuntime)

    def __set__(self, instance, value) -> None:
        raise AttributeError("product learning Observation issuer cannot be rebound")

    def __delete__(self, instance) -> None:
        raise AttributeError("product learning Observation issuer cannot be deleted")


def _observation_payload(observation: Observation) -> dict[str, object]:
    return {
        "schema": _OBSERVATION_SCHEMA,
        "schema_version": _OBSERVATION_SCHEMA_VERSION,
        "environment_id": observation.environment_id,
        "observed_at": observation.observed_at,
        "available_at": observation.available_at,
        "evidence": [[key, value] for key, value in observation.evidence],
        "observation_id": observation.observation_id,
    }


def _observation_from_payload(raw: object) -> Observation:
    if type(raw) is not dict or set(raw) != _OBSERVATION_FIELDS:
        raise _origin.PaperExecutionDecisionOriginError(
            "learning observation payload schema is invalid"
        )
    if (
        raw.get("schema") != _OBSERVATION_SCHEMA
        or raw.get("schema_version") != _OBSERVATION_SCHEMA_VERSION
    ):
        raise _origin.PaperExecutionDecisionOriginError(
            "unsupported learning observation payload schema"
        )
    evidence = raw.get("evidence")
    if type(evidence) is not list:
        raise _origin.PaperExecutionDecisionOriginError(
            "learning observation evidence must be an array"
        )
    normalized: list[tuple[str, str]] = []
    for item in evidence:
        if (
            type(item) is not list
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
        ):
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation evidence entry is invalid"
            )
        normalized.append((item[0], item[1]))
    try:
        observation = Observation(
            environment_id=raw.get("environment_id"),  # type: ignore[arg-type]
            observed_at=raw.get("observed_at"),  # type: ignore[arg-type]
            available_at=raw.get("available_at"),  # type: ignore[arg-type]
            evidence=tuple(normalized),
        )
    except (LearningEnvironmentError, TypeError, ValueError) as exc:
        raise _origin.PaperExecutionDecisionOriginError(
            "learning observation payload is invalid"
        ) from exc
    if raw.get("observation_id") != observation.observation_id:
        raise _origin.PaperExecutionDecisionOriginError(
            "learning observation identity does not match payload bytes"
        )
    return observation


def _utc(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise _origin.PaperExecutionDecisionOriginError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _origin.PaperExecutionDecisionOriginError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return parsed.astimezone(timezone.utc)


def _install() -> None:
    if getattr(PaperExecutionAdoptionRuntime, _INSTALL_SENTINEL, False):
        return

    bindings: WeakKeyDictionary[
        PaperExecutionAdoptionRuntime,
        tuple[CausalLearningEnvironment, str],
    ] = WeakKeyDictionary()
    bindings_lock = RLock()

    if not hasattr(PaperExecutionAdoptionRuntime, _RUNTIME_INIT_SENTINEL):
        setattr(
            PaperExecutionAdoptionRuntime,
            _RUNTIME_INIT_SENTINEL,
            PaperExecutionAdoptionRuntime.__init__,
        )
    if not hasattr(PaperExecutionAdoptionRuntime, _RUNTIME_GETATTRIBUTE_SENTINEL):
        setattr(
            PaperExecutionAdoptionRuntime,
            _RUNTIME_GETATTRIBUTE_SENTINEL,
            PaperExecutionAdoptionRuntime.__getattribute__,
        )
    stable_runtime_init = getattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_INIT_SENTINEL,
    )
    stable_runtime_getattribute = getattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_GETATTRIBUTE_SENTINEL,
    )
    stable_checkpoint = CausalLearningEnvironment.checkpoint
    stable_origin_to_dict = _origin.DecisionRecordOrigin.to_dict
    product_runtime_context = _instance_guard._PRODUCT_ORIGIN_RUNTIME
    json_loads = json.loads
    json_dumps = json.dumps

    @wraps(stable_runtime_init)
    def runtime_init_with_learning_environment(
        self: PaperExecutionAdoptionRuntime,
        *args,
        learning_environment: CausalLearningEnvironment | None = None,
        **kwargs,
    ) -> None:
        stable_runtime_init(self, *args, **kwargs)
        if learning_environment is None:
            return
        if type(learning_environment) is not CausalLearningEnvironment:
            raise TypeError(
                "learning_environment must be exact CausalLearningEnvironment or None"
            )
        try:
            stable_checkpoint(learning_environment)
        except LearningEnvironmentError as exc:
            raise _origin.PaperExecutionDecisionOriginError(
                "campaign learning environment must be at a durable checkpoint before decision"
            ) from exc
        with bindings_lock:
            bindings[self] = (
                learning_environment,
                learning_environment.environment_id,
            )

    def issue_predecision_learning_observation(
        self: PaperExecutionAdoptionRuntime,
        *,
        observed_at: str,
        available_at: str,
        evidence: tuple[tuple[str, str], ...],
    ) -> dict[str, object] | None:
        if type(self) is not PaperExecutionAdoptionRuntime:
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation issuer requires exact product execution runtime"
            )
        with bindings_lock:
            binding = bindings.get(self)
        if binding is None:
            return None
        environment, bound_environment_id = binding
        if environment.environment_id != bound_environment_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "campaign learning environment identity changed before decision"
            )
        if (
            type(evidence) is not tuple
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                or not item[0]
                or not item[1]
                for item in evidence
            )
        ):
            raise _origin.PaperExecutionDecisionOriginError(
                "decision-time learning evidence must be canonical string pairs"
            )
        keys = [item[0] for item in evidence]
        if len(keys) != len(set(keys)) or "environment_checkpoint_id" in keys:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision-time learning evidence keys must be unique"
            )
        try:
            checkpoint = stable_checkpoint(environment)
            observation = Observation(
                environment_id=bound_environment_id,
                observed_at=observed_at,
                available_at=available_at,
                evidence=tuple(
                    sorted(
                        (
                            *evidence,
                            ("environment_checkpoint_id", checkpoint.checkpoint_id),
                        )
                    )
                ),
            )
        except (LearningEnvironmentError, TypeError, ValueError) as exc:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision-time learning observation is not causally valid"
            ) from exc
        return _observation_payload(observation)

    runtime_type = PaperExecutionAdoptionRuntime
    issuer_descriptor = _ProductIssuerDescriptor(issue_predecision_learning_observation)
    canonical_issuer = issue_predecision_learning_observation

    def runtime_getattribute(
        self: PaperExecutionAdoptionRuntime,
        name: str,
    ):
        if name != _ISSUER_METHOD:
            return stable_runtime_getattribute(self, name)
        if type(self) is not runtime_type:
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation issuer requires exact product execution runtime"
            )
        current_descriptor = runtime_type.__dict__.get(_ISSUER_METHOD)
        if current_descriptor is not issuer_descriptor:
            raise _origin.PaperExecutionDecisionOriginError(
                "product learning Observation issuer class dispatch changed"
            )
        try:
            current_issuer = object.__getattribute__(issuer_descriptor, "_issuer")
        except AttributeError as exc:
            raise _origin.PaperExecutionDecisionOriginError(
                "product learning Observation issuer executable seal changed"
            ) from exc
        if current_issuer is not canonical_issuer:
            raise _origin.PaperExecutionDecisionOriginError(
                "product learning Observation issuer executable seal changed"
            )
        # Never dispatch through the mutable class attribute after verification.
        # Bind and return the concrete source-owned function captured by this
        # install closure, so a concurrent class replacement cannot execute.
        return canonical_issuer.__get__(self, runtime_type)

    def origin_to_dict(self: _origin.DecisionRecordOrigin) -> dict[str, object]:
        base = stable_origin_to_dict(self)
        runtime = product_runtime_context.get()
        if runtime is None:
            return base
        if type(runtime) is not PaperExecutionAdoptionRuntime:
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation origin lacks exact product execution runtime"
            )
        with bindings_lock:
            binding = bindings.get(runtime)
        if binding is None:
            return base
        _, bound_environment_id = binding

        # The exact DecisionLedger verifier already resolved these bytes from the
        # economic DecisionRecord. #727 is only a carrier: it must never mint or
        # reconstruct learning evidence from reservation/execution fields.
        raw_json = self.learning_observation_json
        observed_ts = self.decision_observed_ts
        if raw_json is None:
            return base
        if type(raw_json) is not str or type(observed_ts) is not str:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin lacks complete pre-published learning evidence"
            )
        try:
            raw = json_loads(raw_json)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise _origin.PaperExecutionDecisionOriginError(
                "pre-published learning observation is not canonical JSON"
            ) from exc
        if type(raw) is not dict:
            raise _origin.PaperExecutionDecisionOriginError(
                "pre-published learning observation payload is invalid"
            )
        observation = _observation_from_payload(raw)
        if observation.environment_id != bound_environment_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation belongs to another campaign environment"
            )
        if _utc(observation.available_at, "learning observation available_at") > _utc(
            observed_ts,
            "DecisionRecord observed_ts",
        ):
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation was not available before the economic decision"
            )
        canonical_raw = _observation_payload(observation)
        canonical_json = json_dumps(
            canonical_raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if raw != canonical_raw or raw_json != canonical_json:
            raise _origin.PaperExecutionDecisionOriginError(
                "learning observation durable bytes are not canonical"
            )
        return {
            "schema": _ORIGIN_SCHEMA,
            "schema_version": _ORIGIN_SCHEMA_V2,
            "decision_id": self.decision_id,
            "record_sha256": self.record_sha256,
            "learning_observation": canonical_raw,
        }

    @classmethod
    def origin_from_dict(cls, raw: object):
        if type(raw) is not dict:
            raise _origin.PaperExecutionDecisionOriginError(
                "decision origin payload schema is invalid"
            )
        version = raw.get("schema_version")
        if version == _ORIGIN_SCHEMA_V1:
            if set(raw) != _ORIGIN_V1_FIELDS or raw.get("schema") != _ORIGIN_SCHEMA:
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin payload schema is invalid"
                )
        elif version == _ORIGIN_SCHEMA_V2:
            if set(raw) != _ORIGIN_V2_FIELDS or raw.get("schema") != _ORIGIN_SCHEMA:
                raise _origin.PaperExecutionDecisionOriginError(
                    "decision origin payload schema is invalid"
                )
            _observation_from_payload(raw.get("learning_observation"))
        else:
            raise _origin.PaperExecutionDecisionOriginError(
                "unsupported decision origin schema"
            )
        return cls(
            decision_id=raw.get("decision_id"),  # type: ignore[arg-type]
            record_sha256=raw.get("record_sha256"),  # type: ignore[arg-type]
        )

    PaperExecutionAdoptionRuntime.__init__ = runtime_init_with_learning_environment
    setattr(PaperExecutionAdoptionRuntime, _ISSUER_METHOD, issuer_descriptor)
    PaperExecutionAdoptionRuntime.__getattribute__ = runtime_getattribute
    _origin.DecisionRecordOrigin.to_dict = origin_to_dict
    _origin.DecisionRecordOrigin.from_dict = origin_from_dict

    coordinator = PaperCampaignAdmissionCoordinator
    error = PaperCampaignAdmissionError
    original_resolver = coordinator._resolved_execution_decision_id

    def resolved_execution_decision_id(
        self,
        *,
        run_id: str,
        reservation: Mapping[str, object],
        origin: Mapping[str, object],
    ):
        authority = original_resolver(
            self,
            run_id=run_id,
            reservation=reservation,
            origin=origin,
        )
        observation = getattr(authority, "observation", None)
        if not isinstance(observation, Observation):
            raise error("PAPER pre-execution learning Observation is unavailable")
        authority.observation_adapter_schema = (
            "autosport.paper_campaign_preexecution_observation"
        )
        authority.observation_adapter_schema_version = 1
        authority.observation_producer_contract = (
            "autosport.persistent_live_decision.v2"
        )
        return authority

    coordinator._resolved_execution_decision_id = resolved_execution_decision_id
    setattr(PaperExecutionAdoptionRuntime, _INSTALL_SENTINEL, True)


_install()
