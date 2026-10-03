from __future__ import annotations

import bz2
import copy
import hashlib
import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

from autosport.betfair_historical_causal_replay import (
    HistoricalPackageTier,
    HistoricalRepresentation,
)
from autosport.betfair_historical_entitlement import HistoricalProviderOriginWitness
import autosport.betfair_historical_market_definition_origin as origin_module
from autosport.betfair_historical_market_definition_origin import (
    BetfairHistoricalMarketDefinitionOrigin,
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


@contextmanager
def _upstream_authority_stub():
    """Test only: replace the child-captured verifier, not parent class dispatch."""

    def authoritative(witness):
        del witness

    with patch.object(
        origin_module,
        "_CANONICAL_UPSTREAM_WITNESS_ASSERT",
        new=authoritative,
    ), patch.object(
        origin_module,
        "_CANONICAL_UPSTREAM_WITNESS_ASSERT_CODE",
        new=authoritative.__code__,
    ):
        yield


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

    def witness(
        self,
        raw: bytes,
        *,
        provider_path: str = "/data/xds/historic/PRO/table_tennis/file.bz2",
    ) -> HistoricalProviderOriginWitness:
        return HistoricalProviderOriginWitness(
            session_context_id="betfair-session-context:" + _sha("session"),
            entitlement_snapshot_sha256=_sha("entitlement"),
            listing_sha256=_sha("listing"),
            download_file_identity_sha256=_sha("download"),
            provider_path=provider_path,
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
        package_tier: HistoricalPackageTier = HistoricalPackageTier.PRO,
    ):
        # #1345 separately tests issuance of the process-local capability. These
        # composition tests stub only that upstream authority call; raw-file replay,
        # source identity and revision selection remain real #1340 code paths.
        with _upstream_authority_stub():
            return bind_betfair_historical_market_definition_origin(
                witness=witness,
                raw_bytes=raw,
                market_id=market_id or self.MARKET_ID,
                cutoff_pt_ms=cutoff,
                package_tier=package_tier,
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
        self.assertEqual(bound.provider_origin_witness_sha256, witness.witness_sha256)
        with _upstream_authority_stub():
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
        with self.assertRaisesRegex(BetfairHistoricalMarketDefinitionOriginError, "published after"):
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
        with _upstream_authority_stub():
            with self.assertRaisesRegex(
                BetfairHistoricalMarketDefinitionOriginError,
                "raw bytes do not match",
            ):
                bind_betfair_historical_market_definition_origin(
                    witness=witness,
                    raw_bytes=raw + b"tamper",
                    market_id=self.MARKET_ID,
                    cutoff_pt_ms=1000,
                    package_tier=HistoricalPackageTier.PRO,
                )

    def test_future_or_unrelated_revision_cannot_satisfy_requested_market(self) -> None:
        future_raw = _raw(
            _line(3000, self.MARKET_ID, self.definition(status="OPEN", version=1))
        )
        with self.assertRaisesRegex(BetfairHistoricalMarketDefinitionOriginError, "no marketDefinition"):
            self.bind_with_upstream_authority_stub(
                self.witness(future_raw), future_raw, cutoff=2999
            )

        other_raw = _raw(
            _line(1000, "1.other", self.definition(status="OPEN", version=1))
        )
        with self.assertRaisesRegex(BetfairHistoricalMarketDefinitionOriginError, "no marketDefinition"):
            self.bind_with_upstream_authority_stub(
                self.witness(other_raw), other_raw, cutoff=1000
            )

    def test_ambiguous_or_malformed_market_change_fails_closed(self) -> None:
        definition = self.definition(status="OPEN", version=1)
        duplicate = _raw(
            {
                "op": "mcm",
                "pt": 1000,
                "mc": [
                    {"id": self.MARKET_ID, "marketDefinition": definition},
                    {"id": self.MARKET_ID, "marketDefinition": definition},
                ],
            }
        )
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "duplicate marketDefinition revisions",
        ):
            self.bind_with_upstream_authority_stub(
                self.witness(duplicate), duplicate, cutoff=1000
            )

        malformed = _raw({"op": "mcm", "pt": 1000, "mc": {"id": self.MARKET_ID}})
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "mcm.mc must be an exact JSON array",
        ):
            self.bind_with_upstream_authority_stub(
                self.witness(malformed), malformed, cutoff=1000
            )

    def test_package_tier_must_match_authoritative_provider_path(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)

        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "does not match authoritative provider_path package tier",
        ):
            self.bind_with_upstream_authority_stub(
                witness,
                raw,
                cutoff=1000,
                package_tier=HistoricalPackageTier.BASIC,
            )

        basic_witness = self.witness(
            raw,
            provider_path="/data/xds/historic/BASIC/table_tennis/file.bz2",
        )
        bound = self.bind_with_upstream_authority_stub(
            basic_witness,
            raw,
            cutoff=1000,
            package_tier=HistoricalPackageTier.BASIC,
        )
        self.assertIs(bound.package_tier, HistoricalPackageTier.BASIC)
        self.assertEqual(bound.provider_path, basic_witness.provider_path)

    def test_provider_path_must_encode_known_canonical_package_tier(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        cases = (
            (
                "/data/xds/historic/UNKNOWN/table_tennis/file.bz2",
                "unknown historical package tier",
            ),
            (
                "/data/xds/historic/PRO",
                "canonical historical package tier and file path",
            ),
            (
                "/data/xds/historic//table_tennis/file.bz2",
                "canonical historical package tier and file path",
            ),
            (
                "/data/xds/other/PRO/table_tennis/file.bz2",
                "canonical /data/xds/historic/<TIER>/... namespace",
            ),
        )
        for provider_path, message in cases:
            with self.subTest(provider_path=provider_path):
                witness = self.witness(raw, provider_path=provider_path)
                with self.assertRaisesRegex(
                    BetfairHistoricalMarketDefinitionOriginError,
                    message,
                ):
                    self.bind_with_upstream_authority_stub(
                        witness,
                        raw,
                        cutoff=1000,
                        package_tier=HistoricalPackageTier.PRO,
                    )

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


    def test_private_token_direct_constructor_cannot_mint_positive_origin(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        bound = self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)
        fabricated = self.definition(status="CLOSED", version=999)
        canonical = json.dumps(
            fabricated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        forged = BetfairHistoricalMarketDefinitionOrigin(
            provider_origin_witness_sha256=bound.provider_origin_witness_sha256,
            transport_contract_sha256=bound.transport_contract_sha256,
            download_file_identity_sha256=bound.download_file_identity_sha256,
            provider_path=bound.provider_path,
            raw_file_sha256=bound.raw_file_sha256,
            entitlement_snapshot_sha256=bound.entitlement_snapshot_sha256,
            download_retrieved_at=bound.download_retrieved_at,
            replay_source_identity=bound.replay_source_identity,
            source_ordinal=bound.source_ordinal,
            provider_pt_ms=bound.provider_pt_ms,
            line_sha256=_sha("caller-forged-line"),
            record_payload_sha256=_sha("caller-forged-record"),
            market_id=bound.market_id,
            market_definition_sha256=hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest(),
            market_definition_json=canonical,
            package_tier=bound.package_tier,
            representation=bound.representation,
            _witness=witness,
            _token=origin_module._TOKEN,
        )

        with _upstream_authority_stub():
            self.assertTrue(bound.provider_origin_verified)
            self.assertFalse(forged.provider_origin_verified)
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "not issued by the canonical binder",
        ):
            forged.assert_issued_integrity()
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "not issued by the canonical binder",
        ):
            _ = forged.evidence_sha256

    def test_copy_and_replace_do_not_inherit_origin_issuance(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        bound = self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)
        copied = copy.copy(bound)
        replaced = replace(
            bound,
            record_payload_sha256=_sha("replacement-record"),
        )

        bound.assert_issued_integrity()
        for candidate in (copied, replaced):
            with self.subTest(candidate=type(candidate).__name__):
                with self.assertRaisesRegex(
                    BetfairHistoricalMarketDefinitionOriginError,
                    "not issued by the canonical binder",
                ):
                    candidate.assert_issued_integrity()
                with _upstream_authority_stub():
                    self.assertFalse(candidate.provider_origin_verified)

    def test_self_consistent_semantic_mutation_revokes_origin_issuance(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        bound = self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)
        original_evidence_sha = bound.evidence_sha256
        fabricated = self.definition(status="CLOSED", version=999)
        canonical = json.dumps(
            fabricated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        object.__setattr__(bound, "market_definition_json", canonical)
        object.__setattr__(
            bound,
            "market_definition_sha256",
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

        self.assertEqual(len(original_evidence_sha), 64)
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "mutated after canonical issuance",
        ):
            bound.assert_issued_integrity()
        with _upstream_authority_stub():
            self.assertFalse(bound.provider_origin_verified)
        with self.assertRaisesRegex(
            BetfairHistoricalMarketDefinitionOriginError,
            "mutated after canonical issuance",
        ):
            bound.market_definition()


    def test_unissued_copy_cannot_bypass_registry_by_method_substitution(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        bound = self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)
        copied = copy.copy(bound)
        original = vars(BetfairHistoricalMarketDefinitionOrigin)[
            "assert_issued_integrity"
        ]
        hostile_calls: list[object] = []

        def hostile(instance):
            hostile_calls.append(instance)

        type.__setattr__(
            BetfairHistoricalMarketDefinitionOrigin,
            "assert_issued_integrity",
            hostile,
        )
        try:
            with _upstream_authority_stub():
                with self.assertRaisesRegex(
                    BetfairHistoricalMarketDefinitionOriginError,
                    "class dispatch was replaced",
                ):
                    _ = copied.provider_origin_verified
        finally:
            type.__setattr__(
                BetfairHistoricalMarketDefinitionOrigin,
                "assert_issued_integrity",
                original,
            )

        self.assertEqual(hostile_calls, [])
        bound.assert_issued_integrity()

    def test_origin_dispatch_rejects_in_place_integrity_code_replacement(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        bound = self.bind_with_upstream_authority_stub(witness, raw, cutoff=1000)
        target = vars(BetfairHistoricalMarketDefinitionOrigin)[
            "assert_issued_integrity"
        ]
        original_code = target.__code__

        def hostile(instance):
            del instance
            raise AssertionError("hostile origin integrity code executed")

        self.assertEqual(original_code.co_freevars, ())
        self.assertEqual(hostile.__code__.co_freevars, ())
        target.__code__ = hostile.__code__
        try:
            with self.assertRaisesRegex(
                BetfairHistoricalMarketDefinitionOriginError,
                "class dispatch was replaced",
            ):
                _ = bound.evidence_sha256
        finally:
            target.__code__ = original_code

        self.assertEqual(len(bound.evidence_sha256), 64)


    def test_record_extractor_rebinding_fails_before_fabricated_origin_issuance(
        self,
    ) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        hostile_calls: list[bool] = []

        def hostile(record, *, market_id):
            del record, market_id
            hostile_calls.append(True)
            return self.definition(status="CLOSED", version=999)

        with patch.object(
            origin_module,
            "_definition_in_record",
            new=hostile,
        ), _upstream_authority_stub():
            with self.assertRaisesRegex(
                BetfairHistoricalMarketDefinitionOriginError,
                "derivation dispatch was replaced",
            ):
                bind_betfair_historical_market_definition_origin(
                    witness=witness,
                    raw_bytes=raw,
                    market_id=self.MARKET_ID,
                    cutoff_pt_ms=1000,
                    package_tier=HistoricalPackageTier.PRO,
                )

        self.assertEqual(hostile_calls, [])

    def test_record_extractor_in_place_code_replacement_fails_closed(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        target = origin_module._definition_in_record
        original_code = target.__code__
        hostile_calls: list[bool] = []

        def hostile(record, *, market_id):
            del record, market_id
            hostile_calls.append(True)
            return None

        self.assertEqual(original_code.co_freevars, ())
        self.assertEqual(hostile.__code__.co_freevars, ())
        target.__code__ = hostile.__code__
        try:
            with _upstream_authority_stub():
                with self.assertRaisesRegex(
                    BetfairHistoricalMarketDefinitionOriginError,
                    "derivation dispatch was replaced",
                ):
                    bind_betfair_historical_market_definition_origin(
                        witness=witness,
                        raw_bytes=raw,
                        market_id=self.MARKET_ID,
                        cutoff_pt_ms=1000,
                        package_tier=HistoricalPackageTier.PRO,
                    )
        finally:
            target.__code__ = original_code

        self.assertEqual(hostile_calls, [])


    def test_parent_witness_method_rebinding_cannot_bless_child_origin(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)

        with patch.object(
            HistoricalProviderOriginWitness,
            "assert_authoritative",
            autospec=True,
            return_value=None,
        ):
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


    def test_derivation_guard_root_rebinding_fails_before_parser_dispatch(
        self,
    ) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        guard_calls: list[bool] = []

        def hostile_guard():
            guard_calls.append(True)

        with patch.object(
            origin_module,
            "_assert_canonical_origin_derivation_dispatch",
            new=hostile_guard,
        ), _upstream_authority_stub():
            with self.assertRaisesRegex(
                BetfairHistoricalMarketDefinitionOriginError,
                "derivation guard was replaced",
            ):
                bind_betfair_historical_market_definition_origin(
                    witness=witness,
                    raw_bytes=raw,
                    market_id=self.MARKET_ID,
                    cutoff_pt_ms=1000,
                    package_tier=HistoricalPackageTier.PRO,
                )

        self.assertEqual(guard_calls, [])

    def test_child_upstream_verifier_alias_rebinding_fails_closed(self) -> None:
        raw = _raw(_line(1000, self.MARKET_ID, self.definition(status="OPEN", version=1)))
        witness = self.witness(raw)
        hostile_calls: list[bool] = []

        def hostile(witness_arg):
            del witness_arg
            hostile_calls.append(True)

        with patch.object(
            origin_module,
            "_CANONICAL_UPSTREAM_WITNESS_ASSERT",
            new=hostile,
        ):
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

        self.assertEqual(hostile_calls, [])


if __name__ == "__main__":
    unittest.main()
