"""Additional fail-closed status regression cases for Plan 6 atomic closure."""
import importlib.util
from pathlib import Path
import pytest

_ORIGINAL = Path(__file__).with_name("test_check_atomic_closure_authority.py")
_spec = importlib.util.spec_from_file_location("atomic_base_cases", _ORIGINAL)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
module = _base.module
REGISTRY = _base.REGISTRY
ATOMIC = _base.ATOMIC
rejected = _base.rejected


# r55: explicit negative status phrases in Ukrainian and lowercase English
# must never turn a stale leading DONE into terminal closure authority.
@pytest.mark.parametrize("secondary", [
    "не виконано", "невиконано", "скасовано", "відхилено",
    "не підтверджено", "потребує перевірки", "чекає злиття",
    "очікує прийняття", "у процесі", "немає доказів", "не готово",
    "not implemented", "not merged", "not verified", "not ready",
    "needs tests", "needs review", "awaiting review", "pending merge",
    "unverified", "rejected", "broken", "не‑виконано",
])
def test_r55_negative_bare_status_never_terminal(tmp_path, secondary):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — {secondary} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


def test_r55_positive_done_description_not_overrejected():
    assert module._is_terminal_done("DONE — інтегровано та перевірено")
    assert module._is_terminal_done("DONE — merged and checked")
    assert module._is_terminal_done("DONE — не містить секретів; інтегровано")

# r58: a stale DONE followed by a later explicit negative qualifier is not
# terminal even when the qualifier is not the first token of the description.
@pytest.mark.parametrize('qualifier', [
    'інтегровано, але не перевірено',
    'інтегровано; не виконано',
    'інтегровано (не підтверджено)',
    'інтегровано — скасовано',
    'інтегровано: чекає злиття',
    'інтегровано, потребує перевірки',
    'інтегровано, але не готово',
    'merged; not verified',
    'merged (not implemented)',
    'merged, pending review',
    'merged; awaiting tests',
    'merged, but not accepted',
    'merged — rejected',
    'merged: unverified',
    'merged; needs evidence',
    'merged; NOT‑VERIFIED',
])
def test_r58_trailing_explicit_negative_cannot_certify_done(tmp_path, qualifier):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — {qualifier} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


@pytest.mark.parametrize('description', [
    'DONE — інтегровано та перевірено',
    'DONE — не містить секретів; інтегровано',
    'DONE — merged and checked',
    'DONE — not leaking secrets; fully integrated',
    'DONE — інтегровано (усі перевірки успішні)',
])
def test_r58_positive_terminal_description_stays_accepted(description):
    assert module._is_terminal_done(description)


# r59: conjunctions and list separators can also reverse an apparent DONE.
# These are semantic negations, not narrative claims of accepted integration.
@pytest.mark.parametrize("joiner", [" та ", " and ", " / ", " · "])
@pytest.mark.parametrize("negative", [
    "not verified", "не перевірено", "не підтверджено", "pending review",
    "needs tests", "скасовано", "not accepted", "not merged",
    "awaiting tests", "not implemented",
])
def test_r59_conjoined_negative_status_rejects_done(joiner, negative):
    assert not module._is_terminal_done("DONE — merged" + joiner + negative)


@pytest.mark.parametrize("positive", [
    "DONE — merged and verified",
    "DONE — інтегровано та перевірено",
    "DONE — not leaking secrets and fully integrated",
    "DONE — merged / verified",
    "DONE — інтегровано · перевірено",
])
def test_r59_conjunction_positive_descriptions_remain_done(positive):
    assert module._is_terminal_done(positive)

# r67: an adjacent negative completion qualifier has no punctuation or
# conjunction, but still negates a preceding completed-action claim.  A
# source-authority preflight must not treat these as terminal acceptance.
@pytest.mark.parametrize('contradictory', [
    'DONE — merged not verified',
    'DONE — merged not accepted',
    'DONE — merged not merged',
    'DONE — merged pending review',
    'DONE — merged awaiting tests',
    'DONE — merged needs evidence',
    'DONE — інтегровано не перевірено',
    'DONE — інтегровано не підтверджено',
    'DONE — інтегровано очікує перевірки',
    'DONE — прийнято не завершено',
])
def test_r67_adjacent_negative_qualifier_cannot_certify_done(contradictory):
    assert not module._is_terminal_done(contradictory)


@pytest.mark.parametrize('valid', [
    'DONE — merged and verified',
    'DONE — інтегровано та перевірено',
    'DONE — merged not leaking secrets',
    'DONE — прийнято після WAITING_UPSTREAM',
])
def test_r67_positive_description_remains_terminal(valid):
    assert module._is_terminal_done(valid)

@pytest.mark.parametrize('qualifier', [
    'merged not verified',
    'merged pending review',
    'інтегровано не перевірено',
])
def test_r67_adjacent_negative_is_rejected_in_resolved_plan_tree(tmp_path, qualifier):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — {qualifier} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


# r68: a positive merge verb cannot cancel an adjacent failure/pending check.
# This is a current-state contradiction, not a historical repaired failure.
@pytest.mark.parametrize('contradictory', [
    'DONE — merged; tests failed',
    'DONE — merged but CI failed',
    'DONE — merged, Windows CI failure',
    'DONE — merged; tests pending',
    'DONE — merged tests pending',
    'DONE — merged, Windows Candidate failed',
    'DONE — integrated build failure',
    'DONE — інтегровано але тести провалено',
    'DONE — злито; тести не пройдено',
    'DONE — прийнято, перевірки очікують',
])
def test_r68_adjacent_check_failure_is_not_done(contradictory):
    assert not module._is_terminal_done(contradictory)

@pytest.mark.parametrize('positive', [
    'DONE — merged; tests passed',
    'DONE — merged; Windows CI passed',
    'DONE — merged after previously failed CI was repaired',
    'DONE — інтегровано, тести пройдено',
])
def test_r68_positive_or_repaired_check_is_done(positive):
    assert module._is_terminal_done(positive)

@pytest.mark.parametrize('negative', [
    'merged; tests failed',
    'merged but CI failed',
    'інтегровано але тести провалено',
])
def test_r68_check_failure_rejected_in_resolved_plan_tree(tmp_path, negative):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — {negative} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

# r69: CI/Windows/test gate labels often use a colon, equals sign or an
# em dash.  A failed or skipped gate cannot be converted into a DONE claim
# by inserting that separator between the gate name and its outcome.
@pytest.mark.parametrize('contradictory', [
    'DONE — merged; CI: FAILED',
    'DONE — merged; CI = FAILURE',
    'DONE — merged; CI — PENDING',
    'DONE — merged; CI: SKIPPED',
    'DONE — merged; Windows CI: CANCELLED',
    'DONE — merged; Windows Candidate: FAILURE',
    'DONE — merged; Windows Candidate = SKIPPED',
    'DONE — merged; build: TIMED_OUT',
    'DONE — merged; tests: FAILED',
    'DONE — merged; tests = PENDING',
    'DONE — інтегровано; тести: НЕ ПРОЙДЕНО',
    'DONE — прийнято; перевірки — ОЧІКУЮТЬ',
])
def test_r69_explicit_failed_gate_separator_not_done(contradictory):
    assert not module._is_terminal_done(contradictory)


