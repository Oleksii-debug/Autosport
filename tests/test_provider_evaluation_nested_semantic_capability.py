import pytest

from autosport._provider_evaluation_semantic_gate import (
    _validate_product_semantic_authority,
)
from autosport.pre_evaluation_binding import BoundPreEvaluationSession
from autosport.pre_evaluation_product_origin import (
    ProductOwnedPreEvaluationSemanticSession,
)
from autosport.pre_evaluation_semantics import PreEvaluationSemanticSession
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseError


class _ForgedPreEvaluationSemanticSession(PreEvaluationSemanticSession):
    @property
    def slots(self):
        return ()


def test_exact_product_wrapper_rejects_nested_semantic_session_subclass() -> None:
    authority = object.__new__(ProductOwnedPreEvaluationSemanticSession)
    object.__setattr__(
        authority,
        "session",
        object.__new__(_ForgedPreEvaluationSemanticSession),
    )
    bound = object.__new__(BoundPreEvaluationSession)

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="requires exact pre-evaluation semantic session",
    ):
        _validate_product_semantic_authority(
            snapshot=object(),
            session_id="session-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256="a" * 64,
            rows=(),
            pre_evaluation_authority=authority,
            pre_evaluation_bound=bound,
        )
