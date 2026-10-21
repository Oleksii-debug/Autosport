"""The stage-neutral release manifest must require an exact integer schema.

JSON true and 1.0 compare equal to integer 1 in Python, but accepting them
would turn ambiguous update/rollback metadata into a supported release schema.
"""

from __future__ import annotations

import hashlib

import pytest

from autosport import stage_neutral_release


def _members(schema_version: object) -> dict[str, bytes]:
    payload = b"exact-owner-release-payload"
    manifest = {
        "schema_version": schema_version,
        "files": {"payload.bin": hashlib.sha256(payload).hexdigest()},
    }
    return {
        "payload.bin": payload,
        "PACKAGE_MANIFEST.json": stage_neutral_release._canonical_json_bytes(manifest),
    }


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", None])
def test_stage_neutral_manifest_rejects_noninteger_schema(schema_version: object) -> None:
    with pytest.raises(ValueError, match="schema is invalid"):
        stage_neutral_release._validate_manifest(
            _members(schema_version), label="stage-neutral release package"
        )


def test_stage_neutral_manifest_accepts_exact_integer_schema() -> None:
    manifest = stage_neutral_release._validate_manifest(
        _members(1), label="stage-neutral release package"
    )
    assert type(manifest["schema_version"]) is int
    assert manifest["schema_version"] == 1
