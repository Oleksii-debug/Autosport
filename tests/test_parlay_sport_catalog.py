from __future__ import annotations

from datetime import timedelta
import hashlib
import itertools
import json
import unittest

from autosport.parlay_sport_catalog import (
    ParlayActiveSportCatalogEvidence,
    ParlaySportCapabilityState,
    ParlaySportCatalogError,
    ParlaySportCatalogStatus,
    parse_parlay_sport_catalog,
)


OBSERVED_AT = "2026-09-21T10:00:00+00:00"
SOURCE_REF = "candidate-observation://parlay/v1/sports"


def _rows():
    return [
        {
            "key": "basketball",
            "group": "Basketball",
            "title": "Basketball",
            "description": "Umbrella",
            "active": True,
            "has_outrights": False,
        },
        {
            "key": "basketball_nba",
            "group": "Basketball",
            "title": "NBA",
            "description": "National Basketball Association",
            "active": True,
            "has_outrights": True,
        },
        {
            "key": "table_tennis",
            "group": "Table Tennis",
            "title": "Table Tennis",
            "description": "",
            "active": False,
            "has_outrights": False,
        },
        {
            "key": "tennis_atp",
            "group": "Tennis",
            "title": "ATP",
            "description": "ATP tennis",
            "active": True,
            "has_outrights": True,
        },
    ]


def _payload(rows=None) -> bytes:
    return json.dumps(_rows() if rows is None else rows, ensure_ascii=False).encode("utf-8")


