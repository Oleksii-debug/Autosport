#!/usr/bin/env python3
"""Fail-closed Plan-6 atomic closure preflight for a *resolved* merge tree.

This is a source-authority check, not a Windows/UIA/CI/NVDA acceptance oracle.
Supports both in-progress and terminal aggregate states without asserting acceptance.
"""
from __future__ import annotations

import argparse
import html
from html.parser import HTMLParser
import json
import os
import re
import stat
import sys
import unicodedata
from pathlib import Path

REGISTRY = "MULTI_PLAN_CLOSURE_STATE.md"
ATOMIC = "ATOMIC_OPEN_SECTIONS_2026-10-13.md"
REQUIRED_ATOMIC = ("2А", "2Б", "2В")
GATE = "SOURCE_AUTHORITY_ONLY_NOT_WINDOWS_OR_NVDA"
MAX_BYTES = 2_000_000


class AuthorityError(ValueError):
    """An untrusted/missing source-authority input cannot be evaluated."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _check_identity(root: Path, name: str, expected: tuple[int, int, int, int, int]) -> None:
    """Ensure an earlier authority input has not changed while reading its peer."""
    try:
        current = (root / name).lstat()
    except OSError as exc:
        raise AuthorityError(f"{name}: vanished between authority reads") from exc
    if not stat.S_ISREG(current.st_mode) or _identity(current) != expected:
        raise AuthorityError(f"{name}: changed between authority reads")


def _read_regular(root: Path, name: str) -> tuple[str, tuple[int, int, int, int, int]]:
    """Pin the opened regular file; reject symlink swaps and concurrent writes.

    The path is checked both before and after reading. The descriptor, not a
    second path lookup, supplies the bytes. This is an integrity preflight,
    not a substitute for a frozen Git commit/tree identity.
    """
    path = root / name
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise AuthorityError(f"{name}: missing, symlink, or not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise AuthorityError(f"{name}: not a regular opened file")
            if opened.st_size > MAX_BYTES:
                raise AuthorityError(f"{name}: unreasonably large authority file")
            if _identity(before) != _identity(opened):
                raise AuthorityError(f"{name}: file changed before open")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(65536, MAX_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise AuthorityError(f"{name}: unreasonably large authority file")
            after_fd = os.fstat(fd)
            after_path = path.lstat()
            if (not stat.S_ISREG(after_path.st_mode) or
                _identity(opened) != _identity(after_path) or
                _identity(opened) != _identity(after_fd)):
                raise AuthorityError(f"{name}: file changed during read")
            return b"".join(chunks).decode("utf-8", errors="strict"), _identity(opened)
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise AuthorityError(f"{name}: cannot read stable valid UTF-8") from exc


def _mask_fenced_code(text: str) -> str:
    """Keep byte/character offsets while excluding Markdown code examples.

    An example table/heading inside a fenced block is not an authority row.
    This does not attempt to parse arbitrary Markdown: ambiguous/unclosed
    fences hide the remainder rather than granting accidental authority.
    """
    masked: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence_char is None:
            if match:
                fence_char = match.group(1)[0]
                fence_length = len(match.group(1))
                masked.append(re.sub(r"[^\r\n]", " ", line))
            else:
                masked.append(line)
        else:
            if (match and match.group(1)[0] == fence_char and
                len(match.group(1)) >= fence_length and not match.group(2).strip()):
                fence_char = None
                fence_length = 0
            masked.append(re.sub(r"[^\r\n]", " ", line))
    fenced = "".join(masked)
    # HTML comments are invisible, but their delimiters inside a quoted HTML
    # attribute, escaped Markdown punctuation or a backtick code span are
    # literal data. A regex can mask real authority rows after such a marker.
    # Scan in one pass, retaining every source offset and line break.
    source = fenced
    masked_chars = list(source)
    size = len(source)
    # Pre-index backtick runs so thousands of unmatched inline-code markers
    # cannot trigger quadratic forward scans in an untrusted 2 MB document.
    runs: list[tuple[int, int]] = []
    tick = source.find("`")
    while tick >= 0:
        end = tick + 1
        while end < size and source[end] == "`":
            end += 1
        runs.append((tick, end))
        if len(runs) > 65536:
            raise AuthorityError("too many inline-code delimiters in authority")
        tick = source.find("`", end)
    next_run: dict[int, int] = {}
    next_by_length: dict[int, int] = {}
    for start, end in reversed(runs):
        length = end - start
        if length in next_by_length:
            next_run[start] = next_by_length[length]
        next_by_length[length] = end
    i = 0
    while i < size:
        char = source[i]
        if char == "`":
            run_end = i + 1
            while run_end < size and source[run_end] == "`":
                run_end += 1
            # A CommonMark code span exists only with an exact closing run.
            # Never interpret HTML comment syntax inside that span.
            i = next_run.get(i, run_end)
            continue
        if char != "<":
            i += 1
            continue
        # Backslash-escaped angle brackets are displayed literally by
        # Markdown and cannot start an invisible HTML comment.
        backslashes = 0
        j = i - 1
        while j >= 0 and source[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2:
            i += 1
            continue
        if source.startswith("<!--", i):
            close = source.find("-->", i + 4)
            end = size if close < 0 else close + 3
            for j in range(i, end):
                if source[j] not in "\r\n":
                    masked_chars[j] = " "
            i = end
            continue
        # A valid HTML tag may contain <!-- or --> in quoted attributes.
        # Do not interpret these as a comment delimiter. If the tag is
        # unclosed, leave the source unchanged: it cannot authorize a
        # canonical status row and downstream parsing remains fail-closed.
        if i + 1 < size and (source[i + 1].isalpha() or source[i + 1] in "/!?"):
            quote = None
            cursor = i + 1
            tag_limit = min(size, i + 8192)
            while cursor < tag_limit:
                current = source[cursor]
                if quote is not None:
                    if current == quote:
                        quote = None
                elif current in "\"'":
                    quote = current
                elif current == ">":
                    break
                cursor += 1
            if cursor >= tag_limit and cursor < size:
                raise AuthorityError("unbounded HTML tag in authority")
            if cursor < size:
                i = cursor + 1
                continue
            if "<!--" in source[i:cursor]:
                # An unterminated tag containing a comment opener has
                # renderer-dependent semantics. Never let it hide rows.
                raise AuthorityError("ambiguous HTML comment in authority")
        i += 1
    return "".join(masked_chars)


def _split_table_cells(line: str) -> list[str]:
    """Split a GFM table row at unescaped pipes, with optional outer pipes.

    The canonical table *header* remains explicitly pipe-delimited, but GFM
    permits body rows without a leading/trailing pipe. Only an odd number of
    consecutive backslashes escapes a pipe.
    """
    raw = line.strip()
    if not raw or "\n" in raw or "\r" in raw:
        return []
    parts: list[str] = []
    start = 0
    slash_count = 0
    for index, char in enumerate(raw):
        if char == "\\":
            slash_count += 1
            continue
        if char == "|" and slash_count % 2 == 0:
            parts.append(raw[start:index].strip())
            start = index + 1
        slash_count = 0
    parts.append(raw[start:].strip())
    if raw.startswith("|"):
        parts.pop(0)
    if raw.endswith("|"):
        parts.pop()
    return parts


def _three_cells(line: str) -> list[str]:
    """Parse exactly three GFM cells; never accept an appended fourth cell."""
    raw = line.strip()
    if not raw.startswith("|") or not raw.endswith("|"):
        return []
    parts = _split_table_cells(line)
    return parts if len(parts) == 3 else []


def _first_cell(line: str) -> str:
    parts = _split_table_cells(line)
    return parts[0] if parts else ""


def _one_section(text: str, start_pattern: str, next_pattern: str, label: str) -> str:
    matches = list(re.finditer(start_pattern, _mask_fenced_code(text), re.MULTILINE))
    if len(matches) != 1:
        raise AuthorityError(f"{label}: expected one canonical section heading, found {len(matches)}")
    start = matches[0].end()
    following = re.search(next_pattern, _mask_fenced_code(text)[start:], re.MULTILINE)
    end = start + following.start() if following else len(text)
    return text[start:end]


def _canonical_table_lines(section: str, first_column: str) -> list[str]:
    """Bind authority to the first canonical Markdown table, never later prose.

    A narrative note can contain pipe-delimited examples. Such examples are
    not status records, even when they look exactly like a valid table row.
    """
    lines = _mask_fenced_code(section).splitlines()
    # Use the masked view to locate authority rows, but preserve the original
    # status text: HTML comments inside a cell are zero-width in rendered
    # Markdown. Replacing them with spaces would hide N<!-- -->OT DONE.
    source_lines = section.splitlines()
    header = re.compile(r"^ {0,3}\|\s*" + re.escape(first_column) + r"\s*\|\s*(?:State|Зміст)\s*\|")
    positions = [i for i, line in enumerate(lines) if header.match(line)]
    if len(positions) != 1:
        raise AuthorityError(f"{first_column}: expected exactly one canonical table header")
    header_cells = _three_cells(lines[positions[0]])
    expected_second = "State" if first_column == "Section" else "Зміст"
    if (len(header_cells) != 3 or header_cells[0] != first_column or
        header_cells[1] != expected_second or
        (header_cells[2] != "Стан" if first_column == "Розділ" else not header_cells[2])):
        raise AuthorityError(f"{first_column}: malformed canonical table header")
    start = positions[0] + 1
    if start >= len(lines):
        raise AuthorityError(f"{first_column}: missing canonical table separator")
    separator_cells = _three_cells(lines[start])
    if (len(separator_cells) != 3 or
        any(re.fullmatch(r":?-+:?", cell) is None for cell in separator_cells)):
        raise AuthorityError(f"{first_column}: malformed canonical table separator")
    result: list[str] = []
    for index in range(start + 1, len(lines)):
        line = lines[index]
        # GFM permits a body row with *zero* pipes: it renders as a
        # single-cell row padded with two empty cells. Stopping at that
        # line would hide a malformed duplicate section identifier from
        # the authority census. A blank, a heading or four-space-indented
        # code ends this table; an unpiped non-heading line remains a row.
        # Positive DONE still requires an exact three-cell piped record.
        if (not line.strip() or
                not re.match(r"^ {0,3}\S", line) or
                line.lstrip().startswith("#")):
            break
        result.append(source_lines[index])
    return result


def _rows(section: str, key_pattern: str) -> dict[str, list[str]]:
    """Read complete three-cell status records; count malformed keyed rows.

    A prefix match must not grant DONE authority to a four-column row, and
    a malformed duplicate must not disappear from the canonical row census.
    """
    result: dict[str, list[str]] = {}
    for line in _canonical_table_lines(section, "Section"):
        # This helper is scoped to the Plan-6 aggregate table. Do not silently
        # ignore a visually identical parent row with a compatibility digit or
        # invisible format control, even if that row has extra/missing cells.
        raw_key = _first_cell(line)
        # Nonspacing/variation marks (Mn), spacing/ enclosing marks (Mc/Me),
        # and format controls (Cf) cannot be part of a canonical ASCII key.
        # Strip them for *alias detection only*, never to grant authority.
        comparable_key = _comparison_key(raw_key)
        if comparable_key == "2" and raw_key != "2":
            raise AuthorityError("Plan 6 aggregate Section 2: ambiguous section identifier")
        keyed = re.fullmatch(key_pattern, raw_key)
        if not keyed:
            # GFM pads omitted cells in short rows such as ``| 2``.  A
            # duplicate authority key without its next delimiter must still
            # count as a malformed duplicate, not vanish from the census.
            if raw_key == "2":
                result.setdefault("2", []).append("")
            continue
        key = raw_key
        cells = _three_cells(line)
        if len(cells) != 3 or cells[0] != key or not cells[1] or not cells[2]:
            result.setdefault(key, []).append("")
        else:
            result.setdefault(key, []).append(cells[1])
    return result


# This map is deliberately limited to the three Plan-6 authority keys. It is
# for *rejecting* ambiguous identifiers, never for accepting a forged alias.
_ATOMIC_KEY_CONFUSABLES = str.maketrans({
    "A": "А", "a": "А", "Α": "А", "α": "А", "а": "А",
    "B": "В", "b": "В", "Β": "В", "β": "В", "в": "В",
    "б": "Б",
})


def _project_inline_link_labels(value: str, *, preserve_unlinked_brackets: bool = False) -> str:
    """Project Markdown inline-link labels without trusting destinations.

    Markdown allows nested parentheses and escaped delimiters in destinations.
    A flat regex can leave ``[N](url(a))OT DONE`` unprojected even though it
    renders as ``NOT DONE``. This bounded scanner rejects ambiguous reference
    links instead of silently certifying a potentially rendered negative.
    """
    result: list[str] = []
    pos = 0
    while pos < len(value):
        if value[pos] != "[" or (pos > 0 and value[pos - 1] == "\\"):
            result.append(value[pos])
            pos += 1
            continue
        depth = 1
        close = pos + 1
        while close < len(value) and depth:
            if value[close] == "\\":
                close += 2
                continue
            if value[close] == "[":
                depth += 1
            elif value[close] == "]":
                depth -= 1
            close += 1
        if depth:
            # Do not rescan every remaining '[' in a malformed 2 MB cell.
            # A malformed label has no safe rendered authority projection.
            raise AuthorityError("unclosed Markdown label in authority")
        if close >= len(value):
            # A shortcut reference link may be defined elsewhere in the
            # document. Project its label for negative detection only.
            result.append(value[pos:close] if preserve_unlinked_brackets else value[pos + 1:close - 1])
            pos = close
            continue
        label = value[pos + 1:close - 1]
        if value[close] == "[":
            # Reference-link definitions may be elsewhere in the document.
            # Do not accept a terminal DONE without a whole-document parser.
            raise AuthorityError("ambiguous Markdown reference link in authority")
        if value[close] != "(":
            # Could be a shortcut reference link. Never let brackets mask
            # an alias or a visible negative terminal status.
            result.append(value[pos:close] if preserve_unlinked_brackets else label)
            pos = close
            continue
        cursor = close + 1
        parentheses = 1
        while cursor < len(value) and parentheses:
            char = value[cursor]
            if char == "\\":
                cursor += 2
                continue
            if char == "(":
                parentheses += 1
            elif char == ")":
                parentheses -= 1
            cursor += 1
        if parentheses:
            raise AuthorityError("unclosed Markdown link in authority")
        # Markdown link titles (quoted or parenthesized after a destination)
        # can become an accessible description even though the link label is
        # positive. Do not discard that contradictory semantic evidence and
        # accidentally certify terminal DONE. URL-only nested parentheses are
        # still supported; title-bearing links are deliberately ambiguous.
        destination = value[close + 1:cursor - 1]
        if re.search(r"(?<!\\)[\"']", destination) or re.search(r"\s\(", destination):
            raise AuthorityError("Markdown link title in canonical authority")
        result.append(label)
        pos = cursor
    return "".join(result)


class _SemanticOverrideParser(HTMLParser):
    """Detect semantic names from parsed HTML, not angle-bracket regexes.

    In HTML, a quoted ``>`` does not terminate the start tag. Regex-based
    attribute detection can miss a later aria-label and falsely certify DONE.
    The scanner is deliberately conservative: semantic markup is ambiguous
    authority even when its announced value happens to match visible text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name.lower(): value for name, value in attrs if name is not None}
        # Raw HTML can change the *rendered* section identifier without
        # changing its source text: <span hidden>noise</span>2Б visibly
        # names 2Б but the plain-text projection sees noise2Б. CSS classes
        # and inline styles can do the same. Reject such presentation controls
        # rather than granting a terminal DONE with an undetected duplicate.
        if (any(name.startswith("aria-") for name in names)
                or any(name in names for name in
                       ("role", "title", "hidden", "inert", "style", "class"))
                or tag in {"template", "style", "script", "noscript"}):
            self.found = True
        if tag in {"img", "area"} and "alt" in names:
            self.found = True
        # Interactive controls cannot be terminal status evidence. This also
        # avoids interpreting a malformed boolean `type` attribute as a
        # trusted accessible name (HTMLParser returns its value as None).
        if tag in {"input", "button", "select", "textarea"}:
            self.found = True
        # Replaced/embedded elements can suppress their fallback descendants
        # when supported (iframe/canvas/media/object), or render an entirely
        # different accessible/visual tree (SVG).  The raw text projection
        # would count a hidden prefix as visible and miss a forged duplicate
        # Section identifier.  Reject these ambiguous authority cells.
        # Closed <details> and non-open <dialog> suppress descendants;
        # <datalist> is a non-rendered option source. Their fallback text
        # cannot safely participate in the visible key/status census.
        if tag in {"details", "dialog", "datalist"}:
            self.found = True
        if tag in {"iframe", "canvas", "video", "audio", "object", "embed",
                   "svg", "math", "picture", "source", "track"}:
            self.found = True
        # SVG <title> supplies an accessible name without a title attribute.
        if tag == "title":
            self.found = True


