from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from autosport.webview2_runtime_preflight import (
    WEBVIEW2_CLIENT_GUID,
    WEBVIEW2_HKCU_SUBKEY,
    WEBVIEW2_HKLM_SUBKEY,
    RegistryObservationStatus,
    RegistryRead,
    RegistryTarget,
    RegistryView,
    WebView2RuntimeStatus,
    _is_64bit_windows,
    _read_registry_pv,
    _registry_targets,
    evaluate_webview2_registry_reads,
    probe_webview2_runtime,
)

REG_SZ = 1


def _targets(*, windows_64bit: bool = True):
    return _registry_targets(windows_64bit=windows_64bit)


def _reads(*values: RegistryRead, windows_64bit: bool = True):
    targets = _targets(windows_64bit=windows_64bit)
    return tuple((target, read) for target, read in zip(targets, values, strict=True))


def _valid(version: str) -> RegistryRead:
    return RegistryRead(True, True, value=version, value_type=REG_SZ)


def _missing() -> RegistryRead:
    return RegistryRead(False, False)


def _error(name: str = "PermissionError") -> RegistryRead:
    return RegistryRead(False, False, error_type=name)


class _FakeRegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _fake_winreg():
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.HKEY_CURRENT_USER = object()
    fake.KEY_READ = 0x20019
    fake.KEY_WOW64_32KEY = 0x0200
    fake.REG_SZ = REG_SZ
    return fake


def test_guid_matches_documented_evergreen_runtime_client() -> None:
    assert WEBVIEW2_CLIENT_GUID == "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def test_64_bit_windows_uses_logical_hklm_path_and_explicit_32bit_view() -> None:
    machine, user = _targets(windows_64bit=True)
    assert machine == RegistryTarget("HKLM", WEBVIEW2_HKLM_SUBKEY, RegistryView.WOW64_32)
    assert user == RegistryTarget("HKCU", WEBVIEW2_HKCU_SUBKEY, RegistryView.PROCESS_DEFAULT)
    assert "WOW6432Node" not in machine.subkey


def test_32_bit_windows_uses_native_logical_hklm_view() -> None:
    machine, user = _targets(windows_64bit=False)
    assert machine == RegistryTarget("HKLM", WEBVIEW2_HKLM_SUBKEY, RegistryView.PROCESS_DEFAULT)
    assert user.view is RegistryView.PROCESS_DEFAULT


def test_registry_targets_reject_non_bool_os_bitness() -> None:
    with pytest.raises(TypeError):
        _registry_targets(windows_64bit=1)


def test_live_registry_adapter_uses_explicit_wow64_32_view_for_machine_target() -> None:
    fake = _fake_winreg()
    key = _FakeRegistryKey()
    calls = []
    def open_key(root, subkey, reserved, access):
        calls.append((root, subkey, reserved, access))
        return key
    fake.OpenKey = open_key
    fake.QueryValueEx = lambda opened, name: ("153.0.4234.32", fake.REG_SZ)
    target = RegistryTarget("HKLM", WEBVIEW2_HKLM_SUBKEY, RegistryView.WOW64_32)
    with patch.dict(sys.modules, {"winreg": fake}):
        result = _read_registry_pv(target)
    assert result == RegistryRead(True, True, value="153.0.4234.32", value_type=fake.REG_SZ)
    assert calls == [(fake.HKEY_LOCAL_MACHINE, WEBVIEW2_HKLM_SUBKEY, 0, fake.KEY_READ | fake.KEY_WOW64_32KEY)]


def test_live_registry_adapter_uses_plain_read_for_process_default_view() -> None:
    fake = _fake_winreg()
    key = _FakeRegistryKey()
    calls = []
    def open_key(root, subkey, reserved, access):
        calls.append((root, subkey, reserved, access))
        return key
    fake.OpenKey = open_key
    fake.QueryValueEx = lambda opened, name: ("153.0.4234.32", fake.REG_SZ)
    target = RegistryTarget("HKCU", WEBVIEW2_HKCU_SUBKEY, RegistryView.PROCESS_DEFAULT)
    with patch.dict(sys.modules, {"winreg": fake}):
        result = _read_registry_pv(target)
    assert result.value == "153.0.4234.32"
    assert calls == [(fake.HKEY_CURRENT_USER, WEBVIEW2_HKCU_SUBKEY, 0, fake.KEY_READ)]


