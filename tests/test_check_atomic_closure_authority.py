"""Falsifiers for Plan-6 owner authority preservation on resolved merge trees."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_atomic_closure_authority.py"
spec = importlib.util.spec_from_file_location("guard", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

REGISTRY = (
    "## Перебудова незакритих розділів — 2026-10-13\n"
    "Карта: ATOMIC_OPEN_SECTIONS_2026-10-13.md\n"
    "# Autosport — Multi-Plan Closure State\n"
    "### Plan 2 — Financial\n"
    "| Section | State | Note |\n|2|DONE|accepted|\n"
    "### Plan 6 — Windows / accessibility / packaging / UI\n"
    "| Section | State | Evidence / next gate |\n"
    "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|\n"
    "|3|PARTIAL_EXISTING|next|\n"
    "### Plan 7 — PAPER\n|2|WAITING_UPSTREAM|not yet|\n"
)
ATOMIC = (
    "# Autosport — повний реєстр самостійних розділів у незавершених планах\n"
    "## План 6: 21 самостійних розділів\n"
    "| Розділ | Зміст | Стан |\n| --- | --- | --- |\n"
    "| 1А | localization | DONE |\n"
    "| 2А | WebView2 | IN_PROGRESS / NOT DONE |\n"
    "| 2Б | Navigation | IN_PROGRESS / NOT DONE |\n"
    "| 2В | Native STOP | IN_PROGRESS / NOT DONE |\n"
    "## План 7: 21 самостійних розділів\n"
    "| 2А | different plan | WAITING |\n"
)


def make_tree(tmp_path, registry=REGISTRY, atomic=ATOMIC):
    (tmp_path / module.REGISTRY).write_text(registry, encoding="utf-8")
    (tmp_path / module.ATOMIC).write_text(atomic, encoding="utf-8")
    return tmp_path


def rejected(tmp_path, registry=REGISTRY, atomic=ATOMIC):
    assert not module.inspect(make_tree(tmp_path, registry, atomic))["ok"]


def test_valid_resolved_tree(tmp_path):
    assert module.inspect(make_tree(tmp_path))["ok"] is True


def test_missing_owner_directive(tmp_path):
    rejected(tmp_path, REGISTRY.replace("2026-10-13", "2026-10-12"))


def test_owner_date_only_in_history_does_not_count(tmp_path):
    rejected(tmp_path, REGISTRY.replace("## Перебудова незакритих розділів — 2026-10-13", "## Stale\n2026-10-13"))


def test_missing_map_reference(tmp_path):
    rejected(tmp_path, REGISTRY.replace(module.ATOMIC, "OLD_MAP.md"))


def test_missing_atomic_section(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("| 2В | Native STOP | IN_PROGRESS / NOT DONE |", ""))


def test_duplicate_atomic_section(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("## План 7", "| 2Б | duplicate | OPEN |\n## План 7"))


def test_spoof_section_in_other_plan_does_not_count(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |\n", ""))


def test_unproved_parent_done_is_rejected(tmp_path):
    rejected(tmp_path, REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|"))


def test_spoof_parent_in_other_plan_does_not_count(tmp_path):
    registry = REGISTRY.replace("|2|DONE|accepted|", "|2|IN_PROGRESS_NOT_DONE|other plan|")
    registry = registry.replace("|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|", "|2|DONE|unproved|")
    rejected(tmp_path, registry)


def test_duplicate_plan6_parent_row(tmp_path):
    rejected(tmp_path, REGISTRY.replace("|3|PARTIAL_EXISTING|next|", "|2|IN_PROGRESS_NOT_DONE|duplicate|\n|3|PARTIAL_EXISTING|next|"))


def test_duplicate_plan6_heading(tmp_path):
    rejected(tmp_path, REGISTRY + "### Plan 6 — repeated\n|2|IN_PROGRESS_NOT_DONE|duplicate|\n")


def test_duplicate_atomic_plan6_heading(tmp_path):
    rejected(tmp_path, atomic=ATOMIC + "## План 6: 21 самостійних розділів\n")


def test_missing_atomic_header(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("| Розділ | Зміст | Стан |", "| WRONG |"))


def test_missing_file_is_rejected(tmp_path):
    make_tree(tmp_path)
    (tmp_path / module.ATOMIC).unlink()
    try:
        module.inspect(tmp_path)
        assert False
    except module.AuthorityError:
        pass


def test_symlink_is_rejected(tmp_path):
    make_tree(tmp_path)
    f = tmp_path / module.ATOMIC
    target = tmp_path / "original.txt"
    f.rename(target)
    f.symlink_to(target)
    try:
        module.inspect(tmp_path)
        assert False
    except module.AuthorityError:
        pass


def test_bad_utf8_is_rejected(tmp_path):
    make_tree(tmp_path)
    (tmp_path / module.ATOMIC).write_bytes(b"\xff\xfe")
    try:
        module.inspect(tmp_path)
        assert False
    except module.AuthorityError:
        pass


def test_cli_failure_code(tmp_path):
    make_tree(tmp_path)
    (tmp_path / module.ATOMIC).unlink()
    assert module.main(["--resolved-tree", str(tmp_path)]) == 1


def test_cli_success_code(tmp_path):
    make_tree(tmp_path)
    assert module.main(["--resolved-tree", str(tmp_path)]) == 0


def test_output_is_stable_and_read_only(tmp_path):
    make_tree(tmp_path)
    before = [(p.name, p.read_bytes()) for p in tmp_path.iterdir()]
    a = module.inspect(tmp_path)
    b = module.inspect(tmp_path)
    assert a == b and a["gate"] == "SOURCE_AUTHORITY_ONLY_NOT_WINDOWS_OR_NVDA"
    assert before == [(p.name, p.read_bytes()) for p in tmp_path.iterdir()]


def test_terminal_parent_requires_all_three_terminal_atomic_sections(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(f"| {key} | {desc} | IN_PROGRESS / NOT DONE |", f"| {key} | {desc} | Статус: DONE — qualified and merged |")
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"]


def test_partial_terminal_atomic_section_is_allowed_while_parent_in_progress(tmp_path):
    atomic = ATOMIC.replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |", "| 2А | WebView2 | Статус: DONE — merged |")
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_parent_done_with_one_unfinished_atomic_is_rejected(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC.replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |", "| 2А | WebView2 | Статус: DONE — merged |")
    rejected(tmp_path, registry, atomic)


def test_status_column_missing_is_rejected(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |", "| 2Б | Navigation |"))


def test_status_column_injected_fourth_cell_is_rejected(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |", "| 2Б | Navigation | DONE | injected |"))


def test_description_done_cannot_substitute_for_atomic_state(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC.replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |", "| 2А | DONE description | IN_PROGRESS / NOT DONE |")
    rejected(tmp_path, registry, atomic)


def test_mixed_done_not_done_is_not_terminal(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC.replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |", "| 2А | WebView2 | Статус: DONE / NOT DONE |")
    rejected(tmp_path, registry, atomic)


def test_regular_file_path_replaced_during_read_is_rejected(tmp_path, monkeypatch):
    make_tree(tmp_path)
    original_read = module.os.read
    changed = False

    def swapped_read(fd, amount):
        nonlocal changed
        data = original_read(fd, amount)
        if not changed:
            changed = True
            path = tmp_path / module.REGISTRY
            replacement = tmp_path / "replacement.tmp"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
        return data

    monkeypatch.setattr(module.os, "read", swapped_read)
    try:
        module.inspect(tmp_path)
        assert False, "identical-content path replacement must fail"
    except module.AuthorityError:
        pass
    assert changed


def test_regular_file_inplace_modified_during_read_is_rejected(tmp_path, monkeypatch):
    make_tree(tmp_path)
    original_read = module.os.read
    changed = False

    def modified_read(fd, amount):
        nonlocal changed
        data = original_read(fd, amount)
        if not changed:
            changed = True
            path = tmp_path / module.REGISTRY
            path.write_text(REGISTRY + "\nconcurrent edit\n", encoding="utf-8")
        return data

    monkeypatch.setattr(module.os, "read", modified_read)
    try:
        module.inspect(tmp_path)
        assert False, "in-place mutation must fail"
    except module.AuthorityError:
        pass
    assert changed


def test_oversized_file_is_rejected(tmp_path):
    make_tree(tmp_path)
    (tmp_path / module.ATOMIC).write_bytes(b"x" * (module.MAX_BYTES + 1))
    try:
        module.inspect(tmp_path)
        assert False
    except module.AuthorityError:
        pass


def test_directory_in_place_of_file_is_rejected(tmp_path):
    make_tree(tmp_path)
    f = tmp_path / module.ATOMIC
    f.unlink()
    f.mkdir()
    try:
        module.inspect(tmp_path)
        assert False
    except module.AuthorityError:
        pass


def test_registry_replaced_between_authority_file_reads_is_rejected(tmp_path, monkeypatch):
    """Both authority files must belong to one stable read epoch."""
    make_tree(tmp_path)
    original_read = module.os.read
    swapped = False

    def change_after_first_file(fd, amount):
        nonlocal swapped
        # On the first read of the second authority file, replace the first
        # file with identical bytes: a single-file pinned read cannot detect it.
        if not swapped and module.os.fstat(fd).st_ino == (tmp_path / module.ATOMIC).stat().st_ino:
            swapped = True
            target = tmp_path / module.REGISTRY
            replacement = tmp_path / 'replacement.tmp'
            replacement.write_bytes(target.read_bytes())
            replacement.replace(target)
        return original_read(fd, amount)

    monkeypatch.setattr(module.os, 'read', change_after_first_file)
    with __import__('pytest').raises(module.AuthorityError):
        module.inspect(tmp_path)
    assert swapped


def test_registry_modified_between_authority_file_reads_is_rejected(tmp_path, monkeypatch):
    make_tree(tmp_path)
    original_read = module.os.read
    changed = False

    def change_after_first_file(fd, amount):
        nonlocal changed
        if not changed and module.os.fstat(fd).st_ino == (tmp_path / module.ATOMIC).stat().st_ino:
            changed = True
            target = tmp_path / module.REGISTRY
            target.write_text(REGISTRY + '\n# concurrent edit\n', encoding='utf-8')
        return original_read(fd, amount)

    monkeypatch.setattr(module.os, 'read', change_after_first_file)
    with __import__('pytest').raises(module.AuthorityError):
        module.inspect(tmp_path)
    assert changed
