"""End-to-end CLI boundary and fail-closed request parsing tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from autosport import windows_update_cli as cli
from autosport.windows_update_admission import WindowsUpdateDecision
from autosport.windows_update_signature import VerifiedWindowsUpdate


def request(tmp_path):
    return {
        "package_path": "папка з пробілами/candidate.zip",
        "expected_package_sha256": "a" * 64,
        "installed_manifest_path": "installed state.json",
        "expected_installed_manifest_sha256": "b" * 64,
        "signature_hex": "c" * 128,
        "candidate_version": "1.2.3",
        "candidate_schema": 2,
        "expected_source_sha": "d" * 40,
        "supported_schema_migrations": [[1, 2]],
    }


def write(tmp_path, value):
    p = tmp_path / "запит на оновлення.json"
    p.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return p


def verified():
    return VerifiedWindowsUpdate(
        decision=WindowsUpdateDecision("ELIGIBLE_FOR_TRANSACTION_PREPARE", "1.0.0", "1.2.3", 1, 2),
        package_sha256="a" * 64, source_sha="d" * 40, version="1.2.3", schema=2,
    )


def test_operator_preflight_is_non_authorizing_and_uses_cyrillic_paths(tmp_path, monkeypatch):
    received = []
    def verifier(**kwargs):
        received.append(kwargs)
        return verified()
    monkeypatch.setattr(cli, "preflight_signed_windows_update", verifier)
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 0
    assert report["status"] == "DIAGNOSTIC_PASS_UNTRUSTED_ROOT"
    assert report["install_authorized"] is False
    assert report["independent_trust_root_verified"] is False
    assert report["transaction_preview"]["backup_required"] is True
    assert report["transaction_preview"]["migration_steps"] == [{"from_schema": 1, "to_schema": 2}]
    assert report["transaction_preview"]["install_authorized"] is False
    assert report["human_tested"] is False and report["nvda_verified"] is False
    assert received[0]["package_path"] == tmp_path / "папка з пробілами/candidate.zip"
    assert received[0]["signature"] == bytes.fromhex("c" * 128)
    assert received[0]["supported_schema_migrations"] == ((1, 2),)


def test_real_preflight_rejects_missing_files_without_leaking_paths(tmp_path):
    path = write(tmp_path, request(tmp_path))
    code, report = cli.run(path)
    assert code == 2
    assert "запит" not in json.dumps(report, ensure_ascii=False)
    assert report["install_authorized"] is False


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(candidate_schema=True),
    lambda p: p.update(candidate_version="01.2.3"),
    lambda p: p.update(candidate_version="2147483648.0.0"),
    lambda p: p.update(candidate_version="0.2147483648.0"),
    lambda p: p.update(signature_hex="Z" * 128),
    lambda p: p.update(expected_package_sha256="A" * 64),
    lambda p: p.update(supported_schema_migrations=[[1, True]]),
    lambda p: p.update(supported_schema_migrations=["1->2"]),
    lambda p: p.update(supported_schema_migrations=list(range(257))),
    lambda p: p.update(unrecognized="extra"),
    lambda p: p.pop("candidate_version"),
    lambda p: p.update(package_path="a\x00b"),
])
def test_malformed_request_never_reaches_verifier(tmp_path, monkeypatch, mutate):
    p = request(tmp_path)
    mutate(p)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **kwargs: pytest.fail("untrusted request reached verifier"))
    code, report = cli.run(write(tmp_path, p))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("data", [
    b'{"candidate_version":"1.2.3","candidate_version":"2.0.0"}',
    b'{"x":NaN}',
    b'\xff\xfe',
    b' ' * (16 * 1024 + 1),
    b'[]',
])
def test_invalid_json_rejected(tmp_path, data):
    path = tmp_path / "request.json"
    path.write_bytes(data)
    assert cli.run(path)[0] == 2


def test_symlinked_request_is_rejected(tmp_path):
    target = write(tmp_path, request(tmp_path))
    link = tmp_path / "linked.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert cli.run(link)[0] == 2


def test_backend_exception_redacted_and_no_install(tmp_path, monkeypatch):
    def failure(**kwargs):
        raise RuntimeError("private C:\\Users\\secret\\token.json")
    monkeypatch.setattr(cli, "preflight_signed_windows_update", failure)
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 2 and "secret" not in json.dumps(report)
    assert report["install_authorized"] is False


def test_command_line_outputs_machine_readable_status_and_nonzero_exit(tmp_path):
    path = write(tmp_path, request(tmp_path))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.run([sys.executable, "-m", "autosport.windows_update_cli", str(path)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result["status"] == "DIAGNOSTIC_REJECTED"
    assert result["install_authorized"] is False
    assert "token" not in proc.stderr


def test_backend_cannot_inject_private_path_into_success_report(tmp_path, monkeypatch):
    """A success-shaped backend object is not itself diagnostic authority."""
    from types import SimpleNamespace
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: SimpleNamespace(
        decision=SimpleNamespace(disposition=r"C:\Users\secret\token.json"),
        version="1.2.3", schema=2, package_sha256="a" * 64,
    ))
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 2
    assert report == {"status": "DIAGNOSTIC_REJECTED", "install_authorized": False,
                      "independent_trust_root_verified": False,
                      "human_tested": False, "nvda_verified": False}


def test_backend_cannot_forge_authorized_receipt(tmp_path, monkeypatch):
    result = verified()
    object.__setattr__(result, "install_authorized", True)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: result)
    assert cli.run(write(tmp_path, request(tmp_path)))[0] == 2


def test_backend_receipt_must_match_requested_candidate(tmp_path, monkeypatch):
    result = verified()
    object.__setattr__(result, "package_sha256", "b" * 64)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: result)
    assert cli.run(write(tmp_path, request(tmp_path)))[0] == 2


def test_backend_receipt_must_match_source_version_and_schema(tmp_path, monkeypatch):
    for field, value in (("source_sha", "e" * 40), ("version", "1.2.4"), ("schema", 3)):
        result = verified()
        object.__setattr__(result, field, value)
        monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: result)
        assert cli.run(write(tmp_path, request(tmp_path)))[0] == 2, field


def test_no_change_diagnostic_is_distinct_from_eligible_update(tmp_path, monkeypatch):
    unchanged = VerifiedWindowsUpdate(
        decision=WindowsUpdateDecision("NO_CHANGE", "1.2.3", "1.2.3", 2, 2),
        package_sha256="a" * 64, source_sha="d" * 40, version="1.2.3", schema=2,
    )
    p = request(tmp_path)
    p["supported_schema_migrations"] = []
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: unchanged)
    code, report = cli.run(write(tmp_path, p))
    assert code == 0
    assert report["status"] == "DIAGNOSTIC_NO_CHANGE_UNTRUSTED_ROOT"
    assert report["transaction_preview"]["backup_required"] is False
    assert report["disposition"] == "NO_CHANGE"
    assert report["update_available"] is False
    assert report["install_authorized"] is False


def test_eligible_diagnostic_marks_availability_but_never_authorizes_install(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 0
    assert report["status"] == "DIAGNOSTIC_PASS_UNTRUSTED_ROOT"
    assert report["update_available"] is True
    assert report["install_authorized"] is False

@pytest.mark.parametrize("unsafe", [
    r"\\server\share\candidate.zip",
    r"//server/share/candidate.zip",
    r"\\?\C:\private\candidate.zip",
    r"\\.\PhysicalDrive0",
    r"C:drive-relative.zip",
    r"\rooted-on-current-drive.zip",
])
def test_offline_cli_rejects_network_device_and_drive_relative_candidate_paths(
    tmp_path, monkeypatch, unsafe
):
    value = request(tmp_path)
    value["package_path"] = unsafe
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "remote or device path reached the update verifier"
    ))
    code, result = cli.run(write(tmp_path, value))
    assert code == 2 and result["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("unsafe", [
    r"\\server\share\installed.json",
    r"//server/share/installed.json",
    r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\installed.json",
    r"D:installed.json",
])
def test_offline_cli_rejects_remote_installed_trust_paths(tmp_path, monkeypatch, unsafe):
    value = request(tmp_path)
    value["installed_manifest_path"] = unsafe
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "remote installed manifest reached the update verifier"
    ))
    assert cli.run(write(tmp_path, value))[0] == 2


def test_offline_cli_rejects_remote_request_before_any_file_open(monkeypatch):
    monkeypatch.setattr(cli.os, "open", lambda *args, **kwargs: pytest.fail(
        "attempted remote request file access"
    ))
    for name in (r"\\server\share\request.json", "//server/share/request.json", r"C:request.json"):
        code, report = cli.run(Path(name))
        assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("unsafe", [
    "updates/NUL", "updates/nul.txt", "updates/CON ",
    "updates/AUX.log", "updates/COM1.zip", "updates/LPT².log",
    "updates/candidate.zip:alternate", "updates/trailing. ",
    "updates/odd<name.zip", "updates/odd?.zip", "updates/odd|name.zip",
])
def test_offline_cli_rejects_windows_device_ads_and_ambiguous_names_before_verifier(
    tmp_path, monkeypatch, unsafe
):
    candidate = request(tmp_path)
    candidate["package_path"] = unsafe
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "Windows device or alternate stream reached verifier"
    ))
    code, report = cli.run(write(tmp_path, candidate))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("unsafe", [
    "updates/CON", "updates/NUL.txt", "updates/COM².json",
    "updates/installed.json:stream", "updates/installed. ",
])
def test_offline_cli_rejects_ambiguous_installed_manifest_paths(tmp_path, monkeypatch, unsafe):
    candidate = request(tmp_path)
    candidate["installed_manifest_path"] = unsafe
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "Windows device or alternate stream reached installed verifier"
    ))
    assert cli.run(write(tmp_path, candidate))[0] == 2


@pytest.mark.parametrize("unsafe", [
    "updates/NUL", "updates/CON.txt", "updates/COM1.json",
    "updates/request.json:stream", "updates/request. ",
])
def test_offline_cli_rejects_ambiguous_request_path_before_file_open(monkeypatch, unsafe):
    monkeypatch.setattr(cli.os, "open", lambda *a, **kw: pytest.fail(
        "Windows device or alternate stream reached request file open"
    ))
    assert cli.run(Path(unsafe))[0] == 2


def test_offline_cli_preserves_valid_names_with_reserved_prefixes(tmp_path, monkeypatch):
    candidate = request(tmp_path)
    candidate["package_path"] = "папка з пробілами/console.zip"
    candidate["installed_manifest_path"] = "state/com10.json"
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    code, report = cli.run(write(tmp_path, candidate))
    assert code == 0 and report["install_authorized"] is False


@pytest.mark.parametrize("unsafe", [
    "updates/../candidate.zip", "updates/./candidate.zip",
    "updates//candidate.zip", "updates\\\\candidate.zip",
])
def test_offline_cli_rejects_noncanonical_relative_package_path(tmp_path, monkeypatch, unsafe):
    candidate = request(tmp_path)
    candidate["package_path"] = unsafe
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "noncanonical update path reached verifier"
    ))
    code, report = cli.run(write(tmp_path, candidate))
    assert code == 2 and report["install_authorized"] is False


def test_request_parent_symlink_is_rejected_before_open(tmp_path, monkeypatch):
    """A lexically local request must not traverse a redirected parent."""
    real = tmp_path / "real"
    real.mkdir()
    req = write(real, request(real))
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "redirected request reached update verifier"
    ))
    code, report = cli.run(alias / req.name)
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("field", ["package_path", "installed_manifest_path"])
def test_package_or_installed_parent_symlink_rejected_before_verifier(tmp_path, monkeypatch, field):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable")
    payload = request(tmp_path)
    payload[field] = "alias/candidate.zip"
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "redirected package/manifest reached verifier"
    ))
    code, report = cli.run(write(tmp_path, payload))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("spelling", ["./request.json", "folder/../request.json", "folder//request.json"])
def test_cli_rejects_raw_noncanonical_request_spelling(tmp_path, spelling):
    req = tmp_path / "request.json"
    req.write_text(json.dumps(request(tmp_path)), encoding="utf-8")
    # Use a string to preserve the raw CLI argument: Path() would silently
    # normalize './' and repeated separators before lexical validation.
    code, report = cli.run(str(tmp_path) + "/" + spelling)
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


def test_cli_entrypoint_preserves_raw_path_before_validation(monkeypatch, capsys):
    seen = []
    def fake_run(raw):
        seen.append(raw)
        return 2, {"status": "DIAGNOSTIC_REJECTED", "install_authorized": False}
    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.main(["folder/./request.json"]) == 2
    assert seen == ["folder/./request.json"]
    assert json.loads(capsys.readouterr().out)["install_authorized"] is False


def test_windows_reparse_attribute_rejected_without_following(monkeypatch, tmp_path):
    """Junctions/reparse points must fail closed even when not POSIX symlinks."""
    import stat
    from types import SimpleNamespace
    original = Path.lstat
    target = tmp_path / "junction"
    target.mkdir()
    def tagged_lstat(path):
        result = original(path)
        if path == target:
            return SimpleNamespace(
                st_mode=result.st_mode,
                st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
            )
        return result
    monkeypatch.setattr(Path, "lstat", tagged_lstat)
    with pytest.raises(cli.UpdateCliError, match="redirected"):
        cli._reject_redirected_path(target / "candidate.zip")


def test_preflight_success_receipt_with_wrong_migration_chain_cannot_show_preview(tmp_path, monkeypatch):
    p = request(tmp_path)
    p["supported_schema_migrations"] = []
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    code, report = cli.run(write(tmp_path, p))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert "transaction_preview" not in report


def test_request_path_replacement_before_open_is_rejected(tmp_path, monkeypatch):
    """Do not silently validate a different JSON request after the path guard."""
    initial = write(tmp_path, request(tmp_path))
    replacement = tmp_path / "replacement.json"
    altered = request(tmp_path)
    altered["expected_package_sha256"] = "e" * 64
    replacement.write_text(json.dumps(altered), encoding="utf-8")
    real_open = cli.os.open
    switched = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal switched
        if not switched and Path(path) == initial:
            switched = True
            os.replace(replacement, initial)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(cli.os, "open", replacing_open)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "substituted request reached the verifier"
    ))
    code, result = cli.run(initial)
    assert switched
    assert code == 2 and result["status"] == "DIAGNOSTIC_REJECTED"


def test_request_path_replacement_during_read_is_rejected(tmp_path, monkeypatch):
    """A held descriptor is not enough when the named request is replaced."""
    initial = write(tmp_path, request(tmp_path))
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps(request(tmp_path)), encoding="utf-8")
    real_fstat = cli.os.fstat
    seen = 0

    def swapping_fstat(fd):
        nonlocal seen
        seen += 1
        if seen == 2:
            os.replace(replacement, initial)
        return real_fstat(fd)

    monkeypatch.setattr(cli.os, "fstat", swapping_fstat)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "replaced request path reached the verifier"
    ))
    code, result = cli.run(initial)
    assert seen >= 2
    assert code == 2 and result["status"] == "DIAGNOSTIC_REJECTED"


def test_request_change_time_witness_rejects_undetected_rewrite(tmp_path, monkeypatch):
    """Windows creation-time stat cannot substitute for NT ChangeTime."""
    path = write(tmp_path, request(tmp_path))
    seen = 0

    def changing_witness(_fd):
        nonlocal seen
        seen += 1
        return 100 if seen == 1 else 101

    monkeypatch.setattr(cli, "_native_change_time_100ns", changing_witness)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "request changed under a held handle"
    ))
    code, report = cli.run(path)
    assert seen == 2
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


def test_request_reparse_parent_replaced_after_read_is_rejected(tmp_path, monkeypatch):
    """The directory path must still be ordinary after the descriptor read."""
    root = tmp_path / "input"
    root.mkdir()
    path = write(root, request(root))
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    write(replacement, request(replacement))
    original_fstat = cli.os.fstat
    count = 0

    def replace_parent(fd):
        nonlocal count
        count += 1
        if count == 2:
            root.rename(tmp_path / "moved")
            try:
                root.symlink_to(replacement, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("directory symlinks unavailable")
        return original_fstat(fd)

    monkeypatch.setattr(cli.os, "fstat", replace_parent)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "reparse parent reached the verifier"
    ))
    code, report = cli.run(path)
    assert count >= 2
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


@pytest.mark.parametrize("field", ["package_path", "installed_manifest_path"])
@pytest.mark.parametrize("device_name", ["CONIN$", "conout$.json", "folder/CONOUT$.txt"])
def test_console_device_alias_rejected_before_verifier(tmp_path, monkeypatch, field, device_name):
    payload = request(tmp_path)
    payload[field] = device_name
    monkeypatch.setattr(
        cli, "preflight_signed_windows_update",
        lambda **kwargs: pytest.fail("console device path reached verifier"),
    )
    code, report = cli.run(write(tmp_path, payload))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert report["install_authorized"] is False


def test_request_parent_stays_anchored_when_cwd_changes_during_read(tmp_path, monkeypatch):
    """A relative request must never borrow package paths from a later cwd."""
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    write(original, request(original))
    monkeypatch.chdir(original)
    read_original = cli._read_request

    def switch_cwd_after_read(path):
        parsed = read_original(path)
        os.chdir(replacement)
        return parsed

    observed = []
    monkeypatch.setattr(cli, "_read_request", switch_cwd_after_read)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **kwargs: (
        observed.append(kwargs) or verified()
    ))
    code, report = cli.run("запит на оновлення.json")
    assert code == 0 and report["install_authorized"] is False
    assert len(observed) == 1
    assert observed[0]["package_path"] == original / "папка з пробілами/candidate.zip"
    assert observed[0]["installed_manifest_path"] == original / "installed state.json"


def test_relative_request_raw_dot_segments_rejected_before_path_normalization(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, request(tmp_path))
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "raw noncanonical request path reached verifier"
    ))
    for unsafe in ("./запит на оновлення.json", "nested/../запит на оновлення.json"):
        code, report = cli.run(unsafe)
        assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


def test_request_parent_inode_swap_with_same_hardlinked_file_rejected(tmp_path, monkeypatch):
    """A directory can be replaced while its named file keeps the SAME inode.

    Checking only the request file's stat tuple and rejecting symlinks cannot
    detect a parent-directory rename if a hardlink preserves the leaf inode.
    """
    current = tmp_path / "current"
    alternate = tmp_path / "alternate"
    current.mkdir()
    alternate.mkdir()
    path = write(current, request(current))
    try:
        os.link(path, alternate / path.name)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable")
    old_file_identity = cli._request_identity(path.lstat())
    original_fstat = cli.os.fstat
    seen = 0

    def swap_directory(fd):
        nonlocal seen
        seen += 1
        if seen == 2:
            current.rename(tmp_path / "former")
            alternate.rename(current)
        return original_fstat(fd)

    monkeypatch.setattr(cli.os, "fstat", swap_directory)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "same-inode request from swapped parent reached the verifier"
    ))
    code, report = cli.run(path)
    assert seen >= 2
    assert cli._request_identity(path.lstat()) == old_file_identity
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"


def test_request_parent_swap_during_json_parse_rejected(tmp_path, monkeypatch):
    """A same-inode parent swap after read but before return is not accepted."""
    original = tmp_path / "input"
    alternate = tmp_path / "alternate"
    original.mkdir()
    alternate.mkdir()
    path = write(original, request(original))
    try:
        os.link(path, alternate / path.name)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks unavailable")
    real_hook = cli._no_duplicate_keys
    swapped = False

    def swap_after_read(pairs):
        nonlocal swapped
        if not swapped:
            swapped = True
            original.rename(tmp_path / "previous")
            alternate.rename(original)
        return real_hook(pairs)

    monkeypatch.setattr(cli, "_no_duplicate_keys", swap_after_read)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "swapped parent after parse reached the verifier"
    ))
    code, report = cli.run(path)
    assert swapped
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"

@pytest.mark.parametrize("mutation", ["replace_same_bytes", "rewrite_same_bytes"])
def test_request_leaf_change_during_json_parse_never_reaches_preflight(tmp_path, monkeypatch, mutation):
    """The parser may run callbacks after fd close: the leaf must stay pinned.

    Replacing a request with an identical JSON document is still a different
    operator-selected file; rewriting the same bytes changes file metadata.
    Neither operation may yield a success-shaped diagnostic.
    """
    path = write(tmp_path, request(tmp_path))
    original = path.read_bytes()
    original_identity = cli._request_identity(path.lstat())
    real_hook = cli._no_duplicate_keys
    changed = False
    calls = []

    def change_after_read(pairs):
        nonlocal changed
        if not changed:
            changed = True
            if mutation == "replace_same_bytes":
                replacement = tmp_path / "replacement.json"
                replacement.write_bytes(original)
                os.replace(replacement, path)
            else:
                with path.open("r+b") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Make the metadata change deterministic even on filesystems
                # with coarse write-time granularity (including Windows).
                old_mtime_ns = original_identity[3]
                os.utime(path, ns=(old_mtime_ns + 5_000_000_000,
                                   old_mtime_ns + 5_000_000_000))
        return real_hook(pairs)

    monkeypatch.setattr(cli, "_no_duplicate_keys", change_after_read)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **kw: (
        calls.append(kw) or verified()
    ))
    code, report = cli.run(path)
    assert changed
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert report["install_authorized"] is False
    assert calls == []
    if mutation == "replace_same_bytes":
        assert cli._request_identity(path.lstat()) != original_identity


@pytest.mark.parametrize("tamper", [
    lambda p: p.update(secret=r"C:\\Users\\private\\token.json"),
    lambda p: p.update(install_authorized=True),
    lambda p: p.update(backup_required=1),
    lambda p: p.update(migration_steps=[{"from_schema": 1, "to_schema": 2, "secret": "private"}]),
    lambda p: p.update(migration_steps=[{"from_schema": True, "to_schema": 2}]),
    lambda p: p.update(source_sha="e" * 40),
    lambda p: p.update(independent_installer_trust_required=False),
])
def test_backend_preview_cannot_forge_authorization_or_leak_private_data(
    tmp_path, monkeypatch, tamper
):
    """The preview producer is not allowed to choose operator-visible bytes."""
    from autosport.windows_update_transaction_plan import preview_windows_update_transaction
    original = preview_windows_update_transaction(
        verified(), supported_schema_migrations=((1, 2),)
    )
    tamper(original)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    monkeypatch.setattr(cli, "preview_windows_update_transaction", lambda *a, **k: original)
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 2
    assert report == {
        "status": "DIAGNOSTIC_REJECTED", "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False, "nvda_verified": False,
    }


def test_valid_transaction_preview_is_exact_sanitized_copy(tmp_path, monkeypatch):
    """Preserve legitimate non-authorizing migration preview behavior."""
    from autosport.windows_update_transaction_plan import preview_windows_update_transaction
    original = preview_windows_update_transaction(
        verified(), supported_schema_migrations=((1, 2),)
    )
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    monkeypatch.setattr(cli, "preview_windows_update_transaction", lambda *a, **k: original)
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 0 and report["transaction_preview"] == original
    assert report["transaction_preview"] is not original
    assert report["transaction_preview"]["migration_steps"] is not original["migration_steps"]


def test_request_postparse_native_change_time_must_match_original_handle(tmp_path, monkeypatch):
    """Reject a Windows in-place rewrite after JSON parsing begins.

    Windows stat st_ctime_ns can denote creation time, not NT ChangeTime.
    The latter must be checked after parsing, not only during descriptor read.
    """
    path = write(tmp_path, request(tmp_path))
    parsed = False
    real_hook = cli._no_duplicate_keys
    observed = []

    def parsing_hook(pairs):
        nonlocal parsed
        parsed = True
        return real_hook(pairs)

    def native_change(_fd):
        observed.append(parsed)
        return 101 if parsed else 100

    monkeypatch.setattr(cli, "_no_duplicate_keys", parsing_hook)
    monkeypatch.setattr(cli, "_native_change_time_100ns", native_change)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "request with changed native postparse timestamp reached preflight"
    ))
    code, report = cli.run(path)
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert observed.count(False) >= 2 and True in observed


def test_request_postparse_reopen_rejects_same_byte_path_swap(tmp_path, monkeypatch):
    """A replacement between final lstat and the postparse open is rejected."""
    path = write(tmp_path, request(tmp_path))
    original = path.read_bytes()
    original_open = cli.os.open
    opens = 0

    def swapped_open(file, flags, *args, **kwargs):
        nonlocal opens
        if str(file) == str(path):
            opens += 1
            if opens == 2:
                replacement = tmp_path / "swap.json"
                replacement.write_bytes(original)
                os.replace(replacement, path)
        return original_open(file, flags, *args, **kwargs)

    monkeypatch.setattr(cli.os, "open", swapped_open)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "postparse replacement reached preflight"
    ))
    code, report = cli.run(path)
    assert opens >= 2
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"

@pytest.mark.parametrize("mutation", ["replace_same_bytes", "rewrite_candidate"])
def test_cli_rejects_request_mutated_during_signed_preflight(tmp_path, monkeypatch, mutation):
    """A signed preflight can take time; its operator request must not drift."""
    path = write(tmp_path, request(tmp_path))
    original = path.read_bytes()

    def verifier(**_kwargs):
        if mutation == "replace_same_bytes":
            replacement = tmp_path / "replacement.json"
            replacement.write_bytes(original)
            os.replace(replacement, path)
        else:
            altered = request(tmp_path)
            altered["candidate_version"] = "1.2.4"
            path.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
        return verified()

    monkeypatch.setattr(cli, "preflight_signed_windows_update", verifier)
    code, report = cli.run(path)
    assert code == 2
    assert report == {
        "status": "DIAGNOSTIC_REJECTED",
        "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False,
        "nvda_verified": False,
    }


def test_cli_rejects_request_mutated_during_preview(tmp_path, monkeypatch):
    path = write(tmp_path, request(tmp_path))
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    original_preview = cli.preview_windows_update_transaction

    def preview(*args, **kwargs):
        result = original_preview(*args, **kwargs)
        path.write_bytes(path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(cli, "preview_windows_update_transaction", preview)
    assert cli.run(path)[0] == 2


def test_cli_rejects_request_parent_swap_during_signed_preflight(tmp_path, monkeypatch):
    """The leaf may be the same hardlink while its ancestor is replaced."""
    parent = tmp_path / "original"
    parent.mkdir()
    path = write(parent, request(parent))
    moved = tmp_path / "moved"
    substitute = tmp_path / "substitute"
    substitute.mkdir()
    os.link(path, substitute / path.name)

    def verifier(**_kwargs):
        parent.rename(moved)
        substitute.rename(parent)
        return verified()

    monkeypatch.setattr(cli, "preflight_signed_windows_update", verifier)
    assert cli.run(path)[0] == 2


def test_cli_rejects_native_change_time_advance_during_signed_preflight(tmp_path, monkeypatch):
    """Simulate Windows ChangeTime advancing with otherwise stable stat data."""
    path = write(tmp_path, request(tmp_path))
    phase = [100]
    monkeypatch.setattr(cli, "_native_change_time_100ns", lambda _fd: phase[0])

    def verifier(**_kwargs):
        phase[0] = 101
        return verified()

    monkeypatch.setattr(cli, "preflight_signed_windows_update", verifier)
    assert cli.run(path)[0] == 2


@pytest.mark.parametrize("mutation", ["replace_same_bytes", "rewrite_different_bytes", "rewrite_same_bytes"])
def test_request_changed_after_parse_before_witness_never_reaches_preflight(
    tmp_path, monkeypatch, mutation
):
    """The baseline must be returned by the parser, not captured afterward.

    A late pathname swap between parsing and the caller's first witness
    previously became the new accepted baseline, even though the CLI still
    used the *old* parsed candidate. No backend may see that stale request.
    """
    path = write(tmp_path, request(tmp_path))
    original = path.read_bytes()
    parsed_original = cli._read_request

    def switch_after_parse(selected):
        parsed = parsed_original(selected)
        if mutation == "replace_same_bytes":
            replacement = tmp_path / "new request.json"
            replacement.write_bytes(original)
            os.replace(replacement, path)
        elif mutation == "rewrite_different_bytes":
            altered = request(tmp_path)
            altered["candidate_version"] = "1.2.4"
            path.write_text(json.dumps(altered, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_bytes(original)
        return parsed

    monkeypatch.setattr(cli, "_read_request", switch_after_parse)
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: pytest.fail(
        "postparse request swap reached signed preflight"
    ))
    code, report = cli.run(path)
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert report["install_authorized"] is False


def test_deeply_nested_json_request_fails_closed_without_traceback(tmp_path):
    """A bounded JSON file can still exceed CPython's decoder stack depth."""
    payload = b"[" * 1400 + b"0" + b"]" * 1400
    path = tmp_path / "глибокий запит.json"
    path.write_bytes(payload)
    code, report = cli.run(path)
    assert code == 2
    assert report == {
        "status": "DIAGNOSTIC_REJECTED", "install_authorized": False,
        "independent_trust_root_verified": False,
        "human_tested": False, "nvda_verified": False,
    }


