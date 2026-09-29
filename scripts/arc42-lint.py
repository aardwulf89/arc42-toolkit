#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = ["markdown-it-py"]
# ///
"""arc42 cross-section consistency linter.

Usage:
    python scripts/arc42-lint.py [docs_path] [--lang LANG] [--format text|github]
                                 [--strict] [--parser auto|markdown-it|builtin]

Exit codes:
    0  no issues (or only warnings in non-strict mode)
    1  one or more errors found (or warnings with --strict)
    2  invalid invocation (e.g. --parser markdown-it without markdown-it-py installed)

Requires Python 3.8+. Language files live in scripts/languages/ alongside this script.
Default language is English (en). Run with --lang de, fr, it, es, or pt for
other built-in languages, or add a new JSON file to contribute a language.

Markdown tables are read with markdown-it-py when it is importable, and with a
builtin stdlib reader otherwise. Both produce the same cell text for the table
syntax arc42 documents use. `uv run scripts/arc42-lint.py` installs
markdown-it-py on the fly from the inline metadata above; plain
`python scripts/arc42-lint.py` needs no dependencies.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

try:
    from markdown_it import MarkdownIt
except ImportError:  # optional dependency — the builtin reader is used instead
    MarkdownIt = None


# ---------------------------------------------------------------------------
# Language loading
# ---------------------------------------------------------------------------

def load_language(lang_code: str, script_path: Path) -> dict:
    """Load language definition from languages/{lang_code}.json."""
    lang_dir = script_path.parent / "languages"
    lang_file = lang_dir / f"{lang_code}.json"

    if not lang_file.exists():
        available = sorted(p.stem for p in lang_dir.glob("*.json")) if lang_dir.exists() else []
        print(f"arc42-lint: language '{lang_code}' not found.", file=sys.stderr)
        if available:
            print(f"  Available languages: {', '.join(available)}", file=sys.stderr)
        else:
            print(f"  No language files found in {lang_dir}", file=sys.stderr)
            print(f"  Make sure the languages/ directory is present alongside scripts/",
                  file=sys.stderr)
        sys.exit(1)

    with open(lang_file, encoding="utf-8") as f:
        return json.load(f)


def _matches_any(text: str, patterns: list[str]) -> bool:
    """Return True if text matches any pattern (all patterns are case-insensitive regex)."""
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


# ---------------------------------------------------------------------------
# Section detection
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$", re.MULTILINE)


def _section_from_number(heading_text: str) -> int | None:
    """Detect section from the numeric prefix of a heading — language-agnostic.

    Matches: '1. Title', '10 Title', '3: Title', '5.1 Sub-title' (returns 5).
    Only recognises section numbers 1–12.
    """
    m = re.match(r"^(\d+)[.\s:]", heading_text.strip())
    if m:
        num = int(m.group(1))
        if 1 <= num <= 12:
            return num
    return None


def _section_from_keyword(heading_text: str, section_keywords: dict) -> int | None:
    """Detect section from language-specific keyword patterns."""
    for sec_str, patterns in section_keywords.items():
        if _matches_any(heading_text, patterns):
            return int(sec_str)
    return None


def detect_section(path: Path, content: str, lang: dict) -> int | None:
    """Return the arc42 section number for a file, or None if undetermined.

    Detection order:
      1. Numeric digits in filename stem (01–12)
      2. Numeric prefix in the first heading (language-agnostic)
      3. Keyword match in the first heading (language-specific fallback)
    """
    sec = _section_from_filename(path)
    if sec is not None:
        return sec

    for hm in _HEADING_RE.finditer(content):
        heading = hm.group(1)
        num = _section_from_number(heading)
        if num is not None:
            return num
        num = _section_from_keyword(heading, lang["section_keywords"])
        if num is not None:
            return num

    return None


def _section_from_filename(path: Path) -> int | None:
    if re.match(r"adr(?![a-z])", path.stem, re.IGNORECASE):  # adr-5-title.md is an ADR, not Section 5
        return None
    m = re.search(r"(?<!\d)(1[0-2]|0?[1-9])(?!\d)", path.stem)
    return int(m.group(1)) if m else None


def split_monolithic(content: str, lang: dict) -> list[tuple[int, str, int]]:
    """Split a monolithic arc42 file into (section_num, content_slice, line_offset) windows.

    Only headings at the level of the first section heading start a new window,
    so subsections ('## 5.1 ...' under '# 5. ...') stay with their section.
    Consecutive windows for the same section are merged, and headings inside
    fenced code blocks are ignored. line_offset is the 0-based index of the
    window's first line in content.
    """
    segments: list[tuple[int, int, list[str]]] = []
    level: int | None = None
    fence: str | None = None

    for idx, line in enumerate(content.splitlines(keepends=True)):
        in_code = fence is not None
        fence = _update_fence(line, fence)
        m = None if in_code or fence is not None else re.match(r"^(#{1,2})\s+(.+)$", line)
        if m and (level is None or len(m.group(1)) == level):
            heading = m.group(2)
            sec = _section_from_number(heading)
            if sec is None:
                sec = _section_from_keyword(heading, lang["section_keywords"])
            if sec is not None:
                level = len(m.group(1))
                if not segments or segments[-1][0] != sec:
                    segments.append((sec, idx, []))
        if segments:
            segments[-1][2].append(line)

    return [(sec, "".join(lines), offset) for sec, offset, lines in segments]


def section_windows(path: Path, content: str, lang: dict) -> list[tuple[int, str, int]]:
    """Return the (section_num, content, line_offset) windows a file contributes.

    A file named after a section (e.g. 05-building-blocks.md) is that section.
    Otherwise a file whose headings cover two or more sections is split;
    anything else is classified by its first recognisable heading.
    """
    if _section_from_filename(path) is None:
        windows = split_monolithic(content, lang)
        if len({sec for sec, _, _ in windows}) > 1:
            return windows
    sec = detect_section(path, content, lang)
    return [(sec, content, 0)] if sec is not None else []


# ---------------------------------------------------------------------------
# Markdown tables
# ---------------------------------------------------------------------------
#
# Extractors read identifiers from table cells. Cells are reduced to plain text
# first, so `OrderService`, **OrderService** and [OrderService](#bb) all yield
# "OrderService". Two interchangeable readers produce the same TableRow lists:
# markdown-it-py (CommonMark + GFM tables) when installed, and a builtin
# stdlib reader that follows the same GFM table rules.

@dataclass
class TableRow:
    line:      int        # 1-based line number within the parsed content
    cells:     list[str]  # plain-text cell contents, padded/truncated to the header width
    is_header: bool


PARSER_MARKDOWN_IT = "markdown-it"
PARSER_BUILTIN     = "builtin"

_parser = PARSER_MARKDOWN_IT if MarkdownIt is not None else PARSER_BUILTIN
_md = None


def set_parser(choice: str) -> str:
    """Select the table reader ('auto', 'markdown-it' or 'builtin'); return the active one."""
    global _parser
    if choice == "auto":
        _parser = PARSER_MARKDOWN_IT if MarkdownIt is not None else PARSER_BUILTIN
    elif choice == PARSER_MARKDOWN_IT and MarkdownIt is None:
        raise RuntimeError("markdown-it-py is not installed")
    else:
        _parser = choice
    return _parser


def parse_tables(content: str) -> list[list[TableRow]]:
    """Return every table in content as a list of rows (header row first)."""
    if _parser == PARSER_MARKDOWN_IT:
        return _tables_markdown_it(content)
    return _tables_builtin(content)


# --- markdown-it-py reader --------------------------------------------------

def _tables_markdown_it(content: str) -> list[list[TableRow]]:
    global _md
    if _md is None:
        _md = MarkdownIt("commonmark").enable(["table", "strikethrough"])

    tables: list[list[TableRow]] = []
    rows: list[TableRow] = []
    row: TableRow | None = None
    in_head = False
    for tok in _md.parse(content):
        if tok.type == "table_open":
            rows = []
            tables.append(rows)
        elif tok.type == "thead_open":
            in_head = True
        elif tok.type == "thead_close":
            in_head = False
        elif tok.type == "tr_open":
            row = TableRow(tok.map[0] + 1, [], in_head)
            rows.append(row)
        elif tok.type == "tr_close":
            row = None
        elif tok.type == "inline" and row is not None:
            row.cells.append(_plain_text(tok.children or []).strip())
    return tables


def _plain_text(tokens: list) -> str:
    parts: list[str] = []
    for tok in tokens:
        if tok.type in ("text", "text_special", "code_inline"):
            parts.append(tok.content)
        elif tok.type == "image":
            parts.append(_plain_text(tok.children or []))
        elif tok.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return "".join(parts)


# --- builtin stdlib reader --------------------------------------------------

_FENCE_RE       = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_DELIM_CELL_RE  = re.compile(r"^:?-+:?$")
# Lines that start a new block and therefore end a table body (GFM behaviour)
_BLOCK_START_RE = re.compile(
    r"^(?:\#{1,6}(?:\s|$)|>|[-+*]\s|\d{1,9}[.)]\s|`{3,}|~{3,}|<"
    r"|(?:\*[ \t]*){3,}$|(?:-[ \t]*){3,}$|(?:_[ \t]*){3,}$)"
)


def _update_fence(line: str, fence: str | None) -> str | None:
    """Track fenced code blocks: return the fence still open after this line."""
    m = _FENCE_RE.match(line)
    if fence is None:
        return m.group(1) if m else None
    if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
            and not line.strip().lstrip(fence[0]):
        return None
    return fence


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_row(line: str) -> list[str]:
    """Split a table row on unescaped pipes. Code spans do not protect pipes (GFM)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _is_delimiter_row(line: str, width: int) -> bool:
    s = line.strip()
    if not s or s[0] not in "|-:" or set(s) - set("|-: \t"):
        return False
    cells = _split_row(s)
    return len(cells) == width and all(_DELIM_CELL_RE.match(c) for c in cells)


