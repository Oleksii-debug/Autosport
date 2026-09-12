from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class RepeatedExperimentError(RuntimeError):
    pass


class UnresolvedExperimentError(RuntimeError):
    pass


class RunRegistry:
    """Fail-closed experiment ledger preventing accidental replay duplication after restart/crash."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"schema_version": 1, "runs": {}})

    @staticmethod
    def experiment_identity(market_sha256: str, results_sha256: str, strategy_id: str) -> str:
        canonical = f"{market_sha256}|{results_sha256}|{strategy_id}".encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def begin(
        self,
        market_sha256: str,
        results_sha256: str,
        strategy_id: str,
        run_id: str,
        allow_repeat: bool = False,
    ) -> str:
        state = self._read()
        base_identity = self.experiment_identity(market_sha256, results_sha256, strategy_id)
        existing = [item for item in state["runs"].values() if item.get("base_identity") == base_identity]
        unresolved = [item for item in existing if item.get("status") == "in_progress"]
        if unresolved:
            raise UnresolvedExperimentError(
                "An earlier run of this dataset/strategy is unresolved; use a new workspace or repair the unresolved run before replaying."
            )
        completed = [item for item in existing if item.get("status") == "completed"]
        if completed and not allow_repeat:
            raise RepeatedExperimentError(
                "This dataset/strategy already completed in this workspace. Explicit allow_repeat is required for another experiment."
            )
        key = base_identity if not completed else f"{base_identity}:repeat:{run_id}"
        state["runs"][key] = {
            "base_identity": base_identity,
            "run_id": run_id,
            "market_sha256": market_sha256,
            "results_sha256": results_sha256,
            "strategy_id": strategy_id,
            "status": "in_progress",
        }
        self._write(state)
        return key

    def complete(self, key: str, result_path: str | None = None) -> None:
        state = self._read()
        item = state["runs"].get(key)
        if item is None:
            raise KeyError(key)
        if item.get("status") != "in_progress":
            raise ValueError("run is not in progress")
        item["status"] = "completed"
        item["result_path"] = result_path
        self._write(state)

    def _read(self) -> dict:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or not isinstance(raw.get("runs"), dict):
            raise ValueError("invalid run registry")
        return raw

    def _write(self, raw: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