class ParlaySportCatalogTests(unittest.TestCase):
    def test_parse_binds_raw_and_semantic_digests(self):
        payload = _payload()
        evidence = parse_parlay_sport_catalog(
            payload, observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        self.assertEqual(evidence.raw_payload_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(evidence.catalog_id, evidence.canonical_payload_sha256)
        self.assertFalse(evidence.provider_origin_verified)

    def test_row_order_does_not_change_semantic_catalog_identity(self):
        ids = set()
        for rows in itertools.permutations(_rows()):
            evidence = parse_parlay_sport_catalog(
                _payload(list(rows)), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
            )
            ids.add(evidence.catalog_id)
        self.assertEqual(len(ids), 1)

    def test_different_response_bytes_change_evidence_identity(self):
        first = parse_parlay_sport_catalog(
            _payload(_rows()), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        second = parse_parlay_sport_catalog(
            json.dumps(list(reversed(_rows())), separators=(",", ":")).encode(),
            observed_at=OBSERVED_AT,
            source_ref=SOURCE_REF,
        )
        self.assertEqual(first.catalog_id, second.catalog_id)
        self.assertNotEqual(first.raw_payload_sha256, second.raw_payload_sha256)
        self.assertNotEqual(first.evidence_id, second.evidence_id)

    def test_duplicate_json_object_key_is_rejected(self):
        payload = b'[{"key":"basketball_nba","key":"basketball_wnba","group":"Basketball","title":"NBA","description":"x","active":true,"has_outrights":false}]'
        with self.assertRaises(ParlaySportCatalogError):
            parse_parlay_sport_catalog(
                payload, observed_at=OBSERVED_AT, source_ref=SOURCE_REF
            )

    def test_duplicate_sport_key_is_rejected_even_if_rows_match(self):
        rows = [_rows()[1], dict(_rows()[1])]
        with self.assertRaisesRegex(ParlaySportCatalogError, "duplicate sport key"):
            parse_parlay_sport_catalog(
                _payload(rows), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
            )

    def test_provider_booleans_require_exact_bool(self):
        for field in ("active", "has_outrights"):
            for bad in (1, 0, "true", "false", None):
                rows = _rows()
                rows[0] = dict(rows[0], **{field: bad})
                with self.subTest(field=field, bad=bad), self.assertRaisesRegex(
                    ParlaySportCatalogError, f"{field} must be an exact bool"
                ):
                    parse_parlay_sport_catalog(
                        _payload(rows), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
                    )

    def test_unknown_schema_fields_fail_closed(self):
        rows = _rows()
        rows[0] = {**rows[0], "odds_supported": True}
        with self.assertRaisesRegex(ParlaySportCatalogError, "schema-v1 fields"):
            parse_parlay_sport_catalog(
                _payload(rows), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
            )

    def test_active_lookup_remains_observation_only(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        result = evidence.lookup(
            "basketball_nba",
            as_of="2026-09-21T10:05:00+00:00",
            max_age=timedelta(minutes=5),
        )
        self.assertEqual(result.status, ParlaySportCatalogStatus.ACTIVE)
        self.assertTrue(result.is_observed_active)
        self.assertFalse(result.provider_origin_verified)
        self.assertEqual(result.odds_supported, ParlaySportCapabilityState.UNKNOWN)
        self.assertEqual(result.live_supported, ParlaySportCapabilityState.UNKNOWN)
        self.assertEqual(result.historical_supported, ParlaySportCapabilityState.UNKNOWN)
        self.assertFalse(result.provider_write_authorized)
        self.assertFalse(result.real_money_execution_authorized)
        self.assertFalse(result.eligible_for_product_admission)

    def test_inactive_lookup_is_distinct(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        result = evidence.lookup(
            "table_tennis",
            as_of="2026-09-21T10:00:00+00:00",
            max_age=timedelta(0),
        )
        self.assertEqual(result.status, ParlaySportCatalogStatus.INACTIVE)
        self.assertIsNotNone(result.entry)

    def test_missing_key_does_not_fallback(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        result = evidence.lookup(
            "icehockey_nhl",
            as_of="2026-09-21T10:00:01+00:00",
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(result.status, ParlaySportCatalogStatus.UNKNOWN_MISSING)
        self.assertIsNone(result.entry)

    def test_umbrella_and_child_keys_remain_distinct(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        umbrella = evidence.lookup(
            "basketball", as_of=OBSERVED_AT, max_age=timedelta(0)
        )
        child = evidence.lookup(
            "basketball_nba", as_of=OBSERVED_AT, max_age=timedelta(0)
        )
        self.assertNotEqual(umbrella.sport_key, child.sport_key)
        self.assertNotEqual(umbrella.entry, child.entry)

    def test_freshness_boundary_is_inclusive(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        fresh = evidence.lookup(
            "basketball_nba",
            as_of="2026-09-21T10:05:00+00:00",
            max_age=timedelta(minutes=5),
        )
        stale = evidence.lookup(
            "basketball_nba",
            as_of="2026-09-21T10:05:00.000001+00:00",
            max_age=timedelta(minutes=5),
        )
        self.assertEqual(fresh.status, ParlaySportCatalogStatus.ACTIVE)
        self.assertEqual(stale.status, ParlaySportCatalogStatus.UNKNOWN_STALE)
        self.assertIsNone(stale.entry)

    def test_future_observation_is_not_decision_visible(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        result = evidence.lookup(
            "basketball_nba",
            as_of="2026-09-21T09:59:59+00:00",
            max_age=timedelta(days=1),
        )
        self.assertEqual(result.status, ParlaySportCatalogStatus.UNKNOWN_STALE)

    def test_invalid_sport_key_cannot_alias_known_key(self):
        evidence = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        for bad in ("Basketball_NBA", " basketball_nba", "basketball/nba"):
            with self.subTest(bad=bad), self.assertRaises(ParlaySportCatalogError):
                evidence.lookup(bad, as_of=OBSERVED_AT, max_age=timedelta(0))

    def test_manual_digest_tamper_is_rejected(self):
        parsed = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        with self.assertRaisesRegex(ParlaySportCatalogError, "does not match"):
            ParlayActiveSportCatalogEvidence(
                observed_at=parsed.observed_at,
                source_ref=parsed.source_ref,
                raw_payload_sha256=parsed.raw_payload_sha256,
                canonical_payload_sha256="0" * 64,
                entries=parsed.entries,
            )

    def test_relabeling_sport_key_changes_semantic_identity(self):
        first = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        rows = _rows()
        rows[1] = dict(rows[1], key="basketball_wnba", title="WNBA")
        relabeled = parse_parlay_sport_catalog(
            _payload(rows), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        self.assertNotEqual(first.catalog_id, relabeled.catalog_id)

    def test_source_reference_changes_evidence_not_semantic_catalog(self):
        first = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        second = parse_parlay_sport_catalog(
            _payload(),
            observed_at=OBSERVED_AT,
            source_ref="candidate-observation://parlay/v1/sports/replay",
        )
        self.assertEqual(first.catalog_id, second.catalog_id)
        self.assertEqual(first.raw_payload_sha256, second.raw_payload_sha256)
        self.assertNotEqual(first.evidence_id, second.evidence_id)

    def test_schema_version_rejects_bool_alias(self):
        parsed = parse_parlay_sport_catalog(
            _payload(), observed_at=OBSERVED_AT, source_ref=SOURCE_REF
        )
        with self.assertRaisesRegex(ParlaySportCatalogError, "exactly 1"):
            ParlayActiveSportCatalogEvidence(
                observed_at=parsed.observed_at,
                source_ref=parsed.source_ref,
                raw_payload_sha256=parsed.raw_payload_sha256,
                canonical_payload_sha256=parsed.canonical_payload_sha256,
                entries=parsed.entries,
                schema_version=True,
            )

    def test_parser_never_verifies_provider_origin(self):
        parsed = parse_parlay_sport_catalog(
            _payload(),
            observed_at=OBSERVED_AT,
            source_ref="https://parlay-api.com/v1/sports",
        )
        self.assertFalse(parsed.provider_origin_verified)
        with self.assertRaisesRegex(ParlaySportCatalogError, "cannot assert"):
            ParlayActiveSportCatalogEvidence(
                observed_at=parsed.observed_at,
                source_ref=parsed.source_ref,
                raw_payload_sha256=parsed.raw_payload_sha256,
                canonical_payload_sha256=parsed.canonical_payload_sha256,
                entries=parsed.entries,
                provider_origin_verified=True,
            )


if __name__ == "__main__":
    unittest.main()
