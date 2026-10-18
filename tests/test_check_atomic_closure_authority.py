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
    "| Section | State | Note |\n|---:|---|---|\n|2|DONE|accepted|\n"
    "### Plan 6 — Windows / accessibility / packaging / UI\n"
    "| Section | State | Evidence / next gate |\n|---:|---|---|\n"
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


def test_atomic_done_spoofed_in_prose_after_table_is_rejected(tmp_path):
    """A narrative pipe row cannot replace a missing canonical table row."""
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC.replace("| 2А | WebView2 | IN_PROGRESS / NOT DONE |", "| 2А | WebView2 | Статус: DONE — merged |")
    atomic = atomic.replace("| 2В | Native STOP | IN_PROGRESS / NOT DONE |", "| 2В | Native STOP | Статус: DONE — merged |")
    atomic = atomic.replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |", "")
    atomic = atomic.replace("## План 7", "\nNotes, not a table:\n| 2Б | forged | Статус: DONE — fake |\n## План 7")
    rejected(tmp_path, registry, atomic)


def test_registry_done_spoofed_in_prose_after_table_is_rejected(tmp_path):
    """The aggregate authority must come from the table, not later narrative."""
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|", "")
    registry = registry.replace("### Plan 7", "\nHistorical note:\n|2|DONE|forged|\n### Plan 7")
    rejected(tmp_path, registry)


def test_registry_parent_extra_column_cannot_authorize_done(tmp_path):
    """A four-cell registry record must not authorize an aggregate DONE."""
    registry = REGISTRY.replace(
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
        "|2|DONE|claimed evidence|injected cell|",
    )
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — accepted |",
        )
    rejected(tmp_path, registry, atomic)


def test_registry_parent_malformed_duplicate_row_is_rejected(tmp_path):
    """A malformed duplicate status row must not disappear from the census."""
    registry = REGISTRY.replace(
        "|3|PARTIAL_EXISTING|next|",
        "|2|\n|3|PARTIAL_EXISTING|next|",
    )
    rejected(tmp_path, registry)


def test_registry_parent_extra_column_rejected_while_in_progress(tmp_path):
    registry = REGISTRY.replace(
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|unexpected|",
    )
    rejected(tmp_path, registry)


def test_registry_header_fourth_column_is_rejected(tmp_path):
    registry = REGISTRY.replace(
        "| Section | State | Evidence / next gate |",
        "| Section | State | Evidence / next gate | forged |",
    ).replace(
        "|---:|---|---|\n|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
        "|---:|---|---|---|\n|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
    )
    rejected(tmp_path, registry)


def test_atomic_header_fourth_column_is_rejected(tmp_path):
    atomic = ATOMIC.replace(
        "| Розділ | Зміст | Стан |",
        "| Розділ | Зміст | Стан | forged |",
    ).replace(
        "| --- | --- | --- |",
        "| --- | --- | --- | --- |",
    )
    rejected(tmp_path, atomic=atomic)


def test_fenced_fake_registry_heading_does_not_supply_authority(tmp_path):
    registry = REGISTRY.replace("### Plan 6 — Windows / accessibility / packaging / UI", "### Not a plan")
    registry += "\n```markdown\n### Plan 6 — Windows / accessibility / packaging / UI\n| Section | State | Note |\n| --- | --- | --- |\n|2|IN_PROGRESS_NOT_DONE|forged|\n```\n"
    rejected(tmp_path, registry)


def test_fenced_fake_atomic_heading_does_not_supply_authority(tmp_path):
    atomic = ATOMIC.replace("## План 6: 21 самостійних розділів", "## Not the plan")
    atomic += "\n~~~markdown\n## План 6: 21 самостійних розділів\n| Розділ | Зміст | Стан |\n| --- | --- | --- |\n| 2А | fake | DONE |\n| 2Б | fake | DONE |\n| 2В | fake | DONE |\n~~~\n"
    rejected(tmp_path, atomic=atomic)


def test_fenced_fake_table_cannot_replace_missing_canonical_table(tmp_path):
    atomic = ATOMIC.replace("| Розділ | Зміст | Стан |\n| --- | --- | --- |", "not a table")
    atomic = atomic.replace("## План 7", "```md\n| Розділ | Зміст | Стан |\n| --- | --- | --- |\n| 2А | fake | DONE |\n| 2Б | fake | DONE |\n| 2В | fake | DONE |\n```\n## План 7")
    rejected(tmp_path, atomic=atomic)