def _tables_builtin(content: str) -> list[list[TableRow]]:
    lines = content.splitlines()
    tables: list[list[TableRow]] = []
    fence: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]

        in_code = fence is not None
        fence = _update_fence(line, fence)
        if in_code or fence is not None:
            i += 1
            continue

        if re.search(r"(?<!\\)\|", line) and i + 1 < len(lines):
            header = _split_row(line)
            if _is_delimiter_row(lines[i + 1], len(header)):
                table_indent = _indent(line)
                width = len(header)
                rows = [TableRow(i + 1, [_strip_inline(c) for c in header], True)]
                i += 2
                while i < len(lines):
                    body = lines[i]
                    if not body.strip() or _indent(body) > table_indent + 3 \
                            or _BLOCK_START_RE.match(body.lstrip()):
                        break
                    cells = (_split_row(body) + [""] * width)[:width]
                    rows.append(TableRow(i + 1, [_strip_inline(c) for c in cells], False))
                    i += 1
                tables.append(rows)
                continue
        i += 1
    return tables


_ASCII_PUNCT  = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_AUTOLINK_RE  = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*|[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)>")
_HTML_TAG_RE  = re.compile(r"<!--.*?-->|</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_LINK_RE      = re.compile(r"!?\[([^\[\]]*)\]\([^()\s]*(?:\s+\"[^\"]*\")?\)")
_ENTITY_RE    = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_EMPHASIS_RES = [
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
    re.compile(r"\*(?=\S)(.+?)(?<=\S)\*"),
    re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
]


def _strip_inline(text: str) -> str:
    """Reduce inline markdown to plain text, matching markdown-it's text output.

    Handles backslash escapes, code spans, autolinks, inline HTML, links,
    images, emphasis, strikethrough and entities. Literal fragments (escapes,
    code, URLs) are swapped for placeholders so later passes cannot alter them.
    """
    protected: list[str] = []

    def protect(s: str) -> str:
        protected.append(s)
        return f"\x00{len(protected) - 1}\x00"

    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n and text[i + 1] in _ASCII_PUNCT:
            out.append(protect(text[i + 1]))
            i += 2
        elif c == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            run = j - i
            close = next((m for m in re.finditer(r"`+", text[j:]) if len(m.group()) == run), None)
            if close is None:
                out.append(protect(text[i:j]))
                i = j
            else:
                code = text[j:j + close.start()]
                if len(code) >= 2 and code[0] == " " and code[-1] == " " and code.strip():
                    code = code[1:-1]
                out.append(protect(code))
                i = j + close.end()
        else:
            out.append(c)
            i += 1
    s = "".join(out)

    s = _AUTOLINK_RE.sub(lambda m: protect(m.group(1)), s)
    s = _HTML_TAG_RE.sub("", s)
    previous = None
    while previous != s:
        previous = s
        s = _LINK_RE.sub(r"\1", s)
        for pattern in _EMPHASIS_RES:
            s = pattern.sub(r"\1", s)
    s = _ENTITY_RE.sub(lambda m: html.unescape(m.group()), s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], s)
    return s.strip()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SectionData:
    sec1_quality_tags:    set[str]            = field(default_factory=set)
    sec3_interfaces:      dict[str, int]       = field(default_factory=dict)
    sec5_interfaces:      dict[str, int]       = field(default_factory=dict)
    sec5_component_names: set[str]             = field(default_factory=set)
    sec7_content:         str | None           = None
    sec7_path:            str                  = ""
    sec7_diagram_text:    str                  = ""
    sec7_missing_diagrams: list[str]           = field(default_factory=list)
    sec9_adr_risks:       dict[str, list[str]] = field(default_factory=dict)
    sec9_adr_paths:       dict[str, str]       = field(default_factory=dict)
    sec9_listed_adrs:     set[str]             = field(default_factory=set)
    sec9_missing_adr_files: list[str]          = field(default_factory=list)
    sec10_quality_tags:   dict[str, str]       = field(default_factory=dict)
    sec10_aspirational:   set[str]             = field(default_factory=set)
    sec11_risks:          set[str]             = field(default_factory=set)
    sec11_content:        str                  = ""
    sec11_path:           str                  = ""
    sec1_path:  str = ""
    sec3_path:  str = ""
    sec5_path:  str = ""
    sec9_path:  str = ""
    sec10_path: str = ""


@dataclass
class LintIssue:
    rule:     str
    severity: str   # "error" | "warning"
    file:     str
    line:     int   # -1 if not line-specific
    message:  str


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def _body_rows(content: str) -> list[TableRow]:
    """All non-header rows of every table in content."""
    return [row for table in parse_tables(content) for row in table if not row.is_header]


def extract_sec1_quality_tags(content: str) -> set[str]:
    """Q42 tags from the Section 1.2 quality goals table (second column).

    Any tag in the cell counts, so '#reliable Reliability' and
    'Reliability (#reliable)' both work.
    """
    tags: set[str] = set()
    for row in _body_rows(content):
        if len(row.cells) >= 2 and re.fullmatch(r"\d+", row.cells[0]):
            tags.update(re.findall(r"(?<!\w)#\w+", row.cells[1]))
    return tags


def extract_sec3_interfaces(content: str) -> dict[str, int]:
    """IF-xx IDs from Section 3 interface table (first column)."""
    result: dict[str, int] = {}
    for row in _body_rows(content):
        if row.cells and re.fullmatch(r"IF-\d+", row.cells[0]):
            result.setdefault(row.cells[0], row.line)
    return result


def extract_sec5_data(content: str, lang: dict) -> tuple[dict[str, int], set[str]]:
    """IF-xx IDs and component names from Section 5 building block table.

    Only extracts from the specific building-block table (the one whose header
    row's first cell matches the language-specific name-column label), ignoring
    any other tables that may appear in the section.
    """
    interfaces: dict[str, int] = {}
    names: set[str] = set()
    header_names = lang["patterns"]["section5_header_names"]

    for table in parse_tables(content):
        header = table[0]
        if len(header.cells) < 3 or not _matches_any(header.cells[0], header_names):
            continue
        for row in table[1:]:
            name, iface_cell = row.cells[0], row.cells[2]
            if not name:
                continue
            names.add(name)
            for if_id in re.findall(r"IF-\d+", iface_cell):
                interfaces.setdefault(if_id, row.line)

    return interfaces, names


def extract_sec9_adr_risks(content: str, lang: dict) -> dict[str, list[str]]:
    """Map ADR_ID → [RISK_IDs] from Section 9 ADR Implications blocks.

    A heading at the ADR heading's level or above ends the current ADR, so
    stray 'risks created' references after the last ADR are never
    mis-attributed; deeper headings (e.g. '### Implications') stay inside it.
    The trailing colon in ADR headings is optional (## ADR-001 Title works).
    """
    result: dict[str, list[str]] = {}
    current_adr: str | None = None
    adr_level = 0
    risks_labels = lang["patterns"]["risks_created_label"]

    for line in content.splitlines():
        heading_m = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_m:
            level = len(heading_m.group(1))
            adr_m = re.search(r"(ADR-\d+)", heading_m.group(2))
            if adr_m and level <= 3:
                current_adr = adr_m.group(1)
                adr_level = level
                result.setdefault(current_adr, [])
            elif level <= adr_level:
                current_adr = None
            continue

        if current_adr and _matches_any(line, risks_labels):
            result[current_adr].extend(re.findall(r"RISK-\d+", line))

    return result


def extract_adr_file_risks(content: str, lang: dict, fallback_id: str) -> dict[str, list[str]]:
    """Map ADR_ID → [RISK_IDs] for a file holding ADRs outside Section 9.

    A file with several ADR headings is read like Section 9. A file with one
    ADR (or none, e.g. adr-tools' '# 1. Record decisions') is a single ADR:
    every 'risks created' line counts, and the ID comes from its heading or,
    failing that, from fallback_id.
    """
    headed = extract_sec9_adr_risks(content, lang)
    if len(headed) > 1:
        return headed
    adr_id = next(iter(headed), fallback_id)
    labels = lang["patterns"]["risks_created_label"]
    risks = [risk for line in content.splitlines() if _matches_any(line, labels)
             for risk in re.findall(r"RISK-\d+", line)]
    return {adr_id: risks}


def extract_sec10_data(content: str, lang: dict) -> tuple[dict[str, str], set[str]]:
    """Extract quality tags and aspirational scenario IDs from Section 10.

    quality_tags:  {QS_ID: "#tag"} from Quality property rows
    aspirational:  QS-xx IDs whose Current State matches a 'not measured' pattern
    """
    quality_tags: dict[str, str] = {}
    aspirational: set[str] = set()
    quality_prop_labels = lang["patterns"]["quality_property_label"]
    not_measured_values = lang["patterns"]["not_measured"]

    current_qs: str | None = None
    for line in content.splitlines():
        qs_match = re.match(r"^#{1,4}\s+(QS-\d+)\s*:", line)
        if qs_match:
            current_qs = qs_match.group(1)
            continue

        # Pick up QS IDs from quality tree lines (lines with box-drawing chars)
        # Restricting to tree lines avoids mis-attributing QS refs in prose or tables
        if re.search(r"[├└│]", line) and current_qs is None:
            tree_match = re.search(r"\b(QS-\d+)\s*:", line)
            if tree_match:
                current_qs = tree_match.group(1)

        # Quality property row: | Quality property | #efficient |
        if current_qs and _matches_any(line, quality_prop_labels):
            tag_m = re.search(r"(#\w+)", line)
            if tag_m:
                quality_tags[current_qs] = tag_m.group(1)

    # Aspirational row: | QS-xx | description | <not measured> | target | ... |
    for row in _body_rows(content):
        if len(row.cells) >= 3 and re.fullmatch(r"QS-\d+", row.cells[0]) \
                and _matches_any(row.cells[2], not_measured_values):
            aspirational.add(row.cells[0])

    return quality_tags, aspirational


def extract_sec11_risks(content: str) -> set[str]:
    """RISK-xx IDs from Section 11 risk matrix (first column)."""
    return {row.cells[0] for row in _body_rows(content)
            if row.cells and re.fullmatch(r"RISK-\d+", row.cells[0])}


# ---------------------------------------------------------------------------
# Document loader
# ---------------------------------------------------------------------------

def load_docs(docs_path: Path, lang: dict) -> SectionData:
    """Read docs_path (a single file, or all .md files below a directory) into SectionData."""
    data = SectionData()

    md_files = [docs_path] if docs_path.is_file() else sorted(docs_path.rglob("*.md"))
    contents = {path: _read(path) for path in md_files}

    # ADR files are kept out of section detection: ADR-001.md or adr-5-title.md
    # must not be mistaken for a section. They are found by their first heading
    # and by the links in Section 9 (e.g. a decision log linking each ID to its file).
    adr_hints = {path: "" for path, content in contents.items()
                 if _section_from_filename(path) is None
                 and re.match(r"ADR-\d+", _first_heading(content) or "")}
    windows = {path: section_windows(path, content, lang)
               for path, content in contents.items() if path not in adr_hints}

    for path, file_windows in list(windows.items()):
        for sec_num, sec_content, _ in file_windows:
            if sec_num != 9:
                continue
            for target, (adr_path, hint) in _linked_adr_files(sec_content, path).items():
                if not adr_path.is_file():
                    data.sec9_missing_adr_files.append(target)
                    continue
                if adr_path not in contents:
                    contents[adr_path] = _read(adr_path)
                adr_hints[adr_path] = adr_hints.get(adr_path) or hint
                windows.pop(adr_path, None)

    for path, file_windows in windows.items():
        for sec_num, sec_content, offset in file_windows:
            _populate(data, sec_num, sec_content, str(path), lang, offset)

    for path in sorted(adr_hints):
        fallback_id = adr_hints[path] or path.stem
        for adr_id, risks in extract_adr_file_risks(contents[path], lang, fallback_id).items():
            data.sec9_adr_risks.setdefault(adr_id, []).extend(risks)
            data.sec9_adr_paths.setdefault(adr_id, str(path))

    return data


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _first_heading(content: str) -> str | None:
    """Text of the first ATX heading outside fenced code, or None."""
    fence: str | None = None
    for line in content.splitlines():
        in_code = fence is not None
        fence = _update_fence(line, fence)
        if in_code or fence is not None:
            continue
        m = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if m:
            return m.group(1)
    return None


_MD_LINK_RE = re.compile(
    r"\[([^\[\]]*)\]\(\s*<?([^()\s<>]+?\.md)(?:#[^()\s<>]*)?>?(?:\s+\"[^\"]*\")?\s*\)",
    re.IGNORECASE,
)
_ADR_REF_RE = re.compile(r"(?<![a-z])adr[-_ ]?0*(\d+)(?!\d)", re.IGNORECASE)


def _linked_adr_files(content: str, src: Path) -> dict[str, tuple[Path, str]]:
    """Local .md files linked from Section 9 that hold ADRs.

    A link counts when its text or file name names an ADR ('ADR-007',
    'adr-7-cache.md') or the file name follows the numbered adr-tools/MADR
    scheme ('0007-use-a-cache.md'). Returns {link target: (path, ADR ID hint)};
    the hint is the ADR ID as written in the link text, if any.
    """
    result: dict[str, tuple[Path, str]] = {}
    for m in _MD_LINK_RE.finditer(content):
        text, target = m.group(1), m.group(2)
        if "://" in target:
            continue
        stem = Path(target).stem
        if not (_ADR_REF_RE.search(text) or _ADR_REF_RE.search(stem) or re.match(r"\d{3,}-", stem)):
            continue
        path = Path(os.path.normpath(src.parent / unquote(target)))
        if path == Path(os.path.normpath(src)):
            continue
        hint = re.search(r"ADR-\d+", text)
        result.setdefault(target, (path, hint.group() if hint else ""))
    return result


# Text-based diagram sources that may name building blocks (PlantUML, Mermaid)
_DIAGRAM_LINK_RE = re.compile(
    r"\]\(\s*<?([^()\s<>]+\.(?:puml|plantuml|pu|iuml|wsd|mmd|mermaid))>?(?:\s+\"[^\"]*\")?\s*\)",
    re.IGNORECASE,
)


def _referenced_diagrams(content: str, src: str) -> tuple[str, list[str]]:
    """Read local diagram files linked from content, resolved relative to src.

    Returns (combined diagram text, list of links whose file does not exist).
    Literal '\\n' line breaks in diagram labels are read as spaces.
    """
    texts: list[str] = []
    missing: list[str] = []
    for target in dict.fromkeys(m.group(1) for m in _DIAGRAM_LINK_RE.finditer(content)):
        if "://" in target:
            continue
        path = Path(src).parent / unquote(target.split("#", 1)[0])
        if path.is_file():
            texts.append(path.read_text(encoding="utf-8", errors="replace").replace("\\n", " "))
        else:
            missing.append(target)
    return "\n".join(texts), missing


def _populate(data: SectionData, sec: int, content: str, src: str, lang: dict,
              line_offset: int = 0) -> None:
    existing_path = {
        1: data.sec1_path, 3: data.sec3_path, 5: data.sec5_path,
        7: data.sec7_path, 9: data.sec9_path, 10: data.sec10_path, 11: data.sec11_path,
    }.get(sec)
    if existing_path and sec != 9:  # Section 9 data is merged, not replaced
        print(
            f"arc42-lint: warning: Section {sec} detected in both '{existing_path}' and '{src}'"
            f" — data from '{src}' will be used.",
            file=sys.stderr,
        )

    if sec == 1:
        data.sec1_quality_tags = extract_sec1_quality_tags(content)
        data.sec1_path = src
    elif sec == 3:
        data.sec3_interfaces = _shift_lines(extract_sec3_interfaces(content), line_offset)
        data.sec3_path = src
    elif sec == 5:
        interfaces, data.sec5_component_names = extract_sec5_data(content, lang)
        data.sec5_interfaces = _shift_lines(interfaces, line_offset)
        data.sec5_path = src
    elif sec == 7:
        data.sec7_content = content
        data.sec7_path = src
        data.sec7_diagram_text, data.sec7_missing_diagrams = _referenced_diagrams(content, src)
    elif sec == 9:
        for adr_id, risks in extract_sec9_adr_risks(content, lang).items():
            data.sec9_adr_risks.setdefault(adr_id, []).extend(risks)
            data.sec9_adr_paths.setdefault(adr_id, src)
        data.sec9_listed_adrs.update(re.findall(r"ADR-\d+", content))
        data.sec9_path = src
    elif sec == 10:
        data.sec10_quality_tags, data.sec10_aspirational = extract_sec10_data(content, lang)
        data.sec10_path = src
    elif sec == 11:
        data.sec11_risks = extract_sec11_risks(content)
        data.sec11_content = content
        data.sec11_path = src


def _shift_lines(ids: dict[str, int], offset: int) -> dict[str, int]:
    """Convert window-relative line numbers to file line numbers."""
    return {key: line + offset for key, line in ids.items()}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def rule_interface_consistency(data: SectionData) -> list[LintIssue]:
    """Rule 1: IF-xx IDs must be identical in Section 3 and Section 5."""
    issues: list[LintIssue] = []

    if not data.sec3_interfaces and not data.sec5_interfaces:
        return issues

    if not data.sec3_interfaces:
        return [LintIssue("RULE-1", "warning", data.sec5_path, -1,
                          "Section 3 not found; cannot verify interface ID consistency with Section 5")]
    if not data.sec5_interfaces:
        return [LintIssue("RULE-1", "warning", data.sec3_path, -1,
                          "Section 5 not found; cannot verify interface ID consistency with Section 3")]

    for if_id in sorted(data.sec3_interfaces.keys() - data.sec5_interfaces.keys()):
        issues.append(LintIssue(
            "RULE-1", "error", data.sec3_path, data.sec3_interfaces[if_id],
            f"{if_id} defined in Section 3 but missing from Section 5 building blocks",
        ))
    for if_id in sorted(data.sec5_interfaces.keys() - data.sec3_interfaces.keys()):
        issues.append(LintIssue(
            "RULE-1", "error", data.sec5_path, data.sec5_interfaces[if_id],
            f"{if_id} referenced in Section 5 but not defined in Section 3 interface table",
        ))
    return issues


def rule_component_in_deployment(data: SectionData) -> list[LintIssue]:
    """Rule 2: Every Section 5 building block name must appear in Section 7.

    Section 7 covers its markdown and any local diagram files it links to.
    Names match as whole words; whitespace inside a name may differ (e.g. a
    line break in the middle of 'Order Service').
    """
    issues: list[LintIssue] = []

    if not data.sec5_component_names:
        return issues

    if data.sec7_content is None:
        return [LintIssue("RULE-2", "warning", data.sec5_path, -1,
                          "Section 7 not found; cannot verify building block deployment coverage")]

    for target in data.sec7_missing_diagrams:
        issues.append(LintIssue(
            "RULE-2", "warning", data.sec7_path, -1,
            f"Diagram '{target}' linked from Section 7 not found; its contents were not checked",
        ))

    sec7_text = data.sec7_content + "\n" + data.sec7_diagram_text
    for name in sorted(data.sec5_component_names):
        pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, name.split())) + r"(?!\w)"
        if not re.search(pattern, sec7_text, re.IGNORECASE):
            issues.append(LintIssue(
                "RULE-2", "error", data.sec5_path, -1,
                f'Building block "{name}" (Section 5) not found in Section 7 deployment view',
            ))
    return issues