@pytest.mark.parametrize('positive', [
    'DONE — merged; CI: SUCCESS',
    'DONE — merged; Windows Candidate: SUCCESS',
    'DONE — merged; tests: PASSED',
    'DONE — інтегровано; тести: ПРОЙДЕНО',
])
def test_r69_successful_gate_separator_is_done(positive):
    assert module._is_terminal_done(positive)


@pytest.mark.parametrize('contradictory', [
    'merged; CI: FAILED',
    'merged; Windows Candidate = SKIPPED',
    'інтегровано; тести: НЕ ПРОЙДЕНО',
])
def test_r69_failed_gate_separator_rejected_by_cli(tmp_path, contradictory):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — {contradictory} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

# r76: a later explicit failed gate is still current-state evidence even if
# an earlier check in the same cell passed.  A status row may list multiple
# independently required gates; success of one cannot mask another's failure.
@pytest.mark.parametrize('contradictory', [
    'DONE — merged; CI: PASS; Windows Candidate: SKIPPED',
    'DONE — merged; CI: SUCCESS; Windows Candidate = PENDING',
    'DONE — merged; tests: PASSED; Windows CI: FAILURE',
    'DONE — merged; Windows Candidate: SUCCESS; tests: FAILED',
    'DONE — merged; tests passed; Windows CI: FAILURE',
    'DONE — merged; CI: PASS but Windows Candidate: SKIPPED',
    'DONE — merged; CI: PASS and tests: FAILED',
    'DONE — інтегровано; CI: SUCCESS; Windows Candidate = PENDING',
    'DONE — інтегровано; тести: ПРОЙДЕНО; перевірки: НЕ ПРОЙДЕНО',
    'DONE — злито; CI: SUCCESS; тести: ПРОВАЛЕНО',
])
def test_r76_later_explicit_failed_gate_is_not_terminal(contradictory):
    assert not module._is_terminal_done(contradictory)


@pytest.mark.parametrize('positive', [
    'DONE — merged; CI: SUCCESS; Windows Candidate: SUCCESS',
    'DONE — merged; tests: PASSED; Windows CI: SUCCESS',
    'DONE — merged; previous CI: FAILED, repaired; CI: SUCCESS',
    'DONE — інтегровано; тести: ПРОЙДЕНО; перевірки: ПРОЙДЕНО',
])
def test_r76_multiple_positive_or_historical_gates_stay_terminal(positive):
    assert module._is_terminal_done(positive)


@pytest.mark.parametrize('contradictory', [
    'merged; CI: PASS; Windows Candidate: SKIPPED',
    'merged; tests: PASSED; Windows CI: FAILURE',
    'інтегровано; CI: SUCCESS; Windows Candidate = PENDING',
])
def test_r76_later_failed_gate_rejected_by_resolved_tree(tmp_path, contradictory):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — {contradictory} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

# r77: GitHub Actions reports additional non-success check conclusions. A
# terminal Section cannot be inferred from ERROR, ACTION_REQUIRED, STALE,
# STARTUP_FAILURE, NEUTRAL, or still-running gate states after a merge claim.
@pytest.mark.parametrize('bad_gate', [
    'CI: ERROR', 'CI: ACTION_REQUIRED', 'CI: STALE',
    'Windows Candidate: STARTUP_FAILURE', 'Windows Candidate: NEUTRAL',
    'tests: ERROR', 'build: BLOCKED', 'CI: QUEUED', 'CI: IN_PROGRESS',
    'Windows CI: ACTION_REQUIRED',
])
def test_r77_non_success_check_conclusions_reject_done(bad_gate):
    assert not module._is_terminal_done(f'DONE — merged; {bad_gate}')


@pytest.mark.parametrize('bad_gate', [
    'CI: ERROR', 'Windows Candidate: NEUTRAL', 'build: STARTUP_FAILURE',
    'tests: BLOCKED', 'CI: ACTION_REQUIRED',
])
def test_r77_later_non_success_gate_cannot_hide_behind_pass(bad_gate):
    assert not module._is_terminal_done(f'DONE — merged; CI: SUCCESS; {bad_gate}')


@pytest.mark.parametrize('valid', [
    'DONE — merged; CI: SUCCESS; Windows Candidate: SUCCESS',
    'DONE — merged; previous CI: ERROR, repaired; Windows Candidate: PASS',
    'DONE — інтегровано; попередній CI: STALE, виправлено',
])
def test_r77_success_and_historical_failures_remain_terminal(valid):
    assert module._is_terminal_done(valid)


@pytest.mark.parametrize('bad_gate', [
    'CI: ACTION_REQUIRED', 'Windows Candidate: STARTUP_FAILURE',
    'build: BLOCKED',
])
def test_r77_non_success_check_conclusion_in_resolved_tree(tmp_path, bad_gate):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — merged; CI: SUCCESS; {bad_gate} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    result = module.inspect(tmp_path)
    assert not result['ok']
    assert any('2Б' in error for error in result['failures'])

# r78: Section closure requires more than CI and Windows Candidate. Hosted
# Endurance, UIA and package/recovery checks can be explicit independent
# acceptance gates. A failed named gate must not disappear behind earlier
# passing checks, while a historical failure that was repaired is narrative.
@pytest.mark.parametrize('bad_gate', [
    'Endurance: FAILED',
    'Endurance = SKIPPED',
    'Windows Endurance: ERROR',
    'Packaging: FAILURE',
    'Packaging: PENDING',
    'UIA: FAILED',
    'Smoke: SKIPPED',
    'E2E: TIMED_OUT',
    'Recovery: BLOCKED',
    'Install: STARTUP_FAILURE',
    'Update: QUEUED',
    'Restart: IN_PROGRESS',
])
def test_r78_extended_gate_conclusions_reject_terminal(bad_gate):
    assert not module._is_terminal_done(f'DONE — merged; CI: SUCCESS; {bad_gate}')


@pytest.mark.parametrize('bad_gate', [
    'Endurance: FAILED',
    'Packaging: SKIPPED',
    'UIA: FAILURE',
    'Recovery: PENDING',
])
def test_r78_extended_gate_adjacent_to_merge_rejects_terminal(bad_gate):
    assert not module._is_terminal_done(f'DONE — merged; {bad_gate}')


@pytest.mark.parametrize('positive', [
    'DONE — merged; CI: SUCCESS; Windows Candidate: SUCCESS; Endurance: SUCCESS',
    'DONE — merged; Packaging: PASS; UIA: SUCCESS; Recovery: PASSED',
    'DONE — merged; previous Endurance: FAILED, repaired; Endurance: SUCCESS',
])
def test_r78_extended_gate_success_and_history_not_overrejected(positive):
    assert module._is_terminal_done(positive)


@pytest.mark.parametrize('bad_gate', [
    'Endurance: FAILED',
    'Packaging: SKIPPED',
    'UIA: FAILURE',
    'Recovery: PENDING',
])
def test_r78_extended_gate_rejected_in_resolved_tree(tmp_path, bad_gate):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — merged; CI: SUCCESS; {bad_gate} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    result = module.inspect(tmp_path)
    assert not result['ok']
    assert any('2Б' in error for error in result['failures'])

