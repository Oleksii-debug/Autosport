"""Plan 6 §5Б: sealed EXE inventories must reject secret and test payloads.

The ZIP admission path already guards ordinary files; this verifies the
*embedded* CArchive table cannot become a bypass after manifest resealing.
No complete SBOM/license-clearance assertion is made here.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from autosport.release_sbom import (
    capture_embedded_executable_inventory,
    validate_embedded_executable_inventory,
)

SOURCE = "a" * 40


@pytest.fixture
def clean_inventory(tmp_path: Path) -> dict:
    executable = tmp_path / "Autosport.exe"
    executable.write_bytes(b"\0" * 8192)
    return capture_embedded_executable_inventory(
        executable, SOURCE,
        reader_factory=lambda _: SimpleNamespace(
            toc={"PYZ.pyz": (0, 256, 1024, True, "z")},
        ),
    )


@pytest.mark.parametrize("forbidden", [
    ".env", "config/.env.production", "credentials.json",
    "config/token.json", "secrets.toml", "client_secret_123.json",
    "private/id_rsa", "private-key.pem", "settings/cert.pfx",
    "settings/account.key", "app/session.session-journal",
    "build/__pycache__/secret", "build/.pytest_cache/ignored",
    "build/.hypothesis/example", "build/.nox/fixture",
    "profiles/.aws/credentials", "profiles/.ssh/id_ed25519",
    "profile/cookies.sqlite", "workspace/.coverage.123",
    "private\\secrets.json",
])
def test_rehashed_embedded_inventory_cannot_smuggle_private_or_test_members(
    clean_inventory: dict, forbidden: str,
) -> None:
    poisoned = deepcopy(clean_inventory)
    poisoned["components"][0]["name"] = forbidden
    # The enclosing ZIP/package manifest can be recomputed by an adversary:
    # independently validate semantic member policy, not only hashes.
    with pytest.raises(ValueError, match="forbidden private/test state"):
        validate_embedded_executable_inventory(
            poisoned,
            source_sha=SOURCE,
            executable_sha256=poisoned["executable_sha256"],
        )


@pytest.mark.parametrize("allowed", [
    "PYZ.pyz", "python312.dll", "stdlib/lib.zip",
    "licenses/LICENSE.txt", "templates/.env.example", "templates/.env.sample",
    "templates/.env.template", "templates/.env.dist",
])
def test_legitimate_carchive_payload_names_are_unaffected(
    clean_inventory: dict, allowed: str,
) -> None:
    inventory = deepcopy(clean_inventory)
    inventory["components"][0]["name"] = allowed
    validate_embedded_executable_inventory(
        inventory,
        source_sha=SOURCE,
        executable_sha256=inventory["executable_sha256"],
    )


def test_sealed_executable_capture_rejects_embedded_secret_before_publish(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "Autosport.exe"
    executable.write_bytes(b"\0" * 8192)
    with pytest.raises(ValueError, match="forbidden private/test state"):
        capture_embedded_executable_inventory(
            executable, SOURCE,
            reader_factory=lambda _: SimpleNamespace(
                toc={"private/credentials.json": (0, 100, 100, True, "b")},
            ),
        )


def test_both_release_consumer_paths_use_one_inventory_validator() -> None:
    root = Path(__file__).resolve().parents[1]
    release = (root / "src/autosport/release_package.py").read_text("utf-8")
    data = (root / "src/autosport/data_tool_package.py").read_text("utf-8")
    assert "validate_embedded_executable_inventory(" in release
    assert "validate_embedded_executable_inventory(" in data
    assert '"EMBEDDED_EXECUTABLE_INVENTORY.json"' in release
    assert '"DATA_EXECUTABLE_INVENTORY.json"' in data
