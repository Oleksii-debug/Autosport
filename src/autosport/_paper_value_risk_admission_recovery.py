from __future__ import annotations

from pathlib import Path

from . import _paper_value_execution_authority as _authority
from .decision_ledger import GENERAL_DECISION_KIND, DecisionRecord, JsonlDecisionLedger
from .integrity import atomic_write_json
from .json_integrity import strict_json_loads
from .paper import PaperBook
from .paper_execution_adoption import PaperExecutionAdoptionError, PaperExecutionAdoptionRuntime
from .paper_strategy import PaperValueAgent


_PREPARE_SCHEMA = "autosport.paper_value.general_risk_admission.prepare"
_PREPARE_SCHEMA_VERSION = 1
_PREPARE_FIELDS = frozenset(
    {
        "schema",
        "schema_version",
        "witness",
        "prepare_sha256",
    }
)
_ORIGINAL_VERIFY = _authority._verify_general_risk_admission


def _prepare_path(witness_path: Path) -> Path:
    return witness_path.with_name(f"{witness_path.stem}.prepare.json")


def _prepare_payload(witness: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": _PREPARE_SCHEMA,
        "schema_version": _PREPARE_SCHEMA_VERSION,
        "witness": witness,
    }
    payload["prepare_sha256"] = _authority._canonical_payload_sha256(payload)
    return payload


def _load_prepare(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise PaperExecutionAdoptionError(
            "paper-value risk admission PREPARE path is not canonical"
        )
    try:
        raw = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PaperExecutionAdoptionError(
            "durable GENERAL paper-value action lacks canonical risk admission PREPARE"
        ) from exc
    if type(raw) is not dict or set(raw) != _PREPARE_FIELDS:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission PREPARE schema is invalid"
        )
    prepare_sha256 = raw.get("prepare_sha256")
    unsigned = dict(raw)
    unsigned.pop("prepare_sha256", None)
    if (
        raw.get("schema") != _PREPARE_SCHEMA
        or raw.get("schema_version") != _PREPARE_SCHEMA_VERSION
        or type(prepare_sha256) is not str
        or prepare_sha256 != _authority._canonical_payload_sha256(unsigned)
        or type(raw.get("witness")) is not dict
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission PREPARE binding is invalid"
        )
    return raw


def _validate_record_binding(
    *,
    agent: PaperValueAgent,
    context,
    record: DecisionRecord,
    descriptor: _authority.PaperValueExecutionDescriptor,
    expected_run_id: str,
) -> None:
    action = descriptor.execution_plan.actions[0] if len(descriptor.execution_plan.actions) == 1 else None
    if (
        action is None
        or record.decision_kind != GENERAL_DECISION_KIND
        or record.replay_run_id != context.replay_run_id
        or record.agent != PaperValueAgent.name
        or record.action != _authority._PAPER_VALUE_ACTION
        or record.payload.get("material_action_id") != record.decision_id
        or record.payload.get("requested_stake") != str(action.requested_stake)
        or record.payload.get("execution_plan_id") != descriptor.execution_plan.plan_id
        or record.payload.get("execution_plan_fingerprint") != descriptor.execution_plan.fingerprint
        or record.payload.get("execution_run_id") != expected_run_id
        or record.payload.get("execution_authority_json") != descriptor.intent_evidence_json
        or not isinstance(agent.risk_policy, _authority.PaperRiskPolicy)
        or agent.risk_policy.economic_goal is not None
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission decision binding is invalid"
        )


def _expected_witness(
    *,
    agent: PaperValueAgent,
    record: DecisionRecord,
    descriptor: _authority.PaperValueExecutionDescriptor,
    expected_run_id: str,
    pre_action_sha256: str,
) -> dict[str, object]:
    return _authority._risk_admission_payload(
        record=record,
        descriptor=descriptor,
        risk_policy=agent.risk_policy,
        execution_run_id=expected_run_id,
        pre_action_book_sha256=pre_action_sha256,
    )


def _write_commit(path: Path, witness: dict[str, object]) -> None:
    """One injectable atomic boundary used by crash/restart regression tests."""
    atomic_write_json(path, witness)


