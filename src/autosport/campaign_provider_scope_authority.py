from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from weakref import ref

from .betfair_account_readonly import (
    ADAPTER_ID as BETFAIR_ADAPTER_ID,
    ADAPTER_VERSION as BETFAIR_ADAPTER_VERSION,
    BetfairReadOnlyClient,
)
from .bookmaker_capability import BookmakerCapabilityProfile
from .campaign_economic_authority import FinalizedCampaignAuthority
from .decision_ledger import ECONOMIC_DECISION_KIND, JsonlDecisionLedger
from .real_execution_ledger import ExecutionAction, ExecutionPlan, RealExecutionLedger
from .opportunity import Opportunity, OpportunityContractError
from .portfolio_plan import (
    PortfolioPlan,
    _PORTFOLIO_INTENT_EVIDENCE_JSON_PAYLOAD_KEY,
    _PORTFOLIO_PLAN_DECISION_ACTION,
    _PORTFOLIO_PLAN_DECISION_AGENT,
    _PORTFOLIO_PLAN_JSON_PAYLOAD_KEY,
    _PORTFOLIO_PLAN_SHA256_PAYLOAD_KEY,
)


SCHEMA_VERSION = 1
_SOURCE_FAMILY = "betfair.authenticated-execution-scope.v1"
_ACCOUNT_PREFIX = "betfair-account-evidence:"


