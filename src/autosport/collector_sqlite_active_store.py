from __future__ import annotations

from pathlib import Path

from .collector_sqlite_store import CollectorDeltaStore as _SQLiteCollectorDeltaStore


_SQLITE_HEADER = b"SQLite format 3\x00"


class CollectorDeltaStore(_SQLiteCollectorDeltaStore):
    """Keep active-store detection O(1) in retained archive size."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                with self.path.open("rb") as handle:
                    prefix = handle.read(len(_SQLITE_HEADER))
            except OSError as exc:
                raise ValueError("invalid causal collector store") from exc
            if prefix == _SQLITE_HEADER:
                self._verify_sqlite_schema()
            else:
                self._migrate_legacy_json()
        else:
            self._initialize_sqlite(self.path, wal=False)
        self._activate_wal()
        self._verify_sqlite_schema()
