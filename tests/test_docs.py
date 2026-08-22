#!/usr/bin/env python3
"""
The documentation is held to the same standard as the code.

WHY THIS EXISTS
---------------
`build_site.py` already refuses to render when the English on the page and the
English in `site/i18n.es.json` disagree, so the two languages cannot end up
making different claims. Nothing was watching the other seam: the README and
the page are written by hand, share a couple of dozen numbers, and drifted
apart without anything going red.

An audit found the results. The README said the suite was 15 checks while the
page said 14/14 two panels apart. The README had corrected the OpenChoreo row
to v1.2.3 and three MCP servers; the page still said v1.2.2 and two. A GIF was
quoted at a size it had not been for two commits.

None of that is dangerous. All of it is the thing this repository claims not to
do. So the shared numbers now have exactly one source of truth -- the code, or
the committed artefacts -- and every document that repeats one is checked
against it.
"""

import json
import pathlib
import re
import unittest

import context  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parent.parent

README = ROOT / "README.md"
TEMPLATE = ROOT / "site/index.template.html"
I18N = ROOT / "site/i18n.es.json"
RUNSH = ROOT / "run.sh"
MAKEFILE = ROOT / "Makefile"
DEVCONTAINER = ROOT / ".devcontainer/post-create.sh"

PROSE = [README, TEMPLATE, I18N, RUNSH, MAKEFILE, DEVCONTAINER]


def read(p: pathlib.Path) -> str:
    return p.read_text()


class TestCheckCount(unittest.TestCase):
    """Every document that names the size of the acceptance suite must agree
    with `src/build_report.py`, which is the only place it is really decided."""

    @classmethod
    def setUpClass(cls):
        import build_report
        cls.n = len(build_report.CHECKS)

    def test_the_suite_knows_its_own_size(self):
        self.assertGreater(self.n, 0)

    def test_no_document_claims_a_different_number(self):
        # Deliberately narrow patterns. "14/14 tools listed" is a different
        # fourteen and must not trip this.
        patterns = [
            r"(\d+)\s+acceptance checks",
            r"all (\d+) checks",
            r"the (\d+) checks",
            r"(\d+) checks that",
            r"verify[^\n]{0,40}?#\s*(\d+)/(\d+)",
            r"las (\d+) pruebas",
            r"(\d+)/(\d+)\s+pruebas de aceptaci",
            r"verify-(\d+)%20checks",
            r"Verify:\s*(\d+)\s+checks",
        ]
        wrong = []
        for path in PROSE:
            text = read(path)
            for pat in patterns:
                for m in re.finditer(pat, text):
                    for g in m.groups():
                        if g and int(g) != self.n:
                            line = text[:m.start()].count("\n") + 1
                            wrong.append(f"{path.name}:{line} claims {g}, suite has {self.n}"
                                         f"  — {m.group(0)[:60]!r}")
        self.assertEqual(wrong, [], "check-count drift:\n  " + "\n  ".join(wrong))


class TestUnitTestCount(unittest.TestCase):
    """Same, for the number of unit tests."""

    @classmethod
    def setUpClass(cls):
        # Counted by *loading* the suite, not running it. Running it would
        # re-discover this module and recurse until the heat death of CI.
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT))
        cls.n = suite.countTestCases() or None

    def test_discovery_reports_a_count(self):
        self.assertIsNotNone(self.n, "could not determine the real test count")

    def test_no_document_claims_a_different_number(self):
        wrong = []
        for path in PROSE:
            text = read(path)
            for m in re.finditer(r"(\d+)\s+unit tests", text):
                if int(m.group(1)) != self.n:
                    line = text[:m.start()].count("\n") + 1
                    wrong.append(f"{path.name}:{line} claims {m.group(1)}, discovery finds {self.n}")
        self.assertEqual(wrong, [], "unit-test-count drift:\n  " + "\n  ".join(wrong))


