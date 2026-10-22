#!/usr/bin/env python3
"""
Weekly check of OpenAlex, an open index of scholarly publications, for new papers on
Xylella fastidiosa.

New papers are sorted into topics that matter for this project (spread and outbreaks
in the Mediterranean, detection and remote sensing, hosts and vectors) and posted as
one GitHub issue per week, so a person can decide what to act on. The newest papers
are also kept in data/literature.json, which the app lists. Nothing here changes the
map: a paper is a lead, not an official record.

Cost: two or three search calls per run, well inside OpenAlex's free allowance. An API
key is optional; with the repository secret OPENALEX_API_KEY set, it is sent along.

Usage
    python literature_check.py            # check and open the weekly issue (needs GITHUB_TOKEN)
    python literature_check.py --dry-run  # print what would be posted
    python literature_check.py --from-file page.json --dry-run   # offline test

Standard library only.
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

from eppo_check import open_issue

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "data" / "literature.json"
API = "https://api.openalex.org/works"
QUERY = "xylella fastidiosa"
USER_AGENT = "xylella-watch/1.0 (weekly literature check; github.com)"
PER_PAGE = 100
MAX_PAGES = 5            # newest 500 works of this and last year; plenty for a weekly check
KEEP_RECENT = 100        # papers listed in the app
FIRST_RUN_DAYS = 30      # the first run reports the last month, then only new papers
SELECT = ("id,doi,display_name,publication_date,type,primary_location,authorships,"
          "open_access,abstract_inverted_index")

# Mediterranean places: (text to look for, name to show)
PLACES = [(s.lower(), n) for s, n in [
    ("Mediterranean", "Mediterranean"), ("Italy", "Italy"), ("Italian", "Italy"), ("Apulia", "Puglia"),
    ("Puglia", "Puglia"), ("Salento", "Puglia"), ("Calabria", "Calabria"), ("Basilicata", "Basilicata"),
    ("Tuscany", "Tuscany"), ("Toscana", "Tuscany"), ("Lazio", "Lazio"), ("Sicily", "Sicily"), ("Sardinia", "Sardinia"),
    ("Spain", "Spain"), ("Spanish", "Spain"), ("Balearic", "Balearic Islands"), ("Mallorca", "Balearic Islands"),
    ("Majorca", "Balearic Islands"), ("Alicante", "Alicante"), ("Valencia", "Valencia"), ("Extremadura", "Extremadura"),
    ("Andalusia", "Andalusia"), ("France", "France"), ("French", "France"), ("Corsica", "Corsica"),
    ("Occitan", "Occitanie"), ("Provence", "Provence"), ("Portugal", "Portugal"), ("Portuguese", "Portugal"),
    ("Greece", "Greece"), ("Greek", "Greece"), ("Crete", "Greece"), ("Malta", "Malta"), ("Cyprus", "Cyprus"),
    ("Croatia", "Croatia"), ("Slovenia", "Slovenia"), ("Montenegro", "Montenegro"), ("Albania", "Albania"),
    ("Turkey", "Türkiye"), ("Türkiye", "Türkiye"), ("Syria", "Syria"), ("Lebanon", "Lebanon"), ("Israel", "Israel"),
    ("Palestin", "Palestine"), ("Jordan", "Jordan"), ("Egypt", "Egypt"), ("Libya", "Libya"), ("Tunisia", "Tunisia"),
    ("Algeria", "Algeria"), ("Morocco", "Morocco"),
]]

TOPICS = {
    "Spread, outbreaks and surveillance": [
        "outbreak", "spread", "first report", "first detection", "new record", "survey", "surveillance",
        "epidemiolog", "invasion", "demarcated", "eradication", "containment", "dispersal", "risk",
    ],
    "Detection and remote sensing": [
        "remote sensing", "satellite", "sentinel", "hyperspectral", "multispectral", "thermal", "uav",
        "drone", "airborne", "ndvi", "spectral", "machine learning", "deep learning", "detection",
    ],
    "Hosts, vectors and control": [
        "vector", "philaenus", "spittlebug", "insect", "host", "resistan", "toleran", "cultivar",
        "control", "treatment", "biocontrol",
    ],
}


def fetch_json(params: dict, tries: int = 3) -> dict:
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params = {**params, "api_key": key}
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403):         # bad parameter or refused: retrying won't help
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"OpenAlex unreachable: {last}")


def fetch_recent(today: dt.date, fetch=fetch_json) -> list[dict]:
    """Works matching the query from last year and this year, newest first."""
    base = {"search": QUERY, "filter": f"publication_year:{today.year - 1}-{today.year}",
            "per_page": PER_PAGE, "select": SELECT}
    sorts = ["publication_date:desc", None]       # fall back to relevance if sorting is refused
    for sort in sorts:
        params = {**base, **({"sort": sort} if sort else {})}
        try:
            works = []
            for page in range(1, MAX_PAGES + 1):
                data = fetch({**params, "page": page})
                batch = data.get("results", [])
                works.extend(batch)
                if len(batch) < PER_PAGE:
                    break
            return works
        except urllib.error.HTTPError as exc:
            if sort is None or exc.code != 400:
                raise
    return []


def abstract_text(inv: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}; rebuild the text."""
    if not inv:
        return ""
    pos = [(p, w) for w, ps in inv.items() for p in ps]
    return " ".join(w for _, w in sorted(pos))