# r79: Markdown cells can carry rendered HTML line breaks or literal newlines.
# A passing gate followed by a failed independent gate remains a contradiction
# even when the author did not insert a semicolon or comma.
@pytest.mark.parametrize('negative', [
    'DONE — merged; CI: SUCCESS Windows Candidate: FAILED',
    'DONE — merged; CI: SUCCESS\nWindows Candidate: FAILED',
    'DONE — merged; CI: SUCCESS<br>Windows Candidate: FAILED',
    'DONE — merged; CI: SUCCESS<BR/>Windows Candidate: SKIPPED',
    'DONE — merged; CI: SUCCESS<br />Packaging: PENDING',
    'DONE — merged; Windows Candidate: SUCCESS\nEndurance: ERROR',
    'DONE — merged; CI: PASS\nUIA: FAILURE',
    'DONE — merged; CI: PASSED\nRecovery: BLOCKED',
])
def test_r79_pass_followed_by_unseparated_failed_gate_not_done(negative):
    assert not module._is_terminal_done(negative)


@pytest.mark.parametrize('positive', [
    'DONE — merged; CI: SUCCESS Windows Candidate: SUCCESS',
    'DONE — merged; CI: SUCCESS<br>Windows Candidate: PASS',
    'DONE — merged; previous Windows Candidate: FAILED repaired; CI: SUCCESS',
])
def test_r79_unseparated_positive_and_historical_notes_remain_done(positive):
    assert module._is_terminal_done(positive)


def test_r79_unseparated_gate_failure_rejected_by_resolved_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged; CI: SUCCESS<br>Windows Candidate: FAILED |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    result = module.inspect(tmp_path)
    assert not result['ok']
    assert any('2Б' in error for error in result['failures'])

# r80: Markdown punctuation between independent gate clauses must not hide a
# later failure. A period/colon/dash is an ordinary visible separator, not a
# reason to accept a stale successful CI result as final authority.
@pytest.mark.parametrize('negative', [
    'DONE — merged; CI: SUCCESS. Windows Candidate: FAILED',
    'DONE — merged; CI: SUCCESS: Windows Candidate: FAILED',
    'DONE — merged; CI: SUCCESS — Windows Candidate: FAILED',
    'DONE — merged; CI: SUCCESS! Windows Candidate: SKIPPED',
    'DONE — merged; CI: SUCCESS? Packaging: PENDING',
    'DONE — merged; CI: SUCCESS; Windows Candidate: SUCCESS. Endurance: FAILED',
    'DONE — merged; CI: SUCCESS; Windows Candidate: SUCCESS: UIA: ERROR',
    'DONE — merged; CI: SUCCESS; Windows Candidate: SUCCESS — Recovery: BLOCKED',
])
def test_r80_punctuation_separated_gate_failure_rejected(negative):
    assert not module._is_terminal_done(negative)


@pytest.mark.parametrize('positive', [
    'DONE — merged; CI: SUCCESS. Windows Candidate: SUCCESS',
    'DONE — merged; CI: SUCCESS — Windows Candidate: PASS',
    'DONE — merged. previous Windows Candidate: FAILED, repaired; CI: SUCCESS',
])
def test_r80_punctuation_separated_success_or_historical_kept(positive):
    assert module._is_terminal_done(positive)


def test_r80_punctuation_separated_gate_failure_rejected_by_resolved_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged; CI: SUCCESS. Windows Candidate: FAILED |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    result = module.inspect(tmp_path)
    assert not result['ok']
    assert any('2Б' in error for error in result['failures'])

# r81: GitHub and operator reports also use FAIL, NOT_RUN, UNKNOWN and
# REQUIRES_ACTION. These are non-success outcomes, not terminal acceptance.
# In particular, a passing CI must not mask an independent unrun Windows gate.
@pytest.mark.parametrize('bad_gate', [
    'Windows Candidate: FAIL',
    'Windows Candidate: FAILING',
    'Endurance: FAIL',
    'UIA: NOT_RUN',
    'Windows Candidate: NOT RUN',
    'Windows Candidate: UNSUCCESSFUL',
    'Windows Candidate: UNKNOWN',
    'Windows Candidate: REQUIRES_ACTION',
    'Recovery: REQUIRES ACTION',
    'Packaging: NOT_RUN',
])
def test_r81_non_success_gate_conclusions_fail_closed(bad_gate):
    assert not module._is_terminal_done(f'DONE — merged; CI: PASS; {bad_gate}')


@pytest.mark.parametrize('positive', [
    'DONE — merged; CI: PASS; Windows Candidate: PASS',
    'DONE — merged; previous Windows Candidate: FAIL, repaired; CI: SUCCESS',
    'DONE — merged; CI: SUCCESS; UIA: PASSED; Recovery: PASS',
])
def test_r81_successful_and_historical_gate_notes_still_accepted(positive):
    assert module._is_terminal_done(positive)


def test_r81_unrun_windows_gate_cannot_close_resolved_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged; CI: PASS; Windows Candidate: NOT_RUN |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    result = module.inspect(tmp_path)
    assert not result['ok']
    assert any('2Б' in error for error in result['failures'])

# r83: Markdown/operator reports frequently use Unicode bullets, middle
# dots and symbolic separators between independently reported CI gates.
# A passing first gate must not make a later failed Windows gate disappear.
@pytest.mark.parametrize('separator', ['•', '·', '&', '+', '→', '◆'])
def test_r83_symbol_separated_failed_gate_is_not_done(separator):
    status = f'DONE — merged; CI: PASS {separator} Windows Candidate: FAILED'
    assert not module._is_terminal_done(status)


@pytest.mark.parametrize('separator', ['•', '·', '&', '+'])
def test_r83_symbol_separated_passed_gates_remain_done(separator):
    status = f'DONE — merged; CI: PASS {separator} Windows Candidate: SUCCESS'
    assert module._is_terminal_done(status)


def test_r83_bullet_separated_failed_windows_gate_blocks_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged; CI: PASS • Windows Candidate: FAILED |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    result = module.inspect(tmp_path)
    assert not result['ok']
    assert any('2Б' in error for error in result['failures'])

# r85: Section acceptance cannot certify DONE if an independent security,
# integrity, release or accessibility gate is explicitly unsuccessful.
# These are real gate families, not synonyms for the preceding CI check.
@pytest.mark.parametrize('gate', [
    'Security', 'Accessibility', 'Integrity', 'Release',
    'Migration', 'Backup', 'Restore', 'License', 'SBOM',
    'Dependencies', 'SAST', 'Supply Chain',
])
@pytest.mark.parametrize('outcome', ['FAILED', 'SKIPPED'])
def test_r85_additional_independent_gates_fail_closed(gate, outcome):
    status = f'DONE — merged; CI: SUCCESS; {gate}: {outcome}'
    assert not module._is_terminal_done(status)


@pytest.mark.parametrize('gate', [
    'Security', 'Accessibility', 'Integrity', 'Release',
    'Migration', 'Backup', 'Restore', 'License', 'SBOM',
    'Dependencies', 'SAST', 'Supply Chain',
])
def test_r85_additional_independent_gates_success_remains_valid(gate):
    assert module._is_terminal_done(f'DONE — merged; CI: SUCCESS; {gate}: SUCCESS')


