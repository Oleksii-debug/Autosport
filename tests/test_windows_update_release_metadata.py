"""Release builder/validator contract: explicit identity, no silent upgrade."""

from __future__ import annotations

import copy

import pytest

from autosport.windows_update_release_metadata import (
    WindowsUpdateReleaseMetadataError,
    attach_update_identity,
    validate_requested_update_identity,
    validate_embedded_update_identity,
)


_BASE = {
    "product": "Autosport",
    "version": "0.1.0-v1-prehuman",
    "source_sha": "a" * 40,
    "real_money_execution": False,
    "human_tested": False,
    "nvda_verified": False,
}


def test_legacy_release_unchanged_and_not_update_authorized():
    original = copy.deepcopy(_BASE)
    result = attach_update_identity(original)
    assert result == original
    assert result is not original
    validate_embedded_update_identity(result)
    assert "update_version" not in result
    assert "update_schema" not in result


def test_explicit_update_identity_is_canonical_and_no_money_authority():
    original = copy.deepcopy(_BASE)
    result = attach_update_identity(original, update_version="1.2.3", update_schema=2)
    assert original == _BASE
    assert result["version"] == result["update_version"] == "1.2.3"
    assert result["update_schema"] == 2
    assert result["real_money_execution"] is False
    validate_embedded_update_identity(result)


@pytest.mark.parametrize("version,schema", [
    (None, 1), ("1.2.3", None), ("1.2.3-rc1", 1), ("01.2.3", 1),
    ("1.2.3", True), ("1.2.3", 0), ("1.2.3", "1"),
    ("999999999999.0.0", 1), (1, 1), ("1.2.3", 2**31),
])
def test_invalid_or_partial_builder_identity_fails_closed(version, schema):
    with pytest.raises(WindowsUpdateReleaseMetadataError):
        attach_update_identity(_BASE, update_version=version, update_schema=schema)


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(update_schema=None),
    lambda p: p.update(update_schema=True),
    lambda p: p.update(update_schema=0),
    lambda p: p.update(update_version="1.2.3-prehuman"),
    lambda p: p.update(version="2.0.0"),
    lambda p: p.pop("update_version"),
    lambda p: p.pop("update_schema"),
])
def test_verifier_rejects_tampered_update_identity(mutate):
    result = attach_update_identity(_BASE, update_version="1.2.3", update_schema=2)
    mutate(result)
    with pytest.raises(WindowsUpdateReleaseMetadataError):
        validate_embedded_update_identity(result)


def test_verifier_rejects_identity_injected_into_legacy():
    with pytest.raises(WindowsUpdateReleaseMetadataError):
        validate_embedded_update_identity({**_BASE, "update_schema": 1})


def test_builder_rejects_prepopulated_identity():
    with pytest.raises(WindowsUpdateReleaseMetadataError):
        attach_update_identity({**_BASE, "update_version": "1.2.3"}, update_version="1.2.3", update_schema=1)


def test_builder_rejects_product_and_version_type_forgery():
    for invalid in ({**_BASE, "product": "Other"}, {**_BASE, "version": 1}):
        with pytest.raises(WindowsUpdateReleaseMetadataError):
            attach_update_identity(invalid)


@pytest.mark.parametrize("version,schema", [
    ("1.2.3", None), (None, 1), ("1.2.3", False), ("1.2.3", 0),
    ("1.2.3-rc", 1),
])
def test_request_prevalidation_rejects_before_filesystem_work(version, schema):
    with pytest.raises(WindowsUpdateReleaseMetadataError):
        validate_requested_update_identity(update_version=version, update_schema=schema)


def test_request_prevalidation_allows_legacy_and_canonical_pair():
    validate_requested_update_identity()
    validate_requested_update_identity(update_version="1.2.3", update_schema=1)
