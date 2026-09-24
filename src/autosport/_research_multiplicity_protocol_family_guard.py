"""Keep multiplicity capacity bound to durable scientific hypothesis authority.

A ResearchProtocol freezes one statistical comparison contract, but its id/hash is an
integrity identity rather than proof of a new statistical opportunity.  The workspace
enrollment journal already prevents one semantic member from being enrolled twice.
This guard additionally prevents either:

* a sibling family/store under the same exact protocol revision; or
* a renamed/re-hashed protocol from minting another family for a hypothesis authority
  that is already enrolled anywhere in the same canonical workspace.

The cross-protocol key deliberately reuses ``ExperimentFamilyMember.hypothesis_sha256``.
That digest is the scientific lineage that the canonical trial-family path resolves
against ScientificRegistry before promotion-capable use.  We therefore do not create a
second semantic-equivalence registry here.  A genuinely distinct hypothesis authority
can still be enrolled locally; higher-level TrialFamilyAccounting/ScientificRegistry
remains responsible for proving that authority is product-owned and prospectively
frozen.

The underlying enrollment mutation is already serialized by WorkspaceEconomicLock.
This guard adds only fail-closed checks and then delegates publication to the canonical
store implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import research_multiplicity as _rm


def _hypothesis_authority_sha256s(
    plan: _rm.ExperimentFamilyPlan,
) -> frozenset[str]:
    return frozenset(member.hypothesis_sha256.lower() for member in plan.members)


def _enrolled_plans(
    cls: type[_rm.SequentialMultiplicityEvidenceStore],
    workspace: Path,
    enrollments: dict[str, dict[str, str]],
) -> tuple[_rm.ExperimentFamilyPlan, ...]:
    """Resolve each already-enrolled store through the canonical store validator."""

    seen_store_paths: set[str] = set()
    plans: list[_rm.ExperimentFamilyPlan] = []
    for prior in enrollments.values():
        store_path = prior["store_path"]
        if store_path in seen_store_paths:
            continue
        seen_store_paths.add(store_path)

        candidate = (workspace / store_path).resolve(strict=False)
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(
                "multiplicity workspace enrollment store path escapes canonical workspace"
            ) from exc

        plans.append(cls(candidate, workspace_root=workspace).plan)
    return tuple(plans)


def _install_protocol_family_guard() -> None:
    original_next_enrollment = (
        _rm.SequentialMultiplicityEvidenceStore._next_workspace_enrollment_state
    )

    def next_workspace_enrollment_state_with_protocol_freeze(
        cls: type[_rm.SequentialMultiplicityEvidenceStore],
        target: Path,
        workspace: Path,
        plan: _rm.ExperimentFamilyPlan,
    ) -> dict[str, Any]:
        # Preserve the canonical enrollment validator as the first authority.  Besides
        # computing the next journal state, it owns established fail-closed semantics
        # for duplicate members and deleted enrolled stores.  The cross-protocol guard
        # is additive and must not mask those earlier integrity failures.
        next_enrollments = original_next_enrollment(target, workspace, plan)

        enrollments = cls._read_workspace_enrollments(workspace)
        expected = cls._expected_enrollment(target, workspace, plan)
        protocol_sha256 = plan.protocol_sha256.lower()

        for prior in enrollments.values():
            same_protocol = (
                prior["research_protocol_id"] == plan.research_protocol_id
                and prior["protocol_sha256"] == protocol_sha256
            )
            if same_protocol and prior != expected:
                raise ValueError(
                    "frozen ResearchProtocol is already bound to another "
                    "multiplicity family plan or store"
                )

        requested_hypotheses = _hypothesis_authority_sha256s(plan)
        for prior_plan in _enrolled_plans(cls, workspace, enrollments):
            if requested_hypotheses & _hypothesis_authority_sha256s(prior_plan):
                raise ValueError(
                    "hypothesis authority is already bound to multiplicity capacity; "
                    "a new ResearchProtocol id or digest cannot reset that capacity"
                )

        return next_enrollments

    _rm.SequentialMultiplicityEvidenceStore._next_workspace_enrollment_state = classmethod(
        next_workspace_enrollment_state_with_protocol_freeze
    )


_install_protocol_family_guard()
del _install_protocol_family_guard

__all__: list[str] = []
