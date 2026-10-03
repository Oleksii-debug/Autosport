from __future__ import annotations

"""Bind one authoritative complete-board provider capture to a gated collector cycle.

The composition deliberately does not invent a second collector or a fake desktop
MarketEvent.  It reserves the existing scheduled collector START before provider I/O,
uses the existing CompleteGameBoardEvidenceStore for exact provider bytes/provenance,
then binds that evidence digest as an immutable observation artifact in the same
collector cycle terminal.
"""

import builtins
import hashlib
import inspect
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
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
_CANONICAL_TYPE = type
_CANONICAL_CALLABLE = callable
_CANONICAL_GETATTR = getattr
_CANONICAL_SORTED = sorted
_CANONICAL_TYPE_ERROR = TypeError

_CAPTURE = capture_parlay_complete_game_board
_EVIDENCE_SAVE = CompleteGameBoardEvidenceStore.save
_EVIDENCE_PATH = CompleteGameBoardEvidenceStore._path
_CANONICAL_PATH_EQUALITY = Path.__eq__
_CANONICAL_PATH_EQUALITY_CODE = _CANONICAL_GETATTR(_CANONICAL_PATH_EQUALITY, "__code__", None)
_CANONICAL_PATH_JOIN = Path.__truediv__
_CANONICAL_PATH_JOIN_CODE = _CANONICAL_GETATTR(_CANONICAL_PATH_JOIN, "__code__", None)
_CANONICAL_OBJECT_GETATTRIBUTE = object.__getattribute__
_CANONICAL_MODULE_GLOBALS = globals()
_CANONICAL_GETATTR_STATIC = inspect.getattr_static
_CANONICAL_GETATTR_STATIC_CODE = _CANONICAL_GETATTR_STATIC.__code__
_CANONICAL_GETATTR_STATIC_GLOBALS = _CANONICAL_GETATTR_STATIC.__globals__
_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS = tuple(
    (
        name,
        _CANONICAL_GETATTR_STATIC_GLOBALS[name],
        _CANONICAL_GETATTR(_CANONICAL_GETATTR_STATIC_GLOBALS[name], "__code__", None),
    )
    for name in _CANONICAL_GETATTR_STATIC_CODE.co_names
    if name in _CANONICAL_GETATTR_STATIC_GLOBALS
)
_EVIDENCE_DIRECTORY_SURFACE = _CANONICAL_GETATTR_STATIC(
    CompleteGameBoardEvidenceStore,
    "DIRECTORY",
)
_EVIDENCE_DIRECTORY = _EVIDENCE_DIRECTORY_SURFACE
_PRECOMMIT_ROUTING_SEAMS = {
    name: _CANONICAL_GETATTR_STATIC(ForwardUniversePrecommitLocator, name)
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
        "_cycle_terminal_payload_json",
        "_cycle_terminal_payload_sha256",
        "_collector_schedule_id",
        "_collector_schedule_due_at",
    }
)
_STORE_CLASS_SEAMS = {
    name: _CANONICAL_GETATTR_STATIC(CollectorDeltaStore, name) for name in _STORE_SEAMS
}
_STORE_CLASS_SEAM_CODES = {
    name: _CANONICAL_GETATTR(
        _CANONICAL_GETATTR(target, "__func__", target),
        "__code__",
        None,
    )
    for name, target in _STORE_CLASS_SEAMS.items()
}
_STORE_CLASS_SEAM_WITNESSES = tuple(
    (name, target, _STORE_CLASS_SEAM_CODES[name])
    for name, target in _STORE_CLASS_SEAMS.items()
)
_STORE_CLASS_SEAM_GLOBAL_WITNESSES = tuple(
    (
        name,
        function,
        function_globals,
        tuple(
            (
                dependency_name,
                function_globals[dependency_name],
                _CANONICAL_GETATTR(
                    function_globals[dependency_name],
                    "__code__",
                    None,
                ),
            )
            for dependency_name in code.co_names
            if dependency_name in function_globals
        ),
    )
    for name, target, code in _STORE_CLASS_SEAM_WITNESSES
    if code is not None
    for function in (_CANONICAL_GETATTR(target, "__func__", target),)
    for function_globals in (
        _CANONICAL_GETATTR(function, "__globals__", None),
    )
    if function_globals is not None
)
_STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES = tuple(
    (
        name,
        dependency_name,
        module,
        attribute_name,
        _CANONICAL_GETATTR(module, attribute_name),
        _CANONICAL_GETATTR(
            _CANONICAL_GETATTR(module, attribute_name),
            "__code__",
            None,
        ),
    )
    for name, function, function_globals, global_items
    in _STORE_CLASS_SEAM_GLOBAL_WITNESSES
    for dependency_name, module, _dependency_code in global_items
    if _CANONICAL_TYPE(module) is ModuleType
    for attribute_name in function.__code__.co_names
    if hasattr(module, attribute_name)
)
_STORE_CLASS_SEAM_BUILTIN_WITNESSES = tuple(
    (
        name,
        function_globals,
        builtins,
        builtin_name,
        _CANONICAL_GETATTR(builtins, builtin_name),
    )
    for name, function, function_globals, _global_items
    in _STORE_CLASS_SEAM_GLOBAL_WITNESSES
    for builtin_name in function.__code__.co_names
    if (
        builtin_name not in function_globals
        and hasattr(builtins, builtin_name)
    )
)
_EVIDENCE_CLASS_SEAMS = {
    "save": _CANONICAL_GETATTR_STATIC(CompleteGameBoardEvidenceStore, "save"),
    "_path": _CANONICAL_GETATTR_STATIC(CompleteGameBoardEvidenceStore, "_path"),
}
_EVIDENCE_CLASS_SEAM_CODES = {
    name: _CANONICAL_GETATTR(
        _CANONICAL_GETATTR(target, "__func__", target),
        "__code__",
        None,
    )
    for name, target in _EVIDENCE_CLASS_SEAMS.items()
}
_EVIDENCE_CLASS_SEAM_WITNESSES = tuple(
    (name, target, _EVIDENCE_CLASS_SEAM_CODES[name])
    for name, target in _EVIDENCE_CLASS_SEAMS.items()
)
_PROVIDER_REQUEST_SEAMS = {
    "source_id": _CANONICAL_GETATTR_STATIC(CompleteGameBoardRequest, "source_id"),
}
_PROVIDER_REQUEST_SEAM_ITEMS = tuple(_PROVIDER_REQUEST_SEAMS.items())
_PROVIDER_SNAPSHOT_SEAMS = {
    name: _CANONICAL_GETATTR_STATIC(CompleteGameBoardSnapshot, name)
    for name in (
        "captured_at",
        "frame_sha256",
        "evidence_sha256",
    )
}
_PROVIDER_SNAPSHOT_SEAM_ITEMS = tuple(_PROVIDER_SNAPSHOT_SEAMS.items())
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
        _CANONICAL_TYPE(value) is not str
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
    if _CANONICAL_TYPE(store) is not CollectorDeltaStore:
        raise _CANONICAL_TYPE_ERROR("store must be the exact canonical CollectorDeltaStore")
    if _CANONICAL_TYPE(evidence_store) is not CompleteGameBoardEvidenceStore:
        raise _CANONICAL_TYPE_ERROR(
            "evidence_store must be the exact CompleteGameBoardEvidenceStore"
        )
    rebound = _CANONICAL_SORTED(
        name
        for name, expected, _code in _STORE_CLASS_SEAM_WITNESSES
        if _CANONICAL_GETATTR_STATIC(CollectorDeltaStore, name, None) is not expected
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture seam is class-rebound: " + ", ".join(rebound)
        )
    code_changed = _CANONICAL_SORTED(
        name
        for name, expected, code in _STORE_CLASS_SEAM_WITNESSES
        if _CANONICAL_GETATTR(
            _CANONICAL_GETATTR(expected, "__func__", expected),
            "__code__",
            None,
        )
        is not code
    )
    if code_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture seam code changed: "
            + ", ".join(code_changed)
        )
    global_changed = _CANONICAL_SORTED(
        name
        for name, function, function_globals, global_items
        in _STORE_CLASS_SEAM_GLOBAL_WITNESSES
        if (
            _CANONICAL_GETATTR(function, "__globals__", None)
            is not function_globals
            or any(
                function_globals.get(dependency_name) is not expected
                or _CANONICAL_GETATTR(expected, "__code__", None) is not code
                for dependency_name, expected, code in global_items
            )
        )
    )
    if global_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture global dispatch changed: "
            + ", ".join(global_changed)
        )
    module_attr_changed = _CANONICAL_SORTED(
        name + ":" + dependency_name + "." + attribute_name
        for (
            name,
            dependency_name,
            module,
            attribute_name,
            expected,
            code,
        ) in _STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES
        if (
            _CANONICAL_GETATTR(module, attribute_name, None) is not expected
            or _CANONICAL_GETATTR(expected, "__code__", None) is not code
        )
    )
    if module_attr_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture module dispatch changed: "
            + ", ".join(module_attr_changed)
        )
    builtin_changed = _CANONICAL_SORTED(
        name + ":" + builtin_name
        for (
            name,
            function_globals,
            builtin_module,
            builtin_name,
            expected,
        ) in _STORE_CLASS_SEAM_BUILTIN_WITNESSES
        if (
            builtin_name in function_globals
            or _CANONICAL_GETATTR(builtin_module, builtin_name, None)
            is not expected
        )
    )
    if builtin_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture builtin dispatch changed: "
            + ", ".join(builtin_changed)
        )
    rebound = _CANONICAL_SORTED(
        name
        for name, expected, _code in _EVIDENCE_CLASS_SEAM_WITNESSES
        if _CANONICAL_GETATTR_STATIC(CompleteGameBoardEvidenceStore, name, None)
        is not expected
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence campaign capture seam is class-rebound: "
            + ", ".join(rebound)
        )
    code_changed = _CANONICAL_SORTED(
        name
        for name, expected, code in _EVIDENCE_CLASS_SEAM_WITNESSES
        if _CANONICAL_GETATTR(
            _CANONICAL_GETATTR(expected, "__func__", expected),
            "__code__",
            None,
        )
        is not code
    )
    if code_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence campaign capture seam code changed: "
            + ", ".join(code_changed)
        )
    store_state = _CANONICAL_OBJECT_GETATTRIBUTE(store, "__dict__")
    rebound = _CANONICAL_SORTED(name for name in _STORE_SEAMS if name in store_state)
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector campaign capture seam is instance-rebound: " + ", ".join(rebound)
        )
    request_rebound = _CANONICAL_SORTED(
        name
        for name, expected in _PROVIDER_REQUEST_SEAM_ITEMS
        if _CANONICAL_GETATTR_STATIC(CompleteGameBoardRequest, name, None) is not expected
    )
    if request_rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider request authority seam is rebound: "
            + ", ".join(request_rebound)
        )
    snapshot_rebound = _CANONICAL_SORTED(
        name
        for name, expected in _PROVIDER_SNAPSHOT_SEAM_ITEMS
        if _CANONICAL_GETATTR_STATIC(CompleteGameBoardSnapshot, name, None) is not expected
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
    evidence_state = _CANONICAL_OBJECT_GETATTRIBUTE(evidence_store, "__dict__")
    rebound = _CANONICAL_SORTED(
        name for name in ("save",) if name in evidence_state
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "provider evidence campaign capture seam is instance-rebound: "
            + ", ".join(rebound)
        )


def _require_failure_terminal_seams(
    store: CollectorDeltaStore,
) -> None:
    """Validate only collector authority required to close an already-started failure.

    Provider evidence surfaces are intentionally excluded: a provider/evidence callback
    may be the thing that failed or was corrupted, but that must not strand an immutable
    collector START without a terminal receipt.
    """

    if _CANONICAL_TYPE(store) is not CollectorDeltaStore:
        raise _CANONICAL_TYPE_ERROR(
            "store must be the exact canonical CollectorDeltaStore"
        )
    rebound = _CANONICAL_SORTED(
        name
        for name, expected, _code in _STORE_CLASS_SEAM_WITNESSES
        if _CANONICAL_GETATTR_STATIC(CollectorDeltaStore, name, None) is not expected
    )
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector failure-terminal seam is class-rebound: " + ", ".join(rebound)
        )
    code_changed = _CANONICAL_SORTED(
        name
        for name, expected, code in _STORE_CLASS_SEAM_WITNESSES
        if _CANONICAL_GETATTR(
            _CANONICAL_GETATTR(expected, "__func__", expected),
            "__code__",
            None,
        )
        is not code
    )
    if code_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector failure-terminal seam code changed: "
            + ", ".join(code_changed)
        )
    global_changed = _CANONICAL_SORTED(
        name
        for name, function, function_globals, global_items
        in _STORE_CLASS_SEAM_GLOBAL_WITNESSES
        if (
            _CANONICAL_GETATTR(function, "__globals__", None)
            is not function_globals
            or any(
                function_globals.get(dependency_name) is not expected
                or _CANONICAL_GETATTR(expected, "__code__", None) is not code
                for dependency_name, expected, code in global_items
            )
        )
    )
    if global_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector failure-terminal global dispatch changed: "
            + ", ".join(global_changed)
        )
    module_attr_changed = _CANONICAL_SORTED(
        name + ":" + dependency_name + "." + attribute_name
        for (
            name,
            dependency_name,
            module,
            attribute_name,
            expected,
            code,
        ) in _STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES
        if (
            _CANONICAL_GETATTR(module, attribute_name, None) is not expected
            or _CANONICAL_GETATTR(expected, "__code__", None) is not code
        )
    )
    if module_attr_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector failure-terminal module dispatch changed: "
            + ", ".join(module_attr_changed)
        )
    builtin_changed = _CANONICAL_SORTED(
        name + ":" + builtin_name
        for (
            name,
            function_globals,
            builtin_module,
            builtin_name,
            expected,
        ) in _STORE_CLASS_SEAM_BUILTIN_WITNESSES
        if (
            builtin_name in function_globals
            or _CANONICAL_GETATTR(builtin_module, builtin_name, None)
            is not expected
        )
    )
    if builtin_changed:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector failure-terminal builtin dispatch changed: "
            + ", ".join(builtin_changed)
        )
    store_state = _CANONICAL_OBJECT_GETATTRIBUTE(store, "__dict__")
    rebound = _CANONICAL_SORTED(name for name in _STORE_SEAMS if name in store_state)
    if rebound:
        raise CampaignProviderCycleCaptureIntegrityError(
            "collector failure-terminal seam is instance-rebound: "
            + ", ".join(rebound)
        )


