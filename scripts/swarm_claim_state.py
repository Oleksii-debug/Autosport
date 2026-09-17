#!/usr/bin/env python3
"""Deterministic swarm claim-state resolver with protocol-domain validation.

The historical fold implementation lives in ``swarm_claim_state_impl.py`` unchanged.
This facade adds the #368 CLAIM_MODE domain fence at the ownership boundary and then
re-exports the established resolver/CLI surface. Unknown claim modes are fail-closed:
they can never appear as live, admitted, collision, released, or expired ownership.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Mapping, Sequence


_IMPL_PATH = Path(__file__).with_name("swarm_claim_state_impl.py")
_SPEC = importlib.util.spec_from_file_location(
    "_autosport_swarm_claim_state_impl",
    _IMPL_PATH,
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise ImportError(f"cannot load claim-state implementation from {_IMPL_PATH}")
_impl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_impl)

# #368 is the canonical protocol authority for the claim-mode domain.
ALLOWED_CLAIM_MODES = frozenset(
    {
        "SOURCE_MUTATION",
        "INTEGRATION",
        "READ_ONLY_AUDIT",
        "RESEARCH",
        "CI_TRIAGE",
    }
)

# Preserve the established public/helper surface for current callers and tests.
for _name in dir(_impl):
    if _name.startswith("__") or _name in {"resolve_comments", "main"}:
        continue
    globals()[_name] = getattr(_impl, _name)

_BASE_RESOLVE_COMMENTS = _impl.resolve_comments
_STATE_BUCKETS = (
    "live_runs",
    "source_live_runs",
    "non_source_live_runs",
    "admitted_runs",
    "collision_candidates",
    "released_runs",
    "expired_runs",
    "ambiguous_runs",
)
_NON_AMBIGUOUS_BUCKETS = tuple(
    bucket for bucket in _STATE_BUCKETS if bucket != "ambiguous_runs"
)


def _claim_mode_problem(run: Mapping[str, Any]) -> dict[str, Any]:
    problem: dict[str, Any] = {
        "kind": "invalid_claim_mode",
        "message": (
            "CLAIM_V1 has invalid CLAIM_MODE: "
            f"{run.get('claim_mode')!s}; expected one of "
            + ", ".join(sorted(ALLOWED_CLAIM_MODES))
        ),
        "run_id": run.get("run_id"),
        "header": "CLAIM_V1",
    }
    if run.get("claim_comment_index") is not None:
        problem["comment_index"] = run["claim_comment_index"]
    if run.get("claim_comment_id") is not None:
        problem["comment_id"] = run["claim_comment_id"]
    return problem


def _fail_closed_invalid_claim_modes(result: dict[str, Any]) -> dict[str, Any]:
    """Move every nonempty unknown CLAIM_MODE into ambiguous ownership evidence."""

    invalid: dict[str, dict[str, Any]] = {}
    for bucket in _STATE_BUCKETS:
        for run in result.get(bucket, ()):
            mode = run.get("claim_mode")
            run_id = run.get("run_id")
            if (
                isinstance(mode, str)
                and mode
                and mode not in ALLOWED_CLAIM_MODES
                and isinstance(run_id, str)
                and run_id
            ):
                invalid.setdefault(run_id, run)

    if not invalid:
        return result

    invalid_ids = frozenset(invalid)
    for bucket in _NON_AMBIGUOUS_BUCKETS:
        result[bucket] = [
            run for run in result.get(bucket, ()) if run.get("run_id") not in invalid_ids
        ]

    ambiguous = {
        run.get("run_id"): run
        for run in result.get("ambiguous_runs", ())
        if isinstance(run.get("run_id"), str)
    }
    ambiguous.update(invalid)
    result["ambiguous_runs"] = sorted(
        ambiguous.values(), key=lambda run: run.get("claim_order", -1)
    )

    malformed = list(result.get("malformed_events", ()))
    existing = {
        event.get("run_id")
        for event in malformed
        if event.get("kind") == "invalid_claim_mode"
    }
    for run_id, run in invalid.items():
        if run_id not in existing:
            malformed.append(_claim_mode_problem(run))
    result["malformed_events"] = malformed
    return result


def resolve_comments(
    comments: Sequence[Mapping[str, Any]],
    *,
    now: Any,
    capacity: int | None = None,
) -> dict[str, Any]:
    """Fold ownership comments and fail closed on unknown #368 claim modes."""

    result = _BASE_RESOLVE_COMMENTS(comments, now=now, capacity=capacity)
    return _fail_closed_invalid_claim_modes(result)


# The established CLI resolves ``resolve_comments`` through the implementation module's
# globals at call time. Patch only that callable so CLI and Python callers share the same
# validated semantics while retaining the existing parser/error behavior.
_impl.resolve_comments = resolve_comments
main = _impl.main


if __name__ == "__main__":
    raise SystemExit(main())
