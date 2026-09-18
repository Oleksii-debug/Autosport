"""Causal ingress boundary for externally requested research work.

``ResearchSupervisor`` is the only durable owner of research runs and checkpoints.
This module intentionally owns neither a scheduler nor an inbox/outbox store: an
external scheduler, UI, market observer or recovery process presents one immutable
event, and the adapter derives the canonical supervisor trigger from it.  Exact
redelivery therefore collapses through ``ResearchSupervisor.accept_trigger``;
changed content for the same external source event fails closed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from .research_supervisor import ResearchSupervisor, ResearchSupervisorError, ResearchTrigger


TRIGGER_ADAPTER_SCHEMA = "autosport.research_trigger_adapter"
TRIGGER_ADAPTER_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


class ResearchTriggerAdapterError(ResearchSupervisorError):
    """An external event cannot be causally bound to a research run."""


class ResearchTriggerSource(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULE = "SCHEDULE"
    MARKET_OBSERVATION = "MARKET_OBSERVATION"
    RECOVERY = "RECOVERY"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name)
    if text != text.lower():
        raise ValueError(f"{name} must be lowercase canonical SHA-256 hex")
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise ValueError(f"{name} must be canonical SHA-256 hex")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExternalResearchTrigger:
    """One immutable externally observed reason to begin a research run.

    ``source_event_id`` denotes an event in a source namespace, not a caller-made
    run id.  Its identity intentionally excludes mutable request fields, so a
    repeated event with different question, timing, evidence, budget or deadline
    reaches the same supervisor ``trigger_id`` and is rejected as a conflict.
    """

    source_kind: ResearchTriggerSource
    source_scope: str
    source_event_id: str
    question_id: str
    question_record_sha256: str
    source_evidence_sha256: str
    source_observed_at: str
    requested_at: str
    budget_units: int
    deadline_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, ResearchTriggerSource):
            raise ValueError("source_kind must be a ResearchTriggerSource")
        for name in ("source_scope", "source_event_id", "question_id"):
            _text(getattr(self, name), name)
        for name in ("question_record_sha256", "source_evidence_sha256"):
            _sha256(getattr(self, name), name)
        observed = _instant(self.source_observed_at, "source_observed_at")
        requested = _instant(self.requested_at, "requested_at")
        if observed > requested:
            raise ValueError("source_observed_at cannot follow requested_at")
        _positive_integer(self.budget_units, "budget_units")
        if self.deadline_at is not None and _instant(self.deadline_at, "deadline_at") < requested:
            raise ValueError("deadline_at cannot precede requested_at")

    def source_identity_payload(self) -> dict[str, Any]:
        return {
            "schema": TRIGGER_ADAPTER_SCHEMA,
            "schema_version": TRIGGER_ADAPTER_SCHEMA_VERSION,
            "source_kind": self.source_kind.value,
            "source_scope": self.source_scope,
            "source_event_id": self.source_event_id,
        }

    @property
    def source_event_identity_sha256(self) -> str:
        return _digest(self.source_identity_payload())

    def canonical_payload(self) -> dict[str, Any]:
        return {
            **self.source_identity_payload(),
            "question_id": self.question_id,
            "question_record_sha256": _sha256(
                self.question_record_sha256, "question_record_sha256"
            ),
            "source_evidence_sha256": _sha256(
                self.source_evidence_sha256, "source_evidence_sha256"
            ),
            "source_observed_at": _timestamp(self.source_observed_at, "source_observed_at"),
            "requested_at": _timestamp(self.requested_at, "requested_at"),
            "budget_units": self.budget_units,
            "deadline_at": (
                None
                if self.deadline_at is None
                else _timestamp(self.deadline_at, "deadline_at")
            ),
        }

    @property
    def source_event_sha256(self) -> str:
        return _digest(self.canonical_payload())

    @property
    def supervisor_trigger_id(self) -> str:
        return f"{self.supervisor_trigger_prefix}{self.source_event_sha256}"

    @property
    def supervisor_trigger_prefix(self) -> str:
        """Namespace reserved by one immutable external source-event identity."""

        return f"external-research:{self.source_event_identity_sha256}:"

    def to_research_trigger(self) -> ResearchTrigger:
        payload = self.canonical_payload()
        return ResearchTrigger(
            trigger_id=self.supervisor_trigger_id,
            question_id=payload["question_id"],
            requested_at=payload["requested_at"],
            budget_units=payload["budget_units"],
            deadline_at=payload["deadline_at"],
        )


@dataclass(frozen=True, slots=True)
class ResearchTriggerReceipt:
    """Deterministic acknowledgement of a supervisor-owned trigger acceptance."""

    source_event_identity_sha256: str
    source_event_sha256: str
    supervisor_trigger_id: str
    supervisor_trigger_sha256: str
    run_id: str
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "source_event_identity_sha256",
            "source_event_sha256",
            "supervisor_trigger_sha256",
            "run_id",
            "checkpoint_sha256",
        ):
            _sha256(getattr(self, name), name)
        _text(self.supervisor_trigger_id, "supervisor_trigger_id")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema": TRIGGER_ADAPTER_SCHEMA,
            "schema_version": TRIGGER_ADAPTER_SCHEMA_VERSION,
            "source_event_identity_sha256": self.source_event_identity_sha256,
            "source_event_sha256": self.source_event_sha256,
            "supervisor_trigger_id": self.supervisor_trigger_id,
            "supervisor_trigger_sha256": self.supervisor_trigger_sha256,
            "run_id": self.run_id,
            "checkpoint_sha256": self.checkpoint_sha256,
        }

    @property
    def receipt_sha256(self) -> str:
        return _digest(self.canonical_payload())


class ResearchTriggerAdapter:
    """Validate external provenance before delegating once to ResearchSupervisor."""

    def __init__(self, supervisor: ResearchSupervisor) -> None:
        if not isinstance(supervisor, ResearchSupervisor):
            raise TypeError("supervisor must be ResearchSupervisor")
        self.supervisor = supervisor

    def _validate_event(self, event: ExternalResearchTrigger) -> None:
        registry = self.supervisor.scientific_registry
        question = registry.get("ResearchQuestion", event.question_id)
        if question is None:
            raise ResearchTriggerAdapterError(
                f"external trigger references missing ResearchQuestion:{event.question_id}"
            )
        if question.record_sha256 != event.question_record_sha256:
            raise ResearchTriggerAdapterError("external trigger question record hash mismatch")
        expected_source = question.payload.get("source_sha256")
        if expected_source != event.source_evidence_sha256:
            raise ResearchTriggerAdapterError(
                "external trigger source evidence is not bound to the research question"
            )
        if _instant(question.available_at, "ResearchQuestion.available_at") > _instant(
            event.requested_at, "requested_at"
        ):
            raise ResearchTriggerAdapterError(
                "external trigger cannot reference a research question from the future"
            )

    def accept(self, event: ExternalResearchTrigger) -> ResearchTriggerReceipt:
        if not isinstance(event, ExternalResearchTrigger):
            raise TypeError("event must be ExternalResearchTrigger")
        self._validate_event(event)
        trigger = event.to_research_trigger()
        snapshot = self.supervisor.accept_trigger(
            trigger,
            exclusive_trigger_prefix=event.supervisor_trigger_prefix,
        )
        if (
            snapshot.run_id != trigger.run_id
            or snapshot.trigger_id != trigger.trigger_id
            or snapshot.question_id != trigger.question_id
        ):
            raise ResearchTriggerAdapterError(
                "research supervisor returned a trigger identity inconsistent with the event"
            )
        return ResearchTriggerReceipt(
            source_event_identity_sha256=event.source_event_identity_sha256,
            source_event_sha256=event.source_event_sha256,
            supervisor_trigger_id=trigger.trigger_id,
            supervisor_trigger_sha256=trigger.trigger_sha256,
            run_id=snapshot.run_id,
            checkpoint_sha256=snapshot.checkpoint_sha256,
        )
