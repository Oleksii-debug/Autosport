"""Build-host dependency metadata for a source-bound Windows candidate.

This is *not* a complete transitive SBOM, license clearance, or proof of
components bundled in the resulting executable. It records the exact direct
distributions observed by the packaging Python interpreter.
"""
from __future__ import annotations

from importlib import metadata
import platform
import re
import sys
import unicodedata
from typing import Any


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9_.+-]*\Z")
_LICENSE = re.compile(r"[A-Za-z0-9 .()+_-]+\Z")

# proxy-tools 0.1.0 omits its license file from the installed distribution
# RECORD even though the exact PyPI release metadata and upstream source expose
# license information.  Preserve the upstream license bytes and both immutable
# public identities as supplemental evidence.  The conflicting upstream
# setup.py "MIT" metadata versus BSD text is intentionally *not* resolved here:
# this is review evidence, never an automated legal-clearance claim.
_SUPPLEMENTAL_LICENSES = {
    ("proxy-tools", "0.1.0"): {
        "source_url": "https://github.com/jtushman/proxy_tools/blob/master/LICENSE.txt",
        "source_blob_sha": "078411c7399fbc0454f36cbe8c8cdbaaba7ebc95",
        "source_release_artifact_sha256": (
            "ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010"
        ),
        "payload": b"""The BSD License (BSD)\n\nCopyright (c) 2013 Armin Ronacher\nCopyright (c) 2014 Jonathan Tushman\n\nRedistribution and use in source and binary forms, with or without\nmodification, are permitted provided that the following conditions are met:\n\n  * Redistributions of source code must retain the above copyright\n    notice, this list of conditions and the following disclaimer.\n  * Redistributions in binary form must reproduce the above copyright\n    notice, this list of conditions and the following disclaimer in the\n    documentation and/or other materials provided with the distribution.\n\nTHIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS \"AS IS\"\nAND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE\nIMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE\nDISCLAIMED. IN NO EVENT SHALL <COPYRIGHT HOLDER> BE LIABLE FOR ANY\nDIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES\n(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;\nLOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND\nON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT\n(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF\nTHIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.""",
    },
}


def _normalize_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid direct dependency distribution name")
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(source_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(source_manifest, dict):
        raise ValueError("source declarations are required")
    # Direct and transitive dependency evidence must never be qualified against
    # an absent, boolean or malformed source identity. Otherwise two absent
    # identities compare equal in both evidence validators.
    source_sha = source_manifest.get("source_sha")
    if (
        type(source_sha) is not str
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
    ):
        raise ValueError("source declarations require a canonical Git commit SHA")
    result: dict[str, dict[str, Any]] = {}
    for role, key in (
        ("runtime", "declared_runtime_dependencies"),
        ("build", "declared_build_dependencies"),
    ):
        declared = source_manifest.get(key)
        if not isinstance(declared, list) or not declared:
            raise ValueError("missing pinned direct dependencies")
        for requirement in declared:
            if not isinstance(requirement, str) or requirement.count("==") != 1:
                raise ValueError("unpinned direct dependency")
            name, version = requirement.split("==", 1)
            canonical = _normalize_name(name)
            if not _VERSION.fullmatch(version):
                raise ValueError("invalid direct dependency version")
            prior = result.setdefault(canonical, {"version": version, "roles": set()})
            if prior["version"] != version or role in prior["roles"]:
                raise ValueError("conflicting or duplicate direct dependency declarations")
            prior["roles"].add(role)
    return result


def validate_direct_dependency_metadata(
    payload: dict[str, Any], source_manifest: dict[str, Any],
) -> None:
    expected = _requirements(source_manifest)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "source_sha", "evidence_class", "distributions"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["source_sha"] != source_manifest.get("source_sha")
        or payload["evidence_class"] != "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY"
    ):
        raise ValueError("direct dependency metadata schema/identity is invalid")
    entries = payload["distributions"]
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("direct dependency metadata has an incomplete distribution set")
    names: list[str] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "name", "version", "roles", "license_expression", "license_evidence"
        }:
            raise ValueError("direct dependency metadata entry schema is invalid")
        name, version, roles = item["name"], item["version"], item["roles"]
        if not isinstance(name, str) or _normalize_name(name) != name:
            raise ValueError("direct dependency name is not canonical")
        if name not in expected or version != expected[name]["version"]:
            raise ValueError("installed direct dependency does not match the source pin")
        if roles != sorted(expected[name]["roles"]):
            raise ValueError("direct dependency roles are incorrect")
        expression = item["license_expression"]
        evidence = item["license_evidence"]
        if expression is None:
            if evidence != "NOT_PROVIDED":
                raise ValueError("unsupported license evidence claim")
        elif (
            not isinstance(expression, str)
            or not 0 < len(expression) <= 200
            or not _LICENSE.fullmatch(expression)
            or evidence != "DISTRIBUTION_METADATA_UNVERIFIED"
        ):
            raise ValueError("invalid or falsely verified license metadata")
        names.append(name)
    if names != sorted(set(names)):
        raise ValueError("direct dependency metadata order/identity is ambiguous")


