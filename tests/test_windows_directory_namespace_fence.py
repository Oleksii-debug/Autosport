from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"


def _trusted_snapshot_verifier_source() -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = "$trustedSourceSnapshotVerifierLauncher = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def _trusted_directory_fence_type_source() -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = "$trustedDirectoryFenceTypeSource = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def test_windows_build_holds_directory_namespace_fence_through_both_consumers() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    add_type = "Add-Type -TypeDefinition $trustedDirectoryFenceTypeSource -Language CSharp"
    acl_fence = (
        '& icacls $trustedBuildRoot /deny '
        '"*${currentSid}:(OI)(CI)(WD,AD,WEA,WA,DE,DC)" /T /C'
    )
    directory_open = (
        "$namespaceHandle = "
        "[Autosport.Release.TrustedDirectoryFence]::OpenReadFence($trustedBuildDirectoryPath)"
    )
    file_open = "$lockStream = [System.IO.File]::Open("
    snapshot_verify = (
        "$trustedBuildManifestJson | & $pythonExecutable -I -S -c "
        "$trustedSourceSnapshotVerifierLauncher $trustedBuildRoot"
    )
    gui_build = (
        "& $pythonExecutable -I -m PyInstaller --noconfirm --clean --onefile --windowed "
        "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
        "--specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
    )
    data_build = (
        "& $pythonExecutable -I -m PyInstaller --noconfirm --clean --onefile --console "
        "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
        "--specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry"
    )
    directory_dispose = "$trustedBuildDirectoryLocks[$directoryLockIndex].Dispose()"
    acl_remove = '& icacls $trustedBuildRoot /remove:d "*${currentSid}" /T /C'

    add_type_index = script.index(add_type)
    acl_index = script.index(acl_fence, add_type_index)
    directory_index = script.index(directory_open, acl_index)
    file_index = script.index(file_open, directory_index)
    first_verify_index = script.index(snapshot_verify, file_index)
    gui_index = script.index(gui_build, first_verify_index)
    data_index = script.index(data_build, gui_index)
    final_verify_index = script.index(snapshot_verify, data_index)
    dispose_index = script.index(directory_dispose, final_verify_index)
    remove_index = script.index(acl_remove, dispose_index)

    assert (
        add_type_index
        < acl_index
        < directory_index
        < file_index
        < first_verify_index
        < gui_index
        < data_index
        < final_verify_index
        < dispose_index
        < remove_index
    )
    assert "private const uint FILE_LIST_DIRECTORY = 0x00000001;" in script
    assert "private const uint FILE_SHARE_READ = 0x00000001;" in script
    assert "private const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;" in script
    assert (
        "$trustedBuildDirectoryLocks = "
        "[System.Collections.Generic.List[Microsoft.Win32.SafeHandles.SafeFileHandle]]::new()"
    ) in script
    assert "expected_directories = {\"\"}" in script
    assert "actual_directories = {\"\"}" in script
    assert "source snapshot directory membership mismatch" in script


def test_snapshot_verifier_rejects_unexpected_empty_directory(tmp_path: Path) -> None:
    launcher = _trusted_snapshot_verifier_source()
    root = tmp_path / "trusted-source"
    root.mkdir()
    canonical = root / "canonical.py"
    canonical_bytes = b"VALUE = 'canonical'\n"
    canonical.write_bytes(canonical_bytes)
    manifest = {
        "canonical.py": hashlib.sha256(canonical_bytes).hexdigest(),
    }
    (root / "unexpected-empty-directory").mkdir()

    changed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root)],
        input=json.dumps(manifest, sort_keys=True),
        capture_output=True,
        text=True,
    )

    assert changed.returncode != 0
    assert "source snapshot directory membership mismatch" in changed.stderr
    assert "unexpected-empty-directory" in changed.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows directory share-mode regression")
