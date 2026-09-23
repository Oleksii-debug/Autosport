from __future__ import annotations

import json
import os
import platform
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_HKLM_SUBKEY = (
    rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
WEBVIEW2_HKCU_SUBKEY = (
    rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
MICROSOFT_WEBVIEW2_DISTRIBUTION_DOC = (
    "https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution"
)
MICROSOFT_WOW64_REGISTRY_VIEW_DOC = (
    "https://learn.microsoft.com/windows/win32/winprog64/accessing-an-alternate-registry-view"
)

WEBVIEW2_RELEASE_ENVIRONMENT_OVERRIDES = (
    "PYWEBVIEW_GUI",
    "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
    "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
    "WEBVIEW2_RELEASE_CHANNEL_PREFERENCE",
    "WEBVIEW2_RELEASE_CHANNELS",
    "WEBVIEW2_CHANNEL_SEARCH_KIND",
    "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER",
    "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
)

_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class WebView2RuntimeStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    INVALID_REGISTRATION = "INVALID_REGISTRATION"
    BELOW_EXPLICIT_MINIMUM = "BELOW_EXPLICIT_MINIMUM"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"


class RegistryObservationStatus(str, Enum):
    VALID = "VALID"
    MISSING = "MISSING"
    INVALID = "INVALID"
    ERROR = "ERROR"


class RegistryView(str, Enum):
    PROCESS_DEFAULT = "PROCESS_DEFAULT"
    WOW64_32 = "WOW64_32"


class WebView2ReleaseEnvironmentError(RuntimeError):
    """Unsupported process environment can redirect the qualified WebView2 runtime."""


@dataclass(frozen=True)
class WebView2ReleaseEnvironment:
    blocked_names: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.blocked_names

    def to_dict(self) -> dict[str, object]:
        return {
            "blocked_names": list(self.blocked_names),
            "kind": "webview2_release_environment",
            "safe": self.safe,
            "schema_version": 1,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


@dataclass(frozen=True)
class RegistryTarget:
    hive: str
    subkey: str
    view: RegistryView

    def __post_init__(self) -> None:
        if self.hive not in {"HKLM", "HKCU"}:
            raise ValueError("registry target hive must be HKLM or HKCU")
        if type(self.subkey) is not str or not self.subkey:
            raise ValueError("registry target subkey must be non-empty text")
        if type(self.view) is not RegistryView:
            raise TypeError("registry target view must be exact RegistryView")


@dataclass(frozen=True)
class RegistryRead:
    """Raw result of one documented WebView2 registration lookup."""

    key_present: bool
    value_present: bool
    value: object | None = None
    value_type: int | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class RegistryObservation:
    hive: str
    subkey: str
    registry_view: RegistryView
    status: RegistryObservationStatus
    version: str | None
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "hive": self.hive,
            "reason": self.reason,
            "registry_view": self.registry_view.value,
            "status": self.status.value,
            "subkey": self.subkey,
            "version": self.version,
        }


@dataclass(frozen=True)
class WebView2RuntimePreflight:
    status: WebView2RuntimeStatus
    selected_version: str | None
    minimum_version: str | None
    windows_64bit: bool | None
    observations: tuple[RegistryObservation, ...]

    @property
    def available(self) -> bool:
        return self.status is WebView2RuntimeStatus.AVAILABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "documentation": {
                "registry_views": MICROSOFT_WOW64_REGISTRY_VIEW_DOC,
                "webview2_distribution": MICROSOFT_WEBVIEW2_DISTRIBUTION_DOC,
            },
            "kind": "webview2_evergreen_runtime_preflight",
            "minimum_version": self.minimum_version,
            "observations": [item.to_dict() for item in self.observations],
            "schema_version": 2,
            "selected_version": self.selected_version,
            "status": self.status.value,
            "windows_64bit": self.windows_64bit,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def evaluate_webview2_release_environment(
    environment: Mapping[str, str] | None = None,
) -> WebView2ReleaseEnvironment:
    """Inspect only release-relevant override names and never retain their values."""

    source = os.environ if environment is None else environment
    blocked: list[str] = []
    for name in WEBVIEW2_RELEASE_ENVIRONMENT_OVERRIDES:
        value = source.get(name)
        if value is None or value == "":
            continue
        if type(value) is not str:
            raise TypeError("WebView2 release environment values must be text")
        blocked.append(name)
    return WebView2ReleaseEnvironment(blocked_names=tuple(blocked))


def require_webview2_release_environment(
    environment: Mapping[str, str] | None = None,
) -> WebView2ReleaseEnvironment:
    """Fail closed before probing/installing a runtime under unsupported overrides."""

    result = evaluate_webview2_release_environment(environment)
    if not result.safe:
        names = ", ".join(result.blocked_names)
        raise WebView2ReleaseEnvironmentError(
            "Unsupported WebView2 release environment override(s): " + names
        )
    return result


def _parse_version(value: str, *, allow_zero: bool) -> tuple[int, int, int, int]:
    if type(value) is not str or _VERSION_RE.fullmatch(value) is None:
        raise ValueError("version must be canonical four-part decimal text")
    parsed = tuple(int(part) for part in value.split("."))
    if not allow_zero and parsed == (0, 0, 0, 0):
        raise ValueError("runtime version must be greater than 0.0.0.0")
    return parsed  # type: ignore[return-value]


def _is_64bit_windows() -> bool:
    """Detect OS bitness, including a 32-bit Python process on 64-bit Windows."""

    arch_w6432 = os.environ.get("PROCESSOR_ARCHITEW6432", "").upper()
    arch = os.environ.get("PROCESSOR_ARCHITECTURE", "").upper()
    if arch_w6432 in {"AMD64", "IA64", "ARM64"}:
        return True
    if arch in {"AMD64", "IA64", "ARM64"}:
        return True
    machine = platform.machine().upper()
    if machine in {"AMD64", "X86_64", "IA64", "ARM64", "AARCH64"}:
        return True
    return sys.maxsize > 2**32


def _registry_targets(*, windows_64bit: bool) -> tuple[RegistryTarget, ...]:
    """Return the documented machine-first lookup order with explicit registry view."""

    if type(windows_64bit) is not bool:
        raise TypeError("windows_64bit must be bool")
    machine_view = (
        RegistryView.WOW64_32 if windows_64bit else RegistryView.PROCESS_DEFAULT
    )
    return (
        RegistryTarget("HKLM", WEBVIEW2_HKLM_SUBKEY, machine_view),
        RegistryTarget("HKCU", WEBVIEW2_HKCU_SUBKEY, RegistryView.PROCESS_DEFAULT),
    )


def _read_registry_pv(target: RegistryTarget) -> RegistryRead:
    """Read Microsoft's documented pv value without requiring elevated access."""

    if type(target) is not RegistryTarget:
        raise TypeError("target must be exact RegistryTarget")
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - protected by platform gate
        return RegistryRead(False, False, error_type=type(exc).__name__)

    root = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
    }[target.hive]

    access = winreg.KEY_READ
    if target.view is RegistryView.WOW64_32:
        view_flag = getattr(winreg, "KEY_WOW64_32KEY", None)
        if isinstance(view_flag, bool) or not isinstance(view_flag, int):
            return RegistryRead(
                False,
                False,
                error_type="WOW64RegistryViewUnavailable",
            )
        access |= view_flag

    try:
        key = winreg.OpenKey(root, target.subkey, 0, access)
    except FileNotFoundError:
        return RegistryRead(False, False)
    except OSError as exc:
        return RegistryRead(False, False, error_type=type(exc).__name__)

    try:
        with key:
            try:
                value, value_type = winreg.QueryValueEx(key, "pv")
            except FileNotFoundError:
                return RegistryRead(True, False)
            except OSError as exc:
                return RegistryRead(True, False, error_type=type(exc).__name__)
    except OSError as exc:
        return RegistryRead(True, False, error_type=type(exc).__name__)

    return RegistryRead(True, True, value=value, value_type=value_type)


def _classify_registry_read(
    target: RegistryTarget,
    read: RegistryRead,
    *,
    reg_sz_type: int,
) -> RegistryObservation:
    if type(target) is not RegistryTarget:
        raise TypeError("target must be exact RegistryTarget")
    if type(read) is not RegistryRead:
        raise TypeError("read must be exact RegistryRead")
    if read.error_type is not None:
        return RegistryObservation(
            hive=target.hive,
            subkey=target.subkey,
            registry_view=target.view,
            status=RegistryObservationStatus.ERROR,
            version=None,
            reason=read.error_type,
        )
    if not read.key_present:
        return RegistryObservation(
            hive=target.hive,
            subkey=target.subkey,
            registry_view=target.view,
            status=RegistryObservationStatus.MISSING,
            version=None,
            reason="registry_key_missing",
        )
    if not read.value_present:
        return RegistryObservation(
            hive=target.hive,
            subkey=target.subkey,
            registry_view=target.view,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_missing",
        )
    if read.value_type != reg_sz_type:
        return RegistryObservation(
            hive=target.hive,
            subkey=target.subkey,
            registry_view=target.view,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_not_reg_sz",
        )
    if type(read.value) is not str:
        return RegistryObservation(
            hive=target.hive,
            subkey=target.subkey,
            registry_view=target.view,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_not_text",
        )
    try:
        _parse_version(read.value, allow_zero=False)
    except ValueError:
        return RegistryObservation(
            hive=target.hive,
            subkey=target.subkey,
            registry_view=target.view,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_not_canonical_positive_four_part_version",
        )
    return RegistryObservation(
        hive=target.hive,
        subkey=target.subkey,
        registry_view=target.view,
        status=RegistryObservationStatus.VALID,
        version=read.value,
        reason=None,
    )


def _result(
    *,
    status: WebView2RuntimeStatus,
    selected_version: str | None,
    minimum_version: str | None,
    windows_64bit: bool,
    observations: tuple[RegistryObservation, ...],
) -> WebView2RuntimePreflight:
    return WebView2RuntimePreflight(
        status=status,
        selected_version=selected_version,
        minimum_version=minimum_version,
        windows_64bit=windows_64bit,
        observations=observations,
    )


def evaluate_webview2_registry_reads(
    reads: Sequence[tuple[RegistryTarget, RegistryRead]],
    *,
    windows_64bit: bool,
    reg_sz_type: int = 1,
    minimum_version: str | None = None,
) -> WebView2RuntimePreflight:
    """Pure evaluator used by the live registry probe and deterministic tests."""

    if type(windows_64bit) is not bool:
        raise TypeError("windows_64bit must be bool")
    minimum_tuple: tuple[int, int, int, int] | None = None
    if minimum_version is not None:
        minimum_tuple = _parse_version(minimum_version, allow_zero=True)

    canonical_targets = _registry_targets(windows_64bit=windows_64bit)
    supplied = tuple(reads)
    if len(supplied) != len(canonical_targets):
        raise ValueError("registry reads must cover the canonical target set exactly")
    supplied_targets = tuple(item[0] for item in supplied)
    if supplied_targets != canonical_targets:
        raise ValueError("registry reads must use canonical machine-first targets and views")

    observations = tuple(
        _classify_registry_read(target, read, reg_sz_type=reg_sz_type)
        for target, read in supplied
    )

    selected: RegistryObservation | None = None
    for observation in observations:
        if observation.status is RegistryObservationStatus.ERROR:
            if selected is None:
                return _result(
                    status=WebView2RuntimeStatus.INVALID_REGISTRATION,
                    selected_version=None,
                    minimum_version=minimum_version,
                    windows_64bit=windows_64bit,
                    observations=observations,
                )
            break
        if observation.status is RegistryObservationStatus.VALID:
            selected = observation
            break

    if selected is not None:
        assert selected.version is not None
        selected_tuple = _parse_version(selected.version, allow_zero=False)
        status = (
            WebView2RuntimeStatus.BELOW_EXPLICIT_MINIMUM
            if minimum_tuple is not None and selected_tuple < minimum_tuple
            else WebView2RuntimeStatus.AVAILABLE
        )
        return _result(
            status=status,
            selected_version=selected.version,
            minimum_version=minimum_version,
            windows_64bit=windows_64bit,
            observations=observations,
        )

    status = (
        WebView2RuntimeStatus.INVALID_REGISTRATION
        if any(
            item.status
            in {RegistryObservationStatus.INVALID, RegistryObservationStatus.ERROR}
            for item in observations
        )
        else WebView2RuntimeStatus.MISSING
    )
    return _result(
        status=status,
        selected_version=None,
        minimum_version=minimum_version,
        windows_64bit=windows_64bit,
        observations=observations,
    )


def probe_webview2_runtime(
    *, minimum_version: str | None = None
) -> WebView2RuntimePreflight:
    """Probe WebView2 Evergreen Runtime registration using documented locations."""

    if sys.platform != "win32":
        if minimum_version is not None:
            _parse_version(minimum_version, allow_zero=True)
        return WebView2RuntimePreflight(
            status=WebView2RuntimeStatus.UNSUPPORTED_PLATFORM,
            selected_version=None,
            minimum_version=minimum_version,
            windows_64bit=None,
            observations=(),
        )

    import winreg

    windows_64bit = _is_64bit_windows()
    targets = _registry_targets(windows_64bit=windows_64bit)
    reads = tuple((target, _read_registry_pv(target)) for target in targets)
    return evaluate_webview2_registry_reads(
        reads,
        windows_64bit=windows_64bit,
        reg_sz_type=winreg.REG_SZ,
        minimum_version=minimum_version,
    )
