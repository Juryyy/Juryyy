"""Generate the profile stat cards (languages, activity) from the GitHub GraphQL API.

Private and organization repositories count too, but only aggregates come out:
the queries never ask for a repository name, so none can reach the SVGs, the
commit or the log (Actions logs of a public repo are public).

    STATS_TOKEN=... python stats/generate.py [--out assets]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

API = "https://api.github.com/graphql"
OUT = Path(__file__).resolve().parent.parent / "assets"

# Linguist counts these as languages, but they are docs and config, not code.
EXCLUDED = {"TeX", "Mermaid", "Dockerfile", "Procfile", "Batchfile", "Makefile"}
TOP_N = 8
OTHER_COLOR = "#8b949e"
MIN_OTHER_PCT = 0.1

PROFILE_QUERY = """
query {
  viewer {
    id
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

# No name, url or owner on purpose: what is never fetched cannot leak.
REPOS_QUERY = """
query($after: String, $author: ID!) {
  viewer {
    repositories(first: 20, after: $after, isFork: false,
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
        defaultBranchRef {
          target {
            ... on Commit {
              all: history { totalCount }
              mine: history(author: {id: $author}) { totalCount }
            }
          }
        }
      }
    }
  }
}
"""


def graphql(token: str, query: str, variables: dict | None = None, attempts: int = 4) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(API, data=body, headers={
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Juryyy-profile-stats",
    })
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as e:
            # counting commit history is slow, so GitHub times out with 502/504 now and then
            if e.code not in (502, 503, 504) or attempt == attempts:
                raise SystemExit(f"GitHub API returned HTTP {e.code}") from None
            time.sleep(5 * attempt)
    if payload.get("errors"):
        raise SystemExit("GitHub API error: " + "; ".join(e.get("message", "?") for e in payload["errors"]))
    return payload["data"]


def fetch(run_query) -> tuple[list[dict], dict]:
    """All repositories (without names) and the contribution calendar."""
    viewer = run_query(PROFILE_QUERY, None)["viewer"]
    calendar = viewer["contributionsCollection"]["contributionCalendar"]
    repos, after = [], None
    while True:
        page = run_query(REPOS_QUERY, {"after": after, "author": viewer["id"]})["viewer"]["repositories"]
        repos += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return repos, calendar
        after = page["pageInfo"]["endCursor"]


def _commits(repo: dict) -> tuple[int, int]:
    """(my commits, all commits) on the default branch; (0, 0) for an empty repo."""
    target = (repo.get("defaultBranchRef") or {}).get("target") or {}
    return (target.get("mine") or {}).get("totalCount", 0), (target.get("all") or {}).get("totalCount", 0)


def language_totals(repos: list[dict]) -> tuple[dict[str, float], dict[str, str]]:
    """Bytes per language, each repo weighted by my share of its commits.

    One commit out of a hundred in someone else's repo counts one percent of its
    code; a repo without my commits does not count at all.
    """
    totals: dict[str, float] = defaultdict(float)
    colors: dict[str, str] = {}
    for repo in repos:
        mine, total = _commits(repo)
        if not mine or not total:
            continue
        share = mine / total
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            if name in EXCLUDED:
                continue
            totals[name] += edge["size"] * share
            colors[name] = edge["node"]["color"] or OTHER_COLOR
    return dict(totals), colors


def top_languages(totals: dict[str, float], colors: dict[str, str], n: int = TOP_N) -> list[tuple[str, float, str]]:
    """[(name, percent, color)], the n largest plus the rest folded into "Other"."""
    whole = sum(totals.values())
    if not whole:
        return []
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    top = [(name, size / whole * 100, colors[name]) for name, size in ranked[:n]]
    rest = sum(size for _, size in ranked[n:]) / whole * 100
    if rest >= MIN_OTHER_PCT:
        top.append(("Other", rest, OTHER_COLOR))
    return top


def commit_stats(repos: list[dict]) -> tuple[int, int]:
    """(my commits across all repos, number of repos with at least one of them)."""
    mine = [_commits(r)[0] for r in repos]
    return sum(mine), sum(1 for m in mine if m)


def longest_streak(calendar: dict) -> int:
    best = run = 0
    for week in calendar["weeks"]:
        for day in week["contributionDays"]:
            run = run + 1 if day["contributionCount"] else 0
            best = max(best, run)
    return best


# --- SVG --------------------------------------------------------------------

THEMES = {
    "light": {"bg": "#ffffff", "border": "#d0d7de", "title": "#1f2328", "text": "#59636e", "gap": "#ffffff"},
    "dark": {"bg": "#0d1117", "border": "#3d444d", "title": "#f0f6fc", "text": "#9198a1", "gap": "#0d1117"},
}
WIDTH, HEIGHT, PAD = 400, 200, 24
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"


def _card(title: str, body: str, theme: str) -> str:
    c = THEMES[theme]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(title)}">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>text{{font-family:{FONT}}}"
        f".h{{font-size:16px;font-weight:600;fill:{c['title']}}}"
        f".n{{font-size:13px;fill:{c['title']}}}"
        f".p{{font-size:13px;fill:{c['text']}}}"
        f".v{{font-size:26px;font-weight:600;fill:{c['title']}}}"
        f".l{{font-size:12px;fill:{c['text']}}}</style>\n"
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{HEIGHT - 1}" rx="6" '
        f'fill="{c["bg"]}" stroke="{c["border"]}"/>\n'
        f'<text class="h" x="{PAD}" y="36">{escape(title)}</text>\n'
        f"{body}</svg>\n"
    )


