from __future__ import annotations

import unittest

from autosport.betfair_historical_provenance import (
    BetfairHistoricalSnapshot,
    EvaluationStratum,
    FileRedownloadDisposition,
    HistoricalFeatureFact,
    HistoricalFileEvidence,
    HistoricalFileLayout,
    EnrichmentProvenance,
    StreamCaptureEvidence,
    StreamKeyClass,
    canonical_provider_change_identity,
    classify_redownload,
    enrichment_declares_feature_at,
    historical_fact_visible_at,
    qualify_historical_stratum,
    qualify_stream_stratum,
)
from autosport.historical_data_fidelity import HistoricalDataFidelity


BASIC = HistoricalDataFidelity.BETFAIR_BASIC_1M_LTP
PRO = HistoricalDataFidelity.BETFAIR_PRO_TICK_FULL_LADDER
SHA_A = "a" * 64
SHA_B = "b" * 64


def market_file(
    *,
    path: str = "2026/09/20/1.23456789.bz2",
    digest: str = SHA_A,
    size: int = 1234,
    purchase: str = "purchase-42",
    market_id: str = "1.23456789",
) -> HistoricalFileEvidence:
    return HistoricalFileEvidence(
        purchase_item_id=purchase,
        provider_file_path=path,
        file_layout=HistoricalFileLayout.MARKET_FILE,
        file_sha256=digest,
        file_size=size,
        market_id=market_id,
        event_id="event-7",
        provider_publish_start_ts="2026-09-20T10:00:00Z",
        provider_publish_end_ts="2026-09-20T12:00:00Z",
        settlement_available_ts="2026-09-20T13:00:00Z",
        acquisition_ts="2026-09-20T14:00:00Z",
        raw_retention_reference="raw://betfair/purchase-42/1.23456789",
    )


def event_file() -> HistoricalFileEvidence:
    return HistoricalFileEvidence(
        purchase_item_id="purchase-42",
        provider_file_path="2026/09/20/event-7.bz2",
        file_layout=HistoricalFileLayout.EVENT_FILE,
        file_sha256=SHA_B,
        file_size=2222,
        event_id="event-7",
        provider_publish_start_ts="2026-09-20T10:00:00Z",
        provider_publish_end_ts="2026-09-20T12:00:00Z",
        settlement_available_ts="2026-09-20T13:00:00Z",
        acquisition_ts="2026-09-20T14:00:00Z",
        raw_retention_reference="raw://betfair/purchase-42/event-7",
    )


def snapshot(
    *,
    fidelity: HistoricalDataFidelity = BASIC,
    files: tuple[HistoricalFileEvidence, ...] | None = None,
    enrichments: tuple[EnrichmentProvenance, ...] = (),
) -> BetfairHistoricalSnapshot:
    return BetfairHistoricalSnapshot(
        dataset_id="betfair-history-20260920",
        sport="football",
        request_start_date="2026-09-20",
        request_end_date="2026-09-20",
        package_fidelity=fidelity,
        parser_version="stream-parser-v3",
        jurisdiction_class="BETFAIR_COM_SUPPORTED_ACCOUNT",
        files=files or (market_file(),),
        enrichments=enrichments,
    )


