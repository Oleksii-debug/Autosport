import pytest

from autosport.bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from autosport.bookmaker_capability_matrix import (
    BookmakerCapabilityMatrix,
    BookmakerCapabilityMatrixError,
)


_TS = "2026-09-21T09:00:00+00:00"
_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _profile(
    *,
    venue_id: str = "book-a",
    account_id: str = "acct-a",
    adapter_id: str = "adapter-a",
    adapter_version: str = "1.0",
    profile_version: int = 1,
    facts: tuple[BookmakerCapabilityFact, ...] = (),
    observed_at: str = _TS,
    source_ref: str = "provider-capability-probe",
    source_payload_sha256: str = _HASH_A,
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        profile_version=profile_version,
        facts=facts,
        observed_at=observed_at,
        source_ref=source_ref,
        source_payload_sha256=source_payload_sha256,
    )


def _fact(
    capability: BookmakerCapability,
    state: BookmakerCapabilityState,
) -> BookmakerCapabilityFact:
    return BookmakerCapabilityFact(capability=capability, state=state)


def test_matrix_is_deterministic_across_profile_input_order() -> None:
    later = _profile(
        venue_id="book-z",
        account_id="acct-z",
        adapter_id="adapter-z",
    )
    earlier = _profile(
        venue_id="book-a",
        account_id="acct-a",
        adapter_id="adapter-a",
    )

    forward = BookmakerCapabilityMatrix((later, earlier)).to_summary()
    reverse = BookmakerCapabilityMatrix((earlier, later)).to_summary()

    assert forward == reverse
    assert [row["profile_id"] for row in forward["rows"]] == [
        earlier.profile_id,
        later.profile_id,
    ]


def test_matrix_projects_every_capability_and_preserves_tri_state_truth() -> None:
    profile = _profile(
        facts=(
            _fact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            _fact(
                BookmakerCapability.CANCEL_BET,
                BookmakerCapabilityState.UNSUPPORTED,
            ),
        )
    )

    summary = BookmakerCapabilityMatrix((profile,)).to_summary()
    row = summary["rows"][0]
    cells = row["capabilities"]

    assert summary["capability_order"] == sorted(
        capability.value for capability in BookmakerCapability
    )
    assert set(cells) == {capability.value for capability in BookmakerCapability}
    assert cells[BookmakerCapability.BALANCE_READ.value]["state"] == "supported"
    assert cells[BookmakerCapability.CANCEL_BET.value]["state"] == "unsupported"
    assert cells[BookmakerCapability.LIVE_QUOTES_READ.value]["state"] == "unknown"


def test_every_cell_carries_exact_profile_provenance() -> None:
    profile = _profile(
        observed_at="2026-09-21T11:00:00+02:00",
        source_ref="official-provider-capability-response",
        source_payload_sha256=_HASH_B,
    )

    row = BookmakerCapabilityMatrix((profile,)).to_summary()["rows"][0]
    assert row["observed_at"] == profile.observed_at
    assert row["source_ref"] == profile.source_ref
    assert row["source_payload_sha256"] == profile.source_payload_sha256

    for cell in row["capabilities"].values():
        assert cell == {
            "state": "unknown",
            "profile_id": profile.profile_id,
            "observed_at": profile.observed_at,
            "source_ref": profile.source_ref,
            "source_payload_sha256": profile.source_payload_sha256,
        }


def test_matrix_rejects_duplicate_profile_identity() -> None:
    profile = _profile()

    with pytest.raises(BookmakerCapabilityMatrixError, match="duplicate profile_id"):
        BookmakerCapabilityMatrix((profile, profile))


def test_matrix_rejects_two_evidence_records_for_same_scope_version() -> None:
    first = _profile(
        facts=(
            _fact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        ),
        source_payload_sha256=_HASH_A,
    )
    second = _profile(
        facts=(
            _fact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.UNSUPPORTED,
            ),
        ),
        source_payload_sha256=_HASH_B,
    )
    assert first.profile_id != second.profile_id

    with pytest.raises(
        BookmakerCapabilityMatrixError,
        match="duplicate provider scope/version",
    ):
        BookmakerCapabilityMatrix((first, second))


def test_matrix_allows_distinct_profile_versions_for_same_scope() -> None:
    version_one = _profile(profile_version=1)
    version_two = _profile(profile_version=2, source_payload_sha256=_HASH_B)

    matrix = BookmakerCapabilityMatrix((version_two, version_one))

    assert [profile.profile_version for profile in matrix.profiles] == [1, 2]


def test_state_lookup_is_derived_from_profile_not_caller_cells() -> None:
    profile = _profile(
        facts=(
            _fact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
        )
    )
    matrix = BookmakerCapabilityMatrix((profile,))

    assert (
        matrix.state_of(profile.profile_id, BookmakerCapability.BALANCE_READ)
        is BookmakerCapabilityState.SUPPORTED
    )
    assert (
        matrix.state_of(profile.profile_id, BookmakerCapability.LIVE_QUOTES_READ)
        is BookmakerCapabilityState.UNKNOWN
    )
    with pytest.raises(BookmakerCapabilityMatrixError, match="unknown profile_id"):
        matrix.state_of("caller-invented-profile", BookmakerCapability.BALANCE_READ)
