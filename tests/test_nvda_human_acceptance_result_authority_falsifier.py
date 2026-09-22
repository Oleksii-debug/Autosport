from __future__ import annotations

import pytest

from autosport.nvda_human_acceptance import (
    NvdaHumanAcceptanceError,
    NvdaHumanAcceptanceStructuralResult,
)


def test_direct_structural_result_construction_cannot_mint_complete_verdict() -> None:
    with pytest.raises((TypeError, NvdaHumanAcceptanceError)):
        NvdaHumanAcceptanceStructuralResult(
            transcript_sha256="a" * 64,
        )
