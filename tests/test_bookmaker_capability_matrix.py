from __future__ import annotations

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


def _profile(
    *,
    venue_id: str = "venue-a",
    account_id: str = "account-a",
    adapter_id: str = "adapter-a",
    adapter_version: str = "1.0",
    profile_version: int = 1,
    observed_at: str = "2026-09-21T07:00:00+00:00",
    facts: tuple[BookmakerCapabilityFact, ...] = (),
    source_seed: str = "a",
) -> BookmakerCapabilityProfile:
    return BookmakerCapabilityProfile(
        venue_id=venue_id,
        account_id=account_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        profile_version=profile_version,
        facts=facts,
        observed_at=observed_at,
        source_ref=f"evidence://{venue_id}/{account_id}/{profile_version}",
        source_payload_sha256=source_seed * 64,
    )


def test_matrix_expands_missing_facts_to_unknown_without_write_authority() -> None:
    profile = _profile(
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.BALANCE_READ,
                BookmakerCapabilityState.SUPPORTED,
            ),
            BookmakerCapabilityFact(
                BookmakerCapability.PLACE_BET,
                BookmakerCapabilityState.SUPPORTED,
            ),
        )
    )

    matrix = BookmakerCapabilityMatrix(
        profiles=(profile,),
        as_of="2026-09-21T07:30:00+00:00",
    )
    payload = matrix.to_canonical_dict()
    row = payload["profiles"][0]

    assert row["states"][BookmakerCapability.BALANCE_READ.value] == "supported"
    assert row["states"][BookmakerCapability.PLACE_BET.value] == "supported"
    assert row["states"][BookmakerCapability.CASHOUT.value] == "unknown"
    assert set(row["states"]) == {
        capability.value for capability in BookmakerCapability
    }
    assert payload["provider_write_authorized"] is False
    assert payload["real_money_execution"] is False
    assert matrix.provider_write_authorized is False
    assert matrix.real_money_execution is False


def test_matrix_binds_profile_evidence_identity_and_source() -> None:
    profile = _profile(
        facts=(
            BookmakerCapabilityFact(
                BookmakerCapability.LIVE_QUOTES_READ,
                BookmakerCapabilityState.UNSUPPORTED,
            ),
        )
    )
    matrix = BookmakerCapabilityMatrix(
        profiles=(profile,),
        as_of="2026-09-21T07:30:00+00:00",
    )

    row = matrix.to_canonical_dict()["profiles"][0]
    assert row["profile_id"] == profile.profile_id
    assert row["profile_version"] == profile.profile_version
    assert row["source_ref"] == profile.source_ref
    assert row["source_payload_sha256"] == profile.source_payload_sha256
    assert row["states"][BookmakerCapability.LIVE_QUOTES_READ.value] == "unsupported"


def test_matrix_identity_is_independent_of_input_profile_order() -> None:
    first = _profile(source_seed="a")
    second = _profile(
        venue_id="venue-b",
        account_id="account-b",
        adapter_id="adapter-b",
        source_seed="b",
    )

    left = BookmakerCapabilityMatrix(
        profiles=(first, second),
        as_of="2026-09-21T07:30:00+00:00",
    )
    right = BookmakerCapabilityMatrix(
        profiles=(second, first),
        as_of="2026-09-21T07:30:00+00:00",
    )

    assert left.to_canonical_dict() == right.to_canonical_dict()
    assert left.matrix_id == right.matrix_id


def test_matrix_rejects_duplicate_venue_account_adapter_scope() -> None:
    first = _profile(source_seed="a")
    revised = _profile(
        adapter_version="2.0",
        profile_version=2,
        source_seed="b",
    )

    with pytest.raises(BookmakerCapabilityMatrixError, match="duplicate venue/account/adapter"):
        BookmakerCapabilityMatrix(
            profiles=(first, revised),
            as_of="2026-09-21T07:30:00+00:00",
        )


def test_matrix_rejects_profile_observed_after_as_of() -> None:
    profile = _profile(observed_at="2026-09-21T07:31:00+00:00")

    with pytest.raises(BookmakerCapabilityMatrixError, match="after matrix as_of"):
        BookmakerCapabilityMatrix(
            profiles=(profile,),
            as_of="2026-09-21T07:30:00+00:00",
        )


@pytest.mark.parametrize(
    "profiles,as_of,match",
    [
        ((), "2026-09-21T07:30:00+00:00", "must not be empty"),
        ((object(),), "2026-09-21T07:30:00+00:00", "BookmakerCapabilityProfile"),
        ((_profile(),), "2026-09-21T07:30:00", "timezone offset"),
    ],
)
def test_matrix_rejects_vacuous_or_unqualified_evidence(
    profiles: tuple[object, ...],
    as_of: str,
    match: str,
) -> None:
    with pytest.raises(BookmakerCapabilityMatrixError, match=match):
        BookmakerCapabilityMatrix(profiles=profiles, as_of=as_of)  # type: ignore[arg-type]


@pytest.mark.parametrize("schema_version", [True, 1.0, 2])
def test_matrix_rejects_noncanonical_schema_revision(schema_version: object) -> None:
    with pytest.raises(BookmakerCapabilityMatrixError, match="schema_version"):
        BookmakerCapabilityMatrix(
            profiles=(_profile(),),
            as_of="2026-09-21T07:30:00+00:00",
            schema_version=schema_version,  # type: ignore[arg-type]
        )


def test_matrix_rejects_profile_subclasses_from_canonical_projection() -> None:
    class DerivedBookmakerCapabilityProfile(BookmakerCapabilityProfile):
        pass

    profile = DerivedBookmakerCapabilityProfile(
        venue_id="venue-a",
        account_id="account-a",
        adapter_id="adapter-a",
        adapter_version="1.0",
        profile_version=1,
        facts=(),
        observed_at="2026-09-21T07:00:00+00:00",
        source_ref="evidence://venue-a/account-a/1",
        source_payload_sha256="a" * 64,
    )

    with pytest.raises(BookmakerCapabilityMatrixError, match="exact BookmakerCapabilityProfile"):
        BookmakerCapabilityMatrix(
            profiles=(profile,),
            as_of="2026-09-21T07:30:00+00:00",
        )


def test_matrix_rejects_mutable_capability_fact_subclass_after_profile_validation() -> None:
    class MutableCapabilityFact(BookmakerCapabilityFact):
        forged = False

        def __getattribute__(self, name: str):
            if name == "state" and type(self).forged:
                return BookmakerCapabilityState.SUPPORTED
            return super().__getattribute__(name)

    fact = MutableCapabilityFact(
        BookmakerCapability.BALANCE_READ,
        BookmakerCapabilityState.UNSUPPORTED,
    )
    profile = _profile(facts=(fact,))
    MutableCapabilityFact.forged = True

    with pytest.raises(BookmakerCapabilityMatrixError, match="exact BookmakerCapabilityFact"):
        BookmakerCapabilityMatrix(
            profiles=(profile,),
            as_of="2026-09-21T07:30:00+00:00",
        )
