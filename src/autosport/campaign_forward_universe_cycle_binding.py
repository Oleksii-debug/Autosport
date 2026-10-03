from __future__ import annotations

"""Bind one campaign-gated provider capture to the exact durable forward universe.

This module is composition only. It does not own a scheduler, provider session,
collector chronology, universe store, or observation ledger. Positive authority is
re-resolved from those existing product-owned authorities every time.
"""

import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from . import provider_evaluation_universe as _provider_universe_module
from ._forward_universe_backing_guard import (
    ForwardUniverseBackingGuardError,
    load_guarded_provider_evaluation_universe,
)
from .campaign_inception import (
    CampaignInceptionReceipt,
    CampaignInceptionSourceSpec,
    establish_campaign_inception,
)
from .campaign_provider_cycle_capture import (
    ARTIFACT_KIND,
    CampaignCompleteBoardCycleReceipt,
    CampaignProviderCycleCaptureIntegrityError,
    _require_provider_evidence_campaign_scope,
)
from .causal_collector import CollectorDeltaStore
from .evaluation_universe import EvaluationUniverseLedger
from .event_lifecycle import ContinuousEventLifecycle
from .forward_evaluation_universe_binding import (
    ForwardEvaluationUniverseBindingError,
    ForwardUniverseAuthorityIdentity,
    authorize_forward_source_receipts,
    resolve_forward_universe_authority_identity,
)
from .forward_evidence_completeness import (
    AuthoritativeSourceReceipt,
    CampaignEvidence,
    ForwardEvidenceProtocolEnvelope,
    ForwardOpportunityEnvelope,
    VerificationCode,
    VerificationResult,
    verify_campaign,
)
from .forward_universe_precommit_authority import ForwardUniversePrecommitLocator
from .provider_evaluation_universe import (
    ProviderEvaluationUniverseStore,
    ProviderEvaluationUniverseError,
    _ISSUED_UNIVERSES,
)
from .provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardSnapshot,
)


_SCHEMA_VERSION = 1
_DOMAIN = "autosport.campaign-forward-universe-cycle-authority.v1"
_COMPOSED_VERIFICATION_DOMAIN = "autosport.campaign-forward-evidence-verification.v1"
_COMPOSED_VERIFICATION_SCOPE = "CYCLE_BOUND_PROVIDER_UNIVERSE_STRUCTURAL_ONLY"
_AUTHORITY_ISSUANCE_CAPABILITY = object()
_VERIFICATION_ISSUANCE_CAPABILITY = object()
_HEX = frozenset("0123456789abcdef")

_ESTABLISH_CAMPAIGN = establish_campaign_inception
_RESOLVE_ARTIFACT = CollectorDeltaStore.collector_cycle_observation_artifact_evidence
_SCHEDULE_DUE_AT = CollectorDeltaStore._collector_schedule_due_at
_LOAD_PROVIDER_EVIDENCE = CompleteGameBoardEvidenceStore.load
_PROVIDER_EVIDENCE_SCOPE = _require_provider_evidence_campaign_scope
_PROVIDER_EVIDENCE_SCOPE_ERROR = CampaignProviderCycleCaptureIntegrityError
_GUARDED_UNIVERSE_LOAD = load_guarded_provider_evaluation_universe
_RESOLVE_FORWARD_IDENTITY = resolve_forward_universe_authority_identity
_AUTHORIZE_FORWARD_RECEIPTS = authorize_forward_source_receipts
_VERIFY_FORWARD_EVIDENCE = verify_campaign
_CANONICAL_CAMPAIGN_EVIDENCE = CampaignEvidence
_CANONICAL_VERIFICATION_RESULT = VerificationResult
_CANONICAL_VERIFICATION_CODE = VerificationCode
_CANONICAL_CAMPAIGN_EVIDENCE_DATACLASS_FIELDS = CampaignEvidence.__dataclass_fields__
_CANONICAL_CAMPAIGN_EVIDENCE_FIELD_ITEMS = tuple(
    _CANONICAL_CAMPAIGN_EVIDENCE_DATACLASS_FIELDS.items()
)
_CANONICAL_CAMPAIGN_EVIDENCE_FIELD_DESCRIPTORS = tuple(
    (name, inspect.getattr_static(CampaignEvidence, name))
    for name in _CANONICAL_CAMPAIGN_EVIDENCE_DATACLASS_FIELDS
)
_CANONICAL_CAMPAIGN_EVIDENCE_INIT = inspect.getattr_static(
    CampaignEvidence,
    "__init__",
)
_CANONICAL_CAMPAIGN_EVIDENCE_INIT_CODE = getattr(
    _CANONICAL_CAMPAIGN_EVIDENCE_INIT,
    "__code__",
    None,
)
_CANONICAL_VERIFICATION_RESULT_DATACLASS_FIELDS = VerificationResult.__dataclass_fields__
_CANONICAL_VERIFICATION_RESULT_FIELD_ITEMS = tuple(
    _CANONICAL_VERIFICATION_RESULT_DATACLASS_FIELDS.items()
)
_CANONICAL_VERIFICATION_RESULT_FIELD_DESCRIPTORS = tuple(
    (name, inspect.getattr_static(VerificationResult, name))
    for name in _CANONICAL_VERIFICATION_RESULT_DATACLASS_FIELDS
)

_CAPTURED_CALLABLES = (
    ("_ESTABLISH_CAMPAIGN", _ESTABLISH_CAMPAIGN, _ESTABLISH_CAMPAIGN.__code__),
    ("_RESOLVE_ARTIFACT", _RESOLVE_ARTIFACT, _RESOLVE_ARTIFACT.__code__),
    ("_SCHEDULE_DUE_AT", _SCHEDULE_DUE_AT, _SCHEDULE_DUE_AT.__code__),
    (
        "_LOAD_PROVIDER_EVIDENCE",
        _LOAD_PROVIDER_EVIDENCE,
        _LOAD_PROVIDER_EVIDENCE.__code__,
    ),
    (
        "_PROVIDER_EVIDENCE_SCOPE",
        _PROVIDER_EVIDENCE_SCOPE,
        _PROVIDER_EVIDENCE_SCOPE.__code__,
    ),
    (
        "_GUARDED_UNIVERSE_LOAD",
        _GUARDED_UNIVERSE_LOAD,
        _GUARDED_UNIVERSE_LOAD.__code__,
    ),
    (
        "_RESOLVE_FORWARD_IDENTITY",
        _RESOLVE_FORWARD_IDENTITY,
        _RESOLVE_FORWARD_IDENTITY.__code__,
    ),
    (
        "_AUTHORIZE_FORWARD_RECEIPTS",
        _AUTHORIZE_FORWARD_RECEIPTS,
        _AUTHORIZE_FORWARD_RECEIPTS.__code__,
    ),
    (
        "_VERIFY_FORWARD_EVIDENCE",
        _VERIFY_FORWARD_EVIDENCE,
        _VERIFY_FORWARD_EVIDENCE.__code__,
    ),
)
_CANONICAL_COLLECTOR_ARTIFACT_RESOLVER = inspect.getattr_static(
    CollectorDeltaStore,
    "collector_cycle_observation_artifact_evidence",
)
_CANONICAL_PROVIDER_EVIDENCE_LOADER = inspect.getattr_static(
    CompleteGameBoardEvidenceStore,
    "load",
)
_CANONICAL_ISSUED_UNIVERSES = _ISSUED_UNIVERSES
_PROVIDER_UNIVERSE_VALUES = {
    "_ISSUED_UNIVERSES": _provider_universe_module._ISSUED_UNIVERSES,
    "EvaluationRow": _provider_universe_module.EvaluationRow,
    "ObservationIntakeSnapshot": _provider_universe_module.ObservationIntakeSnapshot,
    "EvaluationUniverse": _provider_universe_module.EvaluationUniverse,
    "CompleteGameBoardSnapshot": _provider_universe_module.CompleteGameBoardSnapshot,
    "ContinuousEventLifecycle": _provider_universe_module.ContinuousEventLifecycle,
    "CompleteBoardMemberSpec": _provider_universe_module.CompleteBoardMemberSpec,
    "SlotState": _provider_universe_module.SlotState,
    "_CONSUMER_KIND": _provider_universe_module._CONSUMER_KIND,
    "_PROVIDER_ID": _provider_universe_module._PROVIDER_ID,
}
_PROVIDER_UNIVERSE_CALLABLES = tuple(
    (
        name,
        target,
        getattr(target, "__code__", None),
    )
    for name, target in (
        (
            "assert_complete_game_board_authoritative",
            _provider_universe_module.assert_complete_game_board_authoritative,
        ),
        (
            "complete_game_board_member_specs",
            _provider_universe_module.complete_game_board_member_specs,
        ),
        (
            "_resolve_event_reveal_binding",
            _provider_universe_module._resolve_event_reveal_binding,
        ),
        ("_selection_labels", _provider_universe_module._selection_labels),
        (
            "_validate_row_against_member",
            _provider_universe_module._validate_row_against_member,
        ),
        (
            "_row_semantic_sha256",
            _provider_universe_module._row_semantic_sha256,
        ),
        ("_remember_issued", _provider_universe_module._remember_issued),
        ("_text", _provider_universe_module._text),
        ("_sha", _provider_universe_module._sha),
        ("_instant", _provider_universe_module._instant),
        ("_digest", _provider_universe_module._digest),
    )
)
_INTERNAL_CALLABLES: tuple[tuple[str, object, object], ...] = ()


class CampaignForwardUniverseCycleBindingError(RuntimeError):
    """Campaign/cycle/provider/universe authority does not compose exactly."""


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignForwardUniverseCycleBindingError(
            f"{field} must be non-empty canonical text"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, field: str) -> str:
    raw = _text(value, field)
    if (
        len(raw) != 64
        or raw != raw.lower()
        or any(character not in _HEX for character in raw)
    ):
        raise CampaignForwardUniverseCycleBindingError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _instant(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            f"{field} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignForwardUniverseCycleBindingError(
            f"{field} must include timezone"
        )
    return parsed.astimezone(timezone.utc)


