from __future__ import annotations

"""Bind one authoritative complete-board provider capture to a gated collector cycle.

The composition deliberately does not invent a second collector or a fake desktop
MarketEvent.  It reserves the existing scheduled collector START before provider I/O,
uses the existing CompleteGameBoardEvidenceStore for exact provider bytes/provenance,
then binds that evidence digest as an immutable observation artifact in the same
collector cycle terminal.
"""

import hashlib
import inspect
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from . import provider_observation_authority as _provider_observation_module
from .campaign_inception import (
    CampaignInceptionReceipt,
    CampaignInceptionSourceSpec,
    establish_campaign_inception,
)
from .causal_collector import CollectorDeltaStore
from .forward_universe_precommit_authority import ForwardUniversePrecommitLocator
from .provider_observation_authority import (
    CompleteGameBoardEvidenceStore,
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
    capture_parlay_complete_game_board,
)


ARTIFACT_KIND = "parlay-complete-game-board-v1"
_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")

_CAPTURE = capture_parlay_complete_game_board
_EVIDENCE_SAVE = CompleteGameBoardEvidenceStore.save
_EVIDENCE_PATH = CompleteGameBoardEvidenceStore._path
_CANONICAL_PATH_EQUALITY = Path.__eq__
_CANONICAL_PATH_EQUALITY_CODE = getattr(_CANONICAL_PATH_EQUALITY, "__code__", None)
_CANONICAL_PATH_JOIN = Path.__truediv__
_CANONICAL_PATH_JOIN_CODE = getattr(_CANONICAL_PATH_JOIN, "__code__", None)
_CANONICAL_OBJECT_GETATTRIBUTE = object.__getattribute__
_EVIDENCE_DIRECTORY_SURFACE = inspect.getattr_static(
    CompleteGameBoardEvidenceStore,
    "DIRECTORY",
)
_EVIDENCE_DIRECTORY = _EVIDENCE_DIRECTORY_SURFACE
_PRECOMMIT_ROUTING_SEAMS = {
    name: inspect.getattr_static(ForwardUniversePrecommitLocator, name)
    for name in ("workspace", "authority_root")
}
_PRECOMMIT_ROUTING_SEAM_ITEMS = tuple(_PRECOMMIT_ROUTING_SEAMS.items())
_NEXT_SLOT = CollectorDeltaStore._next_collector_schedule_slot
_SCHEDULE_DUE_AT = CollectorDeltaStore._collector_schedule_due_at
_BEGIN_SCHEDULED = CollectorDeltaStore._begin_scheduled_collector_cycle
_RECORD_ARTIFACT = CollectorDeltaStore._record_collector_cycle_observation_artifact
_FINISH_CYCLE = CollectorDeltaStore._finish_collector_cycle
_RESOLVE_ARTIFACT = CollectorDeltaStore.collector_cycle_observation_artifact_evidence
_STORE_SEAMS = frozenset(
    {
        "_next_collector_schedule_slot",
        "_begin_scheduled_collector_cycle",
        "_record_collector_cycle_observation_artifact",
        "_finish_collector_cycle",
        "collector_cycle_observation_artifact_evidence",
        "_connect",
        "_connect_path",
        "_path_file_identity",
        "_schedule_authority_sha256",
        "_collector_schedule_id",
        "_collector_schedule_due_at",
    }
)
_STORE_CLASS_SEAMS = {
    name: inspect.getattr_static(CollectorDeltaStore, name) for name in _STORE_SEAMS
}
_STORE_CLASS_SEAM_CODES = {
    name: getattr(
        getattr(target, "__func__", target),
        "__code__",
        None,
    )
    for name, target in _STORE_CLASS_SEAMS.items()
}
_EVIDENCE_CLASS_SEAMS = {
    "save": inspect.getattr_static(CompleteGameBoardEvidenceStore, "save"),
    "_path": inspect.getattr_static(CompleteGameBoardEvidenceStore, "_path"),
}
_EVIDENCE_CLASS_SEAM_CODES = {
    name: getattr(
        getattr(target, "__func__", target),
        "__code__",
        None,
    )
    for name, target in _EVIDENCE_CLASS_SEAMS.items()
}
_PROVIDER_REQUEST_SEAMS = {
    "source_id": inspect.getattr_static(CompleteGameBoardRequest, "source_id"),
}
_PROVIDER_SNAPSHOT_SEAMS = {
    name: inspect.getattr_static(CompleteGameBoardSnapshot, name)
    for name in (
        "captured_at",
        "frame_sha256",
        "evidence_sha256",
    )
}
_PROVIDER_CANONICAL_ASSERT = (
    _provider_observation_module._CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE
)
_PROVIDER_CANONICAL_ASSERT_CODE = _PROVIDER_CANONICAL_ASSERT.__code__


class CampaignProviderCycleCaptureError(RuntimeError):
    """The provider capture cannot be proven inside the exact gated collector cycle."""


class CampaignProviderCycleCaptureIntegrityError(CampaignProviderCycleCaptureError):
    """A durable campaign/provider/cycle authority changed or is malformed."""


def _text(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            f"{name} must be non-empty canonical text"
        )
    value.encode("utf-8")
    return value


def _sha(value: object, name: str) -> str:
    raw = _text(value, name)
    if (
        len(raw) != 64
        or raw != raw.lower()
        or any(character not in _HEX for character in raw)
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            f"{name} must be lowercase SHA-256 hex"
        )
    return raw


def _instant(value: object, name: str) -> str:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignProviderCycleCaptureIntegrityError(
            f"{name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CampaignProviderCycleCaptureIntegrityError(
            f"{name} must include timezone"
        )
    return parsed.astimezone(UTC).isoformat()


def _default_clock() -> str:
    return datetime.now(UTC).isoformat()


_CANONICAL_CAMPAIGN_CLOCK = _default_clock
_CANONICAL_CAMPAIGN_CLOCK_CODE = _CANONICAL_CAMPAIGN_CLOCK.__code__
_TEST_CAMPAIGN_CLOCK_CAPABILITY = object()
_TEST_CAMPAIGN_CLOCK_ORIGIN = ContextVar(
    "autosport_campaign_provider_cycle_test_clock",
    default=None,
)
_CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN = _TEST_CAMPAIGN_CLOCK_ORIGIN
_CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY = _TEST_CAMPAIGN_CLOCK_CAPABILITY


