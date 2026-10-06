from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autosport.domain import MarketEvent
from autosport.market_state_identity import (
    MarketStateIdentityError,
    PROPHETX_REST_MARKET_STATE_CONTRACT,
    same_semantic_market_state,
    semantic_market_state_identity,
)
from autosport.prophetx_marketdata import (
    ProphetXJsonResponse,
    ProphetXRestMarketProvider,
)
from autosport.provider_sequence_authority import SQLiteProviderSequenceAuthority
from autosport.providers import CanonicalNormalizer


_AUTHORITY_ID = "tests.prophetx-rest-state-contract.v1"


def _payload() -> dict[str, object]:
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
                                "price": 150,
                                "quantity": "12.50",
                            },
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "competitor_id": "team-a",
                                "name": "Team A",
                                "price": 140,
                                "quantity": "5",
                            },
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


def _response(payload: object, *, salt: str = "") -> ProphetXJsonResponse:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + salt
    ).encode("utf-8")
    return ProphetXJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256=hashlib.sha256(encoded).hexdigest(),
    )


class _Clock:
    def __init__(self, values: list[str]) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


class ProphetXRestSemanticStateContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sequence_path = self.root / "provider-sequence.sqlite"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _authority(self) -> SQLiteProviderSequenceAuthority:
        return SQLiteProviderSequenceAuthority(
            self.sequence_path,
            authority_id=_AUTHORITY_ID,
            create=not self.sequence_path.exists(),
        )

    def _provider(
        self,
        payloads: list[dict[str, object]],
        *,
        response_salts: list[str] | None = None,
        clocks: list[str] | None = None,
    ) -> ProphetXRestMarketProvider:
        pending_payloads = iter(deepcopy(payloads))
        pending_salts = iter(response_salts or [""] * len(payloads))

        def transport(_url, _headers, _timeout):
            return _response(next(pending_payloads), salt=next(pending_salts))

        return ProphetXRestMarketProvider(
            "secret-test-token",
            (1001,),
            sequence_authority=self._authority(),
            transport=transport,
            clock=_Clock(
                clocks
                or [
                    f"2026-10-04T12:00:{index:02d}+00:00"
                    for index in range(len(payloads))
                ]
            ),
        )

    @staticmethod
    def _normalized(provider: ProphetXRestMarketProvider):
        batch = provider.read_batch()
        normalizer = CanonicalNormalizer()
        return batch, tuple(
            normalizer.normalize(batch.source_id, quote) for quote in batch.quotes
        )

    def test_provider_marks_exact_versioned_semantic_state_contract(self) -> None:
        provider = self._provider([_payload()])
        batch, events = self._normalized(provider)

        self.assertEqual(len(events), 2)
        for quote in batch.quotes:
            self.assertEqual(
                quote.metadata["semantic_state_contract"],
                PROPHETX_REST_MARKET_STATE_CONTRACT,
            )
        for event in events:
            identity = semantic_market_state_identity(event)
            self.assertIsInstance(identity, str)
            self.assertTrue(
                identity.startswith(PROPHETX_REST_MARKET_STATE_CONTRACT + ":")
            )

    def test_fresh_identical_acquisition_has_same_semantic_identity(self) -> None:
        provider = self._provider(
            [_payload(), _payload()],
            response_salts=["first-response", "second-response"],
            clocks=[
                "2026-10-04T12:00:00+00:00",
                "2026-10-04T12:00:01+00:00",
            ],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        self.assertLess(first[0].sequence, second[0].sequence)
        self.assertNotEqual(first[0].observed_ts, second[0].observed_ts)
        self.assertNotEqual(
            first[0].metadata["response_sha256"],
            second[0].metadata["response_sha256"],
        )
        self.assertEqual(
            semantic_market_state_identity(first[0]),
            semantic_market_state_identity(second[0]),
        )
        self.assertTrue(same_semantic_market_state(first[0], second[0]))

    def test_acquisition_only_metadata_does_not_mint_state_transition(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]
        metadata = deepcopy(original.metadata)
        metadata["product_acquisition_sequence"] = original.sequence + 1
        metadata["response_sha256"] = "a" * 64
        metadata["snapshot_fingerprint_sha256"] = "b" * 64
        reacquired = replace(
            original,
            sequence=original.sequence + 1,
            observed_ts="2026-10-04T12:10:00+00:00",
            ingest_ts="2026-10-04T12:10:00+00:00",
            metadata=metadata,
        )

        self.assertEqual(
            semantic_market_state_identity(original),
            semantic_market_state_identity(reacquired),
        )

    def test_depth_change_is_semantic_even_when_best_price_is_unchanged(self) -> None:
        changed = _payload()
        changed["data"]["markets"][0]["selections"][0][1]["quantity"] = "6"
        provider = self._provider([_payload(), changed])
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        self.assertEqual(first[0].decimal_odds, second[0].decimal_odds)
        self.assertNotEqual(
            semantic_market_state_identity(first[0]),
            semantic_market_state_identity(second[0]),
        )

    def test_unrelated_selection_change_does_not_dirty_unchanged_selection(self) -> None:
        changed = _payload()
        changed["data"]["markets"][0]["selections"][1][0]["price"] = -180
        provider = self._provider([_payload(), changed])
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        first_by_selection = {event.selection_id: event for event in first}
        second_by_selection = {event.selection_id: event for event in second}
        strike_a = "prophetx:sandbox:strike-a"
        strike_b = "prophetx:sandbox:strike-b"

        self.assertNotEqual(
            first_by_selection[strike_a].metadata["snapshot_fingerprint_sha256"],
            second_by_selection[strike_a].metadata["snapshot_fingerprint_sha256"],
        )
        self.assertEqual(
            semantic_market_state_identity(first_by_selection[strike_a]),
            semantic_market_state_identity(second_by_selection[strike_a]),
        )
        self.assertNotEqual(
            semantic_market_state_identity(first_by_selection[strike_b]),
            semantic_market_state_identity(second_by_selection[strike_b]),
        )

    def test_best_price_change_is_semantic(self) -> None:
        changed = _payload()
        changed["data"]["markets"][0]["selections"][0][0]["price"] = 160
        provider = self._provider([_payload(), changed])
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        self.assertNotEqual(first[0].decimal_odds, second[0].decimal_odds)
        self.assertNotEqual(
            semantic_market_state_identity(first[0]),
            semantic_market_state_identity(second[0]),
        )

    def test_market_status_change_is_semantic(self) -> None:
        changed = _payload()
        changed["data"]["markets"][0]["status"] = "suspended"
        provider = self._provider([_payload(), changed])
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        self.assertNotEqual(
            semantic_market_state_identity(first[0]),
            semantic_market_state_identity(second[0]),
        )

    def test_request_scope_change_is_semantic(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]
        metadata = deepcopy(original.metadata)
        metadata["request_fingerprint_sha256"] = "0" * 64
        changed_scope = replace(original, metadata=metadata)

        self.assertNotEqual(
            semantic_market_state_identity(original),
            semantic_market_state_identity(changed_scope),
        )

    def test_provider_origin_change_is_semantic(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]
        metadata = deepcopy(original.metadata)
        metadata["provider_origin_verified"] = True
        metadata["provider_origin_authority"] = "fixed_sandbox_https"
        changed_origin = replace(original, metadata=metadata)

        self.assertNotEqual(
            semantic_market_state_identity(original),
            semantic_market_state_identity(changed_origin),
        )

    def test_sequence_authority_change_is_semantic(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]
        metadata = deepcopy(original.metadata)
        metadata["sequence_authority_id"] = "different.authority.v1"
        rebound = replace(original, metadata=metadata)

        self.assertNotEqual(
            semantic_market_state_identity(original),
            semantic_market_state_identity(rebound),
        )

    def test_wrong_sequence_source_namespace_fails_closed(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        metadata = deepcopy(events[0].metadata)
        metadata["sequence_source_id"] = "prophetx:sandbox:rest:other-surface"
        rebound = replace(events[0], metadata=metadata)

        with self.assertRaisesRegex(
            MarketStateIdentityError,
            "canonical sequence source identity",
        ):
            semantic_market_state_identity(rebound)

    def test_malformed_request_fingerprint_fails_closed(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        metadata = deepcopy(events[0].metadata)
        metadata["request_fingerprint_sha256"] = "not-a-sha256"
        malformed = replace(events[0], metadata=metadata)

        with self.assertRaisesRegex(
            MarketStateIdentityError,
            "request fingerprint",
        ):
            semantic_market_state_identity(malformed)

    def test_sequence_metadata_must_match_event_sequence(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        metadata = deepcopy(events[0].metadata)
        metadata["product_acquisition_sequence"] = events[0].sequence + 1
        inconsistent = replace(events[0], metadata=metadata)

        with self.assertRaisesRegex(
            MarketStateIdentityError,
            "acquisition sequence is inconsistent",
        ):
            semantic_market_state_identity(inconsistent)

    def test_unknown_future_contract_fails_closed(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        metadata = deepcopy(events[0].metadata)
        metadata["semantic_state_contract"] = (
            "autosport.prophetx-rest-market-state.v999"
        )
        future = replace(events[0], metadata=metadata)

        with self.assertRaisesRegex(MarketStateIdentityError, "unsupported"):
            semantic_market_state_identity(future)

    def test_absent_contract_preserves_legacy_no_suppression_semantics(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        metadata = deepcopy(events[0].metadata)
        metadata.pop("semantic_state_contract")
        legacy = replace(events[0], metadata=metadata)

        self.assertIsNone(semantic_market_state_identity(legacy))
        self.assertFalse(same_semantic_market_state(legacy, events[0]))

    def test_market_event_subclass_cannot_redefine_identity_serialization(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]

        class DerivedMarketEvent(MarketEvent):
            def to_dict(self):
                return {"forged": True}

        derived = DerivedMarketEvent(
            **{
                field.name: getattr(original, field.name)
                for field in fields(MarketEvent)
            }
        )
        with self.assertRaisesRegex(TypeError, "exact MarketEvent"):
            semantic_market_state_identity(derived)

    def test_contract_rejects_another_source_id(self) -> None:
        provider = self._provider([_payload()])
        _batch, events = self._normalized(provider)
        rebound = replace(events[0], source_id="other-provider")

        with self.assertRaisesRegex(MarketStateIdentityError, "canonical source_id"):
            semantic_market_state_identity(rebound)


if __name__ == "__main__":
    unittest.main()
