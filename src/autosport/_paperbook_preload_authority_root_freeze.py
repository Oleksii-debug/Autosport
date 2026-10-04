"""Freeze PaperBook's reuse of the existing PAPER authority-root selector.

This module does not create a root, witness format, or persistence authority.  The
PaperBook witness guard intentionally reuses ``_paper_execution_anti_rollback``'s
canonical authority-root policy.  Its original implementation resolves paths through
``Path.resolve()``, whose Python 3.11 implementation can traverse the mutable public
``posixpath.realpath -> abspath`` function graph after the PaperBook member freeze.

Compose the *same* selector semantics into the PaperBook guard before its positive
load/save graph is frozen, but resolve paths through the already-detached ``realpath``
member installed by ``_paperbook_preload_module_member_freeze``.  The replacement is
materialized with the owning guard globals so the subsequent load-dispatch freeze can
clone and witness it as part of one canonical persistence graph.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import FunctionType

from . import _paper_execution_anti_rollback as _anti_rollback
from . import _paperbook_preload_authority_guard as _guard


def _frozen_authority_root(ledger_path):
    configured = _PBA_ENVIRON.get(_PBA_WITNESS_DIR_ENV)
    if configured:
        root = _PBA_PATH(configured).expanduser()
    elif _PBA_OS_NAME == "nt":
        base = _PBA_ENVIRON.get("LOCALAPPDATA")
        root = (
            _PBA_PATH(base)
            if base
            else _PBA_PATH.home() / "AppData" / "Local"
        ) / "Autosport" / "state" / "paper-execution-reality"
    else:
        base = _PBA_ENVIRON.get("XDG_STATE_HOME")
        root = (
            _PBA_PATH(base).expanduser()
            if base
            else _PBA_PATH.home() / ".local" / "state"
        ) / "autosport" / "paper-execution-reality"

    try:
        # _PBA_REALPATH is the detached posixpath/ntpath function graph already
        # installed by the canonical PaperBook module-member freeze.  Unlike
        # Path.resolve() on Python 3.11, it cannot reach a later same-object
        # mutation of the public os.path.abspath function.
        root = _PBA_PATH(_PBA_REALPATH(_PBA_FSPATH(root)))
        workspace_path = _PBA_PATH(ledger_path).expanduser()
        workspace = _PBA_PATH(
            _PBA_REALPATH(_PBA_FSPATH(workspace_path))
        ).parent
    except OSError as exc:
        raise _PBA_ERROR(
            "cannot resolve independent PAPER execution monotonic authority"
        ) from exc

    try:
        root.relative_to(workspace)
    except ValueError:
        within_workspace = False
    else:
        within_workspace = True
    if root == workspace or within_workspace:
        raise _PBA_ERROR(
            "PAPER execution monotonic authority must resolve outside ledger workspace"
        )
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise _PBA_ERROR(
            "cannot establish independent PAPER execution monotonic authority"
        ) from exc
    return root


def _install() -> None:
    namespace = _guard.__dict__
    if namespace.get("_autosport_paperbook_authority_root_freeze_installed", False):
        return

    if namespace.get("_paper_authority_root") is not _anti_rollback._authority_root:
        raise RuntimeError(
            "canonical PaperBook PAPER authority-root selector changed before freeze"
        )

    # These two callables were detached from the public os.path module function graph
    # by _paperbook_preload_module_member_freeze immediately before this composition.
    frozen_realpath = _guard.os.path.realpath
    frozen_fspath = _guard.os.fspath
    if type(frozen_realpath) is not FunctionType:
        raise RuntimeError("canonical frozen PaperBook realpath authority is unavailable")

    namespace["_PBA_ENVIRON"] = os.environ
    namespace["_PBA_WITNESS_DIR_ENV"] = _anti_rollback._WITNESS_DIR_ENV
    namespace["_PBA_OS_NAME"] = os.name
    namespace["_PBA_PATH"] = Path
    namespace["_PBA_REALPATH"] = frozen_realpath
    namespace["_PBA_FSPATH"] = frozen_fspath
    namespace["_PBA_ERROR"] = _anti_rollback._impl.PaperExecutionIntegrityError

    # The downstream canonical load-dispatch freezer indexes same-globals functions
    # by their owning namespace key and then resolves the root selector by __name__.
    # Keep both identities exactly `_paper_authority_root`; using the legacy external
    # name `_authority_root` would make the already-fail-closed clone contract reject
    # this composition before any tests execute.
    replacement = FunctionType(
        _frozen_authority_root.__code__,
        namespace,
        name="_paper_authority_root",
        argdefs=_frozen_authority_root.__defaults__,
        closure=_frozen_authority_root.__closure__,
    )
    replacement.__qualname__ = "_paper_authority_root"
    replacement.__doc__ = _anti_rollback._authority_root.__doc__
    namespace["_paper_authority_root"] = replacement
    namespace["_autosport_paperbook_authority_root_freeze_installed"] = True


_install()
del _install