def test_deeply_nested_json_command_does_not_expose_traceback(tmp_path):
    path = tmp_path / "deep.json"
    path.write_bytes(b"[" * 1400 + b"0" + b"]" * 1400)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-m", "autosport.windows_update_cli", str(path)],
        capture_output=True, text=True, env=env, timeout=10,
    )
    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout)["status"] == "DIAGNOSTIC_REJECTED"


def test_request_path_str_subclass_cannot_run_conversion_callbacks(tmp_path):
    calls = []
    class HostileString(str):
        def __str__(self):
            calls.append("converted")
            return super().__str__()
    code, report = cli.run(HostileString(str(tmp_path / "absent.json")))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert calls == []


def test_request_path_path_subclass_cannot_run_conversion_callbacks(tmp_path):
    calls = []
    class HostilePath(type(tmp_path)):
        def __str__(self):
            calls.append("converted")
            return super().__str__()
    code, report = cli.run(HostilePath(tmp_path / "absent.json"))
    assert code == 2 and report["status"] == "DIAGNOSTIC_REJECTED"
    assert calls == []


def test_direct_parser_normalizes_decoder_recursion_error(tmp_path, monkeypatch):
    path = write(tmp_path, request(tmp_path))
    def overflow(*args, **kwargs):
        raise RecursionError("untrusted parser nesting")
    monkeypatch.setattr(cli.json, "loads", overflow)
    with pytest.raises(cli.UpdateCliError, match="strict UTF-8 JSON"):
        cli._read_request(path)


