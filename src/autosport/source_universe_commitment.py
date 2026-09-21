from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .causal_collector import CollectorDeltaStore


class SourceUniverseCommitmentError(ValueError):
    """Canonical collector evidence cannot support a bounded universe commitment."""


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceUniverseCommitmentError(
            "source-universe evidence is not canonical JSON"
        ) from exc


@dataclass(frozen=True, slots=True, init=False)
class SourceUniverseCommitment:
    """Read-only product-owned commitment over one exact collector cycle window.

    The commitment proves only what the canonical collector durably observed about the
    requested cycle range. It never upgrades provider visibility into proof that the
    provider's external/global universe was complete.
    """

    schema_version: int
    source_id: str
    start_cycle_seq: int
    end_cycle_seq: int
    cycle_count: int
    success_count: int
    zero_result_success_count: int
    provider_unavailable_count: int
    local_failure_count: int
    stop_requested_count: int
    pending_count: int
    observed_delta_occurrence_count: int
    observed_unique_delta_count: int
    cycle_evidence_sha256: str
    commitment_sha256: str
    observation_ledger_complete: bool
    provider_observation_complete: bool
    external_provider_universe_complete: bool
    promotion_ready: bool

    def __new__(cls, *args: object, **kwargs: object) -> "SourceUniverseCommitment":
        raise TypeError(
            "SourceUniverseCommitment is product-issued; "
            "use build_source_universe_commitment"
        )

    @classmethod
    def _issue(cls, payload: dict[str, object]) -> "SourceUniverseCommitment":
        instance = object.__new__(cls)
        for field_name, value in payload.items():
            object.__setattr__(instance, field_name, value)
        return instance

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "start_cycle_seq": self.start_cycle_seq,
            "end_cycle_seq": self.end_cycle_seq,
            "cycle_count": self.cycle_count,
            "success_count": self.success_count,
            "zero_result_success_count": self.zero_result_success_count,
            "provider_unavailable_count": self.provider_unavailable_count,
            "local_failure_count": self.local_failure_count,
            "stop_requested_count": self.stop_requested_count,
            "pending_count": self.pending_count,
            "observed_delta_occurrence_count": self.observed_delta_occurrence_count,
            "observed_unique_delta_count": self.observed_unique_delta_count,
            "cycle_evidence_sha256": self.cycle_evidence_sha256,
            "commitment_sha256": self.commitment_sha256,
            "observation_ledger_complete": self.observation_ledger_complete,
            "provider_observation_complete": self.provider_observation_complete,
            "external_provider_universe_complete": self.external_provider_universe_complete,
            "promotion_ready": self.promotion_ready,
        }