def _require_cycle_observation_chronology(
    *,
    campaign: CampaignInceptionReceipt,
    source_spec: CampaignInceptionSourceSpec,
    snapshot: CompleteGameBoardSnapshot,
    collector_evidence: dict[str, object],
) -> None:
    attempted = _instant(
        collector_evidence.get("attempted_at"),
        "collector attempted_at",
    )
    completed = _instant(
        collector_evidence.get("completed_at"),
        "collector completed_at",
    )
    captured = _instant(snapshot.captured_at, "provider captured_at")
    slot_due = _instant(
        collector_evidence.get("due_at"),
        "collector due_at",
    )
    slot_ordinal = collector_evidence.get("slot_ordinal")
    if (
        type(slot_ordinal) is not int
        or slot_ordinal < source_spec.evaluation_start_slot_ordinal
        or slot_ordinal > source_spec.evaluation_end_slot_ordinal
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "collector cycle lies outside frozen campaign evaluation slots"
        )
    try:
        slot_deadline = _instant(
            _SCHEDULE_DUE_AT(
                anchor_at=source_spec.anchor_at,
                interval_seconds=repr(source_spec.interval_seconds),
                slot_ordinal=slot_ordinal + 1,
            ),
            "collector next slot due_at",
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "collector fixed schedule slot window is not representable"
        ) from exc
    if attempted < slot_due or attempted >= slot_deadline:
        raise CampaignForwardUniverseCycleBindingError(
            "collector START is outside precommitted fixed schedule slot window"
        )
    if captured >= slot_deadline:
        raise CampaignForwardUniverseCycleBindingError(
            "provider observation is outside precommitted fixed schedule slot window"
        )
    not_before = _instant(
        campaign.observation_not_before,
        "campaign observation_not_before",
    )
    not_after = _instant(
        campaign.observation_not_after,
        "campaign observation_not_after",
    )
    if completed < attempted:
        raise CampaignForwardUniverseCycleBindingError(
            "collector cycle completion predates authorized START"
        )
    if captured < attempted or captured > completed:
        raise CampaignForwardUniverseCycleBindingError(
            "provider observation is outside authorized collector cycle chronology"
        )
    if (
        attempted < not_before
        or attempted > not_after
        or captured < not_before
        or captured > not_after
        or completed > not_after
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "provider cycle is outside precommitted campaign observation window"
        )


@dataclass(frozen=True, slots=True, init=False)
class CampaignForwardUniverseCycleAuthority:
    """Resolver-issued proof that one #1185 universe uses one gated provider artifact."""

    schema_version: int
    campaign_id: str
    source_id: str
    cycle_receipt_sha256: str
    campaign_receipt_sha256: str
    provider_evidence_sha256: str
    provider_frame_sha256: str
    collector_artifact_evidence_sha256: str
    precommit_authority_sha256: str
    prospective_evaluation_plan_sha256: str
    backing_locator_sha256: str
    universe_sha256: str
    membership_sha256: str
    member_count: int
    authority_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError(
            "CampaignForwardUniverseCycleAuthority is resolver-issued; "
            "call resolve_campaign_forward_universe_cycle_authority"
        )

    @classmethod
    def _issue(
        cls,
        *,
        _issuance_capability: object,
        campaign_id: str,
        source_id: str,
        cycle_receipt_sha256: str,
        campaign_receipt_sha256: str,
        provider_evidence_sha256: str,
        provider_frame_sha256: str,
        collector_artifact_evidence_sha256: str,
        forward_identity: ForwardUniverseAuthorityIdentity,
    ) -> "CampaignForwardUniverseCycleAuthority":
        if cls is not CampaignForwardUniverseCycleAuthority:
            raise TypeError(
                "campaign forward-cycle authority issuer requires exact canonical class"
            )
        if _issuance_capability is not _AUTHORITY_ISSUANCE_CAPABILITY:
            raise TypeError(
                "campaign forward-cycle authority issuance is resolver-private"
            )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "campaign_id": _text(campaign_id, "campaign_id"),
            "source_id": _text(source_id, "source_id"),
            "cycle_receipt_sha256": _sha(
                cycle_receipt_sha256,
                "cycle_receipt_sha256",
            ),
            "campaign_receipt_sha256": _sha(
                campaign_receipt_sha256,
                "campaign_receipt_sha256",
            ),
            "provider_evidence_sha256": _sha(
                provider_evidence_sha256,
                "provider_evidence_sha256",
            ),
            "provider_frame_sha256": _sha(
                provider_frame_sha256,
                "provider_frame_sha256",
            ),
            "collector_artifact_evidence_sha256": _sha(
                collector_artifact_evidence_sha256,
                "collector_artifact_evidence_sha256",
            ),
            "precommit_authority_sha256": _sha(
                forward_identity.precommit_authority_sha256,
                "precommit_authority_sha256",
            ),
            "prospective_evaluation_plan_sha256": _sha(
                forward_identity.prospective_evaluation_plan_sha256,
                "prospective_evaluation_plan_sha256",
            ),
            "backing_locator_sha256": _sha(
                forward_identity.backing_locator_sha256,
                "backing_locator_sha256",
            ),
            "universe_sha256": _sha(
                forward_identity.universe_sha256,
                "universe_sha256",
            ),
            "membership_sha256": _sha(
                forward_identity.membership_sha256,
                "membership_sha256",
            ),
            "member_count": forward_identity.member_count,
        }
        if type(payload["member_count"]) is not int or payload["member_count"] <= 0:
            raise CampaignForwardUniverseCycleBindingError(
                "member_count must be a positive integer"
            )
        payload["authority_sha256"] = _digest(
            {"domain": _DOMAIN, "authority": payload}
        )
        instance = object.__new__(cls)
        for name in _AUTHORITY_FIELD_NAMES:
            object.__setattr__(instance, name, payload[name])
        return instance


@dataclass(frozen=True, slots=True, init=False)
class CampaignForwardEvidenceVerification:
    """Resolver-issued machine truth for cycle-bound structural verification.

    This receipt proves that the structural #879 verdict was computed with source
    receipts re-issued from one exact campaign/cycle/provider/universe authority.
    It is deliberately not a promotion, execution, settlement, or profit authority.
    """

    schema_version: int
    campaign_id: str
    protocol_sha256: str
    structural_result_sha256: str
    structural_ok: bool
    structural_codes: tuple[str, ...]
    terminal_root_sha256: str | None
    candidate_count: int
    campaign_cycle_authority_sha256: str
    prospective_evaluation_plan_sha256: str
    universe_sha256: str
    membership_sha256: str
    verification_scope: str
    provider_universe_authority_resolved: bool
    promotion_ready: bool
    real_money_ready: bool
    receipt_sha256: str

    def __new__(cls, *args: object, **kwargs: object):
        raise TypeError(
            "CampaignForwardEvidenceVerification is resolver-issued; "
            "call verify_campaign_forward_evidence"
        )

    @classmethod
    def _issue(
        cls,
        *,
        _issuance_capability: object,
        authority: CampaignForwardUniverseCycleAuthority,
        structural_result: VerificationResult,
    ) -> "CampaignForwardEvidenceVerification":
        if cls is not CampaignForwardEvidenceVerification:
            raise TypeError(
                "campaign forward verification issuer requires exact canonical class"
            )
        if _issuance_capability is not _VERIFICATION_ISSUANCE_CAPABILITY:
            raise TypeError(
                "campaign forward verification issuance is resolver-private"
            )
        if type(authority) is not CampaignForwardUniverseCycleAuthority:
            raise TypeError(
                "authority must be exact CampaignForwardUniverseCycleAuthority"
            )
        if type(structural_result) is not VerificationResult:
            raise TypeError("structural_result must be exact VerificationResult")
        if type(structural_result.ok) is not bool:
            raise CampaignForwardUniverseCycleBindingError(
                "structural result ok flag must be boolean"
            )
        if (
            type(structural_result.codes) is not tuple
            or not structural_result.codes
            or not all(
                type(code) is _CANONICAL_VERIFICATION_CODE
                for code in structural_result.codes
            )
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "structural result codes must be canonical VerificationCode values"
            )
        structural_pass = structural_result.codes == (
            _CANONICAL_VERIFICATION_CODE.PASS,
        )
        if structural_result.ok is not structural_pass:
            raise CampaignForwardUniverseCycleBindingError(
                "structural result ok flag conflicts with canonical verification codes"
            )
        if (
            type(structural_result.candidate_count) is not int
            or structural_result.candidate_count < 0
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "structural result candidate_count must be a non-negative integer"
            )
        if type(structural_result.details) is not tuple or not all(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is str
            and type(item[1]) is str
            for item in structural_result.details
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "structural result details must be canonical text pairs"
            )
        protocol_sha256 = _sha(structural_result.protocol_sha256, "protocol_sha256")
        terminal_root_sha256 = structural_result.terminal_root_sha256
        if terminal_root_sha256 is not None:
            terminal_root_sha256 = _sha(
                terminal_root_sha256,
                "terminal_root_sha256",
            )
        structural_codes = tuple(code.value for code in structural_result.codes)
        structural_result_sha256 = _digest(
            {
                "domain": "autosport.forward-evidence-structural-result.v1",
                "result": {
                    "ok": structural_result.ok,
                    "codes": structural_codes,
                    "protocol_sha256": protocol_sha256,
                    "terminal_root_sha256": terminal_root_sha256,
                    "candidate_count": structural_result.candidate_count,
                    "details": tuple(structural_result.details),
                },
            }
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "campaign_id": _text(authority.campaign_id, "campaign_id"),
            "protocol_sha256": protocol_sha256,
            "structural_result_sha256": structural_result_sha256,
            "structural_ok": structural_result.ok,
            "structural_codes": structural_codes,
            "terminal_root_sha256": terminal_root_sha256,
            "candidate_count": structural_result.candidate_count,
            "campaign_cycle_authority_sha256": _sha(
                authority.authority_sha256,
                "campaign_cycle_authority_sha256",
            ),
            "prospective_evaluation_plan_sha256": _sha(
                authority.prospective_evaluation_plan_sha256,
                "prospective_evaluation_plan_sha256",
            ),
            "universe_sha256": _sha(authority.universe_sha256, "universe_sha256"),
            "membership_sha256": _sha(
                authority.membership_sha256,
                "membership_sha256",
            ),
            "verification_scope": _COMPOSED_VERIFICATION_SCOPE,
            "provider_universe_authority_resolved": True,
            "promotion_ready": False,
            "real_money_ready": False,
        }
        payload["receipt_sha256"] = _digest(
            {
                "domain": _COMPOSED_VERIFICATION_DOMAIN,
                "verification": payload,
            }
        )
        instance = object.__new__(cls)
        for name in _VERIFICATION_RECEIPT_FIELD_NAMES:
            object.__setattr__(instance, name, payload[name])
        return instance


