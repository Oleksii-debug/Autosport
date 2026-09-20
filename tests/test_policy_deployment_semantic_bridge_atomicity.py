from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.policy_deployment_semantic_bridge import (
    CanonicalSemanticBinding,
    semantic_binding_path,
    validate_canonical_activation_binding,
)


def test_failed_activation_validation_does_not_persist_semantic_pin(tmp_path) -> None:
    loop_path = tmp_path / "agent-loop.json"
    semantic_binding = CanonicalSemanticBinding(
        activation_binding_id="1" * 64,
        compatibility_scope_id="2" * 64,
        training_authority_id="3" * 64,
        deployment_authority_id="4" * 64,
        training_market_event_dedupe_key="training-event",
        deployment_market_event_dedupe_key="deployment-event",
        training_runtime_authority_id="5" * 64,
        deployment_runtime_authority_id="6" * 64,
    )
    resolved = SimpleNamespace(
        deployment_scope=object(),
        semantic_binding=semantic_binding,
    )

    with patch(
        "autosport.policy_deployment_semantic_bridge.resolve_cross_session_semantics",
        return_value=resolved,
    ), patch(
        "autosport.policy_deployment_semantic_bridge.validate_activation_binding",
        side_effect=RuntimeError("activation rejected"),
    ):
        with pytest.raises(RuntimeError, match="activation rejected"):
            validate_canonical_activation_binding(
                object(),
                semantic_inputs=object(),
                market_store=object(),
                runtime_authority_store=object(),
                loop_path=loop_path,
                require_existing_semantic_binding=False,
                policy=object(),
                training_identity=object(),
                deployment_identity=object(),
                registry=object(),
                artifact_store=object(),
                canonical_strategy_id="strategy-v1",
                admissible_actions=frozenset({"WAIT"}),
                economic_goal_fingerprint="a" * 64,
                risk_fingerprint="b" * 64,
            )

    assert not semantic_binding_path(loop_path).exists()
