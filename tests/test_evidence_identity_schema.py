import unittest

from autosport.evidence import EvidenceItem, ResearchPacket


class _TrapStr(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("nested evidence identity must reject str subclass before virtual methods")


class EvidenceIdentitySchemaTests(unittest.TestCase):
    def _item(self, *, evidence_id: str = "evidence-1", as_of_ts: str = "2026-10-07T00:00:00+00:00"):
        return EvidenceItem(
            evidence_id=evidence_id,
            as_of_ts=as_of_ts,
            source="provider:test",
            kind="research",
            payload={"participant": "selection-a"},
            source_hash="a" * 64,
        )

    def test_valid_identity_fields_preserve_existing_hash_contract(self):
        item = self._item()
        self.assertEqual(item.evidence_id, "evidence-1")
        self.assertEqual(item.as_of_ts, "2026-10-07T00:00:00+00:00")
        self.assertEqual(len(item.canonical_hash), 64)

    def test_evidence_identity_fields_reject_noncanonical_text(self):
        cases = (
            {"evidence_id": " evidence-1"},
            {"evidence_id": "evidence-1 "},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, "canonical string"):
                    self._item(**overrides)

        for field_name, value in (
            ("source", ""),
            ("source", " provider:test"),
            ("kind", "research\x00forged"),
            ("source_hash", " a" * 32),
        ):
            kwargs = {
                "evidence_id": "evidence-2",
                "as_of_ts": "2026-10-07T00:00:00+00:00",
                "source": "provider:test",
                "kind": "research",
                "payload": {},
                "source_hash": "b" * 64,
            }
            kwargs[field_name] = value
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(ValueError, "canonical string"):
                    EvidenceItem(**kwargs)

    def test_source_hash_requires_lowercase_sha256(self):
        for source_hash in ("A" * 64, "a" * 63, "not-a-hash"):
            with self.subTest(source_hash=source_hash):
                kwargs = {
                    "evidence_id": "evidence-hash",
                    "as_of_ts": "2026-10-07T00:00:00+00:00",
                    "source": "provider:test",
                    "kind": "research",
                    "payload": {},
                    "source_hash": source_hash,
                }
                with self.assertRaisesRegex(
                    ValueError,
                    "source_hash must be a canonical lowercase SHA-256",
                ):
                    EvidenceItem(**kwargs)

    def test_source_hash_is_revalidated_before_canonical_hash(self):
        item = self._item()
        object.__setattr__(item, "source_hash", "A" * 64)

        with self.assertRaisesRegex(
            ValueError,
            "source_hash must be a canonical lowercase SHA-256",
        ):
            _ = item.canonical_hash

    def test_evidence_timestamp_requires_explicit_timezone(self):
        for timestamp in (
            "2026-10-07T00:00:00",
            "not-a-timestamp",
        ):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(ValueError):
                    self._item(as_of_ts=timestamp)

    def test_research_packet_rejects_ambiguous_event_identity(self):
        for event_id in ("", " event-1", "event-1 ", "event\x00forged"):
            with self.subTest(event_id=event_id):
                with self.assertRaisesRegex(ValueError, "event_id"):
                    ResearchPacket(
                        event_id=event_id,
                        generated_at="2026-10-07T00:01:00+00:00",
                        evidence=(self._item(),),
                    )

    def test_research_packet_timestamp_requires_explicit_timezone(self):
        with self.assertRaisesRegex(ValueError, "generated_at"):
            ResearchPacket(
                event_id="event-1",
                generated_at="2026-10-07T00:01:00",
                evidence=(self._item(),),
            )

    def test_research_packet_requires_tuple_of_exact_evidence_items(self):
        item = self._item()
        with self.assertRaisesRegex(ValueError, "must be a tuple"):
            ResearchPacket(
                event_id="event-1",
                generated_at="2026-10-07T00:01:00+00:00",
                evidence=[item],
            )
        with self.assertRaisesRegex(ValueError, "EvidenceItem"):
            ResearchPacket(
                event_id="event-1",
                generated_at="2026-10-07T00:01:00+00:00",
                evidence=(object(),),
            )

    def test_research_packet_revalidates_nested_evidence_identity_after_tamper(self):
        item = self._item()
        object.__setattr__(item, "evidence_id", _TrapStr("evidence-1"))

        with self.assertRaisesRegex(ValueError, "evidence_id.*canonical string"):
            ResearchPacket(
                event_id="event-1",
                generated_at="2026-10-07T00:01:00+00:00",
                evidence=(item,),
            )

    def test_canonical_hash_revalidates_identity_after_tamper(self):
        item = self._item()
        object.__setattr__(item, "source", " provider:test")

        with self.assertRaisesRegex(ValueError, "source.*canonical string"):
            _ = item.canonical_hash

    def test_research_packet_rejects_duplicate_evidence_identity(self):
        first = self._item(evidence_id="same-evidence")
        second = EvidenceItem(
            evidence_id="same-evidence",
            as_of_ts="2026-10-07T00:00:01+00:00",
            source="provider:test",
            kind="research",
            payload={"participant": "selection-b"},
        )
        with self.assertRaisesRegex(ValueError, "duplicate evidence identity"):
            ResearchPacket(
                event_id="event-1",
                generated_at="2026-10-07T00:01:00+00:00",
                evidence=(first, second),
            )


if __name__ == "__main__":
    unittest.main()