_CANONICAL_AUTHORITY_CLASS = CampaignForwardUniverseCycleAuthority
_CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY = _AUTHORITY_ISSUANCE_CAPABILITY
_CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY = _VERIFICATION_ISSUANCE_CAPABILITY
_AUTHORITY_FIELD_NAMES = tuple(
    CampaignForwardUniverseCycleAuthority.__dataclass_fields__
)
_CANONICAL_AUTHORITY_FIELD_DESCRIPTORS = tuple(
    (
        name,
        inspect.getattr_static(CampaignForwardUniverseCycleAuthority, name),
    )
    for name in _AUTHORITY_FIELD_NAMES
)
_CANONICAL_AUTHORITY_ISSUER = inspect.getattr_static(
    CampaignForwardUniverseCycleAuthority,
    "_issue",
)
_CANONICAL_AUTHORITY_ISSUER_FUNCTION = _CANONICAL_AUTHORITY_ISSUER.__func__
_CANONICAL_AUTHORITY_ISSUER_CODE = _CANONICAL_AUTHORITY_ISSUER_FUNCTION.__code__
_CANONICAL_VERIFICATION_CLASS = CampaignForwardEvidenceVerification
_VERIFICATION_RECEIPT_FIELD_NAMES = tuple(
    CampaignForwardEvidenceVerification.__dataclass_fields__
)
_CANONICAL_VERIFICATION_FIELD_DESCRIPTORS = tuple(
    (
        name,
        inspect.getattr_static(CampaignForwardEvidenceVerification, name),
    )
    for name in _VERIFICATION_RECEIPT_FIELD_NAMES
)
_CANONICAL_VERIFICATION_ISSUER = inspect.getattr_static(
    CampaignForwardEvidenceVerification,
    "_issue",
)
_CANONICAL_VERIFICATION_ISSUER_FUNCTION = _CANONICAL_VERIFICATION_ISSUER.__func__
_CANONICAL_VERIFICATION_ISSUER_CODE = _CANONICAL_VERIFICATION_ISSUER_FUNCTION.__code__
_CYCLE_RECEIPT_FIELD_NAMES = tuple(
    CampaignCompleteBoardCycleReceipt.__dataclass_fields__
)
_CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS = tuple(
    (
        name,
        inspect.getattr_static(CampaignCompleteBoardCycleReceipt, name),
    )
    for name in _CYCLE_RECEIPT_FIELD_NAMES
)
_INCEPTION_RECEIPT_FIELD_NAMES = tuple(
    CampaignInceptionReceipt.__dataclass_fields__
)
_CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS = tuple(
    (
        name,
        inspect.getattr_static(CampaignInceptionReceipt, name),
    )
    for name in _INCEPTION_RECEIPT_FIELD_NAMES
)
_CANONICAL_ARTIFACT_KIND = ARTIFACT_KIND
_CANONICAL_HASHLIB = hashlib
_CANONICAL_SHA256 = hashlib.sha256
_CANONICAL_JSON = json
_CANONICAL_JSON_DUMPS = json.dumps
_CANONICAL_DATETIME = datetime
_CANONICAL_TIMEZONE = timezone
_CANONICAL_MODULE_GLOBALS = globals()
_CANONICAL_INSPECT = inspect
_CANONICAL_GETATTR_STATIC = inspect.getattr_static
_CANONICAL_GETATTR_STATIC_CODE = _CANONICAL_GETATTR_STATIC.__code__
_CANONICAL_GETATTR_STATIC_GLOBALS = _CANONICAL_GETATTR_STATIC.__globals__
_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS = tuple(
    (
        name,
        _CANONICAL_GETATTR_STATIC_GLOBALS[name],
        getattr(_CANONICAL_GETATTR_STATIC_GLOBALS[name], "__code__", None),
    )
    for name in _CANONICAL_GETATTR_STATIC_CODE.co_names
    if name in _CANONICAL_GETATTR_STATIC_GLOBALS
)


def _require_dispatch_integrity() -> None:
    module_globals = _CANONICAL_MODULE_GLOBALS
    if (
        "type" in module_globals
        or "getattr" in module_globals
        or "any" in module_globals
        or "all" in module_globals
        or "len" in module_globals
        or "sorted" in module_globals
        or "tuple" in module_globals
        or "isinstance" in module_globals
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward-cycle builtin dispatch shadowed"
        )
    if module_globals.get("_CANONICAL_MODULE_GLOBALS") is not module_globals:
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward-cycle module authority mapping changed"
        )
    if (
        module_globals.get("inspect") is not _CANONICAL_INSPECT
        or _CANONICAL_INSPECT.getattr_static is not _CANONICAL_GETATTR_STATIC
        or _CANONICAL_GETATTR_STATIC.__code__ is not _CANONICAL_GETATTR_STATIC_CODE
        or _CANONICAL_GETATTR_STATIC.__globals__
        is not _CANONICAL_GETATTR_STATIC_GLOBALS
        or any(
            _CANONICAL_GETATTR_STATIC_GLOBALS.get(name) is not target
            or getattr(target, "__code__", None) is not code
            for name, target, code in _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
        )
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward-cycle reflection dispatch changed"
        )
    if (
        module_globals.get("CampaignForwardUniverseCycleAuthority")
        is not _CANONICAL_AUTHORITY_CLASS
        or module_globals.get("CampaignForwardEvidenceVerification")
        is not _CANONICAL_VERIFICATION_CLASS
        or module_globals.get("_COMPOSED_VERIFICATION_DOMAIN")
        != _COMPOSED_VERIFICATION_DOMAIN
        or module_globals.get("_COMPOSED_VERIFICATION_SCOPE")
        != _COMPOSED_VERIFICATION_SCOPE
        or module_globals.get("VerificationCode")
        is not _CANONICAL_VERIFICATION_CODE
        or module_globals.get("_AUTHORITY_ISSUANCE_CAPABILITY")
        is not _CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY
        or module_globals.get("_VERIFICATION_ISSUANCE_CAPABILITY")
        is not _CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY
        or module_globals.get("ARTIFACT_KIND") != _CANONICAL_ARTIFACT_KIND
        or module_globals.get("CampaignProviderCycleCaptureIntegrityError")
        is not _PROVIDER_EVIDENCE_SCOPE_ERROR
        or module_globals.get("_PROVIDER_EVIDENCE_SCOPE_ERROR")
        is not _PROVIDER_EVIDENCE_SCOPE_ERROR
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward-cycle authority class or artifact kind changed"
        )
    if (
        module_globals.get("hashlib") is not _CANONICAL_HASHLIB
        or _CANONICAL_HASHLIB.sha256 is not _CANONICAL_SHA256
        or module_globals.get("json") is not _CANONICAL_JSON
        or _CANONICAL_JSON.dumps is not _CANONICAL_JSON_DUMPS
        or module_globals.get("datetime") is not _CANONICAL_DATETIME
        or module_globals.get("timezone") is not _CANONICAL_TIMEZONE
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward-cycle chronology/digest primitives changed"
        )
    current_issuer = inspect.getattr_static(
        _CANONICAL_AUTHORITY_CLASS,
        "_issue",
    )
    if (
        current_issuer is not _CANONICAL_AUTHORITY_ISSUER
        or current_issuer.__func__ is not _CANONICAL_AUTHORITY_ISSUER_FUNCTION
        or current_issuer.__func__.__code__ is not _CANONICAL_AUTHORITY_ISSUER_CODE
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward-cycle authority issuer is rebound"
        )
    for name, descriptor in _CANONICAL_AUTHORITY_FIELD_DESCRIPTORS:
        if inspect.getattr_static(_CANONICAL_AUTHORITY_CLASS, name) is not descriptor:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle authority field descriptor changed: " + name
            )
    current_verification_issuer = inspect.getattr_static(
        _CANONICAL_VERIFICATION_CLASS,
        "_issue",
    )
    if (
        current_verification_issuer is not _CANONICAL_VERIFICATION_ISSUER
        or current_verification_issuer.__func__
        is not _CANONICAL_VERIFICATION_ISSUER_FUNCTION
        or current_verification_issuer.__func__.__code__
        is not _CANONICAL_VERIFICATION_ISSUER_CODE
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign forward verification issuer is rebound"
        )
    for name, descriptor in _CANONICAL_VERIFICATION_FIELD_DESCRIPTORS:
        if (
            inspect.getattr_static(_CANONICAL_VERIFICATION_CLASS, name)
            is not descriptor
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward verification field descriptor changed: " + name
            )
    for name, descriptor in _CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS:
        if inspect.getattr_static(CampaignCompleteBoardCycleReceipt, name) is not descriptor:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign cycle receipt field descriptor changed: " + name
            )
    for name, descriptor in _CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS:
        if inspect.getattr_static(CampaignInceptionReceipt, name) is not descriptor:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign inception receipt field descriptor changed: " + name
            )
    if (
        module_globals.get("_provider_universe_module")
        is not _provider_universe_module
        or _provider_universe_module._ISSUED_UNIVERSES
        is not _CANONICAL_ISSUED_UNIVERSES
        or module_globals.get("_ISSUED_UNIVERSES")
        is not _CANONICAL_ISSUED_UNIVERSES
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "provider-universe issued-object cleanup authority changed"
        )
    for name, expected in _PROVIDER_UNIVERSE_VALUES.items():
        if getattr(_provider_universe_module, name, None) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "provider-universe transitive authority is rebound: " + name
            )
    for name, expected, code in _PROVIDER_UNIVERSE_CALLABLES:
        if getattr(_provider_universe_module, name, None) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "provider-universe transitive dispatch is rebound: " + name
            )
        if getattr(expected, "__code__", None) is not code:
            raise CampaignForwardUniverseCycleBindingError(
                "provider-universe transitive dispatch code changed: " + name
            )
    for name, expected, code in _INTERNAL_CALLABLES:
        if module_globals.get(name) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle internal dispatch is rebound: " + name
            )
        if getattr(expected, "__code__", None) is not code:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle internal dispatch code changed: " + name
            )
    if (
        inspect.getattr_static(
            CollectorDeltaStore,
            "collector_cycle_observation_artifact_evidence",
        )
        is not _CANONICAL_COLLECTOR_ARTIFACT_RESOLVER
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "collector artifact resolver authority changed"
        )
    if (
        inspect.getattr_static(CompleteGameBoardEvidenceStore, "load")
        is not _CANONICAL_PROVIDER_EVIDENCE_LOADER
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "provider evidence load authority changed"
        )
    for name, expected, code in _CAPTURED_CALLABLES:
        if module_globals.get(name) is not expected:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle dispatch authority is rebound: " + name
            )
        if getattr(expected, "__code__", None) is not code:
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle dispatch code changed: " + name
            )


