from __future__ import annotations

import pytest

import autosport._betfair_supervised_public_transport_boundary as boundary
import autosport.betfair_execution_confirmation as confirmation


@pytest.mark.parametrize(
    "target",
    (
        "binding_validator",
        "consumer_key_builder",
        "decision_id_domain",
        "hash_module",
        "witness_validator",
    ),
)
def test_transitive_confirmation_authority_rebinding_fails_closed(
    monkeypatch,
    target: str,
) -> None:
    assert boundary._confirmation_graph_unchanged()

    if target == "binding_validator":
        monkeypatch.setattr(
            confirmation,
            "_require_confirmation_binding",
            lambda *args, **kwargs: None,
        )
    elif target == "consumer_key_builder":
        monkeypatch.setattr(
            confirmation,
            "_consumer_key",
            lambda **kwargs: "betfair-final-send:v1:" + "0" * 64,
        )
    elif target == "decision_id_domain":
        monkeypatch.setattr(
            confirmation,
            "_DECISION_ID_DOMAIN",
            "autosport.betfair-final-send-decision-id.v999",
        )
    elif target == "hash_module":
        monkeypatch.setattr(confirmation, "hashlib", object())
    else:
        monkeypatch.setattr(
            confirmation.BetfairExecutionConfirmationWitness,
            "__post_init__",
            lambda self: None,
        )

    assert not boundary._confirmation_graph_unchanged()
