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