def _expected_cycle_receipt_payload(
    *,
    campaign,
    snapshot: CompleteGameBoardSnapshot,
    collector_evidence: dict[str, object],
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": campaign.campaign_id,
        "campaign_receipt_sha256": campaign.receipt_sha256,
        "campaign_authority_record_sha256": campaign.authority_record_sha256,
        "source_id": collector_evidence["source_id"],
        "run_id": collector_evidence["run_id"],
        "stream_epoch": collector_evidence["stream_epoch"],
        "schedule_id": collector_evidence["schedule_id"],
        "gate_binding_sha256": collector_evidence["gate_binding_sha256"],
        "cycle_seq": collector_evidence["cycle_seq"],
        "slot_ordinal": collector_evidence["slot_ordinal"],
        "artifact_id": collector_evidence["artifact_id"],
        "artifact_kind": collector_evidence["artifact_kind"],
        "provider_evidence_sha256": snapshot.evidence_sha256,
        "provider_frame_sha256": snapshot.frame_sha256,
        "provider_captured_at": snapshot.captured_at,
        "collector_artifact_evidence_sha256": collector_evidence["evidence_sha256"],
    }
    payload["receipt_sha256"] = _digest(payload)
    return payload


def _assert_cycle_receipt_exact(
    receipt: CampaignCompleteBoardCycleReceipt,
    expected: dict[str, object],
) -> None:
    if type(receipt) is not CampaignCompleteBoardCycleReceipt:
        raise TypeError(
            "cycle_receipt must be exact CampaignCompleteBoardCycleReceipt"
        )
    for name in _CYCLE_RECEIPT_FIELD_NAMES:
        if getattr(receipt, name) != expected.get(name):
            raise CampaignForwardUniverseCycleBindingError(
                "cycle receipt does not re-resolve from campaign/provider/collector "
                f"authority: {name}"
            )


def _verify_exact_provider_universe_snapshot(
    *,
    ledger: EvaluationUniverseLedger,
    snapshot: CompleteGameBoardSnapshot,
    event_lifecycle: ContinuousEventLifecycle | None,
):
    """Read-only proof that durable universe membership came from this snapshot.

    The provider-universe constructor is an issuance boundary guarded by the
    product-owned pre-evaluation semantic capability.  A verifier must never
    reissue that authority merely to compare provenance.  Instead, derive the
    canonical complete-board members from the already-authoritative snapshot and
    require the durable rows/intake metadata to match them exactly.
    """

    universe = ledger.universe
    try:
        _provider_universe_module.assert_complete_game_board_authoritative(snapshot)
        members = _provider_universe_module.complete_game_board_member_specs(
            snapshot,
            event_lifecycle=event_lifecycle,
        )
        rows = tuple(universe.rows)
        if not rows or not all(
            type(row) is _provider_universe_module.EvaluationRow for row in rows
        ):
            raise ProviderEvaluationUniverseError(
                "durable provider universe rows are not canonical EvaluationRow values"
            )
        by_key = {row.row_key: row for row in rows}
        if len(by_key) != len(rows):
            raise ProviderEvaluationUniverseError(
                "durable provider universe row_key values are not unique"
            )
        expected_keys = tuple(member.row_key for member in members)
        if tuple(sorted(by_key)) != tuple(sorted(expected_keys)):
            raise ProviderEvaluationUniverseError(
                "durable provider universe membership does not equal exact provider snapshot"
            )
        if tuple(universe.intake_snapshot.expected_row_keys) != tuple(
            sorted(expected_keys)
        ):
            raise ProviderEvaluationUniverseError(
                "durable provider intake membership does not equal exact provider snapshot"
            )
        if _instant(
            universe.intake_snapshot.committed_at,
            "durable provider intake committed_at",
        ) != _instant(snapshot.captured_at, "provider captured_at"):
            raise ProviderEvaluationUniverseError(
                "durable provider intake capture time does not equal exact provider snapshot"
            )
        if universe.intake_snapshot.source_id != snapshot.request.source_id:
            raise ProviderEvaluationUniverseError(
                "durable provider intake source does not equal exact provider snapshot"
            )
        for member in members:
            _provider_universe_module._validate_row_against_member(
                row=by_key[member.row_key],
                member=member,
                snapshot=snapshot,
                evaluation_not_before=universe.intake_snapshot.evaluation_not_before,
            )
    except ProviderEvaluationUniverseError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "durable forward universe is not derived from the exact "
            "campaign-bound provider snapshot"
        ) from exc
    return universe