def test_missing_final_pipe_in_atomic_status_row_is_rejected(tmp_path):
    rejected(tmp_path, atomic=ATOMIC.replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |", "| 2Б | Navigation | DONE"))


def test_missing_final_pipe_in_parent_status_row_is_rejected(tmp_path):
    rejected(tmp_path, REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|", "|2|IN_PROGRESS_NOT_DONE|accepted"))


def test_fenced_example_after_real_table_does_not_override_rows(tmp_path):
    atomic = ATOMIC.replace("## План 7", "```md\n| 2А | example | DONE |\n| 2Б | example | DONE |\n```\n## План 7")
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_longer_tilde_fence_is_not_closed_by_shorter_fence(tmp_path):
    atomic = ATOMIC.replace("## План 7", "~~~~md\n~~~\n| 2А | fake | DONE |\n~~~~\n## План 7")
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_indented_atomic_duplicate_is_not_silently_skipped(tmp_path):
    """GFM permits up to three leading spaces on a table row."""
    atomic = ATOMIC.replace(
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |\n"
        "  | 2В | duplicate | Статус: DONE — forged |",
    )
    rejected(tmp_path, atomic=atomic)


def test_indented_registry_duplicate_is_not_silently_skipped(tmp_path):
    registry = REGISTRY.replace(
        "|3|PARTIAL_EXISTING|next|",
        "|3|PARTIAL_EXISTING|next|\n |2|DONE|forged|",
    )
    rejected(tmp_path, registry=registry)


def test_indented_duplicate_with_extra_cell_is_rejected(tmp_path):
    atomic = ATOMIC.replace(
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |\n"
        "   | 2В | duplicate | DONE | forged |",
    )
    rejected(tmp_path, atomic=atomic)


def test_indented_three_cell_rows_still_parse(tmp_path):
    registry = REGISTRY.replace(
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
        "  |2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
    )
    atomic = ATOMIC.replace(
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
        "   | 2Б | Navigation | IN_PROGRESS / NOT DONE |",
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"]


def test_gfm_indented_header_and_separator_are_accepted(tmp_path):
    registry = REGISTRY.replace(
        "| Section | State | Evidence / next gate |\n|---:|---|---|",
        " | Section | State | Evidence / next gate |\n  |---:|---|---|",
    )
    atomic = ATOMIC.replace(
        "| Розділ | Зміст | Стан |\n| --- | --- | --- |",
        "  | Розділ | Зміст | Стан |\n   | --- | --- | --- |",
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"]


def test_four_space_indented_code_row_is_not_table_duplicate(tmp_path):
    atomic = ATOMIC.replace(
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |\n"
        "    | 2В | code example | DONE |",
    )
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_indented_malformed_separator_is_rejected(tmp_path):
    atomic = ATOMIC.replace(
        "| --- | --- | --- |",
        "  | --- | --- | --- | extra",
    )
    rejected(tmp_path, atomic=atomic)


def test_html_comment_cannot_supply_registry_parent_status(tmp_path):
    """An entire table in an HTML comment is not authoritative."""
    registry = REGISTRY.replace(
        "| Section | State | Evidence / next gate |\n|---:|---|---|\n"
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|\n"
        "|3|PARTIAL_EXISTING|next|",
        "<!--\n| Section | State | Evidence / next gate |\n|---:|---|---|\n"
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|\n"
        "|3|PARTIAL_EXISTING|next|\n-->",
    )
    rejected(tmp_path, registry)


def test_html_comment_cannot_supply_atomic_section_status(tmp_path):
    """An entire Plan 6 atomic table in an HTML comment is not authoritative."""
    table = (
        "| Розділ | Зміст | Стан |\n| --- | --- | --- |\n"
        "| 1А | localization | DONE |\n"
        "| 2А | WebView2 | IN_PROGRESS / NOT DONE |\n"
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |\n"
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |"
    )
    rejected(tmp_path, atomic=ATOMIC.replace(table, "<!--\n" + table + "\n-->"))


def test_html_comment_cannot_supply_canonical_header(tmp_path):
    """A commented fake header cannot become a second table authority."""
    fake = (
        "<!--\n| Розділ | Зміст | Стан |\n| --- | --- | --- |\n"
        "| 2А | fake | DONE |\n| 2Б | fake | DONE |\n"
        "| 2В | fake | DONE |\n-->\n"
    )
    atomic = ATOMIC.replace("| Розділ | Зміст | Стан |", fake + "| Розділ | Зміст | Стан |")
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_hidden_html_comment_duplicate_is_ignored(tmp_path):
    """An invisible duplicate table in comments must not reject valid authority."""
    fake = (
        "<!--\n| Розділ | Зміст | Стан |\n| --- | --- | --- |\n"
        "| 2А | fake | DONE |\n| 2Б | fake | DONE |\n"
        "| 2В | fake | DONE |\n-->\n"
    )
    atomic = ATOMIC.replace("## План 7", fake + "## План 7")
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_unclosed_html_comment_cannot_supply_atomic_rows(tmp_path):
    """An unterminated comment hides the remainder of the document."""
    table = (
        "| Розділ | Зміст | Стан |\n| --- | --- | --- |\n"
        "| 1А | localization | DONE |\n"
        "| 2А | WebView2 | IN_PROGRESS / NOT DONE |\n"
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |\n"
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |"
    )
    rejected(tmp_path, atomic=ATOMIC.replace(table, "<!--\n" + table))


def test_html_comment_fake_plan_heading_does_not_duplicate_real_heading(tmp_path):
    atomic = ATOMIC.replace(
        "## План 7",
        "<!--\n## План 6: 21 самостійних розділів\n-->\n## План 7",
    )
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_fenced_html_comment_marker_does_not_hide_later_valid_table(tmp_path):
    atomic = ATOMIC.replace(
        "| Розділ | Зміст | Стан |",
        "```md\n<!--\n```\n| Розділ | Зміст | Стан |",
    )
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_escaped_pipe_in_atomic_description_preserves_canonical_row(tmp_path):
    """A literal escaped pipe is cell text, not a new Markdown column."""
    atomic = ATOMIC.replace(
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
        r"| 2Б | Navigation \| focus | IN_PROGRESS / NOT DONE |",
    )
    assert module.inspect(make_tree(tmp_path, atomic=atomic))["ok"]


def test_escaped_pipe_in_parent_note_preserves_canonical_row(tmp_path):
    registry = REGISTRY.replace(
        "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|",
        r"|2|IN_PROGRESS_NOT_DONE|awaiting Windows \| UIA acceptance|",
    )
    assert module.inspect(make_tree(tmp_path, registry=registry))["ok"]


def test_escaped_pipe_in_header_note_preserves_canonical_table(tmp_path):
    registry = REGISTRY.replace(
        "| Section | State | Evidence / next gate |",
        r"| Section | State | Evidence \| next gate |",
    )
    assert module.inspect(make_tree(tmp_path, registry=registry))["ok"]


def test_escaped_pipe_in_terminal_atomic_evidence_is_not_extra_column(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — evidence \\| merged |",
        )
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"]


def test_even_number_of_backslashes_does_not_escape_extra_pipe(tmp_path):
    """Two literal backslashes followed by | are a true column boundary."""
    atomic = ATOMIC.replace(
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
        "| 2Б | Navigation \\\\| unexpected | IN_PROGRESS / NOT DONE |",
    )
    rejected(tmp_path, atomic=atomic)


def test_escaped_pipe_does_not_hide_real_duplicate_atomic_row(tmp_path):
    atomic = ATOMIC.replace(
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |",
        "| 2В | Native STOP | IN_PROGRESS / NOT DONE |\n"
        r"| 2В | forged \| note | Статус: DONE — forged |",
    )
    rejected(tmp_path, atomic=atomic)


def test_unicode_and_spaced_checkout_path_is_supported(tmp_path):
    checkout = tmp_path / "Windows збірка з пробілами"
    checkout.mkdir()
    assert module.inspect(make_tree(checkout))["ok"]


def test_escaped_pipe_cannot_turn_not_done_into_done(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE \\| NOT DONE |",
        )
    rejected(tmp_path, registry, atomic)


def test_invisible_format_character_cannot_hide_not_done_in_terminal_row(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified |", "Статус: DONE — NOT\u200bDONE |", 1)
    rejected(tmp_path, registry, atomic)


def test_mixed_script_confusable_not_done_is_not_terminal(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified |", "Статус: DONE — N\u041eT DONE |", 1)
    rejected(tmp_path, registry, atomic)


def test_normal_ukrainian_terminal_evidence_remains_valid(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — перевірено та інтегровано |",
        )
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"]


def test_greek_omicron_confusable_not_done_is_not_terminal(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified |", "Статус: DONE — N\u039fT DONE |", 1)
    rejected(tmp_path, registry, atomic)


def test_bidi_formatting_inside_done_evidence_is_rejected(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified |", "Статус: DONE — \u202equalified |", 1)
    rejected(tmp_path, registry, atomic)

# r9: fail-closed key and status identity; visually confusable rows must not
# be silently omitted from the canonical census or promoted to DONE.
import pytest


def _terminal_plan6():
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — інтегровано |",
        )
    return registry, atomic


@pytest.mark.parametrize("spoofed_key", ["2A", "2Α", "2А\u200b", "２Ａ", "2B", "2Β", "2В\u2060", "2б"])
def test_confusable_duplicate_atomic_key_cannot_hide_not_done(tmp_path, spoofed_key):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        "## План 7",
        f"| {spoofed_key} | forged duplicate | NOT DONE |\n## План 7",
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize("spoofed_key", ["2A", "2Α", "2А\u200b", "２Ａ", "2B", "2Β", "2В\u2060", "2б"])
def test_confusable_replacement_atomic_key_cannot_authorize_done(tmp_path, spoofed_key):
    registry, atomic = _terminal_plan6()
    original = "2А" if "A" in spoofed_key or "Α" in spoofed_key or "А" in spoofed_key else ("2Б" if spoofed_key == "2б" else "2В")
    atomic = atomic.replace(f"| {original} |", f"| {spoofed_key} |", 1)
    rejected(tmp_path, registry, atomic)


def test_fullwidth_done_is_not_an_explicit_terminal_marker(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace("Статус: DONE — інтегровано", "Статус: ＤＯＮＥ — інтегровано", 1)
    rejected(tmp_path, registry, atomic)


def test_confusable_key_with_extra_column_is_not_silently_ignored(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace("## План 7", "| 2A | forged | NOT DONE | fourth |\n## План 7")
    rejected(tmp_path, registry, atomic)


def test_other_unrelated_plan6_key_remains_acceptable(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace("## План 7", "| 3А | unrelated | PARTIAL_EXISTING |\n## План 7")
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"]

# r10: the *aggregate* Plan-6 section identity must not be spoofable either.
# A visually duplicate parent can make an IN_PROGRESS section appear DONE in
# the canonical status table while the validator silently ignores that row.


@pytest.mark.parametrize('alias', ['２', '𝟚', '2\u200b'])
def test_confusable_duplicate_plan6_parent_is_rejected(tmp_path, alias):
    registry = REGISTRY.replace(
        '|3|PARTIAL_EXISTING|next|',
        f'|{alias}|DONE|visually duplicate parent|\n|3|PARTIAL_EXISTING|next|',
    )
    rejected(tmp_path, registry=registry)


@pytest.mark.parametrize('alias', ['２', '𝟚', '2\u200b'])
def test_confusable_replacement_plan6_parent_is_rejected(tmp_path, alias):
    registry = REGISTRY.replace(
        '|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|',
        f'|{alias}|IN_PROGRESS_NOT_DONE|visually confusable|',
    )
    rejected(tmp_path, registry=registry)


def test_confusable_parent_with_fourth_cell_is_rejected(tmp_path):
    registry = REGISTRY.replace(
        '|3|PARTIAL_EXISTING|next|',
        '|２|DONE|spoof|extra|\n|3|PARTIAL_EXISTING|next|',
    )
    rejected(tmp_path, registry=registry)


def test_other_plan_confusable_parent_does_not_change_plan6_authority(tmp_path):
    registry = REGISTRY.replace(
        '|2|DONE|accepted|',
        '|２|DONE|other plan only|',
    )
    assert module.inspect(make_tree(tmp_path, registry=registry))['ok']

# r11: non-Cf default-ignorable marks and combining characters are not
# authority-bearing syntax. They must not hide a duplicate Section key or
# disguise a negative status as terminal DONE.


@pytest.mark.parametrize('alias', ['2\u034f', '2\ufe0e', '2\u0301'])
def test_combining_mark_duplicate_parent_is_rejected(tmp_path, alias):
    registry = REGISTRY.replace(
        '|3|PARTIAL_EXISTING|next|',
        f'|{alias}|DONE|visually ambiguous parent|\n|3|PARTIAL_EXISTING|next|',
    )
    rejected(tmp_path, registry=registry)


@pytest.mark.parametrize('alias', ['2А\u034f', '2А\ufe0f', '2\u0301А'])
def test_combining_mark_duplicate_atomic_key_is_rejected(tmp_path, alias):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('## План 7', f'| {alias} | duplicate | NOT DONE |\n## План 7')
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('negative', ['NOT\u034f DONE', 'NOT\ufe0f DONE', 'NO\u0301T DONE', 'NÓT DONE'])
def test_combining_mark_cannot_hide_negative_terminal_status(tmp_path, negative):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', f'Статус: DONE — {negative}', 1)
    rejected(tmp_path, registry, atomic)


def test_normal_ukrainian_combining_accent_in_evidence_is_allowed(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', 'Статус: DONE — наді́йно інтегровано', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

# r12: Markdown/HTML-rendered negative status must not become terminal DONE.
# Only the literal positive DONE marker can grant authority; render-time
# decoding/formatting is used strictly to *reject* contradictory evidence.
@pytest.mark.parametrize('negative', [
    'NOT&#32;DONE',
    'NOT&#x2d;DONE',
    'NOT&nbsp;DONE',
    'NOT**DONE**',
    'NOT<br>DONE',
    'NOT—DONE',
    'ΝΟΤ DΟΝΕ',
])
def test_rendered_negative_status_cannot_authorize_parent_done(tmp_path, negative):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', f'Статус: DONE — {negative}', 1)
    rejected(tmp_path, registry, atomic)


def test_html_entity_in_positive_ukrainian_evidence_is_allowed(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', 'Статус: DONE — QA &amp; український опис', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_html_encoded_format_control_in_terminal_evidence_is_rejected(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', 'Статус: DONE — evidence&#8203;validated', 1)
    rejected(tmp_path, registry, atomic)


def test_html_encoded_positive_marker_is_not_accepted(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', 'Статус: D&#79;NE — інтегровано', 1)
    rejected(tmp_path, registry, atomic)


def test_html_inline_emphasis_cannot_hide_negative_status(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', 'Статус: DONE — NOT <strong>DONE</strong>', 1)
    rejected(tmp_path, registry, atomic)

# r13: a rendered HTML/Markdown text node can join fragments that the raw
# source separates. A terminal parent must not accept hidden NOT DONE.
@pytest.mark.parametrize('negative', [
    'N<span>OT</span> DONE',
    'N<!-- editorial note -->OT DONE',
    'N[OT](#status) DONE',
    'N<strong>OT</strong> DONE',
    'N[O](#anchor)T DONE',
    'NO<a title="status">T</a> DONE',
])
def test_inline_rendered_fragments_cannot_hide_not_done(tmp_path, negative):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', f'Статус: DONE — {negative}', 1)
    rejected(tmp_path, registry, atomic)


def test_html_comment_negative_text_not_visible_does_not_block_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — інтегровано <!-- NOT DONE was old status -->', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_markdown_link_destination_negative_text_not_visible_does_not_block_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — [інтегровано](#NOT-DONE-old)', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

@pytest.mark.parametrize('alias', ['2<!-- hidden -->', '2<span></span>'])
def test_rendered_duplicate_parent_key_is_rejected(tmp_path, alias):
    registry = REGISTRY.replace('|3|PARTIAL_EXISTING|next|',
                                f'|{alias}|DONE|duplicate rendered key|\n|3|PARTIAL_EXISTING|next|')
    rejected(tmp_path, registry=registry)


@pytest.mark.parametrize('alias', ['2<!-- hidden -->А', '2<strong>А</strong>'])
def test_rendered_duplicate_atomic_key_is_rejected(tmp_path, alias):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('## План 7', f'| {alias} | duplicate | NOT DONE |\n## План 7')
    rejected(tmp_path, registry, atomic)

# r14: Markdown inline formatting is rendered away inside a table key. A
# second visually identical key must not evade the duplicate-row census.
@pytest.mark.parametrize('alias', [
    '**2**', '_2_', '`2`', '~~2~~', '[2](#section-two)',
    '[**2**](#section-two)',
])
def test_markdown_rendered_duplicate_parent_is_rejected(tmp_path, alias):
    registry = REGISTRY.replace(
        '|3|PARTIAL_EXISTING|next|',
        f'|{alias}|DONE|rendered duplicate|\n|3|PARTIAL_EXISTING|next|',
    )
    rejected(tmp_path, registry=registry)


@pytest.mark.parametrize('alias', [
    '**2А**', '_2А_', '`2А`', '~~2А~~', '[2А](#section-2a)',
    '[**2А**](#section-2a)', '2[А](#a)',
])
def test_markdown_rendered_duplicate_atomic_is_rejected(tmp_path, alias):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('## План 7', f'| {alias} | duplicate | NOT DONE |\n## План 7')
    rejected(tmp_path, registry, atomic)


def test_markdown_formatted_key_cannot_replace_canonical_parent(tmp_path):
    registry = REGISTRY.replace('|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|',
                                '|**2**|IN_PROGRESS_NOT_DONE|rendered alias|')
    rejected(tmp_path, registry=registry)


def test_markdown_formatted_key_cannot_replace_canonical_atomic(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('| 2А | WebView2 | Статус: DONE — інтегровано |',
                            '| **2А** | WebView2 | Статус: DONE — інтегровано |')
    rejected(tmp_path, registry, atomic)


def test_markdown_markup_in_unrelated_description_remains_valid(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('| 2А | WebView2 |', '| 2А | **WebView2** [documentation](#doc) |')
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


# r15: nested Markdown link destinations are legal GFM/Markdown. A flat
# replacement leaves their labels unprojected and can forge terminal DONE.
import pytest

@pytest.mark.parametrize("hidden_negative", [
    "[N](https://example.org/a(b))OT DONE",
    "[NO](https://example.org/a(b))T DONE",
    "N[OT](https://example.org/a(b)) DONE",
    "N[O](https://example.org/a(b))T DONE",
    "[N](https://example.org/a(b) \"title\")OT DONE",
    r"[N](https://example.org/a\(b\))OT DONE",
])
def test_r15_nested_link_cannot_hide_not_done(tmp_path, hidden_negative):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified", "Статус: DONE — " + hidden_negative, 1)
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize("key_alias", [
    "[2](https://example.org/a(b))А",
    "[2А](https://example.org/a(b))",
    "[2](https://example.org/a(b) \"title\")А",
])
def test_r15_nested_link_cannot_hide_duplicate_atomic_key(tmp_path, key_alias):
    atomic = ATOMIC.replace(
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
        f"| {key_alias} | duplicate | OPEN |\n| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
    )
    rejected(tmp_path, atomic=atomic)


def test_r15_reference_link_is_fail_closed_in_status(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified", "Статус: DONE — [N][ref]OT DONE", 1)
    rejected(tmp_path, registry, atomic)


def test_r15_valid_nested_link_in_evidence_stays_accepted(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — [qualified](https://example.org/a(b)) |",
        )
    assert module.inspect(make_tree(tmp_path, registry, atomic))["ok"] is True


def test_r15_unclosed_long_markdown_label_fails_without_quadratic_rescan(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC.replace(
        "| 2А | WebView2 | IN_PROGRESS / NOT DONE |",
        "| 2А | WebView2 | Статус: DONE — " + "[" * 30000 + " |",
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize("hidden_negative", ["[N]OT DONE", "N[OT] DONE", "[NO]T DONE"])
def test_r15_shortcut_reference_link_label_cannot_hide_negative(tmp_path, hidden_negative):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|", "|2|DONE|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(
            f"| {key} | {desc} | IN_PROGRESS / NOT DONE |",
            f"| {key} | {desc} | Статус: DONE — qualified |",
        )
    atomic = atomic.replace("Статус: DONE — qualified", "Статус: DONE — " + hidden_negative, 1)
    rejected(tmp_path, registry, atomic)


def test_r15_shortcut_reference_key_alias_cannot_hide_duplicate(tmp_path):
    atomic = ATOMIC.replace(
        "| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
        "| [2А] | duplicate | OPEN |\n| 2Б | Navigation | IN_PROGRESS / NOT DONE |",
    )
    rejected(tmp_path, atomic=atomic)

# r16: HTML accessible names may contradict rendered status text. A screen
# reader can announce aria-label/aria-labelledby or an image alt instead of
# the visible DONE. These must never authorize aggregate terminal closure.
@pytest.mark.parametrize("accessible_override", [
    '<span aria-label="NOT DONE">qualified</span>',
    '<span ARIA-LABEL = "NOT DONE">qualified</span>',
    '<span aria-labelledby="status-not-done">qualified</span>',
    '<img alt="NOT DONE" src="evidence.png">',
    '<input alt="NOT DONE" type="image" src="evidence.png">',
    '<span aria-label="NOT&#32;DONE">qualified</span>',
])
def test_r16_accessible_name_cannot_contradict_terminal_done(tmp_path, accessible_override):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + accessible_override, 1)
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize("key", [
    '<span aria-label="2А">other</span>',
    '<span aria-labelledby="section-2a">other</span>',
    '<img alt="2А" src="key.png">',
])
def test_r16_accessible_key_alias_cannot_hide_duplicate(tmp_path, key):
    atomic = ATOMIC.replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                            f'| {key} | duplicate | OPEN |\n| 2Б | Navigation | IN_PROGRESS / NOT DONE |')
    rejected(tmp_path, atomic=atomic)


def test_r16_html_comment_with_aria_does_not_invalidate_visible_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — інтегровано <!-- <span aria-label="NOT DONE"> -->', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r16_nonsemantic_html_evidence_remains_allowed(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — <strong>інтегровано</strong>', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

# r17: status/section identity must agree with assistive semantics. An ARIA
# description, role, HTML title, or Markdown link title can contradict the
# rendered DONE marker without changing its visible prefix.
@pytest.mark.parametrize('semantic_override', [
    '<span aria-description="NOT DONE">qualified</span>',
    '<span aria-describedby="not-done">qualified</span>',
    '<span aria-details="not-done">qualified</span>',
    '<span aria-braillelabel="NOT DONE">qualified</span>',
    '<span aria-errormessage="not-done">qualified</span>',
    '<span role="img" title="NOT DONE">qualified</span>',
    '<span title="NOT DONE">qualified</span>',
    '[qualified](https://example.org/evidence "NOT DONE")',
    "[qualified](https://example.org/evidence 'NOT DONE')",
    '[qualified](https://example.org/evidence (NOT DONE))',
])
def test_r17_accessible_description_cannot_override_terminal_status(tmp_path, semantic_override):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + semantic_override, 1)
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('alias', [
    '<span aria-description="2А">other</span>',
    '<span title="2А">other</span>',
    '[other](https://example.org/key "2А")',
])
def test_r17_accessible_description_cannot_hide_duplicate_section_key(tmp_path, alias):
    atomic = ATOMIC.replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                            f'| {alias} | duplicate | OPEN |\n| 2Б | Navigation | IN_PROGRESS / NOT DONE |')
    rejected(tmp_path, atomic=atomic)


def test_r17_data_attributes_do_not_masquerade_as_accessible_overrides(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — <span data-title="metadata" data-aria-label="metadata">інтегровано</span>', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r17_valid_nested_link_destination_remains_accepted(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — [інтегровано](https://example.org/a(b))', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

# r18: a quoted `>` in an earlier benign HTML attribute is not the end of
# the start tag. A flat `<[^<>]*...>` regex can stop there and silently miss
# a later accessible-name/role override, producing a false terminal DONE.
@pytest.mark.parametrize('override', [
    '<span data-note="x > y" aria-label="READY">qualified</span>',
    '<span data-note="x > y" aria-description="WAITING">qualified</span>',
    '<span data-note="x > y" title="WAITING">qualified</span>',
    '<span data-note="x > y" role="button">qualified</span>',
    '<img data-note="x > y" alt="WAITING" src="evidence.png">',
    '<input type="button" value="NOT READY">',
    '<svg><title>WAITING</title><circle r="1" /></svg>',
])
def test_r18_html_accessible_override_cannot_hide_after_quoted_angle(tmp_path, override):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + override, 1)
    rejected(tmp_path, registry, atomic)


def test_r18_html_quoted_angle_cannot_hide_duplicate_accessible_key(tmp_path):
    alias = '<span data-note="x > y" aria-label="2А">other</span>'
    atomic = ATOMIC.replace('| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
                            f'| {alias} | duplicate | OPEN |\n| 2Б | Navigation | IN_PROGRESS / NOT DONE |')
    rejected(tmp_path, atomic=atomic)


def test_r18_benign_quoted_angle_in_html_metadata_remains_accepted(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — <span data-note="x > y" data-title="ordinary">інтегровано</span>', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

@pytest.mark.parametrize('override', [
    '<span data-note="<!--" aria-label="WAITING">qualified</span>',
    '<span data-note="<!--" title="WAITING">qualified</span>',
])
def test_r18_comment_delimiter_inside_quoted_attribute_cannot_mask_accessibility(tmp_path, override):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + override, 1)
    rejected(tmp_path, registry, atomic)


def test_r18_real_html_comment_with_semantic_override_is_ignored(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — інтегровано <!-- <span aria-label="WAITING"> -->', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r18_quoted_comment_delimiters_cannot_erase_aria_authority(tmp_path):
    registry, atomic = _terminal_plan6()
    # Both comment delimiters are inside quoted attributes, NOT an HTML comment.
    # Regex comment masking removes the aria-label between them and falsely
    # certifies the row unless semantic markup is parsed before masking.
    status = '<span data-note="<!--" aria-label="WAITING" data-x="-->">qualified</span>'
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + status, 1)
    rejected(tmp_path, registry, atomic)

@pytest.mark.parametrize('control', [
    '<input type value="WAITING">',
    '<button>WAITING</button>',
])
def test_r18_interactive_controls_cannot_be_terminal_status_evidence(tmp_path, control):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + control, 1)
    rejected(tmp_path, registry, atomic)


def test_r19_parent_duplicate_with_only_first_cell_is_rejected(tmp_path):
    """GFM pads a short table row; malformed duplicate must not disappear."""
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|", "|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|\n| 2")
    rejected(tmp_path, registry=registry)


def test_r19_atomic_duplicate_with_only_first_cell_is_rejected(tmp_path):
    """Short table row renders the same section key with empty remaining cells."""
    atomic = ATOMIC.replace("| 2Б | Navigation | IN_PROGRESS / NOT DONE |", "| 2Б | Navigation | IN_PROGRESS / NOT DONE |\n| 2Б")
    rejected(tmp_path, atomic=atomic)


def test_r19_parent_done_cannot_ignore_short_duplicate(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|", "|2|DONE|accepted|\n| 2")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(f"| {key} | {desc} | IN_PROGRESS / NOT DONE |", f"| {key} | {desc} | Статус: DONE — merged |")
    rejected(tmp_path, registry, atomic)


def test_r19_atomic_done_cannot_ignore_short_duplicate(tmp_path):
    registry = REGISTRY.replace("|2|IN_PROGRESS_NOT_DONE|awaiting Windows acceptance|", "|2|DONE|accepted|")
    atomic = ATOMIC
    for key, desc in (("2А", "WebView2"), ("2Б", "Navigation"), ("2В", "Native STOP")):
        atomic = atomic.replace(f"| {key} | {desc} | IN_PROGRESS / NOT DONE |", f"| {key} | {desc} | Статус: DONE — merged |")
    atomic = atomic.replace("| 2Б | Navigation | Статус: DONE — merged |", "| 2Б | Navigation | Статус: DONE — merged |\n| 2Б")
    rejected(tmp_path, registry, atomic)

# r20: HTML block/line-break elements have rendered whitespace semantics.
# Stripping every HTML tag to an empty string can conceal a visible NOT DONE.
@pytest.mark.parametrize('negative_markup', [
    'NOT<br>DONE',
    'NOT<br/>DONE',
    'NOT<BR class="line">DONE',
    'NOT<br data-note="a > b">DONE',
    '<div>NOT</div><div>DONE</div>',
    '<p>NOT</p><p>DONE</p>',
    '<span>NOT</span><br><span>DONE</span>',
    'NOT<hr>DONE',
])
def test_r20_rendered_html_break_cannot_hide_not_done(tmp_path, negative_markup):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + negative_markup, 1)
    rejected(tmp_path, registry, atomic)


def test_r20_inline_html_without_break_preserves_valid_terminal_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — <strong>інтегровано</strong>', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

@pytest.mark.parametrize('key_alias', [
    '<span data-note="a > b">2А</span>',
    '<span data-note="a > b">2Б</span>',
    '<span data-note="a > b">2В</span>',
])
def test_r20_quoted_gt_html_tag_cannot_hide_duplicate_atomic_key(tmp_path, key_alias):
    atomic = ATOMIC.replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        f'| {key_alias} | duplicate | OPEN |\n| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
    )
    rejected(tmp_path, atomic=atomic)


def test_r20_quoted_gt_html_tag_cannot_hide_duplicate_parent_key(tmp_path):
    registry = REGISTRY.replace(
        '|3|PARTIAL_EXISTING|next|',
        '| <span data-note="a > b">2</span> | IN_PROGRESS_NOT_DONE | duplicate |\n|3|PARTIAL_EXISTING|next|',
    )
    rejected(tmp_path, registry=registry)

# r21: GFM allows table body rows without a leading and/or trailing pipe.
# A duplicate 2A/2B/2V can be rendered in the same table without `|` at
# column zero. The authority guard must count those rows, not stop scanning.
@pytest.mark.parametrize('extra_row', [
    '2Б | duplicate | NOT DONE',
    '2Б | duplicate | NOT DONE |',
    '2Б | duplicate |',
    '2Б |',
    '2Б | duplicate | Статус: DONE — forged',
])
def test_r21_unpiped_atomic_duplicate_must_not_disappear(tmp_path, extra_row):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n' + extra_row,
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('extra_row', [
    '2 | DONE | forged',
    '2 | IN_PROGRESS_NOT_DONE | duplicate |',
    '2 |',
])
def test_r21_unpiped_parent_duplicate_must_not_disappear(tmp_path, extra_row):
    registry, atomic = _terminal_plan6()
    registry = registry.replace('|2|DONE|awaiting Windows acceptance|', '|2|DONE|accepted|\n' + extra_row)
    rejected(tmp_path, registry, atomic)


def test_r21_nonkey_unpiped_gfm_row_does_not_reject(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n3А | next section | OPEN',
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

@pytest.mark.parametrize('spoofed', ['2B', '2Β', '２Ｂ', '2Б\u200b'])
def test_r21_unpiped_confusable_duplicate_is_rejected(tmp_path, spoofed):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n'
        + spoofed + ' | extra | OPEN',
    )
    rejected(tmp_path, registry, atomic)


def test_r21_unpiped_duplicate_with_escaped_pipe_is_rejected(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n'
        '2Б | duplicate \\| escaped | NOT DONE',
    )
    rejected(tmp_path, registry, atomic)


def test_r21_required_row_without_leading_pipe_cannot_grant_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2Б | Navigation | Статус: DONE — інтегровано |',
        '2Б | Navigation | Статус: DONE — інтегровано',
    )
    rejected(tmp_path, registry, atomic)

# r22: GFM body rows with *zero* pipe delimiters still render as table
# rows, padded with empty cells. A malformed duplicate after a valid DONE
# must not end the table scan and disappear from the section census.
@pytest.mark.parametrize('extra_row', [
    '2Б',
    '2B',
    '2Β',
    '２Ｂ',
    '2Б\u200b',
    '<span>2Б</span>',
    '2Б<!-- invisible -->',
])
def test_r22_pipe_free_atomic_duplicate_is_rejected(tmp_path, extra_row):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n' + extra_row,
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('extra_row', ['2', '<span>2</span>', '２', '2\u200b'])
def test_r22_pipe_free_parent_duplicate_is_rejected(tmp_path, extra_row):
    registry, atomic = _terminal_plan6()
    registry = registry.replace(
        '|2|DONE|awaiting Windows acceptance|',
        '|2|DONE|accepted|\n' + extra_row,
    )
    rejected(tmp_path, registry, atomic)


def test_r22_pipe_free_unrelated_gfm_row_is_not_an_authority(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n3А',
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r22_pipe_free_duplicate_before_later_valid_row_is_rejected(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2А | WebView2 | Статус: DONE — інтегровано |',
        '| 2А | WebView2 | Статус: DONE — інтегровано |\n2Б',
    )
    rejected(tmp_path, registry, atomic)

# r23: HTML may suppress or restyle the text of an authority key. A second
# rendered 2Б/2 cannot be hidden from the duplicate census by placing a
# non-rendered prefix in its source first cell.
@pytest.mark.parametrize('alias', [
    '<span hidden>noise</span>2Б',
    '<span HIDDEN>noise</span>2Б',
    '<span style="display:none">noise</span>2Б',
    '<span class="sr-only">noise</span>2Б',
    '<template>noise</template>2Б',
    '<style>noise</style>2Б',
])
def test_r23_presentationally_hidden_atomic_key_cannot_bypass_duplicate_check(tmp_path, alias):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n'
        f'| {alias} | forged duplicate | NOT DONE |',
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('alias', [
    '<span hidden>noise</span>2',
    '<span style="display:none">noise</span>2',
    '<template>noise</template>2',
])
def test_r23_presentationally_hidden_parent_key_cannot_bypass_duplicate_check(tmp_path, alias):
    registry, atomic = _terminal_plan6()
    registry = registry.replace(
        '|2|DONE|awaiting Windows acceptance|',
        '|2|DONE|accepted|\n'
        f'| {alias} | DONE | forged duplicate |',
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('markup', [
    '<span hidden>NOT DONE</span>',
    '<span style="display:none">NOT DONE</span>',
    '<span class="invisible">NOT DONE</span>',
    '<template>NOT DONE</template>',
])
def test_r23_presentational_markup_cannot_certify_terminal_status(tmp_path, markup):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — ' + markup, 1)
    rejected(tmp_path, registry, atomic)


def test_r23_plain_nonsemantic_html_remains_valid(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано',
                            'Статус: DONE — <span data-note="record">інтегровано</span>', 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

# r25: HTML fallback content in replaced/embedded elements may not render.
# A forged row can therefore look like a duplicate key even though its raw
# text includes a hidden prefix. Never certify the aggregate from such rows.
@pytest.mark.parametrize('tag', ['iframe', 'canvas', 'video', 'audio', 'object', 'svg'])
def test_r25_embedded_fallback_cannot_hide_atomic_duplicate(tmp_path, tag):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n'
        f'| <{tag}>hidden prefix</{tag}>2Б | forged duplicate | NOT DONE |',
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('tag', ['iframe', 'canvas', 'video', 'audio', 'object', 'svg'])
def test_r25_embedded_fallback_cannot_hide_parent_duplicate(tmp_path, tag):
    registry, atomic = _terminal_plan6()
    registry = registry.replace(
        '|2|DONE|awaiting Windows acceptance|',
        '|2|DONE|accepted|\n'
        f'| <{tag}>hidden prefix</{tag}>2 | DONE | forged duplicate |',
    )
    rejected(tmp_path, registry, atomic)

@pytest.mark.parametrize('tag', ['iframe', 'canvas', 'video', 'audio', 'object', 'svg'])
def test_r25_embedded_fallback_cannot_certify_terminal_status(tmp_path, tag):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — <{tag}>untrusted fallback</{tag}> accepted', 1,
    )
    rejected(tmp_path, registry, atomic)

# r26: a positive marker cannot coexist with an explicit nonterminal state.
# These are canonical work-state vocabulary, not prose descriptions.
@pytest.mark.parametrize('contradiction', [
    'WAITING_UPSTREAM', 'WAITING_OWNER_UPSTREAM', 'PARKED',
    'BLOCKED', 'IN_PROGRESS', 'QUALIFYING', 'INTEGRATION_PENDING',
    'PARTIAL_EXISTING', 'OPEN',
])
def test_r26_done_with_nonterminal_state_must_fail_closed(tmp_path, contradiction):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — {contradiction}', 1,
    )
    rejected(tmp_path, registry, atomic)

# r27: a superseded, rejected, or still-gated Section cannot certify parent DONE.
@pytest.mark.parametrize('contradiction', [
    'REOPENED', 'FAILED', 'CANCELLED', 'NOT_STARTED', 'PENDING',
    'WAITING_OWNER', 'WAITING_SECTION2', 'WAITING_PLAN6_M1_EXTERNAL_SCOPE',
    'BLOCKED_UPSTREAM', 'PARKED_UPSTREAM', 'NEEDS_REVIEW',
    'CI_FAILURE', 'INVALIDATED', 'NOT_ACCEPTED',
    'WAITING_EXTERNAL_PLAN4', 'WAITING_CLAIMED_SCOPE',
])
def test_r27_done_with_reopened_failed_or_external_wait_must_fail_closed(tmp_path, contradiction):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — {contradiction}', 1,
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('rendered', [
    'REO&#80;ENED', 'RE<span>OPENED</span>',
    'WAITING&#95;SECTION2', 'NOT&#95;ACCEPTED',
])
def test_r27_rendered_contradictory_state_must_fail_closed(tmp_path, rendered):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — {rendered}', 1,
    )
    rejected(tmp_path, registry, atomic)

@pytest.mark.parametrize('accepted_note', [
    'Статус: DONE — інтегровано; попередній FAILED CI виправлено',
    'Статус: DONE — прийнято після WAITING_UPSTREAM',
])
def test_r27_historical_nonterminal_words_after_acceptance_do_not_reopen(tmp_path, accepted_note):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', accepted_note, 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


# r28: CommonMark backslash-escaped ASCII punctuation is rendered without
# the backslash. A forged terminal DONE must not hide NOT_DONE or a workflow
# blocker behind a literal escaped underscore/hyphen or inline link label.
@pytest.mark.parametrize('contradiction', [
    r'NOT\_DONE',
    r'NOT\-DONE',
    r'WAITING\_SECTION2',
    r'IN\_PROGRESS',
    r'NOT\_ACCEPTED',
    r'REOPENED\_UPSTREAM',
    r'[NOT](https://example.invalid/accepted)\_DONE',
])
def test_r28_escaped_commonmark_punctuation_cannot_hide_nonterminal_state(tmp_path, contradiction):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — {contradiction}', 1,
    )
    rejected(tmp_path, registry, atomic)


def test_r28_escaped_punctuation_in_historical_note_is_not_false_blocker(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        r'Статус: DONE — інтегровано; попередній WAITING\_UPSTREAM виправлено', 1,
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


# r28: C0/C1 controls are valid UTF-8 bytes but have no stable Markdown/UI
# rendering. In particular NOT<NUL>DONE was previously accepted as terminal.
@pytest.mark.parametrize('control', ['\x00', '\x08', '\x1b', '\x7f', '\x85'])
def test_r28_control_inside_nonterminal_status_cannot_grant_done(tmp_path, control):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — NOT' + control + 'DONE', 1,
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('control', ['\x00', '\x1b', '\x7f'])
def test_r28_control_in_atomic_section_identifier_cannot_hide_duplicate(tmp_path, control):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n'
        '| 2' + control + 'Б | duplicate | NOT DONE |',
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('control', ['\x00', '\x1b', '\x7f'])
def test_r28_control_in_parent_section_identifier_cannot_hide_duplicate(tmp_path, control):
    registry, atomic = _terminal_plan6()
    registry = registry.replace(
        '|2|DONE|awaiting Windows acceptance|',
        '|2|DONE|accepted|\n|2' + control + '|DONE|forged duplicate|',
    )
    rejected(tmp_path, registry, atomic)

# r29: closed HTML disclosure and inert form/dialog containers do not render
# their descendants as ordinary visible text.  Their hidden text must not
# conceal a second canonical status-row key from the duplicate census.
@pytest.mark.parametrize('tag', ['details', 'dialog', 'datalist'])
def test_r29_hidden_html_container_cannot_hide_atomic_duplicate(tmp_path, tag):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        '| 2В | Native STOP | Статус: DONE — інтегровано |',
        '| 2В | Native STOP | Статус: DONE — інтегровано |\n'
        f'| 2<{tag}>hidden</{tag}>Б | duplicate | NOT DONE |',
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('tag', ['details', 'dialog', 'datalist'])
def test_r29_hidden_html_container_cannot_hide_parent_duplicate(tmp_path, tag):
    registry, atomic = _terminal_plan6()
    registry = registry.replace(
        '|2|DONE|awaiting Windows acceptance|',
        '|2|DONE|accepted|\n'
        f'|2<{tag}>hidden</{tag}>|DONE|forged duplicate|',
    )
    rejected(tmp_path, registry, atomic)

# r30: A later explicit status assignment in the same authoritative cell
# supersedes a stale DONE prefix. Narrative mentions of earlier failed
# checks are not a second assignment and must remain accepted.
@pytest.mark.parametrize('secondary_state', [
    'Статус: REOPENED',
    'Статус міграції: NOT_ACCEPTED',
    'State: IN_PROGRESS',
    'CURRENT_STATE=WAITING_UPSTREAM',
    'LATEST_STATE: FAILED',
    'Статус: REO&#80;ENED',
    'State: <span>REOPENED</span>',
    r'CURRENT\_STATE=REOPENED',
])
def test_r30_later_explicit_nonterminal_state_overrides_stale_done(tmp_path, secondary_state):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано; {secondary_state}', 1,
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('note', [
    'Статус: DONE — інтегровано; попередній FAILED CI виправлено',
    'Статус: DONE — прийнято після WAITING_UPSTREAM',
    'Статус: DONE — інтегровано; State: DONE (підтверджено)',
])
def test_r30_historical_note_or_repeated_done_is_not_reopened(tmp_path, note):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace('Статус: DONE — інтегровано', note, 1)
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r30_missing_resolved_tree_does_not_echo_private_path(tmp_path, capsys):
    private_path = tmp_path / 'Users' / 'приватне ім’я' / 'secret-folder' / 'missing'
    assert module.main(['--resolved-tree', str(private_path)]) == 1
    output = capsys.readouterr().out
    import json
    report = json.loads(output)
    assert report['ok'] is False and report['gate'] == module.GATE
    assert str(private_path) not in output
    assert str(tmp_path) not in output
    assert 'secret-folder' not in output


def test_r30_resolve_permission_error_does_not_echo_exception_path(tmp_path, monkeypatch, capsys):
    secret = str(tmp_path / 'users' / 'секрет' / 'profile')
    original_resolve = Path.resolve
    def forbidden_resolve(path, *args, **kwargs):
        if path == tmp_path:
            raise PermissionError(f'Access denied: {secret}')
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', forbidden_resolve)
    assert module.main(['--resolved-tree', str(tmp_path)]) == 1
    output = capsys.readouterr().out
    import json
    report = json.loads(output)
    assert report['ok'] is False
    assert secret not in output and 'секрет' not in output


def test_r30_separate_process_repeatability_for_reopened_authority(tmp_path):
    import json
    import subprocess
    import sys
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано; Статус: REOPENED', 1,
    )
    make_tree(tmp_path, registry, atomic)
    command = [sys.executable, str(SCRIPT), '--resolved-tree', str(tmp_path)]
    runs = [subprocess.run(command, capture_output=True, text=True, timeout=15)
            for _ in range(2)]
    assert all(run.returncode == 1 and not run.stderr for run in runs)
    assert runs[0].stdout == runs[1].stdout
    assert json.loads(runs[0].stdout)['ok'] is False
    # A fresh process must not retain the prior failed decision.
    (tmp_path / module.ATOMIC).write_text(
        atomic.replace('; Статус: REOPENED', ''), encoding='utf-8'
    )
    accepted = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert accepted.returncode == 0 and not accepted.stderr
    assert json.loads(accepted.stdout)['ok'] is True


# r31: bilingual status assignments are canonical in a mixed-language
# release ledger; an English Status label cannot bypass a later reopen.
@pytest.mark.parametrize('secondary_state', [
    'Status: REOPENED',
    'STATUS=WAITING_UPSTREAM',
    'Current Status: FAILED',
    'Latest Status: NOT_ACCEPTED',
])
def test_r31_english_status_assignment_overrides_stale_done(tmp_path, secondary_state):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано; {secondary_state}', 1,
    )
    rejected(tmp_path, registry, atomic)


def test_r31_english_status_done_remains_accepted(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано; Status: DONE', 1,
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r31_cli_reopen_then_recovery_after_corrected_status(tmp_path):
    import json
    import subprocess
    import sys

    registry, atomic = _terminal_plan6()
    reopened = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано; Status: REOPENED', 1,
    )
    make_tree(tmp_path, registry, reopened)
    command = [sys.executable, str(SCRIPT), '--resolved-tree', str(tmp_path)]
    blocked = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert blocked.returncode == 1 and not blocked.stderr
    assert json.loads(blocked.stdout)['ok'] is False
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    recovered = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert recovered.returncode == 0 and not recovered.stderr
    assert json.loads(recovered.stdout)['ok'] is True

# r32: a later status assignment may be separated by a dash, slash or
# escaped Markdown pipe, not only a semicolon. A stale DONE prefix must not
# authorize a section whose *latest* explicitly recorded status is negative.
@pytest.mark.parametrize('separator', [' — ', ' – ', ' / ', r' \| '])
@pytest.mark.parametrize('secondary', [
    'Status: REOPENED',
    'Current Status: FAILED',
    'Статус: WAITING_UPSTREAM',
])
def test_r32_later_state_after_common_separators_cannot_grant_done(tmp_path, separator, secondary):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано{separator}{secondary}', 1,
    )
    rejected(tmp_path, registry, atomic)


def test_r32_historical_failure_prose_after_dash_stays_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано — попередній FAILED CI виправлено', 1,
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

# r33: comma/parenthetical/colon separators occur in release notes. A
# trailing explicit current state is authority, not historical prose.
@pytest.mark.parametrize('separator', [', ', ' (', ': ', ' — ('])
@pytest.mark.parametrize('secondary', ['Status: REOPENED', 'Статус: NOT_DONE'])
def test_r33_later_state_after_release_note_separator_cannot_grant_done(tmp_path, separator, secondary):
    registry, atomic = _terminal_plan6()
    suffix = ')' if separator.endswith('(') else ''
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано{separator}{secondary}{suffix}', 1,
    )
    rejected(tmp_path, registry, atomic)


def test_r33_comma_reopen_then_restart_corrected_status(tmp_path):
    import json
    import subprocess
    import sys
    registry, atomic = _terminal_plan6()
    reopened = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано, Status: REOPENED', 1,
    )
    make_tree(tmp_path, registry, reopened)
    command = [sys.executable, str(SCRIPT), '--resolved-tree', str(tmp_path)]
    failed = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert failed.returncode == 1 and json.loads(failed.stdout)['ok'] is False
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    restored = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert restored.returncode == 0 and json.loads(restored.stdout)['ok'] is True


def test_r33_comma_historical_prose_without_assignment_remains_done(tmp_path):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано, попередній FAILED CI виправлено', 1,
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']

# r34: Ukrainian is the canonical UI/documentation language. A later
# explicit Ukrainian nonterminal state must never be hidden behind a stale
# English DONE marker in the same authoritative Plan-6 atomic status cell.
@pytest.mark.parametrize('secondary', [
    'Статус: НЕ ЗАВЕРШЕНО',
    'Статус: В РОБОТІ',
    'Стан: У РОБОТІ',
    'Стан: ПОВТОРНО ВІДКРИТО',
    'Поточний статус: ОЧІКУЄ',
    'Останній стан: ЗАБЛОКОВАНО',
    'Статус міграції: НЕ ПРИЙНЯТО',
    'Статус: ПРИЗУПИНЕНО',
    'Status: RE-OPENED',
])
def test_r34_ukrainian_or_hyphenated_explicit_reversal_fails_closed(tmp_path, secondary):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано; {secondary}', 1,
    )
    rejected(tmp_path, registry, atomic)


@pytest.mark.parametrize('positive', [
    'Статус: DONE',
    'Стан: DONE',
    'Поточний статус: DONE',
    'Останній стан: DONE',
])
def test_r34_repeated_positive_ukrainian_state_remains_valid(tmp_path, positive):
    registry, atomic = _terminal_plan6()
    atomic = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано; {positive}', 1,
    )
    assert module.inspect(make_tree(tmp_path, registry, atomic))['ok']


def test_r34_ukrainian_reopen_then_restart_after_correction(tmp_path):
    import json
    import subprocess
    import sys
    registry, atomic = _terminal_plan6()
    reopened = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано; Поточний статус: НЕ ЗАВЕРШЕНО', 1,
    )
    make_tree(tmp_path, registry, reopened)
    command = [sys.executable, str(SCRIPT), '--resolved-tree', str(tmp_path)]
    first = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert first.returncode == 1 and not first.stderr
    assert json.loads(first.stdout)['ok'] is False
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    second = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert second.returncode == 0 and not second.stderr
    assert json.loads(second.stdout)['ok'] is True


def test_r35_symlink_loop_resolution_runtime_error_is_private_and_fail_closed(tmp_path, monkeypatch, capsys):
    """Python <=3.12 can raise RuntimeError for symlink loops in resolve()."""
    import json
    private_path = tmp_path / 'Users' / 'особисте ім’я' / 'private-profile'
    original_resolve = Path.resolve

    def symlink_loop(path, *args, **kwargs):
        if path == tmp_path:
            raise RuntimeError(f'Symlink loop detected at {private_path}')
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', symlink_loop)
    assert module.main(['--resolved-tree', str(tmp_path)]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report['ok'] is False
    assert report['gate'] == module.GATE
    assert str(private_path) not in output
    assert 'private-profile' not in output


def test_r35_atomic_done_reopened_cannot_pass_while_parent_is_in_progress(tmp_path):
    """Each 2А/2Б/2В is an independent Section, even before parent DONE."""
    atomic = ATOMIC.replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | Статус: DONE — інтегровано; Status: REOPENED |',
    )
    report = module.inspect(make_tree(tmp_path, REGISTRY, atomic))
    assert report['ok'] is False
    assert any('2Б' in failure for failure in report['failures'])


@pytest.mark.parametrize('row,negative', [
    ('2А | WebView2', 'Status: REOPENED'),
    ('2Б | Navigation', 'Поточний статус: НЕ ЗАВЕРШЕНО'),
    ('2В | Native STOP', 'Status: NOT DONE'),
])
def test_r35_independent_atomic_done_reversal_rejected_before_parent_done(tmp_path, row, negative):
    original = next(line for line in ATOMIC.splitlines() if f'| {row} |' in line)
    forged = original.replace('IN_PROGRESS / NOT DONE', f'DONE — інтегровано; {negative}')
    assert not module.inspect(make_tree(tmp_path, REGISTRY, ATOMIC.replace(original, forged)))['ok']


def test_r35_valid_independent_atomic_done_does_not_require_parent_done(tmp_path):
    atomic = ATOMIC.replace(
        '| 2Б | Navigation | IN_PROGRESS / NOT DONE |',
        '| 2Б | Navigation | Статус: DONE — інтегровано; Status: DONE |',
    )
    assert module.inspect(make_tree(tmp_path, REGISTRY, atomic))['ok']

# r36: a second explicit authority field is a newer status, regardless of
# whether its vocabulary was known when this preflight was written.  A finite
# deny-list lets READY_FOR_AUDIT or MERGE_PENDING silently override DONE.
@pytest.mark.parametrize('secondary', [
    'Status: READY_FOR_AUDIT',
    'Status: INTEGRATION_OWNED',
    'Current State: MERGE_PENDING',
    'Latest Status: ON_HOLD',
    'Статус: НА ПЕРЕВІРЦІ',
    'Поточний стан: ОЧІКУЄ_ІНТЕГРАЦІЇ',
    'State: SOURCE_ONLY',
])
def test_r36_unknown_secondary_status_must_fail_closed(tmp_path, secondary):
    registry, atomic = _terminal_plan6()
    forged = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано; {secondary}', 1,
    )
    report = module.inspect(make_tree(tmp_path, registry, forged))
    assert report['ok'] is False
    assert any('2А' in reason for reason in report['failures'])


def test_r36_new_secondary_status_recovery_across_process_restart(tmp_path):
    import json
    import subprocess
    import sys
    registry, atomic = _terminal_plan6()
    forged = atomic.replace(
        'Статус: DONE — інтегровано',
        'Статус: DONE — інтегровано; Status: READY_FOR_AUDIT', 1,
    )
    make_tree(tmp_path, registry, forged)
    command = [sys.executable, str(SCRIPT), '--resolved-tree', str(tmp_path)]
    denied = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert denied.returncode == 1 and json.loads(denied.stdout)['ok'] is False
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    accepted = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert accepted.returncode == 0 and json.loads(accepted.stdout)['ok'] is True


@pytest.mark.parametrize('secondary', [
    'Status: DONE',
    'Current State: DONE — перевірено',
    'Останній статус: DONE (підтверджено)',
])
def test_r36_explicit_secondary_terminal_done_remains_valid(tmp_path, secondary):
    registry, atomic = _terminal_plan6()
    affirmed = atomic.replace(
        'Статус: DONE — інтегровано',
        f'Статус: DONE — інтегровано; {secondary}', 1,
    )
    assert module.inspect(make_tree(tmp_path, registry, affirmed))['ok'] is True

# r41: an uppercase workflow-state token after DONE's separator is a
# contradictory second status even without an explicit Status: assignment.
@pytest.mark.parametrize('secondary', [
    'READY_FOR_AUDIT',
    'MERGE_PENDING',
    'НЕ ЗАВЕРШЕНО',
    'В РОБОТІ',
    'WAIT_DEPENDENCY',
])
def test_r41_bare_machine_state_after_done_is_not_terminal(tmp_path, secondary):
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


def test_r41_ordinary_accepted_done_description_still_terminal(tmp_path):
    assert module._is_terminal_done('Статус: DONE — інтегровано')
    assert module._is_terminal_done('DONE — merged and checked')

# r42: a lowercase workflow-state token and a Ukrainian nonterminal phrase
# must not become a terminal DONE merely by changing its capitalization.
@pytest.mark.parametrize('secondary', [
    'ready_for_audit',
    'merge_pending',
    'wait_dependency',
    'не завершено',
    'в роботі',
    'у роботі',
    'очікує',
    'заблоковано',
    'повторно відкрито',
    'не прийнято',
    'призупинено',
    'перевіряється',
])
def test_r42_bare_lowercase_nonterminal_cannot_certify_done(tmp_path, secondary):
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


def test_r42_lowercase_nonterminal_recovery_after_restart(tmp_path):
    import json
    import subprocess
    import sys
    registry, atomic = _terminal_plan6()
    forged = atomic.replace('Статус: DONE — інтегровано', 'Статус: DONE — не завершено', 1)
    make_tree(tmp_path, registry, forged)
    command = [sys.executable, str(SCRIPT), '--resolved-tree', str(tmp_path)]
    denied = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert denied.returncode == 1 and json.loads(denied.stdout)['ok'] is False
    (tmp_path / module.ATOMIC).write_text(atomic, encoding='utf-8')
    accepted = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert accepted.returncode == 0 and json.loads(accepted.stdout)['ok'] is True


# r44: punctuation-separated lowercase workflow states must fail closed.
@pytest.mark.parametrize('secondary', [
    'ready-for-audit', 'ready.for.audit', 'ready–for–audit',
    'merge-pending', 'merge.pending', 'wait-dependency',
    'не-завершено', 'не.завершено', 'в-роботі', 'у-роботі',
    'повторно-відкрито', 'не-прийнято',
])
def test_r44_punctuated_bare_nonterminal_is_not_done(tmp_path, secondary):
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


def test_r44_punctuation_does_not_reject_ordinary_done_prose():
    assert module._is_terminal_done('DONE — інтегровано')
    assert module._is_terminal_done('DONE — merged and checked')


# r47: Unicode punctuation must not turn an explicit nonterminal state into
# a terminal DONE. This exercises visually confusable separators used in
# machine-style status vocabulary without broadening positive DONE authority.
@pytest.mark.parametrize('secondary', [
    'ready‑for‑audit', 'ready‐for‐audit', 'ready−for−audit',
    'ready·for·audit', 'ready‧for‧audit', 'merge‑pending',
    'wait−dependency', 'не‑завершено', 'не·завершено',
    'в‑роботі', 'повторно‑відкрито', 'не‑прийнято',
])
def test_r47_unicode_punctuated_nonterminal_is_not_done(tmp_path, secondary):
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


def test_r47_unicode_punctuation_in_normal_done_description_remains_valid():
    assert module._is_terminal_done('DONE — інтегровано·перевірено')
    assert module._is_terminal_done('DONE — merged‑and‑checked')
