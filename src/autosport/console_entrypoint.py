from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Route public command entrypoints without bypassing Windows product UI contracts."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["gui"]:
        from .windows_gui import main as windows_gui_main

        return windows_gui_main()

    from .cli import main as cli_main

    return cli_main(args)
