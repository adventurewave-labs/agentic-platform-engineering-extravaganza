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

    def test_no_document_quotes_a_monthly_figure_the_model_does_not_produce(self):
        """Containment is only half the question.

        The test above asks whether the right numbers are present. It cannot
        see a *wrong* one sitting next to them -- a stale "$1,010.44/mo" from
        two rate-card revisions ago would satisfy it completely. So every
        dollars-per-month figure in the prose is collected and checked against
        the set the committed artefacts actually contain.
        """
        allowed = {f"${self.rec['costBefore']:,.2f}", f"${self.rec['costAfter']:,.2f}",
                   f"${self.rec['costBefore'] - self.rec['costAfter']:,.2f}"}
        for path in (ROOT / "outputs/final-cost.json", ROOT / "outputs/drift-report.json"):
            for m in re.finditer(r"\$([\d,]+(?:\.\d\d)?)", path.read_text()):
                allowed.add("$" + m.group(1))
        allowed |= {"$400.00", "$400"}          # the budget, from the ServiceRequest
        # Prose rounds: "$602/mo saved" for $601.67 is a fair way to write it,
        # so every allowed figure is also allowed to the nearest dollar. What
        # stays caught is a number with no derivation at all.
        for fig in list(allowed):
            try:
                allowed.add(f"${round(float(fig.lstrip('$').replace(',', ''))):,}")
            except ValueError:
                pass
        wrong = []
        for path in (README, TEMPLATE):
            text = read(path)
            for m in re.finditer(r"(\$[\d,]+(?:\.\d\d)?)\s*/\s*mo", text):
                if m.group(1) not in allowed:
                    line = text[:m.start()].count("\n") + 1
                    wrong.append(f"{path.name}:{line} quotes {m.group(1)}/mo, "
                                 f"which no committed artefact contains")
        self.assertEqual(wrong, [], "cost drift:\n  " + "\n  ".join(wrong))

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


class TestNoHandWrittenTerminalBlocks(unittest.TestCase):
    """Blocks badged "REAL OUTPUT" / "REAL RENDER" must be generated, not typed.

    Three of them used to be hand-marked-up HTML. Every value in them had been
    copied from a real run, which is what made the problem hard to see: they
    were accurate on the day they were pasted and wrong later. The Act V
    transcript had lost the indentation on its continuation lines; the
    SQLInstance was showing 7 of its 13 parameters with the metadata removed
    and the secret name replaced by an ellipsis, under a badge reading REAL
    RENDER.

    The template now carries placeholders and `build_site.py` fills them from
    the program's own output, so the only way to regress is to delete a
    placeholder -- which is what this notices.
    """

    PLACEHOLDERS = ("__ACT5_TRANSCRIPT__", "__TOOLS_PLATFORM_AGENT__",
                    "__SQLINSTANCE_RENDER__")

    def test_the_template_carries_placeholders_not_markup(self):
        template = read(TEMPLATE)
        for name in self.PLACEHOLDERS:
            self.assertIn(name, template,
                          f"{name} is gone from the template -- has a generated "
                          f"block been replaced by hand-written HTML again?")

    def test_every_real_badge_sits_on_a_generated_block(self):
        """A REAL OUTPUT / REAL RENDER badge on a block that is not generated
        is exactly the failure this class exists for, so the badges are counted
        rather than trusted."""
        template = read(TEMPLATE)
        badged = re.findall(
            r'badge-real">(?:REAL OUTPUT|REAL RENDER)</span></div>\s*<pre>([^<]*)',
            template)
        for body in badged:
            self.assertIn(body.strip(), self.PLACEHOLDERS,
                          f"a REAL badge sits above hand-written content: "
                          f"{body.strip()[:60]!r}")


