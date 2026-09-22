from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .risk_sampling_membership import ResolvedFixedNRiskMembership


_DESIGN_KIND = "autosport-risk-iid-resample-with-replacement-v1"
_SAMPLER_KIND = "IID_RESAMPLE_WITH_REPLACEMENT_V1"
_SCOPE = "SIMULATOR_DISTRIBUTION_ONLY"
_STOPPING_RULE = "FIXED_N_NO_EARLY_STOP"
_RANDOMIZATION_AUTHORITY = "PRODUCT_PRECOMMIT_REQUIRED"
_HEX = frozenset("0123456789abcdef")
_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "kind",
        "experiment_id",
        "membership_design_sha256",
        "research_protocol_id",
        "protocol_sha256",
        "dataset_snapshot_id",
        "dataset_manifest_sha256",
        "sampling_frame_sha256",
        "initial_capital_state_sha256",
        "stake_policy_sha256",
        "horizon_sha256",
        "sampler_kind",
        "with_replacement",
        "rng_algorithm",
        "rng_version",
        "randomization_root_sha256",
        "planned_n",
        "planned_member_ids",
        "stopping_rule",
        "risk_scope",
        "randomization_authority",
    }
)


class RiskSamplingDependenceError(RuntimeError):
    """Raised when IID sampling/dependence evidence cannot be qualified fail-closed."""