@contextmanager
def _test_campaign_clock_origin(*, _capability: object):
    if _capability is not _CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY:
        raise TypeError("campaign test clock requires private capability")
    token = _CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.set(
        _CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY
    )
    try:
        yield
    finally:
        _CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.reset(token)


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


def _require_canonical_seams(
    store: CollectorDeltaStore,
    evidence_store: CompleteGameBoardEvidenceStore,
) -> None:
    if type(store) is not CollectorDeltaStore:
        raise TypeError("store must be the exact canonical CollectorDeltaStore")
    if type(evidence_store) is not CompleteGameBoardEvidenceStore:
        raise TypeError(
            "evidence_store must be the exact CompleteGameBoardEvidenceStore"
        )
    rebound = sorted(
        name
        for name, expected in _STORE_CLASS_SEAMS.items()
        if inspect.getattr_static(CollectorDeltaStore, name, None) is not expected
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture seam is class-rebound: " + ", ".join(rebound)
        )
    code_changed = sorted(
        name
        for name, expected in _STORE_CLASS_SEAMS.items()
        if getattr(
            getattr(expected, "__func__", expected),
            "__code__",
            None,
        )
        is not _STORE_CLASS_SEAM_CODES[name]
    )
    if code_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture seam code changed: "
            + ", ".join(code_changed)
        )
    rebound = sorted(
        name
        for name, expected in _EVIDENCE_CLASS_SEAMS.items()
        if inspect.getattr_static(CompleteGameBoardEvidenceStore, name, None)
        is not expected
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence campaign capture seam is class-rebound: "
            + ", ".join(rebound)
        )
    code_changed = sorted(
        name
        for name, expected in _EVIDENCE_CLASS_SEAMS.items()
        if getattr(
            getattr(expected, "__func__", expected),
            "__code__",
            None,
        )
        is not _EVIDENCE_CLASS_SEAM_CODES[name]
    )
    if code_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence campaign capture seam code changed: "
            + ", ".join(code_changed)
        )
    store_state = vars(store)
    rebound = sorted(name for name in _STORE_SEAMS if name in store_state)
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture seam is instance-rebound: " + ", ".join(rebound)
        )
    request_rebound = sorted(
        name
        for name, expected in _PROVIDER_REQUEST_SEAMS.items()
        if inspect.getattr_static(CompleteGameBoardRequest, name, None) is not expected
    )
    if request_rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider request authority seam is rebound: "
            + ", ".join(request_rebound)
        )
    snapshot_rebound = sorted(
        name
        for name, expected in _PROVIDER_SNAPSHOT_SEAMS.items()
        if inspect.getattr_static(CompleteGameBoardSnapshot, name, None) is not expected
    )
    if snapshot_rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider snapshot authority seam is rebound: "
            + ", ".join(snapshot_rebound)
        )
    if (
        _provider_observation_module._CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE
        is not _PROVIDER_CANONICAL_ASSERT
        or _PROVIDER_CANONICAL_ASSERT.__code__ is not _PROVIDER_CANONICAL_ASSERT_CODE
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence assertion authority changed"
        )
    evidence_state = vars(evidence_store)
    rebound = sorted(
        name for name in ("save",) if name in evidence_state
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence campaign capture seam is instance-rebound: "
            + ", ".join(rebound)
        )


def _require_provider_evidence_campaign_scope(
    precommit_locator: ForwardUniversePrecommitLocator,
    evidence_store: CompleteGameBoardEvidenceStore,
) -> None:
    """Bind provider evidence routing to the exact prospective campaign trust root."""

    if type(precommit_locator) is not ForwardUniversePrecommitLocator:
        raise TypeError(
            "precommit_locator must be exact ForwardUniversePrecommitLocator"
        )
    if type(evidence_store) is not CompleteGameBoardEvidenceStore:
        raise TypeError(
            "evidence_store must be the exact CompleteGameBoardEvidenceStore"
        )
    if (
        inspect.getattr_static(
            CompleteGameBoardEvidenceStore,
            "DIRECTORY",
            None,
        )
        is not _EVIDENCE_DIRECTORY_SURFACE
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence directory authority changed"
        )
    if (
        len(_PRECOMMIT_ROUTING_SEAMS) != len(_PRECOMMIT_ROUTING_SEAM_ITEMS)
        or any(
            _PRECOMMIT_ROUTING_SEAMS.get(name) is not descriptor
            for name, descriptor in _PRECOMMIT_ROUTING_SEAM_ITEMS
        )
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "campaign precommit routing witness table changed"
        )
    for name, descriptor in _PRECOMMIT_ROUTING_SEAM_ITEMS:
        if (
            inspect.getattr_static(
                ForwardUniversePrecommitLocator,
                name,
                None,
            )
            is not descriptor
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign precommit routing surface changed: " + name
            )

    locator_workspace = _PRECOMMIT_ROUTING_SEAMS["workspace"].__get__(
        precommit_locator,
        ForwardUniversePrecommitLocator,
    )
    locator_authority_root = _PRECOMMIT_ROUTING_SEAMS["authority_root"].__get__(
        precommit_locator,
        ForwardUniversePrecommitLocator,
    )
    state = _CANONICAL_OBJECT_GETATTRIBUTE(evidence_store, "__dict__")
    if type(state) is not dict:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence store routing state is unavailable"
        )
    if set(("workspace", "root", "authority_root")) - set(state):
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence store routing state is incomplete"
        )
    workspace = state["workspace"]
    root = state["root"]
    authority_root = state["authority_root"]

    if (
        type(locator_workspace) is not type(workspace)
        or _CANONICAL_PATH_EQUALITY(workspace, locator_workspace) is not True
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence workspace does not match campaign precommit workspace"
        )
    expected_root = _CANONICAL_PATH_JOIN(workspace, _EVIDENCE_DIRECTORY)
    if (
        type(root) is not type(expected_root)
        or _CANONICAL_PATH_EQUALITY(root, expected_root) is not True
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence root does not match canonical campaign workspace"
        )
    if locator_authority_root is None:
        if authority_root is not None:
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider evidence authority root does not match campaign precommit authority"
            )
    elif (
        type(authority_root) is not type(locator_authority_root)
        or _CANONICAL_PATH_EQUALITY(authority_root, locator_authority_root) is not True
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence authority root does not match campaign precommit authority"
        )