def test_r85_security_gate_failure_rejected_by_resolved_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged; CI: SUCCESS; Security: FAILED |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


# r87: CI providers and repository rules can add named checks without
# updating a hard-coded vocabulary. Explicit failed check conclusions in
# the canonical status cell must not be hidden by an earlier successful CI.
@pytest.mark.parametrize('label,outcome', [
    ('Checks', 'FAILED'),
    ('CodeQL', 'FAILED'),
    ('Review', 'CHANGES_REQUESTED'),
    ('LicenseScan', 'NOT_RUN'),
    ('Third Party Audit', 'SKIPPED'),
    ('CustomGate', 'UNKNOWN'),
    ('Artifact Scan', 'ERROR'),
    ('PolicyGate', 'BLOCKED'),
])
def test_r87_unknown_named_gate_failure_is_not_done(label, outcome):
    assert not module._is_terminal_done(
        f'DONE — merged; CI: SUCCESS; {label}: {outcome}'
    )


@pytest.mark.parametrize('label', ['Checks', 'CodeQL', 'Review', 'Third Party Audit'])
def test_r87_unknown_named_gate_success_remains_done(label):
    assert module._is_terminal_done(f'DONE — merged; CI: SUCCESS; {label}: PASS')


@pytest.mark.parametrize('text', [
    'DONE — merged; previous CodeQL: FAILED, repaired; CI: SUCCESS',
    'DONE — merged; historical Checks: FAILED, repaired; CI: SUCCESS',
])
def test_r87_historical_named_gate_failure_is_not_a_new_failure(text):
    assert module._is_terminal_done(text)


def test_r87_unknown_gate_failure_blocks_resolved_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged; CI: SUCCESS; CodeQL: FAILED |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


@pytest.mark.parametrize('status', [
    'DONE — merged; CI: SUCCESS CodeQL: FAILED',
    'DONE — merged; CI: SUCCESS • CodeQL: FAILED',
])
def test_r87_unknown_gate_without_semicolon_is_not_done(status):
    assert not module._is_terminal_done(status)


# r91: GitHub Actions check names often contain OS/matrix parentheses and
# comma-separated parameters.  A negative exact-head matrix job must not be
# ignored merely because the display name contains punctuation.
@pytest.mark.parametrize("check", [
    "CI (Windows): FAILED",
    "test (windows-latest, 3.12): FAILURE",
    "pytest (ubuntu-latest, 3.11): SKIPPED",
    "Test / Windows (3.12): NOT RUN",
    "CodeQL [Security]: REQUIRES ACTION",
    "Windows Candidate (x64): TIMED OUT",
    "Accessibility (packaged, x64): FAILED",
    "check [win, py3.12]: CANCELLED",
])
def test_r91_matrix_named_negative_gate_never_certifies_done(check):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + check)


@pytest.mark.parametrize("check", [
    "CI (Windows): FAILED",
    "test (windows-latest, 3.12): FAILURE",
    "CodeQL [Security]: REQUIRES ACTION",
])
def test_r91_matrix_named_negative_gate_at_first_clause_never_done(check):
    assert not module._is_terminal_done("DONE — merged; " + check)


@pytest.mark.parametrize("historical", [
    "previous CI (Windows): FAILED",
    "historical test (windows-latest, 3.12): FAILURE",
    "раніше CodeQL [Security]: FAILED",
])
def test_r91_historical_matrix_gate_not_current_failure(historical):
    assert module._is_terminal_done("DONE — merged; " + historical + ", repaired; CI: SUCCESS")


def test_r91_matrix_named_success_stays_positive():
    assert module._is_terminal_done(
        "DONE — merged; CI: SUCCESS; pytest (windows-latest, 3.12): SUCCESS"
    )

# r93: GitHub job display names may append both a matrix and a qualifier;
# an arrow can also render the result assignment. Neither may hide failure.
@pytest.mark.parametrize("check", [
    "CodeQL (windows-latest, 3.12) [required]: FAILURE",
    "CodeQL [windows-latest, 3.12] (required): FAILURE",
    "CodeQL → FAIL",
    "test (windows-latest, 3.12) [required] → SKIPPED",
])
def test_r93_double_matrix_suffix_and_arrow_failed_gate(check):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + check)


@pytest.mark.parametrize("check", [
    "CodeQL (windows-latest, 3.12) [required]: SUCCESS",
    "CodeQL [windows-latest, 3.12] (required) → PASS",
    "historical CodeQL (windows-latest, 3.12) [required]: FAILED, repaired",
])
def test_r93_double_matrix_suffix_positive_or_historical(check):
    assert module._is_terminal_done("DONE — merged; CI: SUCCESS; " + check)

@pytest.mark.parametrize("negative_gate", [
    "CodeQL (windows-latest, 3.12) [required]: FAILURE",
    "test (windows-latest, 3.12) [required] → SKIPPED",
])
def test_r93_actual_atomic_table_rejects_double_suffix_failure(tmp_path, negative_gate):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged; CI: SUCCESS; ' + negative_gate + ' |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

# r95: GitHub check-run lifecycle states are not successful conclusions.  A
# stale leading DONE and a different SUCCESS check cannot turn REQUESTED,
# COMPLETED (without conclusion), WAITING or TIMEOUT into acceptance.
@pytest.mark.parametrize('outcome', [
    'WAITING', 'REQUESTED', 'SCHEDULED', 'COMPLETED', 'RETRYING',
    'INCOMPLETE', 'INDETERMINATE', 'INVALID', 'NOT_APPLICABLE',
    'NOT-APPLICABLE', 'NOT_STARTED', 'TIMED-OUT', 'TIMEOUT',
    'ACTION-REQUIRED', 'NEEDS-APPROVAL', 'AWAITING_APPROVAL',
])
def test_r95_non_success_check_run_states_fail_closed(outcome):
    assert not module._is_terminal_done(
        f'DONE — merged; CI: SUCCESS; CodeQL (windows-latest, 3.12): {outcome}'
    )

@pytest.mark.parametrize('outcome', ['WAITING', 'COMPLETED', 'REQUESTED', 'TIMEOUT'])
def test_r95_unseparated_non_success_followup_fail_closed(outcome):
    assert not module._is_terminal_done(
        f'DONE — merged; CI: SUCCESS CodeQL: {outcome}'
    )

@pytest.mark.parametrize('outcome', ['WAITING', 'COMPLETED', 'REQUESTED'])
def test_r95_non_success_check_run_blocks_atomic_tree(tmp_path, outcome):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — інтегровано |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| 2Б | Navigation | DONE — merged; CI: SUCCESS; CodeQL: {outcome} |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — інтегровано |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

@pytest.mark.parametrize('description', [
    'DONE — merged; CI: SUCCESS; CodeQL: SUCCESS',
    'DONE — merged; CI: SUCCESS; CodeQL: PASSED',
    'DONE — merged; previous CodeQL: REQUESTED, then passed',
    'DONE — merged; historical CodeQL: COMPLETED without conclusion; repaired',
    'DONE — merged; SHA: 123abc; CI: SUCCESS',
])
def test_r95_valid_or_historical_status_not_overrejected(description):
    assert module._is_terminal_done(description)

