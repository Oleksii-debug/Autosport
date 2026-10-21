"""Negative/recovery/contract checks for non-authorizing update preview."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from autosport.windows_update_admission import WindowsUpdateDecision
from autosport.windows_update_signature import VerifiedWindowsUpdate
from autosport.windows_update_transaction_plan import (
    WindowsUpdatePlanError, preview_windows_update_transaction,
)


def receipt(*, old="1.2.0", new="1.3.0", old_schema=2, new_schema=4, disposition="ELIGIBLE_FOR_TRANSACTION_PREPARE"):
    return VerifiedWindowsUpdate(
        decision=WindowsUpdateDecision(disposition, old, new, old_schema, new_schema),
        package_sha256="a" * 64, source_sha="b" * 40,
        version=new, schema=new_schema,
    )


def test_signed_update_produces_exact_read_only_migration_plan():
    plan = preview_windows_update_transaction(
        receipt(), supported_schema_migrations=((2, 3), (3, 4)),
    )
    assert plan["migration_steps"] == [
        {"from_schema": 2, "to_schema": 3},
        {"from_schema": 3, "to_schema": 4},
    ]
    assert plan["backup_required"] is True
    assert plan["restart_recovery_required"] is True
    assert plan["independent_installer_trust_required"] is True
    assert plan["install_authorized"] is False
    assert "PREVIEW_ONLY" in plan["status"]
    assert json.loads(json.dumps(plan, sort_keys=True)) == plan


def test_same_schema_forward_version_still_requires_backup_and_restart():
    plan = preview_windows_update_transaction(
        receipt(new_schema=2), supported_schema_migrations=(),
    )
    assert plan["migration_steps"] == []
    assert plan["backup_required"] is True


def test_no_change_is_non_authorizing_and_does_not_require_backup():
    plan = preview_windows_update_transaction(
        receipt(new="1.2.0", new_schema=2, disposition="NO_CHANGE"),
        supported_schema_migrations=(),
    )
    assert plan["backup_required"] is False
    assert plan["migration_steps"] == []
    assert plan["install_authorized"] is False


@pytest.mark.parametrize("chain", [
    ((2, 3),), ((2, 4), (3, 4)), ((3, 4), (2, 3)),
    ((2, 3), [3, 4]), ((2, True), (3, 4)),
    ((2, 3), (3, 4), (4, 5)), [],
])
def test_invalid_or_incomplete_migration_evidence_is_rejected(chain):
    with pytest.raises(WindowsUpdatePlanError):
        preview_windows_update_transaction(receipt(), supported_schema_migrations=chain)


@pytest.mark.parametrize("bad", [
    "INVALID", "", None, False, 1,
])
def test_forged_disposition_never_becomes_a_preview(bad):
    value = receipt()
    object.__setattr__(value.decision, "disposition", bad)
    with pytest.raises(WindowsUpdatePlanError):
        preview_windows_update_transaction(value, supported_schema_migrations=((2, 3), (3, 4)))


def test_mutated_signed_receipt_cannot_relabel_installer_authority():
    value = receipt()
    object.__setattr__(value, "install_authorized", True)
    with pytest.raises(WindowsUpdatePlanError):
        preview_windows_update_transaction(value, supported_schema_migrations=((2, 3), (3, 4)))


def test_mismatched_candidate_identity_rejected():
    value = receipt()
    object.__setattr__(value, "version", "2.0.0")
    with pytest.raises(WindowsUpdatePlanError):
        preview_windows_update_transaction(value, supported_schema_migrations=((2, 3), (3, 4)))


def test_invalid_input_type_and_hostile_subclass_rejected():
    class Hostile(VerifiedWindowsUpdate):
        pass
    for value in (None, {}, Hostile(**{
        "decision": receipt().decision, "package_sha256": "a" * 64,
        "source_sha": "b" * 40, "version": "1.3.0", "schema": 4,
    })):
        with pytest.raises(WindowsUpdatePlanError):
            preview_windows_update_transaction(value, supported_schema_migrations=((2, 3), (3, 4)))


def test_no_change_cannot_carry_migration():
    with pytest.raises(WindowsUpdatePlanError):
        preview_windows_update_transaction(
            receipt(new="1.2.0", new_schema=2, disposition="NO_CHANGE"),
            supported_schema_migrations=((2, 3),),
        )


def test_preview_does_not_mutate_receipt_or_migration_tuple():
    value = receipt()
    edges = ((2, 3), (3, 4))
    original = replace(value)
    first = preview_windows_update_transaction(value, supported_schema_migrations=edges)
    first["migration_steps"].clear()
    second = preview_windows_update_transaction(value, supported_schema_migrations=edges)
    assert value == original and edges == ((2, 3), (3, 4))
    assert len(second["migration_steps"]) == 2
