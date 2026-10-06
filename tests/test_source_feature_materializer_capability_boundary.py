from __future__ import annotations

import pytest

from autosport import point_in_time_evidence as evidence
from autosport import source_feature_artifact_authority as source_authority


def test_private_writer_constructor_state_cannot_be_injected_by_ordinary_caller() -> None:
    with pytest.raises(
        evidence.PointInTimeEvidenceError,
        match="only be issued by the canonical source runtime factory",
    ):
        source_authority.SourceFeatureArtifactMaterializer(
            object(),
            collector_store=object(),
            _issuance_capability=(
                source_authority._HEADLESS_COLLECTOR_ISSUANCE_CAPABILITY
            ),
            _source_service=object(),
        )


def test_resolver_only_constructor_still_reaches_canonical_type_validation() -> None:
    with pytest.raises(Exception) as captured:
        source_authority.SourceFeatureArtifactMaterializer(
            object(),
            collector_store=object(),
        )

    assert "source runtime factory" not in str(captured.value)
