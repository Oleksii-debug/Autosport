from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

from .causal_collector import CollectorDeltaStore


_CANONICAL_COLLECTOR_CYCLE_EVIDENCE = CollectorDeltaStore.collector_cycle_evidence
_CANONICAL_READ_SEAM_NAMES = frozenset(
    {
        "collector_cycle_evidence",
        "_connect",
        "_connect_path",
        "_path_file_identity",
        "_cycle_terminal_payload_sha256",
    }
)
_CANONICAL_CLASS_READ_SEAMS = {
    name: inspect.getattr_static(CollectorDeltaStore, name)
    for name in _CANONICAL_READ_SEAM_NAMES
}


class SourceUniverseCommitmentError(ValueError):
    """Canonical collector evidence cannot support a bounded universe commitment."""


def _require_canonical_class_read_seams() -> None:
    """Reject runtime replacement of canonical class-level durable read authority."""

    rebound = sorted(
        name
        for name, expected in _CANONICAL_CLASS_READ_SEAMS.items()
        if inspect.getattr_static(CollectorDeltaStore, name, None) is not expected
    )
    if rebound:
        raise SourceUniverseCommitmentError(
            "store canonical durable read seam is class-rebound: "
            + ", ".join(rebound)
        )


def _require_product_expected_store_path(
    store: CollectorDeltaStore,
    expected_store_path: str | Path,
) -> Path:
    """Fail closed if a caller redirects the canonical store object to another DB."""

    if isinstance(expected_store_path, str):
        if not expected_store_path or expected_store_path.strip() != expected_store_path:
            raise SourceUniverseCommitmentError(
                "expected_store_path must be a non-empty trimmed path"
            )
        expected = Path(expected_store_path)
    elif isinstance(expected_store_path, Path):
        expected = expected_store_path
    else:
        raise TypeError("expected_store_path must be str or Path")

    current = getattr(store, "path", None)
    if not isinstance(current, Path):
        raise SourceUniverseCommitmentError(
            "canonical collector store path identity is unavailable"
        )
    if current != expected:
        raise SourceUniverseCommitmentError(
            "canonical collector store path does not match product-expected authority path"
        )
    return expected


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
    expected_store_path: str | Path,
    source_id: str,
    start_cycle_seq: int,
    end_cycle_seq: int,
) -> SourceUniverseCommitment:
    """Bind every canonical collector cycle in an explicit closed sequence range."""

    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be the exact canonical CollectorDeltaStore")
    _require_product_expected_store_path(store, expected_store_path)
    _require_canonical_class_read_seams()
    instance_state = vars(store)
    rebound = sorted(
        name for name in _CANONICAL_READ_SEAM_NAMES if name in instance_state
    )
    if rebound:
        raise TypeError(
            "store canonical durable read seam is instance-rebound: "
            + ", ".join(rebound)
        )
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

    try:
        evidence = _CANONICAL_COLLECTOR_CYCLE_EVIDENCE(
            store,
            source_id=source_id,
            start_cycle_seq=start_cycle_seq,
            end_cycle_seq=end_cycle_seq,
        )
        _require_canonical_class_read_seams()
    except ValueError as exc:
        raise SourceUniverseCommitmentError(
            "canonical collector store evidence is unavailable"
        ) from exc
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


_COMMITMENT_FIELD_NAMES = (
    "schema_version",
    "source_id",
    "start_cycle_seq",
    "end_cycle_seq",
    "cycle_count",
    "success_count",
    "zero_result_success_count",
    "provider_unavailable_count",
    "local_failure_count",
    "stop_requested_count",
    "pending_count",
    "observed_delta_occurrence_count",
    "observed_unique_delta_count",
    "cycle_evidence_sha256",
    "commitment_sha256",
    "observation_ledger_complete",
    "provider_observation_complete",
    "external_provider_universe_complete",
    "promotion_ready",
)


def verify_source_universe_commitment(
    store: CollectorDeltaStore,
    candidate: SourceUniverseCommitment,
    *,
    expected_store_path: str | Path,
    expected_source_id: str,
    expected_start_cycle_seq: int,
    expected_end_cycle_seq: int,
) -> SourceUniverseCommitment:
    """Re-resolve one candidate from canonical bytes and product-owned scope.

    The candidate is evidence to compare, never authority to choose its own source
    or sequence window. Callers must supply the expected scope from the owning
    schedule/campaign/precommit contract.
    """

    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be the exact canonical CollectorDeltaStore")
    if type(candidate) is not SourceUniverseCommitment:
        raise TypeError("candidate must be an exact SourceUniverseCommitment")

    rebuilt = build_source_universe_commitment(
        store,
        expected_store_path=expected_store_path,
        source_id=expected_source_id,
        start_cycle_seq=expected_start_cycle_seq,
        end_cycle_seq=expected_end_cycle_seq,
    )
    try:
        for field_name in _COMMITMENT_FIELD_NAMES:
            supplied = getattr(candidate, field_name)
            canonical = getattr(rebuilt, field_name)
            if type(supplied) is not type(canonical) or supplied != canonical:
                raise SourceUniverseCommitmentError(
                    "source-universe commitment does not match canonical expected scope"
                )
    except AttributeError as exc:
        raise SourceUniverseCommitmentError(
            "source-universe commitment is structurally incomplete"
        ) from exc
    return rebuilt

