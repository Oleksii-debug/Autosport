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
        "wire_hash",
        "json_encoder",
        "action_projection",
        "bound_verifier",
        "approval_clock",
        "spec_constructor",
        "witness_constructor",
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
    elif target == "wire_hash":
        monkeypatch.setattr(confirmation.hashlib, "sha256", lambda *args, **kwargs: None)
    elif target == "json_encoder":
        monkeypatch.setattr(confirmation.json, "dumps", lambda *args, **kwargs: "{}")
    elif target == "action_projection":
        monkeypatch.setattr(
            confirmation.ExecutionAction,
            "to_dict",
            lambda self: {},
        )
    elif target == "bound_verifier":
        monkeypatch.setattr(
            confirmation.BoundSupervisedExecutionPlan,
            "verify_binding",
            lambda self: None,
        )
    elif target == "approval_clock":
        monkeypatch.setattr(
            confirmation.SupervisedApproval,
            "require_active",
            lambda self, now: None,
        )
    elif target == "spec_constructor":
        monkeypatch.setattr(
            confirmation.BetfairExecutionConfirmationSpec,
            "__init__",
            lambda self, *args, **kwargs: None,
        )
    elif target == "witness_constructor":
        monkeypatch.setattr(
            confirmation.BetfairExecutionConfirmationWitness,
            "__init__",
            lambda self, *args, **kwargs: None,
        )
    else:
        monkeypatch.setattr(
            confirmation.BetfairExecutionConfirmationWitness,
            "__post_init__",
            lambda self: None,
        )

    assert not boundary._confirmation_graph_unchanged()


@pytest.mark.parametrize(
    "target",
    (
        "coordinated_resolve_root",
        "coordinated_consume_root",
        "generic_module_helper",
        "generic_authority_helper",
        "generic_monotonic_authority_helper",
    ),
)
def test_final_send_rejects_moving_generic_confirmation_trust_roots(
    monkeypatch,
    target: str,
) -> None:
    assert boundary._confirmation_graph_unchanged()

    if target == "coordinated_resolve_root":
        replacement = lambda *args, **kwargs: None
        monkeypatch.setattr(
            confirmation._AUTHORITY_TYPE,
            "resolve_receipt_binding",
            replacement,
        )
        monkeypatch.setattr(confirmation, "_RESOLVE_BINDING", replacement)
        monkeypatch.setattr(
            confirmation,
            "_RESOLVE_BINDING_CODE",
            replacement.__code__,
        )
    elif target == "coordinated_consume_root":
        replacement = lambda *args, **kwargs: None
        monkeypatch.setattr(
            confirmation._AUTHORITY_TYPE,
            "consume_receipt",
            replacement,
        )
        monkeypatch.setattr(confirmation, "_CONSUME_RECEIPT", replacement)
        monkeypatch.setattr(
            confirmation,
            "_CONSUME_RECEIPT_CODE",
            replacement.__code__,
        )
    elif target == "generic_module_helper":
        monkeypatch.setattr(
            confirmation._confirmation,
            "_require_sha256",
            lambda *args, **kwargs: "0" * 64,
        )
    elif target == "generic_authority_helper":
        monkeypatch.setattr(
            confirmation._confirmation.SupervisedConfirmationAuthority,
            "_load",
            lambda *args, **kwargs: None,
        )
    else:
        monkeypatch.setattr(
            confirmation._confirmation.MonotonicWorkspaceAuthority,
            "read_history",
            lambda self: (),
        )

    assert not boundary._confirmation_graph_unchanged()