# r96: a conjunction can join a successful current check to an explicitly
# historical check; the generic SUCCESS-followup parser must not treat the
# conjunction itself as part of a *current* failing check name.
@pytest.mark.parametrize('history', [
    'and previous', 'but historical', 'та раніше', 'але попередн',
])
def test_r96_conjoined_historical_gate_failure_remains_historical(history):
    status = f'DONE — merged; CI: SUCCESS {history} CodeQL: FAILURE, repaired'
    assert module._is_terminal_done(status)


@pytest.mark.parametrize('joiner', ['and', 'but', 'та', 'але'])
def test_r96_conjoined_current_gate_failure_remains_blocking(joiner):
    status = f'DONE — merged; CI: SUCCESS {joiner} CodeQL: FAILURE'
    assert not module._is_terminal_done(status)


def test_r96_conjoined_historical_does_not_mask_new_current_failure():
    status = ('DONE — merged; CI: SUCCESS and previous CodeQL: FAILURE, repaired; '
              'new-check: REQUESTED')
    assert not module._is_terminal_done(status)

# r97: GitHub Actions can format a matrix job with named selectors rather
# than positional ones, e.g. "pytest (os: windows-latest, python: 3.12)".
# The internal colon is part of the job name, not the result separator.
@pytest.mark.parametrize('gate', [
    'test (os: windows-latest, py: 3.12): FAILURE',
    'audit (os: windows-latest; py: 3.12): FAILED',
    'CodeQL (os: windows-latest, python: 3.12) [required]: REQUESTED',
    'security (os: ubuntu-latest, py: 3.11) → TIMED-OUT',
    'release [os: windows-latest, py: 3.12] (required): SKIPPED',
    'test [os: windows-latest, py: 3]: SKIPPED',
    'test [os: windows-latest, py: three]: FAILURE',
    'security [os: windows-latest]: REQUESTED',
])
def test_r97_named_matrix_selectors_do_not_hide_failed_checks(gate):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


@pytest.mark.parametrize('gate', [
    'test (os: windows-latest, py: 3.12): SUCCESS',
    'audit (os: windows-latest; py: 3.12): PASSED',
    'previous CodeQL (os: windows-latest, py: 3.12): FAILED, repaired',
    'historical test [os: windows-latest, py: 3.12]: SKIPPED, repaired',
    'previous test [os: windows-latest, py: 3]: FAILED, repaired',
])
def test_r97_named_matrix_success_or_repaired_history_stays_accepted(gate):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


def test_r97_named_matrix_failure_blocks_atomic_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged; CI: SUCCESS; '
        'test (os: windows-latest, py: 3.12): FAILURE |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


# r98: a third/fourth GitHub Actions qualifier must not make a failed
# check disappear from the authority cell. The previous regex admitted
# at most two parenthesized/bracketed suffixes, falsely accepting DONE.
@pytest.mark.parametrize("gate", [
    "CodeQL (windows-latest) [required] (policy): FAILURE",
    "CodeQL [windows-latest] [required] [policy]: FAILURE",
    "test (os: windows-latest, py: 3.12) [required] [owner]: SKIPPED",
    "release [windows-latest] (py: 3.12) [required] [policy]: REQUESTED",
    "audit " + "[required] " * 16 + ": FAILED",
    "audit " + "(policy) " * 16 + ": TIMED_OUT",
])
def test_r98_multi_qualifier_failed_check_blocks_done(gate):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + gate)


@pytest.mark.parametrize("gate", [
    "CodeQL (windows-latest) [required] (policy): SUCCESS",
    "test (os: windows-latest, py: 3.12) [required] [owner]: PASSED",
    "previous CodeQL (windows-latest) [required] (policy): FAILURE, repaired",
    "historical audit " + "[required] " * 16 + ": SKIPPED, repaired",
])
def test_r98_multi_qualifier_success_or_repaired_history(gate):
    assert module._is_terminal_done("DONE — merged; CI: SUCCESS; " + gate)


def test_r98_multi_qualifier_failure_blocks_aggregate_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged; CI: SUCCESS; '
        'test (os: windows-latest, py: 3.12) [required] [policy]: FAILURE |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


# Bound regex work for pathological status cells; over-complex authority is
# NOT an acceptance signal, even when every qualifier claims SUCCESS.
@pytest.mark.parametrize("ending", ["FAILURE", "SUCCESS"])
def test_r98_pathological_qualifier_count_fails_closed(ending):
    gate = "CodeQL " + "[required] " * 65 + ": " + ending
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + gate)


# r99: GitHub check names are user-defined, including punctuation and emoji.
# A prior CI success must never mask a current failure in a differently named
# check. These cases were accepted as DONE by r98.
@pytest.mark.parametrize("check_name", [
    "node@20", "build #2", "checks::linux", "test{win}",
    "test|win", "ci$linux", "ci✓windows", "build 🚀",
])
@pytest.mark.parametrize("delimiter", [":", " → "])
def test_r99_punctuated_check_names_cannot_hide_failure(check_name, delimiter):
    status = f"DONE — merged; CI: SUCCESS; {check_name}{delimiter} FAILURE"
    assert not module._is_terminal_done(status)


@pytest.mark.parametrize("status", [
    "DONE — merged; CI: SUCCESS; previous node@20: FAILURE, repaired",
    "DONE — merged; CI: SUCCESS; historical build #2: SKIPPED, repaired",
    "DONE — merged; CI: SUCCESS; formerly checks::linux: FAILURE, repaired",
    "DONE — merged; CI: SUCCESS; раніше build 🚀: FAILED, виправлено",
    "DONE — merged; CI: SUCCESS; build 🚀: SUCCESS",
])
def test_r99_punctuated_historical_or_successful_check_stays_accepted(status):
    assert module._is_terminal_done(status)


@pytest.mark.parametrize("status", [
    "DONE — merged; CI: SUCCESS → build #2: FAILURE",
    "DONE — merged; CI: SUCCESS • build 🚀: FAILURE",
    "DONE — merged; CI: SUCCESS ◆ node@20: SKIPPED",
])
def test_r99_punctuated_check_after_symbol_separator_fails_closed(status):
    assert not module._is_terminal_done(status)

# r100: GitHub check summaries often use an em/en/ASCII dash instead of
# colon, and report `conclusion: failure` rather than a bare conclusion.
# These are explicit current failed checks, not historical narrative.
@pytest.mark.parametrize('gate', [
    'CodeQL — FAILURE',
    'CodeQL – FAILURE',
    'CodeQL - FAILURE',
    'CodeQL / FAILURE',
    'CodeQL (windows-latest) [required] — SKIPPED',
    'test@windows — ACTION_REQUIRED',
    'test@windows: conclusion FAILURE',
    'CodeQL: conclusion: FAILURE',
    'CodeQL: result=FAILURE',
    'test (os: windows-latest, py: 3.12): conclusion FAILED',
    'audit@linux: result TIMED_OUT',
    'CodeQL → conclusion: REQUESTED',
])
def test_r100_current_check_conclusion_syntax_fails_closed(gate):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