def rule_quality_tag_coverage(data: SectionData) -> list[LintIssue]:
    """Rule 3: Every Q42 tag used in Section 10 must appear in Section 1.2 quality goals."""
    issues: list[LintIssue] = []

    if not data.sec10_quality_tags:
        return issues

    if not data.sec1_quality_tags:
        return [LintIssue("RULE-3", "warning", data.sec10_path, -1,
                          "Section 1 not found; cannot verify quality tag coverage")]

    tags_in_10 = set(data.sec10_quality_tags.values())
    for tag in sorted(tags_in_10 - data.sec1_quality_tags):
        qs_ids = sorted(qs for qs, t in data.sec10_quality_tags.items() if t == tag)
        issues.append(LintIssue(
            "RULE-3", "error", data.sec10_path, -1,
            f"Q42 tag {tag} used in {', '.join(qs_ids)} (Section 10) "
            f"but not present in Section 1.2 quality goals",
        ))
    return issues


def rule_adr_risks_in_register(data: SectionData) -> list[LintIssue]:
    """Rule 4: Every RISK-xx in a Section 9 ADR 'Risks created' must exist in Section 11.

    ADRs are read from Section 9 itself and from ADR files (one per ADR, linked
    from Section 9 or headed '## ADR-xxx'). If Section 9 names ADRs but no ADR
    body is found anywhere, the rule warns rather than passing unchecked.
    """
    issues: list[LintIssue] = [
        LintIssue("RULE-4", "warning", data.sec9_path, -1,
                  f"ADR file '{target}' linked from Section 9 not found; its risk references were not checked")
        for target in data.sec9_missing_adr_files
    ]

    if not data.sec9_adr_risks:
        if data.sec9_listed_adrs:
            ids = sorted(data.sec9_listed_adrs, key=lambda i: int(i.split("-")[1]))
            listed = ", ".join(ids[:3]) + (", …" if len(ids) > 3 else "")
            issues.append(LintIssue(
                "RULE-4", "warning", data.sec9_path, -1,
                f"Section 9 names {len(ids)} ADR(s) ({listed}) but no ADR body was found "
                f"(no '## ADR-xxx' heading in Section 9, no linked ADR files); "
                f"cannot verify ADR risk references",
            ))
        return issues

    if not data.sec11_risks and not data.sec11_content:
        return issues + [LintIssue("RULE-4", "warning", data.sec9_path, -1,
                                   "Section 11 not found; cannot verify ADR risk references")]

    for adr_id, risk_ids in sorted(data.sec9_adr_risks.items()):
        for risk_id in sorted(risk_ids):
            if risk_id not in data.sec11_risks:
                issues.append(LintIssue(
                    "RULE-4", "error", data.sec9_adr_paths.get(adr_id, data.sec9_path), -1,
                    f"{risk_id} referenced in {adr_id} (Section 9 'Risks created') "
                    f"but not found in Section 11 risk matrix",
                ))
    return issues