_RECEIPT_ISSUANCE_CAPABILITY = object()


@dataclass(frozen=True, slots=True, init=False)
class CampaignCompleteBoardCycleReceipt:
    """Resolver-issued proof that exact provider evidence was captured in one cycle."""

    schema_version: int
    campaign_id: str
    campaign_receipt_sha256: str
    campaign_authority_record_sha256: str
    source_id: str
    run_id: str
    stream_epoch: str
    schedule_id: str
    gate_binding_sha256: str
    cycle_seq: int
    slot_ordinal: int
    artifact_id: str
    artifact_kind: str
    provider_evidence_sha256: str
    provider_frame_sha256: str
    provider_captured_at: str
    collector_artifact_evidence_sha256: str
    receipt_sha256: str

    def __new__(
        cls, *args: object, **kwargs: object
    ) -> "CampaignCompleteBoardCycleReceipt":
        raise TypeError(
            "CampaignCompleteBoardCycleReceipt is resolver-issued; "
            "use capture_campaign_complete_game_board"
        )

    @classmethod
    def _issue(
        cls,
        payload: dict[str, object],
        *,
        _issuance_capability: object,
    ) -> "CampaignCompleteBoardCycleReceipt":
        if cls is not CampaignCompleteBoardCycleReceipt:
            raise TypeError(
                "campaign cycle receipt issuer requires exact canonical class"
            )
        if _issuance_capability is not _RECEIPT_ISSUANCE_CAPABILITY:
            raise TypeError(
                "campaign cycle receipt issuance is resolver-private"
            )
        if type(payload) is not dict or set(payload) != set(_RECEIPT_FIELD_NAMES):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign cycle receipt payload is noncanonical"
            )
        instance = object.__new__(CampaignCompleteBoardCycleReceipt)
        for name in _RECEIPT_FIELD_NAMES:
            object.__setattr__(instance, name, payload[name])
        return instance

    def to_dict(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in _RECEIPT_FIELD_NAMES
        }


_RECEIPT_FIELD_NAMES = tuple(
    CampaignCompleteBoardCycleReceipt.__dataclass_fields__
)
_RECEIPT_FIELD_DESCRIPTORS = tuple(
    (
        name,
        inspect.getattr_static(CampaignCompleteBoardCycleReceipt, name),
    )
    for name in _RECEIPT_FIELD_NAMES
)

_CANONICAL_CYCLE_RECEIPT_CLASS = CampaignCompleteBoardCycleReceipt
_CANONICAL_CYCLE_RECEIPT_ISSUER = inspect.getattr_static(
    CampaignCompleteBoardCycleReceipt,
    "_issue",
)
_CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION = _CANONICAL_CYCLE_RECEIPT_ISSUER.__func__
_CANONICAL_CYCLE_RECEIPT_ISSUER_CODE = (
    _CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION.__code__
)
_CANONICAL_RECEIPT_ISSUANCE_CAPABILITY = _RECEIPT_ISSUANCE_CAPABILITY

_INCEPTION_RECEIPT_FIELD_NAMES = tuple(
    CampaignInceptionReceipt.__dataclass_fields__
)
_INCEPTION_RECEIPT_FIELD_DESCRIPTORS = tuple(
    (
        name,
        inspect.getattr_static(CampaignInceptionReceipt, name),
    )
    for name in _INCEPTION_RECEIPT_FIELD_NAMES
)


def _issue_receipt(
    *,
    campaign: CampaignInceptionReceipt,
    snapshot: CompleteGameBoardSnapshot,
    collector_evidence: dict[str, object],
) -> CampaignCompleteBoardCycleReceipt:
    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
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
    return _CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION(
        _CANONICAL_CYCLE_RECEIPT_CLASS,
        payload,
        _issuance_capability=_CANONICAL_RECEIPT_ISSUANCE_CAPABILITY,
    )


