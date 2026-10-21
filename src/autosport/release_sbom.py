"""Build-host dependency metadata for a source-bound Windows candidate.

This is *not* a complete transitive SBOM, license clearance, or proof of
components bundled in the resulting executable. It records the exact direct
distributions observed by the packaging Python interpreter.
"""
from __future__ import annotations

from importlib import metadata
import re
import unicodedata
from typing import Any


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9_.+-]*\Z")
_LICENSE = re.compile(r"[A-Za-z0-9 .()+_-]+\Z")


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
            components.append({
                "name": name, "typecode": typecode,
                "packed_bytes": packed, "unpacked_bytes": unpacked,
            })
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
