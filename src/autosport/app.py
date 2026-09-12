from __future__ import annotations

from pathlib import Path


def main() -> int:
    try:
        import webview
    except ImportError as exc:
        raise SystemExit(
            "Windows UI dependency is not installed. Run: pip install -e '.[windows]'"
        ) from exc

    repo_root = Path(__file__).resolve().parents[2]
    index = repo_root / "web" / "index.html"
    if not index.is_file():
        raise SystemExit(f"Autosport UI not found: {index}")

    webview.create_window(
        "Автоспорт",
        str(index),
        width=1200,
        height=800,
        min_size=(760, 520),
    )
    webview.start(http_server=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