def capture_campaign_complete_game_board(
    *,
    precommit_locator: ForwardUniversePrecommitLocator,
    store: CollectorDeltaStore,
    source_spec: CampaignInceptionSourceSpec,
    evidence_store: CompleteGameBoardEvidenceStore,
    request: CompleteGameBoardRequest,
    api_key: str,
    timeout_seconds: float = 10.0,
    clock: Callable[[], str] = _default_clock,
) -> tuple[CompleteGameBoardSnapshot, CampaignCompleteBoardCycleReceipt]:
    """Capture one provider board only after the exact campaign START is authorized."""

    if type(source_spec) is not CampaignInceptionSourceSpec:
        raise TypeError("source_spec must be exact CampaignInceptionSourceSpec")
    if type(request) is not CompleteGameBoardRequest:
        raise TypeError("request must be exact CompleteGameBoardRequest")
    if not callable(clock):
        raise TypeError("clock must be callable")
    test_clock_origin = (
        _CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.get()
        is _CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY
    )
    if not test_clock_origin and clock is not _CANONICAL_CAMPAIGN_CLOCK:
        raise CampaignProviderCycleCaptureIntegrityError(
            "campaign collector clock must be product-owned"
        )
    effective_clock = clock if test_clock_origin else _CANONICAL_CAMPAIGN_CLOCK
    if request.source_id != source_spec.source_id:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider request source_id does not match campaign collector source"
        )
    require_seams = _require_canonical_seams
    require_evidence_scope = _require_provider_evidence_campaign_scope
    instant = _instant
    establish_inception = establish_campaign_inception
    next_slot = _NEXT_SLOT
    schedule_due_at = _SCHEDULE_DUE_AT
    begin_scheduled = _BEGIN_SCHEDULED
    provider_capture = _CAPTURE
    evidence_save = _EVIDENCE_SAVE
    evidence_path_for = _EVIDENCE_PATH
    path_equal = _CANONICAL_PATH_EQUALITY
    record_artifact = _RECORD_ARTIFACT
    finish_cycle = _FINISH_CYCLE
    resolve_artifact = _RESOLVE_ARTIFACT
    issue_receipt = _issue_receipt
    module_globals = globals()
    expected_dispatch = (
        ("_require_canonical_seams", require_seams),
        ("_require_provider_evidence_campaign_scope", require_evidence_scope),
        ("_instant", instant),
        ("establish_campaign_inception", establish_inception),
        ("_NEXT_SLOT", next_slot),
        ("_SCHEDULE_DUE_AT", schedule_due_at),
        ("_BEGIN_SCHEDULED", begin_scheduled),
        ("_CAPTURE", provider_capture),
        ("_EVIDENCE_SAVE", evidence_save),
        ("_EVIDENCE_PATH", evidence_path_for),
        ("_CANONICAL_PATH_EQUALITY", path_equal),
        ("_CANONICAL_PATH_JOIN", _CANONICAL_PATH_JOIN),
        ("_CANONICAL_OBJECT_GETATTRIBUTE", _CANONICAL_OBJECT_GETATTRIBUTE),
        ("_RECORD_ARTIFACT", record_artifact),
        ("_FINISH_CYCLE", finish_cycle),
        ("_RESOLVE_ARTIFACT", resolve_artifact),
        ("_issue_receipt", issue_receipt),
    )
    expected_codes = tuple(
        (name, target, getattr(getattr(target, "__func__", target), "__code__", None))
        for name, target in expected_dispatch
    )
    expected_inspect = inspect
    expected_getattr_static = inspect.getattr_static
    expected_receipt_field_names = _RECEIPT_FIELD_NAMES
    expected_receipt_field_descriptors = _RECEIPT_FIELD_DESCRIPTORS
    expected_inception_field_names = _INCEPTION_RECEIPT_FIELD_NAMES
    expected_inception_field_descriptors = _INCEPTION_RECEIPT_FIELD_DESCRIPTORS
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_datetime = datetime
    expected_utc = UTC
    expected_campaign_clock = _CANONICAL_CAMPAIGN_CLOCK
    expected_campaign_clock_code = _CANONICAL_CAMPAIGN_CLOCK_CODE
    expected_test_clock_origin = _CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN
    expected_test_clock_capability = _CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY
    expected_provider_module = _provider_observation_module
    expected_provider_request_seams = _PROVIDER_REQUEST_SEAMS
    expected_provider_snapshot_seams = _PROVIDER_SNAPSHOT_SEAMS
    expected_provider_assert = _PROVIDER_CANONICAL_ASSERT
    expected_provider_assert_code = _PROVIDER_CANONICAL_ASSERT_CODE
    expected_receipt_class = _CANONICAL_CYCLE_RECEIPT_CLASS
    expected_receipt_issuer = _CANONICAL_CYCLE_RECEIPT_ISSUER
    expected_receipt_issuer_function = _CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION
    expected_receipt_issuer_code = _CANONICAL_CYCLE_RECEIPT_ISSUER_CODE
    expected_receipt_issuance_capability = _CANONICAL_RECEIPT_ISSUANCE_CAPABILITY
    expected_evidence_directory_surface = _EVIDENCE_DIRECTORY_SURFACE
    expected_precommit_routing_seams = _PRECOMMIT_ROUTING_SEAMS
    expected_precommit_routing_items = _PRECOMMIT_ROUTING_SEAM_ITEMS

    def require_stable_dispatch() -> None:
        if (
            module_globals.get("inspect") is not expected_inspect
            or expected_inspect.getattr_static is not expected_getattr_static
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign provider-cycle reflection dispatch changed"
            )
        if (
            module_globals.get("_CANONICAL_CYCLE_RECEIPT_CLASS")
            is not expected_receipt_class
            or module_globals.get("_CANONICAL_CYCLE_RECEIPT_ISSUER")
            is not expected_receipt_issuer
            or module_globals.get("_CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION")
            is not expected_receipt_issuer_function
            or module_globals.get("_CANONICAL_CYCLE_RECEIPT_ISSUER_CODE")
            is not expected_receipt_issuer_code
            or module_globals.get("_CANONICAL_RECEIPT_ISSUANCE_CAPABILITY")
            is not expected_receipt_issuance_capability
            or module_globals.get("_RECEIPT_ISSUANCE_CAPABILITY")
            is not expected_receipt_issuance_capability
            or expected_receipt_issuer_function.__code__
            is not expected_receipt_issuer_code
            or expected_getattr_static(expected_receipt_class, "_issue")
            is not expected_receipt_issuer
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign cycle receipt issuance authority changed"
            )
        if (
            module_globals.get("_CANONICAL_CAMPAIGN_CLOCK")
            is not expected_campaign_clock
            or module_globals.get("_CANONICAL_CAMPAIGN_CLOCK_CODE")
            is not expected_campaign_clock_code
            or expected_campaign_clock.__code__ is not expected_campaign_clock_code
            or module_globals.get("_TEST_CAMPAIGN_CLOCK_ORIGIN")
            is not expected_test_clock_origin
            or module_globals.get("_TEST_CAMPAIGN_CLOCK_CAPABILITY")
            is not expected_test_clock_capability
            or module_globals.get("_CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN")
            is not expected_test_clock_origin
            or module_globals.get("_CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY")
            is not expected_test_clock_capability
            or module_globals.get("_provider_observation_module")
            is not expected_provider_module
            or module_globals.get("_PROVIDER_REQUEST_SEAMS")
            is not expected_provider_request_seams
            or module_globals.get("_PROVIDER_SNAPSHOT_SEAMS")
            is not expected_provider_snapshot_seams
            or module_globals.get("_PROVIDER_CANONICAL_ASSERT")
            is not expected_provider_assert
            or module_globals.get("_PROVIDER_CANONICAL_ASSERT_CODE")
            is not expected_provider_assert_code
            or expected_provider_module._CANONICAL_ASSERT_COMPLETE_GAME_BOARD_AUTHORITATIVE
            is not expected_provider_assert
            or expected_provider_assert.__code__ is not expected_provider_assert_code
            or any(
                expected_getattr_static(CompleteGameBoardRequest, name)
                is not descriptor
                for name, descriptor in expected_provider_request_seams.items()
            )
            or any(
                expected_getattr_static(CompleteGameBoardSnapshot, name)
                is not descriptor
                for name, descriptor in expected_provider_snapshot_seams.items()
            )
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign provider acquisition authority changed"
            )
        if (
            module_globals.get("_RECEIPT_FIELD_NAMES")
            is not expected_receipt_field_names
            or module_globals.get("_RECEIPT_FIELD_DESCRIPTORS")
            is not expected_receipt_field_descriptors
            or any(
                expected_getattr_static(
                    CampaignCompleteBoardCycleReceipt,
                    name,
                )
                is not descriptor
                for name, descriptor in expected_receipt_field_descriptors
            )
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign provider-cycle receipt field authority changed"
            )
        if (
            module_globals.get("_INCEPTION_RECEIPT_FIELD_NAMES")
            is not expected_inception_field_names
            or module_globals.get("_INCEPTION_RECEIPT_FIELD_DESCRIPTORS")
            is not expected_inception_field_descriptors
            or any(
                expected_getattr_static(
                    CampaignInceptionReceipt,
                    name,
                )
                is not descriptor
                for name, descriptor in expected_inception_field_descriptors
            )
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign inception receipt field authority changed"
            )
        if (
            module_globals.get("hashlib") is not expected_hashlib
            or expected_hashlib.sha256 is not expected_sha256
            or module_globals.get("json") is not expected_json
            or expected_json.dumps is not expected_json_dumps
            or module_globals.get("datetime") is not expected_datetime
            or module_globals.get("UTC") is not expected_utc
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign provider-cycle chronology/digest primitives changed"
            )
        if (
            module_globals.get("_EVIDENCE_DIRECTORY_SURFACE")
            is not expected_evidence_directory_surface
            or expected_getattr_static(
                CompleteGameBoardEvidenceStore,
                "DIRECTORY",
                None,
            )
            is not expected_evidence_directory_surface
            or module_globals.get("_PRECOMMIT_ROUTING_SEAMS")
            is not expected_precommit_routing_seams
            or module_globals.get("_PRECOMMIT_ROUTING_SEAM_ITEMS")
            is not expected_precommit_routing_items
            or len(expected_precommit_routing_seams)
            != len(expected_precommit_routing_items)
            or any(
                expected_precommit_routing_seams.get(name) is not descriptor
                or expected_getattr_static(
                    ForwardUniversePrecommitLocator,
                    name,
                    None,
                )
                is not descriptor
                for name, descriptor in expected_precommit_routing_items
            )
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign provider evidence routing witness changed mid-capture"
            )
        for name, target, code in expected_codes:
            if module_globals.get(name) is not target:
                raise CampaignProviderCycleCaptureIntegrityError(
                    "campaign provider-cycle dispatch authority is rebound: " + name
                )
            function = getattr(target, "__func__", target)
            if getattr(function, "__code__", None) is not code:
                raise CampaignProviderCycleCaptureIntegrityError(
                    "campaign provider-cycle dispatch code changed: " + name
                )
        require_evidence_scope(precommit_locator, evidence_store)

    require_stable_dispatch()
    require_seams(store, evidence_store)

    campaign = establish_inception(
        precommit_locator=precommit_locator,
        store=store,
        source_spec=source_spec,
    )
    require_stable_dispatch()
    require_seams(store, evidence_store)
    slot = next_slot(
        store,
        source_id=source_spec.source_id,
        run_id=source_spec.run_id,
    )
    if (
        type(slot) is not dict
        or slot.get("schedule_id") != campaign.schedule_id
        or slot.get("stream_epoch") != source_spec.stream_epoch
        or slot.get("max_items") != source_spec.max_items
        or type(slot.get("slot_ordinal")) is not int
        or slot.get("slot_ordinal") < source_spec.evaluation_start_slot_ordinal
        or slot.get("slot_ordinal") > source_spec.evaluation_end_slot_ordinal
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "campaign collector next slot does not match inception receipt"
        )
    raw_attempted_at = effective_clock()
    require_stable_dispatch()
    require_seams(store, evidence_store)
    attempted_at = instant(raw_attempted_at, "collector attempted_at")
    attempted_instant = datetime.fromisoformat(attempted_at)
    slot_due_instant = datetime.fromisoformat(
        instant(slot.get("due_at"), "collector slot due_at")
    )
    try:
        slot_deadline_at = schedule_due_at(
            anchor_at=source_spec.anchor_at,
            interval_seconds=repr(source_spec.interval_seconds),
            slot_ordinal=slot.get("slot_ordinal") + 1,
        )
        slot_deadline_instant = datetime.fromisoformat(
            instant(slot_deadline_at, "collector next slot due_at")
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector fixed schedule slot window is not representable"
        ) from exc
    if (
        attempted_instant < slot_due_instant
        or attempted_instant >= slot_deadline_instant
    ):
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector START falls outside precommitted fixed schedule slot window"
        )
    campaign_not_before = datetime.fromisoformat(
        instant(campaign.observation_not_before, "campaign observation_not_before")
    )
    campaign_not_after = datetime.fromisoformat(
        instant(campaign.observation_not_after, "campaign observation_not_after")
    )
    if attempted_instant < campaign_not_before or attempted_instant > campaign_not_after:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector START falls outside precommitted campaign observation window"
        )
    try:
        cycle_seq = begin_scheduled(
            store,
            source_id=source_spec.source_id,
            run_id=source_spec.run_id,
            stream_epoch=source_spec.stream_epoch,
            max_items=source_spec.max_items,
            slot_ordinal=slot.get("slot_ordinal"),
            due_at=slot.get("due_at"),
            attempted_at=attempted_at,
        )
    except (TypeError, ValueError) as exc:
        raise CampaignProviderCycleCaptureIntegrityError(
            "cannot reserve exact campaign collector START before provider I/O"
        ) from exc

    terminal_written = False
    try:
        snapshot = provider_capture(
            api_key=api_key,
            request=request,
            timeout_seconds=timeout_seconds,
        )
        require_stable_dispatch()
        if type(snapshot) is not CompleteGameBoardSnapshot:
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider capture returned noncanonical snapshot type"
            )
        provider_captured_at = instant(
            snapshot.captured_at,
            "provider captured_at",
        )
        provider_captured_instant = datetime.fromisoformat(provider_captured_at)
        if provider_captured_instant < attempted_instant:
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider observation predates authorized collector START"
            )
        if provider_captured_instant >= slot_deadline_instant:
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider observation falls outside precommitted fixed schedule slot window"
            )
        raw_completed_at = effective_clock()
        require_stable_dispatch()
        require_seams(store, evidence_store)
        completed_at = instant(raw_completed_at, "collector completed_at")
        completed_instant = datetime.fromisoformat(completed_at)
        if completed_instant < provider_captured_instant:
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider observation falls after collector cycle completion"
            )
        if (
            provider_captured_instant < campaign_not_before
            or provider_captured_instant > campaign_not_after
            or completed_instant > campaign_not_after
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider cycle falls outside precommitted campaign observation window"
            )
        evidence_path = evidence_save(evidence_store, snapshot)
        repeated_path = evidence_save(evidence_store, snapshot)
        require_stable_dispatch()
        require_seams(store, evidence_store)
        expected_evidence_path = evidence_path_for(
            evidence_store,
            snapshot.evidence_sha256,
        )
        if (
            type(evidence_path) is not type(expected_evidence_path)
            or type(repeated_path) is not type(expected_evidence_path)
            or path_equal(evidence_path, repeated_path) is not True
            or path_equal(evidence_path, expected_evidence_path) is not True
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "provider evidence store did not retain exact captured identity"
            )
        artifact = record_artifact(
            store,
            source_id=source_spec.source_id,
            cycle_seq=cycle_seq,
            artifact_kind=ARTIFACT_KIND,
            artifact_sha256=snapshot.evidence_sha256,
        )
        if (
            type(artifact) is not dict
            or artifact.get("artifact_kind") != ARTIFACT_KIND
            or artifact.get("artifact_sha256") != snapshot.evidence_sha256
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "collector artifact append returned noncanonical identity"
            )
        finish_cycle(
            store,
            source_id=source_spec.source_id,
            cycle_seq=cycle_seq,
            status="SUCCESS",
            completed_at=completed_at,
            catalog_changes=(),
            observed_delta_ids=(),
            committed_delta_ids=(),
            duplicate_delta_ids=(),
        )
        terminal_written = True
        collector_evidence = resolve_artifact(
            store,
            source_id=source_spec.source_id,
            cycle_seq=cycle_seq,
            artifact_kind=ARTIFACT_KIND,
            artifact_sha256=snapshot.evidence_sha256,
        )
        if (
            collector_evidence.get("source_id") != source_spec.source_id
            or collector_evidence.get("run_id") != source_spec.run_id
            or collector_evidence.get("stream_epoch") != source_spec.stream_epoch
            or collector_evidence.get("cycle_seq") != cycle_seq
            or collector_evidence.get("schedule_id") != campaign.schedule_id
            or collector_evidence.get("gate_binding_sha256")
            != campaign.gate_binding_sha256
            or collector_evidence.get("authorization_sha256")
            != campaign.authority_record_sha256
            or collector_evidence.get("slot_ordinal") != slot.get("slot_ordinal")
            or collector_evidence.get("due_at") != slot.get("due_at")
            or collector_evidence.get("attempted_at") != attempted_at
            or collector_evidence.get("completed_at") != completed_at
            or collector_evidence.get("artifact_id") != artifact.get("artifact_id")
        ):
            raise CampaignProviderCycleCaptureIntegrityError(
                "collector artifact evidence does not bind exact campaign authority"
            )
        require_stable_dispatch()
        require_seams(store, evidence_store)
        return snapshot, issue_receipt(
            campaign=campaign,
            snapshot=snapshot,
            collector_evidence=collector_evidence,
        )
    except BaseException as exc:
        if not terminal_written:
            try:
                try:
                    raw_failure_completed_at = effective_clock()
                except BaseException:
                    raw_failure_completed_at = attempted_at
                instant_function = getattr(instant, "__func__", instant)
                finish_function = getattr(finish_cycle, "__func__", finish_cycle)
                require_seams_function = getattr(
                    require_seams,
                    "__func__",
                    require_seams,
                )
                expected_instant_code = next(
                    code
                    for name, target, code in expected_codes
                    if name == "_instant" and target is instant
                )
                expected_finish_code = next(
                    code
                    for name, target, code in expected_codes
                    if name == "_FINISH_CYCLE" and target is finish_cycle
                )
                expected_require_seams_code = next(
                    code
                    for name, target, code in expected_codes
                    if name == "_require_canonical_seams" and target is require_seams
                )
                if (
                    module_globals.get("inspect") is not expected_inspect
                    or expected_inspect.getattr_static is not expected_getattr_static
                    or module_globals.get("hashlib") is not expected_hashlib
                    or expected_hashlib.sha256 is not expected_sha256
                    or module_globals.get("json") is not expected_json
                    or expected_json.dumps is not expected_json_dumps
                    or module_globals.get("datetime") is not expected_datetime
                    or module_globals.get("UTC") is not expected_utc
                    or module_globals.get("_require_canonical_seams")
                    is not require_seams
                    or getattr(require_seams_function, "__code__", None)
                    is not expected_require_seams_code
                    or module_globals.get("_instant") is not instant
                    or getattr(instant_function, "__code__", None)
                    is not expected_instant_code
                    or module_globals.get("_FINISH_CYCLE") is not finish_cycle
                    or getattr(finish_function, "__code__", None)
                    is not expected_finish_code
                ):
                    raise CampaignProviderCycleCaptureIntegrityError(
                        "campaign provider-cycle failure terminal dispatch changed"
                    )
                require_seams(store, evidence_store)
                try:
                    failure_completed_at = instant(
                        raw_failure_completed_at,
                        "collector failure completed_at",
                    )
                except (TypeError, ValueError):
                    failure_completed_at = attempted_at
                if failure_completed_at < attempted_at:
                    failure_completed_at = attempted_at
                finish_cycle(
                    store,
                    source_id=source_spec.source_id,
                    cycle_seq=cycle_seq,
                    status="LOCAL_FAILURE",
                    completed_at=failure_completed_at,
                    catalog_changes=(),
                    observed_delta_ids=(),
                    committed_delta_ids=(),
                    duplicate_delta_ids=(),
                    error_code=type(exc).__name__,
                )
            except BaseException as terminal_error:
                try:
                    exc.add_note(
                        "collector campaign capture failure terminal also failed: "
                        f"{type(terminal_error).__name__}: {terminal_error}"
                    )
                except BaseException:
                    pass
        raise