def _has_semantic_override(raw: str) -> bool:
    """Reject accessible HTML semantics conflicting with canonical status.

    HTML comments are invisible, and plain data-* attributes remain inert.
    A real HTML parser respects quoted angle brackets and case-folds names.
    Any parse failure is treated as ambiguous authority, never as DONE.
    """
    # Parse the original text: comment delimiters may appear INSIDE quoted
    # attributes. Pre-stripping comments with regex would erase subsequent
    # aria-* attributes and permit a false terminal DONE. HTMLParser itself
    # ignores real comment nodes without losing quoted-attribute context.
    if "<" not in raw:
        return False
    parser = _SemanticOverrideParser()
    try:
        parser.feed(raw)
        parser.close()
    except (ValueError, AssertionError):
        return True
    return parser.found


class _RenderedHtmlText(HTMLParser):
    """Project visible HTML text without misparsing quoted ``>`` in tags.

    The result is used only for *negative* status/alias detection, never as
    positive DONE evidence. Inline tags concatenate text; line-break and
    block tags contribute whitespace, as they do in rendered HTML.
    """

    _BREAK_TAGS = frozenset({
        "br", "hr", "div", "p", "section", "article", "header", "footer",
        "main", "aside", "nav", "ul", "ol", "li", "table", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        # CommonMark angle autolinks are *visible* link text, not HTML
        # elements. HTMLParser otherwise discards <https://.../NOT-DONE>
        # as a fictitious tag and may certify a contradictory DONE cell.
        # A real HTML tag with attributes cannot match this bounded form.
        if raw and re.fullmatch(
            r"<(?:[A-Za-z][A-Za-z0-9+.-]*:[^\s<>]+|[^\s<>@]+@[^\s<>@]+)>",
            raw,
        ):
            self.parts.append(raw[1:-1])
            return
        if tag in self._BREAK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BREAK_TAGS:
            self.parts.append(" ")


def _project_html_text(raw: str) -> str:
    parser = _RenderedHtmlText()
    try:
        parser.feed(raw)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise AuthorityError("ambiguous HTML in canonical authority") from exc
    return "".join(parser.parts)


def _comparison_key(raw: str) -> str:
    """Detect rendered aliases for *rejection*; never grant key authority.

    Inline HTML/comments are invisible in the rendered Markdown key. A
    duplicate ``2<!-- -->А`` must not disappear from the status-row census.
    """
    # C0/C1 controls, format controls and surrogate code points do not have
    # stable rendered/accessible semantics. A second 2<NUL>Б must never
    # disappear from the canonical duplicate-row census.
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in raw):
        raise AuthorityError("control character in canonical section identifier")
    if _has_semantic_override(raw):
        raise AuthorityError("accessible name override in canonical section identifier")
    rendered = html.unescape(raw)
    rendered = _project_html_text(rendered)
    # Markdown formatting is also invisible in a rendered table key. This
    # normalization is used ONLY to reject visually ambiguous aliases, never
    # to grant a nonliteral row key positive status authority. Preserve link
    # labels (including a label embedded in a larger key) but not targets.
    rendered = _project_inline_link_labels(rendered)
    rendered = re.sub(r"[`*_~]", "", rendered)
    return "".join(
        char for char in unicodedata.normalize("NFKC", rendered)
        if unicodedata.category(char) != "Cf"
        and not unicodedata.category(char).startswith("M")
    )


def _possible_required_atomic_key(raw: str) -> str:
    return _comparison_key(raw).translate(_ATOMIC_KEY_CONFUSABLES)


def _atomic_statuses(section: str) -> dict[str, list[str]]:
    """Read the *third* column of the canonical table, never later prose."""
    result: dict[str, list[str]] = {}
    for line in _canonical_table_lines(section, "Розділ"):
        # Even a malformed table row must not hide a visually identical
        # duplicate (e.g. Latin 2A, Greek 2Β, or 2А with a zero-width mark).
        # Inspect only the first cell, without granting any alias authority.
        raw_key = _first_cell(line)
        possible_key = _possible_required_atomic_key(raw_key)
        if possible_key in REQUIRED_ATOMIC and raw_key != possible_key:
            raise AuthorityError(f"Plan 6 {possible_key}: ambiguous section identifier")
        match = re.fullmatch(r"\d+[А-ЯІЇЄҐ]", raw_key)
        if not match:
            # A short GFM row (``| 2Б``) still renders a keyed table row
            # with missing cells.  Never let it hide beside a valid DONE row.
            if raw_key in REQUIRED_ATOMIC:
                result.setdefault(raw_key, []).append("")
            continue
        key = raw_key
        cells = _three_cells(line)
        if len(cells) != 3 or cells[0] != key or not cells[1] or not cells[2]:
            result.setdefault(key, []).append("")
        else:
            result.setdefault(key, []).append(cells[2])
    return result



# GitHub check conclusions are machine enums. A mixed-script word visually
# resembling an unsuccessful enum is ambiguous negative evidence, never proof
# that an earlier DONE was accepted. Only the result token is inspected; a
# Ukrainian narrative or an explicitly historical check remains valid.
_UNSUCCESSFUL_GATE_ENUMS = frozenset({
    "FAILED", "FAILURE", "FAILING", "FAIL", "UNSUCCESSFUL", "PENDING",
    "SKIPPED", "CANCELLED", "CANCELED", "UNKNOWN", "ERROR", "STALE",
    "NEUTRAL", "BLOCKED", "QUEUED", "WAITING", "REQUESTED", "SCHEDULED",
    "COMPLETED", "RETRYING", "INCOMPLETE", "INDETERMINATE", "INVALID",
    "TIMEOUT", "UNAVAILABLE", "REJECTED", "UNVERIFIED",
})


def _spoofed_unsuccessful_enum(word: str) -> bool:
    """Recognize mixed-script near-matches without granting positive authority."""
    word = unicodedata.normalize("NFKC", word).upper()
    if not (any("A" <= c <= "Z" for c in word)
            and any(ord(c) > 127 and c.isalpha() for c in word)):
        return False
    return any(
        len(word) == len(candidate) and all(
            (letter == expected if letter.isascii() else letter.isalpha())
            for letter, expected in zip(word, candidate)
        )
        for candidate in _UNSUCCESSFUL_GATE_ENUMS
    )


def _has_spoofed_failed_gate(value: str) -> bool:
    """Reject a forged check conclusion, retaining historical exemptions."""
    for clause in re.split(r"[;→•◆]", value):
        # Matrix selectors can contain a colon; the *last* colon/equal sign
        # introduces the result. A check label is never an acceptance token.
        separator = max(clause.rfind(":"), clause.rfind("="))
        if separator < 0:
            continue
        label = clause[:separator].strip()
        result = clause[separator + 1:].strip()
        result = re.sub(r"^(?:conclusion|result)\s*[:=]?\s*", "", result,
                        flags=re.IGNORECASE)
        token = re.match(r"([\w-]{3,48})(?=$|[\s,;.!?—–/()])", result)
        if not token or not _spoofed_unsuccessful_enum(token.group(1)):
            continue
        if re.search(r"\b(?:previous|historical|formerly|раніше|попередн\w*)\b",
                     label, re.IGNORECASE):
            continue
        return True
    return False


def _is_terminal_done(value: str) -> bool:
    # A description containing the word DONE is not an acceptance record.
    literal = re.sub(r"^(?:Статус(?: міграції)?\s*:\s*)", "", value).strip()
    # Terminal authority is an exact ASCII marker, not a compatibility-
    # normalized lookalike such as fullwidth ＤＯＮＥ. Unicode evidence after
    # the marker remains valid (including Ukrainian text).
    if not re.match(r"^DONE(?:\s*(?:—|$))", literal):
        return False
    normalized = unicodedata.normalize("NFKC", literal)
    # Decode HTML entities only for *negative* detection: a literal ASCII
    # DONE marker is still required. Markdown renders &#32;, &nbsp; and
    # &#x2d; as spaces/hyphens, so leaving them encoded could hide NOT DONE.
    rendered = html.unescape(normalized)
    if _has_semantic_override(rendered):
        return False
    # Reject ambiguous C0/C1 controls and surrogate code points as well as
    # invisible format controls. NOT<NUL>DONE must never certify DONE.
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in rendered):
        return False
    # Render-time HTML tags and Markdown emphasis can separate NOT and DONE.
    # Remove these only from the rejection oracle, not from accepted evidence.
    # HTML comments do not produce text nodes. Inline tags also have no
    # implicit separator; N<span>OT</span> renders as NOT, not N OT.
    rendered = _project_html_text(rendered)
    # A status cell with dozens of nested/adjacent bracket qualifiers is
    # ambiguous authority. Bound the check-name grammar and its backtracking
    # before parsing; never turn a pathological input into a terminal DONE.
    # Ordinary matrix jobs use a handful of qualifiers. This is a rejection
    # guard only and does not grant any new positive closure authority.
    if rendered.count("[") + rendered.count("(") > 64:
        return False
    # Keep non-link bracketed matrix selectors in a second projection.
    # The primary projection still joins split Markdown labels so negative
    # states such as N[OT] DONE cannot hide behind source punctuation.
    matrix_rendered = _project_inline_link_labels(rendered, preserve_unlinked_brackets=True)
    # Preserve Markdown link *labels* and discard hidden destinations. The
    # destination may contain historical NOT-DONE anchors without displaying
    # a negative status; split labels may render as contiguous NOT DONE.
    rendered = _project_inline_link_labels(rendered)
    rendered = re.sub(r"[`*~]", "", rendered)
    # Preserve the original Ukrainian/English field names and sentence
    # separators for explicit secondary-state detection. The separate
    # negative_check below folds some Cyrillic/Greek letters to Latin and
    # intentionally erases punctuation; neither is suitable for labels.
    status_projection = unicodedata.normalize("NFKC", rendered)
    status_projection = re.sub(r"\\([_])", r"\1", status_projection).replace("_", " ")
    matrix_status_projection = unicodedata.normalize("NFKC", matrix_rendered)
    matrix_status_projection = re.sub(r"\\([_])", r"\1", matrix_status_projection).replace("_", " ")
    negative_check = "".join(
        char for char in unicodedata.normalize("NFKD", rendered)
        if not unicodedata.category(char).startswith("M")
    ).translate(str.maketrans({
        "Ν": "N", "ν": "n", "Н": "N", "н": "n",
        "Ο": "O", "ο": "o", "О": "O", "о": "o",
        "Τ": "T", "τ": "t", "Т": "T", "т": "t",
        "Ε": "E", "ε": "e", "Е": "E", "е": "e",
    }))
    # Punctuation/symbols may be rendered as separators (NOT—DONE).
    # Do not normalize them in the positive DONE authority marker.
    negative_check = "".join(
        " " if unicodedata.category(char)[0] in "PS" else char
        for char in negative_check
    )
    # A second explicit status assignment inside the *same* canonical cell
    # supersedes the leading DONE marker. Without this guard a row such as
    # "DONE — merged; Статус: REOPENED" certifies the parent as terminal even
    # though the authoritative cell explicitly records a later reversal.
    # Restrict this check to assignment syntax after an explicit separator
    # (including commas, parentheses and escaped GFM pipes):
    # historical prose ("previous FAILED CI repaired") is not a new state.
    # The comparison projection already resolves entities, inline markup,
    # punctuation and Unicode lookalikes; backslash-escaped Markdown
    # punctuation is likewise inert in the rendered status label.
    # The status vocabulary is extensible. A deny-list of known negative
    # values is unsafe: "Status: READY_FOR_AUDIT" or "Status: MERGE_PENDING"
    # can silently supersede a stale DONE. Treat *every* later explicit
    # status assignment as authoritative and require a terminal DONE token.
    # A second "Status: DONE" remains valid. Prose about repaired historical
    # failures is not an assignment and is deliberately unaffected.
    secondary_assignments = re.finditer(
        r"(?:^|[;,.!?—–/:()]|\\?\|)\s*(?:СТАТУС(?:\s+МІГРАЦІЇ)?|СТАН|"
        r"(?:ПОТОЧНИЙ|ОСТАННІЙ)\s+(?:СТАТУС|СТАН)|STATUS|STATE|"
        r"(?:CURRENT|LATEST)\s+(?:STATE|STATUS))\s*[:=]\s*",
        status_projection,
        re.IGNORECASE,
    )
    secondary_nonterminal = any(
        re.match(
            r"^DONE(?:\s*$|\s*(?:—|[;,.!?–/:()]|\\?\|))",
            status_projection[assignment.end():],
            re.IGNORECASE,
        ) is None
        for assignment in secondary_assignments
    )
    # A terminal marker followed immediately by a recognized nonterminal
    # workflow state is contradictory even when it does not say NOT DONE.
    # Check the rendered projection, so entity/inline-markup splitting cannot
    # turn a pending state into accepted source authority.
    contradictory_state = re.match(
        # Status vocabulary is checked at the beginning of the rendered
        # terminal description. A state such as WAITING_SECTION2 or
        # REOPENED is never proof of accepted integration, even if a stale
        # DONE marker precedes it. Underscores/punctuation have already
        # been projected to whitespace above.
        r"^DONE\s+(?:WAITING|PARKED|BLOCKED|IN\s*PROGRESS|QUALIFYING|"
        r"INTEGRATION\s*PENDING|PARTIAL\s*EXISTING|OPEN|REOPENED|"
        r"FAILED|CANCELLED|CANCELED|PENDING|NOT\s+(?:STARTED|ACCEPTED)|"
        r"NEEDS\s*REVIEW|CI\s*FAILURE|INVALIDATED)\b",
        negative_check,
        re.IGNORECASE,
    )
    # A machine-style workflow state can follow the DONE dash without a
    # ``Status:`` field.  Such a second uppercase state is not a narrative
    # acceptance description. Reject any uppercase state vocabulary here,
    # including future names not present in a fixed negative-state list.
    # Lowercase prose ("DONE — інтегровано" / "DONE — merged") remains valid.
    bare_machine_state = re.match(
        r"^DONE\s*—\s*[A-ZА-ЯІЇЄҐ][A-ZА-ЯІЇЄҐ0-9_]*"
        r"(?:[ \t-]+[A-ZА-ЯІЇЄҐ0-9_]+)*\b(?=\s*(?:$|[;,.!?—–/:()]))",
        status_projection,
    )
    # Capitalization is not authority: a lowercase machine token or a
    # Ukrainian/English nonterminal phrase is still an explicit contradiction.
    # Check snake_case before the projection erases underscores; use the
    # rendered projection for natural-language statuses and case-fold safely.
    bare_snake_state = re.match(
        r"^DONE\s*—\s*[A-Za-zА-Яа-яІіЇїЄєҐґ0-9]+"
        r"(?:_[A-Za-zА-Яа-яІіЇїЄєҐґ0-9]+)+\b",
        unicodedata.normalize("NFKC", rendered),
    )
    # Rendered status tokens can be separated by punctuation as well as
    # spaces. A lowercase ``ready-for-audit`` or ``не-завершено`` must not
    # bypass the fail-closed check merely because it uses hyphens or dots.
    # Keep the positive DONE marker unchanged; this is rejection-only.
    # Unicode hyphen/minus/middle-dot variants render as separators too.
    # A fixed ASCII punctuation class would silently accept a forged
    # nonterminal status such as DONE — ready‑for‑audit.
    state_sep = r"[^\w]+"
    bare_nonterminal_phrase = re.match(
        r"^DONE\s*—\s*(?:"
        + rf"READY{state_sep}FOR{state_sep}AUDIT|"
        + rf"MERGE{state_sep}PENDING|WAIT{state_sep}DEPENDENCY|"
        + rf"НЕ{state_sep}ЗАВЕРШЕНО|[ВУ]{state_sep}РОБОТІ|"
        + rf"ПОВТОРНО{state_sep}ВІДКРИТО|НЕ{state_sep}ПРИЙНЯТО|"
        + rf"НЕ{state_sep}(?:ВИКОНАНО|ЗАВЕРШЕНО|ПІДТВЕРДЖЕНО|ГОТОВО|ПРИЙНЯТО)|"
        + rf"НЕВИКОНАНО|НЕЗАВЕРШЕНО|СКАСОВАНО|ВІДХИЛЕНО|"
        + rf"ПОТРЕБУЄ{state_sep}(?:ПЕРЕВІРКИ|ТЕСТІВ|ДОКАЗІВ)|"
        + rf"ЧЕКАЄ{state_sep}(?:ЗЛИТТЯ|ПЕРЕВІРКИ|ПРИЙНЯТТЯ)|"
        + rf"ОЧІКУЄ{state_sep}(?:ЗЛИТТЯ|ПЕРЕВІРКИ|ПРИЙНЯТТЯ)|"
        + rf"У{state_sep}ПРОЦЕСІ|НЕМАЄ{state_sep}ДОКАЗІВ|"
        + rf"NOT{state_sep}(?:DONE|IMPLEMENTED|MERGED|VERIFIED|READY|ACCEPTED)|"
        + rf"NEEDS{state_sep}(?:TESTS|REVIEW|EVIDENCE)|"
        + rf"AWAITING{state_sep}(?:REVIEW|MERGE|TESTS)|"
        + rf"PENDING{state_sep}(?:MERGE|REVIEW|TESTS)|"
        + r"UNVERIFIED|REJECTED|BROKEN|"
        + r"ОЧІКУЄ|ЗАБЛОКОВАНО|ПРИЗУПИНЕНО|ПЕРЕВІРЯЄТЬСЯ)\b",
        status_projection,
        re.IGNORECASE,
    )
    # A positive description cannot override a later explicit negative qualifier.
    trailing_negative = re.search(
        r"(?:[;,():—–/·]|\b(?:АЛЕ|BUT|ТА|AND)\b)\s*(?:АЛЕ|BUT|ТА|AND)?\s*(?:"
        + rf"НЕ{state_sep}(?:ВИКОНАНО|ЗАВЕРШЕНО|ПІДТВЕРДЖЕНО|ПЕРЕВІРЕНО|ГОТОВО|ПРИЙНЯТО)|"
        + rf"НЕВИКОНАНО|НЕЗАВЕРШЕНО|СКАСОВАНО|ВІДХИЛЕНО|"
        + rf"ПОТРЕБУЄ{state_sep}(?:ПЕРЕВІРКИ|ТЕСТІВ|ДОКАЗІВ)|"
        + rf"ЧЕКАЄ{state_sep}(?:ЗЛИТТЯ|ПЕРЕВІРКИ|ПРИЙНЯТТЯ)|"
        + rf"ОЧІКУЄ{state_sep}(?:ЗЛИТТЯ|ПЕРЕВІРКИ|ПРИЙНЯТТЯ)|"
        + rf"NOT{state_sep}(?:DONE|IMPLEMENTED|MERGED|VERIFIED|READY|ACCEPTED)|"
        + rf"PENDING{state_sep}(?:MERGE|REVIEW|TESTS)|"
        + rf"AWAITING{state_sep}(?:MERGE|REVIEW|TESTS)|"
        + rf"NEEDS{state_sep}(?:TESTS|REVIEW|EVIDENCE)|"
        + r"UNVERIFIED|REJECTED|BROKEN)\b",
        status_projection,
        re.IGNORECASE,
    )
    # A completed-action verb followed immediately by a nonterminal
    # qualifier is a contradiction even without punctuation or "and".
    # "DONE — merged not verified" otherwise bypasses the separator-based
    # guard.  Keep this scoped to an adjacent completion claim so historical
    # notes ("previous FAILED CI repaired") remain valid.
    adjacent_negative = re.search(
        r"\b(?:MERGED|INTEGRATED|ІНТЕГРОВАНО|ПРИЙНЯТО|ЗЛИТО)\s+(?:"
        r"NOT\s+(?:VERIFIED|ACCEPTED|MERGED|IMPLEMENTED|READY|DONE)|"
        r"UNVERIFIED|PENDING\s+(?:REVIEW|MERGE|TESTS)|"
        r"AWAITING\s+(?:REVIEW|MERGE|TESTS)|"
        r"NEEDS\s+(?:REVIEW|TESTS|EVIDENCE)|"
        r"НЕ\s+(?:ПЕРЕВІРЕНО|ПІДТВЕРДЖЕНО|ЗАВЕРШЕНО|ПРИЙНЯТО|ВИКОНАНО)|"
        r"ОЧІКУЄ\s+(?:ПЕРЕВІРКИ|ЗЛИТТЯ|ПРИЙНЯТТЯ))\b",
        status_projection,
        re.IGNORECASE,
    )
    # A successful CI/Windows Candidate check does not override a failed
    # independent gate. Keep this named vocabulary aligned with actual
    # hosted/local acceptance checks (Endurance, UIA, packaging, recovery,
    # install/update/restart and smoke/E2E). These strings are rejection-only;
    # they cannot turn an otherwise incomplete Section into DONE.
    gate_name = (
        r"(?:(?:WINDOWS\s+)?(?:CI|CANDIDATE|BUILD|ENDURANCE|PACKAGING|"
        r"SMOKE|E2E|UIA|RECOVERY|INSTALL|UPDATE|RESTART)|"
        # Other independent product-acceptance gates must not be masked by
        # an earlier CI success. Keep these rejection-only: a PASS here
        # cannot itself establish Section completion.
        r"SECURITY|ACCESSIBILITY|INTEGRITY|RELEASE|MIGRATIONS?|BACKUP|"
        r"RESTORE|LICENSE|SBOM|DEPENDENC(?:Y|IES)|SAST|SUPPLY\s+CHAIN|"
        r"TESTS?|ТЕСТИ|ПЕРЕВІРКИ)"
    )
    # GitHub check-run *status* is not a successful *conclusion*.  For
    # example COMPLETED without a SUCCESS conclusion, or REQUESTED/WAITING,
    # cannot qualify a section. Normalize common hyphen/underscore variants
    # for rejection only; they never become positive acceptance evidence.
    state_join = r"[\s_-]+"
    gate_bad_result = (
        r"(?:FAILED|FAILURE|FAILING|FAIL|UNSUCCESSFUL|PENDING|SKIPPED|"
        r"CANCELLED|CANCELED|UNKNOWN|ERROR|STALE|NEUTRAL|BLOCKED|QUEUED|"
        r"WAITING|REQUESTED|SCHEDULED|COMPLETED|RETRYING|INCOMPLETE|"
        r"INDETERMINATE|INVALID|TIMEOUT|UNAVAILABLE|"
        + rf"NOT{state_join}(?:RUN|STARTED|APPLICABLE)|"
        + rf"REQUIRES{state_join}ACTION|TIMED{state_join}OUT|"
        + rf"ACTION{state_join}REQUIRED|STARTUP{state_join}FAILURE|"
        + rf"IN{state_join}PROGRESS|NEEDS{state_join}APPROVAL|"
        + rf"AWAITING{state_join}APPROVAL|"
        + r"ПРОВАЛЕНО|НЕ\s+ПРОЙДЕНО|ОЧІКУЮТЬ)"
    )
    # Adjacent contradiction: DONE — merged; Endurance: FAILED.
    adjacent_check_failure = re.search(
        r"\b(?:MERGED|INTEGRATED|ІНТЕГРОВАНО|ПРИЙНЯТО|ЗЛИТО)\b"
        r"\s*(?:(?:[,;:—–/]|\bBUT\b|\bАЛЕ\b)\s*)?"
        + gate_name + r"[\s:=—–]+" + gate_bad_result + r"\b",
        status_projection,
        re.IGNORECASE,
    )
    # Later contradiction: DONE — merged; CI: SUCCESS; Endurance: FAILED.
    # The boundary excludes historical narrative like "previous Endurance:
    # FAILED, repaired" when it is not a new explicit gate clause.
    later_gate_failure = re.search(
        # A GFM/HTML status can separate gate clauses with any visible
        # punctuation or symbol (bullet, middle dot, ampersand, arrow),
        # not just ASCII sentence punctuation. Match a named gate and a
        # non-success conclusion only; historical prose remains excluded.
        r"(?:[^\w\s]|\b(?:BUT|AND|АЛЕ|ТА)\b)\s*"
        + gate_name + r"[\s:=—–]+" + gate_bad_result + r"\b",
        status_projection,
        re.IGNORECASE,
    )
    # Rendered HTML breaks and plain whitespace can separate consecutive
    # independent gate results without punctuation.  A successful check
    # cannot mask a following failed check merely because the separator was
    # a space or <br>.  This is rejection-only, never positive evidence.
    unseparated_followup_failure = re.search(
        gate_name + r"[\s:=—–]+(?:SUCCESS|PASSED|PASS|ПРОЙДЕНО)\s+"
        + gate_name + r"[\s:=—–]+" + gate_bad_result + r"\b",
        status_projection,
        re.IGNORECASE,
    )
    # Do not let a closed vocabulary of gate names silently certify an
    # unfamiliar GitHub check (e.g. CodeQL, Checks, an organization policy).
    # A new explicit "Name: FAILED" clause is independent negative evidence
    # regardless of the name. Historical clauses explicitly marked previous
    # or historical are not current gate conclusions. This remains a
    # rejection-only check: no named PASS can establish terminal DONE.
    any_bad_result = (
        gate_bad_result[:-1] +
        r"|CHANGES\s+REQUESTED|REJECTED|UNVERIFIED|NOT\s+VERIFIED)"
    )
    # GitHub Actions matrix job display names are not plain words: they may
    # contain parenthesized OS/Python parameters, commas, dots and bracketed
    # matrix selectors (e.g. "test (windows-latest, 3.12): FAILURE").  These
    # characters belong to the check name, not to a narrative exemption.
    # Keep a bounded name length and require an explicit colon/equal sign;
    # positive DONE still requires independent authoritative evidence.
    # Do not admit sentence punctuation into the *outer* label: doing so
    # would consume "merged. previous CI" as one current check and falsely
    # reject a repaired historical failure. Matrix punctuation is allowed
    # only within a single bounded parenthesized/bracketed suffix.
    check_label = (r"[\w][\w /-]{0,60}?"
                   # GitHub Actions and third-party checks may append multiple
                   # matrix/required/policy qualifiers. A fixed suffix count
                   # makes a third qualifier hide a current FAILED check and
                   # incorrectly certifies DONE. Each group is length-bounded;
                   # the status-cell qualifier census above rejects >64.
                   r"(?:\s*(?:\([^()\r\n]{1,70}\)|\[[^\[\]\r\n]{1,70}\])){0,64}"
                   # The matrix projection retains unlinked [required] suffixes;
                   # admit one bounded plain-text trailing qualifier too.
                   r"(?:\s+[\w-]{1,40})?")
    generic_failed_clause = re.compile(
        r"(?:[;,.!?—–/•·&+→◆]|\b(?:AND|BUT|ТА|АЛЕ)\b)\s*"
        + r"(?P<label>" + check_label + r")\s*(?::|=|→)\s*"
        + any_bad_result + r"\b",
        re.IGNORECASE,
    )
    generic_failed_followup = re.compile(
        r"\b(?:SUCCESS|PASSED|PASS|ПРОЙДЕНО)\s+"
        + r"(?P<label>" + check_label + r")\s*(?::|=|→)\s*"
        + any_bad_result + r"\b",
        re.IGNORECASE,
    )
    # A GitHub check display name is not limited to alphanumeric tokens:
    # names such as ``node@20``, ``build #2`` and ``checks::linux`` are
    # legitimate. The structured matcher above handles nested matrix
    # parameters; this bounded fallback catches arbitrary punctuation in
    # an independently delimited check clause. Never let an unknown check
    # name hide an explicit unsuccessful conclusion behind a prior PASS.
    # Semicolons separate clauses; the label itself is limited to 160 chars
    # and cannot consume a later clause. The historical exemption below
    # still applies to explicitly previous/repaired check evidence.
    # GitHub's check summaries can use "CodeQL — FAILURE" or
    # "CodeQL: conclusion FAILURE" rather than "CodeQL: FAILURE".
    # Require whitespace around dash/slash separators to distinguish them
    # from ordinary punctuation inside a check's display name.  The label
    # remains bounded and cannot consume a semicolon-delimited next check.
    punctuated_failed_clause = re.compile(
        r"[;→•◆]\s*"
        + r"(?P<label>[^\r\n;=→]{1,160}?)\s*"
        + r"(?::|=|→|\s+[—–/-]\s+)\s*"
        + r"(?:(?:conclusion|result)\s*[:=]?\s*)?"
        + any_bad_result + r"\b",
        re.IGNORECASE,
    )
    # Some CI summaries omit punctuation between a check name and its
    # conclusion ("CodeQL FAILURE"). Accept this as *negative* evidence
    # only at a semicolon-delimited clause boundary. Never treat a bare
    # successful result as positive authority. Historical qualifiers are
    # handled by the same explicit-exemption logic below.
    bare_failed_clause = re.compile(
        r";\s*(?P<label>[^\r\n;]{1,160}?)\s+"
        + any_bad_result + r"\b",
        re.IGNORECASE,
    )
    def current_failure(match: re.Match[str]) -> bool:
        label = match.group("label").strip().casefold()
        # The punctuation fallback may span an earlier successful check
        # and a later explicitly historical one within one semicolon clause.
        # That historical failure is not a current failed conclusion.
        if re.search(
            r"\b(?:success|passed|pass|пройдено)\s+"
            r"(?:and|but|та|але)\s+"
            r"(?:previous|historical|formerly|раніше|історичн|попередн)",
            label,
        ):
            return False
        # The SUCCESS-followup matcher may consume an ordinary conjunction
        # before an explicitly historical label ("and previous CodeQL").
        # Strip it only when the following word is a historical qualifier;
        # "and CodeQL: FAILURE" must still block terminal acceptance.
        label = re.sub(
            r"^(?:and|but|та|але)\s+(?=(?:previous|historical|formerly|раніше|історичн|попередн))",
            "", label,
        )
        return not label.startswith((
            "previous ", "historical ", "formerly ",
            "раніше ", "історичн", "попередн",
        ))
    spoofed_failed_gate = _has_spoofed_failed_gate(status_projection)
    unknown_failed_gate = (
        any(current_failure(match) for match in generic_failed_clause.finditer(matrix_status_projection))
        or any(current_failure(match) for match in generic_failed_followup.finditer(matrix_status_projection))
        or any(current_failure(match) for match in punctuated_failed_clause.finditer(matrix_status_projection))
        or any(current_failure(match) for match in bare_failed_clause.finditer(matrix_status_projection))
    )
    return (bool(re.match(r"^DONE(?:\s*(?:—|$))", normalized))
            and bare_machine_state is None
            and bare_snake_state is None
            and bare_nonterminal_phrase is None
            and trailing_negative is None
            and adjacent_negative is None
            and adjacent_check_failure is None
            and later_gate_failure is None
            and unseparated_followup_failure is None
            and not unknown_failed_gate
            and not spoofed_failed_gate
            and contradictory_state is None
            and not secondary_nonterminal
            and not re.search(r"\bNOT\s*DONE\b", negative_check, re.IGNORECASE))


