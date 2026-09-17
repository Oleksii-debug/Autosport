"""Fail-closed guard for source-branch mutation versus live swarm ownership.

The guard is intentionally network-free. It consumes deterministic JSON exported
by the claim-state resolver/control plane and branch-mutation evidence exported by
the caller. Its job is narrow: prove that movement of one canonical source branch
is attributable to the currently admitted SOURCE_MUTATION owner, or fail closed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


STATUSES = {"OK", "COLLISION", "AMBIGUOUS", "NO_LIVE_OWNER"}


def _parse_instant(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("instant must be a non-empty ISO-8601 string")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    result = datetime.fromisoformat(normalized)
    if result.tzinfo is None:
        raise ValueError("instant must include a timezone")
    return result.astimezone(timezone.utc)


def _require_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _records(value: Any, *, key: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{key}[{index}] must be an object")
        result.append(item)
    return result


def _ambiguous_claim_state(claim_state: Mapping[str, Any]) -> bool:
    # Canonical #511 output uses ambiguous_runs. Keep older aliases for
    # compatibility, but deterministic collision_candidates are *not* ambiguity:
    # the resolver has already selected admitted_runs by server-order/capacity.
    # If an ambiguity/malformed-evidence key is present, its shape is part of the
    # trust boundary. A non-list value is itself malformed evidence and therefore
    # must fail closed rather than being silently ignored.
    for key in ("ambiguous_runs", "ambiguous", "ambiguities", "malformed_events"):
        if key not in claim_state:
            continue
        value = claim_state.get(key)
        if not isinstance(value, list):
            return True
        if value:
            return True
    return False


def _admitted_source_owners(
    claim_state: Mapping[str, Any],
    *,
    semantic_key: str,
    now: datetime,
) -> list[dict[str, str]]:
    # Canonical #511 output is admitted_runs. ``admitted`` is retained only for
    # older exported fixtures/control snapshots; live_runs is a final legacy
    # fallback when no admission set is present at all.
    raw = claim_state.get("admitted_runs")
    source_key = "admitted_runs"
    if raw is None:
        raw = claim_state.get("admitted")
        source_key = "admitted"
    if raw is None:
        raw = claim_state.get("live_runs")
        source_key = "live_runs"

    owners = []
    for item in _records(raw, key=source_key):
        item_semantic = item.get("semantic_key") or item.get("SEMANTIC_KEY")
        if item_semantic != semantic_key:
            continue

        mode = item.get("claim_mode") or item.get("CLAIM_MODE")
        if mode != "SOURCE_MUTATION":
            continue

        run_id = item.get("run_id") or item.get("RUN_ID")
        lease_until = item.get("lease_until") or item.get("LEASE_UNTIL")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("admitted source owner is missing run_id")
        if not isinstance(lease_until, str) or not lease_until.strip():
            raise ValueError("admitted source owner is missing lease_until")

        if _parse_instant(lease_until) <= now:
            continue
        owners.append(
            {
                "run_id": run_id.strip(),
                "lease_until": lease_until.strip(),
            }
        )
    return owners


def evaluate_guard(
    claim_state: Mapping[str, Any],
    mutation_state: Mapping[str, Any],
    *,
    now: str,
    semantic_key: str | None = None,
) -> dict[str, Any]:
    """Evaluate whether branch movement is attributable to the live source owner.

    ``claim_state`` is resolver-compatible JSON. The guard reads canonical
    ``admitted_runs`` (with legacy exported aliases only as fallback) and
    deliberately refuses to derive claim precedence itself. This keeps #510/#511
    as the one claim-state authority.

    ``mutation_state`` must contain ``semantic_key``, ``branch``, ``prior_head``,
    ``current_head`` and optional ordered ``mutations``. Each mutation has
    ``head`` and ``run_id``. If a branch moved while a live owner exists but the
    writer identity is absent or cannot be bound to the current head, the result
    is AMBIGUOUS rather than granting write authority.
    """
    if not isinstance(claim_state, Mapping):
        raise ValueError("claim_state must be an object")
    if not isinstance(mutation_state, Mapping):
        raise ValueError("mutation_state must be an object")

    now_dt = _parse_instant(now)
    state_semantic = _require_text(mutation_state, "semantic_key")
    if semantic_key is not None and semantic_key != state_semantic:
        raise ValueError("semantic_key argument does not match mutation_state")
    semantic = semantic_key or state_semantic
    branch = _require_text(mutation_state, "branch")
    prior_head = _require_text(mutation_state, "prior_head")
    current_head = _require_text(mutation_state, "current_head")

    base = {
        "semantic_key": semantic,
        "branch": branch,
        "prior_head": prior_head,
        "current_head": current_head,
        "branch_moved": prior_head != current_head,
        "admitted_owner_run_id": None,
        "mutation_writer_run_ids": [],
        "evidence": [],
    }

    if _ambiguous_claim_state(claim_state):
        return {
            **base,
            "status": "AMBIGUOUS",
            "evidence": ["claim-state input contains ambiguity/malformed evidence"],
        }

    try:
        owners = _admitted_source_owners(
            claim_state,
            semantic_key=semantic,
            now=now_dt,
        )
    except ValueError as exc:
        return {
            **base,
            "status": "AMBIGUOUS",
            "evidence": [f"invalid admitted-owner evidence: {exc}"],
        }

    if len(owners) > 1:
        return {
            **base,
            "status": "AMBIGUOUS",
            "evidence": ["multiple unexpired admitted SOURCE_MUTATION owners"],
        }

    if not owners:
        return {
            **base,
            "status": "NO_LIVE_OWNER",
            "evidence": ["no unexpired admitted SOURCE_MUTATION owner"],
        }

    owner = owners[0]
    base["admitted_owner_run_id"] = owner["run_id"]
    if prior_head == current_head:
        return {
            **base,
            "status": "OK",
            "evidence": [
                f"branch did not move while {owner['run_id']} owns the live lease"
            ],
        }

    try:
        mutations = _records(mutation_state.get("mutations"), key="mutations")
    except ValueError as exc:
        return {
            **base,
            "status": "AMBIGUOUS",
            "evidence": [f"invalid mutation evidence: {exc}"],
        }

    if not mutations:
        return {
            **base,
            "status": "AMBIGUOUS",
            "evidence": [
                "branch moved under a live owner but no mutation writer evidence was supplied"
            ],
        }

    writers: list[str] = []
    seen_heads: set[str] = set()
    for index, mutation in enumerate(mutations):
        head = mutation.get("head")
        run_id = mutation.get("run_id")
        if not isinstance(head, str) or not head.strip():
            return {
                **base,
                "status": "AMBIGUOUS",
                "evidence": [f"mutations[{index}] is missing head"],
            }
        if head in seen_heads:
            return {
                **base,
                "status": "AMBIGUOUS",
                "evidence": [f"duplicate mutation head {head}"],
            }
        seen_heads.add(head)
        if not isinstance(run_id, str) or not run_id.strip():
            return {
                **base,
                "status": "AMBIGUOUS",
                "evidence": [
                    f"branch moved under a live owner but mutations[{index}] lacks run_id"
                ],
            }
        writers.append(run_id.strip())

    if mutations[-1]["head"].strip() != current_head:
        return {
            **base,
            "status": "AMBIGUOUS",
            "mutation_writer_run_ids": writers,
            "evidence": ["mutation evidence is not bound to current_head"],
        }

    base["mutation_writer_run_ids"] = writers
    foreign = sorted({run_id for run_id in writers if run_id != owner["run_id"]})
    if foreign:
        return {
            **base,
            "status": "COLLISION",
            "evidence": [
                f"live owner is {owner['run_id']} but branch movement includes foreign writer(s): "
                + ", ".join(foreign)
            ],
        }

    return {
        **base,
        "status": "OK",
        "evidence": [
            f"all observed branch movement is attributed to live owner {owner['run_id']}"
        ],
    }


def _load_json(path: str) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed when a source branch moves outside its live claim lease."
    )
    parser.add_argument("--claim-state", required=True)
    parser.add_argument("--mutation-state", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--semantic-key")
    args = parser.parse_args(argv)

    try:
        result = evaluate_guard(
            _load_json(args.claim_state),
            _load_json(args.mutation_state),
            now=args.now,
            semantic_key=args.semantic_key,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "status": "AMBIGUOUS",
            "evidence": [f"input error: {exc}"],
        }

    if result.get("status") not in STATUSES:
        result = {
            "status": "AMBIGUOUS",
            "evidence": ["internal guard produced an invalid status"],
        }

    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    # Only proven live-owner attribution authorizes branch mutation. Every other
    # diagnostic status is fail-closed for shell/workflow callers.
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