def _build_provider_evidence_campaign_scope_guard(
    *,
    locator_type,
    evidence_store_type,
    routing_items,
    evidence_directory_surface,
    object_getattribute,
    path_equal,
    path_join,
    getattr_static,
    type_fn,
    getattr_fn,
    any_fn,
    set_fn,
    dict_type,
    type_error,
    error_type,
):
    """Capture positive provider-routing authority outside mutable module globals."""

    routing_items = tuple(routing_items)
    routing_descriptors = dict_type(routing_items)
    workspace_descriptor = routing_descriptors["workspace"]
    authority_root_descriptor = routing_descriptors["authority_root"]
    path_equal_code = getattr_fn(path_equal, "__code__", None)
    path_join_code = getattr_fn(path_join, "__code__", None)
    getattr_static_code = getattr_fn(getattr_static, "__code__", None)
    getattr_static_globals = getattr_fn(getattr_static, "__globals__", None)
    if type_fn(getattr_static_globals) is not dict_type:
        raise error_type("provider evidence scope reflection globals unavailable")
    getattr_static_global_items = tuple(
        (
            name,
            getattr_static_globals[name],
            getattr_fn(getattr_static_globals[name], "__code__", None),
        )
        for name in getattr_static_code.co_names
        if name in getattr_static_globals
    )

    def require_provider_evidence_campaign_scope(
        precommit_locator: ForwardUniversePrecommitLocator,
        evidence_store: CompleteGameBoardEvidenceStore,
    ) -> None:
        """Bind provider evidence routing to the exact prospective campaign trust root."""

        if type_fn(precommit_locator) is not locator_type:
            raise type_error(
                "precommit_locator must be exact ForwardUniversePrecommitLocator"
            )
        if type_fn(evidence_store) is not evidence_store_type:
            raise type_error(
                "evidence_store must be the exact CompleteGameBoardEvidenceStore"
            )
        if (
            getattr_fn(path_equal, "__code__", None) is not path_equal_code
            or getattr_fn(path_join, "__code__", None) is not path_join_code
            or getattr_fn(getattr_static, "__code__", None) is not getattr_static_code
            or getattr_fn(getattr_static, "__globals__", None) is not getattr_static_globals
            or any_fn(
                getattr_static_globals.get(name) is not target
                or getattr_fn(target, "__code__", None) is not code
                for name, target, code in getattr_static_global_items
            )
        ):
            raise error_type("provider evidence scope authority executable changed")
        if (
            getattr_static(
                evidence_store_type,
                "DIRECTORY",
                None,
            )
            is not evidence_directory_surface
        ):
            raise error_type("provider evidence directory authority changed")
        for name, descriptor in routing_items:
            if getattr_static(locator_type, name, None) is not descriptor:
                raise error_type(
                    "campaign precommit routing surface changed: " + name
                )

        locator_workspace = workspace_descriptor.__get__(
            precommit_locator,
            locator_type,
        )
        locator_authority_root = authority_root_descriptor.__get__(
            precommit_locator,
            locator_type,
        )
        state = object_getattribute(evidence_store, "__dict__")
        if type_fn(state) is not dict_type:
            raise error_type("provider evidence store routing state is unavailable")
        if set_fn(("workspace", "root", "authority_root")) - set_fn(state):
            raise error_type("provider evidence store routing state is incomplete")
        workspace = state["workspace"]
        root = state["root"]
        authority_root = state["authority_root"]

        if (
            type_fn(locator_workspace) is not type_fn(workspace)
            or path_equal(workspace, locator_workspace) is not True
        ):
            raise error_type(
                "provider evidence workspace does not match campaign precommit workspace"
            )
        expected_root = path_join(workspace, evidence_directory_surface)
        if (
            type_fn(root) is not type_fn(expected_root)
            or path_equal(root, expected_root) is not True
        ):
            raise error_type(
                "provider evidence root does not match canonical campaign workspace"
            )
        if locator_authority_root is None:
            if authority_root is not None:
                raise error_type(
                    "provider evidence authority root does not match campaign precommit authority"
                )
        elif (
            type_fn(authority_root) is not type_fn(locator_authority_root)
            or path_equal(authority_root, locator_authority_root) is not True
        ):
            raise error_type(
                "provider evidence authority root does not match campaign precommit authority"
            )

    return require_provider_evidence_campaign_scope


