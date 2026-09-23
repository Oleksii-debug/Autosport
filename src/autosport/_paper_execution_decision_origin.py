from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import inspect
import json
from typing import Any, Mapping

from . import _paper_value_execution_authority as _paper_value_authority
from . import live_decision_loop as _live_decision_loop
from .decision_ledger import JsonlDecisionLedger
from .paper_execution_adoption import PaperExecutionAdoptionError, PaperExecutionAdoptionRuntime
from .paper_execution_reality import (
    PaperExecutionIntegrityError,
    PaperExecutionLedger,
    PaperExecutionStateError,
)


_ORIGIN_SCHEMA = "autosport.paper_execution_decision_origin"
_ORIGIN_SCHEMA_VERSION = 1
_SHA256_HEX = frozenset("0123456789abcdef")


class PaperExecutionDecisionOriginError(PaperExecutionIntegrityError):
    """Raised when pre-execution DecisionLedger origin cannot be proven exactly."""


@dataclass(frozen=True, slots=True)
class DecisionRecordOrigin:
    """Immutable digest identity of one already-durable DecisionLedger record."""

    decision_id: str
    record_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.decision_id) is not str
            or not self.decision_id
            or self.decision_id.strip() != self.decision_id
            or "\x00" in self.decision_id
        ):
            raise PaperExecutionDecisionOriginError(
                "decision origin decision_id must be canonical non-empty text"
            )
        try:
            self.decision_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise PaperExecutionDecisionOriginError(
                "decision origin decision_id must be UTF-8 encodable"
            ) from exc
        if (
            type(self.record_sha256) is not str
            or len(self.record_sha256) != 64
            or self.record_sha256 != self.record_sha256.lower()
            or any(character not in _SHA256_HEX for character in self.record_sha256)
        ):
            raise PaperExecutionDecisionOriginError(
                "decision origin record_sha256 must be lowercase SHA-256"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _ORIGIN_SCHEMA,
            "schema_version": _ORIGIN_SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "record_sha256": self.record_sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> "DecisionRecordOrigin":
        expected = {"schema", "schema_version", "decision_id", "record_sha256"}
        if type(raw) is not dict or set(raw) != expected:
            raise PaperExecutionDecisionOriginError(
                "decision origin payload schema is invalid"
            )
        if raw["schema"] != _ORIGIN_SCHEMA or raw["schema_version"] != _ORIGIN_SCHEMA_VERSION:
            raise PaperExecutionDecisionOriginError(
                "unsupported decision origin schema"
            )
        return cls(
            decision_id=raw["decision_id"],  # type: ignore[arg-type]
            record_sha256=raw["record_sha256"],  # type: ignore[arg-type]
        )


def verified_decision_origin(
    ledger: JsonlDecisionLedger,
    decision_id: str,
) -> DecisionRecordOrigin:
    """Resolve one origin only from exact canonical DecisionLedger durable bytes."""

    if type(ledger) is not JsonlDecisionLedger:
        raise PaperExecutionDecisionOriginError(
            "decision origin requires exact JsonlDecisionLedger authority"
        )
    if type(decision_id) is not str or not decision_id or decision_id.strip() != decision_id:
        raise ValueError("decision_id must be non-empty canonical text")

    snapshot = ledger.verified_snapshot()
    matches: list[DecisionRecordOrigin] = []
    for line in snapshot.payload.decode("utf-8").splitlines():
        envelope = json.loads(line)
        record = envelope["record"]
        if record.get("decision_id") != decision_id:
            continue
        matches.append(
            DecisionRecordOrigin(
                decision_id=decision_id,
                record_sha256=envelope["sha256"],
            )
        )
    if len(matches) != 1:
        raise PaperExecutionDecisionOriginError(
            "decision origin requires exactly one verified durable DecisionLedger record"
        )
    return matches[0]


_DECISION_ORIGIN: ContextVar[DecisionRecordOrigin | None] = ContextVar(
    "autosport_paper_execution_decision_origin",
    default=None,
)
_MASK_ORIGIN_FOR_LEGACY_LOAD: ContextVar[bool] = ContextVar(
    "autosport_paper_execution_mask_origin_for_legacy_load",
    default=False,
)

_ORIGINAL_LEDGER_RESERVE = PaperExecutionLedger.reserve_run
_ORIGINAL_LEDGER_LOAD = PaperExecutionLedger.load_run
_ORIGINAL_LEDGER_EVENTS = PaperExecutionLedger.events
_ORIGINAL_RUNTIME_EXECUTE = PaperExecutionAdoptionRuntime.execute


def _raw_events(
    ledger: PaperExecutionLedger,
    run_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    return _ORIGINAL_LEDGER_EVENTS(ledger, run_id)


def _reservation_origin_from_events(
    events: tuple[dict[str, Any], ...],
) -> DecisionRecordOrigin | None:
    reservations = [item for item in events if item.get("event_type") == "RUN_RESERVED"]
    if not reservations:
        return None
    if len(reservations) != 1:
        raise PaperExecutionDecisionOriginError(
            "execution run requires exactly one durable reservation"
        )
    payload = reservations[0].get("payload")
    if type(payload) is not dict:
        raise PaperExecutionDecisionOriginError(
            "execution reservation payload is invalid"
        )
    raw = payload.get("decision_origin")
    if raw is None:
        return None
    return DecisionRecordOrigin.from_dict(raw)


def _events_with_legacy_load_mask(
    self: PaperExecutionLedger,
    run_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    events = _ORIGINAL_LEDGER_EVENTS(self, run_id)
    if not _MASK_ORIGIN_FOR_LEGACY_LOAD.get():
        return events

    masked: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "RUN_RESERVED":
            masked.append(event)
            continue
        payload = event.get("payload")
        if type(payload) is not dict or "decision_origin" not in payload:
            masked.append(event)
            continue
        copy = dict(event)
        copy_payload = dict(payload)
        copy_payload.pop("decision_origin", None)
        copy["payload"] = copy_payload
        masked.append(copy)
    return tuple(masked)


def _reserve_run_with_decision_origin(
    self: PaperExecutionLedger,
    *,
    run_id: str,
    trigger_id: str,
    plan,
    config,
    started_at: str,
    observation_evidence_ids: Mapping[str, str],
) -> None:
    origin = _DECISION_ORIGIN.get()
    if origin is None:
        return _ORIGINAL_LEDGER_RESERVE(
            self,
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
    if type(self) is not PaperExecutionLedger:
        raise PaperExecutionDecisionOriginError(
            "origin-bound execution requires exact PaperExecutionLedger authority"
        )
    if origin.decision_id != trigger_id or origin.decision_id != plan.decision_id:
        raise PaperExecutionStateError(
            "decision origin does not match execution trigger/plan decision identity"
        )

    payload = {
        "trigger_id": trigger_id,
        "plan_id": plan.plan_id,
        "plan_fingerprint": plan.fingerprint,
        "model_fingerprint": config.fingerprint,
        "started_at": started_at,
        "action_ids": [action.action_id for action in plan.actions],
        "observation_evidence_ids": dict(sorted(observation_evidence_ids.items())),
        "decision_origin": origin.to_dict(),
    }
    self._append_event(
        event_type="RUN_RESERVED",
        run_id=run_id,
        key=f"{run_id}:reserve",
        payload=payload,
    )


def _load_run_with_decision_origin(
    self: PaperExecutionLedger,
    *,
    run_id: str,
    trigger_id: str,
    plan,
    config,
    started_at: str,
    observation_evidence_ids: Mapping[str, str],
):
    events = _raw_events(self, run_id)
    stored = _reservation_origin_from_events(events)
    expected = _DECISION_ORIGIN.get()
    if stored is not None and (
        stored.decision_id != trigger_id or stored.decision_id != plan.decision_id
    ):
        raise PaperExecutionDecisionOriginError(
            "durable decision origin conflicts with execution reservation identity"
        )
    if expected is not None:
        if stored is None:
            raise PaperExecutionStateError(
                "existing execution reservation lacks pre-execution decision origin"
            )
        if stored != expected:
            raise PaperExecutionStateError(
                "execution decision origin changed across retry/restart"
            )

    token = _MASK_ORIGIN_FOR_LEGACY_LOAD.set(True)
    try:
        return _ORIGINAL_LEDGER_LOAD(
            self,
            run_id=run_id,
            trigger_id=trigger_id,
            plan=plan,
            config=config,
            started_at=started_at,
            observation_evidence_ids=observation_evidence_ids,
        )
    finally:
        _MASK_ORIGIN_FOR_LEGACY_LOAD.reset(token)


def _reservation_decision_origin(
    self: PaperExecutionLedger,
    run_id: str,
) -> DecisionRecordOrigin | None:
    """Return the immutable DecisionLedger origin bound before this run's attempts."""

    if type(self) is not PaperExecutionLedger:
        raise PaperExecutionDecisionOriginError(
            "reservation origin requires exact PaperExecutionLedger authority"
        )
    return _reservation_origin_from_events(_raw_events(self, run_id))


def _exact_context_ledger(runtime: PaperExecutionAdoptionRuntime, context: object):
    if getattr(context, "paper_execution", None) is not runtime:
        raise PaperExecutionDecisionOriginError(
            "canonical product frame is not bound to this execution runtime"
        )
    ledger = getattr(context, "decision_ledger", None)
    if type(ledger) is not JsonlDecisionLedger:
        raise PaperExecutionDecisionOriginError(
            "canonical product execution requires exact JsonlDecisionLedger authority"
        )
    return ledger


def _resolve_product_origin_from_stack(
    runtime: PaperExecutionAdoptionRuntime,
    decision_id: str,
) -> DecisionRecordOrigin | None:
    """Resolve origin only from exact canonical product code frames.

    Arbitrary callers can construct AgentContext-like objects, local variables, and
    valid DecisionLedger records. None of those become product authority here. The
    executing frame itself must be one of the integrated product producers that
    durably publish the decision before invoking PAPER execution.
    """

    ledgers: dict[int, JsonlDecisionLedger] = {}
    current = inspect.currentframe()
    try:
        frame = current.f_back if current is not None else None
        while frame is not None:
            local = frame.f_locals
            if frame.f_code is _live_decision_loop.PersistentLiveDecisionLoop._persist_plan.__code__:
                owner = local.get("self")
                if type(owner) is not _live_decision_loop.PersistentLiveDecisionLoop:
                    raise PaperExecutionDecisionOriginError(
                        "live decision origin requires exact PersistentLiveDecisionLoop authority"
                    )
                if getattr(owner, "paper_execution", None) is not runtime:
                    raise PaperExecutionDecisionOriginError(
                        "live decision origin runtime binding changed"
                    )
                ledger = getattr(owner, "decision_ledger", None)
                if type(ledger) is not JsonlDecisionLedger:
                    raise PaperExecutionDecisionOriginError(
                        "live decision origin requires exact JsonlDecisionLedger authority"
                    )
                if local.get("decision_id") != decision_id:
                    raise PaperExecutionDecisionOriginError(
                        "live decision frame identity does not match execution plan"
                    )
                ledgers[id(ledger)] = ledger

            elif frame.f_code is _paper_value_authority._ORIGINAL_ON_MARKET_EVENT.__code__:
                context = local.get("context")
                agent = local.get("self")
                event = local.get("event")
                if type(agent) is not _paper_value_authority.PaperValueAgent:
                    raise PaperExecutionDecisionOriginError(
                        "paper-value origin requires exact PaperValueAgent authority"
                    )
                if event is None or agent._material_action_id(context, event) != decision_id:
                    raise PaperExecutionDecisionOriginError(
                        "paper-value decision frame identity does not match execution plan"
                    )
                ledger = _exact_context_ledger(runtime, context)
                ledgers[id(ledger)] = ledger

            elif frame.f_code is _paper_value_authority._resume_durable_paper_value.__code__:
                context = local.get("context")
                record = local.get("record")
                if (
                    type(record) is not _paper_value_authority.DecisionRecord
                    or record.decision_id != decision_id
                ):
                    raise PaperExecutionDecisionOriginError(
                        "paper-value recovery frame identity does not match execution plan"
                    )
                ledger = _exact_context_ledger(runtime, context)
                ledgers[id(ledger)] = ledger
            frame = frame.f_back
    finally:
        del current
        try:
            del frame
        except UnboundLocalError:
            pass

    if not ledgers:
        return None
    if len(ledgers) != 1:
        raise PaperExecutionDecisionOriginError(
            "product execution resolved multiple DecisionLedger authorities"
        )
    return verified_decision_origin(next(iter(ledgers.values())), decision_id)


def _execute_with_product_decision_origin(
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
    decision_id = getattr(getattr(prepared, "execution_plan", None), "decision_id", None)
    if type(decision_id) is not str or not decision_id:
        return _ORIGINAL_RUNTIME_EXECUTE(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )

    origin = _resolve_product_origin_from_stack(self, decision_id)
    if origin is None:
        # Keep isolated execution-reality/replay tests usable. Such runs carry no
        # product-origin authority and downstream product admission must reject them.
        return _ORIGINAL_RUNTIME_EXECUTE(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )
    if trigger_id != origin.decision_id:
        raise PaperExecutionAdoptionError(
            "product execution trigger does not match verified DecisionLedger origin"
        )

    token = _DECISION_ORIGIN.set(origin)
    try:
        return _ORIGINAL_RUNTIME_EXECUTE(
            self,
            prepared=prepared,
            trigger_id=trigger_id,
            started_at=started_at,
            materialize_exposure=materialize_exposure,
            observations=observations,
            evidence_registry=evidence_registry,
            suspended_action_ids=suspended_action_ids,
        )
    finally:
        _DECISION_ORIGIN.reset(token)


def _install() -> None:
    if getattr(PaperExecutionLedger, "_autosport_decision_origin_installed", False):
        return
    PaperExecutionLedger.events = _events_with_legacy_load_mask
    PaperExecutionLedger.reserve_run = _reserve_run_with_decision_origin
    PaperExecutionLedger.load_run = _load_run_with_decision_origin
    PaperExecutionLedger.reservation_decision_origin = _reservation_decision_origin
    PaperExecutionLedger._autosport_decision_origin_installed = True

    PaperExecutionAdoptionRuntime.execute = _execute_with_product_decision_origin
    PaperExecutionAdoptionRuntime._autosport_decision_origin_installed = True


_install()


__all__ = [
    "DecisionRecordOrigin",
    "PaperExecutionDecisionOriginError",
    "verified_decision_origin",
]