class TestGeneratedBlocksMatchTheirCommands(unittest.TestCase):
    """And the generated blocks must still equal what the commands print.

    The test above only checks the wiring. This one runs the commands.
    """

    @classmethod
    def setUpClass(cls):
        cls.page = ROOT / "index.html"
        if not cls.page.exists():
            raise unittest.SkipTest("index.html not built; run ./run.sh site")
        import sys
        sys.path.insert(0, str(ROOT / "src"))

    @staticmethod
    def _block(page: str, marker: str) -> str:
        import html as H
        i = page.index(marker)
        a = page.index("<pre>", i) + 5
        b = page.index("</pre>", a)
        return H.unescape(re.sub(r"<[^>]+>", "", page[a:b]))

    def _run(self, args):
        import os
        import subprocess
        import sys
        env = dict(os.environ, NORTHWIND_SPEED="0", PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-600:])
        return proc.stdout.rstrip("\n")

    def test_act_five_transcript_is_the_programs_own_output(self):
        import ansi2html
        page = read(self.page)
        live = ansi2html.strip(self._run(["src/goldenpath.py", "--acts", "5"]))
        self.assertEqual(self._block(page, "./run.sh act 5"), live,
                         "the Act V block on the page is not what ./run.sh act 5 prints")

    def test_the_tool_list_is_the_servers_own_output(self):
        import ansi2html
        page = read(self.page)
        live = ansi2html.strip(self._run(
            ["src/platform_mcp.py", "--list-tools", "--identity", "platform-agent"]))
        self.assertEqual(self._block(page, "./run.sh tools platform-agent"), live)

    def test_the_sqlinstance_is_the_whole_rendered_object(self):
        import yaml
        page = read(self.page)
        docs = [d for d in yaml.safe_load_all(
            (ROOT / "outputs/final-manifests.yaml").read_text()) if d]
        sql = [d for d in docs if d.get("kind") == "SQLInstance"]
        self.assertEqual(len(sql), 1)
        shown = self._block(page, "rendered by platform/northwind.provisioners.yaml")
        self.assertEqual(shown, yaml.safe_dump(sql[0], sort_keys=False).rstrip("\n"),
                         "the SQLInstance on the page is not the rendered object")
        # The specific ways the hand-written version was wrong.
        self.assertIn("metadata:", shown)
        self.assertNotIn("...", shown)
        params = shown.split("  parameters:\n", 1)[1]
        self.assertEqual(len(re.findall(r"^    \w[\w.\-]*:", params, re.M)),
                         len(sql[0]["spec"]["parameters"]),
                         "the page is not showing every rendered parameter")


class TestPolicyBundleCounts(unittest.TestCase):
    """The README and the page both describe the size of the Rego.

    They said "25 controls across four bundles". There are three bundles and
    seventeen controls. The 34 rule bodies were right. Nobody had counted in a
    while, and nothing was counting for them.
    """

    @classmethod
    def setUpClass(cls):
        policy = ROOT / "policy"
        cls.bundles = sorted(d.name for d in policy.iterdir()
                             if d.is_dir() and any(d.glob("*.rego")))
        text = "\n".join(f.read_text() for f in policy.rglob("*.rego"))
        cls.controls = sorted(set(re.findall(r"NW-[A-Z]+-\d+", text)))
        cls.bodies = sum(1 for f in policy.rglob("*.rego")
                         for line in f.read_text().splitlines()
                         if line.startswith(("deny contains", "warn contains")))

    WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}

    def test_documents_agree_on_the_bundle_count(self):
        expected = {str(len(self.bundles)), self.WORDS[len(self.bundles)]}
        for path in (README, TEMPLATE, I18N):
            for m in re.finditer(r"across (\w+) bundles", read(path)):
                self.assertIn(m.group(1), expected,
                              f"{path.name} says {m.group(1)} bundles, "
                              f"policy/ has {len(self.bundles)}: {self.bundles}")

    NUMBERS = {
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
        "nineteen": 19, "twenty": 20, "twenty-five": 25, "thirty": 30,
    }

    def test_documents_agree_on_the_control_count(self):
        """The README spells the number out and the page writes digits, so both
        forms are read -- the word form is how "Twenty-five" survived a review
        that corrected the digits elsewhere."""
        for path in (README, TEMPLATE, I18N):
            for m in re.finditer(r"([A-Za-z-]+|\d+) controls across", read(path)):
                token = m.group(1)
                n = int(token) if token.isdigit() else self.NUMBERS.get(token.lower())
                self.assertIsNotNone(
                    n, f"{path.name} writes the control count as {token!r}; add it "
                       f"to TestPolicyBundleCounts.NUMBERS so it stays checked")
                self.assertEqual(n, len(self.controls),
                                 f"{path.name} says {token} controls, "
                                 f"policy/ defines {len(self.controls)}")

    def test_documents_agree_on_the_rule_body_count(self):
        for path in (README, TEMPLATE, I18N):
            for m in re.finditer(r"(\d+) <code>deny</code>|(\d+) `deny`", read(path)):
                n = int(m.group(1) or m.group(2))
                self.assertEqual(n, self.bodies,
                                 f"{path.name} says {n} rule bodies, policy/ has "
                                 f"{self.bodies}")


