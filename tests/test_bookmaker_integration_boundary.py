from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import unittest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_integration_boundary import (
    BookmakerIntegrationBoundaryError,
    BookmakerIntegrationChannel,
    BookmakerIntegrationChannelEvidence,
)


def _profile(
    *,
    adapter_version: str = "1.0",
    profile_version: int = 1,
    source_payload_sha256: str = "1" * 64,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="venue-a",
        account_id="account-a",
        adapter_id="adapter-a",
        adapter_version=adapter_version,
        profile_version=profile_version,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.UNSUPPORTED,
            ),
        ),
        observed_at="2026-09-21T08:00:00+00:00",
        source_ref="capability-evidence-1",
        source_payload_sha256=source_payload_sha256,
    )


def _evidence(
    channel: BookmakerIntegrationChannel,
    *,
    profile: BookmakerCapabilityProfile | None = None,
    source_payload_sha256: str = "2" * 64,
) -> BookmakerIntegrationChannelEvidence:
    return BookmakerIntegrationChannelEvidence(
        profile=profile or _profile(),
        channel=channel,
        observed_at="2026-09-21T08:10:00+00:00",
        source_ref="integration-channel-evidence-1",
        source_payload_sha256=source_payload_sha256,
    )


class BookmakerIntegrationBoundaryTests(unittest.TestCase):
    def test_official_api_and_browser_channels_never_upgrade_capabilities(self) -> None:
        for channel in (
            BookmakerIntegrationChannel.OFFICIAL_API,
            BookmakerIntegrationChannel.BROWSER_AUTOMATION,
        ):
            with self.subTest(channel=channel):
                profile = _profile()
                evidence = _evidence(channel, profile=profile)

                self.assertTrue(profile.supports(BookmakerCapability.BALANCE_READ))
                self.assertFalse(profile.supports(BookmakerCapability.PLACE_BET))
                self.assertEqual(evidence.profile_id, profile.profile_id)
                self.assertFalse(evidence.technical_capabilities_authorized)
                self.assertFalse(evidence.legal_terms_permission_proven)
                self.assertFalse(evidence.credentials_proven)
                self.assertFalse(evidence.provider_write_authorized)
                self.assertFalse(evidence.execution_authorized)
                self.assertFalse(evidence.real_money_execution)

                projection = evidence.to_canonical_dict()
                self.assertEqual(projection["channel"], channel.value)
                self.assertEqual(projection["profile_id"], profile.profile_id)
                for key in (
                    "technical_capabilities_authorized",
                    "legal_terms_permission_proven",
                    "credentials_proven",
                    "provider_write_authorized",
                    "execution_authorized",
                    "real_money_execution",
                ):
                    self.assertIs(projection[key], False)

    def test_profile_binding_fails_closed_on_adapter_or_profile_drift(self) -> None:
        evidence = _evidence(
            BookmakerIntegrationChannel.OFFICIAL_API,
            profile=_profile(),
        )
        evidence.verify_profile(_profile())

        for candidate in (
            _profile(adapter_version="1.1"),
            _profile(profile_version=2),
            _profile(source_payload_sha256="3" * 64),
        ):
            with self.subTest(profile_id=candidate.profile_id):
                with self.assertRaisesRegex(
                    BookmakerIntegrationBoundaryError,
                    "does not match",
                ):
                    evidence.verify_profile(candidate)

    def test_evidence_identity_is_deterministic_and_channel_sensitive(self) -> None:
        first = _evidence(BookmakerIntegrationChannel.OFFICIAL_API)
        second = _evidence(BookmakerIntegrationChannel.OFFICIAL_API)
        browser = _evidence(BookmakerIntegrationChannel.BROWSER_AUTOMATION)
        changed_source = _evidence(
            BookmakerIntegrationChannel.OFFICIAL_API,
            source_payload_sha256="4" * 64,
        )

        self.assertEqual(first.evidence_id, second.evidence_id)
        self.assertNotEqual(first.evidence_id, browser.evidence_id)
        self.assertNotEqual(first.evidence_id, changed_source.evidence_id)
        self.assertEqual(len(first.evidence_id), 64)

    def test_malformed_channel_evidence_is_rejected(self) -> None:
        profile = _profile()
        common = {
            "profile": profile,
            "channel": BookmakerIntegrationChannel.OFFICIAL_API,
            "observed_at": "2026-09-21T08:10:00+00:00",
            "source_ref": "integration-channel-evidence-1",
            "source_payload_sha256": "2" * 64,
        }

        cases = (
            {"channel": "official_api"},
            {"observed_at": "2026-09-21T08:10:00"},
            {"source_ref": " "},
            {"source_payload_sha256": "A" * 64},
            {"source_payload_sha256": "2" * 63},
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                values = dict(common)
                values.update(replacement)
                with self.assertRaises(BookmakerIntegrationBoundaryError):
                    BookmakerIntegrationChannelEvidence(**values)

    def test_contract_is_frozen_and_has_no_credential_payload_fields(self) -> None:
        evidence = _evidence(BookmakerIntegrationChannel.BROWSER_AUTOMATION)
        with self.assertRaises(FrozenInstanceError):
            evidence.channel = BookmakerIntegrationChannel.OFFICIAL_API

        field_names = {field.name.lower() for field in fields(BookmakerIntegrationChannelEvidence)}
        forbidden_fragments = (
            "password",
            "credential",
            "api_key",
            "token",
            "authorization",
            "cookie",
            "session",
        )
        for fragment in forbidden_fragments:
            self.assertFalse(
                any(fragment in name for name in field_names),
                f"integration evidence schema unexpectedly carries {fragment}",
            )


if __name__ == "__main__":
    unittest.main()