_require_provider_evidence_campaign_scope = (
    _build_provider_evidence_campaign_scope_guard(
        locator_type=ForwardUniversePrecommitLocator,
        evidence_store_type=CompleteGameBoardEvidenceStore,
        routing_items=_PRECOMMIT_ROUTING_SEAM_ITEMS,
        evidence_directory_surface=_EVIDENCE_DIRECTORY_SURFACE,
        object_getattribute=_CANONICAL_OBJECT_GETATTRIBUTE,
        path_equal=_CANONICAL_PATH_EQUALITY,
        path_join=_CANONICAL_PATH_JOIN,
        getattr_static=_CANONICAL_GETATTR_STATIC,
        type_fn=_CANONICAL_TYPE,
        getattr_fn=_CANONICAL_GETATTR,
        any_fn=any,
        set_fn=set,
        dict_type=dict,
        type_error=TypeError,
        error_type=CampaignProviderCycleCaptureIntegrityError,
    )
)
del _build_provider_evidence_campaign_scope_guard


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
        if _CANONICAL_TYPE(payload) is not dict or set(payload) != set(_RECEIPT_FIELD_NAMES):
            raise CampaignProviderCycleCaptureIntegrityError(
                "campaign cycle receipt payload is noncanonical"
            )
        instance = object.__new__(CampaignCompleteBoardCycleReceipt)
        for name in _RECEIPT_FIELD_NAMES:
            object.__setattr__(instance, name, payload[name])
        return instance

    def to_dict(self) -> dict[str, object]:
        return {
            name: _CANONICAL_GETATTR(self, name)
            for name in _RECEIPT_FIELD_NAMES
        }


