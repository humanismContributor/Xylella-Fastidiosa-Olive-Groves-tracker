#!/usr/bin/env python3
"""
Weekly check of OpenAlex for new research on Xylella fastidiosa.

It asks the OpenAlex API for works that mention X. fastidiosa in the title or
abstract and were published in the last LOOKBACK_DAYS days (papers are often
indexed weeks after their publication date, so the window overlaps between
runs). Works not yet in openalex_seen.json are collected into one GitHub issue,
Mediterranean-relevant ones first, so a person can skim them for new findings.
Nothing is added to the map automatically.

The API key comes from the OPENALEX_API_KEY environment variable (a repository
secret in GitHub Actions) and is sent as a Bearer header, never in the URL.
Without it the check still runs, on the small shared anonymous budget.

Standard library only. Usage:
    python openalex_check.py            # check and open an issue (needs GITHUB_TOKEN, GITHUB_REPOSITORY)
    python openalex_check.py --dry-run  # print what would be reported
    python openalex_check.py --from-file works.json --dry-run   # offline test (an OpenAlex /works response)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from eppo_check import USER_AGENT, is_mediterranean, open_issue

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "openalex_seen.json"
API = "https://api.openalex.org/works"
QUERY = "xylella fastidiosa"
LOOKBACK_DAYS = 60
MAX_PAGES = 10          # 100 works per page; a normal window holds a few dozen
MAX_LISTED = 60         # keep the issue body well under GitHub's 65k limit
FIELDS = "id,doi,display_name,publication_date,type,primary_location,authorships,abstract_inverted_index"


def fetch_json(url: str, key: str | None, tries: int = 3) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(f"OpenAlex refused the API key (HTTP {exc.code}). Check the OPENALEX_API_KEY "
                                   "secret, and that the key was activated from the OpenAlex email.") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


def fetch_works(since: dt.date, key: str | None) -> list[dict]:
    out, cursor = [], "*"
    for _ in range(MAX_PAGES):
        params = {"filter": f"title_and_abstract.search:{QUERY},from_publication_date:{since.isoformat()}",
                  "sort": "publication_date:desc", "per_page": "100", "select": FIELDS, "cursor": cursor}
        page = fetch_json(f"{API}?{urllib.parse.urlencode(params)}", key)
        out += page.get("results", [])
        cursor = page.get("meta", {}).get("next_cursor")
        if not cursor or not page.get("results"):
            break
    return out


def short_id(work: dict) -> str:
    return work["id"].rsplit("/", 1)[-1]


def abstract(work: dict) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; put the words back in order."""
    inv = work.get("abstract_inverted_index") or {}
    words = sorted((pos, w) for w, positions in inv.items() for pos in positions)
    return " ".join(w for _, w in words)


def describe(work: dict) -> dict:
    loc = work.get("primary_location") or {}
    authors = [n for a in work.get("authorships") or [] if (n := (a.get("author") or {}).get("display_name"))]
    text = f"{work.get('display_name') or ''} {abstract(work)}"
    return {
        "id": short_id(work),
        "title": work.get("display_name") or "(untitled)",
        "date": work.get("publication_date") or "",
        "type": work.get("type") or "",
        "venue": (loc.get("source") or {}).get("display_name") or "",
        "authors": authors[0] + (" et al." if len(authors) > 1 else "") if authors else "",
        "url": work.get("doi") or loc.get("landing_page_url") or work["id"],
        "med": is_mediterranean(text),
    }


def digest_issue(works: list[dict]) -> tuple[str, str, list[str]]:
    # Mediterranean first, newest first within each group
    works = sorted(sorted(works, key=lambda w: w["date"], reverse=True), key=lambda w: not w["med"])
    med = sum(w["med"] for w in works)

    def line(w: dict) -> str:
        meta = " · ".join(x for x in (w["authors"], w["venue"], w["date"], w["type"]) if x)
        return f"- [ ] **{w['title']}**\n  {meta}\n  {w['url']}"

    listed = works[:MAX_LISTED]
    body = (f"OpenAlex lists {len(works)} new work(s) on Xylella fastidiosa"
            f" ({med} mentioning a Mediterranean country or region in the title or abstract).\n\n"
            + "\n".join(line(w) for w in listed)
            + (f"\n\n…and {len(works) - len(listed)} more; search OpenAlex for the rest." if len(works) > len(listed) else "")
            + "\n\n**To do**\n- [ ] Skim the list (ticked = read)\n"
              "- [ ] New outbreak or host? Add a row to `official_outbreaks.json` "
              "(place, coordinates, year, subspecies, host, source link)\n"
              "- [ ] Close this issue\n")
    title = f"New Xylella literature: {len(works)} work(s), week of {dt.date.today().isoformat()}"
    return title, body, ["literature"] + (["mediterranean"] if med else [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-file", metavar="WORKS_JSON")
    a = ap.parse_args(argv)

    if a.from_file:
        works = json.loads(Path(a.from_file).read_text(encoding="utf-8"))["results"]
    else:
        key = os.environ.get("OPENALEX_API_KEY") or None
        if not key:
            print("OPENALEX_API_KEY is not set; using the shared anonymous budget.")
        works = fetch_works(dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS), key)
    if not works:
        # Dozens of papers appear in any two months; none means the query or
        # the API broke. Fail loudly (GitHub emails the repository owner).
        print(f"OpenAlex returned no works for the last {LOOKBACK_DAYS} days; the query may have stopped working.")
        return 1

    found = [describe(w) for w in works]
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else None
    if seen is None:
        print(f"First run: recording {len(found)} existing works.")
        new = []
    else:
        known = set(seen.get("works", []))
        new = [w for w in found if w["id"] not in known]

    for w in new:
        print(f"NEW{' (Mediterranean)' if w['med'] else ''}: {w['date']} {w['title']}")
    if new:
        t, b, labels = digest_issue(new)
        if not a.dry_run:
            open_issue(t, b, labels)
    elif seen is not None:
        print("Nothing new on OpenAlex.")

    if not a.dry_run:
        SEEN_FILE.write_text(json.dumps({
            "checked": dt.date.today().isoformat(),
            "works": sorted({w["id"] for w in found} | set((seen or {}).get("works", []))),
        }, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
