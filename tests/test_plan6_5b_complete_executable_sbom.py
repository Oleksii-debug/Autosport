"""Plan 6 §5Б complete sealed-PYZ ownership and license-evidence regressions."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from autosport.release_binary_attribution import (
    capture_complete_binary_sbom,
    validate_binary_module_attribution,
)
import autosport.release_sbom as release_sbom


SHA = "a" * 40
DIGEST = "b" * 64


def _inventory():
    names = ["autosport.core", "example.api", "json.decoder"]
    return {
        "schema_version": 2,
        "source_sha": SHA,
        "executable_sha256": DIGEST,
        "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
        "components": [{
            "name": "PYZ-00.pyz", "typecode": "z",
            "packed_bytes": 30, "unpacked_bytes": 90,
        }],
        "pyz_modules": [
            {"name": name, "typecode": 0, "position": index * 10, "packed_bytes": 10}
            for index, name in enumerate(names)
        ],
    }


def _graph(name="example-dist", version="1.2.3"):
    return {
        "schema_version": 1,
        "source_sha": SHA,
        "evidence_class": "BUILD_HOST_REQUIREMENT_GRAPH_ONLY",
        "components": [{
            "name": name, "version": version, "direct_roles": ["runtime"],
            "requires": [], "license_expression": None,
            "license_evidence": "NOT_PROVIDED",
        }],
    }


def test_complete_sbom_has_exact_single_owner_and_zero_unresolved_modules():
    report = capture_complete_binary_sbom(
        _graph(), _inventory(),
        packages_distributions={"example": ["Example_Dist"]},
        stdlib_module_names={"json"}, python_version="3.12.10",
    )
    assert [(item["module"], item["owner_kind"], item["owner"])
            for item in report["modules"]] == [
        ("autosport.core", "FIRST_PARTY", "autosport"),
        ("example.api", "DISTRIBUTION", "example-dist"),
        ("json.decoder", "PYTHON_STDLIB", "python"),
    ]
    assert report["summary"] == {
        "total_modules": 3,
        "distribution_modules": 1,
        "first_party_modules": 1,
        "python_stdlib_modules": 1,
        "unresolved_modules": 0,
        "ambiguous_modules": 0,
    }
    validate_binary_module_attribution(report, _graph(), _inventory())


def test_complete_sbom_fails_closed_on_unknown_or_ambiguous_owner():
    unknown = _inventory()
    unknown["pyz_modules"][2]["name"] = "unowned.module"
    with pytest.raises(ValueError, match="unresolved module owner"):
        capture_complete_binary_sbom(
            _graph(), unknown, packages_distributions={},
            stdlib_module_names={"json"}, python_version="3.12.10",
        )
    graph = _graph()
    graph["components"].append({
        **graph["components"][0], "name": "second-dist",
    })
    with pytest.raises(ValueError, match="ambiguous distribution owner"):
        capture_complete_binary_sbom(
            graph, _inventory(),
            packages_distributions={"example": ["example-dist", "second-dist"]},
            stdlib_module_names={"json"}, python_version="3.12.10",
        )


@pytest.mark.parametrize("mutation", [
    "forged_owner", "forged_stdlib", "unresolved_summary", "missing_module",
])
def test_complete_sbom_rejects_resealed_owner_or_summary_forgery(mutation):
    report = capture_complete_binary_sbom(
        _graph(), _inventory(), packages_distributions={"example": ["example-dist"]},
        stdlib_module_names={"json"}, python_version="3.12.10",
    )
    forged = deepcopy(report)
    if mutation == "forged_owner":
        forged["modules"][1]["owner"] = "not-in-graph"
    elif mutation == "forged_stdlib":
        forged["modules"][2]["module"] = "not_stdlib.module"
    elif mutation == "unresolved_summary":
        forged["summary"]["unresolved_modules"] = 1
    else:
        forged["modules"].pop()
    with pytest.raises(ValueError):
        validate_binary_module_attribution(forged, _graph(), _inventory())


def test_proxy_tools_missing_wheel_text_uses_pinned_upstream_evidence(monkeypatch):
    graph = _graph("proxy-tools", "0.1.0")
    dist = SimpleNamespace(
        metadata={"Name": "proxy_tools"}, version="0.1.0", files=[],
        locate_file=lambda value: Path("unreachable") / value,
    )
    monkeypatch.setattr(release_sbom.metadata, "distribution", lambda _: dist)
    manifest, members = release_sbom.capture_dependency_license_evidence(graph)
    assert manifest["schema_version"] == 2
    entry = manifest["distributions"][0]
    assert entry["license_review"] == "TEXT_PRESENT_REVIEW_REQUIRED"
    assert entry["files"][0]["origin"]["kind"] == "PINNED_UPSTREAM_SOURCE"
    assert list(members) == ["licenses/proxy-tools/000.txt"]
    release_sbom.validate_dependency_license_evidence(manifest, graph, members)


def test_proxy_tools_pinned_origin_or_payload_cannot_be_resealed(monkeypatch):
    graph = _graph("proxy-tools", "0.1.0")
    dist = SimpleNamespace(
        metadata={"Name": "proxy_tools"}, version="0.1.0", files=[],
        locate_file=lambda value: Path("unreachable") / value,
    )
    monkeypatch.setattr(release_sbom.metadata, "distribution", lambda _: dist)
    manifest, members = release_sbom.capture_dependency_license_evidence(graph)
    forged = deepcopy(manifest)
    forged["distributions"][0]["files"][0]["origin"]["source_blob_sha"] = "0" * 40
    with pytest.raises(ValueError, match="not pinned"):
        release_sbom.validate_dependency_license_evidence(forged, graph, members)
    changed = dict(members)
    changed["licenses/proxy-tools/000.txt"] += b"tamper"
    with pytest.raises(ValueError):
        release_sbom.validate_dependency_license_evidence(manifest, graph, changed)


def _runtime_license_evidence():
    members = {
        "runtime-licenses/000-cpython.txt": b"CPython license terms\n",
        "runtime-licenses/001-tcl.txt": b"Tcl license terms\n",
        "runtime-licenses/002-tk.txt": b"Tk license terms\n",
    }
    origins = {
        "cpython": "PYTHON_INSTALL_ROOT/LICENSE.txt",
        "tcl": "PYTHON_TCL_LIBRARY/license.terms",
        "tk": "PYTHON_TK_LIBRARY/license.terms",
    }
    manifest = {
        "schema_version": 1, "source_sha": SHA,
        "evidence_class": "SEALED_BUILD_RUNTIME_LICENSE_TEXT_NOT_CLEARANCE",
        "runtimes": [],
    }
    for index, (name, version) in enumerate((
        ("cpython", "3.12.10"), ("tcl", "8.6.14"), ("tk", "8.6"),
    )):
        path = f"runtime-licenses/{index:03d}-{name}.txt"
        manifest["runtimes"].append({
            "name": name, "version": version,
            "license_review": "TEXT_PRESENT_REVIEW_REQUIRED",
            "file": {
                "path": path, "sha256": sha256(members[path]).hexdigest(),
                "origin": origins[name],
            },
        })
    return manifest, members


def test_runtime_license_evidence_is_complete_and_source_bound():
    manifest, members = _runtime_license_evidence()
    release_sbom.validate_runtime_license_evidence(manifest, members, source_sha=SHA)
    for mutation in ("missing_tk", "wrong_source", "tampered_bytes", "forged_clearance"):
        forged = deepcopy(manifest)
        payloads = dict(members)
        source = SHA
        if mutation == "missing_tk":
            forged["runtimes"].pop()
        elif mutation == "wrong_source":
            source = "0" * 40
        elif mutation == "tampered_bytes":
            payloads["runtime-licenses/000-cpython.txt"] += b"tamper"
        else:
            forged["runtimes"][0]["license_review"] = "LICENSE_CLEARED"
        with pytest.raises(ValueError):
            release_sbom.validate_runtime_license_evidence(
                forged, payloads, source_sha=source,
            )


def _install_fake_tcl_tk_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tcl_version: str = "8.6.14",
    tk_version: str = "8.6.14",
    installed_license_names: tuple[str, ...] = (),
) -> None:
    base = tmp_path / "python"
    tcl_library = base / "tcl" / "tcl8.6"
    tk_library = base / "tcl" / "tk8.6"
    tcl_library.mkdir(parents=True)
    tk_library.mkdir(parents=True)
    (base / "LICENSE.txt").write_bytes(b"CPython installed license\n")
    (tk_library / "tk.tcl").write_bytes(
        f"# exact installed script binding\npackage require -exact Tk  {tk_version}\n".encode()
    )
    if "tcl" in installed_license_names:
        (tcl_library / "license.terms").write_bytes(b"Installed Tcl license\n")
    if "tk" in installed_license_names:
        (tk_library / "license.terms").write_bytes(b"Installed Tk license\n")

    class FakeTcl:
        def eval(self, expression: str) -> str:
            return {
                "info library": str(tcl_library),
                "info patchlevel": tcl_version,
            }[expression]

    monkeypatch.setattr(release_sbom.sys, "base_prefix", str(base))
    monkeypatch.setitem(
        sys.modules,
        "tkinter",
        SimpleNamespace(Tcl=FakeTcl, TkVersion=8.6),
    )


@pytest.mark.parametrize("runtime_version", ["8.6.13", "8.6.14"])
def test_runtime_license_capture_uses_exact_pinned_tcl_tk_release_when_files_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_version: str,
) -> None:
    _install_fake_tcl_tk_runtime(
        tmp_path, monkeypatch,
        tcl_version=runtime_version, tk_version=runtime_version,
    )

    manifest, members = release_sbom.capture_runtime_license_evidence(SHA)

    assert manifest["schema_version"] == 2
    assert manifest["evidence_class"] == (
        "SEALED_BUILD_AND_PINNED_UPSTREAM_RUNTIME_LICENSE_TEXT_NOT_CLEARANCE"
    )
    assert [item["version"] for item in manifest["runtimes"]] == [
        release_sbom.platform.python_version(), runtime_version, runtime_version,
    ]
    assert manifest["runtimes"][0]["file"]["origin"] == {
        "kind": "INSTALLED_RUNTIME_FILE",
        "path": "PYTHON_INSTALL_ROOT/LICENSE.txt",
    }
    for name, item in zip(("tcl", "tk"), manifest["runtimes"][1:]):
        pinned = release_sbom._PINNED_RUNTIME_LICENSES[(name, runtime_version)]
        origin = item["file"]["origin"]
        assert origin["kind"] == "PINNED_UPSTREAM_SOURCE"
        assert origin["source_commit_sha"] == pinned["source_commit_sha"]
        assert origin["source_blob_sha"] == pinned["source_blob_sha"]
        assert origin["source_byte_length"] == len(pinned["payload"])
    release_sbom.validate_runtime_license_evidence(
        manifest, members, source_sha=SHA,
    )


def test_runtime_license_capture_prefers_sealed_installed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tcl_tk_runtime(
        tmp_path, monkeypatch, installed_license_names=("tcl", "tk"),
    )

    manifest, members = release_sbom.capture_runtime_license_evidence(SHA)

    assert manifest["schema_version"] == 1
    assert [item["file"]["origin"] for item in manifest["runtimes"]] == [
        "PYTHON_INSTALL_ROOT/LICENSE.txt",
        "PYTHON_TCL_LIBRARY/license.terms",
        "PYTHON_TK_LIBRARY/license.terms",
    ]
    assert members["runtime-licenses/001-tcl.txt"] == b"Installed Tcl license\n"
    assert members["runtime-licenses/002-tk.txt"] == b"Installed Tk license\n"


def test_runtime_license_capture_rejects_unpinned_missing_patch_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tcl_tk_runtime(
        tmp_path, monkeypatch, tcl_version="8.6.15", tk_version="8.6.15",
    )

    with pytest.raises(ValueError, match="tcl runtime license text is unavailable"):
        release_sbom.capture_runtime_license_evidence(SHA)


def test_runtime_license_validator_rejects_pinned_origin_or_payload_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tcl_tk_runtime(tmp_path, monkeypatch)
    manifest, members = release_sbom.capture_runtime_license_evidence(SHA)
    forged = deepcopy(manifest)
    forged["runtimes"][1]["file"]["origin"]["source_blob_sha"] = "0" * 40
    with pytest.raises(ValueError, match="pinned runtime license origin is invalid"):
        release_sbom.validate_runtime_license_evidence(forged, members, source_sha=SHA)
    changed = dict(members)
    changed["runtime-licenses/002-tk.txt"] += b"tamper"
    with pytest.raises(ValueError, match="runtime license payload is missing or invalid"):
        release_sbom.validate_runtime_license_evidence(manifest, changed, source_sha=SHA)


def test_runtime_license_schema_two_requires_a_real_pinned_origin() -> None:
    manifest, members = _runtime_license_evidence()
    manifest["schema_version"] = 2
    manifest["evidence_class"] = (
        "SEALED_BUILD_AND_PINNED_UPSTREAM_RUNTIME_LICENSE_TEXT_NOT_CLEARANCE"
    )
    for item in manifest["runtimes"]:
        item["file"]["origin"] = {
            "kind": "INSTALLED_RUNTIME_FILE",
            "path": item["file"]["origin"],
        }

    with pytest.raises(ValueError, match="has no pinned source"):
        release_sbom.validate_runtime_license_evidence(
            manifest, members, source_sha=SHA,
        )


def test_runtime_license_capture_rejects_ambiguous_tk_patchlevel_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tcl_tk_runtime(tmp_path, monkeypatch)
    tk_script = tmp_path / "python" / "tcl" / "tk8.6" / "tk.tcl"
    tk_script.write_bytes(
        b"package require -exact Tk  8.6.14\n"
        b"package require -exact Tk  8.6.14\n"
    )

    with pytest.raises(ValueError, match="patchlevel binding is unavailable or ambiguous"):
        release_sbom.capture_runtime_license_evidence(SHA)

