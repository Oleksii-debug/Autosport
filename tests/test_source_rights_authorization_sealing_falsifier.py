from datetime import datetime, timezone

import pytest

from autosport.source_rights_manifest import (
    SourceRightsAuthorization,
    SourceRightsManifestError,
)


def test_caller_cannot_mint_positive_source_rights_authorization() -> None:
    with pytest.raises((TypeError, SourceRightsManifestError)):
        SourceRightsAuthorization(
            source_identity="unverified-source",
            required_scope="historical.read",
            checked_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            manifest_sha256="0" * 64,
            approved_by="release-owner",
            approval_reference="caller-authored-reference",
        )