class TestHeroStatTiles(unittest.TestCase):
    """The five numbers at the top of the page, which nothing was watching.

    `test_denial_count` looks for "42 policy violations" as adjacent text. The
    tiles split the number and its label across two <div>s, so the regex never
    matched them and the largest, most prominent numbers on the page were the
    only unguarded ones. They happened to be right. That is not the same thing.
    """

    @classmethod
    def setUpClass(cls):
        cls.rec = json.loads((ROOT / "outputs/run-record.json").read_text())
        page = read(TEMPLATE)
        block = page[page.index('<div class="stats">'):]
        block = block[:block.index("</div>\n</div>")]
        cls.tiles = re.findall(
            r'<div class="n [a-z]+">([^<]+)</div><div[^>]*class="k">([^<]+)</div>',
            block)

    def test_all_five_tiles_are_found(self):
        self.assertEqual(len(self.tiles), 5, f"parsed {len(self.tiles)}: {self.tiles}")

    def test_the_denial_tile_matches_the_run_record(self):
        value = next(v for v, k in self.tiles if "policy violations" in k)
        self.assertEqual(int(value), self.rec["vibeDenyCount"])

    def test_the_iteration_tile_matches_the_run_record(self):
        label = next(k for v, k in self.tiles if "iterations" in k)
        n = int(re.search(r"(\d+) real iterations", label).group(1))
        self.assertEqual(n, self.rec["goldenPathIterations"])

    def test_the_savings_tile_matches_the_run_record(self):
        value = next(v for v, k in self.tiles if "FinOps gate caught" in k)
        saved = self.rec["costBefore"] - self.rec["costAfter"]
        self.assertEqual(int(value.lstrip("$")), round(saved),
                         f"tile says {value}, record implies ${saved:,.2f}")


class TestPinnedToolVersions(unittest.TestCase):
    """`bin/setup.sh` decides which tool versions exist; the documents repeat
    them. Nothing read setup.sh, so the pins and the prose could diverge."""

    SETUP = ROOT / "bin/setup.sh"

    @classmethod
    def setUpClass(cls):
        text = cls.SETUP.read_text()
        cls.pins = {m.group(1).lower(): m.group(2) for m in re.finditer(
            r"^(\w+)_VERSION=\"?v?([0-9][0-9A-Za-z.\-]*)\"?", text, re.M)}
        if not cls.pins:
            raise unittest.SkipTest("no *_VERSION pins found in bin/setup.sh")

    def test_pins_were_parsed(self):
        self.assertGreaterEqual(len(self.pins), 3, self.pins)

    def test_no_document_quotes_a_version_setup_does_not_pin(self):
        wrong = []
        for tool, pinned in self.pins.items():
            for path in (README, TEMPLATE):
                text = read(path)
                for m in re.finditer(
                        rf"{re.escape(tool)}[^0-9\n]{{0,24}}v?(\d+\.\d+\.\d+)",
                        text, re.I):
                    if m.group(1) != pinned:
                        line = text[:m.start()].count("\n") + 1
                        wrong.append(f"{path.name}:{line} says {tool} "
                                     f"{m.group(1)}, setup.sh pins {pinned}")
        self.assertEqual(wrong, [], "tool-version drift:\n  " + "\n  ".join(wrong))


