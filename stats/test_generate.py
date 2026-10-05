import io
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate as g  # noqa: E402


def repo(langs, mine, total):
    return {
        "languages": {"edges": [{"size": s, "node": {"name": n, "color": c}} for n, s, c in langs]},
        "defaultBranchRef": {"target": {"mine": {"totalCount": mine}, "all": {"totalCount": total}}},
    }


def calendar(counts, total=None):
    days = [{"date": f"d{i}", "contributionCount": c} for i, c in enumerate(counts)]
    weeks = [{"contributionDays": days[i:i + 7]} for i in range(0, len(days), 7)]
    return {"totalContributions": sum(counts) if total is None else total, "weeks": weeks}


class LanguageTotals(unittest.TestCase):
    def test_weighted_by_my_share_of_commits(self):
        totals, _ = g.language_totals([
            repo([("TypeScript", 1000, "#3178c6")], mine=1, total=100),
            repo([("Python", 500, "#3572A5")], mine=10, total=10),
        ])
        self.assertAlmostEqual(totals["TypeScript"], 10)
        self.assertAlmostEqual(totals["Python"], 500)

    def test_repo_without_my_commits_or_branch_is_skipped(self):
        empty = {"languages": {"edges": [{"size": 9, "node": {"name": "Go", "color": "#00ADD8"}}]},
                 "defaultBranchRef": None}
        totals, _ = g.language_totals([repo([("Java", 900, "#b07219")], mine=0, total=50), empty])
        self.assertEqual(totals, {})

    def test_docs_and_config_are_excluded(self):
        totals, _ = g.language_totals([repo([("TeX", 900, None), ("Dockerfile", 50, None),
                                             ("Vue", 100, "#41b883")], mine=1, total=1)])
        self.assertEqual(set(totals), {"Vue"})

    def test_missing_color_falls_back(self):
        _, colors = g.language_totals([repo([("Mako", 10, None)], mine=1, total=1)])
        self.assertEqual(colors["Mako"], g.OTHER_COLOR)


class TopLanguages(unittest.TestCase):
    def test_top_n_plus_other_sums_to_hundred(self):
        totals = {f"L{i}": float(100 - i) for i in range(12)}
        colors = {name: "#000000" for name in totals}
        top = g.top_languages(totals, colors, n=8)
        self.assertEqual(len(top), 9)
        self.assertEqual(top[0][0], "L0")
        self.assertEqual(top[-1][0], "Other")
        self.assertAlmostEqual(sum(p for _, p, _ in top), 100)

    def test_tiny_rest_is_not_shown_as_other(self):
        top = g.top_languages({"A": 99999.0, "B": 1.0}, {"A": "#1", "B": "#2"}, n=1)
        self.assertEqual([name for name, _, _ in top], ["A"])

    def test_nothing_to_show(self):
        self.assertEqual(g.top_languages({}, {}), [])


class Activity(unittest.TestCase):
    def test_commit_stats(self):
        repos = [repo([], 5, 10), repo([], 0, 3), repo([], 2, 2), {"languages": {"edges": []}, "defaultBranchRef": None}]
        self.assertEqual(g.commit_stats(repos), (7, 2))

    def test_longest_streak_crosses_weeks(self):
        self.assertEqual(g.longest_streak(calendar([0, 1, 0, 0, 1, 1, 1, 1, 0, 2])), 4)
        self.assertEqual(g.longest_streak(calendar([0, 0])), 0)

    def test_values_are_formatted(self):
        stats = g.activity_stats([repo([], 2108, 3000)], calendar([1] + [0] * 6, total=1232))
        self.assertEqual(stats[0][0], "1,232")
        self.assertEqual(stats[1][0], "2,108")
        self.assertEqual(stats[3][0], "1 day")


class Rendering(unittest.TestCase):
    LANGS = [("C++", 50.0, "#f34b7d"), ("<script>", 30.0, "#000000"), ("Other", 20.0, g.OTHER_COLOR)]

    def test_svgs_are_valid_xml_and_escaped(self):
        for theme in g.THEMES:
            svg = g.render_languages(self.LANGS, theme)
            ET.fromstring(svg)
            self.assertNotIn("<script>", svg)
            self.assertIn("&lt;script&gt;", svg)
            ET.fromstring(g.render_activity([("1", "a"), ("2", "b"), ("3", "c"), ("4", "d")], theme))

    def test_legend_fits_in_card(self):
        langs = [(f"Lang{i}", 10.0, "#000000") for i in range(9)]
        svg = g.render_languages(langs, "light")
        ys = [float(y) for y in re.findall(r'<text class="n" x="[\d.]+" y="([\d.]+)"', svg)]
        self.assertEqual(len(ys), 9)
        self.assertLess(max(ys), g.HEIGHT - 8)


class Privacy(unittest.TestCase):
    def test_repository_query_never_asks_for_identity(self):
        q = g.REPOS_QUERY
        for field in ("nameWithOwner", "url", "owner", "description", "homepageUrl", "resourcePath"):
            self.assertNotRegex(q, rf"\b{field}\b")
        # the only "name" is the language's
        self.assertEqual(len(re.findall(r"\bname\b", q)), 1)
        self.assertIn("node { name color }", q)


class EndToEnd(unittest.TestCase):
    def test_main_writes_four_cards_and_logs_only_aggregates(self):
        pages = [
            {"viewer": {"repositories": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                         "nodes": [repo([("TypeScript", 800, "#3178c6")], 3, 4)]}}},
            {"viewer": {"repositories": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                         "nodes": [repo([("Python", 200, "#3572A5")], 1, 1)]}}},
        ]
        calls = []

        def fake(query, variables):
            calls.append(variables)
            if query is g.PROFILE_QUERY:
                return {"viewer": {"id": "U_1", "contributionsCollection": {
                    "contributionCalendar": calendar([1, 1, 0, 1])}}}
            return pages.pop(0)

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(g.main(["--out", tmp], run_query=fake), 0)
            files = sorted(p.name for p in Path(tmp).iterdir())
            for f in Path(tmp).iterdir():
                ET.fromstring(f.read_text(encoding="utf-8"))
        self.assertEqual(files, ["activity-dark.svg", "activity-light.svg",
                                 "languages-dark.svg", "languages-light.svg"])
        self.assertEqual(calls[1], {"after": None, "author": "U_1"})
        self.assertEqual(calls[2], {"after": "c1", "author": "U_1"})
        self.assertIn("2 repositories scanned", out.getvalue())
        self.assertIn("TypeScript", out.getvalue())


if __name__ == "__main__":
    unittest.main()