@pytest.mark.parametrize('gate', [
    'previous CodeQL — FAILURE, repaired',
    'historical test@windows: conclusion FAILURE, repaired',
    'formerly CodeQL: result=FAILURE, repaired',
    'CodeQL — SUCCESS',
    'test@windows: conclusion SUCCESS',
    'test (windows-latest) — PASSED',
])
def test_r100_historical_or_successful_check_not_overrejected(gate):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


def test_r100_dashed_failure_blocks_atomic_aggregate(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged; CI: SUCCESS; CodeQL — FAILURE |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

# r101: a semicolon-delimited independent check may be rendered by GitHub
# without ':'/'='/'—' (for example 'CodeQL FAILURE'). A preceding green CI
# result cannot certify the Section when another named check is unsuccessful.
@pytest.mark.parametrize('gate', [
    'CodeQL FAILURE',
    'Security Audit FAILED',
    'UIA (windows-latest) SKIPPED',
    'packaging build CANCELLED',
    'test (windows-latest, 3.12) ERROR',
    'CodeQL (windows-latest) ACTION_REQUIRED',
    'node@20 FAILURE',
    'npm audit FAIL',
    'Test suite 50 FAIL',
])
def test_r101_semicolon_separated_bare_check_result_blocks_done(gate):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


@pytest.mark.parametrize('gate', [
    'previous CodeQL FAILURE, repaired',
    'historical CodeQL FAILURE, repaired',
    'formerly UIA (windows-latest) SKIPPED, repaired',
    'CodeQL SUCCESS',
])
def test_r101_bare_check_result_historical_or_success_stays_accepted(gate):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


def test_r101_bare_failed_check_blocks_atomic_parent_done(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = ATOMIC.replace(
        '| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
        '| 2А | WebView2 | DONE — merged; CI: SUCCESS; CodeQL FAILURE |',
    ).replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | DONE — merged |',
    ).replace(
        '| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
        '| 2В | Native STOP | DONE — merged |',
    )
    rejected(tmp_path, registry=registry, atomic=atomic)

# r102: a regex comment masker could treat a literal <!-- inside an HTML
# attribute, escaped Markdown text, or a code span as a real comment. When
# it appeared after the three required DONE rows, it hid a later duplicate
# 2Б authority row, allowing a forged parent DONE.
@pytest.mark.parametrize('literal_open,literal_close', [
    ('<span data-note="<!--">ordinary</span>', '<span data-note="-->">ordinary</span>'),
    ("<span data-note='<!--'>ordinary</span>", "<span data-note='-->'>ordinary</span>"),
    (r'\<!-- literal', 'literal -->'),
    ('`<!--` code', '`-->` code'),
    ('``<!--`` code', '``-->`` code'),
])
def test_r102_literal_comment_markers_cannot_hide_duplicate_atomic_section(
    tmp_path, literal_open, literal_close,
):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |')
        .replace('## План 7:',
                 f'| 3А | benign | {literal_open} |\n'
                 '| 2Б | duplicate | NOT DONE |\n'
                 f'| 3Б | benign | {literal_close} |\n'
                 '## План 7:'))
    rejected(tmp_path, registry, atomic)


def test_r102_real_html_comment_still_hides_inert_duplicate(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |')
        .replace('## План 7:',
                 '<!--\n| 2Б | commented example | NOT DONE |\n-->\n## План 7:'))
    (tmp_path / module.REGISTRY).write_text(registry, encoding='utf-8')
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    assert module.inspect(tmp_path)['ok']


@pytest.mark.parametrize('visible_link', [
    '<https://example.org/NOT-DONE>',
    '<https://example.org/NOT_DONE>',
    '<mailto:NOT-DONE@example.org>',
    '<NOT-DONE@example.org>',
])
def test_r102_visible_angle_autolink_negative_is_not_terminal(visible_link):
    assert not module._is_terminal_done('DONE — merged; ' + visible_link)


def test_r102_benign_angle_autolink_is_still_terminal():
    assert module._is_terminal_done('DONE — merged; <https://example.org/accepted>')


def test_r102_autolink_negative_blocks_resolved_tree(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged; <https://example.org/NOT-DONE> |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |'))
    rejected(tmp_path, registry, atomic)


def test_r102_unmatched_backtick_stress_is_bounded():
    # 4,000 unmatched runs used to cause repeated scans of the suffix.
    text = ('`x' * 4000) + '\n<!-- example -->\n'
    masked = module._mask_fenced_code(text)
    assert len(masked) == len(text)
    assert '<!--' not in masked


def test_r102_extreme_unmatched_backtick_count_fails_closed():
    with pytest.raises(module.AuthorityError, match='too many inline-code'):
        module._mask_fenced_code('`x' * 65537 + '\n<!-- text -->\n')


def test_r102_unbounded_html_tag_fails_closed():
    with pytest.raises(module.AuthorityError, match='unbounded HTML tag'):
        module._mask_fenced_code('<span data-note="' + 'x' * 10000 + '<!--')


def test_r102_unterminated_html_tag_with_comment_marker_fails_closed():
    with pytest.raises(module.AuthorityError, match='ambiguous HTML comment'):
        module._mask_fenced_code('<span data-note="<!--')

# r103: a visually confusable Greek/Cyrillic glyph inside an unsuccessful
# GitHub check conclusion must not turn a contradictory DONE into closure.
# The original evidence label (including 'previous'/'historical') remains
# authoritative for historical-exemption decisions.
@pytest.mark.parametrize('gate', [
    'CodeQL: FΑILURE',         # Greek alpha
    'CodeQL: FАILURE',         # Cyrillic A
    'CodeQL: FAΙLURE',         # Greek iota
    'CodeQL: FAІLURE',         # Cyrillic I
    'CodeQL: FΑILED',
    'Security Audit: SKΙPPED',
    'UIA (windows-latest): PΕNDING',
    'CodeQL: CANCΕLLED',
    'Windows Candidate: REQUΕSTED',
    'Security: BLΟCKED',
    'CodeQL: FAILURΕ',
    'CodeQL: FAILUΡE',
])
def test_r103_confusable_non_success_gate_blocks_done(gate):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


@pytest.mark.parametrize('status', [
    'DONE — merged; CI: SUCCESS; previous CodeQL: FΑILURE, repaired',
    'DONE — merged; CI: SUCCESS; historical CodeQL: FAΙLURE, repaired',
    'DONE — merged; CI: SUCCESS; CodeQL: SUCCESS',
    'DONE — інтегровано та перевірено',
])
def test_r103_historical_and_positive_still_terminal(status):
    assert module._is_terminal_done(status)


def test_r103_confusable_failure_blocks_resolved_parent_done(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged; CI: SUCCESS; CodeQL: FΑILURE |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |'))
    rejected(tmp_path, registry, atomic)

@pytest.mark.parametrize('status', [
    'DONE — merged; CodeQL (os: windows-latest): FΑILURE',
    'DONE — merged; CI: SUCCESS; CodeQL: conclusion FΑILURE',
    'DONE — merged; CI: SUCCESS; CodeQL: conclusion: FΑILURE',
    'DONE — merged; CI: SUCCESS → CodeQL: FΑILURE',
    'DONE — merged; CI: SUCCESS • CodeQL: FAΙLURE',
    'DONE — merged; previous CodeQL: FΑILURE, repaired; CodeQL: FAΙLURE',
])
def test_r103_matrix_conclusion_and_history_do_not_hide_current_spoof(status):
    assert not module._is_terminal_done(status)


@pytest.mark.parametrize('status', [
    'DONE — merged; evidence: українськаUTF8',
    'DONE — merged; previous CodeQL: FΑILURE, repaired',
    'DONE — merged; historical CodeQL: FAΙLURE, repaired',
    'DONE — merged; CI: SUCCESS; CodeQL: SUCCESS',
])
def test_r103_non_gate_unicode_or_historical_note_stays_valid(status):
    assert module._is_terminal_done(status)

# r109: result-first check summaries are common in CI aggregators. A green
# CI result cannot certify DONE when a later independent check is reported
# as "FAILURE (CodeQL)" rather than "CodeQL: FAILURE".
@pytest.mark.parametrize('gate', [
    'FAILURE (CodeQL)',
    'FAILED [CodeQL]',
    'SKIPPED (Windows Candidate)',
    'PENDING (UIA)',
    'ERROR (Security Audit)',
    'CANCELLED (Endurance)',
    'ACTION_REQUIRED (CodeQL)',
    'TIMED_OUT (Windows Candidate)',
    'FAILED: CodeQL',
    'FAILED — CodeQL',
])
def test_r109_result_first_failed_check_blocks_done(gate):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + gate)


def test_r109_result_first_failed_check_blocks_resolved_parent_done(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged; CI: SUCCESS; FAILURE (CodeQL) |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |'))
    rejected(tmp_path, registry, atomic)


def test_r109_success_result_first_stays_terminal():
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; SUCCESS (CodeQL)')

# r110: live Windows Candidate log at PR #1598 / 74d29aef includes
# "Packaged Autosport.exe accessibility audit exited 1". A machine gate's
# nonzero process exit is a failed result even without the word FAILURE.
@pytest.mark.parametrize('summary', [
    'Windows Candidate: packaged Autosport.exe accessibility audit exited 1',
    'Windows Candidate: audit exit status 137',
    'UIA: probe returned exit code -1',
    'Windows Candidate: accessibility audit exited 2',
    'Windows Candidate: SUCCESS (previous run exited 1, fixed) but current audit exited 2',
    'UIA: exit code 1 (semantic control unavailable)',
])
def test_r110_nonzero_named_gate_exit_blocks_done(summary):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)


@pytest.mark.parametrize('summary', [
    'Windows Candidate: accessibility audit exited 0',
    'previous Windows Candidate: audit exited 1, repaired',
    'Windows Candidate: SUCCESS (previous run exited 1, fixed)',
])
def test_r110_zero_or_explicitly_historical_exit_not_false_failure(summary):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)