def summarize(work: dict) -> dict:
    loc = work.get("primary_location") or {}
    src = (loc.get("source") or {}).get("display_name") or ""
    authors = [((a.get("author") or {}).get("display_name") or "") for a in work.get("authorships") or []]
    authors = [a for a in authors if a]
    text = f"{work.get('display_name') or ''} {abstract_text(work.get('abstract_inverted_index'))}".lower()
    topics = [t for t, words in TOPICS.items() if any(w in text for w in words)]
    places = sorted({name for stem, name in PLACES if stem in text})
    doi = work.get("doi") or ""
    return {
        "id": (work.get("id") or "").rsplit("/", 1)[-1],
        "title": " ".join((work.get("display_name") or "(untitled)").split()),
        "date": work.get("publication_date") or "",
        "type": work.get("type") or "",
        "source": src,
        "authors": (authors[0] + (" et al." if len(authors) > 1 else "")) if authors else "",
        "url": doi or work.get("id") or "",
        "open_access_url": (work.get("open_access") or {}).get("oa_url") or "",
        "topics": topics,
        "places": places,
    }


def digest(papers: list[dict], today: dt.date, first_run: bool) -> tuple[str, str]:
    """Issue title and Markdown body, most relevant first."""
    def line(p):
        bits = [f"[{p['title']}]({p['url']})"]
        meta = ", ".join(x for x in (p["authors"], p["source"], p["date"]) if x)
        if meta:
            bits.append(f"— {meta}")
        if p["open_access_url"]:
            bits.append(f"([free full text]({p['open_access_url']}))")
        if p["places"]:
            bits.append(f"· mentions {', '.join(p['places'][:5])}")
        return "- " + " ".join(bits)

    groups = {t: [] for t in TOPICS}
    other = []
    for p in papers:
        (groups[p["topics"][0]] if p["topics"] else other).append(p)
    parts = [("First literature check: papers on Xylella fastidiosa published in the last "
              f"{FIRST_RUN_DAYS} days." if first_run else
              "New papers on Xylella fastidiosa indexed by OpenAlex since the last check.")
             + " Papers that mention a Mediterranean place are listed first in each group.\n"]
    for title, items in list(groups.items()) + [("Other", other)]:
        if not items:
            continue
        items.sort(key=lambda p: p["date"], reverse=True)       # newest first ...
        items.sort(key=lambda p: not p["places"])               # ... Mediterranean ones on top
        parts.append(f"\n### {title} ({len(items)})\n" + "\n".join(line(p) for p in items[:40]))
        if len(items) > 40:
            parts.append(f"\n…and {len(items) - 40} more.")
    parts.append("\n\n**To do**\n- [ ] Skim the titles; a paper reporting a new outbreak is a lead to check "
                 "against official sources (EPPO, regional plant-health services) before adding it to "
                 "`official_outbreaks.json`\n- [ ] Note any study with field-confirmed cases that could "
                 "help calibrate the detection thresholds\n- [ ] Close this issue\n")
    body = "".join(parts)
    if len(body) > 60000:                       # GitHub's limit is 65,536 characters
        body = body[:60000] + "\n\n…(cut; see data/literature.json for the full list)"
    title = f"Literature: {len(papers)} new Xylella paper{'s' if len(papers) != 1 else ''} (week of {today.isoformat()})"
    return title, body


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-file", help="saved OpenAlex response (JSON) instead of calling the API")
    ap.add_argument("--today")
    a = ap.parse_args(argv)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()

    if a.from_file:
        works = json.loads(Path(a.from_file).read_text()).get("results", [])
    else:
        works = fetch_recent(today)
    papers = [summarize(w) for w in works if w.get("id")]
    if not papers:
        print("OpenAlex returned no papers; the query or the service may have changed.")
        return 1

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
    first_run = state is None
    seen = set((state or {}).get("seen_ids", []))
    if first_run:
        cutoff = (today - dt.timedelta(days=FIRST_RUN_DAYS)).isoformat()
        new = [p for p in papers if p["date"] >= cutoff]
    else:
        new = [p for p in papers if p["id"] not in seen]

    if new:
        title, body = digest(new, today, first_run)
        print(f"{title}\n")
        if a.dry_run:
            print(body)
        else:
            open_issue(title, body, ["literature"])
    else:
        print("No new papers since the last check.")

    if not a.dry_run:
        recent = {p["id"]: p for p in (state or {}).get("recent", [])}
        recent.update({p["id"]: p for p in papers})
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "checked": today.isoformat(),
            "query": QUERY,
            "source": "OpenAlex (https://openalex.org), CC0",
            "seen_ids": sorted(seen | {p["id"] for p in papers}),
            "recent": sorted(recent.values(), key=lambda p: p["date"], reverse=True)[:KEEP_RECENT],
        }, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
