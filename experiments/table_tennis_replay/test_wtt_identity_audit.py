import hashlib

import pytest

from test_wtt_archive_inventory import COMMON, archive, csv_bytes, samples
from wtt_identity_audit import audit


def raw_sample():
    men, women = samples()
    return archive(csv_bytes(men, COMMON + ["a_id_raw", "x_id_raw"]), csv_bytes(women, COMMON))


def test_fresh_empty_registry_cannot_resolve_source_ids_or_womens_names():
    raw = raw_sample()
    result = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), lookup_as_of="2026-10-30T05:02:46Z")
    assert result["mens_distinct_raw_ids"] == result["mens_unresolved"] == 3
    assert result["mens_resolved"] == 0
    assert result["womens_identity_resolution"] == "not_attempted_no_native_id_fields"
    assert result["registry_mode"] == "fresh_empty_test_only"
    assert result["training_authorized"] is result["source_truth_verified"] is False


def test_bad_source_hash_and_naive_lookup_time_are_rejected():
    raw = raw_sample()
    with pytest.raises(ValueError, match="SHA-256"):
        audit(raw, expected_sha256="0" * 64, lookup_as_of="2026-10-30T05:02:46Z")
    with pytest.raises(ValueError, match="timezone"):
        audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), lookup_as_of="2026-10-30")


def test_womens_ids_present_are_not_incorrectly_reported_as_absent():
    men, women = samples(); women[0].update(a_id_raw="4", x_id_raw="5")
    fields = COMMON + ["a_id_raw", "x_id_raw"]
    raw = archive(csv_bytes(men, fields), csv_bytes(women, fields))
    result = audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), lookup_as_of="2026-10-30T05:02:46Z")
    assert result["womens_native_id_fields_present"] is True
    assert result["womens_identity_resolution"] == "not_attempted_in_this_audit"


def test_invalid_numeric_source_id_fails_closed():
    men, women = samples(); men[0]["a_id_raw"] = "1.0"
    raw = archive(csv_bytes(men, COMMON + ["a_id_raw", "x_id_raw"]), csv_bytes(women, COMMON))
    with pytest.raises(ValueError, match="ASCII digit"):
        audit(raw, expected_sha256=hashlib.sha256(raw).hexdigest(), lookup_as_of="2026-10-30T05:02:46Z")
