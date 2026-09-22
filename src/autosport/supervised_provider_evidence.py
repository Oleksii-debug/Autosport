from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias
from weakref import ref

from .betfair_account_readonly import (
    ADAPTER_ID as BETFAIR_ADAPTER_ID,
    ADAPTER_VERSION as BETFAIR_ADAPTER_VERSION,
    BetfairClearedOrderObservation,
    BetfairClearedOrderPage,
    BetfairCurrentOrderObservation,
    BetfairCurrentOrderPage,
    BetfairExecutionReadbackEnvelope,
    BetfairReadOnlyError,
)
from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
)
from .real_execution_ledger import AcknowledgementStatus, ExecutionAction


class ProviderEvidenceError(RuntimeError):
    """Raised when read-only provider observations do not prove an execution fact."""


def _time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ProviderEvidenceError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderEvidenceError(f"{name} must be timezone-aware")
    return parsed


def _digest(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProviderEvidenceError("provider evidence is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def _current_order_payload(order: BetfairCurrentOrderObservation) -> dict[str, object]:
    return {
        "bet_id": order.bet_id,
        "market_id": order.market_id,
        "selection_id": order.selection_id,
        "side": order.side,
        "status": order.status,
        "placed_date": order.placed_date,
        "price": None if order.price is None else str(order.price),
        "requested_size": (
            None if order.requested_size is None else str(order.requested_size)
        ),
        "average_price_matched": str(order.average_price_matched),
        "size_matched": str(order.size_matched),
        "size_remaining": str(order.size_remaining),
        "customer_order_ref": order.customer_order_ref,
        "customer_strategy_ref": order.customer_strategy_ref,
        "evidence": {
            "observed_at": order.evidence.observed_at,
            "source_payload_sha256": order.evidence.source_payload_sha256,
        },
    }


def _cleared_order_payload(order: BetfairClearedOrderObservation) -> dict[str, object]:
    return {
        "bet_id": order.bet_id,
        "event_id": order.event_id,
        "market_id": order.market_id,
        "selection_id": order.selection_id,
        "side": order.side,
        "bet_status": order.bet_status,
        "placed_date": order.placed_date,
        "settled_date": order.settled_date,
        "price_requested": str(order.price_requested),
        "price_matched": str(order.price_matched),
        "size_settled": str(order.size_settled),
        "profit": str(order.profit),
        "customer_order_ref": order.customer_order_ref,
        "customer_strategy_ref": order.customer_strategy_ref,
        "evidence": {
            "observed_at": order.evidence.observed_at,
            "source_payload_sha256": order.evidence.source_payload_sha256,
        },
    }


def _complete_current_pages(
    pages: tuple[BetfairCurrentOrderPage, ...],
) -> tuple[tuple[BetfairCurrentOrderObservation, ...], str, str]:
    if not pages or any(not isinstance(page, BetfairCurrentOrderPage) for page in pages):
        raise ProviderEvidenceError("current-order evidence requires canonical pages")
    expected_offset = 0
    orders: list[BetfairCurrentOrderObservation] = []
    payload: list[dict[str, object]] = []
    latest_raw = pages[0].evidence.observed_at
    latest = _time(latest_raw, "current page observed_at")
    for index, page in enumerate(pages):
        if page.from_record != expected_offset:
            raise ProviderEvidenceError("current-order pages are not contiguous")
        if index < len(pages) - 1 and not page.more_available:
            raise ProviderEvidenceError("current-order pages continue after terminal page")
        if index == len(pages) - 1 and page.more_available:
            raise ProviderEvidenceError("current-order evidence is incomplete")
        if page.more_available and not page.orders:
            raise ProviderEvidenceError("current-order pagination cannot advance")
        expected_offset += len(page.orders)
        orders.extend(page.orders)
        page_time = _time(page.evidence.observed_at, "current page observed_at")
        if page_time > latest:
            latest = page_time
            latest_raw = page.evidence.observed_at
        payload.append(
            {
                "from_record": page.from_record,
                "record_count": page.record_count,
                "more_available": page.more_available,
                "evidence": {
                    "observed_at": page.evidence.observed_at,
                    "source_payload_sha256": page.evidence.source_payload_sha256,
                },
                "orders": [_current_order_payload(order) for order in page.orders],
            }
        )
    if len({order.bet_id for order in orders}) != len(orders):
        raise ProviderEvidenceError("current-order evidence has duplicate bet_id")
    return tuple(orders), _digest(payload), latest_raw


def _complete_cleared_pages(
    pages: tuple[BetfairClearedOrderPage, ...],
) -> tuple[tuple[BetfairClearedOrderObservation, ...], str, str]:
    if not pages or any(not isinstance(page, BetfairClearedOrderPage) for page in pages):
        raise ProviderEvidenceError("cleared-order evidence requires canonical pages")
    expected_offset = 0
    orders: list[BetfairClearedOrderObservation] = []
    payload: list[dict[str, object]] = []
    latest_raw = pages[0].evidence.observed_at
    latest = _time(latest_raw, "cleared page observed_at")
    for index, page in enumerate(pages):
        if page.from_record != expected_offset:
            raise ProviderEvidenceError("cleared-order pages are not contiguous")
        if index < len(pages) - 1 and not page.more_available:
            raise ProviderEvidenceError("cleared-order pages continue after terminal page")
        if index == len(pages) - 1 and page.more_available:
            raise ProviderEvidenceError("cleared-order evidence is incomplete")
        if page.more_available and not page.orders:
            raise ProviderEvidenceError("cleared-order pagination cannot advance")
        expected_offset += len(page.orders)
        orders.extend(page.orders)
        page_time = _time(page.evidence.observed_at, "cleared page observed_at")
        if page_time > latest:
            latest = page_time
            latest_raw = page.evidence.observed_at
        payload.append(
            {
                "from_record": page.from_record,
                "record_count": page.record_count,
                "more_available": page.more_available,
                "evidence": {
                    "observed_at": page.evidence.observed_at,
                    "source_payload_sha256": page.evidence.source_payload_sha256,
                },
                "orders": [_cleared_order_payload(order) for order in page.orders],
            }
        )
    if len({order.bet_id for order in orders}) != len(orders):
        raise ProviderEvidenceError("cleared-order evidence has duplicate bet_id")
    return tuple(orders), _digest(payload), latest_raw


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedProviderEffectEvidence:
    bookmaker_id: str
    account_id: str
    action_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    event_id: str
    market_id: str
    selection_id: str
    external_receipt_id: str
    observed_at: str
    source_payload_sha256: str
    status: AcknowledgementStatus
    accepted_odds: Decimal
    accepted_stake: Decimal
    evidence_id: str
    provider_order_ref: str | None = None


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedProviderAbsenceEvidence:
    bookmaker_id: str
    account_id: str
    action_id: str
    adapter_id: str
    adapter_version: str
    profile_version: int
    event_id: str
    market_id: str
    selection_id: str
    observed_at: str
    current_source_payload_sha256: str
    cleared_source_payload_sha256: str
    evidence_id: str
    provider_order_ref: str | None = None


VerifiedProviderState: TypeAlias = (
    VerifiedProviderEffectEvidence | VerifiedProviderAbsenceEvidence
)


def _verified_provider_evidence_fingerprint(evidence: VerifiedProviderState) -> str:
    if isinstance(evidence, VerifiedProviderEffectEvidence):
        payload: dict[str, object] = {
            "kind": "effect",
            "bookmaker_id": evidence.bookmaker_id,
            "account_id": evidence.account_id,
            "action_id": evidence.action_id,
            "adapter_id": evidence.adapter_id,
            "adapter_version": evidence.adapter_version,
            "profile_version": evidence.profile_version,
            "event_id": evidence.event_id,
            "market_id": evidence.market_id,
            "selection_id": evidence.selection_id,
            "external_receipt_id": evidence.external_receipt_id,
            "observed_at": evidence.observed_at,
            "source_payload_sha256": evidence.source_payload_sha256,
            "status": evidence.status.value,
            "accepted_odds": str(evidence.accepted_odds),
            "accepted_stake": str(evidence.accepted_stake),
            "evidence_id": evidence.evidence_id,
            "provider_order_ref": evidence.provider_order_ref,
        }
    elif isinstance(evidence, VerifiedProviderAbsenceEvidence):
        payload = {
            "kind": "absence",
            "bookmaker_id": evidence.bookmaker_id,
            "account_id": evidence.account_id,
            "action_id": evidence.action_id,
            "adapter_id": evidence.adapter_id,
            "adapter_version": evidence.adapter_version,
            "profile_version": evidence.profile_version,
            "event_id": evidence.event_id,
            "market_id": evidence.market_id,
            "selection_id": evidence.selection_id,
            "observed_at": evidence.observed_at,
            "current_source_payload_sha256": evidence.current_source_payload_sha256,
            "cleared_source_payload_sha256": evidence.cleared_source_payload_sha256,
            "evidence_id": evidence.evidence_id,
            "provider_order_ref": evidence.provider_order_ref,
        }
    else:
        raise ProviderEvidenceError("provider evidence type is not canonical")
    return _digest(payload)


def _require_bound_profile(
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    bookmaker_id: str,
    account_id: str,
    observed_at: str,
) -> None:
    if type(profile) is not BookmakerCapabilityProfile:
        raise ProviderEvidenceError("provider evidence requires exact canonical capability profile")
    if (
        profile.venue_id != bookmaker_id
        or profile.account_id != account_id
        or profile.adapter_id != BETFAIR_ADAPTER_ID
        or profile.adapter_version != BETFAIR_ADAPTER_VERSION
        or profile.profile_id != expected_profile_sha256
    ):
        raise ProviderEvidenceError("provider capability profile is not exact bound profile")
    try:
        profile.require(BookmakerCapability.BET_READBACK)
    except Exception as exc:
        raise ProviderEvidenceError("BET_READBACK capability is not supported") from exc
    if _time(profile.observed_at, "profile observed_at") > _time(
        observed_at, "provider observed_at"
    ):
        raise ProviderEvidenceError("provider evidence predates capability profile")


_REQUIRED_CLEARED_STATUSES = ("SETTLED", "VOIDED", "LAPSED", "CANCELLED")


def verify_betfair_provider_state(
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    readback: BetfairExecutionReadbackEnvelope,
    expected_provider_order_ref: str | None = None,
) -> VerifiedProviderState:
    """Derive execution truth only from a client-sealed, action-scoped Betfair capture."""

    if type(action) is not ExecutionAction:
        raise ProviderEvidenceError("action must be exact canonical ExecutionAction")
    if type(readback) is not BetfairExecutionReadbackEnvelope:
        raise ProviderEvidenceError(
            "provider evidence requires exact canonical action-scoped readback envelope"
        )
    try:
        readback.assert_authoritative()
    except BetfairReadOnlyError as exc:
        raise ProviderEvidenceError(
            "provider evidence requires authoritative canonical readback capture"
        ) from exc
    if (
        readback.venue_id != action.bookmaker_id
        or readback.account_id != action.account_id
        or readback.adapter_id != BETFAIR_ADAPTER_ID
        or readback.adapter_version != BETFAIR_ADAPTER_VERSION
        or readback.action_id != action.action_id
        or readback.market_id != action.market_id
    ):
        raise ProviderEvidenceError("provider readback scope conflicts with execution action")
    if (
        readback.market_event.market_id != action.market_id
        or readback.market_event.event_id != action.event_id
    ):
        raise ProviderEvidenceError(
            "provider market-to-event identity conflicts with execution action"
        )
    if expected_provider_order_ref is not None:
        if (
            type(expected_provider_order_ref) is not str
            or not expected_provider_order_ref
            or len(expected_provider_order_ref) > 32
            or any(
                character not in "0123456789abcdef"
                for character in expected_provider_order_ref
            )
        ):
            raise ProviderEvidenceError(
                "expected_provider_order_ref must be <=32 lowercase hex characters"
            )
        if readback.provider_order_ref != expected_provider_order_ref:
            raise ProviderEvidenceError(
                "provider readback order reference conflicts with expected durable binding"
            )

    current, current_sha, current_at = _complete_current_pages(readback.current_pages)
    statuses = tuple(status for status, _ in readback.cleared_pages_by_status)
    if statuses != _REQUIRED_CLEARED_STATUSES:
        raise ProviderEvidenceError("cleared-order status coverage is incomplete")

    cleared: list[tuple[str, BetfairClearedOrderObservation]] = []
    cleared_parts: list[dict[str, object]] = []
    observed_times = [current_at, readback.market_event.evidence.observed_at]
    for status, pages in readback.cleared_pages_by_status:
        orders, pages_sha, pages_at = _complete_cleared_pages(pages)
        observed_times.append(pages_at)
        for order in orders:
            if order.bet_status != status:
                raise ProviderEvidenceError(
                    "cleared-order status does not match captured request scope"
                )
            if order.market_id != action.market_id:
                raise ProviderEvidenceError(
                    "cleared-order market conflicts with captured execution scope"
                )
            if order.event_id is None or order.event_id != action.event_id:
                raise ProviderEvidenceError(
                    "cleared-order event identity conflicts with execution action"
                )
            cleared.append((status, order))
        cleared_parts.append({"status": status, "pages_sha256": pages_sha})

    latest_raw = max(
        observed_times,
        key=lambda value: _time(value, "provider observed_at"),
    )
    if latest_raw != readback.observed_at:
        raise ProviderEvidenceError("readback observed_at does not match captured pages")
    observed_at = readback.observed_at
    cleared_sha = _digest(cleared_parts)

    _require_bound_profile(
        profile,
        expected_profile_sha256=expected_profile_sha256,
        bookmaker_id=action.bookmaker_id,
        account_id=action.account_id,
        observed_at=observed_at,
    )
    try:
        provider_selection_id = int(action.selection_id)
    except (TypeError, ValueError) as exc:
        raise ProviderEvidenceError(
            "Betfair readback requires provider-native numeric selection_id"
        ) from exc
    if str(provider_selection_id) != action.selection_id or provider_selection_id <= 0:
        raise ProviderEvidenceError(
            "Betfair readback requires canonical positive numeric selection_id"
        )

    provider_order_ref = readback.provider_order_ref or action.action_id
    candidates: list[
        tuple[
            str,
            str | None,
            BetfairCurrentOrderObservation | BetfairClearedOrderObservation,
        ]
    ] = []
    for order in current:
        if order.customer_order_ref != provider_order_ref:
            raise ProviderEvidenceError(
                "current-order customerOrderRef conflicts with captured execution scope"
            )
        candidates.append(("current", None, order))
    for status, order in cleared:
        if order.customer_order_ref != provider_order_ref:
            raise ProviderEvidenceError(
                "cleared-order customerOrderRef conflicts with captured execution scope"
            )
        candidates.append(("cleared", status, order))

    for kind, _, order in candidates:
        if (
            order.market_id != action.market_id
            or order.selection_id != provider_selection_id
            or order.side != action.side
        ):
            raise ProviderEvidenceError(
                "provider order identity conflicts with execution action"
            )
        if kind == "cleared":
            assert isinstance(order, BetfairClearedOrderObservation)
            if order.event_id != action.event_id:
                raise ProviderEvidenceError(
                    "provider cleared order event conflicts with execution action"
                )
    receipt_ids = {order.bet_id for _, _, order in candidates}
    if len(receipt_ids) > 1:
        raise ProviderEvidenceError(
            "multiple provider receipts claim one execution action"
        )

    if not candidates:
        evidence_id = _digest(
            {
                "schema": "autosport.betfair_execution_absence",
                "schema_version": 2,
                "bookmaker_id": action.bookmaker_id,
                "account_id": action.account_id,
                "action_id": action.action_id,
                "provider_order_ref": provider_order_ref,
                "event_id": readback.market_event.event_id,
                "market_id": action.market_id,
                "selection_id": action.selection_id,
                "request_scope_sha256": readback.request_scope_sha256,
                "capture_evidence_sha256": readback.evidence_sha256,
                "current_pages_sha256": current_sha,
                "cleared_pages_sha256": cleared_sha,
                "observed_at": observed_at,
            }
        )
        return VerifiedProviderAbsenceEvidence(
            action.bookmaker_id,
            action.account_id,
            action.action_id,
            profile.adapter_id,
            profile.adapter_version,
            profile.profile_version,
            readback.market_event.event_id,
            action.market_id,
            action.selection_id,
            observed_at,
            current_sha,
            cleared_sha,
            evidence_id,
            readback.provider_order_ref,
        )

    # A transition can expose the same receipt in current and cleared evidence.
    # Cleared evidence is stronger, but non-SETTLED cleared states do not prove
    # accepted execution economics and therefore remain fail-closed.
    kind, cleared_status, order = sorted(
        candidates,
        key=lambda item: item[0] != "cleared",
    )[0]
    if kind == "cleared":
        assert isinstance(order, BetfairClearedOrderObservation)
        if cleared_status != "SETTLED":
            raise ProviderEvidenceError(
                "provider order exists in non-settled cleared state; execution economics unresolved"
            )
        accepted_stake = order.size_settled
        accepted_odds = order.price_matched
        order_at = order.evidence.observed_at
    else:
        assert isinstance(order, BetfairCurrentOrderObservation)
        accepted_stake = order.size_matched
        accepted_odds = order.average_price_matched
        order_at = order.evidence.observed_at

    if accepted_stake <= 0 or accepted_odds <= 0:
        raise ProviderEvidenceError(
            "provider order exists but matched execution economics remain unresolved"
        )
    if accepted_stake > action.requested_stake:
        raise ProviderEvidenceError("provider matched stake exceeds requested stake")
    status = (
        AcknowledgementStatus.ACCEPTED
        if accepted_stake == action.requested_stake
        else AcknowledgementStatus.PARTIAL
    )
    source_sha = _digest(
        {
            "capture_evidence_sha256": readback.evidence_sha256,
            "request_scope_sha256": readback.request_scope_sha256,
            "current_pages_sha256": current_sha,
            "cleared_pages_sha256": cleared_sha,
            "receipt_id": order.bet_id,
            "order_observed_at": order_at,
        }
    )
    evidence_id = _digest(
        {
            "schema": "autosport.betfair_execution_effect",
            "schema_version": 2,
            "bookmaker_id": action.bookmaker_id,
            "account_id": action.account_id,
            "action_id": action.action_id,
            "provider_order_ref": provider_order_ref,
            "event_id": readback.market_event.event_id,
            "market_id": action.market_id,
            "selection_id": action.selection_id,
            "external_receipt_id": order.bet_id,
            "observed_at": observed_at,
            "source_payload_sha256": source_sha,
            "status": status.value,
            "accepted_odds": str(accepted_odds),
            "accepted_stake": str(accepted_stake),
        }
    )
    return VerifiedProviderEffectEvidence(
        action.bookmaker_id,
        action.account_id,
        action.action_id,
        profile.adapter_id,
        profile.adapter_version,
        profile.profile_version,
        readback.market_event.event_id,
        action.market_id,
        action.selection_id,
        order.bet_id,
        observed_at,
        source_sha,
        status,
        accepted_odds,
        accepted_stake,
        evidence_id,
        readback.provider_order_ref,
    )


# Verified provider state is an in-process capability, not a caller assertion.
# The canonical verifier and its assertion boundary seal their executable dependency
# graph at module initialization. Runtime rebinding of helpers/types/constants must
# never strengthen evidence that can release an UNKNOWN execution state.
def _install_verified_provider_evidence_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    raw_verify = verify_betfair_provider_state
    raw_verify_code = raw_verify.__code__
    sealed_effect_type = VerifiedProviderEffectEvidence
    sealed_absence_type = VerifiedProviderAbsenceEvidence
    sealed_fingerprint = _verified_provider_evidence_fingerprint
    sealed_error = ProviderEvidenceError

    def descriptor_code(value: object) -> object | None:
        code = getattr(value, "__code__", None)
        if code is not None:
            return code
        if isinstance(value, property) and value.fget is not None:
            return getattr(value.fget, "__code__", None)
        return None

    sealed_readback_assertion = BetfairExecutionReadbackEnvelope.assert_authoritative
    sealed_readback_assertion_code = descriptor_code(sealed_readback_assertion)
    sealed_readback_fingerprint = BetfairExecutionReadbackEnvelope._authority_fingerprint
    sealed_readback_fingerprint_code = descriptor_code(sealed_readback_fingerprint)
    sealed_profile_descriptors = {
        "profile_id": BookmakerCapabilityProfile.profile_id,
        "to_canonical_dict": BookmakerCapabilityProfile.to_canonical_dict,
        "state_of": BookmakerCapabilityProfile.state_of,
        "require": BookmakerCapabilityProfile.require,
    }
    sealed_profile_descriptor_codes = {
        name: descriptor_code(value)
        for name, value in sealed_profile_descriptors.items()
    }
    timeout_absence_assertion: object | None = None
    timeout_absence_assertion_code: object | None = None
    missing = object()

    def seal_function_graph(root: object) -> dict[str, tuple[object, object | None]]:
        module_globals = globals()
        sealed: dict[str, tuple[object, object | None]] = {}
        pending = [root]
        visited: set[int] = set()
        while pending:
            function = pending.pop()
            if id(function) in visited:
                continue
            visited.add(id(function))
            code = getattr(function, "__code__", None)
            function_globals = getattr(function, "__globals__", None)
            if code is None or function_globals is not module_globals:
                continue
            for name in code.co_names:
                if name not in module_globals or name in sealed:
                    continue
                value = module_globals[name]
                value_code = getattr(value, "__code__", None)
                sealed[name] = (value, value_code)
                if (
                    value_code is not None
                    and getattr(value, "__globals__", None) is module_globals
                ):
                    pending.append(value)
        return sealed

    sealed_verify_graph = seal_function_graph(raw_verify)
    sealed_wrapper_bindings = {
        "BetfairExecutionReadbackEnvelope": BetfairExecutionReadbackEnvelope,
        "VerifiedProviderEffectEvidence": sealed_effect_type,
        "VerifiedProviderAbsenceEvidence": sealed_absence_type,
        "_verified_provider_evidence_fingerprint": sealed_fingerprint,
        "ProviderEvidenceError": sealed_error,
    }

    def assert_executable_authority_intact() -> None:
        module_globals = globals()
        if raw_verify.__code__ is not raw_verify_code:
            raise sealed_error("provider verifier executable code changed")
        for name, (expected, expected_code) in sealed_verify_graph.items():
            current = module_globals.get(name, missing)
            if current is not expected:
                raise sealed_error(
                    f"provider evidence executable authority changed: {name}"
                )
            if (
                expected_code is not None
                and getattr(current, "__code__", None) is not expected_code
            ):
                raise sealed_error(
                    f"provider evidence executable code changed: {name}"
                )
        for name, expected in sealed_wrapper_bindings.items():
            if module_globals.get(name, missing) is not expected:
                raise sealed_error(
                    f"provider evidence authority binding changed: {name}"
                )
        current_readback_assertion = getattr(
            sealed_wrapper_bindings["BetfairExecutionReadbackEnvelope"],
            "assert_authoritative",
            missing,
        )
        if (
            current_readback_assertion is not sealed_readback_assertion
            or descriptor_code(current_readback_assertion)
            is not sealed_readback_assertion_code
        ):
            raise sealed_error(
                "provider readback origin authority method changed"
            )
        current_readback_fingerprint = getattr(
            BetfairExecutionReadbackEnvelope,
            "_authority_fingerprint",
            missing,
        )
        if (
            current_readback_fingerprint is not sealed_readback_fingerprint
            or descriptor_code(current_readback_fingerprint)
            is not sealed_readback_fingerprint_code
        ):
            raise sealed_error(
                "provider readback authority fingerprint method changed"
            )
        for name, expected in sealed_profile_descriptors.items():
            current = getattr(BookmakerCapabilityProfile, name, missing)
            if (
                current is not expected
                or descriptor_code(current)
                is not sealed_profile_descriptor_codes[name]
            ):
                raise sealed_error(
                    f"provider capability profile authority method changed: {name}"
                )

    def register_timeout_absence_authority(assertion: object) -> None:
        nonlocal timeout_absence_assertion, timeout_absence_assertion_code
        if not callable(assertion):
            raise sealed_error("timeout absence authority assertion must be callable")

        # This registrar is exposed only to break the provider-evidence/timeout
        # import cycle; exposure must not become caller-mintable authority.  A
        # foreign callback registered before the timeout module is imported could
        # otherwise become the permanently captured absence assertion and turn
        # generic complete-empty readback into retry-authoritative absence.
        timeout_module_name = f"{__package__}.betfair_timeout_reconciliation"
        timeout_module = sys.modules.get(timeout_module_name)
        assertion_globals = getattr(assertion, "__globals__", None)
        assertion_qualname = getattr(assertion, "__qualname__", None)
        if (
            getattr(assertion, "__module__", None) != timeout_module_name
            or timeout_module is None
            or assertion_globals is not vars(timeout_module)
            or assertion_qualname
            != (
                "_install_betfair_timeout_absence_authority.<locals>."
                "assert_betfair_timeout_absence_authoritative"
            )
        ):
            raise sealed_error(
                "timeout absence authority assertion origin is not canonical"
            )

        assertion_code = getattr(assertion, "__code__", None)
        if assertion_code is None:
            raise sealed_error(
                "timeout absence authority assertion executable code is unavailable"
            )
        if timeout_absence_assertion is None:
            timeout_absence_assertion = assertion
            timeout_absence_assertion_code = assertion_code
            return
        if timeout_absence_assertion is not assertion:
            raise sealed_error("timeout absence authority assertion is already registered")
        if timeout_absence_assertion_code is not assertion_code:
            raise sealed_error(
                "timeout absence authority assertion executable code changed"
            )

    def authoritative_verify(
        action: ExecutionAction,
        profile: BookmakerCapabilityProfile,
        *,
        expected_profile_sha256: str,
        readback: BetfairExecutionReadbackEnvelope,
        expected_provider_order_ref: str | None = None,
    ) -> VerifiedProviderState:
        assert_executable_authority_intact()
        evidence = raw_verify(
            action,
            profile,
            expected_profile_sha256=expected_profile_sha256,
            readback=readback,
            expected_provider_order_ref=expected_provider_order_ref,
        )
        assert_executable_authority_intact()
        evidence_key = id(evidence)

        def forget(_weakref: object, *, key: int = evidence_key) -> None:
            issued.pop(key, None)

        issued[evidence_key] = (
            ref(evidence, forget),
            sealed_fingerprint(evidence),
        )
        return evidence

    def assert_verified_provider_evidence_authoritative(
        evidence: VerifiedProviderState,
    ) -> None:
        nonlocal timeout_absence_assertion
        assert_executable_authority_intact()
        if not isinstance(evidence, (sealed_effect_type, sealed_absence_type)):
            raise sealed_error("provider evidence type is not canonical")
        record = issued.get(id(evidence))
        if record is None or record[0]() is not evidence:
            raise sealed_error(
                "verified provider evidence was not issued by canonical verifier"
            )
        if record[1] != sealed_fingerprint(evidence):
            raise sealed_error(
                "verified provider evidence changed after canonical verification"
            )
        if isinstance(evidence, sealed_absence_type):
            # Import only for registration side effect. Consumption uses the exact
            # closure-captured assertion registered by the timeout authority, never a
            # later mutable module attribute.
            if timeout_absence_assertion is None:
                try:
                    from . import betfair_timeout_reconciliation as _timeout_authority
                    del _timeout_authority
                except ImportError as exc:
                    raise sealed_error(
                        "verified provider absence lacks durable timeout-horizon authority"
                    ) from exc
            assertion = timeout_absence_assertion
            assertion_code = timeout_absence_assertion_code
            if assertion is None or assertion_code is None:
                raise sealed_error(
                    "verified provider absence lacks durable timeout-horizon authority"
                )
            if getattr(assertion, "__code__", None) is not assertion_code:
                raise sealed_error(
                    "timeout absence authority assertion executable code changed"
                )
            try:
                assertion(evidence)
            except Exception as exc:
                # Preserve one stable provider-evidence boundary for downstream
                # reconciliation without trusting a mutable timeout exception symbol.
                raise sealed_error(
                    "verified provider absence lacks durable timeout-horizon authority"
                ) from exc

    globals()["verify_betfair_provider_state"] = authoritative_verify
    globals()[
        "assert_verified_provider_evidence_authoritative"
    ] = assert_verified_provider_evidence_authoritative
    globals()[
        "_register_betfair_timeout_absence_authority_assertion"
    ] = register_timeout_absence_authority


_install_verified_provider_evidence_authority()
del _install_verified_provider_evidence_authority
