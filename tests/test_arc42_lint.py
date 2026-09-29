"""Tests for scripts/arc42-lint.py (stdlib unittest, no test dependencies).

    python3 -m unittest discover -s tests
    uv run --with markdown-it-py python -m unittest discover -s tests

The builtin table reader is always tested. When markdown-it-py is importable,
the same tests also run against it, plus builtin/markdown-it parity checks.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT     = Path(__file__).resolve().parent.parent
SCRIPT   = ROOT / "scripts" / "arc42-lint.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("arc42_lint", SCRIPT)
lint = importlib.util.module_from_spec(_spec)
sys.modules["arc42_lint"] = lint  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(lint)

HAS_MARKDOWN_IT = lint.MarkdownIt is not None
PARSERS = [lint.PARSER_BUILTIN] + ([lint.PARSER_MARKDOWN_IT] if HAS_MARKDOWN_IT else [])
LANG = json.loads((ROOT / "scripts" / "languages" / "en.json").read_text(encoding="utf-8"))


def tables(content: str, parser: str) -> list[list[tuple[int, list[str], bool]]]:
    lint.set_parser(parser)
    try:
        return [[(r.line, r.cells, r.is_header) for r in t] for t in lint.parse_tables(content)]
    finally:
        lint.set_parser("auto")


def cell(src: str, parser: str) -> str:
    """Plain text of a single body cell."""
    return tables(f"| h |\n|---|\n| {src} |\n", parser)[0][1][1][0]


# (cell markdown, expected plain text)
CELL_CASES = [
    ("plain", "plain"),
    ("`Order Service`", "Order Service"),
    ("**Order Service**", "Order Service"),
    ("__Order Service__", "Order Service"),
    ("*em* and _em_", "em and em"),
    ("*a **b** c*", "a b c"),
    ("~~old~~ new", "old new"),
    ("[IF-02](#if-02)", "IF-02"),
    ("![alt *x*](i.png) z", "alt x z"),
    ("<https://x.io> and <a@b.co>", "https://x.io and a@b.co"),
    ("a<br>b", "ab"),
    ("&amp; &lt;x&gt; &#35;tag", "& <x> #tag"),
    (r"\*not em\* \# \_x\_", "*not em* # _x_"),
    ("snake_case_name", "snake_case_name"),
    ("`` a`b `` and ` c `", "a`b and c"),
    ("`**not bold**`", "**not bold**"),
    ("**unclosed", "**unclosed"),
    ("`unclosed", "`unclosed"),
    ("Warehouse \\| Logistics", "Warehouse | Logistics"),
    ("`#reliable` Reliability", "#reliable Reliability"),
    ("Efficiency (**#efficient**)", "Efficiency (#efficient)"),
]

# Whole-document cases covering table detection and row boundaries
TABLE_CASES = {
    "basic": "| a | b |\n|---|---|\n| 1 | 2 |\n",
    "no outer pipes": "a | b\n--|--\n1 | 2\n",
    "single column": "| a |\n|---|\n| 1 |\n",
    "aligned delimiter": "| a | b |\n| :-: | --: |\n| 1 | 2 |\n",
    "width mismatch is not a table": "| a | b |\n|---|\n| 1 | 2 |\n",
    "header without pipe is not a table": "a\n|---|\n",
    "short and long rows": "| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n",
    "pipe in code span splits": "| a | b |\n|---|---|\n| `x|y` | z |\n",
    "row without pipes continues": "| a | b |\n|---|---|\n| 1 | 2 |\nplain\n",
    "interrupts paragraph": "para\n| a | b |\n|---|---|\n| 1 | 2 |\n",
    "ended by heading": "| a |\n|---|\n| 1 |\n# Heading\n",
    "ended by quote": "| a |\n|---|\n| 1 |\n> quote\n",
    "ended by list": "| a |\n|---|\n| 1 |\n- item\n",
    "ended by fence": "| a |\n|---|\n| 1 |\n```\ncode\n```\n",
    "ended by rule": "| a |\n|---|\n| 1 |\n***\n",
    "ended by html": "| a |\n|---|\n| 1 |\n<div>x</div>\n",
    "ended by blank line": "| a |\n|---|\n| 1 |\n\n| 2 |\n",
    "table in fence ignored": "```markdown\n| a |\n|---|\n| 1 |\n```\n",
    "table in tilde fence ignored": "~~~\n| a |\n|---|\n| 1 |\n~~~\n",
    "two tables": "| a |\n|---|\n| 1 |\n\ntext\n\n| b | c |\n|---|---|\n| 2 | 3 |\n",
    "table in list item": "- item\n\n  | a | b |\n  |---|---|\n  | 1 | 2 |\n",
}


class TableReaderTests(unittest.TestCase):
    def test_cell_text(self):
        for parser in PARSERS:
            for src, expected in CELL_CASES:
                with self.subTest(parser=parser, cell=src):
                    self.assertEqual(cell(src, parser), expected)

    def test_row_structure(self):
        for parser in PARSERS:
            with self.subTest(parser=parser):
                self.assertEqual(
                    tables(TABLE_CASES["short and long rows"], parser),
                    [[(1, ["a", "b"], True), (3, ["1", ""], False), (4, ["1", "2"], False)]],
                )
                self.assertEqual(tables(TABLE_CASES["table in fence ignored"], parser), [])
                self.assertEqual(
                    tables(TABLE_CASES["pipe in code span splits"], parser)[0][1][1],
                    ["`x", "y`"],
                )

    @unittest.skipUnless(HAS_MARKDOWN_IT, "markdown-it-py not installed")
    def test_builtin_matches_markdown_it(self):
        for name, src in TABLE_CASES.items():
            with self.subTest(case=name):
                self.assertEqual(tables(src, lint.PARSER_BUILTIN),
                                 tables(src, lint.PARSER_MARKDOWN_IT))
        for path in sorted(FIXTURES.rglob("*.md")):
            with self.subTest(fixture=str(path.relative_to(FIXTURES))):
                src = path.read_text(encoding="utf-8")
                self.assertEqual(tables(src, lint.PARSER_BUILTIN),
                                 tables(src, lint.PARSER_MARKDOWN_IT))


class ExtractorTests(unittest.TestCase):
    def run_all_parsers(self, fn, *args):
        results = {}
        for parser in PARSERS:
            lint.set_parser(parser)
            try:
                results[parser] = fn(*args)
            finally:
                lint.set_parser("auto")
        return results

    def test_sec1_tags_anywhere_in_goal_cell(self):
        doc = (
            "| Priority | Goal | Scenario |\n|---|---|---|\n"
            "| 1 | `#reliable` Reliability | #usable is not in column 2 |\n"
            "| 2 | Efficiency (**#efficient**) | x |\n"
            "| 3 | #secure, #safe | x |\n"
            "| x | #operable | not a numbered row |\n"
        )
        for parser, tags in self.run_all_parsers(lint.extract_sec1_quality_tags, doc).items():
            with self.subTest(parser=parser):
                self.assertEqual(tags, {"#reliable", "#efficient", "#secure", "#safe"})

    def test_sec3_formatted_ids(self):
        doc = "| ID | Partner |\n|---|---|\n| `IF-01` | A |\n| **IF-02** | B |\n| IF-3x | C |\n"
        for parser, ids in self.run_all_parsers(lint.extract_sec3_interfaces, doc).items():
            with self.subTest(parser=parser):
                self.assertEqual(ids, {"IF-01": 3, "IF-02": 4})

    def test_sec5_names_and_header_detection(self):
        doc = (
            "| Name | Responsibility | Interfaces |\n|---|---|---|\n"
            "| `Order Service` | x | `IF-01` |\n"
            "| Name Registry | x | [IF-02](#a), IF-03 |\n"
            "\n| Owner | Team | Notes |\n|---|---|---|\n| Not A Block | x | IF-09 |\n"
        )
        for parser, (ifaces, names) in self.run_all_parsers(lint.extract_sec5_data, doc, LANG).items():
            with self.subTest(parser=parser):
                self.assertEqual(names, {"Order Service", "Name Registry"})
                self.assertEqual(ifaces, {"IF-01": 3, "IF-02": 4, "IF-03": 4})

    def test_sec10_formatted_aspirational_rows(self):
        doc = (
            "| ID | Scenario | Current State | Target |\n|---|---|---|---|\n"
            "| `QS-01` | x | *not measured* | y |\n"
            "| QS-02 | x | 250 ms | y |\n"
        )
        for parser, (_, asp) in self.run_all_parsers(lint.extract_sec10_data, doc, LANG).items():
            with self.subTest(parser=parser):
                self.assertEqual(asp, {"QS-01"})

    def test_sec11_formatted_ids(self):
        doc = "| ID | Title |\n|---|---|\n| **RISK-01** | x |\n| `RISK-02` | y |\n| DEBT-01 | z |\n"
        for parser, risks in self.run_all_parsers(lint.extract_sec11_risks, doc).items():
            with self.subTest(parser=parser):
                self.assertEqual(risks, {"RISK-01", "RISK-02"})


class EndToEndTests(unittest.TestCase):
    def run_lint(self, fixture: str, parser: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(FIXTURES / fixture), "--parser", parser,
             "--format", "github"],
            capture_output=True, text=True, cwd=ROOT,
        )

    def test_formatted_docs_are_clean(self):
        for parser in PARSERS:
            with self.subTest(parser=parser):
                result = self.run_lint("formatted", parser)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), "")

    def test_broken_docs_report_every_rule(self):
        outputs = {}
        for parser in PARSERS:
            with self.subTest(parser=parser):
                result = self.run_lint("broken", parser)
                self.assertEqual(result.returncode, 1)
                messages = [line.split("::", 2)[2] for line in result.stdout.splitlines()]
                self.assertEqual(messages, [
                    "IF-03 defined in Section 3 but missing from Section 5 building blocks",
                    "IF-04 referenced in Section 5 but not defined in Section 3 interface table",
                    'Building block "Audit Log" (Section 5) not found in Section 7 deployment view',
                    "Q42 tag #secure used in QS-01 (Section 10) but not present in Section 1.2 quality goals",
                    "RISK-09 referenced in ADR-001 (Section 9 'Risks created') but not found in Section 11 risk matrix",
                    "Aspirational scenario QS-03 (Section 10.3) not referenced in Section 11 risks/debt",
                ])
                outputs[parser] = result.stdout
        self.assertEqual(len(set(outputs.values())), 1, "readers disagree")

    def test_without_markdown_it(self):
        # -S skips site-packages, so markdown-it-py is unavailable even if installed
        def run(*extra: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-S", str(SCRIPT), str(FIXTURES / "formatted"), *extra],
                capture_output=True, text=True, cwd=ROOT,
            )

        auto = run()
        self.assertEqual(auto.returncode, 0, auto.stdout + auto.stderr)
        self.assertIn(f"[English, {lint.PARSER_BUILTIN}]", auto.stdout)

        forced = run("--parser", lint.PARSER_MARKDOWN_IT)
        self.assertEqual(forced.returncode, 2)
        self.assertIn("markdown-it-py is not installed", forced.stderr)


if __name__ == "__main__":
    unittest.main()