# r111: Windows native process failures frequently surface as hexadecimal
# NTSTATUS (e.g. access violation 0xC0000005).  The earlier decimal-only
# gate matcher silently accepted those failed Windows checks as DONE.
@pytest.mark.parametrize('summary', [
    'Windows Candidate: packaged accessibility audit exited 0xC0000005',
    'Windows Candidate: accessibility audit exit code 0x1',
    'UIA: process exit status 0x00000001',
    'Windows Candidate: packaged audit exited -0x1',
    'CodeQL: analyzer exited 1',
    'Organization Policy: custom gate exited 0x2',
    'Windows Candidate: ' + ('diagnostic ' * 30) + ' audit exited 1',
    'Windows Candidate: audit exit code 0xBADCODE',
])
def test_r111_nonzero_hex_unknown_gate_or_long_detail_blocks_done(summary):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)

@pytest.mark.parametrize('summary', [
    'Windows Candidate: packaged audit exited 0x0',
    'Windows Candidate: packaged audit exit code 0x00000000',
    'CodeQL: analyzer exited 0',
    'Windows Candidate: packaged audit exited 0.',
    'Windows Candidate: packaged audit exit code 0x00000000.',
    'previous Windows Candidate: audit exited 0xC0000005, repaired',
    'Windows Candidate: SUCCESS (previous run exited 0xC0000005, fixed)',
])
def test_r111_zero_or_explicitly_historical_exit_remains_nonblocking(summary):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)


def test_r111_windows_hex_failure_blocks_parent_section_done(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate: audit exited 0xC0000005 |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |'))
    rejected(tmp_path, registry, atomic)

# r113: PowerShell and Windows process wrappers commonly emit ExitCode,
# ERRORLEVEL or return code instead of the phrase "exited N". A failed
# packaged UIA/Windows gate must not be promoted to terminal DONE.
@pytest.mark.parametrize('summary', [
    'Windows Candidate: packaged UIA audit ExitCode: 1',
    'Windows Candidate: accessibility audit exitCode=0xC0000005',
    'Windows Candidate: accessibility audit exit-code: 2',
    'Windows Candidate: accessibility audit return code 1',
    'Windows Candidate: accessibility audit return code: 0x1',
    'Windows Candidate: accessibility audit ERRORLEVEL 1',
    'Windows Candidate: accessibility audit ERRORLEVEL=0xC0000005',
    'Windows Candidate: accessibility audit rc=1',
    'CodeQL: analyzer exitCode: 3',
])
def test_r113_nonzero_windows_process_aliases_block_done(summary):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)


@pytest.mark.parametrize('summary', [
    'Windows Candidate: accessibility audit ExitCode: 0',
    'Windows Candidate: accessibility audit return code 0',
    'Windows Candidate: accessibility audit ERRORLEVEL 0',
    'Windows Candidate: accessibility audit rc=0',
    'CodeQL: analyzer exitCode: 0x0',
    'previous Windows Candidate: accessibility audit ExitCode: 1, repaired',
])
def test_r113_zero_or_historical_process_aliases_remain_nonblocking(summary):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)


def test_r113_exitcode_failure_blocks_resolved_parent_done(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate: UIA audit ExitCode: 1 |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |'))
    rejected(tmp_path, registry, atomic)


# r114: Real-world Windows wrappers also emit an unpunctuated ExitCode,
# a returned process status, or a parenthesized exit status. Each is a
# failed independent check even if the earlier CI check was successful.
@pytest.mark.parametrize("summary", [
    "Windows Candidate: packaged UIA audit ExitCode 1",
    "Windows Candidate: packaged UIA audit process returned 1",
    "Windows Candidate: packaged UIA audit process exited (1)",
    "CodeQL: analyzer ExitCode 0xC0000005",
])
def test_r114_nonzero_process_wrapper_variants_block_done(summary):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


@pytest.mark.parametrize("summary", [
    "Windows Candidate: packaged UIA audit ExitCode 0",
    "Windows Candidate: packaged UIA audit process returned 0",
    "Windows Candidate: packaged UIA audit process exited (0)",
    "previous Windows Candidate: packaged UIA audit ExitCode 1, repaired",
])
def test_r114_zero_or_historical_wrapper_variants_do_not_block_done(summary):
    assert module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


def test_r114_unpunctuated_exitcode_blocks_resolved_parent_done(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = (ATOMIC
        .replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |",
                 "| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate: UIA audit ExitCode 1 |")
        .replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
                 "| 2Б | Navigation | DONE — merged |")
        .replace("| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
                 "| 2В | Native STOP | DONE — merged |"))
    rejected(tmp_path, registry, atomic)

