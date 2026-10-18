"""Plan 6 / 6B negative and restart-safe preflight contract tests."""

import pytest

from autosport.windows_update_admission import (
    InstalledWindowsProduct,
    WindowsUpdateAdmissionError,
    WindowsUpdateCandidate,
    assess_windows_update,
)


OLD = "a" * 64
NEW = "b" * 64


def installed(**kwargs):
    return InstalledWindowsProduct(**({"version": "1.2.3", "schema": 3, "package_sha256": OLD} | kwargs))


def candidate(**kwargs):
    return WindowsUpdateCandidate(**({"version": "1.2.4", "schema": 3, "package_sha256": NEW, "verified_package_sha256": NEW, "signature_verified": True} | kwargs))


def test_verified_forward_candidate_is_only_preflight_eligible():
    result = assess_windows_update(installed(), candidate(), supported_schema_migrations=())
    assert result.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert (result.from_schema, result.to_schema) == (3, 3)


def test_exact_reinstall_is_noop_and_repeatable():
    same = candidate(version="1.2.3", package_sha256=OLD, verified_package_sha256=OLD)
    for _ in range(3):
        assert assess_windows_update(installed(), same, supported_schema_migrations=()).disposition == "NO_CHANGE"


@pytest.mark.parametrize("changed", [
    {"version": "1.2.2"},
    {"schema": 2},
    {"version": "1.2.3"},
    {"version": "1.2.3", "schema": 4},
    {"signature_verified": False},
    {"verified_package_sha256": OLD},
])
def test_downgrade_repack_unverified_or_tampered_rejected(changed):
    # Supply an otherwise valid migration chain for the exact candidate.
    # A permanently invalid extra edge would make this test pass even if the
    # downgrade, signature or digest guard were accidentally removed.
    migrations = ((3, 4),) if changed.get("schema") == 4 else ()
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), candidate(**changed), supported_schema_migrations=migrations)


def test_explicit_complete_migration_chain_required():
    upgraded = candidate(version="2.0.0", schema=5)
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), upgraded, supported_schema_migrations=((3, 4),))
    assert assess_windows_update(installed(), upgraded, supported_schema_migrations=((3, 4), (4, 5))).to_schema == 5


@pytest.mark.parametrize("edges", [((3, 5),), ((3, 4), (4, True)), ((3, 4), (3, 4)), ("3->4",)])
def test_bad_migration_evidence_is_rejected(edges):
    upgraded = candidate(version="2.0.0", schema=5)
    if edges == ((3, 4), (3, 4)):
        with pytest.raises(WindowsUpdateAdmissionError):
            assess_windows_update(installed(), upgraded, supported_schema_migrations=edges)
    else:
        with pytest.raises(WindowsUpdateAdmissionError):
            assess_windows_update(installed(), upgraded, supported_schema_migrations=edges)


@pytest.mark.parametrize("bad", ["01.2.3", "1.2", "1.2.3.4", "1.2.-1", "1.2.3 ", "1.2.٣", True, 1, "999999999999.1.1"])
def test_malformed_versions_fail_closed(bad):
    with pytest.raises(WindowsUpdateAdmissionError):
        installed(version=bad)


def test_bool_schema_and_invalid_sha_fail_closed():
    with pytest.raises(WindowsUpdateAdmissionError):
        installed(schema=True)
    with pytest.raises(WindowsUpdateAdmissionError):
        candidate(package_sha256="B" * 64)


def test_forged_object_revalidated_at_admission():
    state = installed()
    object.__setattr__(state, "schema", -1)
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(state, candidate(), supported_schema_migrations=())


@pytest.mark.parametrize("edges", [
    ((3, 4), (4, 5), (4, 5)),
    ((4, 5), (3, 4)),
    ((3, 4), (5, 6)),
    ((3, 4), (4, 6)),
    ((3, 4), (4, 5), (5, 6)),
    ((3, 4), (4, 5), (4, 5)),
])
def test_migration_chain_rejects_duplicates_gaps_out_of_order_and_extras(edges):
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), candidate(version="2.0.0", schema=5), supported_schema_migrations=edges)


