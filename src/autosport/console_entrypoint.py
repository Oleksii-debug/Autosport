from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Route public commands while preserving the canonical Windows product entry."""

    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["gui"]:
        # The public router owns and consumes the "gui" token.  Reuse the product-owned
        # Windows entry with an explicit empty argv so it enters its interactive
        # WebView2 path instead of re-reading sys.argv and rejecting "gui" as an
        # unknown packaged machine mode.
        from .windows_entry import main as windows_entry_main

        return windows_entry_main([])

    from .cli import main as cli_main

    return cli_main(args)
