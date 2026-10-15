"""Seal the OS-owned root-selection store locator before authority composition.

The stable selector witness must not follow caller-editable HOME/XDG/LOCALAPPDATA,
but resolving the OS account location through mutable stdlib dispatch at call time is
not a trust root either.  Capture the exact platform resolver callables when the
product package is composed, verify their public dispatch identities on every use,
and invoke only the captured callables.  The existing root-selection dispatch guard
then freezes this resolver together with the rest of the canonical selection chain.

No second store, registry, root, or persistence schema is introduced.
"""
from __future__ import annotations

import os as _os
from pathlib import Path

from . import monotonic_authority_root_binding as _root


_error = _root.AuthorityRootSelectionConfigurationError
_platform_name = _os.name

if _platform_name == "nt":
    import ctypes as _ctypes

    _create_unicode_buffer = _ctypes.create_unicode_buffer
    try:
        _shell32 = _ctypes.windll.shell32  # type: ignore[attr-defined]
        _sh_get_folder_path = _shell32.SHGetFolderPathW
    except (AttributeError, OSError) as exc:  # pragma: no cover - Windows import gate
        raise RuntimeError("Windows product state resolver is unavailable") from exc

    def _sealed_stable_root_selection_store() -> Path:
        if _os.name != _platform_name:
            raise _error("product root-selection platform identity changed")
        if _ctypes.create_unicode_buffer is not _create_unicode_buffer:
            raise _error("Windows root-selection buffer resolver dispatch was rebound")
        try:
            live_shell32 = _ctypes.windll.shell32  # type: ignore[attr-defined]
            live_resolver = live_shell32.SHGetFolderPathW
        except (AttributeError, OSError) as exc:
            raise _error("Windows root-selection resolver dispatch was rebound") from exc
        if live_shell32 is not _shell32 or live_resolver is not _sh_get_folder_path:
            raise _error("Windows root-selection resolver dispatch was rebound")

        buffer = _create_unicode_buffer(32768)
        try:
            result = _sh_get_folder_path(
                None,
                0x001C,  # CSIDL_LOCAL_APPDATA
                None,
                0,
                buffer,
            )
        except (AttributeError, OSError, ValueError) as exc:
            raise _error("cannot resolve product-owned Windows root-selection store") from exc
        if result != 0 or not buffer.value:
            raise _error("cannot resolve product-owned Windows root-selection store")
        base = Path(buffer.value)
        relative = Path("Autosport") / "application-state" / "monotonic-root-selection-v1"
        if not base.is_absolute():
            raise _error("product-owned root-selection store must be absolute")
        return base / relative

else:
    try:
        import pwd as _pwd
    except ImportError as exc:  # pragma: no cover - platform contract
        raise RuntimeError("POSIX account resolver is unavailable") from exc

    _getuid = _os.getuid
    _getpwuid = _pwd.getpwuid

    def _sealed_stable_root_selection_store() -> Path:
        if _os.name != _platform_name:
            raise _error("product root-selection platform identity changed")
        if _os.getuid is not _getuid or _pwd.getpwuid is not _getpwuid:
            raise _error("POSIX root-selection resolver dispatch was rebound")
        try:
            home = _getpwuid(_getuid()).pw_dir
        except (AttributeError, KeyError, OSError) as exc:
            raise _error("cannot resolve product-owned POSIX root-selection store") from exc
        base = Path(home) / ".local" / "state"
        relative = Path("autosport") / "monotonic-root-selection-v1"
        if not base.is_absolute():
            raise _error("product-owned root-selection store must be absolute")
        return base / relative


_sealed_stable_root_selection_store._autosport_os_location_dispatch_sealed = True
_root.stable_root_selection_store = _sealed_stable_root_selection_store