class BetfairHistoricalProvenanceTests(unittest.TestCase):
    def test_snapshot_binds_provider_source_and_package_fidelity(self) -> None:
        current = snapshot()
        self.assertEqual(current.provider, "BETFAIR")
        self.assertEqual(current.source_kind, "HISTORICAL_MARKET_STREAM")
        self.assertEqual(current.package_fidelity, BASIC)
        self.assertFalse(current.historical_market_data_proves_execution)
        self.assertFalse(current.external_licensing_authority_verified)
        self.assertFalse(current.provider_account_capability_verified)

    def test_package_fidelity_changes_snapshot_identity(self) -> None:
        self.assertNotEqual(
            snapshot(fidelity=BASIC).snapshot_identity_sha256,
            snapshot(fidelity=PRO).snapshot_identity_sha256,
        )

    def test_snapshot_identity_is_file_order_independent(self) -> None:
        first = market_file(path="a.bz2", digest=SHA_A, market_id="1.1")
        second = market_file(path="b.bz2", digest=SHA_B, market_id="1.2")
        self.assertEqual(
            snapshot(files=(first, second)).snapshot_identity_sha256,
            snapshot(files=(second, first)).snapshot_identity_sha256,
        )

    def test_mixed_market_and_event_layout_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "one canonical historical file layout"):
            snapshot(files=(market_file(), event_file()))

    def test_layout_independent_change_identity_dedupes_same_provider_change(self) -> None:
        left = canonical_provider_change_identity(
            market_id="1.23456789",
            provider_publish_ts="2026-09-20T10:00:01Z",
            change_payload_sha256=SHA_A,
        )
        right = canonical_provider_change_identity(
            market_id="1.23456789",
            provider_publish_ts="2026-09-20T10:00:01+00:00",
            change_payload_sha256=SHA_A,
        )
        self.assertEqual(left, right)

    def test_redownload_same_bytes_is_duplicate(self) -> None:
        self.assertIs(
            classify_redownload(market_file(), market_file()),
            FileRedownloadDisposition.DUPLICATE_BYTES,
        )

    def test_redownload_changed_bytes_is_new_immutable_revision(self) -> None:
        self.assertIs(
            classify_redownload(market_file(), market_file(digest=SHA_B, size=1235)),
            FileRedownloadDisposition.NEW_IMMUTABLE_REVISION,
        )

    def test_redownload_different_purchase_is_not_same_file_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "same purchase_item_id"):
            classify_redownload(market_file(), market_file(purchase="purchase-99"))

    def test_settlement_label_is_hidden_before_settlement_availability(self) -> None:
        fact = HistoricalFeatureFact(
            provider_publish_ts="2026-09-20T12:00:00Z",
            contains_settlement_or_outcome=True,
            settlement_available_ts="2026-09-20T13:00:00Z",
        )
        self.assertFalse(
            historical_fact_visible_at(fact, decision_cutoff_ts="2026-09-20T12:30:00Z")
        )

    def test_settlement_label_becomes_visible_only_after_availability(self) -> None:
        fact = HistoricalFeatureFact(
            provider_publish_ts="2026-09-20T12:00:00Z",
            contains_settlement_or_outcome=True,
            settlement_available_ts="2026-09-20T13:00:00Z",
        )
        self.assertTrue(
            historical_fact_visible_at(fact, decision_cutoff_ts="2026-09-20T13:00:00Z")
        )

    def test_future_market_fact_is_hidden_even_without_settlement_fields(self) -> None:
        fact = HistoricalFeatureFact(provider_publish_ts="2026-09-20T12:00:00Z")
        self.assertFalse(
            historical_fact_visible_at(fact, decision_cutoff_ts="2026-09-20T11:59:59Z")
        )

    def test_historical_archive_qualifies_only_historical_replay(self) -> None:
        current = snapshot()
        self.assertTrue(
            qualify_historical_stratum(
                current, EvaluationStratum.HISTORICAL_REPLAY
            ).compatible
        )
        self.assertFalse(
            qualify_historical_stratum(
                current, EvaluationStratum.LIVE_READ_FORWARD
            ).compatible
        )
        self.assertFalse(
            qualify_historical_stratum(
                current, EvaluationStratum.PAPER_EXECUTION
            ).compatible
        )

    def test_delayed_stream_cannot_satisfy_live_read_forward(self) -> None:
        capture = StreamCaptureEvidence(
            capture_id="delayed-1",
            key_class=StreamKeyClass.DELAYED,
            configured_delay_seconds=3,
            observed_at="2026-09-20T10:00:00Z",
        )
        self.assertTrue(
            qualify_stream_stratum(capture, EvaluationStratum.DELAYED_FORWARD).compatible
        )
        self.assertFalse(
            qualify_stream_stratum(capture, EvaluationStratum.LIVE_READ_FORWARD).compatible
        )

    def test_live_stream_does_not_prove_execution_stratum(self) -> None:
        capture = StreamCaptureEvidence(
            capture_id="live-1",
            key_class=StreamKeyClass.LIVE,
            configured_delay_seconds=0,
            observed_at="2026-09-20T10:00:00Z",
        )
        self.assertTrue(
            qualify_stream_stratum(capture, EvaluationStratum.LIVE_READ_FORWARD).compatible
        )
        self.assertFalse(
            qualify_stream_stratum(capture, EvaluationStratum.SUPERVISED_EXECUTION).compatible
        )

    def test_delayed_stream_requires_provider_delay_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "1..180"):
            StreamCaptureEvidence(
                capture_id="delayed-0",
                key_class=StreamKeyClass.DELAYED,
                configured_delay_seconds=0,
                observed_at="2026-09-20T10:00:00Z",
            )

    def test_missing_enrichment_never_fabricates_score_feature(self) -> None:
        self.assertFalse(
            enrichment_declares_feature_at(
                snapshot(),
                capability="scores",
                decision_cutoff_ts="2026-09-20T11:00:00Z",
            )
        )

    def test_enrichment_is_causal_only_after_its_observed_at(self) -> None:
        enrichment = EnrichmentProvenance(
            dataset_id="score-source-1",
            dataset_sha256=SHA_B,
            source_identity="lawful-score-provider",
            observed_at="2026-09-20T11:30:00Z",
            license_or_terms_reference="terms://score-provider/v1",
            join_policy_id="join-by-provider-event-id-v1",
            capabilities=("cards", "scores"),
        )
        current = snapshot(enrichments=(enrichment,))
        self.assertFalse(
            enrichment_declares_feature_at(
                current,
                capability="scores",
                decision_cutoff_ts="2026-09-20T11:00:00Z",
            )
        )
        self.assertTrue(
            enrichment_declares_feature_at(
                current,
                capability="scores",
                decision_cutoff_ts="2026-09-20T11:30:00Z",
            )
        )

    def test_provider_change_identity_normalizes_equivalent_timezone_offsets(self) -> None:
        left = canonical_provider_change_identity(
            market_id="1.23456789",
            provider_publish_ts="2026-09-20T10:00:00Z",
            change_payload_sha256=SHA_A,
        )
        right = canonical_provider_change_identity(
            market_id="1.23456789",
            provider_publish_ts="2026-09-20T12:00:00+02:00",
            change_payload_sha256=SHA_A,
        )
        self.assertEqual(left, right)

    def test_same_market_cannot_be_double_counted_under_two_market_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "market_id only once"):
            snapshot(
                files=(
                    market_file(path="first.bz2", digest=SHA_A, market_id="1.7"),
                    market_file(path="second.bz2", digest=SHA_B, market_id="1.7"),
                )
            )

    def test_stratum_compatibility_never_self_authorizes_promotion(self) -> None:
        historical = qualify_historical_stratum(
            snapshot(), EvaluationStratum.HISTORICAL_REPLAY
        )
        live = qualify_stream_stratum(
            StreamCaptureEvidence(
                capture_id="live-authority-boundary",
                key_class=StreamKeyClass.LIVE,
                configured_delay_seconds=0,
                observed_at="2026-09-20T10:00:00Z",
            ),
            EvaluationStratum.LIVE_READ_FORWARD,
        )
        self.assertTrue(historical.compatible)
        self.assertTrue(live.compatible)
        self.assertFalse(historical.promotion_authorized)
        self.assertFalse(live.promotion_authorized)

    def test_enrichment_exact_bytes_are_part_of_snapshot_identity(self) -> None:
        left = EnrichmentProvenance(
            dataset_id="score-source-1",
            dataset_sha256=SHA_A,
            source_identity="lawful-score-provider",
            observed_at="2026-09-20T11:30:00Z",
            license_or_terms_reference="terms://score-provider/v1",
            join_policy_id="join-by-provider-event-id-v1",
            capabilities=("scores",),
        )
        right = EnrichmentProvenance(
            dataset_id="score-source-1",
            dataset_sha256=SHA_B,
            source_identity="lawful-score-provider",
            observed_at="2026-09-20T11:30:00Z",
            license_or_terms_reference="terms://score-provider/v1",
            join_policy_id="join-by-provider-event-id-v1",
            capabilities=("scores",),
        )
        self.assertNotEqual(
            snapshot(enrichments=(left,)).snapshot_identity_sha256,
            snapshot(enrichments=(right,)).snapshot_identity_sha256,
        )

    def test_historical_file_time_order_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "acquisition must not precede"):
            HistoricalFileEvidence(
                purchase_item_id="purchase-42",
                provider_file_path="x.bz2",
                file_layout=HistoricalFileLayout.MARKET_FILE,
                file_sha256=SHA_A,
                file_size=1,
                market_id="1.1",
                provider_publish_start_ts="2026-09-20T10:00:00Z",
                provider_publish_end_ts="2026-09-20T12:00:00Z",
                settlement_available_ts="2026-09-20T13:00:00Z",
                acquisition_ts="2026-09-20T12:59:59Z",
                raw_retention_reference="raw://x",
            )


if __name__ == "__main__":
    unittest.main()