def capture_direct_installed_dependencies(
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Snapshot exact pinned direct distributions visible on the build host."""
    expected = _requirements(source_manifest)
    entries: list[dict[str, Any]] = []
    for name, info in sorted(expected.items()):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError("required build-host dependency is not installed") from exc
        installed_name = distribution.metadata.get("Name")
        installed_version = distribution.version
        if (
            not isinstance(installed_name, str)
            or _normalize_name(installed_name) != name
            or installed_version != info["version"]
        ):
            raise ValueError("build-host distribution differs from exact source declaration")
        expression = distribution.metadata.get("License-Expression")
        if not (
            isinstance(expression, str)
            and 0 < len(expression) <= 200
            and _LICENSE.fullmatch(expression)
        ):
            expression = None
        entries.append({
            "name": name,
            "version": installed_version,
            "roles": sorted(info["roles"]),
            "license_expression": expression,
            "license_evidence": (
                "DISTRIBUTION_METADATA_UNVERIFIED" if expression is not None
                else "NOT_PROVIDED"
            ),
        })
    result = {
        "schema_version": 1,
        "source_sha": source_manifest.get("source_sha"),
        "evidence_class": "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY",
        "distributions": entries,
    }
    validate_direct_dependency_metadata(result, source_manifest)
    return result


# An executable CArchive TOC is stronger evidence than the *build host* wheel
# registry: it is read from the actual sealed binary. It is still NOT a
# complete SBOM, evidence of package-to-wheel attribution, or license clearance.
def validate_embedded_executable_inventory(
    payload: dict[str, Any], *, source_sha: str, executable_sha256: str,
) -> None:
    import re as _re
    if (
        type(source_sha) is not str
        or _re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or type(executable_sha256) is not str
        or _re.fullmatch(r"[0-9a-f]{64}", executable_sha256) is None
    ):
        raise ValueError("invalid embedded inventory source/executable identity")
    # V1 captures only the outer CArchive. V2 also inventories the nested
    # PYZ table, without claiming distribution attribution or license clearance.
    schema_version = payload.get("schema_version") if type(payload) is dict else None
    base_fields = {
        "schema_version", "source_sha", "executable_sha256",
        "evidence_class", "components",
    }
    if (
        type(schema_version) is not int
        or schema_version not in (1, 2)
        or set(payload) != (base_fields | ({"pyz_modules"} if schema_version == 2 else set()))
        or payload["source_sha"] != source_sha
        or payload["executable_sha256"] != executable_sha256
        or payload["evidence_class"] != (
            "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY" if schema_version == 2
            else "EMBEDDED_PYINSTALLER_CARCHIVE_TOC_ONLY"
        )
    ):
        raise ValueError("embedded executable inventory identity/schema mismatch")
    components = payload["components"]
    if type(components) is not list or not (1 <= len(components) <= 20000):
        raise ValueError("embedded executable inventory is empty or unbounded")
    # The ZIP member classifier protects files outside the PE, but a secret
    # hidden *inside* the PyInstaller CArchive bypasses ZIP path screening.
    # A sealed binary containing such members is not a distributable package,
    # even when its hashes and census JSON have been recomputed.
    forbidden_directories = frozenset({
        ".aws", ".azure", ".mozilla", ".ssh", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".tox", "__pycache__",
        "user data", "htmlcov", ".hypothesis", ".nox",
    })
    forbidden_files = frozenset({
        ".env", ".envrc", ".pypirc", "application_default_credentials.json",
        "cookies", "cookies.json", "cookies.sqlite", "credentials",
        "credentials.json", "key3.db", "key4.db", "local state",
        "login data", "logins.json", "oauth_credentials.json",
        "secrets.json", "secrets.toml", "token.json", "web data",
        ".coverage", "coverage.xml", ".testmondata", "pytestdebug.log",
        "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
    })
    safe_env_templates = frozenset({
        ".env.example", ".env.sample", ".env.template", ".env.dist",
    })
    # PyInstaller can extract CArchive names to a case-insensitive Windows
    # filesystem. Distinct raw TOC names must not alias there, even when the
    # outer release ZIP and its SHA-256 manifests are individually canonical.
    windows_names: set[str] = set()
    windows_devices = frozenset({
        "con", "prn", "aux", "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        "com¹", "com²", "com³", "lpt¹", "lpt²", "lpt³",
    })
    # CArchive entries are decompressed by the shipped runtime. The outer ZIP
    # budget does not bound the sum of embedded, decompressed members. Enforce
    # one aggregate resource fence for both main and Data executables.
    max_total_packed = 1 << 30
    max_total_unpacked = 1 << 32
    total_packed = 0
    total_unpacked = 0
    last = None
    for item in components:
        if type(item) is not dict or set(item) != {
            "name", "typecode", "packed_bytes", "unpacked_bytes",
        }:
            raise ValueError("embedded executable component schema is invalid")
        name, code = item["name"], item["typecode"]
        if (
            type(name) is not str or not (1 <= len(name) <= 512)
            or "\x00" in name or name.startswith(("/", "\\"))
            or any(segment in ("", ".", "..") for segment in name.replace("\\", "/").split("/"))
            or _re.match(r"^[A-Za-z]:", name)
        ):
            raise ValueError("embedded executable component name is unsafe")
        if last is not None and name <= last:
            raise ValueError("embedded executable component names are not unique/sorted")
        last = name
        windows_parts = name.replace("\\", "/").split("/")
        for segment in windows_parts:
            if (
                segment.endswith((" ", "."))
                or any(ord(char) < 32 or char in '<>:"|?*' for char in segment)
                or segment.split(".", 1)[0].casefold() in windows_devices
            ):
                raise ValueError("embedded executable Windows member name is unsafe")
        windows_name = unicodedata.normalize("NFC", "/".join(windows_parts)).casefold()
        if windows_name in windows_names:
            raise ValueError("embedded executable Windows member names collide")
        windows_names.add(windows_name)
        pieces = tuple(part.casefold() for part in windows_parts)
        leaf = pieces[-1]
        if (
            any(part in forbidden_directories for part in pieces[:-1])
            or leaf in forbidden_files
            or (leaf.startswith(".env.") and leaf not in safe_env_templates)
            or (leaf.startswith("client_secret") and leaf.endswith(".json"))
            or leaf.startswith(".coverage.")
            or leaf.endswith((".pyc", ".pyo", ".pfx", ".p12", ".ppk", ".key"))
            or leaf.endswith((".session", ".session-journal"))
            or (
                leaf.endswith(".pem")
                and ("private" in leaf or leaf[:-4] in {
                    "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
                })
            )
        ):
            raise ValueError("embedded executable contains forbidden private/test state")
        if type(code) is not str or not _re.fullmatch(r"[A-Za-z0-9]", code):
            raise ValueError("embedded executable component typecode is invalid")
        for key in ("packed_bytes", "unpacked_bytes"):
            size = item[key]
            if type(size) is not int or size < 0 or size > (1 << 35):
                raise ValueError("embedded executable component size is invalid")
        total_packed += item["packed_bytes"]
        total_unpacked += item["unpacked_bytes"]
        if total_packed > max_total_packed or total_unpacked > max_total_unpacked:
            raise ValueError("embedded executable archive size budget exceeded")
    if schema_version == 2:
        # One PYZ is the canonical embedded Python module archive for these
        # one-file executables; no outer-only census may masquerade as V2.
        pyz_parents = [entry for entry in components if entry["typecode"] == "z"]
        if len(pyz_parents) != 1 or pyz_parents[0]["unpacked_bytes"] <= 0:
            raise ValueError("nested PYZ inventory requires exactly one bounded archive")
        archive_size = pyz_parents[0]["unpacked_bytes"]
        modules = payload["pyz_modules"]
        if type(modules) is not list or not (1 <= len(modules) <= 20000):
            raise ValueError("nested PYZ module inventory is empty or unbounded")
        last_module = None
        windows_modules: set[str] = set()
        # Each PYZ module is a separate compressed byte range. Independently
        # in-bounds entries must not alias the same sealed executable bytes.
        spans: list[tuple[int, int]] = []
        for module in modules:
            if type(module) is not dict or set(module) != {
                "name", "typecode", "position", "packed_bytes",
            }:
                raise ValueError("nested PYZ module schema is invalid")
            name = module["name"]
            if (
                type(name) is not str or not (1 <= len(name) <= 512)
                or any(not part.isidentifier() for part in name.split("."))
                or name != unicodedata.normalize("NFC", name)
                or (last_module is not None and name <= last_module)
            ):
                raise ValueError("nested PYZ module names must be safe and sorted")
            last_module = name
            # The outer CArchive private/test-state exclusion does not
            # cover Python modules nested inside its PYZ. A releasable
            # PAPER executable must not silently contain test frameworks
            # or test packages merely because they were frozen as modules.
            # Check exact dotted name segments, not substrings: e.g.
            # "contest" and "hypothesis_engine" remain ordinary modules.
            if any(
                segment.casefold() in {"test", "tests", "pytest", "hypothesis", "nox"}
                for segment in name.split(".")
            ):
                raise ValueError("nested PYZ contains non-release test modules")
            windows_name = name.casefold()
            if windows_name in windows_modules:
                raise ValueError("nested PYZ module names collide on Windows")
            windows_modules.add(windows_name)
            typecode = module["typecode"]
            position = module["position"]
            length = module["packed_bytes"]
            if (
                type(typecode) is not int or typecode not in (0, 1, 2, 3)
                or type(position) is not int or position < 0
                or type(length) is not int or length < 0
                or position > archive_size or length > archive_size - position
            ):
                raise ValueError("nested PYZ module offset/type is invalid")
            if length:
                spans.append((position, position + length))
        spans.sort()
        for previous, following in zip(spans, spans[1:]):
            if previous[1] > following[0]:
                raise ValueError("nested PYZ module ranges overlap")


def capture_embedded_executable_inventory(
    exe_path: "str | Path", source_sha: str, *, reader_factory: Any = None,
    include_nested_pyz: bool = False,
) -> dict[str, Any]:
    """Census names/typecodes/sizes inside the *shipped EXE*, not host wheels.

    The caller must first seal the PE file by SHA-256. The reader is used only
    for a nonextracting TOC read, never to execute/archive-expand its members.
    """
    import hashlib
    from pathlib import Path
    path = Path(exe_path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("embedded archive source is not a regular executable")
    # Bind the archive table to the exact bytes independently hashed before
    # the reader opens its own path. A swapped/replaced executable must not
    # produce an inventory for one CArchive and a digest for another binary.
    digest_before = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest_before.update(chunk)
    # The CArchive reader opens a filename independently of our hash reads.
    # A temporarily substituted source path could otherwise yield a forged TOC
    # between two identical source digest reads. Copy to an inaccessible
    # package-local temporary snapshot and validate its exact source bytes.
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory(prefix="autosport-carchive-") as temp_dir:
        snapshot_path = Path(temp_dir) / "Autosport-sealed.exe"
        shutil.copyfile(path, snapshot_path)
        snapshot_before = hashlib.sha256()
        with snapshot_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                snapshot_before.update(chunk)
        if snapshot_before.digest() != digest_before.digest():
            raise ValueError("executable changed during CArchive inventory capture")
        if reader_factory is None:
            try:
                from PyInstaller.archive.readers import CArchiveReader
            except ImportError as exc:
                raise ValueError("PyInstaller archive reader is unavailable") from exc
            reader_factory = CArchiveReader
        try:
            reader = reader_factory(str(snapshot_path))
            toc = reader.toc
        except Exception as exc:
            raise ValueError("unable to inspect executable PyInstaller CArchive") from exc
        if type(toc) is not dict or not 1 <= len(toc) <= 20000:
            raise ValueError("executable has no bounded CArchive member table")
        # CArchive member offsets are archive-relative; even without trusting the
        # archive start, a compressed member cannot extend beyond the EXE bytes.
        executable_size = snapshot_path.stat().st_size
        components: list[dict[str, Any]] = []
        packed_ranges: list[tuple[int, int]] = []
        for name, record in sorted(toc.items()):
            if type(record) not in (tuple, list) or len(record) != 5:
                raise ValueError("malformed executable CArchive member record")
            position, packed, unpacked, compressed, typecode = record
            if (
                type(position) is not int or position < 0
                or type(packed) is not int or packed < 0
                or type(unpacked) is not int or unpacked < 0
                or position > executable_size
                or packed > executable_size - position
                or type(compressed) not in (bool, int) or compressed not in (False, True, 0, 1)
            ):
                raise ValueError("malformed executable CArchive member flags")
            # Distinct TOC names must not alias the same sealed CArchive bytes.
            # Adjacent spans and zero-length metadata members are permitted.
            if packed:
                packed_ranges.append((position, position + packed))
            components.append({
                "name": name, "typecode": typecode,
                "packed_bytes": packed, "unpacked_bytes": unpacked,
            })
        packed_ranges.sort()
        for previous, following in zip(packed_ranges, packed_ranges[1:]):
            if previous[1] > following[0]:
                raise ValueError("embedded CArchive member byte ranges overlap")
        pyz_modules = None
        if include_nested_pyz:
            # Inspect the in-place PyInstaller PYZ TOC through the sealed
            # snapshot; never unmarshal/execute individual Python code objects.
            pyz_names = [
                item["name"] for item in components if item["typecode"] == "z"
            ]
            if len(pyz_names) != 1:
                raise ValueError("exactly one embedded PYZ archive is required")
            try:
                pyz = reader.open_embedded_archive(pyz_names[0])
                pyz_toc = pyz.toc
            except Exception as exc:
                raise ValueError("unable to inspect sealed embedded PYZ archive") from exc
            if type(pyz_toc) is not dict or not 1 <= len(pyz_toc) <= 20000:
                raise ValueError("embedded PYZ module table is missing or unbounded")
            pyz_modules = []
            for module_name, record in sorted(pyz_toc.items()):
                if type(record) not in (tuple, list) or len(record) != 3:
                    raise ValueError("malformed embedded PYZ module record")
                kind, position, packed = record
                pyz_modules.append({
                    "name": module_name,
                    "typecode": kind,
                    "position": position,
                    "packed_bytes": packed,
                })
        # Verify TOC and bounded offsets belong to the same sealed snapshot.
        snapshot_final = hashlib.sha256()
        with snapshot_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                snapshot_final.update(chunk)
        if snapshot_final.digest() != digest_before.digest():
            raise ValueError("executable changed during CArchive inventory capture")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.digest() != digest_before.digest():
            raise ValueError("executable changed during CArchive inventory capture")
        result = {
            "schema_version": 2 if include_nested_pyz else 1,
            "source_sha": source_sha,
            "executable_sha256": digest.hexdigest(),
            "evidence_class": (
                "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY"
                if include_nested_pyz else "EMBEDDED_PYINSTALLER_CARCHIVE_TOC_ONLY"
            ),
            "components": components,
        }
        if pyz_modules is not None:
            result["pyz_modules"] = pyz_modules
        validate_embedded_executable_inventory(
            result, source_sha=source_sha, executable_sha256=digest.hexdigest(),
        )
        return result


def _owned_distribution_license_record(record_path: str, name: str, version: str) -> bool:
    if (
        type(record_path) is not str or not (1 <= len(record_path) <= 512)
        or record_path.startswith(("/", "\\")) or "\\" in record_path
        or any(ord(character) < 32 or ord(character) == 127 for character in record_path)
    ):
        return False
    parts = record_path.split("/")
    if len(parts) < 2 or any(not part or part in (".", "..") or ":" in part for part in parts):
        return False
    folder = parts[0]
    if folder.endswith(".dist-info") and "-" in folder[:-10]:
        distribution_name, distribution_version = folder[:-10].rsplit("-", 1)
        try:
            if _normalize_name(distribution_name) != name or distribution_version != version:
                return False
        except ValueError:
            return False
    elif (
        # The setuptools wheel RECORD can own vendored license files.
        # This proves wheel-byte origin, not separately installed vendors or legal clearance.
        name == "setuptools" and parts[:2] == ["setuptools", "_vendor"]
        and len(parts) in (4, 5)
        and (len(parts) == 4 or parts[3] == "licenses")
        and parts[2].endswith(".dist-info") and "-" in parts[2][:-10]
    ):
        vendor_name, vendor_version = parts[2][:-10].rsplit("-", 1)
        try:
            _normalize_name(vendor_name)
        except ValueError:
            return False
        if not _VERSION.fullmatch(vendor_version):
            return False
    else:
        return False
    leaf = parts[-1]
    stem = leaf.split(".", 1)[0].upper()
    return (
        leaf.upper() in {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
        or (
            leaf in {"LICENSE.APACHE", "LICENSE.BSD"}
            and (
                (name == "packaging" and len(parts) == 3 and parts[1] == "licenses")
                or (
                    name == "setuptools"
                    and parts[:2] == ["setuptools", "_vendor"]
                    and len(parts) == 5 and parts[3] == "licenses"
                )
            )
        )
        or (
            stem in {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
            and leaf.lower().endswith((".txt", ".md", ".rst", ".license"))
        )
    )

# This is a bounded copy of build-host distribution LICENSE/NOTICE text only.
# Its evidence class deliberately does not claim that the distribution is
# embedded in either executable or that the copyright/license is cleared.
def validate_dependency_license_evidence(
    manifest: dict[str, Any],
    dependency_graph: dict[str, Any],
    license_members: dict[str, bytes],
) -> None:
    import hashlib

    if (
        type(manifest) is not dict
        or set(manifest) != {"schema_version", "source_sha", "evidence_class", "distributions"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] not in (1, 2)
        or type(dependency_graph) is not dict
        or type(manifest["source_sha"]) is not str
        or re.fullmatch(r"[0-9a-f]{40}", manifest["source_sha"]) is None
        or manifest["source_sha"] != dependency_graph.get("source_sha")
        or manifest["evidence_class"] != (
            "BUILD_HOST_AND_PINNED_UPSTREAM_LICENSE_TEXT_NOT_CLEARANCE"
            if manifest["schema_version"] == 2
            else "BUILD_HOST_LICENSE_TEXT_ONLY_NOT_CLEARANCE"
        )
        or type(dependency_graph.get("components")) is not list
        or type(license_members) is not dict
    ):
        raise ValueError("distribution license evidence schema/source mismatch")
    components = dependency_graph["components"]
    if not (1 <= len(components) <= 256):
        raise ValueError("license evidence has an invalid source graph")
    # A duplicate or malformed build-host component cannot disappear under a
    # dictionary comprehension and thereby borrow another distribution's
    # LICENSE/NOTICE evidence. Validate the independent graph boundary even
    # when this verifier is invoked outside the full ZIP verifier.
    expected: dict[str, str] = {}
    for component in components:
        if (
            type(component) is not dict
            or type(component.get("name")) is not str
            or not _NAME.fullmatch(component["name"])
            or _normalize_name(component["name"]) != component["name"]
            or type(component.get("version")) is not str
            or not _VERSION.fullmatch(component["version"])
            or component["name"] in expected
        ):
            raise ValueError("license evidence has an invalid source graph")
        expected[component["name"]] = component["version"]
    entries = manifest["distributions"]
    if type(entries) is not list or len(entries) != len(expected):
        raise ValueError("distribution license evidence is incomplete")
    actual_members: set[str] = set()
    total_bytes = 0
    last = None
    for item in entries:
        if type(item) is not dict or set(item) != {"name", "version", "files", "license_review"}:
            raise ValueError("distribution license entry schema is invalid")
        name = item["name"]
        if (
            type(name) is not str or name not in expected
            or (last is not None and name <= last)
            or item["version"] != expected[name]
        ):
            raise ValueError("distribution license identity/order mismatch")
        last = name
        files = item["files"]
        if type(files) is not list or len(files) > 16:
            raise ValueError("distribution license file count is unbounded")
        if item["license_review"] != (
            "TEXT_PRESENT_REVIEW_REQUIRED" if files else "TEXT_ABSENT_REVIEW_REQUIRED"
        ):
            raise ValueError("distribution license evidence cannot claim clearance")
        for index, record in enumerate(files):
            expected_record_fields = (
                {"path", "sha256", "origin"}
                if manifest["schema_version"] == 2
                else {"path", "sha256", "record_path"}
            )
            if type(record) is not dict or set(record) != expected_record_fields:
                raise ValueError("distribution license file entry is invalid")
            path = f"licenses/{name}/{index:03d}.txt"
            if record["path"] != path or path in actual_members:
                raise ValueError("distribution license file path is ambiguous")
            if manifest["schema_version"] == 1:
                raw_path = record["record_path"]
                if (
                    type(raw_path) is not str or not (1 <= len(raw_path) <= 512)
                    or "\\" in raw_path or raw_path.startswith("/")
                    or any(part in ("", ".", "..") for part in raw_path.split("/"))
                    or not _owned_distribution_license_record(raw_path, name, item["version"])
                ):
                    raise ValueError("distribution license origin path is unsafe")
            else:
                origin = record["origin"]
                if type(origin) is not dict or type(origin.get("kind")) is not str:
                    raise ValueError("distribution license origin is invalid")
                if origin["kind"] == "INSTALLED_WHEEL_RECORD":
                    if set(origin) != {"kind", "record_path"}:
                        raise ValueError("installed license origin schema is invalid")
                    raw_path = origin["record_path"]
                    if (
                        type(raw_path) is not str or not (1 <= len(raw_path) <= 512)
                        or "\\" in raw_path or raw_path.startswith("/")
                        or any(part in ("", ".", "..") for part in raw_path.split("/"))
                        or not _owned_distribution_license_record(
                            raw_path, name, item["version"]
                        )
                    ):
                        raise ValueError("distribution license origin path is unsafe")
                elif origin["kind"] == "PINNED_UPSTREAM_SOURCE":
                    if set(origin) != {
                        "kind", "source_url", "source_blob_sha",
                        "source_release_artifact_sha256",
                    }:
                        raise ValueError("supplemental license origin schema is invalid")
                    supplemental = _SUPPLEMENTAL_LICENSES.get((name, item["version"]))
                    if supplemental is None or any(
                        origin[key] != supplemental[key]
                        for key in (
                            "source_url", "source_blob_sha",
                            "source_release_artifact_sha256",
                        )
                    ):
                        raise ValueError("supplemental license origin is not pinned")
                else:
                    raise ValueError("distribution license origin kind is unsupported")
            digest = record["sha256"]
            if type(digest) is not str or re.fullmatch("[0-9a-f]{64}", digest) is None:
                raise ValueError("distribution license digest is invalid")
            payload = license_members.get(path)
            if type(payload) is not bytes or not (1 <= len(payload) <= 262144):
                raise ValueError("distribution license text is missing or unbounded")
            if b"\x00" in payload:
                raise ValueError("distribution license text is not a text file")
            total_bytes += len(payload)
            if total_bytes > 8 * 1024 * 1024:
                raise ValueError("distribution license evidence total size is unbounded")
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("distribution license file hash mismatch")
            if (
                manifest["schema_version"] == 2
                and record["origin"]["kind"] == "PINNED_UPSTREAM_SOURCE"
                and payload != _SUPPLEMENTAL_LICENSES[(name, item["version"])]["payload"]
            ):
                raise ValueError("supplemental license payload differs from pinned source")
            actual_members.add(path)
    if set(license_members) != actual_members:
        raise ValueError("distribution license evidence includes unclaimed members")


def _read_sealed_license_text(path: "Path") -> bytes:
    """Reject swapped/unbounded license sources before sealing release evidence.

    A path.lstat() followed by Path.read_bytes() permits a replacement between
    the metadata check and the actual read. On both Windows and POSIX, bind the
    opened descriptor to the original and final file identities. O_NOFOLLOW
    provides an additional symlink fence where the platform supports it.
    """
    import os
    import stat

    def identity(info: os.stat_result) -> tuple[int, int, int, int]:
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 262144:
        raise ValueError("distribution license source is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(before):
            raise ValueError("distribution license source changed before read")
        chunks: list[bytes] = []
        remaining = 262145  # max legal size plus one sentinel byte
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if (
            identity(os.fstat(descriptor)) != identity(opened)
            or identity(path.lstat()) != identity(before)
        ):
            raise ValueError("distribution license source changed during read")
    finally:
        os.close(descriptor)
    if not 1 <= len(payload) <= 262144 or b"\x00" in payload:
        raise ValueError("distribution license source is not bounded plain text")
    return payload


def validate_runtime_license_evidence(
    manifest: dict[str, Any], runtime_members: dict[str, bytes], *, source_sha: str,
) -> None:
    """Validate CPython plus Tcl/Tk license bytes used by the frozen runtime."""
    import hashlib

    if (
        type(manifest) is not dict
        or set(manifest) != {
            "schema_version", "source_sha", "evidence_class", "runtimes",
        }
        or manifest["schema_version"] != 1
        or manifest["source_sha"] != source_sha
        or type(source_sha) is not str
        or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or manifest["evidence_class"] != "SEALED_BUILD_RUNTIME_LICENSE_TEXT_NOT_CLEARANCE"
        or type(runtime_members) is not dict
    ):
        raise ValueError("runtime license evidence schema/source mismatch")
    runtimes = manifest["runtimes"]
    expected_names = ["cpython", "tcl", "tk"]
    if type(runtimes) is not list or [item.get("name") if type(item) is dict else None
                                      for item in runtimes] != expected_names:
        raise ValueError("runtime license evidence is incomplete")
    claimed = set()
    for index, item in enumerate(runtimes):
        if type(item) is not dict or set(item) != {
            "name", "version", "license_review", "file",
        }:
            raise ValueError("runtime license entry schema is invalid")
        if (
            type(item["version"]) is not str
            or not 1 <= len(item["version"]) <= 64
            or item["license_review"] != "TEXT_PRESENT_REVIEW_REQUIRED"
        ):
            raise ValueError("runtime license identity/review is invalid")
        record = item["file"]
        if type(record) is not dict or set(record) != {"path", "sha256", "origin"}:
            raise ValueError("runtime license file schema is invalid")
        expected_path = f"runtime-licenses/{index:03d}-{item['name']}.txt"
        if record["path"] != expected_path or expected_path in claimed:
            raise ValueError("runtime license file path is ambiguous")
        expected_origin = {
            "cpython": "PYTHON_INSTALL_ROOT/LICENSE.txt",
            "tcl": "PYTHON_TCL_LIBRARY/license.terms",
            "tk": "PYTHON_TK_LIBRARY/license.terms",
        }[item["name"]]
        if record["origin"] != expected_origin:
            raise ValueError("runtime license origin is invalid")
        payload = runtime_members.get(expected_path)
        if (
            type(payload) is not bytes or not 1 <= len(payload) <= 262144
            or b"\x00" in payload
            or type(record["sha256"]) is not str
            or hashlib.sha256(payload).hexdigest() != record["sha256"]
        ):
            raise ValueError("runtime license payload is missing or invalid")
        claimed.add(expected_path)
    if set(runtime_members) != claimed:
        raise ValueError("runtime license evidence includes unclaimed members")


def capture_runtime_license_evidence(
    source_sha: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Seal CPython and Tcl/Tk license texts from the exact build runtime."""
    import hashlib
    from pathlib import Path
    import tkinter

    if type(source_sha) is not str or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None:
        raise ValueError("runtime license capture requires canonical source SHA")
    base = Path(sys.base_prefix)
    python_candidates = [base / "LICENSE.txt", base / "LICENSE"]
    python_license = next((path for path in python_candidates if path.is_file()), None)
    if python_license is None:
        raise ValueError("CPython runtime license text is unavailable")
    interpreter = tkinter.Tcl()
    tcl_library = Path(interpreter.eval("info library"))
    tcl_version = str(interpreter.eval("info patchlevel"))
    tk_series = str(tkinter.TkVersion)
    tk_library = tcl_library.parent / f"tk{tk_series}"
    sources = [
        ("cpython", platform.python_version(), python_license,
         "PYTHON_INSTALL_ROOT/LICENSE.txt"),
        ("tcl", tcl_version, tcl_library / "license.terms",
         "PYTHON_TCL_LIBRARY/license.terms"),
        ("tk", tk_series, tk_library / "license.terms",
         "PYTHON_TK_LIBRARY/license.terms"),
    ]
    entries = []
    members = {}
    for index, (name, version, path, origin) in enumerate(sources):
        try:
            payload = _read_sealed_license_text(path)
        except OSError as exc:
            raise ValueError(f"{name} runtime license text is unavailable") from exc
        member = f"runtime-licenses/{index:03d}-{name}.txt"
        members[member] = payload
        entries.append({
            "name": name, "version": version,
            "license_review": "TEXT_PRESENT_REVIEW_REQUIRED",
            "file": {
                "path": member, "sha256": hashlib.sha256(payload).hexdigest(),
                "origin": origin,
            },
        })
    result = {
        "schema_version": 1,
        "source_sha": source_sha,
        "evidence_class": "SEALED_BUILD_RUNTIME_LICENSE_TEXT_NOT_CLEARANCE",
        "runtimes": entries,
    }
    validate_runtime_license_evidence(result, members, source_sha=source_sha)
    return result, members


def capture_dependency_license_evidence(
    dependency_graph: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Capture safe distribution .dist-info license text from the build interpreter.

    Missing/unknown files are represented as REVIEW_REQUIRED, not legal approval.
    Source/binary attribution remains a separate Plan 6 §5Б acceptance gate.
    """
    import hashlib
    import base64
    from pathlib import Path

    components = dependency_graph.get("components")
    if (
        type(components) is not list or not 1 <= len(components) <= 256
        or type(dependency_graph.get("source_sha")) is not str
        or re.fullmatch("[0-9a-f]{40}", dependency_graph["source_sha"]) is None
    ):
        raise ValueError("license capture requires a bounded source-bound graph")
    use_supplemental_schema = any(
        (component.get("name"), component.get("version")) in _SUPPLEMENTAL_LICENSES
        for component in components if type(component) is dict
    )
    members: dict[str, bytes] = {}
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for component in components:
        name, version = component["name"], component["version"]
        if type(name) is not str or _normalize_name(name) != name:
            raise ValueError("license capture distribution name is invalid")
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError("license capture distribution is not installed") from exc
        actual_name = dist.metadata.get("Name")
        if (
            type(actual_name) is not str or _normalize_name(actual_name) != name
            or dist.version != version
        ):
            raise ValueError("license capture distribution differs from source graph")
        candidates = []
        for entry in dist.files or ():
            source_name = str(entry).replace("\\", "/")
            parts = source_name.split("/")
            leaf = parts[-1]
            stem = leaf.split(".", 1)[0].upper()
            allowed_name = (
                leaf.upper() in {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
                or (
                    leaf in {"LICENSE.APACHE", "LICENSE.BSD"}
                    and (
                        (name == "packaging" and len(parts) == 3 and parts[1] == "licenses")
                        or (
                            name == "setuptools"
                            and parts[:2] == ["setuptools", "_vendor"]
                            and len(parts) == 5 and parts[3] == "licenses"
                        )
                    )
                )
                or (
                    stem in {"LICENSE", "LICENCE", "COPYING", "NOTICE"}
                    and leaf.lower().endswith((".txt", ".md", ".rst", ".license"))
                )
            )
            if (
                1 <= len(source_name) <= 512
                and all(part not in ("", ".", "..") for part in parts)
                and all(":" not in part and "\\x00" not in part for part in parts)
                and any(part.endswith(".dist-info") for part in parts[:-1])
                and allowed_name
            ):
                if not _owned_distribution_license_record(source_name, name, version):
                    raise ValueError("distribution license RECORD origin mismatches owner/version")
                candidates.append((source_name, entry))
        if len(candidates) > 16:
            raise ValueError("distribution has unbounded license-file candidates")
        files = []
        for source_name, record in sorted(candidates):
            path = Path(dist.locate_file(record))
            try:
                payload = _read_sealed_license_text(path)
            except OSError as exc:
                raise ValueError("distribution license source became unavailable") from exc
            # License bytes must match the installed wheel RECORD, not merely
            # the file currently readable on the build host.
            record_size = getattr(record, "size", None)
            record_hash = getattr(record, "hash", None)
            if (
                type(record_size) is not int
                or record_size != len(payload)
                or record_hash is None
                or getattr(record_hash, "mode", None) != "sha256"
                or type(getattr(record_hash, "value", None)) is not str
                or record_hash.value != base64.urlsafe_b64encode(
                    hashlib.sha256(payload).digest()
                ).rstrip(b"=").decode("ascii")
            ):
                raise ValueError("distribution license RECORD digest/size mismatch")
            total_bytes += len(payload)
            if total_bytes > 8 * 1024 * 1024:
                raise ValueError("distribution licenses exceed overall size budget")
            member = f"licenses/{name}/{len(files):03d}.txt"
            members[member] = payload
            record = {"path": member, "sha256": hashlib.sha256(payload).hexdigest()}
            if use_supplemental_schema:
                record["origin"] = {
                    "kind": "INSTALLED_WHEEL_RECORD", "record_path": source_name,
                }
            else:
                record["record_path"] = source_name
            files.append(record)
        if not files and (name, version) in _SUPPLEMENTAL_LICENSES:
            supplemental = _SUPPLEMENTAL_LICENSES[(name, version)]
            payload = supplemental["payload"]
            total_bytes += len(payload)
            if total_bytes > 8 * 1024 * 1024:
                raise ValueError("distribution licenses exceed overall size budget")
            member = f"licenses/{name}/000.txt"
            members[member] = payload
            files.append({
                "path": member,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "origin": {
                    "kind": "PINNED_UPSTREAM_SOURCE",
                    "source_url": supplemental["source_url"],
                    "source_blob_sha": supplemental["source_blob_sha"],
                    "source_release_artifact_sha256": supplemental[
                        "source_release_artifact_sha256"
                    ],
                },
            })
        entries.append({
            "name": name, "version": version, "files": files,
            "license_review": (
                "TEXT_PRESENT_REVIEW_REQUIRED" if files else "TEXT_ABSENT_REVIEW_REQUIRED"
            ),
        })
    result = {
        "schema_version": 2 if use_supplemental_schema else 1,
        "source_sha": dependency_graph["source_sha"],
        "evidence_class": (
            "BUILD_HOST_AND_PINNED_UPSTREAM_LICENSE_TEXT_NOT_CLEARANCE"
            if use_supplemental_schema
            else "BUILD_HOST_LICENSE_TEXT_ONLY_NOT_CLEARANCE"
        ),
        "distributions": entries,
    }
    validate_dependency_license_evidence(result, dependency_graph, members)
    return result, members