# r115: Process wrappers can report a bare "returned N", a POSIX signal,
# or a malformed decimal/hex token. Never parse only the leading zero of
# "0.1" as successful exit. These are independent gate failures even after
# an earlier successful CI result.
@pytest.mark.parametrize("summary", [
    "Windows Candidate: packaged audit returned 1",
    "Windows Candidate: packaged audit returned 0xC0000005",
    "Windows Candidate: packaged audit exited 0.1",
    "Windows Candidate: packaged audit exited 0x0.1",
    "Windows Candidate: packaged audit ExitCode 0.1",
    "Windows Candidate: packaged audit exited 0 (signal 11)",
    "Windows Candidate: packaged audit exited 0, signal 11",
    "Windows Candidate: packaged audit exited 0 (signal SIGSEGV)",
])
def test_r115_malformed_or_signaled_process_cannot_certify_done(summary):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


@pytest.mark.parametrize("summary", [
    "Windows Candidate: packaged audit returned 0",
    "Windows Candidate: packaged audit exited 0.",
    "Windows Candidate: packaged audit exited 0x0.",
    "Windows Candidate: packaged audit exited 0 (signal 0)",
    "previous Windows Candidate: packaged audit returned 1, repaired",
    "Windows Candidate: SUCCESS (previous audit exited 0, signal 11, fixed)",
])
def test_r115_zero_or_historical_process_remains_nonblocking(summary):
    assert module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


def test_r115_signal_failure_blocks_resolved_parent_done(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = (ATOMIC
        .replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |",
                 "| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate: audit exited 0 (signal 11) |")
        .replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
                 "| 2Б | Navigation | DONE — merged |")
        .replace("| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
                 "| 2В | Native STOP | DONE — merged |"))
    rejected(tmp_path, registry, atomic)


# r119: Real CI wrappers report ``returncode``, ``exited:`` and even omit
# a colon between the check display name and its process result. None may
# convert an unsuccessful Windows gate into a terminal Section DONE.
@pytest.mark.parametrize("summary", [
    "Windows Candidate: packaged UIA audit returncode=1",
    "Windows Candidate: packaged UIA audit returncode: 0xC0000005",
    "Windows Candidate: packaged UIA audit exited: 1",
    "Windows Candidate exited 1",
    "Windows Candidate process exited with code 1",
    "Organization Policy returned 2",
])
def test_r119_process_exit_variants_block_done(summary):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


@pytest.mark.parametrize("summary", [
    "Windows Candidate: packaged UIA audit returncode=0",
    "Windows Candidate: packaged UIA audit exited: 0",
    "Windows Candidate exited 0",
    "Windows Candidate process exited with code 0",
    "previous Windows Candidate exited 1, repaired",
    "historical Windows Candidate returned 2, fixed",
])
def test_r119_zero_or_historical_exit_does_not_block_done(summary):
    assert module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


def test_r119_colonless_windows_exit_blocks_parent_done(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC.replace(
        "| 2А | WebView2 | IN_PROGRESS / NOT DONE |",
        "| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate exited 1 |",
    ).replace(
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
        "| 2Б | Navigation | DONE — merged |",
    ).replace(
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
        "| 2В | Native STOP | DONE — merged |",
    )
    rejected(tmp_path, registry=registry, atomic=atomic)


# r122: A Windows/PowerShell native-process failure may be reported by
# $LASTEXITCODE or a symbolic termination signal even when the wrapper's
# preceding summary says PASS.  These are negative exact-gate evidence.
@pytest.mark.parametrize("summary", [
    "Windows Candidate: PASS ($LASTEXITCODE=1)",
    "Windows Candidate: PASS (PowerShell $LASTEXITCODE 1)",
    "Windows Candidate: PASS (return value 1)",
    "Windows Candidate: PASS (terminated by SIGSEGV)",
    "Windows Candidate: PASS (killed by SIGKILL)",
    "Windows Candidate: PASS (ExitCode=0; signal SIGSEGV)",
])
def test_r122_powershell_exit_or_signal_cannot_certify_done(summary):
    assert not module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


@pytest.mark.parametrize("summary", [
    "Windows Candidate: PASS ($LASTEXITCODE=0)",
    "Windows Candidate: PASS (return value 0)",
    "Windows Candidate: PASS (ExitCode=0; signal 0)",
    "previous Windows Candidate: PASS ($LASTEXITCODE=1), repaired",
])
def test_r122_zero_or_historical_powershell_exit_stays_accepted(summary):
    assert module._is_terminal_done("DONE — merged; CI: SUCCESS; " + summary)


def test_r122_powershell_failure_blocks_parent_done(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = (ATOMIC
        .replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |",
                 "| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate: PASS ($LASTEXITCODE=1) |")
        .replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
                 "| 2Б | Navigation | DONE — merged |")
        .replace("| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
                 "| 2В | Native STOP | DONE — merged |"))
    rejected(tmp_path, registry=registry, atomic=atomic)

# r123: A check can print a positive banner and then emit its real process
# result as a separate semicolon clause or a structured diagnostic object.
# The previous check-clause parser missed these and certified false DONE.
@pytest.mark.parametrize("summary", [
    'Windows Candidate: PASS; exit code: 1',
    'Windows Candidate: PASS; exit code: 0xC0000005',
    'Windows Candidate: PASS; returncode: 1',
    'Windows Candidate: PASS; rc=1',
    'Windows Candidate: PASS; {"exit_code": 1}',
    'Windows Candidate: PASS; {"exit_code": "0xC0000005"}',
    'Windows Candidate: PASS; {"returncode": 1}',
    'Windows Candidate: PASS; {"signal": 11}',
    'Windows Candidate: PASS; {"signal": "SIGSEGV"}',
    'Windows Candidate: PASS; {"exit_code": "garbled"}',
])
def test_r123_structured_or_separated_process_failure_blocks_done(summary):
    assert not module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)


@pytest.mark.parametrize("summary", [
    'Windows Candidate: PASS; exit code: 0',
    'Windows Candidate: PASS; returncode: 0',
    'Windows Candidate: PASS; rc=0',
    'Windows Candidate: PASS; {"exit_code": 0}',
    'Windows Candidate: PASS; {"returncode": "0x0"}',
    'Windows Candidate: PASS; {"signal": 0}',
    'previous Windows Candidate: {"exit_code": 1} fixed',
])
def test_r123_zero_or_historical_diagnostic_remains_nonblocking(summary):
    assert module._is_terminal_done('DONE — merged; CI: SUCCESS; ' + summary)


def test_r123_structured_failure_blocks_aggregate_parent_done(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|', '|2|DONE|')
    atomic = (ATOMIC
        .replace('| 2А | WebView2 | IN_PROGRESS / NOT DONE |',
                 '| 2А | WebView2 | DONE — merged; CI: SUCCESS; Windows Candidate: PASS; {"exit_code": 1} |')
        .replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                 '| 2Б | Navigation | DONE — merged |')
        .replace('| 2В | Native STOP | IN_PROGRESS / NOT DONE |',
                 '| 2В | Native STOP | DONE — merged |'))
    rejected(tmp_path, registry=registry, atomic=atomic)
