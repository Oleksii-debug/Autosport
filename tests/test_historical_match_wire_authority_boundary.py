from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Mapping

from autosport.historical_matches import capture_historical_matches
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider


_HEADERS = {
    "x-historical-window-hours": "168",
    "x-historical-window-from": "2026-09-06T00:00:00Z",
    "x-api-version": "wire-boundary-test",
}


class _RawJsonTransport:
    """Model the current transport boundary: raw bytes are decoded before HttpJsonResponse."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.raw_sha256 = hashlib.sha256(raw).hexdigest()
        self.payload = json.loads(raw.decode("utf-8"))
        self.urls: list[str] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpJsonResponse:
        del headers, timeout
        self.urls.append(url)
        return HttpJsonResponse(self.payload, 200, _HEADERS)


class HistoricalMatchWireAuthorityBoundaryTests(unittest.TestCase):
    def _capture(
        self,
        transport: _RawJsonTransport,
        *,
        output_path: Path,
        evidence_path: Path,
    ) -> tuple[object, dict[str, object], dict[str, object]]:
        provider = ParlayApiTableTennisProvider(
            "unit-test-key",
            transport=transport,
            clock=lambda: "2026-09-13T03:00:00+00:00",
            sleeper=lambda _: None,
        )
        report = capture_historical_matches(
            provider,
            requested_date="2026-09-10",
            output_path=output_path,
            evidence_path=evidence_path,
        )
        capture = json.loads(output_path.read_text(encoding="utf-8"))
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        return report, capture, evidence

    def test_canonical_json_alias_cannot_mint_exact_wire_or_outcome_authority(self) -> None:
        raw_a = (
            b'[ { "provider_defined_id": "wire-match", '
            b'"opaque": { "b": 2, "a": 1 } } ]'
        )
        raw_b = (
            b'[\n'
            b'  {"opaque":{"a":1,"b":2},'
            b'"provider_defined_id":"wire-match"}\n'
            b']'
        )
        transport_a = _RawJsonTransport(raw_a)
        transport_b = _RawJsonTransport(raw_b)

        self.assertEqual(transport_a.payload, transport_b.payload)
        self.assertNotEqual(transport_a.raw_sha256, transport_b.raw_sha256)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_a, capture_a, evidence_a = self._capture(
                transport_a,
                output_path=root / "matches-a.json",
                evidence_path=root / "matches-a.evidence.json",
            )
            report_b, capture_b, evidence_b = self._capture(
                transport_b,
                output_path=root / "matches-b.json",
                evidence_path=root / "matches-b.evidence.json",
            )

        # The current response envelope has already discarded exact wire bytes.
        # Semantically identical JSON therefore aliases at the canonical-response
        # boundary even though the provider byte sequences are provably distinct.
        self.assertEqual(
            report_a.canonical_response_sha256,
            report_b.canonical_response_sha256,
        )
        self.assertNotEqual(
            transport_a.raw_sha256,
            report_a.canonical_response_sha256,
        )
        self.assertNotEqual(
            transport_b.raw_sha256,
            report_b.canonical_response_sha256,
        )

        # With all other causal inputs fixed, the durable capture also aliases.
        # That is safe only while this representation is explicitly non-authoritative
        # for provider origin and outcomes.
        self.assertEqual(report_a.capture_sha256, report_b.capture_sha256)

        for report, capture, evidence, raw_sha256 in (
            (report_a, capture_a, evidence_a, transport_a.raw_sha256),
            (report_b, capture_b, evidence_b, transport_b.raw_sha256),
        ):
            self.assertFalse(report.provider_response_origin_verified)
            self.assertFalse(report.trusted_outcome_source_admissible)
            self.assertFalse(capture["trust"]["provider_response_origin_verified"])
            self.assertFalse(capture["trust"]["trusted_outcome_source_admissible"])
            self.assertEqual(
                capture["trust"]["response_origin_limitation"],
                "provider_response_envelope_omits_final_url_and_exact_wire_bytes",
            )
            self.assertFalse(evidence["provider_response_origin_verified"])
            self.assertFalse(evidence["trusted_outcome_source_admissible"])

            persisted = json.dumps(
                {"capture": capture, "evidence": evidence},
                sort_keys=True,
            )
            self.assertNotIn(raw_sha256, persisted)


if __name__ == "__main__":
    unittest.main()