def _ensure_pre_action(
    *,
    agent: PaperValueAgent,
    context,
    runtime: PaperExecutionAdoptionRuntime,
    pre_action_path: Path,
    expected_sha256: str,
) -> PaperBook:
    if pre_action_path.is_symlink():
        raise PaperExecutionAdoptionError(
            "paper-value risk admission pre-action path is not canonical"
        )
    if pre_action_path.exists():
        try:
            persisted = PaperBook.load(pre_action_path)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission pre-action PaperBook is unreadable"
            ) from exc
    else:
        current_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(context.paper_book)
        if current_sha256 != expected_sha256:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission PREPARE no longer matches current PaperBook"
            )
        context.paper_book.save(pre_action_path)
        try:
            persisted = PaperBook.load(pre_action_path)
        except (OSError, TypeError, ValueError) as exc:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission pre-action PaperBook is unreadable"
            ) from exc

    persisted_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(persisted)
    if (
        persisted_sha256 != expected_sha256
        or not runtime._same_book_state(context.paper_book, persisted)
    ):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission conflicts with existing pre-action state"
        )
    return persisted


def _require_pre_action_risk_pass(
    *,
    agent: PaperValueAgent,
    pre_action_book: PaperBook,
    descriptor: _authority.PaperValueExecutionDescriptor,
) -> None:
    """Re-run the canonical GENERAL risk gate on the exact durable pre-action book."""
    if len(descriptor.execution_plan.actions) != 1:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission requires exactly one execution action"
        )
    action = descriptor.execution_plan.actions[0]
    risk = agent.risk_policy.evaluate(
        pre_action_book,
        action.requested_stake,
    )
    if getattr(risk, "allowed", None) is not True:
        raise PaperExecutionAdoptionError(
            "durable GENERAL paper-value risk admission no longer passes canonical risk evaluation"
        )


def _issue_general_risk_admission(
    *,
    agent: PaperValueAgent,
    context,
    descriptor: _authority.PaperValueExecutionDescriptor,
    decision_id: str,
    started_at: str,
) -> None:
    """Publish PREPARE -> pre-action snapshot -> COMMIT as a recoverable transaction."""
    ledger = context.decision_ledger
    runtime = context.paper_execution
    if not isinstance(ledger, JsonlDecisionLedger) or not isinstance(runtime, PaperExecutionAdoptionRuntime):
        raise PaperExecutionAdoptionError(
            "paper-value risk admission requires canonical runtime authority"
        )
    matches = tuple(record for record in ledger.verified_records() if record.decision_id == decision_id)
    if len(matches) != 1:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission requires one exact durable decision"
        )
    record = matches[0]
    expected_run_id = runtime.expected_run_id(descriptor, decision_id)
    if record.observed_ts != started_at:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission decision binding is invalid"
        )
    _validate_record_binding(
        agent=agent,
        context=context,
        record=record,
        descriptor=descriptor,
        expected_run_id=expected_run_id,
    )

    # The initial strategy risk result predates the durable decision append. A
    # concurrent/simultaneous paper allocation can therefore change exposure in
    # that window. Re-run the canonical policy on the exact current book and bind
    # that PASS to one digest before publishing any risk-admission evidence.
    pre_action_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(context.paper_book)
    if pre_action_sha256 is None:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission cannot validate pre-action PaperBook"
        )
    _require_pre_action_risk_pass(
        agent=agent,
        pre_action_book=context.paper_book,
        descriptor=descriptor,
    )
    validated_sha256 = agent.risk_policy.risk_of_ruin_portfolio_sha256(context.paper_book)
    if validated_sha256 != pre_action_sha256:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission PaperBook changed during canonical risk evaluation"
        )
    witness = _expected_witness(
        agent=agent,
        record=record,
        descriptor=descriptor,
        expected_run_id=expected_run_id,
        pre_action_sha256=pre_action_sha256,
    )
    witness_path, pre_action_path = _authority._risk_admission_paths(ledger, decision_id)
    prepare_path = _prepare_path(witness_path)
    witness_path.parent.mkdir(parents=True, exist_ok=True)
    if witness_path.is_symlink() or prepare_path.is_symlink():
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness path is not canonical"
        )

    expected_prepare = _prepare_payload(witness)
    if prepare_path.exists():
        if _load_prepare(prepare_path) != expected_prepare:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission conflicts with existing PREPARE"
            )
    else:
        atomic_write_json(prepare_path, expected_prepare)
        if _load_prepare(prepare_path) != expected_prepare:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission PREPARE did not persist exactly"
            )

    _ensure_pre_action(
        agent=agent,
        context=context,
        runtime=runtime,
        pre_action_path=pre_action_path,
        expected_sha256=pre_action_sha256,
    )

    if witness_path.exists():
        existing, _ = _authority._load_general_risk_admission(ledger, decision_id)
        if existing != witness:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission conflicts with existing witness"
            )
        return
    _write_commit(witness_path, witness)
    verified, _ = _authority._load_general_risk_admission(ledger, decision_id)
    if verified != witness:
        raise PaperExecutionAdoptionError(
            "paper-value risk admission witness did not persist exactly"
        )