def _seal_campaign_provider_cycle_capture_dispatch() -> None:
    """Seal authority-bearing capture dispatch against runtime rebinding."""

    module_globals = globals()
    expected_error = CampaignProviderCycleCaptureIntegrityError
    expected_capture = capture_campaign_complete_game_board
    expected_capture_code = expected_capture.__code__
    expected_inspect = inspect
    expected_getattr_static = inspect.getattr_static
    expected_receipt_field_names = _RECEIPT_FIELD_NAMES
    expected_receipt_field_descriptors = _RECEIPT_FIELD_DESCRIPTORS
    expected_inception_field_names = _INCEPTION_RECEIPT_FIELD_NAMES
    expected_inception_field_descriptors = _INCEPTION_RECEIPT_FIELD_DESCRIPTORS
    expected_hashlib = hashlib
    expected_sha256 = hashlib.sha256
    expected_json = json
    expected_json_dumps = json.dumps
    expected_datetime = datetime
    expected_utc = UTC
    expected_values = {
        "CollectorDeltaStore": CollectorDeltaStore,
        "CompleteGameBoardEvidenceStore": CompleteGameBoardEvidenceStore,
        "CompleteGameBoardRequest": CompleteGameBoardRequest,
        "CompleteGameBoardSnapshot": CompleteGameBoardSnapshot,
        "Path": Path,
        "CampaignInceptionReceipt": CampaignInceptionReceipt,
        "CampaignInceptionSourceSpec": CampaignInceptionSourceSpec,
        "ForwardUniversePrecommitLocator": ForwardUniversePrecommitLocator,
        "CampaignCompleteBoardCycleReceipt": CampaignCompleteBoardCycleReceipt,
        "CampaignProviderCycleCaptureIntegrityError": expected_error,
        "inspect": expected_inspect,
        "hashlib": expected_hashlib,
        "json": expected_json,
        "datetime": expected_datetime,
        "UTC": expected_utc,
        "_RECEIPT_FIELD_NAMES": expected_receipt_field_names,
        "_RECEIPT_FIELD_DESCRIPTORS": expected_receipt_field_descriptors,
        "_INCEPTION_RECEIPT_FIELD_NAMES": expected_inception_field_names,
        "_INCEPTION_RECEIPT_FIELD_DESCRIPTORS": expected_inception_field_descriptors,
        "ARTIFACT_KIND": ARTIFACT_KIND,
        "_CANONICAL_CYCLE_RECEIPT_CLASS": _CANONICAL_CYCLE_RECEIPT_CLASS,
        "_CANONICAL_CYCLE_RECEIPT_ISSUER": _CANONICAL_CYCLE_RECEIPT_ISSUER,
        "_CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION": _CANONICAL_CYCLE_RECEIPT_ISSUER_FUNCTION,
        "_CANONICAL_CYCLE_RECEIPT_ISSUER_CODE": _CANONICAL_CYCLE_RECEIPT_ISSUER_CODE,
        "_CANONICAL_RECEIPT_ISSUANCE_CAPABILITY": _CANONICAL_RECEIPT_ISSUANCE_CAPABILITY,
        "_RECEIPT_ISSUANCE_CAPABILITY": _RECEIPT_ISSUANCE_CAPABILITY,
        "_CANONICAL_CAMPAIGN_CLOCK": _CANONICAL_CAMPAIGN_CLOCK,
        "_CANONICAL_CAMPAIGN_CLOCK_CODE": _CANONICAL_CAMPAIGN_CLOCK_CODE,
        "_TEST_CAMPAIGN_CLOCK_ORIGIN": _TEST_CAMPAIGN_CLOCK_ORIGIN,
        "_TEST_CAMPAIGN_CLOCK_CAPABILITY": _TEST_CAMPAIGN_CLOCK_CAPABILITY,
        "_CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN": _CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN,
        "_CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY": _CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY,
        "_provider_observation_module": _provider_observation_module,
        "_PROVIDER_REQUEST_SEAMS": _PROVIDER_REQUEST_SEAMS,
        "_PROVIDER_SNAPSHOT_SEAMS": _PROVIDER_SNAPSHOT_SEAMS,
        "_PROVIDER_CANONICAL_ASSERT": _PROVIDER_CANONICAL_ASSERT,
        "_PROVIDER_CANONICAL_ASSERT_CODE": _PROVIDER_CANONICAL_ASSERT_CODE,
        "_EVIDENCE_PATH": _EVIDENCE_PATH,
        "_CANONICAL_PATH_EQUALITY": _CANONICAL_PATH_EQUALITY,
        "_CANONICAL_PATH_EQUALITY_CODE": _CANONICAL_PATH_EQUALITY_CODE,
        "_CANONICAL_PATH_JOIN": _CANONICAL_PATH_JOIN,
        "_CANONICAL_PATH_JOIN_CODE": _CANONICAL_PATH_JOIN_CODE,
        "_CANONICAL_OBJECT_GETATTRIBUTE": _CANONICAL_OBJECT_GETATTRIBUTE,
        "_EVIDENCE_DIRECTORY": _EVIDENCE_DIRECTORY,
        "_EVIDENCE_DIRECTORY_SURFACE": _EVIDENCE_DIRECTORY_SURFACE,
        "_PRECOMMIT_ROUTING_SEAMS": _PRECOMMIT_ROUTING_SEAMS,
        "_PRECOMMIT_ROUTING_SEAM_ITEMS": _PRECOMMIT_ROUTING_SEAM_ITEMS,
    }
    expected_callables = tuple(
        (
            name,
            target,
            getattr(getattr(target, "__func__", target), "__code__", None),
        )
        for name, target in (
            ("_CAPTURE", _CAPTURE),
            ("_EVIDENCE_SAVE", _EVIDENCE_SAVE),
            ("_EVIDENCE_PATH", _EVIDENCE_PATH),
            ("_CANONICAL_PATH_EQUALITY", _CANONICAL_PATH_EQUALITY),
            ("_CANONICAL_PATH_JOIN", _CANONICAL_PATH_JOIN),
            ("_require_provider_evidence_campaign_scope", _require_provider_evidence_campaign_scope),
            ("_NEXT_SLOT", _NEXT_SLOT),
            ("_SCHEDULE_DUE_AT", _SCHEDULE_DUE_AT),
            ("_BEGIN_SCHEDULED", _BEGIN_SCHEDULED),
            ("_RECORD_ARTIFACT", _RECORD_ARTIFACT),
            ("_FINISH_CYCLE", _FINISH_CYCLE),
            ("_RESOLVE_ARTIFACT", _RESOLVE_ARTIFACT),
            ("establish_campaign_inception", establish_campaign_inception),
            ("_require_canonical_seams", _require_canonical_seams),
            ("_issue_receipt", _issue_receipt),
            ("_text", _text),
            ("_sha", _sha),
            ("_instant", _instant),
            ("_digest", _digest),
        )
    )
    expected_issue_surface = expected_getattr_static(
        CampaignCompleteBoardCycleReceipt,
        "_issue",
    )
    expected_issue_function = getattr(
        expected_issue_surface,
        "__func__",
        expected_issue_surface,
    )
    expected_issue_code = getattr(expected_issue_function, "__code__", None)

    def require_dispatch_integrity() -> None:
        for name, expected in expected_values.items():
            if module_globals.get(name) is not expected:
                raise expected_error(
                    "campaign provider-cycle dispatch authority is rebound: " + name
                )
        if expected_inspect.getattr_static is not expected_getattr_static:
            raise expected_error(
                "campaign provider-cycle reflection dispatch changed"
            )
        if any(
            expected_getattr_static(
                CampaignCompleteBoardCycleReceipt,
                name,
            )
            is not descriptor
            for name, descriptor in expected_receipt_field_descriptors
        ):
            raise expected_error(
                "campaign provider-cycle receipt field authority changed"
            )
        if any(
            expected_getattr_static(
                CampaignInceptionReceipt,
                name,
            )
            is not descriptor
            for name, descriptor in expected_inception_field_descriptors
        ):
            raise expected_error(
                "campaign inception receipt field authority changed"
            )
        if (
            expected_hashlib.sha256 is not expected_sha256
            or expected_json.dumps is not expected_json_dumps
        ):
            raise expected_error(
                "campaign provider-cycle chronology/digest primitives changed"
            )
        if (
            expected_getattr_static(CompleteGameBoardEvidenceStore, "DIRECTORY", None)
            is not _EVIDENCE_DIRECTORY_SURFACE
            or len(_PRECOMMIT_ROUTING_SEAMS) != len(_PRECOMMIT_ROUTING_SEAM_ITEMS)
            or any(
                _PRECOMMIT_ROUTING_SEAMS.get(name) is not descriptor
                or expected_getattr_static(ForwardUniversePrecommitLocator, name, None)
                is not descriptor
                for name, descriptor in _PRECOMMIT_ROUTING_SEAM_ITEMS
            )
        ):
            raise expected_error(
                "campaign provider evidence routing authority changed"
            )
        for name, expected, code in expected_callables:
            if module_globals.get(name) is not expected:
                raise expected_error(
                    "campaign provider-cycle dispatch authority is rebound: " + name
                )
            function = getattr(expected, "__func__", expected)
            if getattr(function, "__code__", None) is not code:
                raise expected_error(
                    "campaign provider-cycle dispatch code changed: " + name
                )
        current_issue_surface = expected_getattr_static(
            CampaignCompleteBoardCycleReceipt,
            "_issue",
        )
        if current_issue_surface is not expected_issue_surface:
            raise expected_error(
                "campaign provider-cycle receipt issuer is rebound"
            )
        current_issue_function = getattr(
            current_issue_surface,
            "__func__",
            current_issue_surface,
        )
        if (
            current_issue_function is not expected_issue_function
            or getattr(current_issue_function, "__code__", None)
            is not expected_issue_code
        ):
            raise expected_error(
                "campaign provider-cycle receipt issuer code changed"
            )
        if expected_capture.__code__ is not expected_capture_code:
            raise expected_error(
                "campaign provider-cycle capture implementation changed"
            )

    def sealed_capture_campaign_complete_game_board(*args, **kwargs):
        require_dispatch_integrity()
        result = expected_capture(*args, **kwargs)
        require_dispatch_integrity()
        return result

    sealed_capture_campaign_complete_game_board.__name__ = expected_capture.__name__
    sealed_capture_campaign_complete_game_board.__qualname__ = expected_capture.__qualname__
    sealed_capture_campaign_complete_game_board.__doc__ = expected_capture.__doc__
    module_globals["capture_campaign_complete_game_board"] = (
        sealed_capture_campaign_complete_game_board
    )


_seal_campaign_provider_cycle_capture_dispatch()


__all__ = [
    "ARTIFACT_KIND",
    "CampaignCompleteBoardCycleReceipt",
    "CampaignProviderCycleCaptureError",
    "CampaignProviderCycleCaptureIntegrityError",
    "capture_campaign_complete_game_board",
]
