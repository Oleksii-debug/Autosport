from __future__ import annotations

import bz2
import hashlib
import json
import unittest
from dataclasses import replace

from autosport.betfair_historical_causal_replay import (
    BetfairHistoricalReplayError,
    BetfairHistoricalReplaySource,
    HistoricalCompression,
    HistoricalFieldUnavailableError,
    HistoricalPackageTier,
    HistoricalRepresentation,
    replay_betfair_historical_until,
    require_observed_field,
    validate_replay_campaign_sources,
)


def _line(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _raw(*payloads: dict, crlf: bool = False) -> bytes:
    sep = b"\r\n" if crlf else b"\n"
    return sep.join(_line(payload) for payload in payloads) + sep


def _source(
    raw: bytes,
    *,
    representation: HistoricalRepresentation = HistoricalRepresentation.MARKET,
    tier: HistoricalPackageTier = HistoricalPackageTier.BASIC,
    compression: HistoricalCompression = HistoricalCompression.PLAIN,
    provider_file_identity: str = "/data/xds/historic/BASIC/1/1.123.bz2",
) -> BetfairHistoricalReplaySource:
    return BetfairHistoricalReplaySource(
        provider_file_identity=provider_file_identity,
        raw_file_sha256=hashlib.sha256(raw).hexdigest(),
        entitlement_snapshot_sha256="1" * 64,
        package_tier=tier,
        representation=representation,
        parser_revision="betfair-stream-v1",
        compression=compression,
    )


class BetfairHistoricalCausalReplayTests(unittest.TestCase):
    def test_future_settlement_is_not_exposed_before_cutoff(self) -> None:
        raw = _raw(
            {"op": "mcm", "pt": 100, "mc": [{"id": "1.1", "rc": [{"id": 7, "ltp": 2.0}]}]},
            {"op": "mcm", "pt": 200, "mc": [{"id": "1.1", "marketDefinition": {"status": "CLOSED"}}]},
        )
        window = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=150)
        self.assertEqual([record.provider_pt_ms for record in window.records], [100])
        self.assertNotIn("CLOSED", window.records[0].payload_json)

    def test_equal_publish_time_preserves_source_ordinal(self) -> None:
        raw = _raw(
            {"op": "mcm", "pt": 100, "mc": [{"id": "first"}]},
            {"op": "mcm", "pt": 100, "mc": [{"id": "second"}]},
            {"op": "mcm", "pt": 101, "mc": [{"id": "third"}]},
        )
        window = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=101)
        self.assertEqual([record.source_ordinal for record in window.records], [1, 2, 3])
        self.assertEqual(
            [record.payload()["mc"][0]["id"] for record in window.records],
            ["first", "second", "third"],
        )

    def test_provider_time_regression_fails_when_reached(self) -> None:
        raw = _raw(
            {"op": "mcm", "pt": 100, "mc": []},
            {"op": "mcm", "pt": 99, "mc": []},
        )
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "regressed"):
            replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=200)

    def test_future_time_regression_is_not_preloaded_before_cutoff(self) -> None:
        raw = _raw(
            {"op": "mcm", "pt": 100, "mc": []},
            {"op": "mcm", "pt": 300, "mc": [{"id": "future"}]},
            {"op": "mcm", "pt": 200, "mc": [{"id": "bad-future-order"}]},
        )
        window = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=150)
        self.assertEqual(len(window.records), 1)
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "regressed"):
            replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=400)

    def test_resume_revalidates_prefix_and_emits_only_new_records(self) -> None:
        raw = _raw(
            {"op": "mcm", "pt": 100, "mc": [{"id": "a"}]},
            {"op": "mcm", "pt": 200, "mc": [{"id": "b"}]},
            {"op": "mcm", "pt": 300, "mc": [{"id": "c"}]},
        )
        source = _source(raw)
        first = replay_betfair_historical_until(raw, source, cutoff_pt_ms=200)
        resumed = replay_betfair_historical_until(
            raw,
            source,
            cutoff_pt_ms=300,
            resume_from=first.cursor,
        )
        self.assertEqual([r.source_ordinal for r in first.records], [1, 2])
        self.assertEqual([r.source_ordinal for r in resumed.records], [3])
        full = replay_betfair_historical_until(raw, source, cutoff_pt_ms=300)
        self.assertEqual(resumed.cursor.replay_state_sha256, full.cursor.replay_state_sha256)
        self.assertEqual(resumed.cursor.next_source_ordinal, full.cursor.next_source_ordinal)

    def test_cursor_state_tamper_fails_closed(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []}, {"op": "mcm", "pt": 200, "mc": []})
        source = _source(raw)
        first = replay_betfair_historical_until(raw, source, cutoff_pt_ms=100)
        forged = replace(first.cursor, replay_state_sha256="2" * 64)
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "self-digest"):
            replay_betfair_historical_until(raw, source, cutoff_pt_ms=200, resume_from=forged)

    def test_cursor_source_drift_fails_closed(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []})
        source = _source(raw)
        cursor = replay_betfair_historical_until(raw, source, cutoff_pt_ms=100).cursor
        rebound = BetfairHistoricalReplaySource(
            provider_file_identity="/different/provider/path",
            raw_file_sha256=source.raw_file_sha256,
            entitlement_snapshot_sha256=source.entitlement_snapshot_sha256,
            package_tier=source.package_tier,
            representation=source.representation,
            parser_revision=source.parser_revision,
            compression=source.compression,
        )
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "source identity"):
            replay_betfair_historical_until(raw, rebound, cutoff_pt_ms=100, resume_from=cursor)

    def test_raw_sha_drift_fails_before_parse(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []})
        source = _source(raw)
        mutated = raw.replace(b"100", b"101")
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "SHA-256"):
            replay_betfair_historical_until(mutated, source, cutoff_pt_ms=100)

    def test_missing_field_never_defaults_to_zero_or_empty(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": [{"id": "1.1"}]})
        record = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=100).records[0]
        self.assertEqual(require_observed_field(record, "pt"), 100)
        with self.assertRaises(HistoricalFieldUnavailableError):
            require_observed_field(record, "mc", "atb")

    def test_historical_record_cannot_claim_live_or_actual_execution(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []})
        record = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=100).records[0]
        self.assertEqual(record.evidence_scope, "RESEARCH_BACKTEST_ONLY")
        self.assertFalse(record.live_quote_proven)
        self.assertFalse(record.actual_execution_proven)

    def test_source_binding_does_not_mint_entitlement_authority(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []})
        source = _source(raw)
        self.assertFalse(source.lawful_provider_evidence_proven)
        self.assertEqual(source.evidence_scope, "RESEARCH_BACKTEST_ONLY")

    def test_campaign_rejects_mixed_market_and_event_representations(self) -> None:
        a = _raw({"op": "mcm", "pt": 100, "mc": []})
        b = _raw({"op": "mcm", "pt": 200, "mc": []})
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "mix M and E"):
            validate_replay_campaign_sources(
                [
                    _source(a, representation=HistoricalRepresentation.MARKET),
                    _source(
                        b,
                        representation=HistoricalRepresentation.EVENT,
                        provider_file_identity="/data/xds/historic/BASIC/1/event.bz2",
                    ),
                ]
            )

    def test_campaign_can_mix_tiers_without_erasing_per_source_tier(self) -> None:
        a = _raw({"op": "mcm", "pt": 100, "mc": []})
        b = _raw({"op": "mcm", "pt": 200, "mc": []})
        basic = _source(a, tier=HistoricalPackageTier.BASIC)
        pro = _source(
            b,
            tier=HistoricalPackageTier.PRO,
            provider_file_identity="/data/xds/historic/PRO/1/1.124.bz2",
        )
        identities = validate_replay_campaign_sources([basic, pro])
        self.assertEqual(identities, (basic.source_identity, pro.source_identity))
        basic_record = replay_betfair_historical_until(a, basic, cutoff_pt_ms=100).records[0]
        pro_record = replay_betfair_historical_until(b, pro, cutoff_pt_ms=200).records[0]
        self.assertEqual(basic_record.package_tier, HistoricalPackageTier.BASIC)
        self.assertEqual(pro_record.package_tier, HistoricalPackageTier.PRO)

    def test_campaign_rejects_duplicate_source_bytes(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []})
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "duplicate historical source bytes"):
            validate_replay_campaign_sources(
                [
                    _source(raw),
                    _source(raw, provider_file_identity="/data/xds/historic/BASIC/1/duplicate.bz2"),
                ]
            )

    def test_bz2_raw_digest_binds_compressed_bytes(self) -> None:
        decoded = _raw({"op": "mcm", "pt": 100, "mc": [{"id": "compressed"}]})
        compressed = bz2.compress(decoded)
        source = _source(
            compressed,
            compression=HistoricalCompression.BZ2,
            provider_file_identity="/data/xds/historic/BASIC/1/1.125.bz2",
        )
        window = replay_betfair_historical_until(compressed, source, cutoff_pt_ms=100)
        self.assertEqual(window.records[0].payload()["mc"][0]["id"], "compressed")

    def test_crlf_is_supported_but_stray_cr_is_rejected(self) -> None:
        raw = _raw({"op": "mcm", "pt": 100, "mc": []}, crlf=True)
        window = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=100)
        self.assertEqual(len(window.records), 1)
        bad = b'{"op":"mcm","pt":100,"mc":[]}\rX\n'
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "stray carriage return"):
            replay_betfair_historical_until(bad, _source(bad), cutoff_pt_ms=100)

    def test_duplicate_json_keys_and_nonfinite_numbers_fail(self) -> None:
        duplicate = b'{"op":"mcm","pt":100,"pt":101,"mc":[]}\n'
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "duplicate JSON key"):
            replay_betfair_historical_until(duplicate, _source(duplicate), cutoff_pt_ms=200)
        nonfinite = b'{"op":"mcm","pt":100,"mc":[],"x":NaN}\n'
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "non-finite"):
            replay_betfair_historical_until(nonfinite, _source(nonfinite), cutoff_pt_ms=200)

    def test_finite_syntax_exponent_overflow_fails_closed_recursively(self) -> None:
        nested_object = (
            b'{"op":"mcm","pt":100,"mc":[],"metrics":{"edge":1e9999}}\n'
        )
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "non-finite JSON number"):
            replay_betfair_historical_until(
                nested_object,
                _source(nested_object),
                cutoff_pt_ms=100,
            )

        nested_array = (
            b'{"op":"mcm","pt":100,"mc":[],"metrics":[0,-1e9999]}\n'
        )
        with self.assertRaisesRegex(BetfairHistoricalReplayError, "non-finite JSON number"):
            replay_betfair_historical_until(
                nested_array,
                _source(nested_array),
                cutoff_pt_ms=100,
            )

    def test_finite_scientific_notation_remains_valid(self) -> None:
        raw = b'{"op":"mcm","pt":100,"mc":[],"metric":1.25e2}\n'
        record = replay_betfair_historical_until(
            raw,
            _source(raw),
            cutoff_pt_ms=100,
        ).records[0]
        self.assertEqual(record.payload()["metric"], 125.0)
        self.assertNotIn("Infinity", record.payload_json)
        self.assertNotIn("NaN", record.payload_json)

    def test_non_mcm_lines_do_not_gain_replay_authority(self) -> None:
        raw = _raw(
            {"op": "connection", "connectionId": "abc"},
            {"op": "mcm", "pt": 100, "mc": []},
        )
        window = replay_betfair_historical_until(raw, _source(raw), cutoff_pt_ms=100)
        self.assertEqual(len(window.records), 1)
        self.assertEqual(window.records[0].source_ordinal, 1)


if __name__ == "__main__":
    unittest.main()