def _verify_general_risk_admission(
    *,
    agent: PaperValueAgent,
    context,
    record: DecisionRecord,
    descriptor: _authority.PaperValueExecutionDescriptor,
    expected_run_id: str,
) -> None:
    ledger = context.decision_ledger
    runtime = context.paper_execution
    if not isinstance(ledger, JsonlDecisionLedger) or not isinstance(runtime, PaperExecutionAdoptionRuntime):
        raise PaperExecutionAdoptionError(
            "durable GENERAL paper-value recovery lacks canonical runtime authority"
        )
    _validate_record_binding(
        agent=agent,
        context=context,
        record=record,
        descriptor=descriptor,
        expected_run_id=expected_run_id,
    )
    witness_path, pre_action_path = _authority._risk_admission_paths(ledger, record.decision_id)
    prepare_path = _prepare_path(witness_path)

    if not witness_path.exists() or not pre_action_path.exists():
        if not prepare_path.exists():
            raise PaperExecutionAdoptionError(
                "durable GENERAL paper-value action lacks canonical risk admission witness"
            )
        prepare = _load_prepare(prepare_path)
        prepared_witness = prepare["witness"]
        assert isinstance(prepared_witness, dict)
        pre_action_sha256 = prepared_witness.get("pre_action_book_sha256")
        if type(pre_action_sha256) is not str:
            raise PaperExecutionAdoptionError(
                "paper-value risk admission PREPARE lacks pre-action digest"
            )
        expected = _expected_witness(
            agent=agent,
            record=record,
            descriptor=descriptor,
            expected_run_id=expected_run_id,
            pre_action_sha256=pre_action_sha256,
        )
        if prepare != _prepare_payload(expected):
            raise PaperExecutionAdoptionError(
                "paper-value risk admission PREPARE binding changed across restart"
            )
        if _authority._run_reserved(runtime, record.decision_id, run_id=expected_run_id):
            raise PaperExecutionAdoptionError(
                "incomplete paper-value risk admission cannot follow execution reservation"
            )
        pre_action_book = _ensure_pre_action(
            agent=agent,
            context=context,
            runtime=runtime,
            pre_action_path=pre_action_path,
            expected_sha256=pre_action_sha256,
        )
        _require_pre_action_risk_pass(
            agent=agent,
            pre_action_book=pre_action_book,
            descriptor=descriptor,
        )
        if witness_path.exists():
            existing, _ = _authority._load_general_risk_admission(ledger, record.decision_id)
            if existing != expected:
                raise PaperExecutionAdoptionError(
                    "paper-value risk admission conflicts with recovered COMMIT"
                )
        else:
            _write_commit(witness_path, expected)
    else:
        _, pre_action_book = _authority._load_general_risk_admission(
            ledger,
            record.decision_id,
        )
        _require_pre_action_risk_pass(
            agent=agent,
            pre_action_book=pre_action_book,
            descriptor=descriptor,
        )

    _ORIGINAL_VERIFY(
        agent=agent,
        context=context,
        record=record,
        descriptor=descriptor,
        expected_run_id=expected_run_id,
    )


def _install() -> None:
    if getattr(_authority, "_autosport_risk_admission_recovery_installed", False):
        return
    _authority._issue_general_risk_admission = _issue_general_risk_admission
    _authority._verify_general_risk_admission = _verify_general_risk_admission
    _authority._autosport_risk_admission_recovery_installed = True


_install()