_RECEIPT_FIELD_NAMES = tuple(
    CampaignCompleteBoardCycleReceipt.__dataclass_fields__
)
_RECEIPT_FIELD_DESCRIPTORS = tuple(
    (
        name,
        _CANONICAL_GETATTR_STATIC(CampaignCompleteBoardCycleReceipt, name),
    )
    for name in _RECEIPT_FIELD_NAMES
)

_CANONICAL_CYCLE_RECEIPT_CLASS = CampaignCompleteBoardCycleReceipt
_CANONICAL_CYCLE_RECEIPT_ISSUER = _CANONICAL_GETATTR_STATIC(
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
        _CANONICAL_GETATTR_STATIC(CampaignInceptionReceipt, name),
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
    _public_surface_guard: Callable[[], None] | None = None,
) -> tuple[CompleteGameBoardSnapshot, CampaignCompleteBoardCycleReceipt]:
    """Capture one provider board only after the exact campaign START is authorized."""

    integrity_error = CampaignProviderCycleCaptureIntegrityError
    artifact_kind = ARTIFACT_KIND
    if _public_surface_guard is None:
        raise integrity_error(
            "campaign provider-cycle sealed public surface guard is required"
        )

    def require_public_surface() -> None:
        _public_surface_guard()

    require_public_surface()
    type_error = TypeError
    value_error = ValueError
    overflow_error = OverflowError
    base_exception = BaseException
    dict_type = dict
    int_type = int
    repr_fn = repr
    if _CANONICAL_TYPE(source_spec) is not CampaignInceptionSourceSpec:
        raise type_error("source_spec must be exact CampaignInceptionSourceSpec")
    if _CANONICAL_TYPE(request) is not CompleteGameBoardRequest:
        raise type_error("request must be exact CompleteGameBoardRequest")
    if not _CANONICAL_CALLABLE(clock):
        raise type_error("clock must be callable")
    test_clock_origin = (
        _CANONICAL_TEST_CAMPAIGN_CLOCK_ORIGIN.get()
        is _CANONICAL_TEST_CAMPAIGN_CLOCK_CAPABILITY
    )
    if not test_clock_origin and clock is not _CANONICAL_CAMPAIGN_CLOCK:
        raise integrity_error(
            "campaign collector clock must be product-owned"
        )
    effective_clock = clock if test_clock_origin else _CANONICAL_CAMPAIGN_CLOCK
    if request.source_id != source_spec.source_id:
        raise integrity_error(
            "provider request source_id does not match campaign collector source"
        )
    require_seams = _require_canonical_seams
    require_failure_terminal_seams = _require_failure_terminal_seams
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
    module_globals = _CANONICAL_MODULE_GLOBALS
    expected_dispatch = (
        ("_require_canonical_seams", require_seams),
        ("_require_failure_terminal_seams", require_failure_terminal_seams),
        ("_require_provider_evidence_campaign_scope", require_evidence_scope),
        ("_text", _text),
        ("_sha", _sha),
        ("_digest", _digest),
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
        (name, target, _CANONICAL_GETATTR(_CANONICAL_GETATTR(target, "__func__", target), "__code__", None))
        for name, target in expected_dispatch
    )
    expected_finish_code = _CANONICAL_GETATTR(
        _CANONICAL_GETATTR(finish_cycle, "__func__", finish_cycle),
        "__code__",
        None,
    )
    expected_failure_terminal_seams_code = _CANONICAL_GETATTR(
        _CANONICAL_GETATTR(
            require_failure_terminal_seams,
            "__func__",
            require_failure_terminal_seams,
        ),
        "__code__",
        None,
    )
    expected_inspect = inspect
    expected_getattr_static = _CANONICAL_GETATTR_STATIC
    expected_getattr_static_code = _CANONICAL_GETATTR_STATIC_CODE
    expected_getattr_static_globals = _CANONICAL_GETATTR_STATIC_GLOBALS
    expected_getattr_static_global_items = _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
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
    expected_request_type = CompleteGameBoardRequest
    expected_snapshot_type = CompleteGameBoardSnapshot
    expected_evidence_store_type = CompleteGameBoardEvidenceStore
    expected_evidence_class_seam_witnesses = _EVIDENCE_CLASS_SEAM_WITNESSES
    expected_provider_request_seams = _PROVIDER_REQUEST_SEAMS
    expected_provider_request_seam_items = _PROVIDER_REQUEST_SEAM_ITEMS
    expected_provider_snapshot_seams = _PROVIDER_SNAPSHOT_SEAMS
    expected_provider_snapshot_seam_items = _PROVIDER_SNAPSHOT_SEAM_ITEMS
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
    expected_integrity_error = integrity_error
    expected_collector_store_type = CollectorDeltaStore
    expected_store_seams = _STORE_SEAMS
    expected_store_class_seam_witnesses = _STORE_CLASS_SEAM_WITNESSES
    expected_store_class_seam_global_witnesses = (
        _STORE_CLASS_SEAM_GLOBAL_WITNESSES
    )
    expected_store_class_seam_module_attr_witnesses = (
        _STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES
    )
    expected_object_getattribute = _CANONICAL_OBJECT_GETATTRIBUTE
    expected_artifact_kind = artifact_kind
    expected_schema_version = _SCHEMA_VERSION
    expected_hex = _HEX
    expected_type = _CANONICAL_TYPE
    expected_callable = _CANONICAL_CALLABLE
    expected_getattr = _CANONICAL_GETATTR
    expected_sorted = _CANONICAL_SORTED
    expected_type_error_alias = _CANONICAL_TYPE_ERROR
    expected_unshadowed_builtins = ("any", "len", "set", "sorted", "tuple")

    def require_stable_dispatch() -> None:
        require_public_surface()
        if (
            module_globals.get("CampaignProviderCycleCaptureIntegrityError")
            is not expected_integrity_error
        ):
            raise integrity_error(
                "campaign provider-cycle integrity error authority changed"
            )
        if (
            module_globals.get("CollectorDeltaStore")
            is not expected_collector_store_type
            or module_globals.get("_STORE_SEAMS") is not expected_store_seams
            or module_globals.get("_STORE_CLASS_SEAM_WITNESSES")
            is not expected_store_class_seam_witnesses
            or module_globals.get("_STORE_CLASS_SEAM_GLOBAL_WITNESSES")
            is not expected_store_class_seam_global_witnesses
            or module_globals.get("_STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES")
            is not expected_store_class_seam_module_attr_witnesses
            or module_globals.get("_CANONICAL_OBJECT_GETATTRIBUTE")
            is not expected_object_getattribute
        ):
            raise integrity_error(
                "campaign provider-cycle collector seam authority changed"
            )
        if (
            module_globals.get("CompleteGameBoardRequest") is not expected_request_type
            or module_globals.get("CompleteGameBoardSnapshot") is not expected_snapshot_type
            or module_globals.get("CompleteGameBoardEvidenceStore")
            is not expected_evidence_store_type
            or module_globals.get("_EVIDENCE_CLASS_SEAM_WITNESSES")
            is not expected_evidence_class_seam_witnesses
            or module_globals.get("_PROVIDER_REQUEST_SEAM_ITEMS")
            is not expected_provider_request_seam_items
            or module_globals.get("_PROVIDER_SNAPSHOT_SEAM_ITEMS")
            is not expected_provider_snapshot_seam_items
            or tuple(expected_provider_request_seams.items())
            != expected_provider_request_seam_items
            or tuple(expected_provider_snapshot_seams.items())
            != expected_provider_snapshot_seam_items
        ):
            raise integrity_error(
                "campaign provider-cycle provider/evidence seam authority changed"
            )
        if module_globals.get("ARTIFACT_KIND") != expected_artifact_kind:
            raise integrity_error(
                "campaign provider-cycle artifact kind authority changed"
            )
        if (
            module_globals.get("_SCHEMA_VERSION") != expected_schema_version
            or module_globals.get("_HEX") is not expected_hex
        ):
            raise integrity_error(
                "campaign provider-cycle schema/hash authority changed"
            )
        for builtin_name in expected_unshadowed_builtins:
            if builtin_name in module_globals:
                raise integrity_error(
                    "campaign provider-cycle builtin dispatch shadowed: "
                    + builtin_name
                )
        if (
            module_globals.get("_CANONICAL_TYPE") is not expected_type
            or module_globals.get("_CANONICAL_CALLABLE") is not expected_callable
            or module_globals.get("_CANONICAL_GETATTR") is not expected_getattr
            or module_globals.get("_CANONICAL_SORTED") is not expected_sorted
            or module_globals.get("_CANONICAL_TYPE_ERROR") is not expected_type_error_alias
        ):
            raise integrity_error(
                "campaign provider-cycle builtin dispatch changed"
            )
        if (
            module_globals.get("inspect") is not expected_inspect
            or expected_inspect.getattr_static is not expected_getattr_static
            or expected_getattr_static.__code__ is not expected_getattr_static_code
            or expected_getattr_static.__globals__ is not expected_getattr_static_globals
            or any(
                expected_getattr_static_globals.get(name) is not target
                or _CANONICAL_GETATTR(target, "__code__", None) is not code
                for name, target, code in expected_getattr_static_global_items
            )
        ):
            raise integrity_error(
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
            raise integrity_error(
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
            raise integrity_error(
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
            raise integrity_error(
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
            raise integrity_error(
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
            raise integrity_error(
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
            raise integrity_error(
                "campaign provider evidence routing witness changed mid-capture"
            )
        for name, target, code in expected_codes:
            if module_globals.get(name) is not target:
                raise integrity_error(
                    "campaign provider-cycle dispatch authority is rebound: " + name
                )
            function = _CANONICAL_GETATTR(target, "__func__", target)
            if _CANONICAL_GETATTR(function, "__code__", None) is not code:
                raise integrity_error(
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
        _CANONICAL_TYPE(slot) is not dict_type
        or slot.get("schedule_id") != campaign.schedule_id
        or slot.get("stream_epoch") != source_spec.stream_epoch
        or slot.get("max_items") != source_spec.max_items
        or _CANONICAL_TYPE(slot.get("slot_ordinal")) is not int_type
        or slot.get("slot_ordinal") < source_spec.evaluation_start_slot_ordinal
        or slot.get("slot_ordinal") > source_spec.evaluation_end_slot_ordinal
    ):
        raise integrity_error(
            "campaign collector next slot does not match inception receipt"
        )
    raw_attempted_at = effective_clock()
    require_public_surface()
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
            interval_seconds=repr_fn(source_spec.interval_seconds),
            slot_ordinal=slot.get("slot_ordinal") + 1,
        )
        slot_deadline_instant = datetime.fromisoformat(
            instant(slot_deadline_at, "collector next slot due_at")
        )
    except (overflow_error, type_error, value_error) as exc:
        raise integrity_error(
            "collector fixed schedule slot window is not representable"
        ) from exc
    if (
        attempted_instant < slot_due_instant
        or attempted_instant >= slot_deadline_instant
    ):
        raise integrity_error(
            "collector START falls outside precommitted fixed schedule slot window"
        )
    campaign_not_before = datetime.fromisoformat(
        instant(campaign.observation_not_before, "campaign observation_not_before")
    )
    campaign_not_after = datetime.fromisoformat(
        instant(campaign.observation_not_after, "campaign observation_not_after")
    )
    if attempted_instant < campaign_not_before or attempted_instant > campaign_not_after:
        raise integrity_error(
            "collector START falls outside precommitted campaign observation window"
        )
    require_public_surface()
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
    except (type_error, value_error) as exc:
        raise integrity_error(
            "cannot reserve exact campaign collector START before provider I/O"
        ) from exc

    terminal_written = False
    try:
        snapshot = provider_capture(
            api_key=api_key,
            request=request,
            timeout_seconds=timeout_seconds,
        )
        require_public_surface()
        require_stable_dispatch()
        if _CANONICAL_TYPE(snapshot) is not CompleteGameBoardSnapshot:
            raise integrity_error(
                "provider capture returned noncanonical snapshot type"
            )
        provider_captured_at = instant(
            snapshot.captured_at,
            "provider captured_at",
        )
        provider_captured_instant = datetime.fromisoformat(provider_captured_at)
        if provider_captured_instant < attempted_instant:
            raise integrity_error(
                "provider observation predates authorized collector START"
            )
        if provider_captured_instant >= slot_deadline_instant:
            raise integrity_error(
                "provider observation falls outside precommitted fixed schedule slot window"
            )
        raw_completed_at = effective_clock()
        require_public_surface()
        require_stable_dispatch()
        require_seams(store, evidence_store)
        completed_at = instant(raw_completed_at, "collector completed_at")
        completed_instant = datetime.fromisoformat(completed_at)
        if completed_instant < provider_captured_instant:
            raise integrity_error(
                "provider observation falls after collector cycle completion"
            )
        if (
            provider_captured_instant < campaign_not_before
            or provider_captured_instant > campaign_not_after
            or completed_instant > campaign_not_after
        ):
            raise integrity_error(
                "provider cycle falls outside precommitted campaign observation window"
            )
        require_public_surface()
        evidence_path = evidence_save(evidence_store, snapshot)
        require_public_surface()
        repeated_path = evidence_save(evidence_store, snapshot)
        require_public_surface()
        require_stable_dispatch()
        require_seams(store, evidence_store)
        expected_evidence_path = evidence_path_for(
            evidence_store,
            snapshot.evidence_sha256,
        )
        if (
            _CANONICAL_TYPE(evidence_path) is not _CANONICAL_TYPE(expected_evidence_path)
            or _CANONICAL_TYPE(repeated_path) is not _CANONICAL_TYPE(expected_evidence_path)
            or path_equal(evidence_path, repeated_path) is not True
            or path_equal(evidence_path, expected_evidence_path) is not True
        ):
            raise integrity_error(
                "provider evidence store did not retain exact captured identity"
            )
        require_public_surface()
        artifact = record_artifact(
            store,
            source_id=source_spec.source_id,
            cycle_seq=cycle_seq,
            artifact_kind=artifact_kind,
            artifact_sha256=snapshot.evidence_sha256,
        )
        require_public_surface()
        if (
            _CANONICAL_TYPE(artifact) is not dict_type
            or artifact.get("artifact_kind") != artifact_kind
            or artifact.get("artifact_sha256") != snapshot.evidence_sha256
        ):
            raise integrity_error(
                "collector artifact append returned noncanonical identity"
            )
        require_public_surface()
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
        require_public_surface()
        collector_evidence = resolve_artifact(
            store,
            source_id=source_spec.source_id,
            cycle_seq=cycle_seq,
            artifact_kind=artifact_kind,
            artifact_sha256=snapshot.evidence_sha256,
        )
        require_public_surface()
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
            or collector_evidence.get("artifact_kind") != artifact_kind
            or collector_evidence.get("artifact_sha256") != snapshot.evidence_sha256
        ):
            raise integrity_error(
                "collector artifact evidence does not bind exact campaign authority"
            )
        require_public_surface()
        require_stable_dispatch()
        require_seams(store, evidence_store)
        require_public_surface()
        receipt = issue_receipt(
            campaign=campaign,
            snapshot=snapshot,
            collector_evidence=collector_evidence,
        )
        require_public_surface()
        return snapshot, receipt
    except base_exception as exc:
        if not terminal_written:
            try:
                finish_function = _CANONICAL_GETATTR(finish_cycle, "__func__", finish_cycle)
                require_failure_terminal_seams_function = _CANONICAL_GETATTR(
                    require_failure_terminal_seams,
                    "__func__",
                    require_failure_terminal_seams,
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
                    or module_globals.get("_require_failure_terminal_seams")
                    is not require_failure_terminal_seams
                    or _CANONICAL_GETATTR(
                        require_failure_terminal_seams_function,
                        "__code__",
                        None,
                    )
                    is not expected_failure_terminal_seams_code
                    or module_globals.get("CampaignProviderCycleCaptureIntegrityError")
                    is not expected_integrity_error
                    or module_globals.get("CollectorDeltaStore")
                    is not expected_collector_store_type
                    or module_globals.get("_CANONICAL_TYPE") is not expected_type
                    or module_globals.get("_CANONICAL_GETATTR") is not expected_getattr
                    or module_globals.get("_CANONICAL_GETATTR_STATIC")
                    is not expected_getattr_static
                    or module_globals.get("_STORE_SEAMS") is not expected_store_seams
                    or module_globals.get("_STORE_CLASS_SEAM_WITNESSES")
                    is not expected_store_class_seam_witnesses
                    or module_globals.get("_CANONICAL_OBJECT_GETATTRIBUTE")
                    is not expected_object_getattribute
                    or module_globals.get("_CANONICAL_SORTED") is not expected_sorted
                    or module_globals.get("_CANONICAL_TYPE_ERROR")
                    is not expected_type_error_alias
                    or module_globals.get("_FINISH_CYCLE") is not finish_cycle
                    or _CANONICAL_GETATTR(finish_function, "__code__", None)
                    is not expected_finish_code
                ):
                    raise integrity_error(
                        "campaign provider-cycle failure terminal dispatch changed"
                    )
                require_failure_terminal_seams(store)
                finish_cycle(
                    store,
                    source_id=source_spec.source_id,
                    cycle_seq=cycle_seq,
                    status="LOCAL_FAILURE",
                    completed_at=attempted_at,
                    catalog_changes=(),
                    observed_delta_ids=(),
                    committed_delta_ids=(),
                    duplicate_delta_ids=(),
                    error_code=_CANONICAL_TYPE(exc).__name__,
                )
            except base_exception as terminal_error:
                try:
                    exc.add_note(
                        "collector campaign capture failure terminal also failed: "
                        f"{_CANONICAL_TYPE(terminal_error).__name__}: {terminal_error}"
                    )
                except base_exception:
                    pass
        raise


def _seal_campaign_provider_cycle_capture_dispatch() -> None:
    """Seal authority-bearing capture dispatch against runtime rebinding."""

    module_globals = _CANONICAL_MODULE_GLOBALS
    expected_error = CampaignProviderCycleCaptureIntegrityError
    expected_type_error = TypeError
    expected_capture = capture_campaign_complete_game_board
    expected_capture_code = expected_capture.__code__
    expected_inspect = inspect
    expected_getattr_static = _CANONICAL_GETATTR_STATIC
    expected_getattr_static_code = _CANONICAL_GETATTR_STATIC_CODE
    expected_getattr_static_globals = _CANONICAL_GETATTR_STATIC_GLOBALS
    expected_getattr_static_global_items = _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS
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
        "_PROVIDER_REQUEST_SEAM_ITEMS": _PROVIDER_REQUEST_SEAM_ITEMS,
        "_PROVIDER_SNAPSHOT_SEAMS": _PROVIDER_SNAPSHOT_SEAMS,
        "_PROVIDER_SNAPSHOT_SEAM_ITEMS": _PROVIDER_SNAPSHOT_SEAM_ITEMS,
        "_EVIDENCE_CLASS_SEAM_WITNESSES": _EVIDENCE_CLASS_SEAM_WITNESSES,
        "_PROVIDER_CANONICAL_ASSERT": _PROVIDER_CANONICAL_ASSERT,
        "_PROVIDER_CANONICAL_ASSERT_CODE": _PROVIDER_CANONICAL_ASSERT_CODE,
        "_EVIDENCE_PATH": _EVIDENCE_PATH,
        "_CANONICAL_PATH_EQUALITY": _CANONICAL_PATH_EQUALITY,
        "_CANONICAL_PATH_EQUALITY_CODE": _CANONICAL_PATH_EQUALITY_CODE,
        "_CANONICAL_PATH_JOIN": _CANONICAL_PATH_JOIN,
        "_CANONICAL_PATH_JOIN_CODE": _CANONICAL_PATH_JOIN_CODE,
        "_CANONICAL_OBJECT_GETATTRIBUTE": _CANONICAL_OBJECT_GETATTRIBUTE,
        "_CANONICAL_MODULE_GLOBALS": _CANONICAL_MODULE_GLOBALS,
        "_CANONICAL_TYPE": _CANONICAL_TYPE,
        "_CANONICAL_CALLABLE": _CANONICAL_CALLABLE,
        "_CANONICAL_GETATTR": _CANONICAL_GETATTR,
        "_CANONICAL_SORTED": _CANONICAL_SORTED,
        "_CANONICAL_TYPE_ERROR": _CANONICAL_TYPE_ERROR,
        "_CANONICAL_GETATTR_STATIC": _CANONICAL_GETATTR_STATIC,
        "_CANONICAL_GETATTR_STATIC_CODE": _CANONICAL_GETATTR_STATIC_CODE,
        "_CANONICAL_GETATTR_STATIC_GLOBALS": _CANONICAL_GETATTR_STATIC_GLOBALS,
        "_CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS": _CANONICAL_GETATTR_STATIC_GLOBAL_ITEMS,
        "_STORE_SEAMS": _STORE_SEAMS,
        "_STORE_CLASS_SEAM_WITNESSES": _STORE_CLASS_SEAM_WITNESSES,
        "_STORE_CLASS_SEAM_GLOBAL_WITNESSES": _STORE_CLASS_SEAM_GLOBAL_WITNESSES,
        "_STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES": _STORE_CLASS_SEAM_MODULE_ATTR_WITNESSES,
        "_EVIDENCE_DIRECTORY": _EVIDENCE_DIRECTORY,
        "_EVIDENCE_DIRECTORY_SURFACE": _EVIDENCE_DIRECTORY_SURFACE,
        "_PRECOMMIT_ROUTING_SEAMS": _PRECOMMIT_ROUTING_SEAMS,
        "_PRECOMMIT_ROUTING_SEAM_ITEMS": _PRECOMMIT_ROUTING_SEAM_ITEMS,
    }
    expected_callables = tuple(
        (
            name,
            target,
            _CANONICAL_GETATTR(_CANONICAL_GETATTR(target, "__func__", target), "__code__", None),
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
            ("_require_failure_terminal_seams", _require_failure_terminal_seams),
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
    expected_issue_function = _CANONICAL_GETATTR(
        expected_issue_surface,
        "__func__",
        expected_issue_surface,
    )
    expected_issue_code = _CANONICAL_GETATTR(expected_issue_function, "__code__", None)

    def require_dispatch_integrity() -> None:
        if (
            "any" in module_globals
            or "len" in module_globals
            or "set" in module_globals
            or "sorted" in module_globals
            or "tuple" in module_globals
            or "TypeError" in module_globals
            or "ValueError" in module_globals
            or "OverflowError" in module_globals
            or "BaseException" in module_globals
            or "dict" in module_globals
            or "int" in module_globals
            or "repr" in module_globals
        ):
            raise expected_error(
                "campaign provider-cycle builtin dispatch shadowed"
            )
        for name, expected in expected_values.items():
            if module_globals.get(name) is not expected:
                raise expected_error(
                    "campaign provider-cycle dispatch authority is rebound: " + name
                )
        if (
            expected_inspect.getattr_static is not expected_getattr_static
            or expected_getattr_static.__code__ is not expected_getattr_static_code
            or expected_getattr_static.__globals__ is not expected_getattr_static_globals
            or any(
                expected_getattr_static_globals.get(name) is not target
                or _CANONICAL_GETATTR(target, "__code__", None) is not code
                for name, target, code in expected_getattr_static_global_items
            )
        ):
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
            function = _CANONICAL_GETATTR(expected, "__func__", expected)
            if _CANONICAL_GETATTR(function, "__code__", None) is not code:
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
        current_issue_function = _CANONICAL_GETATTR(
            current_issue_surface,
            "__func__",
            current_issue_surface,
        )
        if (
            current_issue_function is not expected_issue_function
            or _CANONICAL_GETATTR(current_issue_function, "__code__", None)
            is not expected_issue_code
        ):
            raise expected_error(
                "campaign provider-cycle receipt issuer code changed"
            )
        if expected_capture.__code__ is not expected_capture_code:
            raise expected_error(
                "campaign provider-cycle capture implementation changed"
            )

    def require_public_capture_surface() -> None:
        if (
            module_globals.get("capture_campaign_complete_game_board")
            is not sealed_capture_campaign_complete_game_board
        ):
            raise expected_error(
                "campaign provider-cycle public capture surface changed"
            )

    def sealed_capture_campaign_complete_game_board(*args, **kwargs):
        if "_public_surface_guard" in kwargs:
            raise expected_type_error(
                "_public_surface_guard is private to the sealed campaign capture"
            )
        require_public_capture_surface()
        require_dispatch_integrity()
        result = expected_capture(
            *args,
            _public_surface_guard=require_public_capture_surface,
            **kwargs,
        )
        require_dispatch_integrity()
        require_public_capture_surface()
        return result

    sealed_capture_campaign_complete_game_board.__name__ = expected_capture.__name__
    sealed_capture_campaign_complete_game_board.__qualname__ = expected_capture.__qualname__
    sealed_capture_campaign_complete_game_board.__doc__ = expected_capture.__doc__
    if hasattr(sealed_capture_campaign_complete_game_board, "__wrapped__"):
        raise RuntimeError(
            "campaign provider-cycle capture seal must not expose unsealed delegate"
        )
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
