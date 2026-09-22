from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence


WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_HKCU_SUBKEY = (
    rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
WEBVIEW2_HKLM_64_SUBKEY = (
    rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
WEBVIEW2_HKLM_32_SUBKEY = (
    rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
)
MICROSOFT_WEBVIEW2_DISTRIBUTION_DOC = (
    "https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution"
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
    status: RegistryObservationStatus
    version: str | None
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "hive": self.hive,
            "reason": self.reason,
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

    def operator_message_uk(self) -> str:
        if self.status is WebView2RuntimeStatus.AVAILABLE:
            return (
                "Microsoft Edge WebView2 Runtime доступний. "
                f"Версія: {self.selected_version}."
            )
        if self.status is WebView2RuntimeStatus.BELOW_EXPLICIT_MINIMUM:
            return (
                "Microsoft Edge WebView2 Runtime встановлено, але версія "
                f"{self.selected_version} нижча за мінімум політики "
                f"{self.minimum_version}. Оновіть Evergreen Runtime і повторіть запуск."
            )
        if self.status is WebView2RuntimeStatus.INVALID_REGISTRATION:
            return (
                "Реєстрація Microsoft Edge WebView2 Runtime некоректна або недоступна "
                "для надійної перевірки. Відновіть або перевстановіть Evergreen Runtime "
                "і повторіть запуск."
            )
        if self.status is WebView2RuntimeStatus.MISSING:
            return (
                "Microsoft Edge WebView2 Runtime не знайдено. Встановіть Evergreen Runtime "
                "і повторіть запуск Автоспорт."
            )
        return "Перевірка Microsoft Edge WebView2 Runtime підтримується лише у Windows."

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "documentation": MICROSOFT_WEBVIEW2_DISTRIBUTION_DOC,
            "kind": "webview2_evergreen_runtime_preflight",
            "minimum_version": self.minimum_version,
            "observations": [item.to_dict() for item in self.observations],
            "operator_message_uk": self.operator_message_uk(),
            "schema_version": 1,
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


def _registry_targets(*, windows_64bit: bool) -> tuple[tuple[str, str], ...]:
    hklm = WEBVIEW2_HKLM_64_SUBKEY if windows_64bit else WEBVIEW2_HKLM_32_SUBKEY
    return (("HKLM", hklm), ("HKCU", WEBVIEW2_HKCU_SUBKEY))


def _read_registry_pv(hive: str, subkey: str) -> RegistryRead:
    """Read Microsoft's documented pv value without requiring elevated access."""

    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - protected by platform gate
        return RegistryRead(False, False, error_type=type(exc).__name__)

    root = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
    }[hive]
    try:
        key = winreg.OpenKey(root, subkey, 0, winreg.KEY_READ)
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
    hive: str,
    subkey: str,
    read: RegistryRead,
    *,
    reg_sz_type: int,
) -> RegistryObservation:
    if read.error_type is not None:
        return RegistryObservation(
            hive=hive,
            subkey=subkey,
            status=RegistryObservationStatus.ERROR,
            version=None,
            reason=read.error_type,
        )
    if not read.key_present:
        return RegistryObservation(
            hive=hive,
            subkey=subkey,
            status=RegistryObservationStatus.MISSING,
            version=None,
            reason="registry_key_missing",
        )
    if not read.value_present:
        return RegistryObservation(
            hive=hive,
            subkey=subkey,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_missing",
        )
    if read.value_type != reg_sz_type:
        return RegistryObservation(
            hive=hive,
            subkey=subkey,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_not_reg_sz",
        )
    if type(read.value) is not str:
        return RegistryObservation(
            hive=hive,
            subkey=subkey,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_not_text",
        )
    try:
        _parse_version(read.value, allow_zero=False)
    except ValueError:
        return RegistryObservation(
            hive=hive,
            subkey=subkey,
            status=RegistryObservationStatus.INVALID,
            version=None,
            reason="pv_not_canonical_positive_four_part_version",
        )
    return RegistryObservation(
        hive=hive,
        subkey=subkey,
        status=RegistryObservationStatus.VALID,
        version=read.value,
        reason=None,
    )


def evaluate_webview2_registry_reads(
    reads: Sequence[tuple[str, str, RegistryRead]],
    *,
    windows_64bit: bool,
    reg_sz_type: int = 1,
    minimum_version: str | None = None,
) -> WebView2RuntimePreflight:
    """Pure evaluator used by the live registry probe and deterministic tests."""

    minimum_tuple: tuple[int, int, int, int] | None = None
    if minimum_version is not None:
        minimum_tuple = _parse_version(minimum_version, allow_zero=True)

    observations = tuple(
        _classify_registry_read(hive, subkey, read, reg_sz_type=reg_sz_type)
        for hive, subkey, read in reads
    )
    # Preserve the documented installed-Runtime resolution precedence: machine-level
    # registration is considered before per-user registration.  Choosing the highest
    # visible pv would be fail-open when a lower machine Runtime shadows a newer
    # per-user Runtime and an explicit release minimum is in force.
    valid_versions = [
        item.version
        for item in observations
        if item.status is RegistryObservationStatus.VALID and item.version is not None
    ]
    if valid_versions:
        selected = valid_versions[0]
        selected_tuple = _parse_version(selected, allow_zero=False)
        status = (
            WebView2RuntimeStatus.BELOW_EXPLICIT_MINIMUM
            if minimum_tuple is not None and selected_tuple < minimum_tuple
            else WebView2RuntimeStatus.AVAILABLE
        )
        return WebView2RuntimePreflight(
            status=status,
            selected_version=selected,
            minimum_version=minimum_version,
            windows_64bit=windows_64bit,
            observations=observations,
        )

    if any(
        item.status in {RegistryObservationStatus.INVALID, RegistryObservationStatus.ERROR}
        for item in observations
    ):
        status = WebView2RuntimeStatus.INVALID_REGISTRATION
    else:
        status = WebView2RuntimeStatus.MISSING
    return WebView2RuntimePreflight(
        status=status,
        selected_version=None,
        minimum_version=minimum_version,
        windows_64bit=windows_64bit,
        observations=observations,
    )


def probe_webview2_runtime(
    *, minimum_version: str | None = None
) -> WebView2RuntimePreflight:
    """Probe WebView2 Evergreen Runtime registration using Microsoft's documented keys."""

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
    reads = tuple(
        (hive, subkey, _read_registry_pv(hive, subkey))
        for hive, subkey in _registry_targets(windows_64bit=windows_64bit)
    )
    return evaluate_webview2_registry_reads(
        reads,
        windows_64bit=windows_64bit,
        reg_sz_type=winreg.REG_SZ,
        minimum_version=minimum_version,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Перевірити наявність Microsoft Edge WebView2 Evergreen Runtime."
    )
    parser.add_argument(
        "--minimum-version",
        help="Необов'язковий явний мінімум політики у форматі N.N.N.N.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вивести deterministic JSON evidence замість короткого повідомлення.",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = probe_webview2_runtime(minimum_version=args.minimum_version)
    except ValueError as exc:
        print(f"Некоректний мінімум WebView2: {exc}", file=sys.stderr)
        return 2
    print(result.to_json() if args.json else result.operator_message_uk())
    return 0 if result.available else 2


if __name__ == "__main__":
    raise SystemExit(main())
