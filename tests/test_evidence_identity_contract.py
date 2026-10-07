from __future__ import annotations

import pytest

from autosport.evidence import EvidenceItem, ResearchPacket


SHA = "a" * 64
TS = "2026-10-01T12:00:00Z"


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("identity str subclass virtual method must not execute")


class _EvidenceSubclass(EvidenceItem):
    pass


def _item(evidence_id: str = "evidence-1") -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        as_of_ts=TS,
        source="provider-a",
        kind="market-observation",
        payload={"participant": "selection-a"},
        source_hash=SHA,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("evidence_id", " evidence-1"),
        ("evidence_id", "evidence-1 "),
        ("source", " provider-a"),
        ("kind", "market-observation "),
    ),
)
def test_evidence_identity_rejects_noncanonical_whitespace(field: str, value: str) -> None:
    values: dict[str, object] = {
        "evidence_id": "evidence-1",
        "as_of_ts": TS,
        "source": "provider-a",
        "kind": "market-observation",
        "payload": {"participant": "selection-a"},
        "source_hash": SHA,
    }
    values[field] = value

    with pytest.raises(ValueError, match="non-empty trimmed string"):
        EvidenceItem(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ("evidence_id", "as_of_ts", "source", "kind", "source_hash"))
def test_evidence_identity_rejects_str_subclass_before_virtual_method(field: str) -> None:
    values: dict[str, object] = {
        "evidence_id": "evidence-1",
        "as_of_ts": TS,
        "source": "provider-a",
        "kind": "market-observation",
        "payload": {"participant": "selection-a"},
        "source_hash": SHA,
    }
    values[field] = _TrapStr(str(values[field]))

    with pytest.raises(ValueError):
        EvidenceItem(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("as_of_ts", ("not-a-time", "2026-10-01T12:00:00"))
def test_evidence_as_of_must_be_timezone_aware_iso8601(as_of_ts: str) -> None:
    with pytest.raises(ValueError, match="as_of_ts must be"):
        EvidenceItem(
            evidence_id="evidence-1",
            as_of_ts=as_of_ts,
            source="provider-a",
            kind="market-observation",
            payload={},
        )


@pytest.mark.parametrize("source_hash", ("A" * 64, "a" * 63, "not-a-hash"))
def test_evidence_source_hash_is_canonical_sha256_when_present(source_hash: str) -> None:
    with pytest.raises(ValueError, match="source_hash must be a canonical lowercase SHA-256"):
        EvidenceItem(
            evidence_id="evidence-1",
            as_of_ts=TS,
            source="provider-a",
            kind="market-observation",
            payload={},
            source_hash=source_hash,
        )


def test_research_packet_requires_canonical_event_and_generated_time() -> None:
    with pytest.raises(ValueError, match="event_id must be a non-empty trimmed string"):
        ResearchPacket(" event-1", TS, ())
    with pytest.raises(ValueError, match="generated_at must be timezone-aware"):
        ResearchPacket("event-1", "2026-10-01T12:00:00", ())


def test_research_packet_requires_exact_tuple_and_exact_evidence_items() -> None:
    item = _item()
    with pytest.raises(ValueError, match="exact tuple"):
        ResearchPacket("event-1", TS, [item])  # type: ignore[arg-type]

    subclass = _EvidenceSubclass(
        evidence_id="evidence-2",
        as_of_ts=TS,
        source="provider-a",
        kind="market-observation",
        payload={},
        source_hash=SHA,
    )
    with pytest.raises(ValueError, match="exact EvidenceItem"):
        ResearchPacket("event-1", TS, (subclass,))


def test_research_packet_rejects_duplicate_evidence_identity() -> None:
    first = _item("same-evidence")
    second = _item("same-evidence")

    with pytest.raises(ValueError, match="duplicate evidence_id"):
        ResearchPacket("event-1", TS, (first, second))


def test_research_packet_rejects_evidence_after_generation_cutoff() -> None:
    future = EvidenceItem(
        evidence_id="future-evidence",
        as_of_ts="2026-10-01T12:00:01Z",
        source="provider-a",
        kind="market-observation",
        payload={},
        source_hash=SHA,
    )

    with pytest.raises(ValueError, match="after generated_at"):
        ResearchPacket("event-1", "2026-10-01T12:00:00Z", (future,))


def test_research_packet_compares_timestamp_instants_not_lexical_offsets() -> None:
    same_instant = EvidenceItem(
        evidence_id="same-instant",
        as_of_ts="2026-10-01T14:00:00+02:00",
        source="provider-a",
        kind="market-observation",
        payload={},
        source_hash=SHA,
    )

    packet = ResearchPacket(
        "event-1",
        "2026-10-01T12:00:00Z",
        (same_instant,),
    )

    assert packet.evidence == (same_instant,)