def _canonical_text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RiskSamplingDependenceError(f"{name} must be a non-empty canonical string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RiskSamplingDependenceError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(value: object, name: str) -> str:
    text = _canonical_text(value, name).lower()
    if len(text) != 64 or any(character not in _HEX for character in text):
        raise RiskSamplingDependenceError(
            f"{name} must be a canonical SHA-256 hex string"
        )
    return text


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RiskSamplingDependenceError(f"{name} must be a positive integer")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RiskSamplingDependenceError(
                f"IID sampling manifest contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise RiskSamplingDependenceError(
        f"IID sampling manifest contains non-finite JSON value {value!r}"
    )


def _parse_manifest(value: object) -> tuple[dict[str, Any], str]:
    raw = _canonical_text(value, "sampling_manifest_json")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise RiskSamplingDependenceError(
            "sampling_manifest_json must be canonical JSON"
        ) from exc
    if type(payload) is not dict or set(payload) != _REQUIRED_MANIFEST_FIELDS:
        raise RiskSamplingDependenceError(
            "IID sampling manifest fields do not match the supported schema"
        )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if raw != canonical:
        raise RiskSamplingDependenceError(
            "sampling_manifest_json must use canonical JSON serialization"
        )
    return payload, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stream_sha256(
    *,
    randomization_root_sha256: str,
    experiment_id: str,
    member_index: int,
    member_id: str,
) -> str:
    material = (
        "autosport-risk-iid-stream-v1\n"
        f"{randomization_root_sha256}\n"
        f"{experiment_id}\n"
        f"{member_index}\n"
        f"{member_id}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedFixedNIidSamplingStructure:
    """Immutable structural IID design.

    This is not a bearer capability. It proves only that one manifest is internally
    consistent with one frozen fixed-N membership. Positive IID authority additionally
    requires product-owned, non-backdateable membership and randomization precommit
    chronology that this module intentionally does not manufacture from local hashes.
    """

    experiment_id: str
    membership_design_sha256: str
    research_protocol_id: str
    protocol_sha256: str
    dataset_snapshot_id: str
    dataset_manifest_sha256: str
    sampling_frame_sha256: str
    initial_capital_state_sha256: str
    stake_policy_sha256: str
    horizon_sha256: str
    rng_algorithm: str
    rng_version: str
    randomization_root_sha256: str
    planned_member_ids: tuple[str, ...]
    member_stream_sha256: tuple[str, ...]
    manifest_sha256: str
    sampler_kind: str = _SAMPLER_KIND
    risk_scope: str = _SCOPE
    stopping_rule: str = _STOPPING_RULE

    @property
    def planned_n(self) -> int:
        return len(self.planned_member_ids)

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class IidSamplingOccurrence:
    member_id: str
    member_index: int
    stream_sha256: str
    draw_transcript_sha256: str
    initial_capital_state_sha256: str
    sampling_frame_sha256: str
    protocol_sha256: str
    stake_policy_sha256: str
    horizon_sha256: str
    complete: bool


@dataclass(frozen=True, slots=True)
class ResolvedFixedNIidOccurrenceSet:
    experiment_id: str
    planned_member_ids: tuple[str, ...]
    occurrence_root_sha256: str
    manifest_sha256: str
    complete: bool = True

    @property
    def iid_qualified(self) -> bool:
        return False

    @property
    def grants_real_money_authority(self) -> bool:
        return False


def inspect_fixed_n_iid_sampling_structure(
    membership: ResolvedFixedNRiskMembership,
    *,
    sampling_manifest_json: str,
) -> ResolvedFixedNIidSamplingStructure:
    """Validate the frozen IID design without claiming causal precommit authority."""

    if type(membership) is not ResolvedFixedNRiskMembership:
        raise RiskSamplingDependenceError(
            "membership must be an exact ResolvedFixedNRiskMembership"
        )
    payload, manifest_sha256 = _parse_manifest(sampling_manifest_json)

    if payload.get("kind") != _DESIGN_KIND:
        raise RiskSamplingDependenceError("IID sampling design kind is unsupported")
    if payload.get("sampler_kind") != _SAMPLER_KIND:
        raise RiskSamplingDependenceError(
            "only IID_RESAMPLE_WITH_REPLACEMENT_V1 is supported"
        )
    if payload.get("with_replacement") is not True:
        raise RiskSamplingDependenceError(
            "fixed-N binomial IID qualification requires sampling with replacement"
        )
    if payload.get("stopping_rule") != _STOPPING_RULE:
        raise RiskSamplingDependenceError(
            "fixed-N IID qualification forbids adaptive or early stopping"
        )
    if payload.get("risk_scope") != _SCOPE:
        raise RiskSamplingDependenceError(
            "IID qualification must remain scoped to the frozen simulator distribution"
        )
    if payload.get("randomization_authority") != _RANDOMIZATION_AUTHORITY:
        raise RiskSamplingDependenceError(
            "IID randomization must require product-owned precommit authority"
        )

    experiment_id = _canonical_text(payload.get("experiment_id"), "experiment_id")
    membership_design_sha256 = _sha256(
        payload.get("membership_design_sha256"),
        "membership_design_sha256",
    )
    if membership_design_sha256 != membership.design_sha256:
        raise RiskSamplingDependenceError(
            "IID design does not bind the exact fixed-N membership design"
        )

    research_protocol_id = _canonical_text(
        payload.get("research_protocol_id"),
        "research_protocol_id",
    )
    if research_protocol_id != membership.research_protocol_id:
        raise RiskSamplingDependenceError(
            "IID design research protocol identity mismatch"
        )
    protocol_sha256 = _sha256(payload.get("protocol_sha256"), "protocol_sha256")
    if protocol_sha256 != membership.protocol_sha256:
        raise RiskSamplingDependenceError("IID design protocol digest mismatch")

    dataset_snapshot_id = _canonical_text(
        payload.get("dataset_snapshot_id"),
        "dataset_snapshot_id",
    )
    if dataset_snapshot_id != membership.dataset_snapshot_id:
        raise RiskSamplingDependenceError(
            "IID design dataset snapshot identity mismatch"
        )
    dataset_manifest_sha256 = _sha256(
        payload.get("dataset_manifest_sha256"),
        "dataset_manifest_sha256",
    )
    if dataset_manifest_sha256 != membership.dataset_manifest_sha256:
        raise RiskSamplingDependenceError(
            "IID design dataset manifest digest mismatch"
        )
    sampling_frame_sha256 = _sha256(
        payload.get("sampling_frame_sha256"),
        "sampling_frame_sha256",
    )
    if sampling_frame_sha256 != membership.sampling_frame_sha256:
        raise RiskSamplingDependenceError("IID sampling frame identity mismatch")

    initial_capital_state_sha256 = _sha256(
        payload.get("initial_capital_state_sha256"),
        "initial_capital_state_sha256",
    )
    stake_policy_sha256 = _sha256(
        payload.get("stake_policy_sha256"),
        "stake_policy_sha256",
    )
    horizon_sha256 = _sha256(payload.get("horizon_sha256"), "horizon_sha256")
    rng_algorithm = _canonical_text(payload.get("rng_algorithm"), "rng_algorithm")
    rng_version = _canonical_text(payload.get("rng_version"), "rng_version")
    randomization_root_sha256 = _sha256(
        payload.get("randomization_root_sha256"),
        "randomization_root_sha256",
    )

    planned_n = _positive_int(payload.get("planned_n"), "planned_n")
    raw_member_ids = payload.get("planned_member_ids")
    if type(raw_member_ids) is not list or not raw_member_ids:
        raise RiskSamplingDependenceError(
            "planned_member_ids must be a non-empty JSON array"
        )
    member_ids = tuple(
        _canonical_text(value, f"planned_member_ids[{index}]")
        for index, value in enumerate(raw_member_ids)
    )
    if len(member_ids) != len(set(member_ids)):
        raise RiskSamplingDependenceError("planned_member_ids must be unique")
    if planned_n != len(member_ids):
        raise RiskSamplingDependenceError(
            "planned_n must equal the exact planned member membership"
        )
    if member_ids != membership.planned_run_ids:
        raise RiskSamplingDependenceError(
            "IID design member membership/order differs from fixed-N membership"
        )

    streams = tuple(
        _stream_sha256(
            randomization_root_sha256=randomization_root_sha256,
            experiment_id=experiment_id,
            member_index=index,
            member_id=member_id,
        )
        for index, member_id in enumerate(member_ids)
    )
    if len(streams) != len(set(streams)):
        raise RiskSamplingDependenceError(
            "derived member randomization streams must be unique"
        )

    return ResolvedFixedNIidSamplingStructure(
        experiment_id=experiment_id,
        membership_design_sha256=membership_design_sha256,
        research_protocol_id=research_protocol_id,
        protocol_sha256=protocol_sha256,
        dataset_snapshot_id=dataset_snapshot_id,
        dataset_manifest_sha256=dataset_manifest_sha256,
        sampling_frame_sha256=sampling_frame_sha256,
        initial_capital_state_sha256=initial_capital_state_sha256,
        stake_policy_sha256=stake_policy_sha256,
        horizon_sha256=horizon_sha256,
        rng_algorithm=rng_algorithm,
        rng_version=rng_version,
        randomization_root_sha256=randomization_root_sha256,
        planned_member_ids=member_ids,
        member_stream_sha256=streams,
        manifest_sha256=manifest_sha256,
    )


def inspect_fixed_n_iid_occurrences(
    structure: ResolvedFixedNIidSamplingStructure,
    occurrences: Iterable[IidSamplingOccurrence],
) -> ResolvedFixedNIidOccurrenceSet:
    """Validate a complete fixed-N occurrence set for the frozen structural design."""

    if type(structure) is not ResolvedFixedNIidSamplingStructure:
        raise RiskSamplingDependenceError(
            "structure must be an exact ResolvedFixedNIidSamplingStructure"
        )
    values = tuple(occurrences)
    if len(values) != structure.planned_n:
        raise RiskSamplingDependenceError(
            "all precommitted fixed-N members must complete; denominator changes are forbidden"
        )

    canonical_rows: list[dict[str, object]] = []
    observed_streams: set[str] = set()
    for index, expected_member_id in enumerate(structure.planned_member_ids):
        occurrence = values[index]
        if type(occurrence) is not IidSamplingOccurrence:
            raise RiskSamplingDependenceError(
                "occurrences must contain exact IidSamplingOccurrence values"
            )
        member_id = _canonical_text(
            occurrence.member_id,
            f"occurrences[{index}].member_id",
        )
        if occurrence.member_index != index or isinstance(occurrence.member_index, bool):
            raise RiskSamplingDependenceError(
                "occurrence member_index must equal its frozen member position"
            )
        if member_id != expected_member_id:
            raise RiskSamplingDependenceError(
                "occurrence member identity/order differs from the fixed membership"
            )
        if occurrence.complete is not True:
            raise RiskSamplingDependenceError(
                "unresolved or incomplete path cannot count as a non-ruin Bernoulli trial"
            )

        stream_sha256 = _sha256(
            occurrence.stream_sha256,
            f"occurrences[{index}].stream_sha256",
        )
        if stream_sha256 != structure.member_stream_sha256[index]:
            raise RiskSamplingDependenceError(
                "occurrence randomization stream does not match product domain separation"
            )
        if stream_sha256 in observed_streams:
            raise RiskSamplingDependenceError(
                "one randomization stream cannot be rebound to multiple fixed-N members"
            )
        observed_streams.add(stream_sha256)

        draw_transcript_sha256 = _sha256(
            occurrence.draw_transcript_sha256,
            f"occurrences[{index}].draw_transcript_sha256",
        )
        for actual, expected, label in (
            (
                occurrence.initial_capital_state_sha256,
                structure.initial_capital_state_sha256,
                "initial capital state",
            ),
            (
                occurrence.sampling_frame_sha256,
                structure.sampling_frame_sha256,
                "sampling frame",
            ),
            (occurrence.protocol_sha256, structure.protocol_sha256, "protocol"),
            (
                occurrence.stake_policy_sha256,
                structure.stake_policy_sha256,
                "stake policy",
            ),
            (occurrence.horizon_sha256, structure.horizon_sha256, "horizon"),
        ):
            if _sha256(actual, f"occurrences[{index}].{label}") != expected:
                raise RiskSamplingDependenceError(
                    f"occurrence {label} differs from the frozen IID design"
                )

        canonical_rows.append(
            {
                "draw_transcript_sha256": draw_transcript_sha256,
                "member_id": member_id,
                "member_index": index,
                "stream_sha256": stream_sha256,
            }
        )

    canonical = json.dumps(
        {
            "experiment_id": structure.experiment_id,
            "manifest_sha256": structure.manifest_sha256,
            "occurrences": canonical_rows,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return ResolvedFixedNIidOccurrenceSet(
        experiment_id=structure.experiment_id,
        planned_member_ids=structure.planned_member_ids,
        occurrence_root_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        manifest_sha256=structure.manifest_sha256,
    )


def resolve_fixed_n_iid_sampling_authority(
    membership: ResolvedFixedNRiskMembership,
    *,
    sampling_manifest_json: str,
    occurrences: Iterable[IidSamplingOccurrence],
) -> ResolvedFixedNIidOccurrenceSet:
    """Fail closed until causal membership and randomization precommit are product-owned.

    Local canonical JSON, deterministic hashes, unique streams and complete replay
    transcripts are structural evidence only. They cannot prove that the experiment
    design/randomization root existed before outcomes were knowable.
    """

    structure = inspect_fixed_n_iid_sampling_structure(
        membership,
        sampling_manifest_json=sampling_manifest_json,
    )
    resolved = inspect_fixed_n_iid_occurrences(structure, occurrences)
    if not membership.causal_precommit_proven:
        raise RiskSamplingDependenceError(
            "fixed-N membership lacks non-backdateable product-owned pre-outcome precommit authority"
        )
    raise RiskSamplingDependenceError(
        "IID randomization root lacks non-backdateable product-owned pre-outcome precommit authority"
    )
