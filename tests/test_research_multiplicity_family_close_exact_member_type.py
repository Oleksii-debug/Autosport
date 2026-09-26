from __future__ import annotations

import pytest

from autosport.research_multiplicity import SequentialDecision
from autosport.research_multiplicity_family_close import (
    MultiplicityFamilyCloseEvidence,
    TerminalMultiplicityMemberEvidence,
)


class _CallerControlledTerminalMember(TerminalMultiplicityMemberEvidence):
    """An isinstance-compatible member can still own virtual serialization."""

    def to_payload(self) -> dict[str, object]:
        return {
            "member_authority_id": "f" * 64,
            "hypothesis_id": "caller-controlled",
            "evidence_sha256": "f" * 64,
            "decision": SequentialDecision.REJECT_NULL.value,
            "look_index": 999,
            "observed_at": "2099-01-01T00:00:00+00:00",
        }


def _member(
    member_type: type[TerminalMultiplicityMemberEvidence] = TerminalMultiplicityMemberEvidence,
) -> TerminalMultiplicityMemberEvidence:
    return member_type(
        member_authority_id="a" * 64,
        hypothesis_id="hypothesis-a",
        evidence_sha256="b" * 64,
        decision=SequentialDecision.REJECT_NULL,
        look_index=1,
        observed_at="2026-09-25T18:00:00+00:00",
    )


def _close(
    member: TerminalMultiplicityMemberEvidence,
) -> MultiplicityFamilyCloseEvidence:
    return MultiplicityFamilyCloseEvidence(
        family_plan_sha256="c" * 64,
        family_id="family-a",
        research_protocol_id="protocol-a",
        protocol_sha256="d" * 64,
        research_question_id="question-a",
        method_id="method-a",
        journal_state_sha256="e" * 64,
        record_count=1,
        terminal_members=(member,),
    )


def test_canonical_terminal_member_remains_valid() -> None:
    evidence = _close(_member())

    assert evidence.terminal_members[0].hypothesis_id == "hypothesis-a"
    assert evidence.promotion_authorized is False


def test_family_close_rejects_isinstance_compatible_terminal_member_subclass() -> None:
    forged = _member(_CallerControlledTerminalMember)
    assert isinstance(forged, TerminalMultiplicityMemberEvidence)
    assert forged.to_payload()["hypothesis_id"] == "caller-controlled"

    with pytest.raises(
        ValueError,
        match="terminal_members must contain TerminalMultiplicityMemberEvidence values",
    ):
        _close(forged)