def resolve_campaign_forward_universe_cycle_authority(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    collector_store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
    cycle_receipt: CampaignCompleteBoardCycleReceipt,
    provider_evidence_store: CompleteGameBoardEvidenceStore,
    universe_store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
    event_lifecycle: ContinuousEventLifecycle | None,
) -> CampaignForwardUniverseCycleAuthority:
    """Re-resolve exact campaign -> cycle -> provider snapshot -> #1185 universe."""

    module_globals = _CANONICAL_MODULE_GLOBALS
    integrity_guard = _require_dispatch_integrity
    integrity_guard_code = integrity_guard.__code__
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_datetime = datetime
    expected_timezone = timezone
    expected_module_globals = _CANONICAL_MODULE_GLOBALS
    expected_inspect = _CANONICAL_INSPECT
    expected_getattr_static = _CANONICAL_GETATTR_STATIC
    expected_getattr_static_code = _CANONICAL_GETATTR_STATIC_CODE
    expected_getattr_static_globals = _CANONICAL_GETATTR_STATIC_GLOBALS
    expected_getattr_static_global_items = _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
    expected_internal_callables = _INTERNAL_CALLABLES
    expected_captured_callables = _CAPTURED_CALLABLES
    expected_provider_callables = _PROVIDER_UNIVERSE_CALLABLES
    expected_provider_values = _PROVIDER_UNIVERSE_VALUES
    expected_provider_value_items = tuple(expected_provider_values.items())
    expected_authority_class = _CANONICAL_AUTHORITY_CLASS
    expected_authority_issuance_capability = _CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY
    expected_verification_issuance_capability = (
        _CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY
    )
    expected_authority_field_descriptors = _CANONICAL_AUTHORITY_FIELD_DESCRIPTORS
    expected_authority_issuer = _CANONICAL_AUTHORITY_ISSUER
    expected_authority_issuer_function = _CANONICAL_AUTHORITY_ISSUER_FUNCTION
    expected_authority_issuer_code = _CANONICAL_AUTHORITY_ISSUER_CODE
    expected_cycle_receipt_field_descriptors = _CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS
    expected_inception_receipt_field_descriptors = (
        _CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS
    )
    expected_artifact_kind = _CANONICAL_ARTIFACT_KIND
    expected_collector_artifact_resolver = _CANONICAL_COLLECTOR_ARTIFACT_RESOLVER
    expected_provider_evidence_loader = _CANONICAL_PROVIDER_EVIDENCE_LOADER
    expected_provider_evidence_scope = _PROVIDER_EVIDENCE_SCOPE
    expected_provider_evidence_scope_code = expected_provider_evidence_scope.__code__
    expected_provider_evidence_scope_error = _PROVIDER_EVIDENCE_SCOPE_ERROR
    expected_issued_universes = _CANONICAL_ISSUED_UNIVERSES
    expected_provider_universe_module = _provider_universe_module

    def require_stable_integrity() -> None:
        if (
            "type" in module_globals
            or "getattr" in module_globals
            or "any" in module_globals
            or "all" in module_globals
            or "len" in module_globals
            or "sorted" in module_globals
            or "tuple" in module_globals
            or "isinstance" in module_globals
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle builtin dispatch shadowed"
            )
        if (
            module_globals.get("_require_dispatch_integrity") is not integrity_guard
            or integrity_guard.__code__ is not integrity_guard_code
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle integrity guard changed mid-resolution"
            )
        if (
            module_globals.get("_CANONICAL_MODULE_GLOBALS")
            is not expected_module_globals
            or module_globals is not expected_module_globals
            or module_globals.get("inspect") is not expected_inspect
            or module_globals.get("_CANONICAL_INSPECT") is not expected_inspect
            or module_globals.get("_CANONICAL_GETATTR_STATIC")
            is not expected_getattr_static
            or module_globals.get("_CANONICAL_GETATTR_STATIC_CODE")
            is not expected_getattr_static_code
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBALS")
            is not expected_getattr_static_globals
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS")
            is not expected_getattr_static_global_items
            or expected_inspect.getattr_static is not expected_getattr_static
            or expected_getattr_static.__code__ is not expected_getattr_static_code
            or expected_getattr_static.__globals__ is not expected_getattr_static_globals
            or any(
                expected_getattr_static_globals.get(name) is not target
                or getattr(target, "__code__", None) is not code
                for name, target, code in expected_getattr_static_global_items
            )
            or module_globals.get("_INTERNAL_CALLABLES") is not expected_internal_callables
            or module_globals.get("_CAPTURED_CALLABLES") is not expected_captured_callables
            or module_globals.get("_PROVIDER_UNIVERSE_CALLABLES") is not expected_provider_callables
            or module_globals.get("_PROVIDER_UNIVERSE_VALUES") is not expected_provider_values
            or tuple(expected_provider_values.items()) != expected_provider_value_items
            or module_globals.get("_CANONICAL_AUTHORITY_CLASS") is not expected_authority_class
            or module_globals.get("_AUTHORITY_ISSUANCE_CAPABILITY")
            is not expected_authority_issuance_capability
            or module_globals.get("_VERIFICATION_ISSUANCE_CAPABILITY")
            is not expected_verification_issuance_capability
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY")
            is not expected_authority_issuance_capability
            or module_globals.get("_CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY")
            is not expected_verification_issuance_capability
            or module_globals.get("_CANONICAL_AUTHORITY_FIELD_DESCRIPTORS") is not expected_authority_field_descriptors
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER") is not expected_authority_issuer
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER_FUNCTION") is not expected_authority_issuer_function
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER_CODE") is not expected_authority_issuer_code
            or module_globals.get("_CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS") is not expected_cycle_receipt_field_descriptors
            or module_globals.get("_CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS")
            is not expected_inception_receipt_field_descriptors
            or module_globals.get("_CANONICAL_ARTIFACT_KIND") != expected_artifact_kind
            or module_globals.get("_CANONICAL_COLLECTOR_ARTIFACT_RESOLVER") is not expected_collector_artifact_resolver
            or module_globals.get("_CANONICAL_PROVIDER_EVIDENCE_LOADER") is not expected_provider_evidence_loader
            or module_globals.get("_PROVIDER_EVIDENCE_SCOPE")
            is not expected_provider_evidence_scope
            or expected_provider_evidence_scope.__code__
            is not expected_provider_evidence_scope_code
            or module_globals.get("_PROVIDER_EVIDENCE_SCOPE_ERROR")
            is not expected_provider_evidence_scope_error
            or module_globals.get("CampaignProviderCycleCaptureIntegrityError")
            is not expected_provider_evidence_scope_error
            or module_globals.get("_CANONICAL_ISSUED_UNIVERSES") is not expected_issued_universes
            or module_globals.get("_provider_universe_module") is not expected_provider_universe_module
            or expected_provider_universe_module._ISSUED_UNIVERSES is not expected_issued_universes
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle authority witness changed mid-resolution"
            )
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256
            or module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or module_globals.get("datetime") is not expected_datetime
            or module_globals.get("timezone") is not expected_timezone
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle chronology/digest primitives changed"
            )
        integrity_guard()
        if (
            module_globals.get("_PROVIDER_EVIDENCE_SCOPE")
            is not expected_provider_evidence_scope
            or expected_provider_evidence_scope.__code__
            is not expected_provider_evidence_scope_code
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "provider evidence routing guard changed"
            )
        try:
            expected_provider_evidence_scope(
                precommit_locator,
                provider_evidence_store,
            )
        except expected_provider_evidence_scope_error as exc:
            raise CampaignForwardUniverseCycleBindingError(
                "provider evidence routing does not match campaign precommit authority"
            ) from exc

    require_stable_integrity()
    if type(precommit_locator) is not ForwardUniversePrecommitLocator:
        raise TypeError("precommit_locator must be exact ForwardUniversePrecommitLocator")
    if type(collector_store) is not CollectorDeltaStore:
        raise TypeError("collector_store must be exact CollectorDeltaStore")
    if type(source_spec) is not CampaignInceptionSourceSpec:
        raise TypeError("source_spec must be exact CampaignInceptionSourceSpec")
    if type(provider_evidence_store) is not CompleteGameBoardEvidenceStore:
        raise TypeError(
            "provider_evidence_store must be exact CompleteGameBoardEvidenceStore"
        )
    if type(universe_store) is not ProviderEvaluationUniverseStore:
        raise TypeError("universe_store must be exact ProviderEvaluationUniverseStore")
    if type(protocol) is not ForwardEvidenceProtocolEnvelope:
        raise TypeError("protocol must be exact ForwardEvidenceProtocolEnvelope")
    if event_lifecycle is not None and type(event_lifecycle) is not ContinuousEventLifecycle:
        raise TypeError(
            "event_lifecycle must be exact ContinuousEventLifecycle or None"
        )
    if type(cycle_receipt) is not CampaignCompleteBoardCycleReceipt:
        raise TypeError(
            "cycle_receipt must be exact CampaignCompleteBoardCycleReceipt"
        )

    campaign = _ESTABLISH_CAMPAIGN(
        precommit_locator=precommit_locator,
        store=collector_store,
        source_spec=source_spec,
    )
    require_stable_integrity()

    collector_evidence = _RESOLVE_ARTIFACT(
        collector_store,
        source_id=cycle_receipt.source_id,
        cycle_seq=cycle_receipt.cycle_seq,
        artifact_kind=ARTIFACT_KIND,
        artifact_sha256=cycle_receipt.provider_evidence_sha256,
    )
    if type(collector_evidence) is not dict:
        raise CampaignForwardUniverseCycleBindingError(
            "collector artifact resolver returned noncanonical evidence"
        )
    require_stable_integrity()
    universe_slot_ordinal = collector_evidence.get("slot_ordinal")
    if (
        type(universe_slot_ordinal) is not int
        or universe_slot_ordinal != source_spec.evaluation_start_slot_ordinal
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "forward universe must derive from the first precommitted evaluation slot"
        )

    snapshot = _LOAD_PROVIDER_EVIDENCE(
        provider_evidence_store,
        cycle_receipt.provider_evidence_sha256,
    )
    if type(snapshot) is not CompleteGameBoardSnapshot:
        raise CampaignForwardUniverseCycleBindingError(
            "provider evidence resolver returned noncanonical snapshot"
        )
    require_stable_integrity()
    _require_cycle_observation_chronology(
        campaign=campaign,
        source_spec=source_spec,
        snapshot=snapshot,
        collector_evidence=collector_evidence,
    )
    require_stable_integrity()

    expected_receipt = _expected_cycle_receipt_payload(
        campaign=campaign,
        snapshot=snapshot,
        collector_evidence=collector_evidence,
    )
    _assert_cycle_receipt_exact(cycle_receipt, expected_receipt)

    if (
        collector_evidence.get("authorization_sha256")
        != campaign.authority_record_sha256
        or cycle_receipt.source_id != campaign.source_id
        or cycle_receipt.source_id != source_spec.source_id
        or cycle_receipt.source_id != snapshot.request.source_id
        or cycle_receipt.source_id != universe_store.source_id
        or cycle_receipt.campaign_id != campaign.campaign_id
        or cycle_receipt.campaign_id != protocol.campaign_id
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "campaign/cycle/provider/universe source or campaign identity conflicts"
        )

    try:
        ledger, _backing = _GUARDED_UNIVERSE_LOAD(universe_store)
    except ForwardUniverseBackingGuardError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "durable provider universe backing authority cannot be resolved"
        ) from exc
    if type(ledger) is not EvaluationUniverseLedger:
        raise CampaignForwardUniverseCycleBindingError(
            "durable provider evaluation universe is unavailable"
        )
    require_stable_integrity()

    universe = _verify_exact_provider_universe_snapshot(
        ledger=ledger,
        snapshot=snapshot,
        event_lifecycle=event_lifecycle,
    )
    if (
        universe.campaign_id != campaign.campaign_id
        or universe.campaign_id != protocol.campaign_id
        or universe.intake_snapshot.source_id != cycle_receipt.source_id
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "cycle-derived provider universe crosses campaign or source authority"
        )

    try:
        forward_identity = _RESOLVE_FORWARD_IDENTITY(
            store=universe_store,
            protocol=protocol,
            precommit=precommit_locator,
        )
    except ForwardEvaluationUniverseBindingError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "forward universe authority cannot be re-resolved"
        ) from exc
    if type(forward_identity) is not ForwardUniverseAuthorityIdentity:
        raise CampaignForwardUniverseCycleBindingError(
            "forward universe resolver returned noncanonical authority identity"
        )
    if (
        forward_identity.prospective_evaluation_plan_sha256
        != campaign.evaluation_universe_sha256
        or forward_identity.universe_sha256 != universe.universe_sha256
        or forward_identity.membership_sha256 != universe.membership_sha256
        or forward_identity.member_count != len(universe.rows)
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "prospective plan or realized forward universe identity conflicts "
            "with campaign-cycle authority"
        )

    require_stable_integrity()
    return _CANONICAL_AUTHORITY_ISSUER_FUNCTION(
        _CANONICAL_AUTHORITY_CLASS,
        _issuance_capability=_CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY,
        campaign_id=campaign.campaign_id,
        source_id=cycle_receipt.source_id,
        cycle_receipt_sha256=cycle_receipt.receipt_sha256,
        campaign_receipt_sha256=campaign.receipt_sha256,
        provider_evidence_sha256=snapshot.evidence_sha256,
        provider_frame_sha256=snapshot.frame_sha256,
        collector_artifact_evidence_sha256=collector_evidence["evidence_sha256"],
        forward_identity=forward_identity,
    )


