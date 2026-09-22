from __future__ import annotations

from pathlib import Path
import re


SCRIPT = Path(__file__).parents[1] / "scripts" / "external_uia_notification_listener.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_listener_uses_public_managed_uia_notification_surface() -> None:
    assert "AutomationElement]::NotificationEvent" in TEXT
    assert "Automation]::AddAutomationEventHandler" in TEXT
    assert "NotificationEventArgs" in TEXT
    assert "Automation]::RemoveAutomationEventHandler" in TEXT


def test_listener_never_reaches_into_provider_private_state_or_raises_events() -> None:
    forbidden = (
        "tk_uia._uiacore",
        "_BY_HWND",
        "UiaHostProviderFromHwnd",
        "UiaRaiseNotificationEvent",
    )
    for token in forbidden:
        assert token not in TEXT


def test_listener_defaults_to_digest_only_display_evidence() -> None:
    assert "[switch]$IncludeDisplayString" in TEXT
    assert "display_string_sha256" in TEXT
    assert "display_string_utf8_length" in TEXT
    assert re.search(
        r"if \(\$IncludeDisplayString\)\s*\{\s*\$record\.display_string",
        TEXT,
    )


def test_listener_binds_sender_and_notification_semantics() -> None:
    required = (
        "sender_process_id",
        "sender_automation_id",
        "notification_kind",
        "notification_processing",
        "activity_id",
        "process_family_ids",
    )
    for field in required:
        assert field in TEXT


def test_listener_records_focus_without_requesting_focus() -> None:
    assert "focus_before" in TEXT
    assert "focus_after" in TEXT
    assert "focus_changed" in TEXT
    assert ".SetFocus(" not in TEXT
    assert "SetFocus()" not in TEXT
    assert "focus_force" not in TEXT


def test_listener_has_bounded_waits_and_no_unbounded_event_loop() -> None:
    assert "TimeoutSeconds -lt 1 -or $TimeoutSeconds -gt 300" in TEXT
    assert "MinimumEvents -lt 0 -or $MinimumEvents -gt 10000" in TEXT
    assert "AddSeconds($TimeoutSeconds)" in TEXT
    assert "while ($true)" not in TEXT


def test_listener_capability_probe_does_not_claim_runtime_event_proof() -> None:
    assert "status = 'CAPABILITY_READY'" in TEXT
    assert "minimum_events = 0" in TEXT
    assert "CapabilityProbeOnly" in TEXT


def test_listener_hard_codes_prehuman_truth_false() -> None:
    for field in (
        "real_money_execution = $false",
        "human_tested = $false",
        "nvda_verified = $false",
        "whole_product_complete = $false",
    ):
        assert field in TEXT


def test_listener_uses_thread_safe_csharp_callback_recorder() -> None:
    assert "ConcurrentQueue<AutosportUiaNotificationRecord>" in TEXT
    assert "AutomationEventHandler Handler" in TEXT
    assert "private void OnEvent" in TEXT
    assert "Records.Enqueue" in TEXT


def test_listener_unsubscribes_in_finally() -> None:
    finally_index = TEXT.index("} finally {")
    remove_index = TEXT.index("RemoveAutomationEventHandler")
    assert remove_index > finally_index