def _seal_source_universe_dispatch() -> None:
    """Seal positive source-universe dispatch against ordinary runtime rebinding.

    This keeps the existing evidence algorithm/schema unchanged.  The public builder
    and verifier fail closed when their canonical module/class helper graph is
    replaced after import instead of silently trusting the replacement.
    """

    module_globals = globals()
    expected_store_type = CollectorDeltaStore
    expected_commitment_type = SourceUniverseCommitment
    expected_error_type = SourceUniverseCommitmentError
    expected_path_type = Path
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_json_dumps_code = getattr(expected_json_dumps, "__code__", None)
    expected_inspect = inspect
    expected_getattr_static = inspect.getattr_static
    expected_getattr_static_code = getattr(
        expected_getattr_static, "__code__", None
    )
    expected_cycle_evidence = _CANONICAL_COLLECTOR_CYCLE_EVIDENCE
    expected_cycle_evidence_code = getattr(expected_cycle_evidence, "__code__", None)
    expected_read_names = _CANONICAL_READ_SEAM_NAMES
    expected_class_seams = _CANONICAL_CLASS_READ_SEAMS
    expected_class_seam_witnesses = tuple(
        (
            name,
            expected,
            getattr(expected, "__code__", None),
        )
        for name, expected in sorted(expected_class_seams.items())
    )
    expected_field_names = _COMMITMENT_FIELD_NAMES
    expected_commitment_field_surfaces = tuple(
        (
            name,
            expected_getattr_static(expected_commitment_type, name),
        )
        for name in expected_field_names
    )
    expected_issue_surface = expected_getattr_static(
        expected_commitment_type, "_issue"
    )
    expected_issue_function = getattr(expected_issue_surface, "__func__", None)
    expected_issue_function_code = getattr(
        expected_issue_function, "__code__", None
    )
    helper_witnesses = tuple(
        (
            name,
            helper,
            getattr(helper, "__code__", None),
        )
        for name, helper in (
            (
                "_require_canonical_class_read_seams",
                _require_canonical_class_read_seams,
            ),
            (
                "_require_product_expected_store_path",
                _require_product_expected_store_path,
            ),
            ("_canonical_json", _canonical_json),
        )
    )
    original_build = build_source_universe_commitment
    original_build_code = original_build.__code__
    original_verify = verify_source_universe_commitment
    original_verify_code = original_verify.__code__

    def require_dispatch_integrity() -> None:
        if module_globals.get("CollectorDeltaStore") is not expected_store_type:
            raise expected_error_type(
                "source-universe collector type authority is rebound"
            )
        if module_globals.get("SourceUniverseCommitment") is not expected_commitment_type:
            raise expected_error_type(
                "source-universe commitment type authority is rebound"
            )
        if module_globals.get("SourceUniverseCommitmentError") is not expected_error_type:
            raise expected_error_type(
                "source-universe error authority is rebound"
            )
        if module_globals.get("Path") is not expected_path_type:
            raise expected_error_type("source-universe path authority is rebound")
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256
        ):
            raise expected_error_type("source-universe digest authority is rebound")
        if (
            module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or getattr(expected_json_dumps, "__code__", None)
            is not expected_json_dumps_code
        ):
            raise expected_error_type(
                "source-universe canonical JSON authority is rebound"
            )
        if (
            module_globals.get("inspect") is not expected_inspect
            or expected_inspect.getattr_static is not expected_getattr_static
            or getattr(expected_getattr_static, "__code__", None)
            is not expected_getattr_static_code
        ):
            raise expected_error_type(
                "source-universe reflection authority is rebound"
            )
        if (
            module_globals.get("_CANONICAL_COLLECTOR_CYCLE_EVIDENCE")
            is not expected_cycle_evidence
            or getattr(expected_cycle_evidence, "__code__", None)
            is not expected_cycle_evidence_code
        ):
            raise expected_error_type(
                "source-universe collector-cycle evidence authority is rebound"
            )
        if module_globals.get("_CANONICAL_READ_SEAM_NAMES") is not expected_read_names:
            raise expected_error_type(
                "source-universe durable read-seam identity is rebound"
            )
        current_class_seams = module_globals.get("_CANONICAL_CLASS_READ_SEAMS")
        if (
            current_class_seams is not expected_class_seams
            or type(current_class_seams) is not dict
        ):
            raise expected_error_type(
                "source-universe class read-seam witness map is rebound"
            )
        for name, expected_surface, expected_code in expected_class_seam_witnesses:
            current_surface = expected_getattr_static(
                expected_store_type, name, None
            )
            if (
                current_class_seams.get(name) is not expected_surface
                or current_surface is not expected_surface
                or getattr(expected_surface, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "source-universe canonical durable read seam drifted: " + name
                )
        if module_globals.get("_COMMITMENT_FIELD_NAMES") is not expected_field_names:
            raise expected_error_type(
                "source-universe verification field authority is rebound"
            )
        for name, expected_surface in expected_commitment_field_surfaces:
            if (
                expected_getattr_static(expected_commitment_type, name, None)
                is not expected_surface
            ):
                raise expected_error_type(
                    "source-universe result field surface is rebound: " + name
                )
        current_issue_surface = expected_getattr_static(
            expected_commitment_type, "_issue", None
        )
        if (
            current_issue_surface is not expected_issue_surface
            or getattr(current_issue_surface, "__func__", None)
            is not expected_issue_function
            or getattr(expected_issue_function, "__code__", None)
            is not expected_issue_function_code
        ):
            raise expected_error_type(
                "source-universe result issuance surface is rebound or mutated"
            )
        for name, expected_helper, expected_code in helper_witnesses:
            current_helper = module_globals.get(name)
            if (
                current_helper is not expected_helper
                or getattr(expected_helper, "__code__", None) is not expected_code
            ):
                raise expected_error_type(
                    "source-universe helper authority is rebound: " + name
                )
        if original_build.__code__ is not original_build_code:
            raise expected_error_type(
                "source-universe builder implementation drifted"
            )
        if original_verify.__code__ is not original_verify_code:
            raise expected_error_type(
                "source-universe verifier implementation drifted"
            )

    def sealed_build_source_universe_commitment(
        store: CollectorDeltaStore,
        *,
        expected_store_path: str | Path,
        source_id: str,
        start_cycle_seq: int,
        end_cycle_seq: int,
    ) -> SourceUniverseCommitment:
        if (
            module_globals.get("build_source_universe_commitment")
            is not sealed_build_source_universe_commitment
        ):
            raise expected_error_type(
                "source-universe public builder authority is rebound"
            )
        require_dispatch_integrity()
        result = original_build(
            store,
            expected_store_path=expected_store_path,
            source_id=source_id,
            start_cycle_seq=start_cycle_seq,
            end_cycle_seq=end_cycle_seq,
        )
        require_dispatch_integrity()
        if type(result) is not expected_commitment_type:
            raise expected_error_type(
                "source-universe builder returned non-canonical result type"
            )
        return result

    def sealed_verify_source_universe_commitment(
        store: CollectorDeltaStore,
        candidate: SourceUniverseCommitment,
        *,
        expected_store_path: str | Path,
        expected_source_id: str,
        expected_start_cycle_seq: int,
        expected_end_cycle_seq: int,
    ) -> SourceUniverseCommitment:
        if (
            module_globals.get("verify_source_universe_commitment")
            is not sealed_verify_source_universe_commitment
        ):
            raise expected_error_type(
                "source-universe public verifier authority is rebound"
            )
        if (
            module_globals.get("build_source_universe_commitment")
            is not sealed_build_source_universe_commitment
        ):
            raise expected_error_type(
                "source-universe verifier builder authority is rebound"
            )
        require_dispatch_integrity()
        result = original_verify(
            store,
            candidate,
            expected_store_path=expected_store_path,
            expected_source_id=expected_source_id,
            expected_start_cycle_seq=expected_start_cycle_seq,
            expected_end_cycle_seq=expected_end_cycle_seq,
        )
        require_dispatch_integrity()
        if type(result) is not expected_commitment_type:
            raise expected_error_type(
                "source-universe verifier returned non-canonical result type"
            )
        return result

    sealed_build_source_universe_commitment.__name__ = original_build.__name__
    sealed_build_source_universe_commitment.__qualname__ = original_build.__qualname__
    sealed_build_source_universe_commitment.__doc__ = original_build.__doc__
    sealed_build_source_universe_commitment.__module__ = original_build.__module__
    sealed_build_source_universe_commitment.__annotations__ = dict(
        original_build.__annotations__
    )
    sealed_verify_source_universe_commitment.__name__ = original_verify.__name__
    sealed_verify_source_universe_commitment.__qualname__ = original_verify.__qualname__
    sealed_verify_source_universe_commitment.__doc__ = original_verify.__doc__
    sealed_verify_source_universe_commitment.__module__ = original_verify.__module__
    sealed_verify_source_universe_commitment.__annotations__ = dict(
        original_verify.__annotations__
    )

    module_globals["build_source_universe_commitment"] = (
        sealed_build_source_universe_commitment
    )
    module_globals["verify_source_universe_commitment"] = (
        sealed_verify_source_universe_commitment
    )


_seal_source_universe_dispatch()
del _seal_source_universe_dispatch

