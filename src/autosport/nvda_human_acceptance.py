"""Structural evidence gate for one real human NVDA acceptance session.

This module never promotes HUMAN_TESTED or NVDA_VERIFIED truth.  It validates
that a transcript is structurally complete enough for a separate manual truth
promotion process, bound to one exact packaged Windows artifact and source SHA.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any


SCHEMA_VERSION = 1
STATUS_STRUCTURALLY_COMPLETE = "STRUCTURALLY_COMPLETE_FOR_MANUAL_REVIEW"
HUMAN_NVDA_ORIGIN = "HUMAN_NVDA_SESSION"
TEMPLATE_ORIGIN = "TEMPLATE_ONLY"

REQUIRED_JOURNEY_IDS = (
    "fresh_launch",
    "first_run_workspace",
    "primary_navigation",
    "configuration_edit",
    "paper_start_stop",
    "blocking_error_recovery",
    "process_restart_recovery",
    "close_reopen_persistence",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_TEXT_LENGTH = 16_384
_MAX_STEPS_PER_JOURNEY = 1_000
_MAX_LIVE_STRUCTURAL_RESULTS = 256

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "artifact_sha256",
        "source_sha",
        "windows_version",
        "nvda_version",
        "keyboard_only",
        "mouse_used",
        "evidence_origin",
        "human_tester_attestation",
        "journeys",
    }
)
_JOURNEY_KEYS = frozenset({"journey_id", "human_passed", "steps"})
_STEP_KEYS = frozenset(
    {
        "keyboard_input",
        "expected_accessible_result",
        "actual_nvda_speech",
        "focus_before",
        "focus_after",
        "human_passed",
    }
)


class NvdaHumanAcceptanceError(ValueError):
    """Raised when a human NVDA transcript fails the structural evidence gate."""


@dataclass(frozen=True, slots=True, init=False)
class NvdaHumanAcceptanceStructuralResult:
    """Validator-issued, non-promoting structural transcript result."""

    transcript_sha256: str
    artifact_sha256: str
    source_sha: str
    windows_version: str
    nvda_version: str
    evidence_origin: str
    human_tester_attestation_sha256: str
    journey_content_sha256: str
    status: str = field(default=STATUS_STRUCTURALLY_COMPLETE, init=False)
    human_tested: bool = field(default=False, init=False)
    nvda_verified: bool = field(default=False, init=False)
    manual_truth_promotion_required: bool = field(default=True, init=False)
    real_money_execution: bool = field(default=False, init=False)
    whole_product_complete: bool = field(default=False, init=False)

    def __init__(self, transcript_sha256: str) -> None:
        _require_sha256("transcript_sha256", transcript_sha256)
        raise NvdaHumanAcceptanceError(
            "NvdaHumanAcceptanceStructuralResult is validator-issued only"
        )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("NvdaHumanAcceptanceStructuralResult may not be subclassed")


# Process-local structural authority.  A bounded strong-reference registry prevents
# object-id reuse while an issuance is live.  Eviction is deliberately fail-closed:
# a durable/restarted consumer must reopen the transcript and validate it again.
_ISSUED_STRUCTURAL_RESULTS: OrderedDict[
    int, tuple[NvdaHumanAcceptanceStructuralResult, str]
] = OrderedDict()


def _require_exact_dict(name: str, value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise NvdaHumanAcceptanceError(f"{name} must be an exact dict")
    return value


def _require_exact_list(name: str, value: object) -> list[Any]:
    if type(value) is not list:
        raise NvdaHumanAcceptanceError(f"{name} must be an exact list")
    return value


def _require_exact_keys(name: str, value: dict[str, Any], expected: frozenset[str]) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise NvdaHumanAcceptanceError(
            f"{name} keys mismatch; missing={missing}; extra={extra}"
        )


def _require_bool(name: str, value: object, expected: bool) -> None:
    if type(value) is not bool or value is not expected:
        raise NvdaHumanAcceptanceError(f"{name} must be exactly {expected}")


def _require_text(name: str, value: object) -> str:
    if type(value) is not str:
        raise NvdaHumanAcceptanceError(f"{name} must be a string")
    if value != value.strip() or not value:
        raise NvdaHumanAcceptanceError(f"{name} must be non-empty and trimmed")
    if len(value) > _MAX_TEXT_LENGTH:
        raise NvdaHumanAcceptanceError(f"{name} is too long")
    if "\x00" in value:
        raise NvdaHumanAcceptanceError(f"{name} must not contain NUL")
    return value


def _require_sha256(name: str, value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise NvdaHumanAcceptanceError(
            f"{name} must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _require_git_commit_sha(name: str, value: object) -> str:
    if type(value) is not str or _GIT_COMMIT_SHA_RE.fullmatch(value) is None:
        raise NvdaHumanAcceptanceError(
            f"{name} must be exactly 40 lowercase hexadecimal Git commit characters"
        )
    return value


def _snapshot_json_tree(value: object, *, name: str) -> Any:
    """Take one detached exact-container JSON snapshot before structural validation.

    Transcript dict/list containers are caller-owned and mutable.  Validation must
    never inspect one version and later hash another.  This copier intentionally
    rejects tuple/custom-container/coercible objects instead of normalizing them.
    If a concurrent mutation makes iteration inconsistent, the resulting detached
    snapshot still has to pass the complete schema below before any authority is
    issued.
    """

    if type(value) is dict:
        snapshot: dict[str, Any] = {}
        try:
            items = tuple(value.items())
        except RuntimeError as exc:
            raise NvdaHumanAcceptanceError(
                f"{name} changed while the transcript snapshot was captured"
            ) from exc
        for key, item in items:
            if type(key) is not str:
                raise NvdaHumanAcceptanceError(f"{name} object keys must be strings")
            snapshot[key] = _snapshot_json_tree(item, name=f"{name}.{key}")
        return snapshot
    if type(value) is list:
        try:
            items = tuple(value)
        except RuntimeError as exc:
            raise NvdaHumanAcceptanceError(
                f"{name} changed while the transcript snapshot was captured"
            ) from exc
        return [
            _snapshot_json_tree(item, name=f"{name}[{index}]")
            for index, item in enumerate(items)
        ]
    if type(value) in (str, int, bool) or value is None:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise NvdaHumanAcceptanceError(f"{name} must not contain NaN or Infinity")
        return value
    raise NvdaHumanAcceptanceError(
        f"{name} must contain only exact JSON dict/list/scalar values"
    )


def _canonical_sha256(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _structural_result_fingerprint(
    result: NvdaHumanAcceptanceStructuralResult,
) -> str:
    try:
        payload = {
            "artifact_sha256": _require_sha256(
                "result.artifact_sha256", result.artifact_sha256
            ),
            "evidence_origin": result.evidence_origin,
            "human_tested": result.human_tested,
            "human_tester_attestation_sha256": _require_sha256(
                "result.human_tester_attestation_sha256",
                result.human_tester_attestation_sha256,
            ),
            "journey_content_sha256": _require_sha256(
                "result.journey_content_sha256", result.journey_content_sha256
            ),
            "manual_truth_promotion_required": result.manual_truth_promotion_required,
            "nvda_verified": result.nvda_verified,
            "nvda_version": _require_text("result.nvda_version", result.nvda_version),
            "real_money_execution": result.real_money_execution,
            "source_sha": _require_git_commit_sha("result.source_sha", result.source_sha),
            "status": result.status,
            "transcript_sha256": _require_sha256(
                "result.transcript_sha256", result.transcript_sha256
            ),
            "whole_product_complete": result.whole_product_complete,
            "windows_version": _require_text(
                "result.windows_version", result.windows_version
            ),
        }
    except (AttributeError, TypeError) as exc:
        raise NvdaHumanAcceptanceError(
            "structural result payload is incomplete or malformed"
        ) from exc

    if payload["evidence_origin"] != HUMAN_NVDA_ORIGIN:
        raise NvdaHumanAcceptanceError(
            "structural result evidence_origin is not HUMAN_NVDA_SESSION"
        )
    if payload["status"] != STATUS_STRUCTURALLY_COMPLETE:
        raise NvdaHumanAcceptanceError("structural result status is not canonical")
    if type(payload["human_tested"]) is not bool or payload["human_tested"] is not False:
        raise NvdaHumanAcceptanceError("structural result human_tested must be false")
    if type(payload["nvda_verified"]) is not bool or payload["nvda_verified"] is not False:
        raise NvdaHumanAcceptanceError("structural result nvda_verified must be false")
    if (
        type(payload["manual_truth_promotion_required"]) is not bool
        or payload["manual_truth_promotion_required"] is not True
    ):
        raise NvdaHumanAcceptanceError(
            "structural result must require manual truth promotion"
        )
    if (
        type(payload["real_money_execution"]) is not bool
        or payload["real_money_execution"] is not False
    ):
        raise NvdaHumanAcceptanceError(
            "structural result real_money_execution must be false"
        )
    if (
        type(payload["whole_product_complete"]) is not bool
        or payload["whole_product_complete"] is not False
    ):
        raise NvdaHumanAcceptanceError(
            "structural result whole_product_complete must be false"
        )
    return _canonical_sha256(payload)


def verify_human_nvda_acceptance_structural_result(
    result: object,
    *,
    expected_artifact_sha256: str,
    expected_source_sha: str,
) -> NvdaHumanAcceptanceStructuralResult:
    """Verify live issuance and bind it to the consumer's exact artifact/source."""

    expected_artifact = _require_sha256(
        "expected_artifact_sha256", expected_artifact_sha256
    )
    expected_source = _require_git_commit_sha("expected_source_sha", expected_source_sha)
    if type(result) is not NvdaHumanAcceptanceStructuralResult:
        raise NvdaHumanAcceptanceError(
            "structural result must be the exact canonical result type"
        )
    issued = _ISSUED_STRUCTURAL_RESULTS.get(id(result))
    if issued is None or issued[0] is not result:
        raise NvdaHumanAcceptanceError(
            "structural result is not a live validator-issued authority"
        )
    fingerprint = _structural_result_fingerprint(result)
    if fingerprint != issued[1]:
        raise NvdaHumanAcceptanceError(
            "structural result payload changed after validator issuance"
        )
    if result.artifact_sha256 != expected_artifact:
        raise NvdaHumanAcceptanceError(
            "structural result does not match the expected artifact"
        )
    if result.source_sha != expected_source:
        raise NvdaHumanAcceptanceError(
            "structural result does not match the expected source"
        )
    return result


