from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Callable

from .decision_ledger import EconomicDecisionAuthority
from .live_decision_loop import (
    LiveCycleResult,
    LiveDecisionMode,
    LiveIntentFactory,
    LiveLoopBounds,
    PersistentLiveDecisionLoop,
)
from .paper import PaperBook
from .paper_execution_adoption import PaperExecutionAdoptionRuntime
from .paper_execution_reality import PaperExecutionLedger, PaperExecutionModelConfig
from .scientific_registry import ScientificRegistry


class ProductPaperDecisionCycleError(RuntimeError):
    """The product cannot reconstruct one canonical PAPER decision cycle safely."""


def _canonical_workspace(value: str | Path) -> Path:
    try:
        workspace = Path(value).expanduser().resolve(strict=False)
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise ProductPaperDecisionCycleError(
            "product PAPER decision workspace cannot be resolved"
        ) from exc
    if not workspace.is_dir():
        raise ProductPaperDecisionCycleError(
            "product PAPER decision workspace must already exist"
        )
    return workspace


def _required_file(workspace: Path, name: str) -> Path:
    path = workspace / name
    if not path.is_file():
        raise ProductPaperDecisionCycleError(
            f"canonical product state {name} is missing"
        )
    return path


def _effective_quote_age(
    authority: EconomicDecisionAuthority,
    config: PaperExecutionModelConfig,
) -> timedelta:
    if not isinstance(authority, EconomicDecisionAuthority):
        raise TypeError("authority must be EconomicDecisionAuthority")
    if not isinstance(config, PaperExecutionModelConfig):
        raise TypeError("paper_execution_config must be PaperExecutionModelConfig")

    goal_seconds = authority.contract.max_quote_age_seconds
    model_seconds = Decimal(config.max_quote_age_ms) / Decimal(1000)
    seconds = min(goal_seconds, model_seconds)
    microseconds = int(
        (seconds * Decimal(1_000_000)).to_integral_value(rounding=ROUND_FLOOR)
    )
    if microseconds <= 0:
        raise ProductPaperDecisionCycleError(
            "PAPER decision execution requires a positive effective quote age"
        )
    try:
        return timedelta(microseconds=microseconds)
    except OverflowError as exc:
        raise ProductPaperDecisionCycleError(
            "effective PAPER decision quote age is outside timedelta range"
        ) from exc


def _no_provider_io(_updates: object) -> None:
    """Deliberately perform no acquisition.

    PersistentLiveDecisionLoop reconstructs its mirror from canonical market.db
    before this callback can run. The product collector remains the sole network /
    provider acquisition owner.
    """


def run_product_paper_decision_cycle(
    *,
    workspace: str | Path,
    loop_id: str,
    authority: EconomicDecisionAuthority,
    intent_factory: LiveIntentFactory,
    scientific_registry: ScientificRegistry,
    paper_execution_config: PaperExecutionModelConfig,
    bounds: LiveLoopBounds | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LiveCycleResult:
    """Run exactly one reconstructed PAPER decision cycle over durable product state.

    The continuous product runtime owns collection, lifecycle and settlement. This
    composition seam starts only after those writes have completed for a product
    cycle. It reloads paper_book.json every invocation, reuses the canonical
    decision/scientific/#623 execution authorities, consumes the already-persisted
    market.db view, performs no provider I/O, runs one bounded decision cycle,
    and closes the ephemeral live-loop projection.

    No caller can supply an alternate PaperBook, decision ledger, execution ledger,
    market store or provider through this boundary.
    """

    root = _canonical_workspace(workspace)
    if type(loop_id) is not str or not loop_id or loop_id.strip() != loop_id:
        raise ValueError("loop_id must be a non-empty trimmed string")
    if not callable(intent_factory):
        raise TypeError("intent_factory must be callable")
    if not isinstance(scientific_registry, ScientificRegistry):
        raise TypeError("scientific_registry must be ScientificRegistry")
    if bounds is not None and not isinstance(bounds, LiveLoopBounds):
        raise TypeError("bounds must be LiveLoopBounds or None")
    if clock is not None and not callable(clock):
        raise TypeError("clock must be callable or None")

    paper_book_path = _required_file(root, "paper_book.json")
    _required_file(root, "market.db")
    registry_path = _required_file(root, "scientific_registry.json")
    try:
        supplied_registry_path = scientific_registry.path.resolve(strict=False)
        canonical_registry_path = registry_path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProductPaperDecisionCycleError(
            "scientific registry path cannot be resolved"
        ) from exc
    if supplied_registry_path != canonical_registry_path:
        raise ProductPaperDecisionCycleError(
            "scientific_registry must own the canonical product workspace registry"
        )

    max_quote_age = _effective_quote_age(authority, paper_execution_config)
    try:
        book = PaperBook.load(paper_book_path)
    except (OSError, TypeError, ValueError) as exc:
        raise ProductPaperDecisionCycleError(
            "canonical product PaperBook cannot be verified"
        ) from exc

    execution = PaperExecutionAdoptionRuntime(
        book=book,
        ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
        config=paper_execution_config,
        max_quote_age=max_quote_age,
        paper_book_path=paper_book_path,
    )
    with PersistentLiveDecisionLoop(
        root,
        loop_id=loop_id,
        mode=LiveDecisionMode.PAPER,
        book=book,
        authority=authority,
        intent_factory=intent_factory,
        scientific_registry=scientific_registry,
        paper_execution=execution,
        max_quote_age=max_quote_age,
        bounds=bounds,
        clock=clock,
        observation_runner=_no_provider_io,
    ) as decision_loop:
        return decision_loop.run_cycle()