@pytest.mark.parametrize("edges", [[], iter(()), ((3, 4),), ("3->4",)])
def test_noop_rejects_any_noncanonical_migration_evidence(edges):
    same = candidate(version="1.2.3", package_sha256=OLD, verified_package_sha256=OLD)
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), same, supported_schema_migrations=edges)


def test_forward_no_schema_change_rejects_hidden_migrations():
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), candidate(), supported_schema_migrations=((3, 4),))


def test_hostile_migration_iterator_never_executed():
    class Hostile:
        def __iter__(self):
            raise AssertionError("untrusted migration iterator executed")
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), candidate(version="2.0.0", schema=4), supported_schema_migrations=Hostile())


def test_current_release_version_is_not_misrepresented_as_update_version():
    # Existing BUILD_INFO.json uses this historic build label, not a monotonic
    # update version. Admission must fail until signed monotonic metadata exists.
    with pytest.raises(WindowsUpdateAdmissionError):
        candidate(version="0.1.0-v1-prehuman")


def test_admission_never_grants_installation_authority():
    result = assess_windows_update(installed(), candidate(), supported_schema_migrations=())
    assert result.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert result.install_authorized is False


def test_forged_candidate_digest_revalidated_at_boundary():
    state = candidate()
    object.__setattr__(state, "verified_package_sha256", "not-a-digest")
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), state, supported_schema_migrations=())


def test_forged_candidate_signature_type_revalidated_at_boundary():
    state = candidate()
    object.__setattr__(state, "signature_verified", 1)
    with pytest.raises(WindowsUpdateAdmissionError):
        assess_windows_update(installed(), state, supported_schema_migrations=())


def test_schema_upgrade_with_exact_chain_remains_preflight_only():
    decision = assess_windows_update(
        installed(),
        candidate(version="1.3.0", schema=4),
        supported_schema_migrations=((3, 4),),
    )
    assert decision.disposition == "ELIGIBLE_FOR_TRANSACTION_PREPARE"
    assert decision.install_authorized is False
    assert (decision.from_schema, decision.to_schema) == (3, 4)

@pytest.mark.parametrize("disposition,old,new,old_schema,new_schema", [
    ("UNKNOWN", "1.0.0", "1.1.0", 1, 1),
    ("NO_CHANGE", "1.0.0", "1.0.1", 1, 1),
    ("NO_CHANGE", "1.0.0", "1.0.0", 1, 2),
    ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.1.0", "1.0.0", 1, 1),
    ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.0", 1, 1),
    ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.1.0", 2, 1),
    ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "01.0.0", "1.1.0", 1, 1),
    ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.1.0", True, 1),
    ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.1.0", 1, True),
])
def test_decision_constructor_rejects_forged_transition(disposition, old, new, old_schema, new_schema):
    from autosport.windows_update_admission import WindowsUpdateDecision
    with pytest.raises(WindowsUpdateAdmissionError):
        WindowsUpdateDecision(disposition, old, new, old_schema, new_schema)


def test_decision_revalidation_rejects_postconstruction_downgrade():
    from autosport.windows_update_admission import WindowsUpdateDecision
    decision = WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.1.0", 1, 1)
    object.__setattr__(decision, "to_schema", 0)
    with pytest.raises(WindowsUpdateAdmissionError):
        decision.__post_init__()


def test_decision_valid_transitions_remain_non_authorizing():
    from autosport.windows_update_admission import WindowsUpdateDecision
    for args in (
        ("NO_CHANGE", "1.0.0", "1.0.0", 1, 1),
        ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.0.1", 1, 1),
        ("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "2.0.0", 1, 2),
    ):
        assert WindowsUpdateDecision(*args).install_authorized is False
