from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping

from .iso_currency import ISO4217CurrencyError, require_iso_4217_currency


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CampaignDenominationError(ValueError):
    """Raised when a campaign denomination authority is malformed or drifts."""


def _require_nonempty(name: str, value: object) -> str:
    if type(value) is not str or not value.strip():
        raise CampaignDenominationError(f"{name} must be a non-empty string")
    return value


def _require_sha256(name: str, value: object) -> str:
    value = _require_nonempty(name, value)
    if _SHA256_RE.fullmatch(value) is None:
        raise CampaignDenominationError(f"{name} must be a lowercase sha256 hex digest")
    return value


def _require_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise CampaignDenominationError("economic_goal_revision must be a non-negative integer")
    return value


def _require_timestamp(name: str, value: object) -> datetime:
    if type(value) is not datetime:
        raise CampaignDenominationError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CampaignDenominationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_timestamp(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    text = value.isoformat(timespec="microseconds")
    return text.replace("+00:00", "Z")


def _canonical_payload(binding: "CampaignDenominationBinding", *, include_binding_id: bool) -> dict[str, object]:
    payload: dict[str, object] = {}
    for field in fields(binding):
        if field.name == "binding_id" and not include_binding_id:
            continue
        value = getattr(binding, field.name)
        if isinstance(value, datetime):
            payload[field.name] = _canonical_timestamp(value)
        else:
            payload[field.name] = value
    return payload


def _digest_payload(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CampaignDenominationBinding:
    """Immutable denomination/owner binding for one finalized campaign projection.

    This type is deliberately authority-neutral at construction: positive product authority
    is established only by the canonical finalization/runtime owner that records and later
    re-resolves this exact value. The record itself makes retroactive owner/currency
    relabelling and silent digest drift mechanically detectable.
    """

    campaign_id: str
    campaign_version: str
    campaign_sha256: str
    session_id: str
    run_id: str
    evaluation_id: str
    projection_sha256: str
    economic_goal_id: str
    economic_goal_revision: int
    economic_goal_sha256: str
    bankroll_id: str
    portfolio_identity: str
    currency: str
    effective_at: datetime
    observed_at: datetime
    available_at: datetime
    source_evidence_sha256: str
    binding_id: str = ""

    def __post_init__(self) -> None:
        normalized: dict[str, object] = {
            "campaign_id": _require_nonempty("campaign_id", self.campaign_id),
            "campaign_version": _require_nonempty("campaign_version", self.campaign_version),
            "campaign_sha256": _require_sha256("campaign_sha256", self.campaign_sha256),
            "session_id": _require_nonempty("session_id", self.session_id),
            "run_id": _require_nonempty("run_id", self.run_id),
            "evaluation_id": _require_nonempty("evaluation_id", self.evaluation_id),
            "projection_sha256": _require_sha256("projection_sha256", self.projection_sha256),
            "economic_goal_id": _require_nonempty("economic_goal_id", self.economic_goal_id),
            "economic_goal_revision": _require_revision(self.economic_goal_revision),
            "economic_goal_sha256": _require_sha256("economic_goal_sha256", self.economic_goal_sha256),
            "bankroll_id": _require_nonempty("bankroll_id", self.bankroll_id),
            "portfolio_identity": _require_nonempty("portfolio_identity", self.portfolio_identity),
            "currency": _require_nonempty("currency", self.currency),
            "effective_at": _require_timestamp("effective_at", self.effective_at),
            "observed_at": _require_timestamp("observed_at", self.observed_at),
            "available_at": _require_timestamp("available_at", self.available_at),
            "source_evidence_sha256": _require_sha256("source_evidence_sha256", self.source_evidence_sha256),
        }
        try:
            normalized["currency"] = require_iso_4217_currency(normalized["currency"])
        except ISO4217CurrencyError as exc:
            raise CampaignDenominationError(str(exc)) from exc
        if not (
            normalized["effective_at"] <= normalized["observed_at"] <= normalized["available_at"]
        ):
            raise CampaignDenominationError("campaign denomination authority timestamps are not causal")

        for name, value in normalized.items():
            object.__setattr__(self, name, value)

        expected_id = _digest_payload(_canonical_payload(self, include_binding_id=False))
        if self.binding_id:
            supplied_id = _require_sha256("binding_id", self.binding_id)
            if supplied_id != expected_id:
                raise CampaignDenominationError("binding_id does not match immutable binding payload")
        object.__setattr__(self, "binding_id", expected_id)

    @property
    def canonical_payload(self) -> Mapping[str, object]:
        return MappingProxyType(_canonical_payload(self, include_binding_id=True))

    def verify_exact(
        self,
        *,
        campaign_id: str,
        campaign_version: str,
        campaign_sha256: str,
        session_id: str,
        run_id: str,
        evaluation_id: str,
        projection_sha256: str,
        economic_goal_id: str,
        economic_goal_revision: int,
        economic_goal_sha256: str,
        bankroll_id: str,
        portfolio_identity: str,
        currency: str,
    ) -> None:
        expected = {
            "campaign_id": campaign_id,
            "campaign_version": campaign_version,
            "campaign_sha256": campaign_sha256,
            "session_id": session_id,
            "run_id": run_id,
            "evaluation_id": evaluation_id,
            "projection_sha256": projection_sha256,
            "economic_goal_id": economic_goal_id,
            "economic_goal_revision": economic_goal_revision,
            "economic_goal_sha256": economic_goal_sha256,
            "bankroll_id": bankroll_id,
            "portfolio_identity": portfolio_identity,
            "currency": currency,
        }
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise CampaignDenominationError(f"campaign denomination authority mismatch: {name}")

    def verify_cost_currency(self, cost_currency: str) -> None:
        if type(cost_currency) is not str or cost_currency != self.currency:
            raise CampaignDenominationError(
                "cost currency does not match canonical campaign denomination; explicit FX authority required"
            )


def rehydrate_campaign_denomination_binding(payload: Mapping[str, object]) -> CampaignDenominationBinding:
    """Strictly rehydrate an immutable binding and re-check its digest."""

    if type(payload) not in (dict, MappingProxyType):
        raise CampaignDenominationError("binding payload must be a mapping")
    expected_keys = {field.name for field in fields(CampaignDenominationBinding)}
    if set(payload) != expected_keys:
        raise CampaignDenominationError("binding payload keys are not exact")

    def parse_timestamp(name: str) -> datetime:
        raw = payload[name]
        if type(raw) is not str or not raw.endswith("Z"):
            raise CampaignDenominationError(f"{name} must be a canonical UTC timestamp")
        try:
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise CampaignDenominationError(f"{name} is not a valid timestamp") from exc
        if _canonical_timestamp(parsed) != raw:
            raise CampaignDenominationError(f"{name} is not canonical")
        return parsed

    return CampaignDenominationBinding(
        campaign_id=payload["campaign_id"],
        campaign_version=payload["campaign_version"],
        campaign_sha256=payload["campaign_sha256"],
        session_id=payload["session_id"],
        run_id=payload["run_id"],
        evaluation_id=payload["evaluation_id"],
        projection_sha256=payload["projection_sha256"],
        economic_goal_id=payload["economic_goal_id"],
        economic_goal_revision=payload["economic_goal_revision"],
        economic_goal_sha256=payload["economic_goal_sha256"],
        bankroll_id=payload["bankroll_id"],
        portfolio_identity=payload["portfolio_identity"],
        currency=payload["currency"],
        effective_at=parse_timestamp("effective_at"),
        observed_at=parse_timestamp("observed_at"),
        available_at=parse_timestamp("available_at"),
        source_evidence_sha256=payload["source_evidence_sha256"],
        binding_id=payload["binding_id"],
    )
