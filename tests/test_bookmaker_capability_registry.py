from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerCapabilityRegistryError,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)


_TS = "2026-09-17T16:00:00+00:00"
_HASH = "b" * 64


def _profile(
    version: int = 1,
    state: BookmakerCapabilityState = BookmakerCapabilityState.SUPPORTED,
    *,
    source_hash: str = _HASH,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        profile_version=version,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                state,
            ),
        ),
        observed_at=_TS,
        source_ref=f"capability-probe-v{version}",
        source_payload_sha256=source_hash,
    )


def _governance(
    permission: GovernancePermissionState = GovernancePermissionState.UNKNOWN,
    *,
    source_hash: str = _HASH,
) -> BookmakerGovernanceEvidence:
    return BookmakerGovernanceEvidence(
        venue_id="book-a",
        account_id="acct-a",
        jurisdiction="SK",
        terms_version="2026-09",
        automation_permission=permission,
        observed_at=_TS,
        source_ref="terms-snapshot",
        source_payload_sha256=source_hash,
    )


def test_registry_reopens_and_returns_latest_version(tmp_path) -> None:
    path = tmp_path / "bookmaker-capabilities.json"
    registry = BookmakerCapabilityRegistry(path)

    assert registry.register_profile(_profile(version=1))
    assert registry.register_profile(_profile(version=2))
    assert not registry.register_profile(_profile(version=2))

    reopened = BookmakerCapabilityRegistry(path)
    history = reopened.profile_history("book-a", "acct-a", "adapter-a")
    assert [item.profile_version for item in history] == [1, 2]
    assert reopened.latest_profile(
        "book-a", "acct-a", "adapter-a"
    ).profile_version == 2


def test_registry_rejects_conflicting_immutable_profile_version(tmp_path) -> None:
    registry = BookmakerCapabilityRegistry(tmp_path / "registry.json")
    assert registry.register_profile(_profile(version=1))
    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="conflicting immutable profile identity",
    ):
        registry.register_profile(
            _profile(
                version=1,
                state=BookmakerCapabilityState.UNSUPPORTED,
            )
        )


def test_corrupt_registry_fails_closed_on_reopen(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="unreadable or corrupt",
    ):
        BookmakerCapabilityRegistry(path).profile_history(
            "book-a", "acct-a", "adapter-a"
        )


def test_tampered_profile_identity_fails_closed(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    registry.register_profile(_profile())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["profiles"][0]["profile"]["source_ref"] = "tampered"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="stored profile_id does not match",
    ):
        BookmakerCapabilityRegistry(path).latest_profile(
            "book-a", "acct-a", "adapter-a"
        )


def test_governance_is_persisted_separately_and_idempotently(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    registry.register_profile(_profile())

    assert registry.governance_history("book-a", "acct-a") == ()
    evidence = _governance(GovernancePermissionState.PERMITTED)
    assert registry.register_governance(evidence)
    assert not registry.register_governance(evidence)

    reopened = BookmakerCapabilityRegistry(path)
    history = reopened.governance_history("book-a", "acct-a")
    assert history == (evidence,)
    assert (
        reopened.latest_profile("book-a", "acct-a", "adapter-a").supports(
            BookmakerCapability.BALANCE_READ
        )
        is True
    )


def test_governance_conflict_for_same_immutable_scope_fails_closed(tmp_path) -> None:
    registry = BookmakerCapabilityRegistry(tmp_path / "registry.json")
    assert registry.register_governance(
        _governance(GovernancePermissionState.UNKNOWN)
    )
    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="conflicting immutable governance evidence",
    ):
        registry.register_governance(
            _governance(GovernancePermissionState.PROHIBITED)
        )


def test_registry_rejects_duplicate_persisted_profile_entries(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    registry.register_profile(_profile())
    document = json.loads(path.read_text(encoding="utf-8"))
    document["profiles"].append(document["profiles"][0])
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="duplicate profile_id",
    ):
        BookmakerCapabilityRegistry(path).profile_history(
            "book-a", "acct-a", "adapter-a"
        )


def test_registry_serializes_concurrent_profile_and_governance_writers(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "registry.json"
    first = BookmakerCapabilityRegistry(path)
    second = BookmakerCapabilityRegistry(path)

    first_write_entered = Event()
    allow_first_write = Event()
    second_load_entered = Event()

    original_first_write = first._write_document
    original_second_load = second._load_document

    def blocked_first_write(profiles, governance) -> None:
        first_write_entered.set()
        assert allow_first_write.wait(5)
        original_first_write(profiles, governance)

    def observed_second_load():
        second_load_entered.set()
        return original_second_load()

    monkeypatch.setattr(first, "_write_document", blocked_first_write)
    monkeypatch.setattr(second, "_load_document", observed_second_load)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first.register_profile, _profile())
        assert first_write_entered.wait(5)

        second_future = pool.submit(
            second.register_governance,
            _governance(GovernancePermissionState.PERMITTED),
        )
        assert not second_load_entered.wait(0.2)

        allow_first_write.set()
        assert first_future.result(timeout=5)
        assert second_future.result(timeout=5)

    assert second_load_entered.is_set()
    reopened = BookmakerCapabilityRegistry(path)
    assert [
        item.profile_version
        for item in reopened.profile_history("book-a", "acct-a", "adapter-a")
    ] == [1]
    assert reopened.governance_history("book-a", "acct-a") == (
        _governance(GovernancePermissionState.PERMITTED),
    )


def test_registry_file_is_deterministic_json_with_schema_version(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    registry.register_governance(_governance())
    registry.register_profile(_profile())

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert len(document["profiles"]) == 1
    assert len(document["governance"]) == 1
    assert path.read_text(encoding="utf-8").endswith("\n")
