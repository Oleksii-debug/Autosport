"""Bind one exact ResearchProtocol to one frozen multiplicity family.

A ResearchProtocol freezes the statistical comparison contract before final
assessment.  The workspace enrollment journal already prevents one semantic
member from being enrolled twice, but a caller could otherwise mint a new
semantic-variant digest under the same exact protocol and obtain another fresh
alpha-spending family.  Treat the exact protocol id + digest as the durable
family namespace: changing the comparison family requires a new versioned
ResearchProtocol rather than another store or caller-authored member digest.

The underlying enrollment mutation is already serialized by
WorkspaceEconomicLock.  This guard adds only the protocol-level invariant and
then delegates publication to the canonical store implementation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import research_multiplicity as _rm


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

        return original_next_enrollment(target, workspace, plan)

    _rm.SequentialMultiplicityEvidenceStore._next_workspace_enrollment_state = classmethod(
        next_workspace_enrollment_state_with_protocol_freeze
    )


_install_protocol_family_guard()
del _install_protocol_family_guard

__all__: list[str] = []
