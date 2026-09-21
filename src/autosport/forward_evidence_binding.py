"""Cross-layer causal binding for forward scientific evidence.

This module composes existing authorities instead of minting a replacement source of
truth.  A ForwardCapturePlan must already be immutable in ScientificRegistry and
durably witnessed in the Decision Ledger prefix captured by BoundPreEvaluationSession.
Only then may the exact provider-member session be bound to a later EvaluationBundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping

from .decision_ledger import JsonlDecisionLedger, DecisionRecord
from .pre_evaluation_binding import BoundPreEvaluationSession
from .scientific_registry import ForwardCapturePlan, ScientificRegistry


AUTHORITY_FAMILY = "scientific.forward-evidence-cross-layer-binding-v1"
CAPTURE_PLAN_ACTION = "PRECOMMIT_FORWARD_CAPTURE_PLAN"
CAPTURE_PLAN_AGENT = "scientific-forward-evidence"
SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_json(value)).hexdigest()


def _text(name: str, value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{name} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _sha(name: str, value: object) -> str:
    raw = _text(name, value).lower()
    if len(raw) != 64 or any(ch not in _HEX for ch in raw):
        raise ValueError(f"{name} must be canonical SHA-256 hex")
    return raw


def _instant(value: object, name: str) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _epoch_ns(value: object, name: str) -> int:
    instant = _instant(value, name)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = instant - epoch
    return (
        delta.days * 86_400 * 1_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


def _plan_ledger_payload(plan: ForwardCapturePlan, record_sha256: str) -> dict[str, str]:
    return {
        "capture_plan_id": plan.capture_plan_id,
        "capture_plan_record_sha256": _sha(
            "capture_plan_record_sha256", record_sha256
        ),
        "capture_plan_sha256": plan.capture_plan_sha256,
        "session_id": plan.session_id,
        "campaign_id": plan.campaign_id,
        "research_protocol_id": plan.research_protocol_id,
        "evaluation_bundle_id": plan.evaluation_bundle_id,
        "protocol_sha256": plan.protocol_sha256.lower(),
    }


def register_forward_capture_plan(
    *,
    registry: ScientificRegistry,
    ledger: JsonlDecisionLedger,
    plan: ForwardCapturePlan,
) -> str:
    """Durably register one pre-outcome plan and witness its exact registry bytes.

    The registry append happens first.  A crash between the two writes leaves an
    unusable plan, not an authorized one: the cross-layer verifier requires the
    matching Decision Ledger witness to be present in the exact prefix later sealed
    into BoundPreEvaluationSession.
    """

    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")
    if not isinstance(plan, ForwardCapturePlan):
        raise TypeError("plan must be ForwardCapturePlan")

    record_sha256 = registry.append(plan)
    expected_payload = _plan_ledger_payload(plan, record_sha256)

    matches = [
        record
        for record in ledger.verified_records()
        if record.action == CAPTURE_PLAN_ACTION
        and record.payload.get("capture_plan_id") == plan.capture_plan_id
    ]
    if matches:
        if len(matches) != 1:
            raise ValueError("duplicate forward capture plan witness in Decision Ledger")
        existing = matches[0]
        if (
            existing.replay_run_id != plan.run_id
            or existing.agent != CAPTURE_PLAN_AGENT
            or existing.context_hash != plan.capture_plan_sha256
            or dict(existing.payload) != expected_payload
        ):
            raise ValueError("conflicting forward capture plan witness in Decision Ledger")
        return record_sha256

    ledger.append(
        DecisionRecord(
            replay_run_id=plan.run_id,
            agent=CAPTURE_PLAN_AGENT,
            observed_ts=plan.created_at,
            action=CAPTURE_PLAN_ACTION,
            payload=expected_payload,
            context_hash=plan.capture_plan_sha256,
            decision_id=f"forward-capture-plan:{plan.capture_plan_id}",
            recorded_at=plan.created_at,
        )
    )
    return record_sha256


def _verified_bound_prefix(
    ledger: JsonlDecisionLedger,
    bound: BoundPreEvaluationSession,
) -> bytes:
    expected_sha = bound.decision_ledger_prefix_sha256
    expected_count = bound.decision_ledger_prefix_record_count
    if expected_sha is None or expected_count is None:
        raise ValueError(
            "forward evidence requires BoundPreEvaluationSession with Decision Ledger prefix"
        )
    snapshot = ledger.verified_snapshot()
    lines = snapshot.payload.splitlines(keepends=True)
    if expected_count > len(lines):
        raise ValueError("bound Decision Ledger prefix record_count exceeds durable ledger")
    prefix = b"".join(lines[:expected_count])
    JsonlDecisionLedger._verify_bytes(prefix)
    if sha256(prefix).hexdigest() != _sha(
        "decision_ledger_prefix_sha256", expected_sha
    ):
        raise ValueError("Decision Ledger no longer matches bound pre-evaluation prefix")
    return prefix


def _require_plan_witness(
    *,
    prefix: bytes,
    plan: ForwardCapturePlan,
    record_sha256: str,
) -> None:
    expected = _plan_ledger_payload(plan, record_sha256)
    matches: list[Mapping[str, Any]] = []
    for line in prefix.decode("utf-8").splitlines():
        envelope = json.loads(line)
        record = envelope["record"]
        if (
            record.get("action") == CAPTURE_PLAN_ACTION
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("capture_plan_id") == plan.capture_plan_id
        ):
            matches.append(record)
    if len(matches) != 1:
        raise ValueError(
            "bound Decision Ledger prefix requires exactly one forward capture plan witness"
        )
    record = matches[0]
    if record.get("replay_run_id") != plan.run_id:
        raise ValueError("forward capture plan witness run_id mismatch")
    if record.get("agent") != CAPTURE_PLAN_AGENT:
        raise ValueError("forward capture plan witness agent mismatch")
    if record.get("context_hash") != plan.capture_plan_sha256:
        raise ValueError("forward capture plan witness context hash mismatch")
    if record.get("payload") != expected:
        raise ValueError("forward capture plan witness payload mismatch")


@dataclass(frozen=True, slots=True)
class ForwardEvidenceCrossLayerBinding:
    capture_plan_id: str
    capture_plan_record_sha256: str
    run_id: str
    campaign_id: str
    research_protocol_id: str
    protocol_sha256: str
    session_id: str
    bound_session_authority_sha256: str
    provider_evidence_sha256: str
    evaluation_bundle_id: str
    evaluation_bundle_record_sha256: str
    member_authority_sha256: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name in (
            "capture_plan_id",
            "run_id",
            "campaign_id",
            "research_protocol_id",
            "session_id",
            "evaluation_bundle_id",
        ):
            _text(name, getattr(self, name))
        for name in (
            "capture_plan_record_sha256",
            "protocol_sha256",
            "bound_session_authority_sha256",
            "provider_evidence_sha256",
            "evaluation_bundle_record_sha256",
        ):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        if (
            not isinstance(self.member_authority_sha256, tuple)
            or not self.member_authority_sha256
        ):
            raise ValueError("member_authority_sha256 must be a non-empty tuple")
        last = ""
        seen: set[str] = set()
        for item in self.member_authority_sha256:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise ValueError("member authority entry must be (row_key, sha256)")
            row_key = _text("row_key", item[0])
            _sha("member_authority_sha256", item[1])
            if row_key in seen or row_key <= last:
                raise ValueError("member authority rows must be unique canonical order")
            seen.add(row_key)
            last = row_key

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "authority_family": AUTHORITY_FAMILY,
            "capture_plan_id": self.capture_plan_id,
            "capture_plan_record_sha256": self.capture_plan_record_sha256,
            "run_id": self.run_id,
            "campaign_id": self.campaign_id,
            "research_protocol_id": self.research_protocol_id,
            "protocol_sha256": self.protocol_sha256,
            "session_id": self.session_id,
            "bound_session_authority_sha256": self.bound_session_authority_sha256,
            "provider_evidence_sha256": self.provider_evidence_sha256,
            "evaluation_bundle_id": self.evaluation_bundle_id,
            "evaluation_bundle_record_sha256": self.evaluation_bundle_record_sha256,
            "member_authority_sha256": [
                [row_key, digest]
                for row_key, digest in self.member_authority_sha256
            ],
        }

    @property
    def authority_sha256(self) -> str:
        return _digest(self.to_payload())


def bind_forward_evidence(
    *,
    registry: ScientificRegistry,
    ledger: JsonlDecisionLedger,
    capture_plan_id: str,
    bound_session: BoundPreEvaluationSession,
    evaluation_bundle_id: str,
) -> ForwardEvidenceCrossLayerBinding:
    """Bind one precommitted plan to one exact forward session and later evaluation."""

    if not isinstance(registry, ScientificRegistry):
        raise TypeError("registry must be ScientificRegistry")
    if not isinstance(ledger, JsonlDecisionLedger):
        raise TypeError("ledger must be JsonlDecisionLedger")
    if not isinstance(bound_session, BoundPreEvaluationSession):
        raise TypeError("bound_session must be BoundPreEvaluationSession")

    plan_entry = registry.get(
        "ForwardCapturePlan", _text("capture_plan_id", capture_plan_id)
    )
    if plan_entry is None:
        raise ValueError("forward capture plan is missing from ScientificRegistry")
    plan = ForwardCapturePlan.from_payload(plan_entry.payload)
    if plan.record_id != plan_entry.record_id or plan.available_at != plan_entry.available_at:
        raise ValueError("forward capture plan registry identity mismatch")

    protocol_entry = registry.get("ResearchProtocol", plan.research_protocol_id)
    if protocol_entry is None:
        raise ValueError("forward capture plan ResearchProtocol is missing")
    if protocol_entry.payload.get("protocol_sha256") != plan.protocol_sha256.lower():
        raise ValueError("forward capture plan ResearchProtocol hash mismatch")
    if not registry.causal_precedes(
        "ResearchProtocol",
        plan.research_protocol_id,
        "ForwardCapturePlan",
        plan.capture_plan_id,
    ):
        raise ValueError("ResearchProtocol must durably precede ForwardCapturePlan")

    requested_evaluation_bundle_id = _text("evaluation_bundle_id", evaluation_bundle_id)
    if requested_evaluation_bundle_id != plan.evaluation_bundle_id:
        raise ValueError("forward evidence evaluation_bundle_id mismatch")
    evaluation_entry = registry.get(
        "EvaluationBundle", requested_evaluation_bundle_id
    )
    if evaluation_entry is None:
        raise ValueError("forward evaluation bundle is missing from ScientificRegistry")
    if not registry.causal_precedes(
        "ForwardCapturePlan",
        plan.capture_plan_id,
        "EvaluationBundle",
        evaluation_bundle_id,
    ):
        raise ValueError("ForwardCapturePlan must durably precede EvaluationBundle")
    if evaluation_entry.payload.get("protocol_sha256") != plan.protocol_sha256.lower():
        raise ValueError("evaluation bundle protocol_sha256 mismatch")
    if _instant(
        evaluation_entry.available_at, "EvaluationBundle.available_at"
    ) < _instant(plan.window_close_utc, "ForwardCapturePlan.window_close_utc"):
        raise ValueError("evaluation bundle became available before capture window closed")

    context = bound_session.context
    if context.session_id != plan.session_id:
        raise ValueError("forward evidence session_id mismatch")
    if context.campaign_id != plan.campaign_id:
        raise ValueError("forward evidence campaign_id mismatch")
    if context.research_protocol_id != plan.research_protocol_id:
        raise ValueError("forward evidence research_protocol_id mismatch")
    if context.protocol_sha256 != plan.protocol_sha256.lower():
        raise ValueError("forward evidence protocol_sha256 mismatch")

    prefix = _verified_bound_prefix(ledger, bound_session)
    _require_plan_witness(
        prefix=prefix,
        plan=plan,
        record_sha256=plan_entry.record_sha256,
    )

    planned_ids = {slot.slot_id for slot in plan.slots}
    member_ids = {member.row_key for member in bound_session.members}
    if planned_ids != member_ids:
        raise ValueError(
            "forward capture plan slot membership must exactly equal bound provider members"
        )

    schedule = {slot.slot_id: slot for slot in plan.slots}
    for member in bound_session.members:
        slot = bound_session.resolve_slot(member.row_key)
        planned = schedule[member.row_key]
        scheduled_ns = _epoch_ns(planned.scheduled_at_utc, "scheduled_at_utc")
        latest_ns = scheduled_ns + planned.max_lateness_s * 1_000_000_000
        if not scheduled_ns <= slot.evaluated_at_ns <= latest_ns:
            raise ValueError(
                f"pre-evaluation time is outside precommitted capture interval for {member.row_key}"
            )
        if slot.facts is None:
            raise ValueError(
                f"forward evidence is incomplete for planned slot {member.row_key}"
            )
        if not scheduled_ns <= slot.facts.observed_at_ns <= slot.evaluated_at_ns:
            raise ValueError(
                f"observation time is outside precommitted capture interval for {member.row_key}"
            )

    binding = ForwardEvidenceCrossLayerBinding(
        capture_plan_id=plan.capture_plan_id,
        capture_plan_record_sha256=plan_entry.record_sha256,
        run_id=plan.run_id,
        campaign_id=plan.campaign_id,
        research_protocol_id=plan.research_protocol_id,
        protocol_sha256=plan.protocol_sha256,
        session_id=context.session_id,
        bound_session_authority_sha256=bound_session.authority_digest,
        provider_evidence_sha256=context.provider_evidence_sha256,
        evaluation_bundle_id=evaluation_entry.record_id,
        evaluation_bundle_record_sha256=evaluation_entry.record_sha256,
        member_authority_sha256=bound_session.member_authority_sha256,
    )
    # Construction is the final canonicalization check; digest is intentionally
    # computed here so callers cannot claim a binding without materializing it.
    binding.authority_sha256
    return binding
