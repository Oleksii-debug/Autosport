"""Owner-facing, creation-only workflow for the economic-goal authority.

The Windows presentation may use this module, but it has no right to bypass the
typed contract or its durable store.  In particular, a persisted contract is
always read-only here: changing or replacing it needs a future explicit owner
authority boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Literal, Mapping

from .economic_goal import AutomationLevel, EconomicGoalContract, EconomicGoalContractError
from .economic_goal_store import EconomicGoalStore
from .localization import text
from .workspace_lock import WorkspaceEconomicLockError


OwnerEconomicAuthorityState = Literal["absent", "valid", "corrupt"]


class OwnerEconomicAuthorityError(ValueError):
    """Raised when an owner workflow request is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class OwnerEconomicAuthorityView:
    """Read-only projection for the Windows owner-authority surface."""

    state: OwnerEconomicAuthorityState
    summary_uk: str
    lines_uk: tuple[str, ...]
    contract: EconomicGoalContract | None = None

    @property
    def can_initialize(self) -> bool:
        return self.state == "absent"


INITIAL_OWNER_FORM_DEFAULTS: Final[Mapping[str, str]] = {
    "goal_id": "owner-primary",
    "bankroll_id": "paper-bankroll",
    "currency": "USD",
    "max_stake_fraction": "0.02",
    "max_stake_amount": "",
    "max_session_loss_fraction": "0.05",
    "max_day_loss_fraction": "0.05",
    "max_drawdown_fraction": "0.20",
    "max_capital_at_risk_fraction": "0.20",
    "max_risk_of_ruin": "0.01",
    "max_quote_age_seconds": "5",
    "minimum_data_quality": "0",
    "max_concurrent_positions": "1",
    "max_parlay_legs": "1",
    "automation_level": str(int(AutomationLevel.ANALYSIS_ONLY)),
    "blocked_sports": "",
    "blocked_providers": "",
    "blocked_markets": "",
}

OWNER_ECONOMIC_FORM_FIELDS: Final[tuple[str, ...]] = tuple(INITIAL_OWNER_FORM_DEFAULTS)


def _decimal_from_form(name: str, value: object, *, optional: bool = False) -> Decimal | None:
    if not isinstance(value, str):
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.text", field=_field_label(name)))
    candidate = value.strip()
    if optional and not candidate:
        return None
    if not candidate:
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.required", field=_field_label(name)))
    try:
        parsed = Decimal(candidate)
    except InvalidOperation as exc:
        raise OwnerEconomicAuthorityError(
            text("ui.windows.owner_authority.error.decimal", field=_field_label(name))
        ) from exc
    if not parsed.is_finite() or str(parsed) != candidate:
        raise OwnerEconomicAuthorityError(
            text("ui.windows.owner_authority.error.decimal", field=_field_label(name))
        )
    return parsed


def _positive_int_from_form(name: str, value: object) -> int:
    if not isinstance(value, str) or not value.strip():
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.required", field=_field_label(name)))
    candidate = value.strip()
    if not candidate.isascii() or not candidate.isdecimal() or str(int(candidate)) != candidate:
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.integer", field=_field_label(name)))
    result = int(candidate)
    if result <= 0:
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.integer", field=_field_label(name)))
    return result


