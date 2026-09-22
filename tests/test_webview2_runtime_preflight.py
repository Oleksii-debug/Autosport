from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from autosport.webview2_runtime_preflight import (
    WEBVIEW2_CLIENT_GUID,
    WEBVIEW2_HKCU_SUBKEY,
    WEBVIEW2_HKLM_32_SUBKEY,
    WEBVIEW2_HKLM_64_SUBKEY,
    RegistryObservationStatus,
    RegistryRead,
    WebView2RuntimeStatus,
    _is_64bit_windows,
    _read_registry_pv,
    _registry_targets,
    evaluate_webview2_registry_reads,
    main,
    probe_webview2_runtime,
)


REG_SZ = 1


def _reads(*values: RegistryRead):
    targets = (("HKLM", WEBVIEW2_HKLM_64_SUBKEY), ("HKCU", WEBVIEW2_HKCU_SUBKEY))
    return tuple((hive, subkey, read) for (hive, subkey), read in zip(targets, values, strict=True))


def _valid(version: str) -> RegistryRead:
    return RegistryRead(True, True, value=version, value_type=REG_SZ)


def _missing() -> RegistryRead:
    return RegistryRead(False, False)



class _FakeRegistryKey:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_live_registry_adapter_uses_read_only_open_and_returns_reg_sz() -> None:
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.HKEY_CURRENT_USER = object()
    fake.KEY_READ = 0x20019
    fake.REG_SZ = 1
    key = _FakeRegistryKey()
    calls: list[tuple[object, str, int, int]] = []

    def open_key(root, subkey, reserved, access):
        calls.append((root, subkey, reserved, access))
        return key

    def query_value_ex(opened, name):
        assert opened is key
        assert name == "pv"
        return "153.0.4234.32", fake.REG_SZ

    fake.OpenKey = open_key
    fake.QueryValueEx = query_value_ex
    with patch.dict(sys.modules, {"winreg": fake}):
        result = _read_registry_pv("HKLM", WEBVIEW2_HKLM_64_SUBKEY)

    assert result == RegistryRead(
        True, True, value="153.0.4234.32", value_type=fake.REG_SZ
    )
    assert calls == [
        (fake.HKEY_LOCAL_MACHINE, WEBVIEW2_HKLM_64_SUBKEY, 0, fake.KEY_READ)
    ]


def test_live_registry_adapter_distinguishes_missing_key_and_missing_pv() -> None:
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.HKEY_CURRENT_USER = object()
    fake.KEY_READ = 0x20019
    fake.REG_SZ = 1

    def missing_key(*_args):
        raise FileNotFoundError("not present")

    fake.OpenKey = missing_key
    fake.QueryValueEx = lambda *_args: (_ for _ in ()).throw(AssertionError("unreachable"))
    with patch.dict(sys.modules, {"winreg": fake}):
        assert _read_registry_pv("HKCU", WEBVIEW2_HKCU_SUBKEY) == RegistryRead(
            False, False
        )

    key = _FakeRegistryKey()
    fake.OpenKey = lambda *_args: key

    def missing_pv(*_args):
        raise FileNotFoundError("pv missing")

    fake.QueryValueEx = missing_pv
    with patch.dict(sys.modules, {"winreg": fake}):
        assert _read_registry_pv("HKCU", WEBVIEW2_HKCU_SUBKEY) == RegistryRead(
            True, False
        )


def test_live_registry_adapter_fails_closed_on_read_error_without_detail() -> None:
    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.HKEY_CURRENT_USER = object()
    fake.KEY_READ = 0x20019
    fake.REG_SZ = 1

    def denied(*_args):
        raise PermissionError("secret-bearing operating-system detail")

    fake.OpenKey = denied
    fake.QueryValueEx = lambda *_args: (_ for _ in ()).throw(AssertionError("unreachable"))
    with patch.dict(sys.modules, {"winreg": fake}):
        result = _read_registry_pv("HKLM", WEBVIEW2_HKLM_64_SUBKEY)
    assert result.error_type == "PermissionError"
    assert "secret-bearing" not in repr(result)

def test_guid_matches_documented_evergreen_runtime_client() -> None:
    assert WEBVIEW2_CLIENT_GUID == "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def test_64_bit_machine_uses_wow6432node_hklm_and_hkcu() -> None:
    assert _registry_targets(windows_64bit=True) == (
        ("HKLM", WEBVIEW2_HKLM_64_SUBKEY),
        ("HKCU", WEBVIEW2_HKCU_SUBKEY),
    )
    assert "WOW6432Node" in WEBVIEW2_HKLM_64_SUBKEY


def test_32_bit_machine_uses_native_hklm_and_hkcu() -> None:
    assert _registry_targets(windows_64bit=False) == (
        ("HKLM", WEBVIEW2_HKLM_32_SUBKEY),
        ("HKCU", WEBVIEW2_HKCU_SUBKEY),
    )
    assert "WOW6432Node" not in WEBVIEW2_HKLM_32_SUBKEY


def test_missing_both_registrations_is_missing() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_missing(), _missing()), windows_64bit=True, reg_sz_type=REG_SZ
    )
    assert result.status is WebView2RuntimeStatus.MISSING
    assert not result.available
    assert result.selected_version is None