def test_live_registry_adapter_fails_closed_if_explicit_view_flag_is_unavailable() -> None:
    fake = _fake_winreg()
    del fake.KEY_WOW64_32KEY
    fake.OpenKey = lambda *_args: (_ for _ in ()).throw(AssertionError("unreachable"))
    fake.QueryValueEx = lambda *_args: (_ for _ in ()).throw(AssertionError("unreachable"))
    target = RegistryTarget("HKLM", WEBVIEW2_HKLM_SUBKEY, RegistryView.WOW64_32)
    with patch.dict(sys.modules, {"winreg": fake}):
        result = _read_registry_pv(target)
    assert result == RegistryRead(False, False, error_type="WOW64RegistryViewUnavailable")


def test_live_registry_adapter_distinguishes_missing_key_and_missing_pv() -> None:
    fake = _fake_winreg()
    def missing_key(*_args):
        raise FileNotFoundError("not present")
    fake.OpenKey = missing_key
    fake.QueryValueEx = lambda *_args: (_ for _ in ()).throw(AssertionError("unreachable"))
    with patch.dict(sys.modules, {"winreg": fake}):
        assert _read_registry_pv(_targets()[1]) == RegistryRead(False, False)
    key = _FakeRegistryKey()
    fake.OpenKey = lambda *_args: key
    def missing_pv(*_args):
        raise FileNotFoundError("pv missing")
    fake.QueryValueEx = missing_pv
    with patch.dict(sys.modules, {"winreg": fake}):
        assert _read_registry_pv(_targets()[1]) == RegistryRead(True, False)


def test_live_registry_adapter_fails_closed_on_read_error_without_detail() -> None:
    fake = _fake_winreg()
    def denied(*_args):
        raise PermissionError("secret-bearing operating-system detail")
    fake.OpenKey = denied
    fake.QueryValueEx = lambda *_args: (_ for _ in ()).throw(AssertionError("unreachable"))
    with patch.dict(sys.modules, {"winreg": fake}):
        result = _read_registry_pv(_targets()[0])
    assert result.error_type == "PermissionError"
    assert "secret-bearing" not in repr(result)


def test_x86_process_on_64bit_windows_uses_explicit_32bit_machine_view() -> None:
    fake = _fake_winreg()
    key = _FakeRegistryKey()
    calls = []
    def open_key(root, subkey, reserved, access):
        calls.append((root, subkey, reserved, access))
        return key
    fake.OpenKey = open_key
    fake.QueryValueEx = lambda opened, name: ("153.0.4234.32", fake.REG_SZ)
    env = {"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "AMD64"}
    with (
        patch.object(sys, "platform", "win32"),
        patch.dict("os.environ", env, clear=True),
        patch.dict(sys.modules, {"winreg": fake}),
    ):
        result = probe_webview2_runtime()
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.windows_64bit is True
    assert calls[0] == (fake.HKEY_LOCAL_MACHINE, WEBVIEW2_HKLM_SUBKEY, 0, fake.KEY_READ | fake.KEY_WOW64_32KEY)
    assert calls[1] == (fake.HKEY_CURRENT_USER, WEBVIEW2_HKCU_SUBKEY, 0, fake.KEY_READ)


def test_missing_both_registrations_is_missing() -> None:
    result = evaluate_webview2_registry_reads(_reads(_missing(), _missing()), windows_64bit=True, reg_sz_type=REG_SZ)
    assert result.status is WebView2RuntimeStatus.MISSING
    assert not result.available
    assert result.selected_version is None


@pytest.mark.parametrize("bad_value", ["", "0.0.0.0", "1", "1.2.3", "1.2.3.4.5", "01.2.3.4", "1.02.3.4", "1.2.x.4", " 1.2.3.4", "1.2.3.4 ", "1.2.3.-1"])
def test_invalid_pv_text_fails_closed_without_lower_valid(bad_value: str) -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(True, True, value=bad_value, value_type=REG_SZ), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.observations[0].status is RegistryObservationStatus.INVALID


def test_machine_invalid_can_continue_to_valid_user_registration() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(True, True, value="bad", value_type=REG_SZ), _valid("153.0.4234.32")),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.selected_version == "153.0.4234.32"


def test_machine_read_error_blocks_lower_precedence_valid_registration() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_error(), _valid("200.0.0.0")),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="150.0.0.0",
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.selected_version is None
    assert not result.available


