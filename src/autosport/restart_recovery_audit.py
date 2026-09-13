from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .dataset import ReplayDataset
from .integrity import ensure_durable_file, sha256_file
from .paper import PaperBook
from .run_registry import RunRegistry
from .run_transaction import RunTransaction
from .session import AutosportSession


_MARKET = (
    '{"event_id":"restart-audit-1","market_id":"winner","selection_id":"player-a",'
    '"decimal_odds":"1.62","observed_ts":"2026-09-12T10:00:00+00:00",'
    '"source_id":"restart-audit","sequence":1,"market_type":"winner","score_state":"0-0",'
    '"metadata":{"paper_signal":true,"paper_signal_id":"restart-audit-signal"}}\n'
    '{"event_id":"restart-audit-1","market_id":"winner","selection_id":"player-b",'
    '"decimal_odds":"2.30","observed_ts":"2026-09-12T10:00:00+00:00",'
    '"source_id":"restart-audit","sequence":2,"market_type":"winner","score_state":"0-0"}\n'
)
_RESULTS = (
    '{"quote_outcomes":{"restart-audit-1|winner|player-a":"win",'
    '"restart-audit-1|winner|player-b":"loss"},"schema_version":1}\n'
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_dataset(root: Path) -> ReplayDataset:
    root.mkdir(parents=True, exist_ok=True)
    market = root / "market.jsonl"
    results = root / "results.json"
    market.write_text(_MARKET, encoding="utf-8")
    results.write_text(_RESULTS, encoding="utf-8")
    return ReplayDataset(
        root=root,
        name="packaged-restart-audit",
        sport="table_tennis",
        market_path=market,
        results_path=results,
        market_sha256=_sha256(market),
        results_sha256=_sha256(results),
    )


def _audit_session_restart(root: Path) -> dict[str, object]:
    dataset = _fixture_dataset(root / "dataset")
    workspace = root / "session-workspace"

    first = AutosportSession(workspace, initial_bankroll="100", strategy_id="baseline-v1")
    try:
        result = first.run_dataset(dataset)
        if not result.settled_ticket_ids:
            raise RuntimeError("restart audit did not produce a settled paper ticket")
        balance = result.balance
        ticket_ids = tuple(sorted(first.book.tickets))
        if not ticket_ids:
            raise RuntimeError("restart audit paper ticket did not persist in memory")
    finally:
        first.close()

    paper_path = workspace / "paper_book.json"
    ledger_path = workspace / "decisions.jsonl"
    book_hash_before = sha256_file(paper_path)
    ledger_hash_before = sha256_file(ledger_path)

    reopened = AutosportSession(workspace, initial_bankroll="1", strategy_id="baseline-v1")
    try:
        if reopened.book.balance != balance:
            raise RuntimeError("PaperBook balance changed across restart")
        if tuple(sorted(reopened.book.tickets)) != ticket_ids:
            raise RuntimeError("PaperBook ticket identity changed across restart")
        if reopened.registry.in_progress():
            raise RuntimeError("completed paper run became unresolved after restart")
        if not reopened.store.current():
            raise RuntimeError("MarketStore did not survive restart")
    finally:
        reopened.close()

    book_hash_after = sha256_file(paper_path)
    ledger_hash_after = sha256_file(ledger_path)
    if book_hash_after != book_hash_before:
        raise RuntimeError("PaperBook hash changed during read-only restart")
    if ledger_hash_after != ledger_hash_before:
        raise RuntimeError("Decision Ledger hash changed during read-only restart")

    return {
        "status": "PASS",
        "balance": str(balance),
        "ticket_count": len(ticket_ids),
        "paper_book_sha256": book_hash_after,
        "decision_ledger_sha256": ledger_hash_after,
    }


def _audit_uncommitted_recovery(root: Path) -> dict[str, object]:
    workspace = root / "recovery-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    paper_path = workspace / "paper_book.json"
    ledger_path = workspace / "decisions.jsonl"

    PaperBook("100").save(paper_path)
    ensure_durable_file(ledger_path)
    book_hash = sha256_file(paper_path)
    ledger_hash = sha256_file(ledger_path)

    registry = RunRegistry(workspace / "run_registry.json")
    run_id = "packaged-restart-recovery-audit"
    market_sha = "a" * 64
    results_sha = "b" * 64
    strategy_id = "baseline-v1"
    experiment_key = registry.begin(
        market_sha,
        results_sha,
        strategy_id,
        run_id,
        base_paper_book_sha256=book_hash,
        base_decision_ledger_sha256=ledger_hash,
    )
    tx = RunTransaction.start(
        workspace,
        run_id=run_id,
        experiment_key=experiment_key,
        market_sha256=market_sha,
        results_sha256=results_sha,
        strategy_id=strategy_id,
        base_paper_book_sha256=book_hash,
        base_decision_ledger_sha256=ledger_hash,
    )

    recovered = RunTransaction.recover(
        workspace,
        run_id=run_id,
        registry_item=registry.get(experiment_key),
        experiment_key=experiment_key,
    )
    if recovered.disposition != "aborted_uncommitted":
        raise RuntimeError("staging recovery did not fail closed as aborted_uncommitted")

    registry.abort_uncommitted(
        experiment_key,
        reason="packaged restart/recovery audit",
        paper_book_sha256=sha256_file(paper_path),
        decision_ledger_sha256=sha256_file(ledger_path),
    )
    if registry.in_progress():
        raise RuntimeError("recovered registry remains unresolved")
    if registry.get(experiment_key).get("status") != "aborted":
        raise RuntimeError("recovered registry did not record aborted state")
    manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
    if manifest.get("phase") != "aborted":
        raise RuntimeError("recovered transaction manifest did not record aborted phase")
    if sha256_file(paper_path) != book_hash or sha256_file(ledger_path) != ledger_hash:
        raise RuntimeError("recovery changed canonical economic BASE state")

    return {
        "status": "PASS",
        "disposition": recovered.disposition,
        "paper_book_sha256": book_hash,
        "decision_ledger_sha256": ledger_hash,
    }


def run_restart_recovery_audit(output_path: str | Path) -> int:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restart = _audit_session_restart(root)
            recovery = _audit_uncommitted_recovery(root)
        payload = {
            "status": "PASS",
            "session_restart_status": restart["status"],
            "transaction_recovery_status": recovery["status"],
            "recovery_disposition": recovery["disposition"],
            "persistent_balance": restart["balance"],
            "ticket_count": restart["ticket_count"],
            "paper_book_sha256": restart["paper_book_sha256"],
            "decision_ledger_sha256": restart["decision_ledger_sha256"],
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    except Exception as exc:
        payload = {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 1