def _canonical_text_from_form(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.required", field=_field_label(name)))
    return value


def _restrictions_from_form(name: str, value: object) -> frozenset[str]:
    if not isinstance(value, str):
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.text", field=_field_label(name)))
    members = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(members) != len(set(members)):
        raise OwnerEconomicAuthorityError(
            text("ui.windows.owner_authority.error.duplicates", field=_field_label(name))
        )
    if any("\x00" in item for item in members):
        raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.text", field=_field_label(name)))
    return frozenset(members)


def build_initial_owner_contract(
    values: Mapping[str, object], *, emergency_stop: bool
) -> EconomicGoalContract:
    """Parse an explicit, finite, initial owner contract from form values.

    All omitted contract properties retain the conservative typed contract
    defaults.  This parser cannot create a revision after the initial one.
    """

    missing = sorted(set(OWNER_ECONOMIC_FORM_FIELDS) - set(values))
    if missing:
        raise OwnerEconomicAuthorityError(
            text(
                "ui.windows.owner_authority.error.missing",
                fields=", ".join(_field_label(name) for name in missing),
            )
        )
    try:
        automation_level = AutomationLevel(_positive_or_zero_int("automation_level", values["automation_level"]))
    except (ValueError, EconomicGoalContractError) as exc:
        raise OwnerEconomicAuthorityError(
            text("ui.windows.owner_authority.error.automation")
        ) from exc
    try:
        return EconomicGoalContract(
            goal_id=_canonical_text_from_form("goal_id", values["goal_id"]),
            revision=1,
            bankroll_id=_canonical_text_from_form("bankroll_id", values["bankroll_id"]),
            currency=_canonical_text_from_form("currency", values["currency"]),
            max_stake_fraction=_decimal_from_form("max_stake_fraction", values["max_stake_fraction"]),  # type: ignore[arg-type]
            max_stake_amount=_decimal_from_form("max_stake_amount", values["max_stake_amount"], optional=True),
            max_session_loss_fraction=_decimal_from_form("max_session_loss_fraction", values["max_session_loss_fraction"]),  # type: ignore[arg-type]
            max_day_loss_fraction=_decimal_from_form("max_day_loss_fraction", values["max_day_loss_fraction"]),  # type: ignore[arg-type]
            max_drawdown_fraction=_decimal_from_form("max_drawdown_fraction", values["max_drawdown_fraction"]),  # type: ignore[arg-type]
            max_capital_at_risk_fraction=_decimal_from_form("max_capital_at_risk_fraction", values["max_capital_at_risk_fraction"]),  # type: ignore[arg-type]
            max_risk_of_ruin=_decimal_from_form("max_risk_of_ruin", values["max_risk_of_ruin"]),  # type: ignore[arg-type]
            max_quote_age_seconds=_decimal_from_form("max_quote_age_seconds", values["max_quote_age_seconds"]),  # type: ignore[arg-type]
            minimum_data_quality=_decimal_from_form("minimum_data_quality", values["minimum_data_quality"]),  # type: ignore[arg-type]
            max_concurrent_positions=_positive_int_from_form("max_concurrent_positions", values["max_concurrent_positions"]),
            max_parlay_legs=_positive_int_from_form("max_parlay_legs", values["max_parlay_legs"]),
            automation_level=automation_level,
            emergency_stop=emergency_stop,
            blocked_sports=_restrictions_from_form("blocked_sports", values["blocked_sports"]),
            blocked_providers=_restrictions_from_form("blocked_providers", values["blocked_providers"]),
            blocked_markets=_restrictions_from_form("blocked_markets", values["blocked_markets"]),
        )
    except EconomicGoalContractError as exc:
        raise OwnerEconomicAuthorityError(
            text("ui.windows.owner_authority.error.contract")
        ) from exc


def _positive_or_zero_int(name: str, value: object) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is missing")
    candidate = value.strip()
    if not candidate.isascii() or not candidate.isdecimal() or str(int(candidate)) != candidate:
        raise ValueError(f"{name} is invalid")
    return int(candidate)


def _field_label(name: str) -> str:
    return text(f"ui.windows.owner_authority.field.{name}")


def contract_readback_lines(contract: EconomicGoalContract) -> tuple[str, ...]:
    """Render all owner-relevant contract limits as selectable Ukrainian text."""

    amount = "—" if contract.max_stake_amount is None else str(contract.max_stake_amount)
    restrictions = (
        ("blocked_sports", contract.blocked_sports),
        ("blocked_providers", contract.blocked_providers),
        ("blocked_markets", contract.blocked_markets),
    )
    lines = (
        text("ui.windows.owner_authority.readback.identity", goal_id=contract.goal_id, revision=contract.revision),
        text("ui.windows.owner_authority.readback.bankroll", bankroll_id=contract.bankroll_id, currency=contract.currency),
        text("ui.windows.owner_authority.readback.objective"),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_stake_fraction"), value=contract.max_stake_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_stake_amount"), value=amount),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_session_loss_fraction"), value=contract.max_session_loss_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_day_loss_fraction"), value=contract.max_day_loss_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_drawdown_fraction"), value=contract.max_drawdown_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_capital_at_risk_fraction"), value=contract.max_capital_at_risk_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_event_concentration_fraction"), value=contract.max_event_concentration_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_market_concentration_fraction"), value=contract.max_market_concentration_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_provider_concentration_fraction"), value=contract.max_provider_concentration_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_sport_concentration_fraction"), value=contract.max_sport_concentration_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_turnover_fraction"), value=contract.max_turnover_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_risk_of_ruin"), value=contract.max_risk_of_ruin),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_execution_slippage_fraction"), value=contract.max_execution_slippage_fraction),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_quote_age_seconds"), value=contract.max_quote_age_seconds),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.minimum_data_quality"), value=contract.minimum_data_quality),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_concurrent_positions"), value=contract.max_concurrent_positions),
        text("ui.windows.owner_authority.readback.limit", name=text("ui.windows.owner_authority.field.max_parlay_legs"), value=contract.max_parlay_legs),
        text("ui.windows.owner_authority.readback.automation", level=int(contract.automation_level), emergency_stop=text("ui.boolean.true") if contract.emergency_stop else text("ui.boolean.false")),
    )
    return lines + tuple(
        text(
            "ui.windows.owner_authority.readback.restrictions",
            name=text(f"ui.windows.owner_authority.field.{name}"),
            values=", ".join(sorted(values)) if values else text("ui.windows.owner_authority.none"),
        )
        for name, values in restrictions
    )