def _validate_step(step: object, *, journey_id: str, index: int) -> None:
    frozen = _require_exact_dict(f"journey {journey_id} step {index}", step)
    _require_exact_keys(
        f"journey {journey_id} step {index}",
        frozen,
        _STEP_KEYS,
    )
    _require_text("keyboard_input", frozen["keyboard_input"])
    _require_text("expected_accessible_result", frozen["expected_accessible_result"])
    _require_text("actual_nvda_speech", frozen["actual_nvda_speech"])
    _require_text("focus_before", frozen["focus_before"])
    _require_text("focus_after", frozen["focus_after"])
    _require_bool("step human_passed", frozen["human_passed"], True)


def _validate_journey(journey: object, *, expected_id: str) -> None:
    frozen = _require_exact_dict(f"journey {expected_id}", journey)
    _require_exact_keys(f"journey {expected_id}", frozen, _JOURNEY_KEYS)
    if frozen["journey_id"] != expected_id:
        raise NvdaHumanAcceptanceError(
            f"journey order/id mismatch: expected {expected_id!r}"
        )
    _require_bool("journey human_passed", frozen["human_passed"], True)
    steps = _require_exact_list(f"journey {expected_id} steps", frozen["steps"])
    if not steps:
        raise NvdaHumanAcceptanceError(f"journey {expected_id} must contain a step")
    if len(steps) > _MAX_STEPS_PER_JOURNEY:
        raise NvdaHumanAcceptanceError(f"journey {expected_id} has too many steps")
    for index, step in enumerate(steps):
        _validate_step(step, journey_id=expected_id, index=index)


