"""Direct build-host metadata is evidence only, never a full SBOM assertion."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from autosport.release_sbom import (
    capture_direct_installed_dependencies, validate_direct_dependency_metadata,
)

SHA = "a" * 40
SOURCE = {
    "source_sha": SHA,
    "declared_runtime_dependencies": ["APScheduler==3.11.3", "tk-uia==0.8.0"],
    "declared_build_dependencies": ["pyinstaller==6.22.3"],
}


def _fake_distribution(name: str):
    versions = {"apscheduler": "3.11.3", "tk-uia": "0.8.0", "pyinstaller": "6.22.3"}
    labels = {"apscheduler": "APScheduler", "tk-uia": "tk_uia", "pyinstaller": "pyinstaller"}
    return SimpleNamespace(
        version=versions[name],
        metadata={"Name": labels[name], "License-Expression": "MIT" if name == "tk-uia" else None},
    )


def test_build_host_direct_metadata_is_deterministic_and_honest():
    with patch("autosport.release_sbom.metadata.distribution", side_effect=_fake_distribution):
        one = capture_direct_installed_dependencies(SOURCE)
        two = capture_direct_installed_dependencies(SOURCE)
    assert one == two
    assert one["source_sha"] == SHA
    assert one["evidence_class"] == "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY"
    assert [entry["name"] for entry in one["distributions"]] == [
        "apscheduler", "pyinstaller", "tk-uia"
    ]
    assert one["distributions"][-1]["license_evidence"] == "DISTRIBUTION_METADATA_UNVERIFIED"
    assert one["distributions"][0]["license_evidence"] == "NOT_PROVIDED"


@pytest.mark.parametrize("edit", [
    lambda x: x.update(schema_version=True),
    lambda x: x.update(evidence_class="COMPLETE_SBOM_VERIFIED"),
    lambda x: x["distributions"].pop(),
    lambda x: x["distributions"][0].update(version="999"),
    lambda x: x["distributions"][0].update(name="APScheduler"),
    lambda x: x["distributions"][0].update(license_evidence="LICENSE_CLEARED"),
    lambda x: x["distributions"][0].update(license_expression="MIT"),
    lambda x: x["distributions"].reverse(),
])
def test_rejects_forged_or_incomplete_metadata(edit):
    with patch("autosport.release_sbom.metadata.distribution", side_effect=_fake_distribution):
        original = capture_direct_installed_dependencies(SOURCE)
    tampered = deepcopy(original)
    edit(tampered)
    with pytest.raises(ValueError):
        validate_direct_dependency_metadata(tampered, SOURCE)


def test_rejects_missing_or_wrong_distribution_before_release():
    with patch(
        "autosport.release_sbom.metadata.distribution",
        side_effect=__import__("importlib").metadata.PackageNotFoundError,
    ):
        with pytest.raises(ValueError, match="not installed"):
            capture_direct_installed_dependencies(SOURCE)
    wrong = lambda name: SimpleNamespace(
        version="999", metadata={"Name": name, "License-Expression": "MIT"}
    )
    with patch("autosport.release_sbom.metadata.distribution", side_effect=wrong):
        with pytest.raises(ValueError, match="differs"):
            capture_direct_installed_dependencies(SOURCE)


def test_rejects_duplicate_names_and_conflicting_pins():
    bad = deepcopy(SOURCE)
    bad["declared_runtime_dependencies"].append("tk_uia==0.8.0")
    with pytest.raises(ValueError, match="duplicate"):
        capture_direct_installed_dependencies(bad)
    bad = deepcopy(SOURCE)
    bad["declared_build_dependencies"].append("TK_UIA==9.0.0")
    with pytest.raises(ValueError, match="conflicting"):
        capture_direct_installed_dependencies(bad)
