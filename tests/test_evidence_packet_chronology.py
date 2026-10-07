from __future__ import annotations

import pytest

from autosport.evidence import EvidenceItem, ResearchPacket


SHA = "a" * 64


def _item(
    as_of_ts: str,
    evidence_id: str = "evidence-1",
    *,
    available_at: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        as_of_ts=as_of_ts,
        source="provider-a",
        kind="market-observation",
        payload={"participant": "selection-a"},
        source_hash=SHA,
        available_at=as_of_ts if available_at is None else available_at,
    )


def test_research_packet_rejects_evidence_after_generation_cutoff() -> None:
    future = _item("2026-10-01T12:00:01Z")

    with pytest.raises(ValueError, match="unavailable at generated_at"):
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:00Z",
            evidence=(future,),
        )


def test_research_packet_rejects_backdated_evidence_learned_after_generation() -> None:
    backdated = _item(
        "2026-10-01T11:59:00Z",
        available_at="2026-10-01T12:00:01Z",
    )

    with pytest.raises(ValueError, match="unavailable at generated_at"):
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:00Z",
            evidence=(backdated,),
        )


def test_research_packet_requires_explicit_availability_witness() -> None:
    unstamped = EvidenceItem(
        evidence_id="unstamped",
        as_of_ts="2026-10-01T11:59:00Z",
        source="provider-a",
        kind="market-observation",
        payload={"participant": "selection-a"},
        source_hash=SHA,
    )

    with pytest.raises(ValueError, match="requires explicit available_at"):
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:00Z",
            evidence=(unstamped,),
        )


def test_research_packet_rejects_content_time_after_availability() -> None:
    impossible = _item(
        "2026-10-01T12:00:00Z",
        available_at="2026-10-01T11:59:59Z",
    )

    with pytest.raises(ValueError, match="as_of_ts cannot be after available_at"):
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:01Z",
            evidence=(impossible,),
        )


def test_availability_witness_is_hash_bound_without_rewriting_legacy_hashes() -> None:
    legacy = EvidenceItem(
        evidence_id="hash-witness",
        as_of_ts="2026-10-01T11:59:00Z",
        source="provider-a",
        kind="market-observation",
        payload={"participant": "selection-a"},
        source_hash=SHA,
    )
    stamped = _item(
        "2026-10-01T11:59:00Z",
        "hash-witness",
        available_at="2026-10-01T11:59:01Z",
    )

    assert legacy.canonical_hash != stamped.canonical_hash


def test_research_packet_accepts_evidence_at_exact_generation_instant() -> None:
    exact = _item("2026-10-01T12:00:00Z")

    packet = ResearchPacket(
        event_id="event-1",
        generated_at="2026-10-01T12:00:00Z",
        evidence=(exact,),
    )

    assert packet.evidence == (exact,)


def test_research_packet_compares_true_instants_across_timezone_offsets() -> None:
    same_instant = _item("2026-10-01T14:00:00+02:00")

    packet = ResearchPacket(
        event_id="event-1",
        generated_at="2026-10-01T12:00:00Z",
        evidence=(same_instant,),
    )

    assert packet.evidence == (same_instant,)


def test_research_packet_rejects_one_future_member_in_mixed_evidence() -> None:
    known = _item("2026-10-01T11:59:59Z", "known")
    future = _item("2026-10-01T12:00:00.000001Z", "future")

    with pytest.raises(ValueError, match="unavailable at generated_at"):
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:00Z",
            evidence=(known, future),
        )


def test_research_packet_rejects_nonzero_submicrosecond_future_evidence() -> None:
    with pytest.raises(ValueError, match="precision finer than microseconds"):
        future = _item("2026-10-01T12:00:00.0000001Z")
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:00Z",
            evidence=(future,),
        )


def test_research_packet_rejects_nonzero_submicrosecond_generation_time() -> None:
    exact = _item("2026-10-01T12:00:00Z")
    with pytest.raises(ValueError, match="precision finer than microseconds"):
        ResearchPacket(
            event_id="event-1",
            generated_at="2026-10-01T12:00:00.0000001Z",
            evidence=(exact,),
        )