def build_source_universe_commitment(
    store: CollectorDeltaStore,
    *,
    source_id: str,
    start_cycle_seq: int,
    end_cycle_seq: int,
) -> SourceUniverseCommitment:
    """Bind every canonical collector cycle in an explicit closed sequence range."""

    if not isinstance(store, CollectorDeltaStore):
        raise TypeError("store must be the canonical CollectorDeltaStore")
    if type(source_id) is not str or not source_id or source_id.strip() != source_id:
        raise SourceUniverseCommitmentError(
            "source_id must be a non-empty trimmed string"
        )
    for name, value in (
        ("start_cycle_seq", start_cycle_seq),
        ("end_cycle_seq", end_cycle_seq),
    ):
        if type(value) is not int or value <= 0:
            raise SourceUniverseCommitmentError(f"{name} must be a positive integer")
    if end_cycle_seq < start_cycle_seq:
        raise SourceUniverseCommitmentError(
            "end_cycle_seq cannot precede start_cycle_seq"
        )

    evidence = store.collector_cycle_evidence(
        source_id=source_id,
        start_cycle_seq=start_cycle_seq,
        end_cycle_seq=end_cycle_seq,
    )
    expected_sequences = tuple(range(start_cycle_seq, end_cycle_seq + 1))
    observed_sequences = tuple(item["cycle_seq"] for item in evidence)
    if observed_sequences != expected_sequences:
        raise SourceUniverseCommitmentError(
            "collector cycle window is incomplete or non-contiguous"
        )

    counts = {
        "SUCCESS": 0,
        "PROVIDER_UNAVAILABLE": 0,
        "LOCAL_FAILURE": 0,
        "STOP_REQUESTED": 0,
        "PENDING": 0,
    }
    zero_result_success_count = 0
    observed_delta_ids: list[str] = []

    for item in evidence:
        if item.get("source_id") != source_id:
            raise SourceUniverseCommitmentError(
                "collector cycle evidence crosses source authority"
            )
        terminal = item.get("terminal")
        if terminal is None:
            counts["PENDING"] += 1
            continue
        if type(terminal) is not dict:
            raise SourceUniverseCommitmentError(
                "collector cycle terminal evidence is malformed"
            )
        status = terminal.get("status")
        if status not in counts or status == "PENDING":
            raise SourceUniverseCommitmentError(
                "collector cycle terminal status is unsupported"
            )
        counts[status] += 1
        deltas = terminal.get("observed_deltas")
        if type(deltas) is not list:
            raise SourceUniverseCommitmentError(
                "collector cycle observed_deltas evidence is malformed"
            )
        if status == "SUCCESS" and not deltas:
            zero_result_success_count += 1
        for delta in deltas:
            if type(delta) is not dict or set(delta) != {
                "delta_id",
                "payload_sha256",
            }:
                raise SourceUniverseCommitmentError(
                    "collector cycle delta evidence is malformed"
                )
            delta_id = delta["delta_id"]
            digest = delta["payload_sha256"]
            if type(delta_id) is not str or not delta_id:
                raise SourceUniverseCommitmentError(
                    "collector cycle delta identity is malformed"
                )
            if (
                type(digest) is not str
                or len(digest) != 64
                or digest != digest.lower()
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise SourceUniverseCommitmentError(
                    "collector cycle delta digest is malformed"
                )
            observed_delta_ids.append(delta_id)

    cycle_evidence_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "schema": "autosport.collector_source_universe_cycle_window",
                "schema_version": 1,
                "source_id": source_id,
                "start_cycle_seq": start_cycle_seq,
                "end_cycle_seq": end_cycle_seq,
                "cycles": list(evidence),
            }
        )
    ).hexdigest()

    observation_ledger_complete = counts["PENDING"] == 0
    provider_observation_complete = (
        observation_ledger_complete
        and counts["PROVIDER_UNAVAILABLE"] == 0
        and counts["LOCAL_FAILURE"] == 0
        and counts["STOP_REQUESTED"] == 0
    )

    authority_payload: dict[str, object] = {
        "schema_version": 1,
        "source_id": source_id,
        "start_cycle_seq": start_cycle_seq,
        "end_cycle_seq": end_cycle_seq,
        "cycle_count": len(evidence),
        "success_count": counts["SUCCESS"],
        "zero_result_success_count": zero_result_success_count,
        "provider_unavailable_count": counts["PROVIDER_UNAVAILABLE"],
        "local_failure_count": counts["LOCAL_FAILURE"],
        "stop_requested_count": counts["STOP_REQUESTED"],
        "pending_count": counts["PENDING"],
        "observed_delta_occurrence_count": len(observed_delta_ids),
        "observed_unique_delta_count": len(set(observed_delta_ids)),
        "cycle_evidence_sha256": cycle_evidence_sha256,
        "observation_ledger_complete": observation_ledger_complete,
        "provider_observation_complete": provider_observation_complete,
        # A provider/API visibility universe needs separate first-party authority.
        "external_provider_universe_complete": False,
        # This object is evidence input only, never a promotion decision.
        "promotion_ready": False,
    }
    commitment_sha256 = hashlib.sha256(_canonical_json(authority_payload)).hexdigest()
    return SourceUniverseCommitment._issue(
        {
            **authority_payload,
            "commitment_sha256": commitment_sha256,
        }
    )
