"""Pre-execution learning Observation authority for PAPER campaign admission.

Campaign learning must not invent an Observation after execution.  The existing #727
product-origin reservation is the first durable boundary that is both downstream of
an already-published DecisionLedger record and upstream of every PAPER attempt.  This
module binds one exact learning Observation at that boundary when the canonical
PaperExecutionAdoptionRuntime was constructed with the exact learning environment.

The Observation is carried inside #727 ``decision_origin`` bytes.  Existing v1
origins remain readable for execution recovery, but they are intentionally
insufficient for campaign admission.  No second origin store or standalone event is
introduced.
"""

from __future__ import annotations

import hashlib
import inspect
import json
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
_INSTALL_SENTINEL = "_autosport_campaign_preexecution_observation_v2"
_SHA256_HEX = frozenset("0123456789abcdef")


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_origin_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value:
        raise _origin.PaperExecutionDecisionOriginError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _origin.PaperExecutionDecisionOriginError(
            f"{name} must be UTF-8 encodable"
        ) from exc
    return value


def _canonical_sha(value: object, name: str) -> str:
    text = _canonical_origin_text(value, name)
    if (
        len(text) != 64
        or text.lower() != text
        or any(character not in _SHA256_HEX for character in text)
    ):
        raise _origin.PaperExecutionDecisionOriginError(
            f"{name} must be lowercase SHA-256"
        )
    return text


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


def _install() -> None:
    if getattr(PaperExecutionAdoptionRuntime, _INSTALL_SENTINEL, False):
        return

    # Keep the environment binding outside caller-mutable runtime attributes.  A
    # runtime may opt into campaign learning exactly once at construction; generic
    # PAPER execution remains valid but produces a v1 origin that admission rejects.
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
    stable_runtime_init = getattr(
        PaperExecutionAdoptionRuntime,
        _RUNTIME_INIT_SENTINEL,
    )
    stable_checkpoint = CausalLearningEnvironment.checkpoint
    stable_origin_to_dict = _origin.DecisionRecordOrigin.to_dict
    canonical_append_code = _instance_guard._CanonicalReservationView._append_event.__code__
    product_runtime_context = _instance_guard._PRODUCT_ORIGIN_RUNTIME

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
                "campaign learning environment must be at a durable checkpoint before execution"
            ) from exc
        with bindings_lock:
            bindings[self] = (
                learning_environment,
                learning_environment.environment_id,
            )

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
        environment, bound_environment_id = binding
        if environment.environment_id != bound_environment_id:
            raise _origin.PaperExecutionDecisionOriginError(
                "campaign learning environment identity changed before execution"
            )

        current = inspect.currentframe()
        append_frame = None
        reserve_frame = None
        try:
            append_frame = current.f_back if current is not None else None
            reserve_frame = append_frame.f_back if append_frame is not None else None
            if (
                append_frame is None
                or append_frame.f_code is not canonical_append_code
                or append_frame.f_locals.get("self")._origin is not self
            ):
                raise _origin.PaperExecutionDecisionOriginError(
                    "learning observation must be issued by canonical #727 reservation"
                )
            payload = append_frame.f_locals.get("payload")
            if type(payload) is not dict:
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical reservation payload is unavailable for learning observation"
                )
            if reserve_frame is None:
                raise _origin.PaperExecutionDecisionOriginError(
                    "canonical reservation frame is unavailable for learning observation"
                )
            plan = reserve_frame.f_locals.get("plan")
            observed_at = getattr(plan, "created_at", None)
            available_at = payload.get("started_at")
            if type(observed_at) is not str or type(available_at) is not str:
                raise _origin.PaperExecutionDecisionOriginError(
                    "pre-execution learning observation timestamps are unavailable"
                )
            if payload.get("trigger_id") != self.decision_id:
                raise _origin.PaperExecutionDecisionOriginError(
                    "learning observation decision identity conflicts with reservation"
                )
            try:
                checkpoint = stable_checkpoint(environment)
            except LearningEnvironmentError as exc:
                raise _origin.PaperExecutionDecisionOriginError(
                    "campaign learning environment is not checkpointable before execution"
                ) from exc

            action_ids = payload.get("action_ids")
            observation_evidence_ids = payload.get("observation_evidence_ids")
            if type(action_ids) is not list or type(observation_evidence_ids) is not dict:
                raise _origin.PaperExecutionDecisionOriginError(
                    "reservation evidence is unavailable for learning observation"
                )
            evidence = tuple(
                sorted(
                    (
                        ("decision_id", self.decision_id),
                        ("decision_record_sha256", self.record_sha256),
                        ("environment_checkpoint_id", checkpoint.checkpoint_id),
                        ("execution_action_ids_sha256", _digest(action_ids)),
                        (
                            "execution_model_fingerprint",
                            _canonical_origin_text(
                                payload.get("model_fingerprint"),
                                "execution model_fingerprint",
                            ),
                        ),
                        (
                            "execution_observation_evidence_ids_sha256",
                            _digest(observation_evidence_ids),
                        ),
                        (
                            "execution_plan_fingerprint",
                            _canonical_origin_text(
                                payload.get("plan_fingerprint"),
                                "execution plan_fingerprint",
                            ),
                        ),
                        (
                            "execution_plan_id",
                            _canonical_origin_text(
                                payload.get("plan_id"),
                                "execution plan_id",
                            ),
                        ),
                    )
                )
            )
            try:
                observation = Observation(
                    environment_id=bound_environment_id,
                    observed_at=observed_at,
                    available_at=available_at,
                    evidence=evidence,
                )
            except LearningEnvironmentError as exc:
                raise _origin.PaperExecutionDecisionOriginError(
                    "pre-execution learning observation is not causal"
                ) from exc
            return {
                "schema": _ORIGIN_SCHEMA,
                "schema_version": _ORIGIN_SCHEMA_V2,
                "decision_id": self.decision_id,
                "record_sha256": self.record_sha256,
                "learning_observation": _observation_payload(observation),
            }
        finally:
            del current
            del append_frame
            del reserve_frame

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
            decision_id=_canonical_origin_text(
                raw.get("decision_id"),
                "decision origin decision_id",
            ),
            record_sha256=_canonical_sha(
                raw.get("record_sha256"),
                "decision origin record_sha256",
            ),
        )

    # Extend only the serialization boundary used by #727.  Equality and durable
    # retry identity remain the original (decision_id, record_sha256) pair, so a v1
    # in-flight execution can still converge but can never be retroactively promoted
    # into campaign learning.
    PaperExecutionAdoptionRuntime.__init__ = runtime_init_with_learning_environment
    _origin.DecisionRecordOrigin.to_dict = origin_to_dict
    _origin.DecisionRecordOrigin.from_dict = origin_from_dict

    # The convergence resolver has already rebuilt the exact durable v2 Observation.
    # Attach only explicit producer-contract metadata here; never synthesize or
    # replace learning bytes after execution.
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
        authority.observation_producer_contract = (
            "autosport.paper_execution_decision_origin.v2"
        )
        return authority

    coordinator._resolved_execution_decision_id = resolved_execution_decision_id
    setattr(PaperExecutionAdoptionRuntime, _INSTALL_SENTINEL, True)


_install()
