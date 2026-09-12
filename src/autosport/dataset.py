from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .domain import MarketEvent


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    root: Path
    name: str
    sport: str
    market_path: Path
    results_path: Path
    market_sha256: str
    results_sha256: str

    def load_market_events(self) -> list[MarketEvent]:
        events: list[MarketEvent] = []
        with self.market_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(MarketEvent.from_dict(json.loads(line)))
        return events

    def load_results_after_replay(self) -> dict[str, str]:
        raw = json.loads(self.results_path.read_text(encoding="utf-8"))
        if int(raw.get("schema_version", 0)) != 1:
            raise ValueError("unsupported results schema")
        outcomes = raw.get("quote_outcomes")
        if not isinstance(outcomes, dict):
            raise ValueError("quote_outcomes must be an object")
        return {str(key): str(value) for key, value in outcomes.items()}


def load_dataset(root: str | Path) -> ReplayDataset:
    root = Path(root)
    manifest_path = root / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError("unsupported dataset schema")
    market_path = root / str(raw["market_file"])
    results_path = root / str(raw["results_file"])
    if market_path.resolve() == results_path.resolve():
        raise ValueError("market and results files must remain physically separate")
    expected_market = str(raw["market_sha256"])
    expected_results = str(raw["results_sha256"])
    actual_market = _sha256(market_path)
    actual_results = _sha256(results_path)
    if actual_market != expected_market:
        raise ValueError("market dataset hash mismatch")
    if actual_results != expected_results:
        raise ValueError("sealed results hash mismatch")
    return ReplayDataset(
        root=root,
        name=str(raw.get("name", root.name)),
        sport=str(raw.get("sport", "unknown")),
        market_path=market_path,
        results_path=results_path,
        market_sha256=actual_market,
        results_sha256=actual_results,
    )
