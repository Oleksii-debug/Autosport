"""Durable manual-attestation ledger for physical Windows + NVDA review.

This module records explicit human-review decisions for an exact transcript/candidate.
It deliberately does not promote HUMAN_TESTED or NVDA_VERIFIED. Consumers that
eventually own release truth must re-resolve this ledger and apply their separate
manual reviewer-identity/trust policy instead of trusting caller booleans or refs.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from .nvda_human_acceptance import (
    STATUS_STRUCTURALLY_COMPLETE,
    NvdaHumanAcceptanceError,
    validate_human_nvda_acceptance_transcript,
    verify_human_nvda_acceptance_structural_result,
)
from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
)


SCHEMA_VERSION = 1
ANCHOR_SCHEMA_VERSION = 1
PROTOCOL_VERSION = "autosport-physical-nvda-manual-review-v1"
EVENT_TYPE = "MANUAL_NVDA_DECISION"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_TEXT_LENGTH = 16_384
_MAX_LIVE_RESOLUTIONS = 256

_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "event_type",
        "sequence",
        "previous_sha256",
        "payload",
        "event_sha256",
    }
)
_PAYLOAD_KEYS = frozenset(
    {
        "decision_id",
        "decision",
        "reviewer_ref",
        "reviewer_attestation_sha256",
        "reviewed_at",
        "protocol_version",
        "transcript_sha256",
        "artifact_sha256",
        "source_sha",
        "structural_status",
        "structural_human_tester_attestation_sha256",
        "windows_version",
        "nvda_version",
    }
)
_ANCHOR_KEYS = frozenset(
    {
        "anchor_schema_version",
        "ledger_schema_version",
        "event_count",
        "ledger_root_sha256",
        "anchor_sha256",
    }
)


class NvdaManualAcceptanceError(RuntimeError):
    """Base error for durable physical-NVDA manual-review evidence."""


class NvdaManualAcceptanceIntegrityError(NvdaManualAcceptanceError):
    """Raised when durable manual-review evidence is malformed or inconsistent."""


class NvdaManualAcceptanceStateError(NvdaManualAcceptanceError):
    """Raised when a manual-review operation cannot safely proceed."""


class ManualNvdaDecision(str, Enum):
    ACCEPT_PHYSICAL_NVDA = "ACCEPT_PHYSICAL_NVDA"
    REJECT_PHYSICAL_NVDA = "REJECT_PHYSICAL_NVDA"


@dataclass(frozen=True, slots=True)
class ManualNvdaDecisionRecord:
    """One validated record re-resolved from the durable ledger.

    This object is descriptive, not a transferable truth token. Positive product
    truth must be re-resolved from the ledger for the exact candidate.
    """

    sequence: int
    decision_id: str
    decision: ManualNvdaDecision
    reviewer_ref: str
    reviewer_attestation_sha256: str
    reviewed_at: str
    protocol_version: str
    transcript_sha256: str
    artifact_sha256: str
    source_sha: str
    structural_status: str
    structural_human_tester_attestation_sha256: str
    windows_version: str
    nvda_version: str
    event_sha256: str


@dataclass(frozen=True, slots=True, init=False)
class ManualNvdaAcceptanceResolution:
    """Resolver-issued current manual decision for one exact candidate.

    Recording an explicit manual ACCEPT is not, by itself, proof of reviewer
    identity. Until a separate canonical release authority composes that trust
    boundary, HUMAN_TESTED and NVDA_VERIFIED remain hard false here.
    """

    record: ManualNvdaDecisionRecord
    accepted_manual_decision: bool
    reviewer_identity_verified: bool = field(default=False, init=False)
    human_tested: bool = field(default=False, init=False)
    nvda_verified: bool = field(default=False, init=False)
    manual_truth_promotion_required: bool = field(default=True, init=False)
    real_money_execution: bool = field(default=False, init=False)
    whole_product_complete: bool = field(default=False, init=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NvdaManualAcceptanceStateError(
            "ManualNvdaAcceptanceResolution is resolver-issued only"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ManualNvdaAcceptanceResolution may not be subclassed")


_ISSUED_RESOLUTIONS: OrderedDict[
    int, tuple[ManualNvdaAcceptanceResolution, str]
] = OrderedDict()


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise NvdaManualAcceptanceIntegrityError(
            "value is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_json_object(raw: str, *, what: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise NvdaManualAcceptanceIntegrityError(
                    f"duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                NvdaManualAcceptanceIntegrityError(
                    f"non-finite JSON constant {token!r}"
                )
            ),
        )
    except NvdaManualAcceptanceIntegrityError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NvdaManualAcceptanceIntegrityError(f"invalid {what} JSON") from exc
    if type(value) is not dict:
        raise NvdaManualAcceptanceIntegrityError(f"{what} must be a JSON object")
    return value


def _require_text(name: str, value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise NvdaManualAcceptanceStateError(
            f"{name} must be non-empty canonical text"
        )
    if len(value) > _MAX_TEXT_LENGTH:
        raise NvdaManualAcceptanceStateError(f"{name} is too long")
    if "\x00" in value:
        raise NvdaManualAcceptanceStateError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise NvdaManualAcceptanceStateError(
            f"{name} must be UTF-8 encodable"
        ) from exc
    return value


def _require_sha256(name: str, value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise NvdaManualAcceptanceStateError(
            f"{name} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _require_git_commit_sha(name: str, value: object) -> str:
    if type(value) is not str or _GIT_COMMIT_SHA_RE.fullmatch(value) is None:
        raise NvdaManualAcceptanceStateError(
            f"{name} must be exactly 40 lowercase hexadecimal Git commit characters"
        )
    return value


def _require_canonical_utc(name: str, value: object) -> str:
    raw = _require_text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NvdaManualAcceptanceStateError(
            f"{name} must be canonical UTC ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise NvdaManualAcceptanceStateError(
            f"{name} must be timezone-aware canonical UTC"
        )
    canonical = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if raw != canonical:
        raise NvdaManualAcceptanceStateError(
            f"{name} must use canonical UTC +00:00 with microseconds"
        )
    return raw


def _require_protocol(value: object) -> str:
    protocol = _require_text("protocol_version", value)
    if protocol != PROTOCOL_VERSION:
        raise NvdaManualAcceptanceStateError(
            f"protocol_version must equal {PROTOCOL_VERSION}"
        )
    return protocol


def _structural_result(
    transcript: object,
    *,
    expected_artifact_sha256: str,
    expected_source_sha: str,
):
    artifact = _require_sha256(
        "expected_artifact_sha256", expected_artifact_sha256
    )
    source = _require_git_commit_sha("expected_source_sha", expected_source_sha)
    try:
        result = validate_human_nvda_acceptance_transcript(
            transcript,
            expected_artifact_sha256=artifact,
            expected_source_sha=source,
        )
        return verify_human_nvda_acceptance_structural_result(
            result,
            expected_artifact_sha256=artifact,
            expected_source_sha=source,
        )
    except NvdaHumanAcceptanceError as exc:
        raise NvdaManualAcceptanceStateError(
            "physical NVDA transcript did not pass canonical structural validation"
        ) from exc


def _decision_payload(
    *,
    structural,
    decision: ManualNvdaDecision,
    reviewer_ref: str,
    reviewer_attestation: str,
    reviewed_at: str,
    protocol_version: str,
) -> dict[str, Any]:
    if not isinstance(decision, ManualNvdaDecision):
        raise TypeError("decision must be ManualNvdaDecision")
    reviewer = _require_text("reviewer_ref", reviewer_ref)
    attestation = _require_text("reviewer_attestation", reviewer_attestation)
    reviewed = _require_canonical_utc("reviewed_at", reviewed_at)
    protocol = _require_protocol(protocol_version)
    body = {
        "decision": decision.value,
        "reviewer_ref": reviewer,
        "reviewer_attestation_sha256": hashlib.sha256(
            attestation.encode("utf-8")
        ).hexdigest(),
        "reviewed_at": reviewed,
        "protocol_version": protocol,
        "transcript_sha256": structural.transcript_sha256,
        "artifact_sha256": structural.artifact_sha256,
        "source_sha": structural.source_sha,
        "structural_status": structural.status,
        "structural_human_tester_attestation_sha256": (
            structural.human_tester_attestation_sha256
        ),
        "windows_version": structural.windows_version,
        "nvda_version": structural.nvda_version,
    }
    decision_id = "manual-nvda-decision-v1-" + _digest(
        {"schema": "autosport.manual_nvda_decision", **body}
    )
    return {"decision_id": decision_id, **body}


def _record_from_event(event: dict[str, Any]) -> ManualNvdaDecisionRecord:
    if frozenset(event) != _EVENT_KEYS:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA ledger event schema is invalid"
        )
    if event["schema_version"] != SCHEMA_VERSION:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA ledger schema version is invalid"
        )
    if event["event_type"] != EVENT_TYPE:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA ledger event type is invalid"
        )
    if type(event["sequence"]) is not int or event["sequence"] < 0:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA ledger event sequence is invalid"
        )
    previous = event["previous_sha256"]
    if previous is not None and (
        type(previous) is not str or _SHA256_RE.fullmatch(previous) is None
    ):
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA ledger predecessor digest is invalid"
        )
    payload = event["payload"]
    if type(payload) is not dict or frozenset(payload) != _PAYLOAD_KEYS:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA decision payload schema is invalid"
        )

    try:
        decision_id = _require_text("decision_id", payload["decision_id"])
        if not decision_id.startswith("manual-nvda-decision-v1-"):
            raise NvdaManualAcceptanceStateError(
                "decision_id has an invalid namespace"
            )
        digest_part = decision_id.removeprefix("manual-nvda-decision-v1-")
        _require_sha256("decision_id digest", digest_part)
        decision = ManualNvdaDecision(payload["decision"])
        reviewer_ref = _require_text("reviewer_ref", payload["reviewer_ref"])
        reviewer_attestation_sha256 = _require_sha256(
            "reviewer_attestation_sha256",
            payload["reviewer_attestation_sha256"],
        )
        reviewed_at = _require_canonical_utc(
            "reviewed_at", payload["reviewed_at"]
        )
        protocol_version = _require_protocol(payload["protocol_version"])
        transcript_sha256 = _require_sha256(
            "transcript_sha256", payload["transcript_sha256"]
        )
        artifact_sha256 = _require_sha256(
            "artifact_sha256", payload["artifact_sha256"]
        )
        source_sha = _require_git_commit_sha("source_sha", payload["source_sha"])
        if payload["structural_status"] != STATUS_STRUCTURALLY_COMPLETE:
            raise NvdaManualAcceptanceStateError(
                "structural_status is not canonical"
            )
        structural_attestation = _require_sha256(
            "structural_human_tester_attestation_sha256",
            payload["structural_human_tester_attestation_sha256"],
        )
        windows_version = _require_text(
            "windows_version", payload["windows_version"]
        )
        nvda_version = _require_text("nvda_version", payload["nvda_version"])
    except (KeyError, ValueError, TypeError, NvdaManualAcceptanceStateError) as exc:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA decision payload is invalid"
        ) from exc

    expected_decision_id = "manual-nvda-decision-v1-" + _digest(
        {
            "schema": "autosport.manual_nvda_decision",
            **{key: payload[key] for key in payload if key != "decision_id"},
        }
    )
    if decision_id != expected_decision_id:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA decision identity does not match payload"
        )

    body = {key: event[key] for key in event if key != "event_sha256"}
    expected_event_sha = _digest(body)
    event_sha = event["event_sha256"]
    if type(event_sha) is not str or event_sha != expected_event_sha:
        raise NvdaManualAcceptanceIntegrityError(
            "manual NVDA ledger event digest mismatch"
        )

    return ManualNvdaDecisionRecord(
        sequence=event["sequence"],
        decision_id=decision_id,
        decision=decision,
        reviewer_ref=reviewer_ref,
        reviewer_attestation_sha256=reviewer_attestation_sha256,
        reviewed_at=reviewed_at,
        protocol_version=protocol_version,
        transcript_sha256=transcript_sha256,
        artifact_sha256=artifact_sha256,
        source_sha=source_sha,
        structural_status=payload["structural_status"],
        structural_human_tester_attestation_sha256=structural_attestation,
        windows_version=windows_version,
        nvda_version=nvda_version,
        event_sha256=event_sha,
    )


def _resolution_fingerprint(resolution: ManualNvdaAcceptanceResolution) -> str:
    if type(resolution) is not ManualNvdaAcceptanceResolution:
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution must be the exact canonical type"
        )
    record = resolution.record
    if type(record) is not ManualNvdaDecisionRecord:
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution record type is invalid"
        )
    if (
        type(resolution.accepted_manual_decision) is not bool
        or resolution.accepted_manual_decision
        is not (record.decision is ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA)
    ):
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution decision projection is invalid"
        )
    hard_false = {
        "reviewer_identity_verified": resolution.reviewer_identity_verified,
        "human_tested": resolution.human_tested,
        "nvda_verified": resolution.nvda_verified,
        "real_money_execution": resolution.real_money_execution,
        "whole_product_complete": resolution.whole_product_complete,
    }
    if any(type(value) is not bool or value is not False for value in hard_false.values()):
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution cannot promote protected truth"
        )
    if (
        type(resolution.manual_truth_promotion_required) is not bool
        or resolution.manual_truth_promotion_required is not True
    ):
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution must require separate truth promotion"
        )
    return _digest(
        {
            "record": {
                "sequence": record.sequence,
                "decision_id": record.decision_id,
                "decision": record.decision.value,
                "reviewer_ref": record.reviewer_ref,
                "reviewer_attestation_sha256": record.reviewer_attestation_sha256,
                "reviewed_at": record.reviewed_at,
                "protocol_version": record.protocol_version,
                "transcript_sha256": record.transcript_sha256,
                "artifact_sha256": record.artifact_sha256,
                "source_sha": record.source_sha,
                "structural_status": record.structural_status,
                "structural_human_tester_attestation_sha256": (
                    record.structural_human_tester_attestation_sha256
                ),
                "windows_version": record.windows_version,
                "nvda_version": record.nvda_version,
                "event_sha256": record.event_sha256,
            },
            "accepted_manual_decision": resolution.accepted_manual_decision,
            **hard_false,
            "manual_truth_promotion_required": (
                resolution.manual_truth_promotion_required
            ),
        }
    )


def verify_manual_nvda_acceptance_resolution(
    resolution: object,
    *,
    expected_artifact_sha256: str,
    expected_source_sha: str,
    expected_transcript_sha256: str,
) -> ManualNvdaAcceptanceResolution:
    """Verify one live resolver-issued projection for an exact candidate."""

    artifact = _require_sha256(
        "expected_artifact_sha256", expected_artifact_sha256
    )
    source_sha = _require_git_commit_sha(
        "expected_source_sha", expected_source_sha
    )
    transcript_sha = _require_sha256(
        "expected_transcript_sha256", expected_transcript_sha256
    )
    if type(resolution) is not ManualNvdaAcceptanceResolution:
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution must be the exact canonical type"
        )
    issued = _ISSUED_RESOLUTIONS.get(id(resolution))
    if issued is None or issued[0] is not resolution:
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution is not a live resolver-issued authority"
        )
    fingerprint = _resolution_fingerprint(resolution)
    if fingerprint != issued[1]:
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution changed after issuance"
        )
    record = resolution.record
    if (
        record.artifact_sha256 != artifact
        or record.source_sha != source_sha
        or record.transcript_sha256 != transcript_sha
    ):
        raise NvdaManualAcceptanceStateError(
            "manual NVDA resolution does not match the expected candidate"
        )
    return resolution


class _ManualNvdaWriterLock(WorkspaceEconomicLock):
    """Crash-releasing writer fence for one manual-NVDA ledger pathname."""

    def __init__(self, ledger_path: str | Path) -> None:
        path = Path(ledger_path)
        super().__init__(path.parent)
        self.path = path.with_name(path.name + ".writer.lock")


class ManualNvdaAcceptanceLedger:
    """Append-only exact-candidate manual review ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._anchor_path = self.path.with_name(self.path.name + ".anchor.json")
        self._lock_path = self.path.with_name(self.path.name + ".writer.lock")

    def _sync_parent_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(self.path.parent, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _anchor(self, *, event_count: int, root: str | None) -> dict[str, Any]:
        body = {
            "anchor_schema_version": ANCHOR_SCHEMA_VERSION,
            "ledger_schema_version": SCHEMA_VERSION,
            "event_count": event_count,
            "ledger_root_sha256": root,
        }
        return {**body, "anchor_sha256": _digest(body)}

    def _write_anchor(self, *, event_count: int, root: str | None) -> None:
        anchor = self._anchor(event_count=event_count, root=root)
        tmp = self._anchor_path.with_name(self._anchor_path.name + ".tmp")
        encoded = _canonical(anchor) + "\n"
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._anchor_path)
            self._sync_parent_directory()
        except OSError as exc:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise NvdaManualAcceptanceIntegrityError(
                "manual NVDA ledger anchor durability barrier failed"
            ) from exc

    def _read_anchor(self) -> dict[str, Any] | None:
        if not self._anchor_path.exists():
            return None
        try:
            raw = self._anchor_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise NvdaManualAcceptanceIntegrityError(
                "cannot read manual NVDA ledger anchor"
            ) from exc
        anchor = _parse_json_object(raw, what="manual NVDA ledger anchor")
        if frozenset(anchor) != _ANCHOR_KEYS:
            raise NvdaManualAcceptanceIntegrityError(
                "manual NVDA ledger anchor schema is invalid"
            )
        body = {key: anchor[key] for key in anchor if key != "anchor_sha256"}
        if (
            anchor["anchor_schema_version"] != ANCHOR_SCHEMA_VERSION
            or anchor["ledger_schema_version"] != SCHEMA_VERSION
            or type(anchor["event_count"]) is not int
            or anchor["event_count"] < 0
            or anchor["anchor_sha256"] != _digest(body)
        ):
            raise NvdaManualAcceptanceIntegrityError(
                "manual NVDA ledger anchor is invalid"
            )
        root = anchor["ledger_root_sha256"]
        if root is not None and (
            type(root) is not str or _SHA256_RE.fullmatch(root) is None
        ):
            raise NvdaManualAcceptanceIntegrityError(
                "manual NVDA ledger anchor root is invalid"
            )
        return anchor

    def events(self) -> tuple[ManualNvdaDecisionRecord, ...]:
        if not self.path.exists():
            if self._anchor_path.exists():
                raise NvdaManualAcceptanceIntegrityError(
                    "manual NVDA ledger is missing while anchor exists"
                )
            return ()
        try:
            raw_lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise NvdaManualAcceptanceIntegrityError(
                "cannot read manual NVDA ledger"
            ) from exc
        if any(not line for line in raw_lines):
            raise NvdaManualAcceptanceIntegrityError(
                "manual NVDA ledger contains a blank event line"
            )

        records: list[ManualNvdaDecisionRecord] = []
        prior_sha: str | None = None
        for sequence, raw in enumerate(raw_lines):
            event = _parse_json_object(raw, what="manual NVDA ledger event")
            if event.get("sequence") != sequence:
                raise NvdaManualAcceptanceIntegrityError(
                    "manual NVDA ledger sequence is invalid"
                )
            if event.get("previous_sha256") != prior_sha:
                raise NvdaManualAcceptanceIntegrityError(
                    "manual NVDA ledger predecessor chain is invalid"
                )
            record = _record_from_event(event)
            records.append(record)
            prior_sha = record.event_sha256

        anchor = self._read_anchor()
        if anchor is None:
            if records:
                raise NvdaManualAcceptanceIntegrityError(
                    "manual NVDA ledger anchor is missing"
                )
        else:
            if (
                anchor["event_count"] != len(records)
                or anchor["ledger_root_sha256"] != prior_sha
            ):
                raise NvdaManualAcceptanceIntegrityError(
                    "manual NVDA ledger anchor does not match durable history"
                )
        return tuple(records)

    def record_decision(
        self,
        *,
        transcript: object,
        expected_artifact_sha256: str,
        expected_source_sha: str,
        reviewer_ref: str,
        reviewer_attestation: str,
        reviewed_at: str,
        decision: ManualNvdaDecision,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> ManualNvdaDecisionRecord:
        structural = _structural_result(
            transcript,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_source_sha=expected_source_sha,
        )
        payload = _decision_payload(
            structural=structural,
            decision=decision,
            reviewer_ref=reviewer_ref,
            reviewer_attestation=reviewer_attestation,
            reviewed_at=reviewed_at,
            protocol_version=protocol_version,
        )

        writer_lock = _ManualNvdaWriterLock(self.path)
        try:
            with writer_lock:
                records = self.events()
                for record in records:
                    if record.decision_id == payload["decision_id"]:
                        return record
                if records and payload["reviewed_at"] <= records[-1].reviewed_at:
                    raise NvdaManualAcceptanceStateError(
                        "new manual NVDA decision must have a later reviewed_at"
                    )

                previous_sha = None if not records else records[-1].event_sha256
                sequence = len(records)
                body = {
                    "schema_version": SCHEMA_VERSION,
                    "event_type": EVENT_TYPE,
                    "sequence": sequence,
                    "previous_sha256": previous_sha,
                    "payload": payload,
                }
                event = {**body, "event_sha256": _digest(body)}
                encoded = _canonical(event) + "\n"

                try:
                    with self.path.open(
                        "a",
                        encoding="utf-8",
                        newline="\n",
                    ) as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._sync_parent_directory()
                    self._write_anchor(
                        event_count=sequence + 1,
                        root=event["event_sha256"],
                    )
                except OSError as exc:
                    raise NvdaManualAcceptanceIntegrityError(
                        "manual NVDA ledger durability barrier failed"
                    ) from exc

                return _record_from_event(event)
        except WorkspaceEconomicLockBusyError as exc:
            raise NvdaManualAcceptanceStateError(
                "manual NVDA ledger writer is active"
            ) from exc
        except WorkspaceEconomicLockError as exc:
            raise NvdaManualAcceptanceIntegrityError(
                "manual NVDA ledger writer authority is invalid"
            ) from exc

    def resolve_current(
        self,
        *,
        transcript: object,
        expected_artifact_sha256: str,
        expected_source_sha: str,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> ManualNvdaAcceptanceResolution | None:
        structural = _structural_result(
            transcript,
            expected_artifact_sha256=expected_artifact_sha256,
            expected_source_sha=expected_source_sha,
        )
        protocol = _require_protocol(protocol_version)
        matching = [
            record
            for record in self.events()
            if (
                record.protocol_version == protocol
                and record.transcript_sha256 == structural.transcript_sha256
                and record.artifact_sha256 == structural.artifact_sha256
                and record.source_sha == structural.source_sha
                and record.structural_status == structural.status
                and record.structural_human_tester_attestation_sha256
                == structural.human_tester_attestation_sha256
                and record.windows_version == structural.windows_version
                and record.nvda_version == structural.nvda_version
            )
        ]
        if not matching:
            return None
        record = matching[-1]
        resolution = object.__new__(ManualNvdaAcceptanceResolution)
        object.__setattr__(resolution, "record", record)
        object.__setattr__(
            resolution,
            "accepted_manual_decision",
            record.decision is ManualNvdaDecision.ACCEPT_PHYSICAL_NVDA,
        )
        object.__setattr__(resolution, "reviewer_identity_verified", False)
        object.__setattr__(resolution, "human_tested", False)
        object.__setattr__(resolution, "nvda_verified", False)
        object.__setattr__(resolution, "manual_truth_promotion_required", True)
        object.__setattr__(resolution, "real_money_execution", False)
        object.__setattr__(resolution, "whole_product_complete", False)
        fingerprint = _resolution_fingerprint(resolution)
        while len(_ISSUED_RESOLUTIONS) >= _MAX_LIVE_RESOLUTIONS:
            _ISSUED_RESOLUTIONS.popitem(last=False)
        _ISSUED_RESOLUTIONS[id(resolution)] = (resolution, fingerprint)
        return resolution


__all__ = [
    "ANCHOR_SCHEMA_VERSION",
    "EVENT_TYPE",
    "ManualNvdaAcceptanceLedger",
    "ManualNvdaAcceptanceResolution",
    "ManualNvdaDecision",
    "ManualNvdaDecisionRecord",
    "NvdaManualAcceptanceError",
    "NvdaManualAcceptanceIntegrityError",
    "NvdaManualAcceptanceStateError",
    "PROTOCOL_VERSION",
    "SCHEMA_VERSION",
    "verify_manual_nvda_acceptance_resolution",
]
