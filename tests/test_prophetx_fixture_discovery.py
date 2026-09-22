import hashlib
import json
import unittest
from unittest.mock import patch

from autosport.prophetx_fixture_discovery import (
    ProphetXDiscoveryJsonResponse,
    ProphetXDiscoveryPayloadError,
    ProphetXDiscoveryUnavailable,
    ProphetXFixtureDiscovery,
    _decode_provider_json,
    _default_transport,
)
from autosport.providers import ProviderUnavailableError


def _response(payload, status_code=200):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ProphetXDiscoveryJsonResponse(
        payload=payload,
        status_code=status_code,
        headers={},
        body_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _tournaments(rows):
    return {"data": {"tournaments": rows}}


def _events(rows):
    return {"data": {"sport_events": rows}}


class ProphetXFixtureDiscoveryTests(unittest.TestCase):
    def _discovery(self, tournament_rows=None, events_by_tournament=None, *, mapping=None, clocks=None):
        if tournament_rows is None:
            tournament_rows = [
                {"id": 109, "name": "MLB"},
                {"id": 210, "name": "Some League"},
            ]
        if events_by_tournament is None:
            events_by_tournament = {
                "109": [{"event_id": 1001, "name": "A vs B"}],
                "210": [{"event_id": 2001, "name": "C vs D"}],
            }
        calls = []

        def transport(url, headers, timeout):
            calls.append((url, dict(headers), timeout))
            if url.endswith("/mm/get_tournaments"):
                return _response(_tournaments(tournament_rows))
            tid = url.split("tournament_id=", 1)[1]
            value = events_by_tournament[tid]
            if isinstance(value, Exception):
                raise value
            return _response(_events(value))

        time_values = iter(clocks or [
            "2026-09-22T20:00:00+00:00",
            "2026-09-22T20:00:01+00:00",
            "2026-09-22T20:00:02+00:00",
        ])
        discovery = ProphetXFixtureDiscovery(
            "secret-token",
            data_context_id="sandbox-aggregator-account-a",
            sport_by_tournament_id=mapping,
            transport=transport,
            clock=lambda: next(time_values),
        )
        return discovery, calls

    def test_multiple_tournaments_and_events_preserve_provider_scoped_identity(self):
        discovery, _ = self._discovery(
            events_by_tournament={
                "109": [{"event_id": 1001, "name": "A"}, {"event_id": 1002, "name": "B"}],
                "210": [{"event_id": 2001, "name": "C"}],
            },
            mapping={"109": "baseball"},
        )
        catalog = discovery.discover()
        self.assertTrue(catalog.complete)
        self.assertFalse(catalog.atomic_snapshot)
        self.assertEqual([x.identity for x in catalog.tournaments], ["109", "210"])
        self.assertEqual(
            [x.identity for x in catalog.events],
            [("109", "1001"), ("109", "1002"), ("210", "2001")],
        )
        self.assertEqual([x.canonical_sport for x in catalog.events], ["baseball", "baseball", None])

    def test_name_changes_do_not_change_tournament_or_event_identity(self):
        first, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "Old Tournament Name"}],
            events_by_tournament={"109": [{"event_id": 1001, "name": "Old Event Name"}]},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        second, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "Renamed Tournament"}],
            events_by_tournament={"109": [{"event_id": 1001, "name": "Renamed Event"}]},
            clocks=["2026-09-22T20:10:00Z", "2026-09-22T20:10:01Z"],
        )
        first_catalog = first.discover()
        second_catalog = second.discover()
        self.assertEqual(first_catalog.tournaments[0].identity, second_catalog.tournaments[0].identity)
        self.assertEqual(first_catalog.events[0].identity, second_catalog.events[0].identity)

    def test_conflicting_duplicate_tournament_identity_fails_closed(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}, {"id": 109, "name": "B"}],
            clocks=["2026-09-22T20:00:00Z"],
        )
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "conflicting duplicate tournament"):
            discovery.discover()

    def test_identical_duplicate_tournament_is_idempotent(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}, {"id": 109, "name": "A"}],
            events_by_tournament={"109": []},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        self.assertEqual(len(discovery.discover().tournaments), 1)

    def test_conflicting_duplicate_event_inside_tournament_fails_closed(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": [{"event_id": 1, "name": "A"}, {"event_id": 1, "name": "B"}]},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "conflicting duplicate event"):
            discovery.discover()

    def test_missing_tournaments_collection_is_not_empty_success(self):
        def transport(url, headers, timeout):
            return _response({"data": {}})
        discovery = ProphetXFixtureDiscovery(
            "token", data_context_id="ctx", transport=transport, clock=lambda: "2026-09-22T20:00:00Z"
        )
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "data.tournaments"):
            discovery.discover()

    def test_explicit_empty_tournament_collection_is_complete_zero(self):
        discovery, _ = self._discovery(tournament_rows=[], events_by_tournament={}, clocks=["2026-09-22T20:00:00Z"])
        catalog = discovery.discover()
        self.assertTrue(catalog.complete)
        self.assertEqual(catalog.tournaments, ())
        self.assertEqual(catalog.events, ())
        self.assertEqual(len(catalog.acquisitions), 1)

    def test_explicit_empty_event_collection_is_complete_for_that_scope(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": []},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertTrue(catalog.complete)
        self.assertEqual(catalog.events, ())
        self.assertEqual(len(catalog.acquisitions), 2)

    def test_one_event_request_unavailable_yields_incomplete_catalog_not_silent_omission(self):
        discovery, _ = self._discovery(
            events_by_tournament={
                "109": [{"event_id": 1001, "name": "A"}],
                "210": ProphetXDiscoveryUnavailable("HTTP_429", 429),
            },
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertFalse(catalog.complete)
        self.assertEqual([x.identity for x in catalog.events], [("109", "1001")])
        self.assertEqual(catalog.failures[0].tournament_id, "210")
        self.assertEqual(catalog.failures[0].code, "HTTP_429")
        self.assertEqual(catalog.failures[0].status_code, 429)

    def test_unknown_sport_mapping_never_uses_name_heuristic(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "Major League Baseball MLB"}],
            events_by_tournament={"109": [{"event_id": 1001, "name": "Baseball Game"}]},
            mapping={},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertIsNone(catalog.tournaments[0].canonical_sport)
        self.assertIsNone(catalog.events[0].canonical_sport)

    def test_explicit_sport_mapping_is_keyed_only_by_provider_tournament_id(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "Anything"}],
            events_by_tournament={"109": [{"event_id": 1001, "name": "Anything"}]},
            mapping={"109": "baseball"},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertEqual(catalog.events[0].canonical_sport, "baseball")

    def test_cross_tournament_event_id_reuse_is_explicitly_ambiguous(self):
        discovery, _ = self._discovery(
            events_by_tournament={
                "109": [{"event_id": 7, "name": "A"}],
                "210": [{"event_id": 7, "name": "B"}],
            }
        )
        catalog = discovery.discover()
        self.assertEqual(catalog.ambiguous_event_ids, ("7",))
        self.assertFalse(catalog.globally_unique_event_ids_proven)
        self.assertEqual([x.identity for x in catalog.events], [("109", "7"), ("210", "7")])

    def test_event_tournament_id_must_match_request_scope(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": [{"event_id": 1, "tournament_id": 210, "name": "A"}]},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "request scope"):
            discovery.discover()

    def test_product_observation_time_is_separate_and_no_provider_source_time_is_fabricated(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": []},
            clocks=["2026-09-22T20:00:00+02:00", "2026-09-22T20:00:01+02:00"],
        )
        catalog = discovery.discover()
        self.assertEqual(catalog.acquisitions[0].observed_at, "2026-09-22T18:00:00Z")
        self.assertIsNone(catalog.acquisitions[0].source_timestamp)
        self.assertIsNone(catalog.acquisitions[1].source_timestamp)
        self.assertNotEqual(catalog.acquisitions[0].observed_at, catalog.acquisitions[1].observed_at)
        self.assertFalse(catalog.atomic_snapshot)

    def test_data_context_is_bound_to_every_acquisition_without_token_persistence(self):
        discovery, calls = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": []},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertTrue(all(x.data_context_id == "sandbox-aggregator-account-a" for x in catalog.acquisitions))
        evidence = repr(catalog)
        self.assertNotIn("secret-token", evidence)
        self.assertTrue(all(call[1]["Authorization"] == "Bearer secret-token" for call in calls))

    def test_fixed_read_only_endpoints_keep_token_out_of_url(self):
        discovery, calls = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": []},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        discovery.discover()
        self.assertEqual(calls[0][0], "https://api.sandbox.prophetx.dev/partner/mm/get_tournaments")
        self.assertEqual(calls[1][0], "https://api.sandbox.prophetx.dev/partner/mm/get_sport_events?tournament_id=109")
        self.assertTrue(all("secret-token" not in call[0] for call in calls))
        self.assertTrue(all(call[2] == 10.0 for call in calls))

    def test_non_200_injected_response_is_unavailability_not_empty(self):
        def transport(url, headers, timeout):
            return _response(_tournaments([]), status_code=503)
        discovery = ProphetXFixtureDiscovery(
            "token", data_context_id="ctx", transport=transport, clock=lambda: "2026-09-22T20:00:00Z"
        )
        with self.assertRaises(ProphetXDiscoveryUnavailable) as caught:
            discovery.discover()
        self.assertEqual(caught.exception.code, "HTTP_503")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIsInstance(caught.exception, ProviderUnavailableError)

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_bad_utf8(self):
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "duplicate JSON key"):
            _decode_provider_json(b'{"data":1,"data":2}')
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "non-standard JSON number"):
            _decode_provider_json(b'{"data":NaN}')
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "invalid UTF-8"):
            _decode_provider_json(b'\xff')

    @patch("autosport.prophetx_fixture_discovery.build_opener")
    def test_builtin_transport_sanitizes_timeout_and_secret_shaped_detail(self, build_opener):
        build_opener.return_value.open.side_effect = TimeoutError("Bearer secret-token server detail")
        with self.assertRaises(ProphetXDiscoveryUnavailable) as caught:
            _default_transport(
                "https://api.sandbox.prophetx.dev/partner/mm/get_tournaments",
                {"Authorization": "Bearer secret-token"},
                1.0,
            )
        self.assertEqual(str(caught.exception), "TRANSPORT_UNAVAILABLE")
        self.assertNotIn("secret-token", str(caught.exception))


    def test_duplicate_identity_with_conflicting_unmodeled_provider_field_fails_closed(self):
        discovery, _ = self._discovery(
            tournament_rows=[
                {"id": 109, "name": "A", "classification": {"tier": 1}},
                {"id": 109, "name": "A", "classification": {"tier": 2}},
            ],
            clocks=["2026-09-22T20:00:00Z"],
        )
        with self.assertRaisesRegex(ProphetXDiscoveryPayloadError, "conflicting duplicate tournament"):
            discovery.discover()

    def test_raw_provider_metadata_is_preserved_separately_from_canonical_sport_mapping(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A", "sport_name": "Provider Baseball"}],
            events_by_tournament={
                "109": [{"event_id": 1, "name": "Game", "league_code": "RAW-LEAGUE"}]
            },
            mapping={"109": "baseball"},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertEqual(catalog.tournaments[0].canonical_sport, "baseball")
        self.assertEqual(catalog.events[0].canonical_sport, "baseball")
        self.assertIn("sport_name", dict(catalog.tournaments[0].provider_metadata))
        self.assertIn("league_code", dict(catalog.events[0].provider_metadata))
        self.assertRegex(catalog.tournaments[0].provider_row_sha256, r"^[0-9a-f]{64}$")
        self.assertRegex(catalog.events[0].provider_row_sha256, r"^[0-9a-f]{64}$")

    def test_injected_transport_cannot_claim_verified_provider_origin(self):
        discovery, _ = self._discovery(
            tournament_rows=[{"id": 109, "name": "A"}],
            events_by_tournament={"109": []},
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertTrue(catalog.acquisitions)
        self.assertTrue(all(not item.provider_origin_verified for item in catalog.acquisitions))

    def test_injected_unavailability_detail_is_sanitized_before_durable_failure(self):
        discovery, _ = self._discovery(
            events_by_tournament={
                "109": [{"event_id": 1001, "name": "A"}],
                "210": ProphetXDiscoveryUnavailable("Bearer secret-token provider detail"),
            },
            clocks=["2026-09-22T20:00:00Z", "2026-09-22T20:00:01Z"],
        )
        catalog = discovery.discover()
        self.assertFalse(catalog.complete)
        self.assertEqual(catalog.failures[0].code, "PROVIDER_UNAVAILABLE")
        self.assertIsNone(catalog.failures[0].status_code)
        self.assertNotIn("secret-token", repr(catalog.failures))

    def test_response_rejects_non_http_status_range(self):
        with self.assertRaisesRegex(ValueError, "valid HTTP status"):
            _response(_tournaments([]), status_code=999)

    def test_no_write_account_market_price_or_execution_surface_is_exported(self):
        public = set(dir(ProphetXFixtureDiscovery))
        forbidden = {
            "place_order", "submit_order", "cancel_order", "replace_order",
            "get_balance", "get_order_history", "read_market_quotes", "subscribe",
        }
        self.assertFalse(public & forbidden)


if __name__ == "__main__":
    unittest.main()