def validate_human_nvda_acceptance_transcript(
    transcript: object,
    *,
    expected_artifact_sha256: str,
    expected_source_sha: str,
) -> NvdaHumanAcceptanceStructuralResult:
    """Validate one detached transcript snapshot without promoting human/NVDA truth."""

    expected_artifact = _require_sha256(
        "expected_artifact_sha256", expected_artifact_sha256
    )
    expected_source = _require_git_commit_sha("expected_source_sha", expected_source_sha)
    if type(transcript) is not dict:
        raise NvdaHumanAcceptanceError("transcript must be an exact dict")
    frozen = _snapshot_json_tree(transcript, name="transcript")
    frozen = _require_exact_dict("transcript", frozen)
    _require_exact_keys("transcript", frozen, _TOP_LEVEL_KEYS)

    if type(frozen["schema_version"]) is not int or frozen["schema_version"] != SCHEMA_VERSION:
        raise NvdaHumanAcceptanceError(f"schema_version must equal {SCHEMA_VERSION}")

    artifact_sha = _require_sha256("artifact_sha256", frozen["artifact_sha256"])
    source_sha = _require_git_commit_sha("source_sha", frozen["source_sha"])
    if artifact_sha != expected_artifact:
        raise NvdaHumanAcceptanceError(
            "artifact_sha256 does not match the artifact under review"
        )
    if source_sha != expected_source:
        raise NvdaHumanAcceptanceError("source_sha does not match the source under review")

    windows_version = _require_text("windows_version", frozen["windows_version"])
    nvda_version = _require_text("nvda_version", frozen["nvda_version"])
    _require_bool("keyboard_only", frozen["keyboard_only"], True)
    _require_bool("mouse_used", frozen["mouse_used"], False)
    if frozen["evidence_origin"] != HUMAN_NVDA_ORIGIN:
        raise NvdaHumanAcceptanceError(
            "evidence_origin must be HUMAN_NVDA_SESSION; "
            "automated/synthetic/template evidence is insufficient"
        )
    attestation = _require_text(
        "human_tester_attestation", frozen["human_tester_attestation"]
    )

    journeys = _require_exact_list("journeys", frozen["journeys"])
    if len(journeys) != len(REQUIRED_JOURNEY_IDS):
        raise NvdaHumanAcceptanceError(
            f"journeys must contain exactly {len(REQUIRED_JOURNEY_IDS)} required journeys"
        )
    for expected_id, journey in zip(REQUIRED_JOURNEY_IDS, journeys, strict=True):
        _validate_journey(journey, expected_id=expected_id)

    result = object.__new__(NvdaHumanAcceptanceStructuralResult)
    object.__setattr__(result, "transcript_sha256", _canonical_sha256(frozen))
    object.__setattr__(result, "artifact_sha256", artifact_sha)
    object.__setattr__(result, "source_sha", source_sha)
    object.__setattr__(result, "windows_version", windows_version)
    object.__setattr__(result, "nvda_version", nvda_version)
    object.__setattr__(result, "evidence_origin", HUMAN_NVDA_ORIGIN)
    object.__setattr__(
        result,
        "human_tester_attestation_sha256",
        _text_sha256(attestation),
    )
    object.__setattr__(
        result,
        "journey_content_sha256",
        _canonical_sha256({"journeys": journeys}),
    )
    object.__setattr__(result, "status", STATUS_STRUCTURALLY_COMPLETE)
    object.__setattr__(result, "human_tested", False)
    object.__setattr__(result, "nvda_verified", False)
    object.__setattr__(result, "manual_truth_promotion_required", True)
    object.__setattr__(result, "real_money_execution", False)
    object.__setattr__(result, "whole_product_complete", False)

    fingerprint = _structural_result_fingerprint(result)
    while len(_ISSUED_STRUCTURAL_RESULTS) >= _MAX_LIVE_STRUCTURAL_RESULTS:
        _ISSUED_STRUCTURAL_RESULTS.popitem(last=False)
    _ISSUED_STRUCTURAL_RESULTS[id(result)] = (result, fingerprint)
    return result


def build_manual_nvda_transcript_template(
    *,
    artifact_sha256: str,
    source_sha: str,
) -> dict[str, Any]:
    """Return an intentionally invalid blank template for a future human session.

    The template cannot pass validation until a real human tester replaces the
    template origin and blank/manual-fail placeholders with observed evidence.
    """

    artifact = _require_sha256("artifact_sha256", artifact_sha256)
    source = _require_git_commit_sha("source_sha", source_sha)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_sha256": artifact,
        "source_sha": source,
        "windows_version": "",
        "nvda_version": "",
        "keyboard_only": True,
        "mouse_used": False,
        "evidence_origin": TEMPLATE_ORIGIN,
        "human_tester_attestation": "",
        "journeys": [
            {
                "journey_id": journey_id,
                "human_passed": False,
                "steps": [
                    {
                        "keyboard_input": "",
                        "expected_accessible_result": "",
                        "actual_nvda_speech": "",
                        "focus_before": "",
                        "focus_after": "",
                        "human_passed": False,
                    }
                ],
            }
            for journey_id in REQUIRED_JOURNEY_IDS
        ],
    }