def authorize_campaign_forward_source_receipts(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    collector_store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
    cycle_receipt: CampaignCompleteBoardCycleReceipt,
    provider_evidence_store: CompleteGameBoardEvidenceStore,
    universe_store: ProviderEvaluationUniverseStore,
    protocol: ForwardEvidenceProtocolEnvelope,
    event_lifecycle: ContinuousEventLifecycle | None,
    opportunities: Sequence[ForwardOpportunityEnvelope],
) -> tuple[AuthoritativeSourceReceipt, ...]:
    """Authorize #1185 receipts only while the cycle-bound provider authority is stable."""

    module_globals = _CANONICAL_MODULE_GLOBALS
    integrity_guard = _require_dispatch_integrity
    integrity_guard_code = integrity_guard.__code__
    resolve_authority = resolve_campaign_forward_universe_cycle_authority
    resolve_authority_code = resolve_authority.__code__
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_datetime = datetime
    expected_timezone = timezone
    expected_module_globals = _CANONICAL_MODULE_GLOBALS
    expected_inspect = _CANONICAL_INSPECT
    expected_getattr_static = _CANONICAL_GETATTR_STATIC
    expected_getattr_static_code = _CANONICAL_GETATTR_STATIC_CODE
    expected_getattr_static_globals = _CANONICAL_GETATTR_STATIC_GLOBALS
    expected_getattr_static_global_items = _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
    expected_internal_callables = _INTERNAL_CALLABLES
    expected_captured_callables = _CAPTURED_CALLABLES
    expected_provider_callables = _PROVIDER_UNIVERSE_CALLABLES
    expected_provider_values = _PROVIDER_UNIVERSE_VALUES
    expected_provider_value_items = tuple(expected_provider_values.items())
    expected_authority_class = _CANONICAL_AUTHORITY_CLASS
    expected_authority_issuance_capability = _CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY
    expected_verification_issuance_capability = (
        _CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY
    )
    expected_authority_field_descriptors = _CANONICAL_AUTHORITY_FIELD_DESCRIPTORS
    expected_authority_issuer = _CANONICAL_AUTHORITY_ISSUER
    expected_authority_issuer_function = _CANONICAL_AUTHORITY_ISSUER_FUNCTION
    expected_authority_issuer_code = _CANONICAL_AUTHORITY_ISSUER_CODE
    expected_cycle_receipt_field_descriptors = _CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS
    expected_inception_receipt_field_descriptors = (
        _CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS
    )
    expected_artifact_kind = _CANONICAL_ARTIFACT_KIND
    expected_collector_artifact_resolver = _CANONICAL_COLLECTOR_ARTIFACT_RESOLVER
    expected_provider_evidence_loader = _CANONICAL_PROVIDER_EVIDENCE_LOADER
    expected_provider_evidence_scope = _PROVIDER_EVIDENCE_SCOPE
    expected_provider_evidence_scope_code = expected_provider_evidence_scope.__code__
    expected_provider_evidence_scope_error = _PROVIDER_EVIDENCE_SCOPE_ERROR
    expected_issued_universes = _CANONICAL_ISSUED_UNIVERSES
    expected_provider_universe_module = _provider_universe_module

    def require_stable_authorization_dispatch() -> None:
        if (
            "type" in module_globals
            or "getattr" in module_globals
            or "any" in module_globals
            or "all" in module_globals
            or "len" in module_globals
            or "sorted" in module_globals
            or "tuple" in module_globals
            or "isinstance" in module_globals
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle builtin dispatch shadowed"
            )
        if (
            module_globals.get("_require_dispatch_integrity") is not integrity_guard
            or integrity_guard.__code__ is not integrity_guard_code
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle integrity guard changed during authorization"
            )
        if (
            module_globals.get("resolve_campaign_forward_universe_cycle_authority")
            is not resolve_authority
            or resolve_authority.__code__ is not resolve_authority_code
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle resolver changed during authorization"
            )
        if (
            module_globals.get("_CANONICAL_MODULE_GLOBALS")
            is not expected_module_globals
            or module_globals is not expected_module_globals
            or module_globals.get("inspect") is not expected_inspect
            or module_globals.get("_CANONICAL_INSPECT") is not expected_inspect
            or module_globals.get("_CANONICAL_GETATTR_STATIC")
            is not expected_getattr_static
            or module_globals.get("_CANONICAL_GETATTR_STATIC_CODE")
            is not expected_getattr_static_code
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBALS")
            is not expected_getattr_static_globals
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS")
            is not expected_getattr_static_global_items
            or expected_inspect.getattr_static is not expected_getattr_static
            or expected_getattr_static.__code__ is not expected_getattr_static_code
            or expected_getattr_static.__globals__ is not expected_getattr_static_globals
            or any(
                expected_getattr_static_globals.get(name) is not target
                or getattr(target, "__code__", None) is not code
                for name, target, code in expected_getattr_static_global_items
            )
            or module_globals.get("_INTERNAL_CALLABLES") is not expected_internal_callables
            or module_globals.get("_CAPTURED_CALLABLES") is not expected_captured_callables
            or module_globals.get("_PROVIDER_UNIVERSE_CALLABLES") is not expected_provider_callables
            or module_globals.get("_PROVIDER_UNIVERSE_VALUES") is not expected_provider_values
            or tuple(expected_provider_values.items()) != expected_provider_value_items
            or module_globals.get("_CANONICAL_AUTHORITY_CLASS") is not expected_authority_class
            or module_globals.get("_AUTHORITY_ISSUANCE_CAPABILITY")
            is not expected_authority_issuance_capability
            or module_globals.get("_VERIFICATION_ISSUANCE_CAPABILITY")
            is not expected_verification_issuance_capability
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY")
            is not expected_authority_issuance_capability
            or module_globals.get("_CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY")
            is not expected_verification_issuance_capability
            or module_globals.get("_CANONICAL_AUTHORITY_FIELD_DESCRIPTORS") is not expected_authority_field_descriptors
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER") is not expected_authority_issuer
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER_FUNCTION") is not expected_authority_issuer_function
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER_CODE") is not expected_authority_issuer_code
            or module_globals.get("_CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS") is not expected_cycle_receipt_field_descriptors
            or module_globals.get("_CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS")
            is not expected_inception_receipt_field_descriptors
            or module_globals.get("_CANONICAL_ARTIFACT_KIND") != expected_artifact_kind
            or module_globals.get("_CANONICAL_COLLECTOR_ARTIFACT_RESOLVER") is not expected_collector_artifact_resolver
            or module_globals.get("_CANONICAL_PROVIDER_EVIDENCE_LOADER") is not expected_provider_evidence_loader
            or module_globals.get("_PROVIDER_EVIDENCE_SCOPE")
            is not expected_provider_evidence_scope
            or expected_provider_evidence_scope.__code__
            is not expected_provider_evidence_scope_code
            or module_globals.get("_PROVIDER_EVIDENCE_SCOPE_ERROR")
            is not expected_provider_evidence_scope_error
            or module_globals.get("CampaignProviderCycleCaptureIntegrityError")
            is not expected_provider_evidence_scope_error
            or module_globals.get("_CANONICAL_ISSUED_UNIVERSES") is not expected_issued_universes
            or module_globals.get("_provider_universe_module") is not expected_provider_universe_module
            or expected_provider_universe_module._ISSUED_UNIVERSES is not expected_issued_universes
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle authority witness changed during authorization"
            )
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256
            or module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or module_globals.get("datetime") is not expected_datetime
            or module_globals.get("timezone") is not expected_timezone
        ):
            raise CampaignForwardUniverseCycleBindingError(
                "campaign forward-cycle chronology/digest primitives changed"
            )
        integrity_guard()

    require_stable_authorization_dispatch()
    before = resolve_authority(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
    )
    require_stable_authorization_dispatch()
    try:
        receipts = _AUTHORIZE_FORWARD_RECEIPTS(
            store=universe_store,
            protocol=protocol,
            precommit=precommit_locator,
            opportunities=opportunities,
        )
    except ForwardEvaluationUniverseBindingError as exc:
        raise CampaignForwardUniverseCycleBindingError(
            "forward source receipt authorization failed"
        ) from exc
    require_stable_authorization_dispatch()
    after = resolve_authority(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
    )
    if after != before:
        raise CampaignForwardUniverseCycleBindingError(
            "campaign/cycle/provider/universe authority changed during authorization"
        )
    if not isinstance(receipts, tuple) or not all(
        type(receipt) is AuthoritativeSourceReceipt for receipt in receipts
    ):
        raise CampaignForwardUniverseCycleBindingError(
            "forward source authorization returned noncanonical receipts"
        )
    return receipts


def verify_campaign_forward_evidence(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    collector_store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
    cycle_receipt: CampaignCompleteBoardCycleReceipt,
    provider_evidence_store: CompleteGameBoardEvidenceStore,
    universe_store: ProviderEvaluationUniverseStore,
    event_lifecycle: ContinuousEventLifecycle | None,
    evidence: CampaignEvidence,
) -> CampaignForwardEvidenceVerification:
    """Run structural #879 verification only over cycle-authorized source receipts.

    This is composition, not promotion authority. Caller-supplied authoritative
    receipts are replaced by receipts re-issued from the exact campaign/cycle/provider
    universe before the structural verifier is invoked.
    """

    campaign_evidence_type = _CANONICAL_CAMPAIGN_EVIDENCE
    verification_result_type = _CANONICAL_VERIFICATION_RESULT
    if type(evidence) is not campaign_evidence_type:
        raise TypeError("evidence must be exact CampaignEvidence")

    protocol = evidence.protocol
    before = resolve_campaign_forward_universe_cycle_authority(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
    )
    receipts = authorize_campaign_forward_source_receipts(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
        opportunities=evidence.opportunities,
    )
    canonical_evidence = campaign_evidence_type(
        protocol=evidence.protocol,
        opportunities=evidence.opportunities,
        cohort_roots=evidence.cohort_roots,
        closes=evidence.closes,
        reveal_boundaries=evidence.reveal_boundaries,
        authoritative_receipts=receipts,
        denominator_sequences=evidence.denominator_sequences,
        cost_evidence=evidence.cost_evidence,
        safety_margin=evidence.safety_margin,
        stopping_rule_satisfied=evidence.stopping_rule_satisfied,
    )
    result = _VERIFY_FORWARD_EVIDENCE(canonical_evidence)
    if type(result) is not verification_result_type:
        raise CampaignForwardUniverseCycleBindingError(
            "structural forward verifier returned noncanonical result"
        )
    after = resolve_campaign_forward_universe_cycle_authority(
        precommit_locator=precommit_locator,
        collector_store=collector_store,
        source_spec=source_spec,
        cycle_receipt=cycle_receipt,
        provider_evidence_store=provider_evidence_store,
        universe_store=universe_store,
        protocol=protocol,
        event_lifecycle=event_lifecycle,
    )
    if after != before:
        raise CampaignForwardUniverseCycleBindingError(
            "campaign/cycle/provider/universe authority changed during structural verification"
        )
    _require_dispatch_integrity()
    receipt = _CANONICAL_VERIFICATION_ISSUER_FUNCTION(
        _CANONICAL_VERIFICATION_CLASS,
        _issuance_capability=_VERIFICATION_ISSUANCE_CAPABILITY,
        authority=after,
        structural_result=result,
    )
    _require_dispatch_integrity()
    if type(receipt) is not _CANONICAL_VERIFICATION_CLASS:
        raise CampaignForwardUniverseCycleBindingError(
            "composed forward verification returned noncanonical receipt"
        )
    return receipt