class CampaignProviderScopeError(RuntimeError):
    """Raised when exact campaign-to-provider applicability cannot be proven."""


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CampaignProviderScopeError(
            "provider scope is not canonical JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignProviderScopeError(
            f"{name} must be canonical non-empty text"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CampaignProviderScopeError(f"{name} must be UTF-8") from exc
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise CampaignProviderScopeError(
            f"{name} must be canonical SHA-256"
        )
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignProviderScopeError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignProviderScopeError(
            f"{name} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _canonical_instant(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, weakref_slot=True)
class VerifiedBetfairProviderScopeCapture:
    """Source-issued account + execution readback evidence from one client context."""

    venue_id: str
    client_account_scope: str
    authenticated_account_id: str
    account_details_sha256: str
    adapter_id: str
    adapter_version: str
    action_id: str
    event_id: str
    market_id: str
    provider_order_ref: str | None
    provider_evidence_id: str
    provider_source_sha256: str
    request_scope_sha256: str
    readback_evidence_sha256: str
    quote_observed_at: str
    account_observed_at: str
    readback_observed_at: str
    source_interval_start: str
    source_interval_end: str
    available_at: str
    source_family: str = _SOURCE_FAMILY
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "venue_id",
            "client_account_scope",
            "authenticated_account_id",
            "adapter_id",
            "adapter_version",
            "action_id",
            "event_id",
            "market_id",
            "provider_evidence_id",
            "source_family",
        ):
            _text(getattr(self, name), name)
        for name in (
            "account_details_sha256",
            "provider_source_sha256",
            "request_scope_sha256",
            "readback_evidence_sha256",
        ):
            _sha(getattr(self, name), name)
        if self.venue_id != "betfair":
            raise CampaignProviderScopeError(
                "authenticated scope supports exact Betfair venue only"
            )
        if (
            self.adapter_id != BETFAIR_ADAPTER_ID
            or self.adapter_version != BETFAIR_ADAPTER_VERSION
        ):
            raise CampaignProviderScopeError(
                "Betfair adapter identity mismatch"
            )
        if (
            self.source_family != _SOURCE_FAMILY
            or self.schema_version != SCHEMA_VERSION
        ):
            raise CampaignProviderScopeError(
                "provider scope capture schema mismatch"
            )
        if (
            self.authenticated_account_id
            != _ACCOUNT_PREFIX + self.account_details_sha256
        ):
            raise CampaignProviderScopeError(
                "authenticated account identity is not source-derived"
            )
        if self.provider_order_ref is not None:
            _text(self.provider_order_ref, "provider_order_ref")
        start = _instant(
            self.source_interval_start, "source_interval_start"
        )
        end = _instant(self.source_interval_end, "source_interval_end")
        available = _instant(self.available_at, "available_at")
        quote = _instant(self.quote_observed_at, "quote_observed_at")
        account = _instant(
            self.account_observed_at, "account_observed_at"
        )
        readback = _instant(
            self.readback_observed_at, "readback_observed_at"
        )
        if start != min(quote, account, readback):
            raise CampaignProviderScopeError(
                "provider scope start is not mechanically derived"
            )
        if end != max(quote, account, readback):
            raise CampaignProviderScopeError(
                "provider scope end is not mechanically derived"
            )
        if available != end:
            raise CampaignProviderScopeError(
                "provider scope availability must equal latest source observation"
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.verified_betfair_provider_scope_capture",
            "schema_version": self.schema_version,
            "source_family": self.source_family,
            "venue_id": self.venue_id,
            "client_account_scope": self.client_account_scope,
            "authenticated_account_id": self.authenticated_account_id,
            "account_details_sha256": self.account_details_sha256,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "action_id": self.action_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "provider_order_ref": self.provider_order_ref,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_source_sha256": self.provider_source_sha256,
            "request_scope_sha256": self.request_scope_sha256,
            "readback_evidence_sha256": self.readback_evidence_sha256,
            "quote_observed_at": self.quote_observed_at,
            "account_observed_at": self.account_observed_at,
            "readback_observed_at": self.readback_observed_at,
            "source_interval_start": self.source_interval_start,
            "source_interval_end": self.source_interval_end,
            "available_at": self.available_at,
        }

    @property
    def capture_sha256(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True, slots=True, weakref_slot=True)
class CampaignProviderScopeProjection:
    """Immutable applicability proof consumed by campaign cost allocation."""

    campaign_id: str
    campaign_version: int
    campaign_sha256: str
    session_id: str
    run_id: str
    session_evidence_id: str
    session_evidence_sha256: str
    run_summary_sha256: str
    decision_id: str
    plan_id: str
    plan_fingerprint: str
    action_id: str
    provider_capture_sha256: str
    provider_evidence_id: str
    provider_source_sha256: str
    venue_id: str
    authenticated_account_id: str
    event_id: str
    market_id: str
    source_interval_start: str
    source_interval_end: str
    observed_at: str
    available_at: str
    schema_version: int = SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        return {
            "schema": "autosport.campaign_provider_scope_projection",
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "campaign_version": self.campaign_version,
            "campaign_sha256": self.campaign_sha256,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "session_evidence_id": self.session_evidence_id,
            "session_evidence_sha256": self.session_evidence_sha256,
            "run_summary_sha256": self.run_summary_sha256,
            "decision_id": self.decision_id,
            "plan_id": self.plan_id,
            "plan_fingerprint": self.plan_fingerprint,
            "action_id": self.action_id,
            "provider_capture_sha256": self.provider_capture_sha256,
            "provider_evidence_id": self.provider_evidence_id,
            "provider_source_sha256": self.provider_source_sha256,
            "venue_id": self.venue_id,
            "authenticated_account_id": self.authenticated_account_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "source_interval_start": self.source_interval_start,
            "source_interval_end": self.source_interval_end,
            "observed_at": self.observed_at,
            "available_at": self.available_at,
        }

    @property
    def applicability_digest(self) -> str:
        return _digest(self.payload())

    def assert_provider_key(
        self,
        *,
        venue_id: str,
        authenticated_account_id: str,
        market_id: str,
        event_id: str | None = None,
    ) -> None:
        """Fail closed when a downstream receipt/source key is not this scope."""

        if (
            venue_id != self.venue_id
            or authenticated_account_id != self.authenticated_account_id
            or market_id != self.market_id
            or (event_id is not None and event_id != self.event_id)
        ):
            raise CampaignProviderScopeError(
                "provider receipt/source key is not applicable to campaign"
            )


_ACCOUNT_IDENTITY_UNAVAILABLE = (
    "stable authenticated Betfair account identity is unavailable from the "
    "supported source-owned provider surface"
)


def capture_betfair_provider_scope(
    client: BetfairReadOnlyClient,
    action: ExecutionAction,
    profile: BookmakerCapabilityProfile,
    *,
    expected_profile_sha256: str,
    provider_order_ref: str | None = None,
) -> VerifiedBetfairProviderScopeCapture:
    """Fail closed until Betfair exposes a supported stable account identity.

    getAccountDetails response bytes are valid evidence-instance integrity,
    but they are not account identity: JSON-RPC request ids change those bytes
    across reads, and the returned account-details fields do not contain a
    provider-unique account identifier. The public read-only client also
    accepts injected transports for deterministic tests. Neither fact may mint
    positive campaign/provider applicability.

    A future source-owned stable account-identity capability may replace this
    fail-closed seam. This task deliberately does not expand the provider
    adapter to add one.
    """

    if type(client) is not BetfairReadOnlyClient:
        raise CampaignProviderScopeError(
            "provider scope requires canonical BetfairReadOnlyClient"
        )
    if not isinstance(action, ExecutionAction):
        raise CampaignProviderScopeError(
            "provider scope requires canonical ExecutionAction"
        )
    if not isinstance(profile, BookmakerCapabilityProfile):
        raise CampaignProviderScopeError(
            "provider scope requires canonical BookmakerCapabilityProfile"
        )
    _sha(expected_profile_sha256, "expected_profile_sha256")
    if provider_order_ref is not None:
        _text(provider_order_ref, "provider_order_ref")
    raise CampaignProviderScopeError(_ACCOUNT_IDENTITY_UNAVAILABLE)


def assert_provider_scope_capture_authoritative(
    capture: VerifiedBetfairProviderScopeCapture,
) -> None:
    """Reject every capture until stable provider-owned account identity exists."""

    if not isinstance(capture, VerifiedBetfairProviderScopeCapture):
        raise CampaignProviderScopeError(
            "provider scope capture type is not canonical"
        )
    raise CampaignProviderScopeError(_ACCOUNT_IDENTITY_UNAVAILABLE)


def _decision_prefix_records(run_registry, run_id: str):
    summary, summary_sha256 = (
        run_registry.verified_completed_summary_for_run(run_id)
    )
    expected_sha = summary.get("decision_ledger_sha256")
    _sha(expected_sha, "completed run decision_ledger_sha256")
    ledger = JsonlDecisionLedger(
        run_registry.path.parent / "decisions.jsonl"
    )
    snapshot = ledger.verified_snapshot()
    lines = snapshot.payload.splitlines(keepends=True)
    prefix = b""
    record_count: int | None = None
    if expected_sha == hashlib.sha256(b"").hexdigest():
        record_count = 0
    for index, line in enumerate(lines, start=1):
        prefix += line
        if hashlib.sha256(prefix).hexdigest() == expected_sha:
            record_count = index
            break
    if record_count is None:
        raise CampaignProviderScopeError(
            "completed run Decision Ledger prefix is unavailable or drifted"
        )
    records = ledger.verified_records()[:record_count]
    return summary, summary_sha256, records


def _execution_plan_action(
    ledger: RealExecutionLedger,
    *,
    plan_id: str,
    action_id: str,
    attempt_id: str,
) -> tuple[ExecutionPlan, ExecutionAction, dict[str, object]]:
    if not isinstance(ledger, RealExecutionLedger):
        raise CampaignProviderScopeError(
            "execution ledger is not canonical"
        )
    events = ledger._events()
    plan_event, _ = ledger._action_payload(
        events, plan_id, action_id
    )
    plan = ledger._plan_from_dict(plan_event["payload"]["plan"])
    if (
        plan.fingerprint
        != plan_event["payload"].get("plan_fingerprint")
    ):
        raise CampaignProviderScopeError(
            "execution plan fingerprint drifted"
        )
    actions = [
        item for item in plan.actions if item.action_id == action_id
    ]
    if len(actions) != 1:
        raise CampaignProviderScopeError(
            "execution action is not uniquely reserved"
        )
    saga = ledger.saga(plan_id)
    if saga.attempt_action_ids.get(attempt_id) != action_id:
        raise CampaignProviderScopeError(
            "provider evidence attempt does not own execution action"
        )
    return plan, actions[0], plan_event


def _canonical_json_document(value: object, name: str) -> dict[str, object]:
    text = _text(value, name)
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CampaignProviderScopeError(
            f"{name} must be canonical JSON"
        ) from exc
    if type(decoded) is not dict or _canonical_json(decoded) != text:
        raise CampaignProviderScopeError(
            f"{name} must be a canonical JSON object"
        )
    return decoded


def _portfolio_execution_membership(
    records: tuple[object, ...] | list[object],
    execution_plan: ExecutionPlan,
):
    if not isinstance(execution_plan, ExecutionPlan):
        raise CampaignProviderScopeError(
            "execution membership requires canonical ExecutionPlan"
        )
    parts = execution_plan.decision_id.split(":")
    if (
        len(parts) != 4
        or parts[0] != "portfolio"
        or parts[2] != "intent"
    ):
        raise CampaignProviderScopeError(
            "execution decision identity is not canonical portfolio/intent authority"
        )
    plan_sha256 = _sha(parts[1], "execution portfolio plan_sha256")
    intent_sha256 = _sha(parts[3], "execution intent_sha256")
    expected_decision_id = (
        f"portfolio:{plan_sha256}:intent:{intent_sha256}"
    )
    if execution_plan.decision_id != expected_decision_id:
        raise CampaignProviderScopeError(
            "execution decision identity is not canonical"
        )

    matches = []
    for record in records:
        payload = getattr(record, "payload", None)
        if (
            getattr(record, "agent", None) == _PORTFOLIO_PLAN_DECISION_AGENT
            and getattr(record, "action", None) == _PORTFOLIO_PLAN_DECISION_ACTION
            and getattr(record, "decision_kind", None) == ECONOMIC_DECISION_KIND
            and isinstance(payload, Mapping)
            and payload.get(_PORTFOLIO_PLAN_SHA256_PAYLOAD_KEY)
            == plan_sha256
        ):
            matches.append(record)
    if len(matches) != 1:
        raise CampaignProviderScopeError(
            "execution plan lacks exact frozen PortfolioPlan decision membership"
        )
    decision = matches[0]
    payload = decision.payload

    try:
        portfolio_raw = _canonical_json_document(
            payload.get(_PORTFOLIO_PLAN_JSON_PAYLOAD_KEY),
            "portfolio_plan_json",
        )
        portfolio_plan = PortfolioPlan.from_dict(portfolio_raw)
    except (TypeError, ValueError) as exc:
        raise CampaignProviderScopeError(
            "frozen PortfolioPlan evidence cannot be reconstructed"
        ) from exc
    if portfolio_plan.plan_sha256 != plan_sha256:
        raise CampaignProviderScopeError(
            "frozen PortfolioPlan identity drifted"
        )
    if getattr(decision, "observed_ts", None) != portfolio_plan.decision_ts:
        raise CampaignProviderScopeError(
            "PortfolioPlan decision timestamp drifted"
        )

    intent_document = _canonical_json_document(
        payload.get(_PORTFOLIO_INTENT_EVIDENCE_JSON_PAYLOAD_KEY),
        "portfolio_intent_evidence_json",
    )
    if set(intent_document) != {"schema", "schema_version", "intents"}:
        raise CampaignProviderScopeError(
            "portfolio intent evidence fields are not canonical"
        )
    if (
        intent_document.get("schema")
        != "autosport.portfolio_plan_intent_evidence"
        or intent_document.get("schema_version") != 1
    ):
        raise CampaignProviderScopeError(
            "portfolio intent evidence schema is unsupported"
        )
    intent_rows = intent_document.get("intents")
    if type(intent_rows) is not list or len(intent_rows) != len(
        portfolio_plan.intent_sha256s
    ):
        raise CampaignProviderScopeError(
            "portfolio intent evidence vector cardinality drifted"
        )

    selected_indexes = [
        index
        for index, expected_sha in enumerate(portfolio_plan.intent_sha256s)
        if expected_sha == intent_sha256
    ]
    if len(selected_indexes) != 1:
        raise CampaignProviderScopeError(
            "execution intent is not unique frozen PortfolioPlan membership"
        )
    selected_index = selected_indexes[0]
    selected_stake = portfolio_plan.stakes[selected_index]
    if selected_stake <= 0:
        raise CampaignProviderScopeError(
            "execution intent has no positive PortfolioPlan stake authority"
        )

    selected_row: dict[str, object] | None = None
    for index, raw in enumerate(intent_rows):
        if type(raw) is not dict:
            raise CampaignProviderScopeError(
                "portfolio intent evidence member is not canonical"
            )
        if (
            raw.get("intent_id") != portfolio_plan.intent_ids[index]
            or raw.get("intent_sha256")
            != portfolio_plan.intent_sha256s[index]
        ):
            raise CampaignProviderScopeError(
                "portfolio intent evidence vector identity drifted"
            )
        if index == selected_index:
            selected_row = raw
    if selected_row is None:
        raise CampaignProviderScopeError(
            "selected portfolio intent evidence is unavailable"
        )

    try:
        opportunity = Opportunity.from_dict(selected_row.get("opportunity"))
    except (OpportunityContractError, TypeError, ValueError) as exc:
        raise CampaignProviderScopeError(
            "selected frozen Opportunity evidence is invalid"
        ) from exc
    if (
        opportunity.strategy_class.value
        != portfolio_plan.opportunity_classes[selected_index]
    ):
        raise CampaignProviderScopeError(
            "selected Opportunity class drifted from PortfolioPlan"
        )

    risk_context = selected_row.get("risk_context")
    if type(risk_context) is not dict:
        raise CampaignProviderScopeError(
            "selected intent lacks frozen risk context"
        )
    expected_risk_fields = {
        "provider_accounts",
        "bankroll_id",
        "currency",
        "measurement_window_start",
        "measurement_window_end",
        "proposal_ts",
    }
    if set(risk_context) != expected_risk_fields:
        raise CampaignProviderScopeError(
            "selected intent risk context fields are not canonical"
        )
    raw_accounts = risk_context.get("provider_accounts")
    if type(raw_accounts) is not list:
        raise CampaignProviderScopeError(
            "selected intent provider accounts are not canonical"
        )
    provider_accounts: set[tuple[str, str]] = set()
    for binding in raw_accounts:
        if type(binding) is not list or len(binding) != 2:
            raise CampaignProviderScopeError(
                "selected intent provider account binding is invalid"
            )
        pair = (
            _text(binding[0], "provider venue_id"),
            _text(binding[1], "provider account_id"),
        )
        if pair in provider_accounts:
            raise CampaignProviderScopeError(
                "selected intent provider account bindings are duplicated"
            )
        provider_accounts.add(pair)

    if _instant(execution_plan.created_at, "execution plan created_at") < _instant(
        portfolio_plan.decision_ts,
        "portfolio decision_ts",
    ):
        raise CampaignProviderScopeError(
            "execution plan predates frozen PortfolioPlan decision"
        )

    total_stake = Decimal("0")
    for action in execution_plan.actions:
        if (action.bookmaker_id, action.account_id) not in provider_accounts:
            raise CampaignProviderScopeError(
                "execution action provider account is outside frozen intent"
            )
        quote_matches = [
            quote
            for quote in opportunity.quotes
            if (
                quote.source_id == action.bookmaker_id
                and quote.event_id == action.event_id
                and quote.market_id == action.market_id
                and quote.selection_id == action.selection_id
                and quote.decimal_odds == action.requested_odds
                and _canonical_instant(
                    quote.observed_ts,
                    "frozen quote observed_ts",
                )
                == _canonical_instant(
                    action.quote_observed_at,
                    "execution quote_observed_at",
                )
            )
        ]
        if len(quote_matches) != 1:
            raise CampaignProviderScopeError(
                "execution action is not exact frozen Opportunity quote membership"
            )
        total_stake += action.requested_stake
    if total_stake > selected_stake:
        raise CampaignProviderScopeError(
            "execution action vector exceeds frozen PortfolioPlan stake"
        )
    return decision, portfolio_plan


def _resolve_campaign_provider_scope_raw(
    authority: FinalizedCampaignAuthority,
    *,
    session_id: str,
    execution_ledger: RealExecutionLedger,
    plan_id: str,
    attempt_id: str,
    capture: VerifiedBetfairProviderScopeCapture,
    expected_applicability_digest: str | None = None,
) -> CampaignProviderScopeProjection:
    """Return applicability only on exact campaign/execution/provider membership."""

    if not isinstance(authority, FinalizedCampaignAuthority):
        raise CampaignProviderScopeError(
            "campaign authority is not canonical"
        )
    assert_provider_scope_capture_authoritative(capture)
    campaign_projection = authority.projection()
    campaign = authority._campaign
    run_registry = campaign._run_registry
    if run_registry is None:
        raise CampaignProviderScopeError(
            "finalized campaign lost canonical RunRegistry binding"
        )
    sessions = [
        item
        for item in campaign.sessions
        if item.session_id == session_id
    ]
    if len(sessions) != 1:
        raise CampaignProviderScopeError(
            "campaign session is not uniquely resolved"
        )
    session = sessions[0]
    session_refs = [
        item
        for item in campaign_projection.session_refs
        if item.evidence_id == session.evidence_id
        and item.evidence_sha256 == session.evidence_sha256
    ]
    if len(session_refs) != 1:
        raise CampaignProviderScopeError(
            "session is not exact finalized campaign membership"
        )

    summary, run_summary_sha256, records = _decision_prefix_records(
        run_registry, session.run_id
    )
    plan, action, plan_event = _execution_plan_action(
        execution_ledger,
        plan_id=plan_id,
        action_id=capture.action_id,
        attempt_id=attempt_id,
    )
    run_records = [
        record
        for record in records
        if getattr(record, "replay_run_id", None) == session.run_id
    ]
    decision, _portfolio_plan = _portfolio_execution_membership(
        run_records,
        plan,
    )

    if (
        action.bookmaker_id != capture.venue_id
        or action.event_id != capture.event_id
        or action.market_id != capture.market_id
        or action.action_id != capture.action_id
    ):
        raise CampaignProviderScopeError(
            "provider capture conflicts with reserved execution action"
        )
    binding = execution_ledger.provider_evidence_binding(
        attempt_id
    )
    if binding is None:
        raise CampaignProviderScopeError(
            "execution attempt lacks durable provider evidence binding"
        )
    if (
        binding.get("evidence_id") != capture.provider_evidence_id
        or _canonical_instant(
            binding.get("observed_at"),
            "provider binding observed_at",
        )
        != _canonical_instant(
            capture.readback_observed_at,
            "readback observed_at",
        )
    ):
        raise CampaignProviderScopeError(
            "durable provider evidence binding drifted"
        )

    observations = tuple(
        _canonical_instant(
            value, "campaign observation timestamp"
        )
        for value in session.observation_timestamps
    )
    quote_at = _canonical_instant(
        action.quote_observed_at, "action quote_observed_at"
    )
    if quote_at not in observations:
        raise CampaignProviderScopeError(
            "execution quote is not exact campaign observation membership"
        )
    window_start = _instant(
        session.evaluation_window_start,
        "evaluation_window_start",
    )
    window_end = _instant(
        session.evaluation_window_end,
        "evaluation_window_end",
    )
    source_start = _instant(
        capture.source_interval_start,
        "source_interval_start",
    )
    source_end = _instant(
        capture.source_interval_end,
        "source_interval_end",
    )
    as_of = _instant(session.as_of, "session as_of")
    if source_start > window_end or source_end < window_start:
        raise CampaignProviderScopeError(
            "provider source interval does not intersect campaign interval"
        )
    if source_end > as_of:
        raise CampaignProviderScopeError(
            "future-available provider evidence cannot backdate campaign scope"
        )
    if _instant(plan.created_at, "plan created_at") > as_of:
        raise CampaignProviderScopeError(
            "execution plan postdates campaign authority"
        )
    if _instant(
        plan_event["recorded_at"],
        "plan recorded_at",
    ) > as_of:
        raise CampaignProviderScopeError(
            "execution reservation postdates campaign authority"
        )

    causal = summary.get("campaign_causal_membership")
    if (
        not isinstance(causal, dict)
        or causal.get("observation_membership_sha256")
        != session.observation_membership_sha256
    ):
        raise CampaignProviderScopeError(
            "campaign causal membership drifted"
        )

    resolved = CampaignProviderScopeProjection(
        campaign_id=campaign_projection.campaign_id,
        campaign_version=campaign_projection.campaign_version,
        campaign_sha256=campaign_projection.campaign_sha256,
        session_id=session.session_id,
        run_id=session.run_id,
        session_evidence_id=session.evidence_id,
        session_evidence_sha256=session.evidence_sha256,
        run_summary_sha256=run_summary_sha256,
        decision_id=decision.decision_id,
        plan_id=plan.plan_id,
        plan_fingerprint=plan.fingerprint,
        action_id=action.action_id,
        provider_capture_sha256=capture.capture_sha256,
        provider_evidence_id=capture.provider_evidence_id,
        provider_source_sha256=capture.provider_source_sha256,
        venue_id=capture.venue_id,
        authenticated_account_id=capture.authenticated_account_id,
        event_id=capture.event_id,
        market_id=capture.market_id,
        source_interval_start=capture.source_interval_start,
        source_interval_end=capture.source_interval_end,
        observed_at=capture.readback_observed_at,
        available_at=capture.available_at,
    )
    if expected_applicability_digest is not None:
        if (
            _sha(
                expected_applicability_digest,
                "expected_applicability_digest",
            )
            != resolved.applicability_digest
        ):
            raise CampaignProviderScopeError(
                "re-resolved provider scope digest drifted"
            )
    return resolved


def _install_projection_authority() -> None:
    issued: dict[int, tuple[object, str]] = {}
    raw_resolve = _resolve_campaign_provider_scope_raw

    def authoritative_resolve(
        authority: FinalizedCampaignAuthority,
        *,
        session_id: str,
        execution_ledger: RealExecutionLedger,
        plan_id: str,
        attempt_id: str,
        capture: VerifiedBetfairProviderScopeCapture,
        expected_applicability_digest: str | None = None,
    ) -> CampaignProviderScopeProjection:
        projection = raw_resolve(
            authority,
            session_id=session_id,
            execution_ledger=execution_ledger,
            plan_id=plan_id,
            attempt_id=attempt_id,
            capture=capture,
            expected_applicability_digest=expected_applicability_digest,
        )
        key = id(projection)

        def forget(
            _weakref: object,
            *,
            projection_key: int = key,
        ) -> None:
            issued.pop(projection_key, None)

        issued[key] = (
            ref(projection, forget),
            projection.applicability_digest,
        )
        return projection

    def assert_authoritative(
        projection: CampaignProviderScopeProjection,
    ) -> None:
        if not isinstance(projection, CampaignProviderScopeProjection):
            raise CampaignProviderScopeError(
                "campaign provider scope projection type is not canonical"
            )
        record = issued.get(id(projection))
        if record is None or record[0]() is not projection:
            raise CampaignProviderScopeError(
                "campaign provider scope projection was not issued by canonical resolver"
            )
        if record[1] != projection.applicability_digest:
            raise CampaignProviderScopeError(
                "campaign provider scope projection changed after resolution"
            )

    globals()["resolve_campaign_provider_scope"] = authoritative_resolve
    globals()[
        "assert_campaign_provider_scope_authoritative"
    ] = assert_authoritative


_install_projection_authority()
del _install_projection_authority