class TestRunRecordFigures(unittest.TestCase):
    """The headline figures live in `outputs/run-record.json`. Any document
    repeating one must repeat the committed value."""

    @classmethod
    def setUpClass(cls):
        cls.rec = json.loads((ROOT / "outputs/run-record.json").read_text())

    def test_denial_count(self):
        n = self.rec["vibeDenyCount"]
        wrong = []
        for path in PROSE:
            text = read(path)
            for m in re.finditer(r"(\d+)\s+(?:real\s+)?policy (?:violations|checks)", text):
                if int(m.group(1)) != n:
                    line = text[:m.start()].count("\n") + 1
                    wrong.append(f"{path.name}:{line} claims {m.group(1)}, record says {n}")
        self.assertEqual(wrong, [], "denial-count drift:\n  " + "\n  ".join(wrong))

    def test_distinct_policy_count(self):
        distinct = len(self.rec["vibeByPolicy"])
        text = read(README)
        for m in re.finditer(r"across (\d+) rules", text):
            self.assertEqual(int(m.group(1)), distinct,
                             f"README says {m.group(1)} rules, record has {distinct}")

    def test_cost_figures(self):
        for value in (self.rec["costBefore"], self.rec["costAfter"]):
            self.assertIn(f"${value:,.2f}", read(README),
                          f"README no longer quotes ${value:,.2f} from the run record")

    def test_iteration_count(self):
        n = self.rec["goldenPathIterations"]
        for m in re.finditer(r"converge[ds]? (?:to zero )?in (\d+) iterations", read(TEMPLATE)):
            self.assertEqual(int(m.group(1)), n)


class TestStackTableParity(unittest.TestCase):
    """The README's stack table and the page's version table describe the same
    projects. They drifted once; this is why they cannot again."""

    def test_every_pinned_version_in_the_readme_appears_on_the_page(self):
        readme = read(README)
        table = readme[readme.index("## The stack, and why each piece is here"):]
        table = table[:table.index("---", 10)]
        page = read(TEMPLATE)
        missing = []
        for version in sorted(set(re.findall(r"\bv\d+\.\d+\.\d+\b", table))):
            if version not in page:
                missing.append(version)
        self.assertEqual(missing, [],
                         "the README pins a version the showcase page does not mention: "
                         + ", ".join(missing))

    def test_the_page_does_not_pin_a_version_the_readme_dropped(self):
        readme, page = read(README), read(TEMPLATE)
        stale = []
        for m in re.finditer(r'<td class="mono">(v\d+\.\d+\.\d+)</td>', page):
            if m.group(1) not in readme:
                line = page[:m.start()].count("\n") + 1
                stale.append(f"index.template.html:{line} pins {m.group(1)}, README does not")
        self.assertEqual(stale, [], "stack-table drift:\n  " + "\n  ".join(stale))


class TestQuotedFileSizes(unittest.TestCase):
    """A size quoted to a decimal place is a fact about a file on disk."""

    def test_gif_sizes(self):
        text = read(README)
        wrong = []
        for m in re.finditer(r"`gifs/(\w+\.gif)`\]\([^)]*\)\s*\((\d+\.\d+)\s*MB\)", text):
            name, claimed = m.group(1), float(m.group(2))
            actual = (ROOT / "gifs" / name).stat().st_size / 1_000_000
            if abs(actual - claimed) > 0.1:
                wrong.append(f"{name}: README says {claimed} MB, file is {actual:.2f} MB")
        self.assertEqual(wrong, [], "GIF size drift:\n  " + "\n  ".join(wrong))


class TestNoDanglingPaths(unittest.TestCase):
    """Every repo-relative path the README points at must exist. A broken link
    in the file that tells people what to trust is its own small lie."""

    def test_readme_links_resolve(self):
        text = read(README)
        missing = []
        for m in re.finditer(r"\]\((?!https?:|#)([^)]+)\)", text):
            target = m.group(1).split("#")[0]
            if not target:
                continue
            if not (ROOT / target).exists():
                line = text[:m.start()].count("\n") + 1
                missing.append(f"README.md:{line} -> {target}")
        self.assertEqual(missing, [], "dangling links:\n  " + "\n  ".join(missing))

    def test_shell_entry_points_referenced_by_the_devcontainer_exist(self):
        """The bootstrap chmod'd a path that had never existed, and `set -e`
        turned that into a codespace with no tools and no complaint."""
        text = read(DEVCONTAINER)
        missing = []
        for m in re.finditer(r"chmod \+x ([^\n]+)", text):
            for token in m.group(1).split():
                if token.startswith("./") and not (ROOT / token[2:]).exists():
                    missing.append(token)
        self.assertEqual(missing, [], f"post-create.sh chmods paths that do not exist: {missing}")


if __name__ == "__main__":
    unittest.main()
