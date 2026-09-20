from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from autosport.campaign_cost_evidence import CostBasis, CostClass, CostEvidenceError, CostTreatment
from autosport.owner_fixed_expense_authority import (
    OwnerFixedExpenseAuthority,
    OwnerFixedExpenseAuthorityError,
    SOURCE_FAMILY,
)
from test_campaign_cost_evidence import _fixture_authority


UTC = timezone.utc


def test_owner_record_derives_membership_treatment_amount_and_currency(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        projection = campaign.projection()
        authority = OwnerFixedExpenseAuthority(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        incurred = datetime.now(UTC) - timedelta(minutes=1)
        record = authority.record_expense(
            campaign=campaign,
            amount=Decimal("3.25"),
            currency="EUR",
            incurred_at=incurred,
        )
        cost = authority.resolve_cost_evidence(
            campaign=campaign,
            expense_id=record.expense_id,
            record_sha256=record.record_sha256,
            as_of=record.recorded_at + timedelta(seconds=1),
        )
        assert cost.cost_class is CostClass.FIXED_CAMPAIGN
        assert cost.basis is CostBasis.OBSERVED_INCURRED
        assert cost.treatment is CostTreatment.SUBTRACT_FROM_GROSS
        assert cost.amount == Decimal("3.25")
        assert cost.currency == "EUR"
        assert cost.memberships == projection.membership_refs
        assert cost.source.family == SOURCE_FAMILY
    finally:
        fixture.doCleanups()


def test_arbitrary_receipt_object_cannot_enter_owner_authority(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = OwnerFixedExpenseAuthority(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        with pytest.raises(TypeError):
            authority.record_expense(
                campaign=campaign,
                receipt={
                    "source_class": "PROVIDER_DATA",
                    "source_authority_id": "provider-account-7",
                    "amount": "100",
                    "currency": "EUR",
                },
            )
    finally:
        fixture.doCleanups()


def test_owner_authority_does_not_accept_caller_treatment_or_membership(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = OwnerFixedExpenseAuthority(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        with pytest.raises(TypeError):
            authority.record_expense(
                campaign=campaign,
                amount=Decimal("1"),
                currency="EUR",
                incurred_at=datetime.now(UTC) - timedelta(minutes=1),
                treatment=CostTreatment.EMBEDDED_IN_GROSS,
                memberships=(),
            )
    finally:
        fixture.doCleanups()


def test_correction_is_append_only_and_old_record_is_historical(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = OwnerFixedExpenseAuthority(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        incurred = datetime.now(UTC) - timedelta(minutes=2)
        first = authority.record_expense(
            campaign=campaign,
            amount=Decimal("5"),
            currency="EUR",
            incurred_at=incurred,
        )
        corrected = authority.record_expense(
            campaign=campaign,
            amount=Decimal("4"),
            currency="EUR",
            incurred_at=incurred,
            supersedes_expense_id=first.expense_id,
        )
        authority.resolve_cost_evidence(
            campaign=campaign,
            expense_id=first.expense_id,
            record_sha256=first.record_sha256,
            as_of=first.recorded_at,
        )
        with pytest.raises(CostEvidenceError, match="superseded"):
            authority.resolve_cost_evidence(
                campaign=campaign,
                expense_id=first.expense_id,
                record_sha256=first.record_sha256,
                as_of=corrected.recorded_at + timedelta(seconds=1),
            )
        cost = authority.resolve_cost_evidence(
            campaign=campaign,
            expense_id=corrected.expense_id,
            record_sha256=corrected.record_sha256,
            as_of=corrected.recorded_at + timedelta(seconds=1),
        )
        assert cost.amount == Decimal("4")
    finally:
        fixture.doCleanups()


def test_tampered_admin_state_fails_closed_after_restart(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        root = tmp_path / "workspace"
        authority = OwnerFixedExpenseAuthority(root, authority_root=tmp_path / "authority")
        authority.record_expense(
            campaign=campaign,
            amount=Decimal("2"),
            currency="EUR",
            incurred_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        path = root / "owner_fixed_expense" / "state.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["records"][0]["amount"] = "999"
        path.write_text(json.dumps(raw), encoding="utf-8")
        restarted = OwnerFixedExpenseAuthority(root, authority_root=tmp_path / "authority")
        with pytest.raises(OwnerFixedExpenseAuthorityError):
            restarted.verify()
    finally:
        fixture.doCleanups()


def test_wrong_campaign_cannot_reuse_owner_expense(tmp_path) -> None:
    fixture, campaign = _fixture_authority()
    try:
        authority = OwnerFixedExpenseAuthority(
            tmp_path / "workspace",
            authority_root=tmp_path / "authority",
        )
        record = authority.record_expense(
            campaign=campaign,
            amount=Decimal("1"),
            currency="EUR",
            incurred_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        with pytest.raises(CostEvidenceError):
            authority.resolve_cost_evidence(
                campaign=object(),
                expense_id=record.expense_id,
                record_sha256=record.record_sha256,
                as_of=record.recorded_at + timedelta(seconds=1),
            )
    finally:
        fixture.doCleanups()
