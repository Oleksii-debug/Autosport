from __future__ import annotations

import os
import sys
from pathlib import Path


def _windows_known_folder_local_app_data() -> Path:
    """Resolve the current user's LocalAppData through the Windows Known Folder API."""

    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        raise OSError("Windows LocalAppData known folder is unavailable on this platform")

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder_id_local_app_data = _GUID(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(
            0x9D,
            0x55,
            0x7B,
            0x8E,
            0x7F,
            0x15,
            0x70,
            0x91,
        ),
    )

    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        # Use WinDLL rather than OleDLL so failed HRESULT values remain observable.
        # OleDLL would translate RPC_E_CHANGED_MODE into OSError before the
        # explicit COM-apartment handling below can accept that existing state.
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise OSError("Windows Known Folder APIs are unavailable") from exc

    co_initialize_ex = ole32.CoInitializeEx
    co_initialize_ex.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    co_initialize_ex.restype = ctypes.c_long

    co_uninitialize = ole32.CoUninitialize
    co_uninitialize.argtypes = []
    co_uninitialize.restype = None

    co_task_mem_free = ole32.CoTaskMemFree
    co_task_mem_free.argtypes = [ctypes.c_void_p]
    co_task_mem_free.restype = None

    get_known_folder_path = shell32.SHGetKnownFolderPath
    get_known_folder_path.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_known_folder_path.restype = ctypes.c_long

    # COINIT_APARTMENTTHREADED. RPC_E_CHANGED_MODE means COM is already initialized
    # on this thread with a different concurrency model; that existing apartment is
    # sufficient for the Known Folder call and must not be uninitialized here.
    initialize_result = int(co_initialize_ex(None, 0x2)) & 0xFFFFFFFF
    should_uninitialize = initialize_result in {0x00000000, 0x00000001}
    if initialize_result not in {
        0x00000000,  # S_OK
        0x00000001,  # S_FALSE
        0x80010106,  # RPC_E_CHANGED_MODE
    }:
        raise OSError(
            f"cannot initialize Windows Known Folder access "
            f"(HRESULT 0x{initialize_result:08x})"
        )

    path_pointer = ctypes.c_void_p()
    try:
        result = int(
            get_known_folder_path(
                ctypes.byref(folder_id_local_app_data),
                0,
                None,
                ctypes.byref(path_pointer),
            )
        ) & 0xFFFFFFFF
        if result & 0x80000000:
            raise OSError(
                f"cannot resolve FOLDERID_LocalAppData (HRESULT 0x{result:08x})"
            )
        if not path_pointer.value:
            raise OSError("FOLDERID_LocalAppData returned an empty path")

        value = ctypes.wstring_at(path_pointer.value)
        if not value or "\x00" in value:
            raise OSError("FOLDERID_LocalAppData returned an invalid path")
        resolved = Path(value)
        if not resolved.is_absolute():
            raise OSError("FOLDERID_LocalAppData did not resolve to an absolute path")
        return resolved
    finally:
        if path_pointer.value:
            co_task_mem_free(path_pointer)
        if should_uninitialize:
            co_uninitialize()


def default_workspace() -> Path:
    override = os.environ.get("AUTOSPORT_WORKSPACE")
    if override is not None and override.strip():
        try:
            override_path = Path(override).expanduser()
        except RuntimeError as exc:
            raise ValueError(
                "AUTOSPORT_WORKSPACE home expansion could not be resolved; "
                "configure an absolute workspace path"
            ) from exc
        if not override_path.is_absolute():
            raise ValueError(
                "AUTOSPORT_WORKSPACE must be an absolute path so durable workspace identity "
                "does not depend on the process working directory"
            )
        return override_path

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_app_data_path = Path(local_app_data)
        if not local_app_data_path.is_absolute():
            raise ValueError(
                "LOCALAPPDATA must be an absolute path so durable workspace identity "
                "does not depend on the process working directory"
            )
        return local_app_data_path / "Autosport" / "workspace"

    if sys.platform == "win32":
        try:
            local_app_data_path = _windows_known_folder_local_app_data()
        except OSError as exc:
            raise ValueError(
                "Windows LocalAppData known folder could not be resolved; "
                "configure an absolute AUTOSPORT_WORKSPACE"
            ) from exc
        if not local_app_data_path.is_absolute():
            raise ValueError(
                "Windows LocalAppData known folder must resolve to an absolute path"
            )
        return local_app_data_path / "Autosport" / "workspace"

    try:
        home = Path.home()
    except RuntimeError as exc:
        raise ValueError(
            "home directory could not be resolved; configure an absolute AUTOSPORT_WORKSPACE"
        ) from exc
    if not home.is_absolute():
        raise ValueError(
            "home directory must be an absolute path so durable workspace identity "
            "does not depend on the process working directory"
        )
    return home / ".autosport" / "workspace"
