from __future__ import annotations

import bz2
import hashlib
import json
import unittest
from unittest.mock import patch

from autosport.betfair_historical_causal_replay import (
    HistoricalPackageTier,
    HistoricalRepresentation,
)
from autosport.betfair_historical_entitlement import HistoricalProviderOriginWitness
from autosport.betfair_historical_market_definition_origin import (
    BetfairHistoricalMarketDefinitionOriginError,
    bind_betfair_historical_market_definition_origin,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _line(pt: int, market_id: str, definition: object) -> dict[str, object]:
    return {
        "op": "mcm",
        "pt": pt,
        "mc": [{"id": market_id, "marketDefinition": definition}],
    }


def _raw(*records: dict[str, object]) -> bytes:
    payload = "\n".join(
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for record in records
    ).encode("utf-8")
    return bz2.compress(payload + b"\n")


class BetfairHistoricalMarketDefinitionOriginTests(unittest.TestCase):
    MARKET_ID = "1.23456789"

    def definition(self, *, status: str, version: int) -> dict[str, object]:
        return {
            "eventId": "event-123",
            "eventTypeId": "2593174",
            "marketType": "MATCH_ODDS",
            "status": status,
            "version": version,
            "runners": [{"id": 11}, {"id": 22}, {"id": 33}],
        }

    def witness(self, raw: bytes) -> HistoricalProviderOriginWitness:
        return HistoricalProviderOriginWitness(
            session_context_id=_sha("session"),
            entitlement_snapshot_sha256=_sha("entitlement"),
            listing_sha256=_sha("listing"),
            download_file_identity_sha256=_sha("download"),
            provider_path="/data/xds/historic/table_tennis/file.bz2",
            retrieved_at="2026-09-23T06:30:00+00:00",
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
            transport_contract_sha256=_sha("transport-contract"),
        )

    def bind_with_upstream_authority_stub(
        self,
        witness: HistoricalProviderOriginWitness,
        raw: bytes,
        *,
        cutoff: int = 2500,
        market_id: str | None = None,
    ):
        # #1345 separately tests issuance of the process-local capability.  These
        # composition tests stub only that upstream authority call; raw-file replay,
        # source identity and revision selection remain real #1340 code paths.
        with patch.object(
            HistoricalProviderOriginWitness,
            "assert_authoritative",
            autospec=True,
            return_value=None,
        ):
            return bind_betfair_historical_market_definition_origin(
                witness=witness,
                raw_bytes=raw,
                market_id=market_id or self.MARKET_ID,
                cutoff_pt_ms=cutoff,
                package_tier=HistoricalPackageTier.PRO,
                representation=HistoricalRepresentation.MARKET,
            )

    def test_latest_visible_revision_binds_exact_authenticated_file_and_keeps_times_separate(self) -> None:
        first = self.definition(status="OPEN", version=1)
        second = self.definition(status="OPEN", version=2)
        future = self.definition(status="CLOSED", version=3)
        raw = _raw(
            _line(1000, self.MARKET_ID, first),
            _line(2000, self.MARKET_ID, second),
            _line(3000, self.MARKET_ID, future),
        )
        witness = self.witness(raw)

        bound = self.bind_with_upstream_authority_stub(witness, raw)

        self.assertEqual(bound.provider_pt_ms, 2000)
        self.assertEqual(bound.source_ordinal, 2)
        self.assertEqual(bound.market_definition(), second)
        self.assertEqual(bound.raw_file_sha256, witness.raw_sha256)
        self.assertEqual(bound.provider_path, witness.provider_path)
        self.assertEqual(bound.download_retrieved_at, witness.retrieved_at)
        self.assertEqual(
            bound.provider_origin_witness_sha256,
            witness.witness_sha256,
        )
        with patch.object(
            HistoricalProviderOriginWitness,
            "assert_authoritative",
            autospec=True,
            return_value=None,
        ):
            truth = bound.to_dict()["truth"]
            self.assertTrue(truth["provider_origin_verified"])
            bound.assert_provider_origin()
        self.assertTrue(truth["historical_provider_publish_time_bound"])
        self.assertTrue(truth["download_acquisition_time_kept_separate"])
        self.assertFalse(truth["product_observation_time_backdated_from_provider_pt"])
        self.assertFalse(truth["usage_rights_verified"])
        self.assertTrue(truth["rights_revalidation_required"])
        self.assertFalse(truth["source_stream_continuity_proven"])
        self.assertFalse(truth["outcome_roster_completeness_proven"])
        self.assertFalse(truth["live_quote_proven"])
        self.assertFalse(truth["execution_authority"])
        self.assertFalse(truth["promotion_authority"])
        self.assertFalse(truth["real_money_execution"])
        bound.assert_published_by(2000)
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "published after",
        ):
            bound.assert_published_by(1999)

    def test_caller_constructed_upstream_witness_cannot_mint_origin(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)

        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "lacks live canonical provider-origin authority",
        ):
            bind_betfair_historical_market_definition_origin(
                witness=witness,
                raw_bytes=raw,
                market_id=self.MARKET_ID,
                cutoff_pt_ms=1000,
                package_tier=HistoricalPackageTier.PRO,
            )

    def test_authoritative_witness_cannot_be_reused_for_mutated_bytes(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        mutated = raw + b"tamper"

        with patch.object(
            HistoricalProviderOriginWitness,
            "assert_authoritative",
            autospec=True,
            return_value=None,
        ):
            with self.assertRaisesRegex(
                BetfairHistoricalMarketDefinitionOriginError,
                "raw bytes do not match",
            ):
                bind_betfair_historical_market_definition_origin(
                    witness=witness,
                    raw_bytes=mutated,
                    market_id=self.MARKET_ID,
                    cutoff_pt_ms=1000,
                    package_tier=HistoricalPackageTier.PRO,
                )

    def test_future_only_revision_is_not_visible_before_provider_cutoff(self) -> None:
        raw = _raw(_line(3000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)

        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "no marketDefinition revision",
        ):
            self.bind_with_upstream_authority_stub(witness, raw, cutoff=2999)

    def test_unrelated_market_cannot_satisfy_requested_market_identity(self) -> None:
        raw = _raw(_line(1000, "1.other", self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)

        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "no marketDefinition revision",
        ):
            self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)

    def test_duplicate_market_revision_inside_one_provider_record_fails_closed(self) -> None:
        definition = self.definition(status="OPEN", version=1)
        raw = _raw(
            {
                "op": "mcm",
                "pt": 1000,
                "mc": [
                    {"id": self.MARKET_ID, "marketDefinition": definition},
                    {"id": self.MARKET_ID, "marketDefinition": definition},
                ],
            }
        )
        witness = self.witness(raw)

        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "duplicate marketDefinition revisions",
        ):
            self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)

    def test_malformed_market_change_container_fails_closed(self) -> None:
        raw = _raw({"op": "mcm", "pt": 1000, "mc": {"id": self.MARKET_ID}})
        witness = self.witness(raw)

        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "mcm.mc must be an exact JSON array",
        ):
            self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)

    def test_dynamic_provider_origin_truth_revokes_with_upstream_capability(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        bound = self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)

        # Outside the explicit composition-test stub this caller-created witness is
        # not in #1345's process-local issuer registry, so positive origin truth drops.
        self.assertFalse(bound.provider_origin_verified)
        self.assertFalse(bound.to_dict()["truth"]["provider_origin_verified"])
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "no longer authoritative",
        ):
            bound.assert_provider_origin()


if __name__ == "__main__":
    unittest.main()