def rule_aspirational_in_risks(data: SectionData) -> list[LintIssue]:
    """Rule 5: Every Section 10 aspirational scenario must be referenced in Section 11."""
    issues: list[LintIssue] = []

    if not data.sec10_aspirational:
        return issues

    if not data.sec11_content:
        return [LintIssue("RULE-5", "warning", data.sec10_path, -1,
                          "Section 11 not found; cannot verify aspirational scenario traceability")]

    for qs_id in sorted(data.sec10_aspirational):
        if not re.search(r"(?<![\w-])" + re.escape(qs_id) + r"(?!\d)", data.sec11_content):
            issues.append(LintIssue(
                "RULE-5", "error", data.sec10_path, -1,
                f"Aspirational scenario {qs_id} (Section 10.3) not referenced in Section 11 risks/debt",
            ))
    return issues


ALL_RULES = [
    rule_interface_consistency,
    rule_component_in_deployment,
    rule_quality_tag_coverage,
    rule_adr_risks_in_register,
    rule_aspirational_in_risks,
]

RULE_DESCRIPTIONS = {
    "RULE-1": "Section 3 ↔ Section 5  Interface IDs (IF-xx)",
    "RULE-2": "Section 5 ↔ Section 7  Building block deployment coverage",
    "RULE-3": "Section 1 ↔ Section 10 Quality goal tag coverage (Q42)",
    "RULE-4": "Section 9 ↔ Section 11 ADR risk references (RISK-xx)",
    "RULE-5": "Section 10 ↔ Section 11 Aspirational scenario traceability",
}