def test_direct_parser_does_not_coerce_subclass_path(tmp_path):
    calls = []
    class HostileString(str):
        def __str__(self):
            calls.append("converted")
            return super().__str__()
    with pytest.raises(cli.UpdateCliError):
        cli._read_request(HostileString(str(tmp_path / "absent.json")))
    assert calls == []


def test_cli_accepts_maximum_release_version_at_request_boundary(tmp_path):
    payload = request(tmp_path)
    payload["candidate_version"] = "2147483647.2147483647.2147483647"
    parsed = cli._arguments(payload, tmp_path)
    assert parsed["candidate_version"] == payload["candidate_version"]

@pytest.mark.parametrize("field,new_value", [
    ("from_version", "0.9.0"),
    ("package_sha256", "b" * 64),
    ("source_sha", "e" * 40),
])
def test_preview_callback_cannot_rebind_validated_receipt(tmp_path, monkeypatch, field, new_value):
    """The preview backend must not rewrite a previously checked signed receipt.

    A frozen dataclass is mutable via object.__setattr__. Without an
    independent receipt copy, the CLI can return success with a forged FROM
    version or mismatched SHA in the nested preview despite its own top-level
    operator-selected identity.
    """
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    original_preview = cli.preview_windows_update_transaction

    def mutated_preview(receipt, *, supported_schema_migrations):
        if field == "from_version":
            object.__setattr__(receipt.decision, field, new_value)
        else:
            object.__setattr__(receipt, field, new_value)
        return original_preview(
            receipt, supported_schema_migrations=supported_schema_migrations
        )

    monkeypatch.setattr(cli, "preview_windows_update_transaction", mutated_preview)
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 2
    assert report["status"] == "DIAGNOSTIC_REJECTED"
    assert report["install_authorized"] is False


def test_canonical_preview_still_reports_exact_installed_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "preflight_signed_windows_update", lambda **_: verified())
    code, report = cli.run(write(tmp_path, request(tmp_path)))
    assert code == 0
    assert report["transaction_preview"]["from_version"] == "1.0.0"
    assert report["transaction_preview"]["from_schema"] == 1
    assert report["transaction_preview"]["package_sha256"] == "a" * 64
    assert report["transaction_preview"]["source_sha"] == "d" * 40
    assert report["install_authorized"] is False