class OwnerEconomicAuthorityService:
    """Read and initialize the one allowed owner-contract lifecycle step."""

    def __init__(self, workspace: str | Path) -> None:
        self.store = EconomicGoalStore(workspace)

    def read_view(self) -> OwnerEconomicAuthorityView:
        try:
            exists = self.store.path.exists()
        except OSError:
            exists = True
        if not exists:
            return OwnerEconomicAuthorityView(
                state="absent",
                summary_uk=text("ui.windows.owner_authority.state.absent"),
                lines_uk=(
                    text("ui.windows.owner_authority.state.absent"),
                    text("ui.windows.owner_authority.boundary.initial_only"),
                ),
            )
        try:
            contract = self.store.load()
        except (EconomicGoalContractError, OSError, UnicodeError, ValueError):
            return OwnerEconomicAuthorityView(
                state="corrupt",
                summary_uk=text("ui.windows.owner_authority.state.corrupt"),
                lines_uk=(
                    text("ui.windows.owner_authority.state.corrupt"),
                    text("ui.windows.owner_authority.boundary.corrupt"),
                ),
            )
        return OwnerEconomicAuthorityView(
            state="valid",
            summary_uk=text("ui.windows.owner_authority.state.valid"),
            lines_uk=(text("ui.windows.owner_authority.state.valid"),)
            + contract_readback_lines(contract)
            + (text("ui.windows.owner_authority.boundary.read_only"),),
            contract=contract,
        )

    def initialize_from_form(
        self, values: Mapping[str, object], *, emergency_stop: bool, confirmed: bool
    ) -> OwnerEconomicAuthorityView:
        """Create once only after caller has shown a review and received consent."""

        current = self.read_view()
        if current.state != "absent":
            raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.not_absent"))
        if not confirmed:
            raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.confirmation"))
        contract = build_initial_owner_contract(values, emergency_stop=emergency_stop)
        try:
            self.store.initialize_owner(contract)
        except (EconomicGoalContractError, WorkspaceEconomicLockError, OSError) as exc:
            raise OwnerEconomicAuthorityError(
                text("ui.windows.owner_authority.error.persist")
            ) from exc
        persisted = self.read_view()
        if persisted.state != "valid" or persisted.contract != contract:
            raise OwnerEconomicAuthorityError(text("ui.windows.owner_authority.error.readback"))
        return persisted
