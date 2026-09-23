import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from autosport.prophetx_marketdata import ProphetXJsonResponse, ProphetXRestMarketProvider
from autosport.prophetx_snapshot_publication import (
    ProphetXSnapshotPublicationError,
    SQLiteProphetXSnapshotPublicationJournal,
)
from autosport.provider_sequence_authority import SQLiteProviderSequenceAuthority
from autosport.providers import ProviderBatch


def _response(payload):
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return ProphetXJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _market_payload(price=150):
    return {
        "data": {
            "markets": [
                {
                    "event_id": 1001,
                    "market_id": "market-1",
                    "type": "moneyline",
                    "status": "open",
                    "sub_type": None,
                    "selections": [
                        [
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "competitor_id": "team-a",
                                "name": "Team A",
                                "price": price,
                                "quantity": "12.50",
                            }
                        ],
                        [
                            {
                                "strike_id": "strike-b",
                                "outcome_id": "outcome-b",
                                "competitor_id": "team-b",
                                "name": "Team B",
                                "price": -200,
                                "quantity": "9",
                            }
                        ],
                    ],
                }
            ]
        }
    }


class ProphetXSnapshotPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sequence_path = root / "provider-sequence.sqlite"
        self.journal_path = root / "prophetx-publication.sqlite"
        self.sequence = SQLiteProviderSequenceAuthority(
            self.sequence_path,
            authority_id="tests.prophetx-sequence.v1",
            create=True,
        )
        self.journal = SQLiteProphetXSnapshotPublicationJournal(
            self.journal_path,
            authority_id="tests.prophetx-publication.v1",
            sequence_authority=self.sequence,
            event_ids=(1001,),
            create=True,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _provider(self, payload=None, *, sequence=None):
        chosen = _market_payload() if payload is None else payload

        def transport(url, headers, timeout):
            self.assertIn("event_id=1001", url)
            self.assertIn("Authorization", headers)
            return _response(chosen)

        return ProphetXRestMarketProvider(
            "secret-provider-token",
            (1001,),
            sequence_authority=self.sequence if sequence is None else sequence,
            transport=transport,
            clock=lambda: "2026-09-23T19:00:00+00:00",
        )

    def test_complete_snapshot_is_persisted_across_chunking(self):
        result = self.journal.acquire_and_publish(self._provider(), max_items=1)
        self.assertTrue(result.created)
        self.assertEqual(result.publication.acquisition_sequence, 1)
        self.assertEqual(result.publication.quote_count, 2)
        self.assertNotIn("TRUNCATED_BATCH", result.batch.quality_flags)
        self.assertFalse(result.publication.provider_continuity_verified)
        self.assertFalse(result.publication.grants_provider_origin_authority)
        self.assertFalse(result.publication.grants_retention_rights)
        self.assertFalse(result.publication.grants_execution_authority)

        payload = self.journal.resolve_payload(1)
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            result.publication.batch_sha256,
        )
        self.assertNotIn(b"secret-provider-token", payload)
        reopened = SQLiteProphetXSnapshotPublicationJournal(
            self.journal_path,
            authority_id="tests.prophetx-publication.v1",
            sequence_authority=self.sequence,
            event_ids=(1001,),
            create=False,
        )
        self.assertEqual(reopened.resolve(1), result.publication)

    def test_same_response_under_new_sequence_is_a_new_observation(self):
        first = self.journal.acquire_and_publish(self._provider())
        second = self.journal.acquire_and_publish(self._provider())
        self.assertEqual(first.publication.acquisition_sequence, 1)
        self.assertEqual(second.publication.acquisition_sequence, 2)
        self.assertEqual(
            first.publication.snapshot_fingerprint_sha256,
            second.publication.snapshot_fingerprint_sha256,
        )
        self.assertNotEqual(
            first.publication.publication_fingerprint_sha256,
            second.publication.publication_fingerprint_sha256,
        )
        self.assertNotEqual(first.publication.batch_sha256, second.publication.batch_sha256)

    def test_exact_replay_is_idempotent_but_conflicting_replay_fails(self):
        original = self.journal.acquire_and_publish(self._provider())
        replay = self.journal.assert_idempotent_replay(original.batch)
        self.assertFalse(replay.created)
        self.assertEqual(replay.publication, original.publication)

        changed_quote = replace(original.batch.quotes[0], decimal_odds=original.batch.quotes[0].decimal_odds + 1)
        conflicting = ProviderBatch(
            source_id=original.batch.source_id,
            quotes=(changed_quote, *original.batch.quotes[1:]),
            cursor=original.batch.cursor,
            quality_flags=original.batch.quality_flags,
        )
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "already binds different",
        ):
            self.journal.assert_idempotent_replay(conflicting)

    def test_replay_of_unpublished_sequence_cannot_create_state(self):
        batch = self._provider().read_batch()
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "no durable publication",
        ):
            self.journal.assert_idempotent_replay(batch)
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "not retained",
        ):
            self.journal.resolve(1)

    def test_product_sequence_gap_is_allowed_without_continuity_claim(self):
        first = self.journal.acquire_and_publish(self._provider())
        skipped = self.sequence("prophetx:sandbox:rest:v3-affiliate-get-markets")
        third = self.journal.acquire_and_publish(self._provider())
        self.assertEqual(first.publication.acquisition_sequence, 1)
        self.assertEqual(skipped, 2)
        self.assertEqual(third.publication.acquisition_sequence, 3)
        self.assertFalse(third.publication.provider_continuity_verified)

    def test_restart_reopens_sequence_and_publication_state(self):
        first = self.journal.acquire_and_publish(self._provider())
        sequence2 = SQLiteProviderSequenceAuthority(
            self.sequence_path,
            authority_id="tests.prophetx-sequence.v1",
            create=False,
        )
        journal2 = SQLiteProphetXSnapshotPublicationJournal(
            self.journal_path,
            authority_id="tests.prophetx-publication.v1",
            sequence_authority=sequence2,
            event_ids=(1001,),
            create=False,
        )
        self.assertEqual(journal2.resolve(1), first.publication)
        second = journal2.acquire_and_publish(self._provider(sequence=sequence2))
        self.assertEqual(second.publication.acquisition_sequence, 2)

    def test_empty_snapshot_is_retained_without_fabricating_provider_continuity(self):
        provider = self._provider({"data": {"markets": []}})
        result = self.journal.acquire_and_publish(provider)
        self.assertEqual(result.publication.quote_count, 0)
        self.assertEqual(result.batch.quotes, ())
        self.assertFalse(result.publication.provider_continuity_verified)

    def test_partially_consumed_provider_is_rejected(self):
        provider = self._provider()
        first_chunk = provider.read_batch(max_items=1)
        self.assertIn("TRUNCATED_BATCH", first_chunk.quality_flags)
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "snapshot boundary",
        ):
            self.journal.acquire_and_publish(provider)

    def test_scope_and_exact_sequence_authority_are_bound(self):
        other_sequence = SQLiteProviderSequenceAuthority(
            Path(self.tmp.name) / "other-sequence.sqlite",
            authority_id="tests.other-sequence.v1",
            create=True,
        )
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "sequence authority",
        ):
            self.journal.acquire_and_publish(self._provider(sequence=other_sequence))

        wrong_scope = ProphetXRestMarketProvider(
            "secret-provider-token",
            (2002,),
            sequence_authority=self.sequence,
            transport=lambda *args: _response({"data": {"markets": []}}),
            clock=lambda: "2026-09-23T19:00:00+00:00",
        )
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "event scope",
        ):
            self.journal.acquire_and_publish(wrong_scope)

    def test_durable_payload_tamper_fails_closed(self):
        result = self.journal.acquire_and_publish(self._provider())
        connection = sqlite3.connect(self.journal_path)
        try:
            connection.execute(
                """UPDATE prophetx_snapshot_publications_v1
                   SET batch_json=batch_json || ' '
                   WHERE acquisition_sequence=?""",
                (result.publication.acquisition_sequence,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "payload hash mismatch",
        ):
            self.journal.resolve(result.publication.acquisition_sequence)

    def test_journal_trigger_tamper_fails_closed(self):
        self.journal.acquire_and_publish(self._provider())
        connection = sqlite3.connect(self.journal_path)
        try:
            connection.execute(
                """CREATE TRIGGER bad_publication_trigger
                   AFTER UPDATE ON prophetx_snapshot_publications_v1
                   BEGIN
                     SELECT 1;
                   END"""
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(
            ProphetXSnapshotPublicationError,
            "must not have triggers",
        ):
            self.journal.resolve(1)


if __name__ == "__main__":
    unittest.main()