_INTERNAL_CALLABLES = tuple(
    (name, target, getattr(target, "__code__", None))
    for name, target in (
        ("_text", _text),
        ("_sha", _sha),
        ("_digest", _digest),
        ("_instant", _instant),
        (
            "_require_cycle_observation_chronology",
            _require_cycle_observation_chronology,
        ),
        (
            "_expected_cycle_receipt_payload",
            _expected_cycle_receipt_payload,
        ),
        ("_assert_cycle_receipt_exact", _assert_cycle_receipt_exact),
        ("_verify_exact_provider_universe_snapshot", _verify_exact_provider_universe_snapshot),
    )
)


def _seal_campaign_forward_universe_cycle_dispatch() -> None:
    """Seal exported composite authority paths around the private integrity guard."""

    module_globals = _CANONICAL_MODULE_GLOBALS
    expected_error = CampaignForwardUniverseCycleBindingError
    expected_guard = _require_dispatch_integrity
    expected_guard_code = expected_guard.__code__
    expected_resolve = resolve_campaign_forward_universe_cycle_authority
    expected_resolve_code = expected_resolve.__code__
    expected_authorize = authorize_campaign_forward_source_receipts
    expected_authorize_code = expected_authorize.__code__
    expected_verify = verify_campaign_forward_evidence
    expected_verify_code = expected_verify.__code__
    expected_campaign_evidence_type = CampaignEvidence
    expected_verification_result_type = VerificationResult
    expected_canonical_campaign_evidence = _CANONICAL_CAMPAIGN_EVIDENCE
    expected_canonical_verification_result = _CANONICAL_VERIFICATION_RESULT
    expected_canonical_verification_code = _CANONICAL_VERIFICATION_CODE
    expected_campaign_evidence_fields = _CANONICAL_CAMPAIGN_EVIDENCE_DATACLASS_FIELDS
    expected_campaign_evidence_field_items = _CANONICAL_CAMPAIGN_EVIDENCE_FIELD_ITEMS
    expected_campaign_evidence_field_descriptors = (
        _CANONICAL_CAMPAIGN_EVIDENCE_FIELD_DESCRIPTORS
    )
    expected_campaign_evidence_init = _CANONICAL_CAMPAIGN_EVIDENCE_INIT
    expected_campaign_evidence_init_code = _CANONICAL_CAMPAIGN_EVIDENCE_INIT_CODE
    expected_verification_result_fields = _CANONICAL_VERIFICATION_RESULT_DATACLASS_FIELDS
    expected_verification_result_field_items = _CANONICAL_VERIFICATION_RESULT_FIELD_ITEMS
    expected_verification_result_field_descriptors = (
        _CANONICAL_VERIFICATION_RESULT_FIELD_DESCRIPTORS
    )
    expected_module_globals = _CANONICAL_MODULE_GLOBALS
    expected_inspect = _CANONICAL_INSPECT
    expected_getattr_static = _CANONICAL_GETATTR_STATIC
    expected_getattr_static_code = _CANONICAL_GETATTR_STATIC_CODE
    expected_getattr_static_globals = _CANONICAL_GETATTR_STATIC_GLOBALS
    expected_getattr_static_global_items = _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_datetime = datetime
    expected_timezone = timezone
    expected_authority_class = _CANONICAL_AUTHORITY_CLASS
    expected_authority_issuance_capability = _CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY
    expected_verification_issuance_capability = (
        _CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY
    )
    expected_authority_field_descriptors = _CANONICAL_AUTHORITY_FIELD_DESCRIPTORS
    expected_authority_issuer = _CANONICAL_AUTHORITY_ISSUER
    expected_authority_issuer_function = _CANONICAL_AUTHORITY_ISSUER_FUNCTION
    expected_authority_issuer_code = _CANONICAL_AUTHORITY_ISSUER_CODE
    expected_verification_class = _CANONICAL_VERIFICATION_CLASS
    expected_verification_field_descriptors = _CANONICAL_VERIFICATION_FIELD_DESCRIPTORS
    expected_verification_issuer = _CANONICAL_VERIFICATION_ISSUER
    expected_verification_issuer_function = _CANONICAL_VERIFICATION_ISSUER_FUNCTION
    expected_verification_issuer_code = _CANONICAL_VERIFICATION_ISSUER_CODE
    expected_verification_domain = _COMPOSED_VERIFICATION_DOMAIN
    expected_verification_scope = _COMPOSED_VERIFICATION_SCOPE
    expected_cycle_receipt_field_descriptors = _CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS
    expected_inception_receipt_field_descriptors = (
        _CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS
    )
    expected_artifact_kind = _CANONICAL_ARTIFACT_KIND
    expected_collector_artifact_resolver = _CANONICAL_COLLECTOR_ARTIFACT_RESOLVER
    expected_provider_evidence_loader = _CANONICAL_PROVIDER_EVIDENCE_LOADER
    expected_provider_evidence_scope = _PROVIDER_EVIDENCE_SCOPE
    expected_provider_evidence_scope_code = expected_provider_evidence_scope.__code__
    expected_provider_evidence_scope_error = _PROVIDER_EVIDENCE_SCOPE_ERROR
    expected_issued_universes = _CANONICAL_ISSUED_UNIVERSES
    expected_provider_universe_module = _provider_universe_module
    expected_internal_callables = _INTERNAL_CALLABLES
    expected_captured_callables = _CAPTURED_CALLABLES
    expected_provider_callables = _PROVIDER_UNIVERSE_CALLABLES
    expected_provider_values = _PROVIDER_UNIVERSE_VALUES
    expected_provider_value_items = tuple(expected_provider_values.items())

    def require_sealed_surface() -> None:
        if (
            "type" in module_globals
            or "getattr" in module_globals
            or "any" in module_globals
            or "all" in module_globals
            or "len" in module_globals
            or "sorted" in module_globals
            or "tuple" in module_globals
            or "isinstance" in module_globals
        ):
            raise expected_error(
                "campaign forward-cycle builtin dispatch shadowed"
            )
        if (
            module_globals.get("_require_dispatch_integrity") is not expected_guard
            or expected_guard.__code__ is not expected_guard_code
        ):
            raise expected_error(
                "campaign forward-cycle integrity guard changed"
            )
        if (
            module_globals.get("_CANONICAL_MODULE_GLOBALS")
            is not expected_module_globals
            or module_globals is not expected_module_globals
            or module_globals.get("inspect") is not expected_inspect
            or module_globals.get("_CANONICAL_INSPECT") is not expected_inspect
            or module_globals.get("_CANONICAL_GETATTR_STATIC")
            is not expected_getattr_static
            or module_globals.get("_CANONICAL_GETATTR_STATIC_CODE")
            is not expected_getattr_static_code
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBALS")
            is not expected_getattr_static_globals
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS")
            is not expected_getattr_static_global_items
            or expected_inspect.getattr_static is not expected_getattr_static
            or expected_getattr_static.__code__ is not expected_getattr_static_code
            or expected_getattr_static.__globals__ is not expected_getattr_static_globals
            or any(
                expected_getattr_static_globals.get(name) is not target
                or getattr(target, "__code__", None) is not code
                for name, target, code in expected_getattr_static_global_items
            )
        ):
            raise expected_error(
                "campaign forward-cycle reflection dispatch changed"
            )
        if (
            module_globals.get("_CANONICAL_AUTHORITY_CLASS") is not expected_authority_class
            or module_globals.get("_AUTHORITY_ISSUANCE_CAPABILITY")
            is not expected_authority_issuance_capability
            or module_globals.get("_VERIFICATION_ISSUANCE_CAPABILITY")
            is not expected_verification_issuance_capability
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUANCE_CAPABILITY")
            is not expected_authority_issuance_capability
            or module_globals.get("_CANONICAL_VERIFICATION_ISSUANCE_CAPABILITY")
            is not expected_verification_issuance_capability
            or module_globals.get("_CANONICAL_AUTHORITY_FIELD_DESCRIPTORS") is not expected_authority_field_descriptors
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER") is not expected_authority_issuer
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER_FUNCTION") is not expected_authority_issuer_function
            or module_globals.get("_CANONICAL_AUTHORITY_ISSUER_CODE") is not expected_authority_issuer_code
            or module_globals.get("_CANONICAL_VERIFICATION_CLASS")
            is not expected_verification_class
            or module_globals.get("_CANONICAL_VERIFICATION_FIELD_DESCRIPTORS")
            is not expected_verification_field_descriptors
            or module_globals.get("_CANONICAL_VERIFICATION_ISSUER")
            is not expected_verification_issuer
            or module_globals.get("_CANONICAL_VERIFICATION_ISSUER_FUNCTION")
            is not expected_verification_issuer_function
            or module_globals.get("_CANONICAL_VERIFICATION_ISSUER_CODE")
            is not expected_verification_issuer_code
            or module_globals.get("_COMPOSED_VERIFICATION_DOMAIN")
            != expected_verification_domain
            or module_globals.get("_COMPOSED_VERIFICATION_SCOPE")
            != expected_verification_scope
            or module_globals.get("_CANONICAL_CYCLE_RECEIPT_FIELD_DESCRIPTORS") is not expected_cycle_receipt_field_descriptors
            or module_globals.get("_CANONICAL_INCEPTION_RECEIPT_FIELD_DESCRIPTORS")
            is not expected_inception_receipt_field_descriptors
            or module_globals.get("_CANONICAL_ARTIFACT_KIND") != expected_artifact_kind
            or module_globals.get("_CANONICAL_COLLECTOR_ARTIFACT_RESOLVER") is not expected_collector_artifact_resolver
            or module_globals.get("_CANONICAL_PROVIDER_EVIDENCE_LOADER") is not expected_provider_evidence_loader
            or module_globals.get("_PROVIDER_EVIDENCE_SCOPE")
            is not expected_provider_evidence_scope
            or expected_provider_evidence_scope.__code__
            is not expected_provider_evidence_scope_code
            or module_globals.get("_PROVIDER_EVIDENCE_SCOPE_ERROR")
            is not expected_provider_evidence_scope_error
            or module_globals.get("CampaignProviderCycleCaptureIntegrityError")
            is not expected_provider_evidence_scope_error
            or module_globals.get("_CANONICAL_ISSUED_UNIVERSES") is not expected_issued_universes
            or module_globals.get("_provider_universe_module") is not expected_provider_universe_module
            or expected_provider_universe_module._ISSUED_UNIVERSES is not expected_issued_universes
        ):
            raise expected_error(
                "campaign forward-cycle authority witness globals changed"
            )
        if (
            module_globals.get("_INTERNAL_CALLABLES") is not expected_internal_callables
            or module_globals.get("_CAPTURED_CALLABLES")
            is not expected_captured_callables
            or module_globals.get("_PROVIDER_UNIVERSE_CALLABLES")
            is not expected_provider_callables
            or module_globals.get("_PROVIDER_UNIVERSE_VALUES")
            is not expected_provider_values
            or tuple(expected_provider_values.items())
            != expected_provider_value_items
        ):
            raise expected_error(
                "campaign forward-cycle witness tables changed"
            )
        if (
            module_globals.get("CampaignEvidence") is not expected_campaign_evidence_type
            or module_globals.get("VerificationResult") is not expected_verification_result_type
            or module_globals.get("_CANONICAL_CAMPAIGN_EVIDENCE")
            is not expected_canonical_campaign_evidence
            or module_globals.get("_CANONICAL_VERIFICATION_RESULT")
            is not expected_canonical_verification_result
            or module_globals.get("VerificationCode")
            is not expected_canonical_verification_code
            or module_globals.get("_CANONICAL_VERIFICATION_CODE")
            is not expected_canonical_verification_code
            or module_globals.get("_CANONICAL_CAMPAIGN_EVIDENCE_DATACLASS_FIELDS")
            is not expected_campaign_evidence_fields
            or CampaignEvidence.__dataclass_fields__ is not expected_campaign_evidence_fields
            or tuple(expected_campaign_evidence_fields.items())
            != expected_campaign_evidence_field_items
            or module_globals.get("_CANONICAL_CAMPAIGN_EVIDENCE_FIELD_ITEMS")
            is not expected_campaign_evidence_field_items
            or module_globals.get("_CANONICAL_CAMPAIGN_EVIDENCE_FIELD_DESCRIPTORS")
            is not expected_campaign_evidence_field_descriptors
            or any(
                expected_getattr_static(CampaignEvidence, name) is not descriptor
                for name, descriptor in expected_campaign_evidence_field_descriptors
            )
            or module_globals.get("_CANONICAL_CAMPAIGN_EVIDENCE_INIT")
            is not expected_campaign_evidence_init
            or module_globals.get("_CANONICAL_CAMPAIGN_EVIDENCE_INIT_CODE")
            is not expected_campaign_evidence_init_code
            or expected_getattr_static(CampaignEvidence, "__init__")
            is not expected_campaign_evidence_init
            or getattr(expected_campaign_evidence_init, "__code__", None)
            is not expected_campaign_evidence_init_code
            or module_globals.get("_CANONICAL_VERIFICATION_RESULT_DATACLASS_FIELDS")
            is not expected_verification_result_fields
            or VerificationResult.__dataclass_fields__
            is not expected_verification_result_fields
            or tuple(expected_verification_result_fields.items())
            != expected_verification_result_field_items
            or module_globals.get("_CANONICAL_VERIFICATION_RESULT_FIELD_ITEMS")
            is not expected_verification_result_field_items
            or module_globals.get("_CANONICAL_VERIFICATION_RESULT_FIELD_DESCRIPTORS")
            is not expected_verification_result_field_descriptors
            or any(
                expected_getattr_static(VerificationResult, name) is not descriptor
                for name, descriptor in expected_verification_result_field_descriptors
            )
        ):
            raise expected_error(
                "campaign forward-cycle structural evidence types changed"
            )
        if (
            module_globals.get("_CANONICAL_MODULE_GLOBALS")
            is not expected_module_globals
            or module_globals is not expected_module_globals
            or module_globals.get("inspect") is not expected_inspect
            or module_globals.get("_CANONICAL_INSPECT") is not expected_inspect
            or module_globals.get("_CANONICAL_GETATTR_STATIC")
            is not expected_getattr_static
            or module_globals.get("_CANONICAL_GETATTR_STATIC_CODE")
            is not expected_getattr_static_code
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBALS")
            is not expected_getattr_static_globals
            or module_globals.get("_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS")
            is not expected_getattr_static_global_items
            or expected_inspect.getattr_static is not expected_getattr_static
            or expected_getattr_static.__code__ is not expected_getattr_static_code
            or expected_getattr_static.__globals__ is not expected_getattr_static_globals
            or any(
                expected_getattr_static_globals.get(name) is not target
                or getattr(target, "__code__", None) is not code
                for name, target, code in expected_getattr_static_global_items
            )
        ):
            raise expected_error(
                "campaign forward-cycle reflection dispatch changed"
            )
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256
            or module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or module_globals.get("datetime") is not expected_datetime
            or module_globals.get("timezone") is not expected_timezone
        ):
            raise expected_error(
                "campaign forward-cycle chronology/digest primitives changed"
            )
        if expected_resolve.__code__ is not expected_resolve_code:
            raise expected_error(
                "campaign forward-cycle resolver implementation changed"
            )
        if expected_authorize.__code__ is not expected_authorize_code:
            raise expected_error(
                "campaign forward-cycle authorizer implementation changed"
            )
        if expected_verify.__code__ is not expected_verify_code:
            raise expected_error(
                "campaign forward-cycle structural verifier implementation changed"
            )
        expected_guard()

    def require_public_resolver_surface() -> None:
        if (
            module_globals.get(
                "resolve_campaign_forward_universe_cycle_authority"
            )
            is not sealed_resolve_campaign_forward_universe_cycle_authority
        ):
            raise expected_error(
                "campaign forward-cycle resolver surface changed"
            )

    def require_public_authority_surface() -> None:
        if (
            module_globals.get("authorize_campaign_forward_source_receipts")
            is not sealed_authorize_campaign_forward_source_receipts
            or module_globals.get(
                "resolve_campaign_forward_universe_cycle_authority"
            )
            is not sealed_resolve_campaign_forward_universe_cycle_authority
        ):
            raise expected_error(
                "campaign forward-cycle public authority surface changed"
            )

    def require_public_verification_surface() -> None:
        if (
            module_globals.get("verify_campaign_forward_evidence")
            is not sealed_verify_campaign_forward_evidence
            or module_globals.get("authorize_campaign_forward_source_receipts")
            is not sealed_authorize_campaign_forward_source_receipts
            or module_globals.get(
                "resolve_campaign_forward_universe_cycle_authority"
            )
            is not sealed_resolve_campaign_forward_universe_cycle_authority
        ):
            raise expected_error(
                "campaign forward-cycle public verification surface changed"
            )

    def sealed_resolve_campaign_forward_universe_cycle_authority(*args, **kwargs):
        require_public_resolver_surface()
        require_sealed_surface()
        result = expected_resolve(*args, **kwargs)
        require_sealed_surface()
        require_public_resolver_surface()
        return result

    def sealed_authorize_campaign_forward_source_receipts(*args, **kwargs):
        require_public_authority_surface()
        require_sealed_surface()
        result = expected_authorize(*args, **kwargs)
        require_sealed_surface()
        require_public_authority_surface()
        return result

    def sealed_verify_campaign_forward_evidence(*args, **kwargs):
        require_public_verification_surface()
        require_sealed_surface()
        result = expected_verify(*args, **kwargs)
        require_sealed_surface()
        require_public_verification_surface()
        return result

    sealed_resolve_campaign_forward_universe_cycle_authority.__name__ = (
        expected_resolve.__name__
    )
    sealed_resolve_campaign_forward_universe_cycle_authority.__qualname__ = (
        expected_resolve.__qualname__
    )
    sealed_resolve_campaign_forward_universe_cycle_authority.__doc__ = (
        expected_resolve.__doc__
    )
    sealed_authorize_campaign_forward_source_receipts.__name__ = (
        expected_authorize.__name__
    )
    sealed_authorize_campaign_forward_source_receipts.__qualname__ = (
        expected_authorize.__qualname__
    )
    sealed_authorize_campaign_forward_source_receipts.__doc__ = (
        expected_authorize.__doc__
    )
    sealed_verify_campaign_forward_evidence.__name__ = expected_verify.__name__
    sealed_verify_campaign_forward_evidence.__qualname__ = expected_verify.__qualname__
    sealed_verify_campaign_forward_evidence.__doc__ = expected_verify.__doc__
    for sealed in (
        sealed_resolve_campaign_forward_universe_cycle_authority,
        sealed_authorize_campaign_forward_source_receipts,
        sealed_verify_campaign_forward_evidence,
    ):
        if hasattr(sealed, "__wrapped__"):
            raise RuntimeError(
                "campaign forward-cycle seal must not expose unsealed delegate"
            )
    module_globals["resolve_campaign_forward_universe_cycle_authority"] = (
        sealed_resolve_campaign_forward_universe_cycle_authority
    )
    module_globals["authorize_campaign_forward_source_receipts"] = (
        sealed_authorize_campaign_forward_source_receipts
    )
    module_globals["verify_campaign_forward_evidence"] = (
        sealed_verify_campaign_forward_evidence
    )


_seal_campaign_forward_universe_cycle_dispatch()


__all__ = [
    "CampaignForwardEvidenceVerification",
    "CampaignForwardUniverseCycleAuthority",
    "CampaignForwardUniverseCycleBindingError",
    "authorize_campaign_forward_source_receipts",
    "resolve_campaign_forward_universe_cycle_authority",
    "verify_campaign_forward_evidence",
]
