from __future__ import annotations

import ast
import inspect

import autosport.betfair_timeout_reconciliation as timeout_resolution


_ELAPSED_PRIMITIVES = {
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
}


def _call_name(node: ast.Call) -> str | None:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _relevant_function_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        lowered = node.name.lower()
        if any(
            token in lowered
            for token in ("timeout", "absence", "capture", "clock", "elapsed", "visibility")
        ):
            names.add(node.name)
    return names


def test_negative_timeout_absence_has_elapsed_clock_authority() -> None:
    """A UTC clock step must not manufacture Betfair's 15-second visibility wait.

    Durable UTC timestamps remain necessary audit evidence, but the provider's
    visibility horizon is an elapsed-time safety condition.  A local wall clock can
    jump forward because of NTP/manual correction/VM resume.  Therefore the
    negative-absence lineage needs a monotonic elapsed-time authority (or an
    explicit clock-discontinuity-safe equivalent), rather than proving the wait
    solely from datetime.now()/ISO timestamps.
    """

    source = inspect.getsource(timeout_resolution)
    tree = ast.parse(source)

    elapsed_calls: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            call_name = _call_name(child)
            if call_name in _ELAPSED_PRIMITIVES:
                elapsed_calls.append((node.name, call_name))

    assert elapsed_calls, (
        "P0 timeout absence is currently wall-clock-only: a forward UTC clock step "
        "can make capture/page timestamps cross timeout+15s without proving 15 seconds "
        "actually elapsed. Bind negative absence to a monotonic elapsed-time authority "
        "or an equally explicit discontinuity-safe clock guard; keep UTC timestamps "
        "for durable audit and keep positive provider presence eager."
    )

    relevant = _relevant_function_names(tree)
    assert any(function_name in relevant for function_name, _ in elapsed_calls), (
        "An elapsed-clock primitive exists somewhere in the module but is not used "
        "inside the timeout/absence/capture/visibility authority. The negative "
        "Betfair timeout decision must consume the elapsed-time guard itself."
    )


def test_wall_clock_timestamp_is_not_the_only_capture_start_authority() -> None:
    """Mechanize the exact current regression instead of accepting a dead import."""

    source = inspect.getsource(timeout_resolution)
    tree = ast.parse(source)

    capture_installer = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_betfair_readback_capture_start_authority"
        ),
        None,
    )
    assert capture_installer is not None

    utc_now_calls = [
        node
        for node in ast.walk(capture_installer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "now"
    ]
    assert utc_now_calls, (
        "The canonical capture-start lineage changed shape; requalify this falsifier "
        "against the exact parent before disposition."
    )

    elapsed_calls = [
        node
        for node in ast.walk(capture_installer)
        if isinstance(node, ast.Call) and _call_name(node) in _ELAPSED_PRIMITIVES
    ]
    assert elapsed_calls, (
        "capture_started_at is sealed from wall-clock datetime.now() only. A forward "
        "system-clock correction can satisfy the +15s comparison prematurely. Seal "
        "a monotonic/elapsed companion at capture start (and conservatively handle "
        "restart when no comparable monotonic ancestry survives)."
    )
