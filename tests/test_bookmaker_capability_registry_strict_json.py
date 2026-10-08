import json
from pathlib import Path

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
_HASH = "c" * 64


def _profile() -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        profile_version=1,
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        observed_at=_TS,
        source_ref="capability-probe",
        source_payload_sha256=_HASH,
    )


def _governance() -> BookmakerGovernanceEvidence:
    return BookmakerGovernanceEvidence(
        venue_id="book-a",
        account_id="acct-a",
        jurisdiction="SK",
        terms_version="2026-09",
        automation_permission=GovernancePermissionState.UNKNOWN,
        observed_at=_TS,
        source_ref="terms-snapshot",
        source_payload_sha256=_HASH,
    )


def _assert_corrupt(path: Path) -> None:
    with pytest.raises(
        BookmakerCapabilityRegistryError,
        match="unreadable or corrupt",
    ):
        BookmakerCapabilityRegistry(path).profile_history(
            "book-a", "acct-a", "adapter-a"
        )


def test_registry_rejects_duplicate_top_level_key(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        '{"schema_version":1,"schema_version":1,"profiles":[],"governance":[]}',
        encoding="utf-8",
    )
    _assert_corrupt(path)


def test_registry_rejects_duplicate_profile_entry_key(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    profile = _profile()
    assert registry.register_profile(profile)

    raw = path.read_text(encoding="utf-8")
    needle = f'"profile_id":"{profile.profile_id}"'
    assert needle in raw
    path.write_text(raw.replace(needle, f"{needle},{needle}", 1), encoding="utf-8")

    _assert_corrupt(path)


def test_registry_rejects_duplicate_governance_entry_key(tmp_path) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    evidence = _governance()
    assert registry.register_governance(evidence)

    raw = path.read_text(encoding="utf-8")
    needle = f'"evidence_id":"{evidence.evidence_id}"'
    assert needle in raw
    path.write_text(raw.replace(needle, f"{needle},{needle}", 1), encoding="utf-8")

    _assert_corrupt(path)


def test_registry_rejects_nonstandard_json_constant(tmp_path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(
        '{"schema_version":1,"profiles":[],"governance":[],"extra":NaN}',
        encoding="utf-8",
    )
    _assert_corrupt(path)


@pytest.mark.parametrize("aliased_version", [True, 1.0])
def test_registry_rejects_schema_version_aliases(tmp_path, aliased_version) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({
        "schema_version": aliased_version, "profiles": [], "governance": []
    }), encoding="utf-8")
    with pytest.raises(BookmakerCapabilityRegistryError, match="schema_version"):
        BookmakerCapabilityRegistry(path).profile_history(
            "book-a", "acct-a", "adapter-a"
        )


@pytest.mark.parametrize("injection", [
    "root", "profile_entry", "profile", "fact", "governance_entry", "governance"
])
def test_registry_rejects_unknown_nested_versioned_fields(tmp_path, injection) -> None:
    path = tmp_path / "registry.json"
    registry = BookmakerCapabilityRegistry(path)
    assert registry.register_profile(_profile())
    assert registry.register_governance(_governance())
    document = json.loads(path.read_text(encoding="utf-8"))
    targets = {
        "root": document,
        "profile_entry": document["profiles"][0],
        "profile": document["profiles"][0]["profile"],
        "fact": document["profiles"][0]["profile"]["facts"][0],
        "governance_entry": document["governance"][0],
        "governance": document["governance"][0]["evidence"],
    }
    targets[injection]["unknown_future_authority"] = True
    path.write_text(json.dumps(document), encoding="utf-8")
    # The public decoder intentionally wraps nested fact-schema errors; both
    # surfaces must reject the record rather than silently accept unknown data.
    expected_error = (
        "invalid capability profile payload" if injection == "fact"
        else "schema fields"
    )
    with pytest.raises(BookmakerCapabilityRegistryError, match=expected_error):
        registry.profile_history("book-a", "acct-a", "adapter-a")

    # A future schema must not be erased by a later read-modify-write,
    # including an idempotent replay. Reopen must remain fail-closed.
    corrupt_bytes = path.read_bytes()
    for mutation in (
        lambda: registry.register_profile(_profile()),
        lambda: registry.register_governance(_governance()),
    ):
        with pytest.raises(BookmakerCapabilityRegistryError):
            mutation()
        assert path.read_bytes() == corrupt_bytes

    with pytest.raises(BookmakerCapabilityRegistryError):
        BookmakerCapabilityRegistry(path).governance_history("book-a", "acct-a")
    assert path.read_bytes() == corrupt_bytes


@pytest.mark.parametrize("field", [
    "venue_id", "account_id", "jurisdiction", "terms_version", "source_ref",
])
def test_governance_rejects_malformed_unicode_without_publishing(tmp_path, field) -> None:
    # A lone surrogate is legal inside a Python str but cannot encode to
    # canonical UTF-8. Technical and legal/terms identities share this fence.
    from dataclasses import replace

    invalid = "x" + chr(0xD800)
    path = tmp_path / "registry.json"
    with pytest.raises(BookmakerCapabilityRegistryError, match="valid UTF-8"):
        replace(_governance(), **{field: invalid})
    with pytest.raises(BookmakerCapabilityRegistryError, match="valid UTF-8"):
        BookmakerCapabilityRegistry(path).governance_history(invalid, "acct-a")
    assert not path.exists()


def test_governance_valid_non_ascii_identity_survives_restart(tmp_path) -> None:
    from dataclasses import replace

    path = tmp_path / "registry.json"
    evidence = replace(_governance(), jurisdiction="Україна")
    assert BookmakerCapabilityRegistry(path).register_governance(evidence)
    reopened = BookmakerCapabilityRegistry(path)
    assert reopened.governance_history("book-a", "acct-a") == (evidence,)