def test_windows_directory_fence_rejects_preopened_namespace_writer(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    file_list_directory = 0x00000001
    file_add_file = 0x00000002
    file_add_subdirectory = 0x00000004
    file_delete_child = 0x00000040
    delete_access = 0x00010000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    error_sharing_violation = 32
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    root = tmp_path / "trusted-source"
    root.mkdir()
    fence_source = tmp_path / "trusted-directory-fence.cs"
    fence_source.write_text(_trusted_directory_fence_type_source(), encoding="utf-8")

    def raw_handle(handle: object) -> int | None:
        if handle is None:
            return None
        if isinstance(handle, int):
            return handle
        return ctypes.cast(handle, ctypes.c_void_p).value

    def is_invalid(handle: object) -> bool:
        return raw_handle(handle) in {None, invalid_handle_value}

    def open_directory(desired_access: int, share_mode: int):
        ctypes.set_last_error(0)
        handle = create_file(
            str(root),
            desired_access,
            share_mode,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        return handle, ctypes.get_last_error()

    production_fence_script = r"""
$ErrorActionPreference = 'Stop'
$source = Get-Content -Raw -LiteralPath $env:AUTOSPORT_FENCE_SOURCE
Add-Type -TypeDefinition $source -Language CSharp
try {
  $handle = [Autosport.Release.TrustedDirectoryFence]::OpenReadFence($env:AUTOSPORT_FENCE_ROOT)
  try {
    [Console]::Out.WriteLine('FENCE=PASS')
  } finally {
    $handle.Dispose()
  }
} catch {
  $current = $_.Exception
  while ($null -ne $current) {
    if ($current -is [System.ComponentModel.Win32Exception]) {
      [Console]::Error.WriteLine("WIN32_ERROR=$($current.NativeErrorCode)")
      exit 23
    }
    $current = $current.InnerException
  }
  throw
}
"""

    def run_production_fence() -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["AUTOSPORT_FENCE_SOURCE"] = str(fence_source)
        env["AUTOSPORT_FENCE_ROOT"] = str(root)
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", production_fence_script],
            capture_output=True,
            text=True,
            env=env,
        )

    sid_result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    sid = sid_result.stdout.strip()
    assert sid.startswith("S-")
    principal = f"*{sid}"

    def apply_mutation_deny() -> None:
        subprocess.run(
            [
                "icacls",
                str(root),
                "/deny",
                f"{principal}:(OI)(CI)(WD,AD,WEA,WA,DE,DC)",
                "/T",
                "/C",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def remove_mutation_deny() -> None:
        subprocess.run(
            ["icacls", str(root), "/remove:d", principal, "/T", "/C"],
            check=True,
            capture_output=True,
            text=True,
        )

    hostile_access = (
        file_list_directory
        | file_add_file
        | file_add_subdirectory
        | file_delete_child
        | delete_access
    )
    share_all = file_share_read | file_share_write | file_share_delete
    hostile_handle = None
    reader_handle = None
    fence_handle = None
    acl_applied = False
    try:
        hostile_handle, hostile_error = open_directory(hostile_access, share_all)
        assert not is_invalid(hostile_handle), hostile_error

        apply_mutation_deny()
        acl_applied = True

        production_blocked = run_production_fence()
        assert production_blocked.returncode == 23, (
            production_blocked.stdout,
            production_blocked.stderr,
        )
        assert "WIN32_ERROR=32" in production_blocked.stderr

        blocked_handle, blocked_error = open_directory(
            file_list_directory,
            file_share_read,
        )
        if not is_invalid(blocked_handle):
            close_handle(blocked_handle)
        assert is_invalid(blocked_handle)
        assert blocked_error == error_sharing_violation

        assert close_handle(hostile_handle)
        hostile_handle = None

        remove_mutation_deny()
        acl_applied = False

        reader_handle, reader_error = open_directory(
            file_list_directory,
            file_share_read,
        )
        assert not is_invalid(reader_handle), reader_error

        apply_mutation_deny()
        acl_applied = True

        production_allowed = run_production_fence()
        assert production_allowed.returncode == 0, (
            production_allowed.stdout,
            production_allowed.stderr,
        )
        assert "FENCE=PASS" in production_allowed.stdout

        assert close_handle(reader_handle)
        reader_handle = None

        fence_handle, fence_error = open_directory(
            file_list_directory,
            file_share_read,
        )
        assert not is_invalid(fence_handle), fence_error

        remove_mutation_deny()
        acl_applied = False

        fresh_writer, fresh_writer_error = open_directory(hostile_access, share_all)
        if not is_invalid(fresh_writer):
            close_handle(fresh_writer)
        assert is_invalid(fresh_writer)
        assert fresh_writer_error == error_sharing_violation
    finally:
        if hostile_handle is not None and not is_invalid(hostile_handle):
            close_handle(hostile_handle)
        if reader_handle is not None and not is_invalid(reader_handle):
            close_handle(reader_handle)
        if fence_handle is not None and not is_invalid(fence_handle):
            close_handle(fence_handle)
        if acl_applied:
            remove_mutation_deny()