def inspect(root: Path) -> dict[str, object]:
    try:
        root = root.resolve(strict=True)
    except RuntimeError as exc:
        # Python <=3.12 may raise RuntimeError for a cyclic symlink path.
        # Its original message can contain the caller's private workspace.
        raise AuthorityError("resolved-tree: invalid or cyclic path") from exc
    if not root.is_dir():
        raise AuthorityError("root must be a directory")
    registry, registry_identity = _read_regular(root, REGISTRY)
    atomic, atomic_identity = _read_regular(root, ATOMIC)
    # A file may be replaced after its own descriptor was closed but before
    # its peer was read. Fail closed on mixed-epoch source authority.
    _check_identity(root, REGISTRY, registry_identity)
    _check_identity(root, ATOMIC, atomic_identity)
    failures: list[str] = []
    if not re.match(r"\A## Перебудова незакритих розділів — 2026-10-13\s*\n", registry):
        failures.append("missing 2026-10-13 owner directive at start of registry")
    if ATOMIC not in registry.split("# Autosport — Multi-Plan Closure State", 1)[0]:
        failures.append("owner directive does not reference canonical atomic map")
    if not re.match(r"\A# Autosport — повний реєстр самостійних розділів", atomic):
        failures.append("atomic map has no canonical registry title")
    parent_state: str | None = None
    atomic_states: dict[str, str] = {}
    try:
        plan6 = _one_section(registry, r"^### Plan 6\s+—[^\n]*$", r"^### Plan \d+\s+—", "registry Plan 6")
        parent = _rows(plan6, r"\d+").get("2", [])
        if len(parent) != 1 or parent[0] not in {"IN_PROGRESS_NOT_DONE", "DONE"}:
            failures.append("Plan 6 aggregate Section 2 must have exactly one recognized state row")
        else:
            parent_state = parent[0]
    except AuthorityError as exc:
        failures.append(str(exc))
    try:
        plan6_atomic = _one_section(atomic, r"^## План 6:\s*\d+ самостійних розділів\s*$", r"^## План \d+:", "atomic Plan 6")
        if not re.search(r"^ {0,3}\|\s*Розділ\s*\|\s*Зміст\s*\|\s*Стан\s*\|", plan6_atomic, re.MULTILINE):
            failures.append("atomic Plan 6 section table header missing")
        atomic_rows = _atomic_statuses(plan6_atomic)
        for key in REQUIRED_ATOMIC:
            values = atomic_rows.get(key, [])
            if len(values) != 1 or not values[0]:
                failures.append(f"Plan 6 {key}: expected one valid three-column row, found {len(values)}")
            else:
                atomic_states[key] = values[0]
                # 2А/2Б/2В are independently closable Sections. A stale
                # leading DONE contradicted by a later explicit REOPENED or
                # NOT DONE must fail even while the legacy parent is active.
                literal = re.sub(r"^(?:Статус(?: міграції)?\s*:\s*)", "", values[0]).strip()
                if (re.match(r"^DONE(?:\s*(?:—|$))", literal)
                        and not _is_terminal_done(values[0])):
                    failures.append(f"Plan 6 {key}: contradictory atomic DONE status")
        if parent_state == "DONE" and len(atomic_states) == len(REQUIRED_ATOMIC):
            incomplete = [key for key, value in atomic_states.items() if not _is_terminal_done(value)]
            if incomplete:
                failures.append("Plan 6 aggregate DONE conflicts with unfinished atomic rows: " + ", ".join(incomplete))
    except AuthorityError as exc:
        failures.append(str(exc))
    return {"ok": not failures, "gate": GATE, "required_sections": list(REQUIRED_ATOMIC), "failures": failures}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resolved-tree", type=Path, required=True, help="Checked-out resolved integration tree, not stale PR head")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.resolved_tree)
    except AuthorityError as exc:
        report = {"ok": False, "gate": GATE, "failures": [str(exc)]}
    except OSError:
        # Path.resolve(strict=True) and OS permission errors can embed a
        # private Windows profile or workspace path in their exception text.
        # The CLI output is evidence that may be exported or attached to CI.
        report = {"ok": False, "gate": GATE,
                  "failures": ["resolved-tree: unavailable or inaccessible"]}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