def render_languages(langs: list[tuple[str, float, str]], theme: str) -> str:
    bar_w = WIDTH - 2 * PAD
    parts = [f'<clipPath id="bar"><rect x="{PAD}" y="52" width="{bar_w}" height="8" rx="4"/></clipPath>',
             '<g clip-path="url(#bar)">']
    x = float(PAD)
    for name, pct, color in langs:
        w = bar_w * pct / 100
        parts.append(f'<rect x="{x:.2f}" y="52" width="{w:.2f}" height="8" fill="{color}"/>')
        x += w
    # thin gaps between segments, like the language bar on a repo page
    x = float(PAD)
    for _, pct, _ in langs[:-1]:
        x += bar_w * pct / 100
        parts.append(f'<rect x="{x - 1:.2f}" y="52" width="2" height="8" fill="{THEMES[theme]["gap"]}"/>')
    parts.append("</g>")

    col_w = bar_w // 2 + 8
    rows = (len(langs) + 1) // 2
    for i, (name, pct, color) in enumerate(langs):
        cx = PAD + (i // rows) * col_w
        y = 92 + (i % rows) * 22
        parts.append(f'<circle cx="{cx + 5}" cy="{y - 4.5}" r="5" fill="{color}"/>')
        parts.append(f'<text class="n" x="{cx + 16}" y="{y}">{escape(name)}</text>')
        parts.append(f'<text class="p" x="{cx + col_w - 24}" y="{y}" text-anchor="end">{pct:.1f}%</text>')
    return _card("Languages", "\n".join(parts) + "\n", theme)


def render_activity(stats: list[tuple[str, str]], theme: str) -> str:
    """stats = [(value, label)] × 4, laid out 2 × 2."""
    parts = []
    for i, (value, label) in enumerate(stats):
        x = PAD + (i % 2) * (WIDTH - 2 * PAD) // 2
        y = 64 + (i // 2) * 68
        parts.append(f'<text class="v" x="{x}" y="{y + 28}">{escape(value)}</text>')
        parts.append(f'<text class="l" x="{x}" y="{y + 48}">{escape(label)}</text>')
    return _card("Activity", "\n".join(parts) + "\n", theme)


def activity_stats(repos: list[dict], calendar: dict) -> list[tuple[str, str]]:
    commits, repo_count = commit_stats(repos)
    streak = longest_streak(calendar)
    return [
        (f"{calendar['totalContributions']:,}", "Contributions · past year"),
        (f"{commits:,}", "Commits"),
        (f"{repo_count:,}", "Repositories contributed to"),
        (f"{streak} day{'s' if streak != 1 else ''}", "Longest streak · past year"),
    ]


def main(argv: list[str] | None = None, run_query=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    if run_query is None:
        token = os.environ.get("STATS_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("Set STATS_TOKEN (classic PAT with repo + read:org).")
        run_query = lambda query, variables: graphql(token, query, variables)  # noqa: E731

    repos, calendar = fetch(run_query)
    langs = top_languages(*language_totals(repos))
    activity = activity_stats(repos, calendar)

    args.out.mkdir(parents=True, exist_ok=True)
    for theme in THEMES:
        cards = {"languages": render_languages(langs, theme), "activity": render_activity(activity, theme)}
        for name, svg in cards.items():
            # LF everywhere, so a run on Windows does not differ from the Action's
            (args.out / f"{name}-{theme}.svg").write_text(svg, encoding="utf-8", newline="\n")

    # aggregates only: this ends up in a public Actions log
    print(f"{len(repos)} repositories scanned")
    print("languages: " + ", ".join(f"{name} {pct:.1f}%" for name, pct, _ in langs))
    print("activity: " + ", ".join(f"{label} = {value}" for value, label in activity))
    return 0


if __name__ == "__main__":
    sys.exit(main())
