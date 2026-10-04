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

@pytest.mark.parametrize(
    ("type_name", "member_name", "replacement_kind"),
    (
        ("SupervisedExecutionReview", "expires_at", "slot"),
        ("OperatorConfirmationReceipt", "consumed_at", "slot"),
        ("SupervisedConfirmationBinding", "review", "slot"),
        ("_Record", "payload", "slot"),
        ("_State", "reviews", "slot"),
        ("SupervisedExecutionReview", "__init__", "callable"),
        ("OperatorConfirmationReceipt", "__init__", "callable"),
        ("SupervisedConfirmationBinding", "__init__", "callable"),
        ("_Record", "__init__", "callable"),
        ("_State", "__init__", "callable"),
    ),
)
def test_generic_confirmation_projection_surface_rebinding_fails_closed(
    monkeypatch,
    type_name: str,
    member_name: str,
    replacement_kind: str,
) -> None:
    assert boundary._confirmation_graph_unchanged()
    projection_type = getattr(confirmation._confirmation, type_name)

    if replacement_kind == "slot":
        monkeypatch.setattr(
            projection_type,
            member_name,
            property(lambda self: None),
        )
    else:
        monkeypatch.setattr(
            projection_type,
            member_name,
            lambda self, *args, **kwargs: None,
        )

    assert not boundary._confirmation_graph_unchanged()

@pytest.mark.parametrize(
    "member_name",
    (
        "ledger",
        "bound",
        "approval",
        "action_id",
        "attempt_id",
        "client",
        "workspace",
        "receipt_id",
        "review_sha256",
    ),
)
def test_execution_confirmation_context_slot_rebinding_fails_closed(
    monkeypatch,
    member_name: str,
) -> None:
    assert boundary._confirmation_graph_unchanged()
    monkeypatch.setattr(
        boundary._ExecutionConfirmationContext,
        member_name,
        property(lambda self: None),
    )
    assert not boundary._confirmation_graph_unchanged()


def test_execution_confirmation_context_constructor_rebinding_fails_closed(
    monkeypatch,
) -> None:
    assert boundary._confirmation_graph_unchanged()
    monkeypatch.setattr(
        boundary._ExecutionConfirmationContext,
        "__init__",
        lambda self, *args, **kwargs: None,
    )
    assert not boundary._confirmation_graph_unchanged()

@pytest.mark.parametrize(
    ("type_name", "member_name", "replacement_kind"),
    (
        ("ExecutionAction", "expires_at", "slot"),
        ("ExecutionPlan", "plan_id", "slot"),
        ("ExecutionAttempt", "attempt_id", "slot"),
        ("ExecutionAttemptReadView", "submitted_request_sha256", "slot"),
        ("VerifiedExecutionPlanView", "attempts", "slot"),
        ("ExecutionAction", "__init__", "callable"),
        ("ExecutionAttemptReadView", "__init__", "callable"),
        ("VerifiedExecutionPlanView", "__init__", "callable"),
    ),
)
def test_final_send_ledger_projection_surface_rebinding_fails_closed(
    monkeypatch,
    type_name: str,
    member_name: str,
    replacement_kind: str,
) -> None:
    assert boundary._confirmation_graph_unchanged()
    view_type = getattr(boundary._ledger_runtime, type_name)

    if replacement_kind == "slot":
        monkeypatch.setattr(
            view_type,
            member_name,
            property(lambda self: None),
        )
    else:
        monkeypatch.setattr(
            view_type,
            member_name,
            lambda self, *args, **kwargs: None,
        )

    assert not boundary._confirmation_graph_unchanged()