@pytest.mark.parametrize(
    "bad_value",
    ["", "0.0.0.0", "1", "1.2.3", "1.2.3.4.5", "01.2.3.4", "1.02.3.4", "1.2.x.4", " 1.2.3.4", "1.2.3.4 ", "1.2.3.-1"],
)
def test_invalid_pv_text_fails_closed(bad_value: str) -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(True, True, value=bad_value, value_type=REG_SZ), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.observations[0].status is RegistryObservationStatus.INVALID


def test_missing_pv_under_existing_key_is_invalid_registration() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(True, False), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.observations[0].reason == "pv_missing"


def test_non_reg_sz_pv_is_invalid_registration() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(True, True, value="153.0.4234.32", value_type=REG_SZ + 1), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.observations[0].reason == "pv_not_reg_sz"


def test_non_text_pv_is_invalid_even_if_type_claims_reg_sz() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(True, True, value=153, value_type=REG_SZ), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.observations[0].reason == "pv_not_text"


def test_registry_read_error_fails_closed_when_no_valid_registration_exists() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(False, False, error_type="PermissionError"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.INVALID_REGISTRATION
    assert result.observations[0].status is RegistryObservationStatus.ERROR


def test_valid_hklm_registration_is_available() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.available
    assert result.selected_version == "153.0.4234.32"


def test_valid_hkcu_registration_is_available() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_missing(), _valid("153.0.4234.32")),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.selected_version == "153.0.4234.32"


def test_one_valid_registration_is_sufficient_even_if_other_registration_is_invalid() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), RegistryRead(True, True, value="bad", value_type=REG_SZ)),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.selected_version == "153.0.4234.32"


def test_machine_registration_precedes_newer_per_user_registration() -> None:
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


def test_explicit_minimum_equal_to_runtime_is_available() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="153.0.4234.32",
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE


def test_explicit_minimum_below_runtime_is_available() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="152.0.0.0",
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE


def test_explicit_minimum_above_runtime_is_below_minimum() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="154.0.0.0",
    )
    assert result.status is WebView2RuntimeStatus.BELOW_EXPLICIT_MINIMUM
    assert not result.available


def test_no_default_minimum_is_invented() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("1.0.0.1"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE
    assert result.minimum_version is None


@pytest.mark.parametrize("minimum", ["153", "153.0.0", "0153.0.0.0", "153.0.0.0 ", "x.y.z.w"])
def test_noncanonical_explicit_minimum_is_rejected(minimum: str) -> None:
    with pytest.raises(ValueError):
        evaluate_webview2_registry_reads(
            _reads(_valid("153.0.4234.32"), _missing()),
            windows_64bit=True,
            reg_sz_type=REG_SZ,
            minimum_version=minimum,
        )


def test_zero_explicit_minimum_is_allowed_as_policy_floor() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("1.0.0.1"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
        minimum_version="0.0.0.0",
    )
    assert result.status is WebView2RuntimeStatus.AVAILABLE


def test_json_evidence_is_canonical_and_round_trips_unicode() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    encoded = result.to_json()
    decoded = json.loads(encoded)
    assert encoded == json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert decoded["status"] == "AVAILABLE"
    assert decoded["operator_message_uk"].startswith("Microsoft Edge WebView2 Runtime доступний")
    assert decoded["available"] is True


def test_json_evidence_is_deterministic() -> None:
    kwargs = dict(windows_64bit=True, reg_sz_type=REG_SZ, minimum_version="150.0.0.0")
    first = evaluate_webview2_registry_reads(_reads(_valid("153.0.4234.32"), _missing()), **kwargs)
    second = evaluate_webview2_registry_reads(_reads(_valid("153.0.4234.32"), _missing()), **kwargs)
    assert first.to_json() == second.to_json()


def test_ukrainian_missing_message_is_actionable() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_missing(), _missing()), windows_64bit=True, reg_sz_type=REG_SZ
    )
    message = result.operator_message_uk()
    assert "не знайдено" in message
    assert "Evergreen Runtime" in message


def test_ukrainian_invalid_message_does_not_echo_registry_exception_detail() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(RegistryRead(False, False, error_type="SecretBearingPermissionError"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    assert "SecretBearingPermissionError" not in result.operator_message_uk()
    assert "некоректна" in result.operator_message_uk()


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
    with patch.dict(
        "os.environ",
        {"PROCESSOR_ARCHITEW6432": "AMD64", "PROCESSOR_ARCHITECTURE": "x86"},
        clear=True,
    ):
        assert _is_64bit_windows() is True


def test_cli_returns_nonzero_on_unsupported_platform_and_emits_json(capsys) -> None:
    with patch.object(sys, "platform", "linux"):
        assert main(["--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "UNSUPPORTED_PLATFORM"
    assert payload["available"] is False


def test_cli_rejects_bad_explicit_minimum(capsys) -> None:
    with patch.object(sys, "platform", "linux"):
        assert main(["--minimum-version", "bad"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Некоректний мінімум WebView2" in captured.err


def test_evidence_never_claims_human_or_nvda_verification() -> None:
    result = evaluate_webview2_registry_reads(
        _reads(_valid("153.0.4234.32"), _missing()),
        windows_64bit=True,
        reg_sz_type=REG_SZ,
    )
    payload = result.to_dict()
    assert "human_tested" not in payload
    assert "nvda_verified" not in payload
    assert "whole_product_complete" not in payload