def run_all_rules(data: SectionData) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for rule_fn in ALL_RULES:
        issues.extend(rule_fn(data))
    return issues


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def format_text(issues: list[LintIssue], docs_path: Path, lang_name: str) -> str:
    if not issues:
        return f"arc42-lint: no issues found — all consistency rules passed. [{lang_name}]"

    lines: list[str] = [f"arc42-lint: checking {docs_path} [{lang_name}]\n"]
    by_rule: dict[str, list[LintIssue]] = {}
    for issue in issues:
        by_rule.setdefault(issue.rule, []).append(issue)

    for rule_id in sorted(by_rule):
        desc = RULE_DESCRIPTIONS.get(rule_id, rule_id)
        lines.append(f"[{rule_id}] {desc}")
        for issue in by_rule[rule_id]:
            loc = f"{issue.file}:{issue.line}" if issue.line > 0 else issue.file
            lines.append(f"  [{issue.severity.upper()}] {loc}: {issue.message}")
        lines.append("")

    errors   = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")
    parts = []
    if errors:
        parts.append(f"{errors} error{'s' if errors != 1 else ''}")
    if warnings:
        parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
    lines.append("Found " + ", ".join(parts))
    return "\n".join(lines)


def format_github(issues: list[LintIssue]) -> str:
    lines: list[str] = []
    for issue in issues:
        level = "error" if issue.severity == "error" else "warning"
        title = RULE_DESCRIPTIONS.get(issue.rule, issue.rule)
        loc_parts = []
        if issue.file:
            loc_parts.append(f"file={issue.file}")
        if issue.line > 0:
            loc_parts.append(f"line={issue.line}")
        loc_parts.append(f"title={title}")
        lines.append(f"::{level} {','.join(loc_parts)}::{issue.message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="arc42 cross-section consistency linter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "docs_path", nargs="?", default="docs",
        help="Path to arc42 docs directory (default: docs/)",
    )
    parser.add_argument(
        "--lang", default="en", metavar="LANG",
        help="Documentation language, e.g. en, de, fr, it, es, pt (default: en)",
    )
    parser.add_argument(
        "--format", choices=["text", "github"], default="text",
        help="Output format: 'text' for local use, 'github' for Actions annotations",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Treat warnings as errors (non-zero exit)",
    )
    parser.add_argument(
        "--parser", choices=["auto", PARSER_MARKDOWN_IT, PARSER_BUILTIN], default="auto",
        dest="table_parser",
        help="Markdown table reader: markdown-it-py if installed ('auto'), or force one",
    )
    args = parser.parse_args()

    try:
        table_parser = set_parser(args.table_parser)
    except RuntimeError as exc:
        parser.error(f"--parser {args.table_parser}: {exc}")

    script_path = Path(__file__).resolve()
    lang = load_language(args.lang, script_path)

    docs = Path(args.docs_path)
    if not docs.exists():
        print(f"arc42-lint: '{docs}' does not exist — nothing to lint.")
        sys.exit(0)

    data = load_docs(docs, lang)

    if not any([
        data.sec1_quality_tags, data.sec3_interfaces, data.sec5_interfaces,
        data.sec7_content, data.sec9_adr_risks, data.sec9_listed_adrs,
        data.sec9_missing_adr_files, data.sec10_quality_tags, data.sec11_risks,
    ]):
        print(f"arc42-lint: no arc42 sections detected in '{docs}' — nothing to lint.")
        sys.exit(0)

    issues = run_all_rules(data)

    lang_name = f"{lang.get('name', args.lang)}, {table_parser}"
    if args.format == "github":
        output = format_github(issues)
    else:
        output = format_text(issues, docs, lang_name)

    if output:
        print(output)

    errors   = sum(1 for i in issues if i.severity == "error")
    warnings = sum(1 for i in issues if i.severity == "warning")

    if errors:
        sys.exit(1)
    if args.strict and warnings:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
