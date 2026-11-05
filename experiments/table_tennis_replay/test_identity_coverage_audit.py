from identity_coverage_audit import audit, compare_sessions
from test_quote_delay_audit import build_session, match


def test_label_overlap_and_field_presence_never_verify_identity(tmp_path):
    a, b = match("a", 0), match("b", 0)
    b["home"] = a["home"]
    b["quotes"][0]["side"] = a["home"]
    b["home_player_id"] = "unverified-id"
    build_session(tmp_path, [(0, [a, b]), (40, [match("a", 40)])])
    report = audit(tmp_path)
    first = report["snapshots"][0]
    assert first["eligible_pairs"] == first["pairs_sharing_exact_source_name"] == 1
    assert first["candidate_player_id_fields_populated"]["home_player_id"] == 1
    assert report["identity_verified"] is report["source_names_resolve_people"] is False


def test_different_names_do_not_establish_distinct_people(tmp_path):
    build_session(tmp_path, [(0, [match("a", 0), match("b", 0)]),
                             (40, [match("a", 40), match("b", 40)])])
    report = audit(tmp_path)
    assert report["snapshots"][0]["pairs_sharing_exact_source_name"] == 0
    assert report["identity_verified"] is False


def test_compare_reports_exact_overlap_without_merging_people(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    build_session(first, [(0, [match("a", 0)]), (40, [match("a", 40)])])
    build_session(second, [(0, [match("a", 0), match("b", 0)]),
                           (40, [match("a", 40), match("b", 40)])])
    report = compare_sessions(first, second)
    assert report["common_fixture_ids"] == 1
    assert report["common_exact_name_strings"] == 2
    assert report["participant_identity_verified"] is report["independence_verified"] is False