class TestIllustrativeTicketTile(unittest.TestCase):
    """The one hero tile that is not a measurement.

    Four of the five numbers at the top of the page come from the run record.
    This one comes from `goldenpath.TICKET_TRAIL`, whose own comment says the
    per-step days are invented and only the shape is sourced. Rendered in the
    same style as the other four, it read as measured. It now says
    "illustrative" -- and its two numbers are still held to the code, because a
    scenario being illustrative is not a licence for it to disagree with the
    thing that prints it.
    """

    @classmethod
    def setUpClass(cls):
        import goldenpath
        cls.trail = goldenpath.TICKET_TRAIL
        page = read(TEMPLATE)
        block = page[page.index('<div class="stats">'):]
        cls.block = block[:block.index("</div>\n</div>")]

    def test_the_tile_does_not_present_itself_as_measured(self):
        tile = next(t for t in self.block.split("<div class=\"stat\">")
                    if "ticket trail" in t)
        self.assertIn("illustrative", tile,
                      "the ticket-trail tile is styled like the four measured "
                      "stats; it must say what it is")

    def test_the_days_match_the_trail(self):
        days = sum(t[3] for t in self.trail)
        m = re.search(r'<div class="n amber">(\d+) d</div>', self.block)
        self.assertIsNotNone(m, "the ticket-trail tile changed shape")
        self.assertEqual(int(m.group(1)), round(days),
                         f"tile says {m.group(1)} days, TICKET_TRAIL sums to {days}")

    def test_the_team_count_matches_the_trail(self):
        teams = {t[2] for t in self.trail}
        for path in (TEMPLATE, I18N):
            for m in re.finditer(r"across (\d+) teams|en (\d+) equipos", read(path)):
                n = int(m.group(1) or m.group(2))
                self.assertEqual(n, len(teams),
                                 f"{path.name} says {n} teams, TICKET_TRAIL names "
                                 f"{len(teams)}: {sorted(teams)}")


class TestTranslationKeyIntegrity(unittest.TestCase):
    """Every data-i18n key on the page exists exactly once in the Spanish file.

    `build_site.py` already refuses when the English text disagrees between the
    two. It does not notice a *key* going missing or being defined twice --
    json.loads keeps the last of a duplicate pair silently, so the page would
    render the wrong paragraph under the right heading with nothing going red.
    A careless find-and-replace on a number renamed t103 to t106 while this was
    being written, which is how the gap was found.
    """

    @classmethod
    def setUpClass(cls):
        import json as _json
        pairs = _json.JSONDecoder(object_pairs_hook=lambda p: p).decode(read(I18N))
        cls.keys = [k for k, _ in pairs if not k.startswith("_")]
        cls.used = set(re.findall(r'data-i18n="([^"]+)"', read(TEMPLATE)))

    def test_no_duplicate_keys(self):
        import collections
        dupes = [k for k, c in collections.Counter(self.keys).items() if c > 1]
        self.assertEqual(dupes, [], f"duplicate i18n keys: {dupes}")

    def test_every_key_the_page_uses_is_defined(self):
        missing = sorted(self.used - set(self.keys))
        self.assertEqual(missing, [], f"page uses undefined i18n keys: {missing}")

    def test_no_defined_key_is_unused(self):
        """An orphan is how a rename half-lands."""
        orphans = sorted(set(self.keys) - self.used)
        self.assertEqual(orphans, [], f"i18n keys nothing references: {orphans}")