def test_machine_valid_owns_selection_despite_lower_precedence_error() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _error()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="150.0.0.0",
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.selected_version == "153.0.4234.32"


def test_machine_runtime_precedes_newer_per_user_registration() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("99.10.2.3"), _valid("100.0.0.1")),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.selected_version == "99.10.2.3"


def test_explicit_minimum_fails_closed_when_machine_runtime_shadows_newer_per_user() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("99.10.2.3"), _valid("100.0.0.1")),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="100.0.0.0",
    )
    assert result.status is WebView2RuntimeStatus.BELOW_EXPLICIT_MINIMUM
    assert result.selected_version == "99.10.2.3"


@pytest.mark.parametrize(("minimum", "status"), [
    ("153.0.4234.32", WebView2RuntimeStatus.AVAILABLE),
    ("152.0.0.0", WebView2RuntimeStatus.AVAILABLE),
    ("154.0.0.0", WebView2RuntimeStatus.BELOW_EXPLICIT_MINIMUM),
    ("0.0.0.0", WebView2RuntimeStatus.AVAILABLE),
])
def test_explicit_minimum_policy(minimum, status) -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version=minimum,
    )
    assert result.status is status


@pytest.mark.parametrize("minimum", ["153", "153.0.0", "0153.0.0.0", "153.0.0.0 ", "x.y.z.w"])
def test_noncanonical_explicit_minimum_is_rejected(minimum: str) -> None:
    with pytest.raises(ValueError):
        evaluate_webview2_registry_reads(
            _reads(_valid("153.0.4234.32"), _missing()),
            windows_64bit=True,
            reg_sz_type=REG_SZ,
            minimum_version=minimum,
        )


def test_registry_read_sequence_must_use_canonical_machine_first_targets() -> None:
    canonical = _reads(_valid("1.0.0.1"), _missing())
    with pytest.raises(ValueError, match="machine-first"):
        evaluate_webview2_registry_reads(tuple(reversed(canonical)), windows_64bit=True, reg_sz_type=REG_SZ)


def test_registry_read_sequence_must_bind_expected_view() -> None:
    targets = list(_targets())
    forged_machine = RegistryTarget("HKLM", WEBVIEW2_HKLM_SUBKEY, RegistryView.PROCESS_DEFAULT)
    reads = ((forged_machine, _valid("153.0.4234.32")), (targets[1], _missing()))
    with pytest.raises(ValueError, match="views"):
        evaluate_webview2_registry_reads(reads, windows_64bit=True, reg_sz_type=REG_SZ)


def test_json_evidence_binds_registry_view_and_round_trips() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    encoded = result.to_json()
    decoded = json.loads(encoded)
    assert encoded == json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert decoded["schema_version"] == 2
    assert decoded["observations"][0]["registry_view"] == "WOW64_32"
    assert decoded["status"] == "AVAILABLE"
    assert "operator_message_uk" not in decoded


def test_json_evidence_is_deterministic() -> None:
    kwargs = dict(windows_64bit=True, reg_sz_type=REG_SZ, minimum_version="150.0.0.0")
    first = evaluate_webview2_registry_reads(_reads(_valid("153.0.4234.32"), _missing()), **kwargs)
    second = evaluate_webview2_registry_reads(_reads(_valid("153.0.4234.32"), _missing()), **kwargs)
    assert first.to_json() == second.to_json()


def test_non_windows_probe_is_explicitly_unsupported_without_registry_import() -> None:
    with patch.object(sys, "platform", "linux"):
        result = probe_webview2_runtime()
    assert result.status is WebView2RuntimeStatus.UNSUPPORTED_PLATFORM
    assert result.windows_64bit is None
    assert result.observations == ()


def test_non_windows_probe_still_validates_explicit_minimum() -> None:
    with patch.object(sys, "platform", "linux"):
        with pytest.raises(ValueError):
            probe_webview2_runtime(minimum_version="not-a-version")


def test_64bit_detection_honors_processor_architew6432() -> None:
    with patch.dict("os.environ", {"PROCESSOR_ARCHITEW6432": "AMD64", "PROCESSOR_ARCHITECTURE": "x86"}, clear=True):
        assert _is_64bit_windows() is True


def test_evidence_never_claims_human_nvda_or_product_completion() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    payload = result.to_dict()
    assert "human_tested" not in payload
    assert "nvda_verified" not in payload
    assert "whole_product_complete" not in payload
    assert "v1_ready" not in payload
