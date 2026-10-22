#!/usr/bin/env python3
"""
Weekly check of official sources on Xylella fastidiosa, besides EPPO
(eppo_check.py) and the literature (openalex_check.py).

Each source in SOURCES is watched in one of three ways:
  * "document": a file linked from a page (found by its link text); a new link
    or different file contents count as an update,
  * "links": item links on one or more pages (matched by a pattern); links not
    seen before count as updates,
  * "zenodo": a Zenodo record; a new version counts as an update.
What has been seen is kept in official_seen.json. Each source with updates
gets one GitHub issue so a person can check it and update official_outbreaks.json,
country_status.json or the host list. A source seen for the first time is only
recorded. A source that can't be read is skipped for this run and makes the
check fail at the end (GitHub emails the repository owner); the others still
report.

Standard library only. Usage:
    python official_check.py            # check and open issues (needs GITHUB_TOKEN, GITHUB_REPOSITORY)
    python official_check.py --dry-run  # print what would be reported
    python official_check.py --only eu-demarcated --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from eppo_check import USER_AGENT, open_issue, strip_tags

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "official_seen.json"

SOURCES = [
    {
        "id": "eu-demarcated",
        "name": "EU list of demarcated areas",
        "kind": "document",
        "page": "https://food.ec.europa.eu/plants/plant-health-and-biosecurity/plant-health-rules/control-measures/xylella-fastidiosa_en",
        "link_text": "List of demarcated areas in the EU",
        "todo": "Compare the member-state lists with `official_outbreaks.json` (new or lifted demarcated areas)",
    },
    {
        "id": "efsa-hosts",
        "name": "EFSA Xylella spp. host plant database",
        "kind": "zenodo",
        "concept": "1339343",
        "todo": "Read the release notes for new host species; note any found in the Mediterranean",
    },
    {
        "id": "es-mapa",
        "name": "Spain, Ministry of Agriculture (MAPA)",
        "kind": "links",
        "pages": ["https://www.mapa.gob.es/es/agricultura/temas/sanidad-vegetal/organismos-nocivos/xylella-fastidiosa"],
        # MAPA spells the folder "xylella-fastydiosa"
        "pattern": r"/dam/mapa/[^\"']*xylella[^\"']*\.pdf",
        "todo": "New outbreak or demarcated area in Spain? Update `official_outbreaks.json` / `country_status.json`",
    },
    {
        "id": "it-puglia",
        "name": "Puglia, Emergenza Xylella (regional plant health service)",
        "kind": "links",
        # The site's HTTPS certificate is issued for another domain, so it is read over HTTP
        "pages": ["http://www.emergenzaxylella.it/",
                  "http://www.emergenzaxylella.it/portal/portale_gestione_agricoltura/Documenti/normRegionale"],
        "pattern": r"/portal/portale_gestione_agricoltura/Documenti/[^\"']*IDNEWS=\d+[^\"']*",
        "key": r"IDNEWS=(\d+)",
        "todo": "New outbreak (focolaio) or change to the demarcated area? Update `official_outbreaks.json`",
    },
]


# Keeps cookies between requests: the Puglia portal sets a session cookie and redirects to itself
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def fetch(url: str, tries: int = 3) -> bytes:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with OPENER.open(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(f"{url} no longer exists (404); the source moved") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
        time.sleep(10 * (i + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", "replace")


def check_document(src: dict, seen: dict | None) -> tuple[dict, list[str]]:
    page = fetch_text(src["page"])
    m = next((m for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I)
              if src["link_text"].lower() in strip_tags(m.group(2)).lower()), None)
    if not m:
        raise RuntimeError(f"no link named '{src['link_text']}' on {src['page']}; the page changed")
    href = urllib.parse.urljoin(src["page"], m.group(1).replace("&amp;", "&"))
    state = {"href": href, "sha256": hashlib.sha256(fetch(href)).hexdigest()}
    if seen is None or seen == state:
        return state, []
    what = "a new file" if seen.get("href") != href else "the file contents changed"
    return state, [f"**{src['link_text']}** was updated ({what}): {href}"]


def check_links(src: dict, seen: dict | None) -> tuple[dict, list[str]]:
    found = {}
    for url in src["pages"]:
        page = fetch_text(url)
        for m in re.finditer(src["pattern"], page, flags=re.I):
            href = urllib.parse.urljoin(url, m.group(0).replace("&amp;", "&"))
            key = re.search(src["key"], href).group(1) if src.get("key") else href
            if key not in found:
                found[key] = (href, describe_link(page, m.start(), m.end()))
    if not found:
        raise RuntimeError(f"no item links found on {', '.join(src['pages'])}; the page changed")
    state = {"items": sorted(set(found) | set((seen or {}).get("items", [])))}
    if seen is None:
        return state, []
    known = set(seen.get("items", []))
    return state, [f"**{text or 'New item'}**\n  {href}" for key, (href, text) in found.items() if key not in known]


def describe_link(page: str, start: int, end: int) -> str:
    """A title for an item link: its news block (Puglia), else its link text, else the text after it."""
    block_start = page.rfind('class="blocco_news"', 0, start)
    if block_start != -1 and page.find('class="blocco_news"', block_start + 1, start) == -1:
        date = re.search(r'blonews_data">(.*?)</div>', page[block_start:start], flags=re.S)
        title = re.search(r'blonews_titolo">(.*?)</div>', page[block_start:start], flags=re.S)
        if title:
            return f"{strip_tags(date.group(1)) + ': ' if date else ''}{strip_tags(title.group(1))}"
    close = page.find("</a>", end)
    text = strip_tags(page[page.find(">", end) + 1:close]) if close != -1 else ""
    if len(text) < 15 or text.lower() in ("leggi tutto", "read more"):
        text = strip_tags(page[close:close + 1200])
    return text.lstrip(". ")[:200]


def check_zenodo(src: dict, seen: dict | None) -> tuple[dict, list[str]]:
    rec = json.loads(fetch_text(f"https://zenodo.org/api/records/{src['concept']}/versions/latest"))
    state = {"record": str(rec["id"])}
    if seen is None or seen == state:
        return state, []
    md = rec.get("metadata", {})
    return state, [f"**{md.get('title', 'New version')}** ({md.get('publication_date', '')})\n  "
                   f"{rec.get('links', {}).get('self_html') or 'https://zenodo.org/records/' + state['record']}"]


CHECKS = {"document": check_document, "links": check_links, "zenodo": check_zenodo}


def source_issue(src: dict, updates: list[str]) -> tuple[str, str, list[str]]:
    body = (f"{src['name']} has {len(updates)} update(s):\n\n"
            + "\n".join(f"- {u}" for u in updates)
            + f"\n\n**To do**\n- [ ] {src['todo']}\n- [ ] Close this issue\n")
    return f"Official update: {src['name']}"[:240], body, ["official-update"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", metavar="SOURCE_ID", help="check one source")
    a = ap.parse_args(argv)

    saved = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    states = dict(saved.get("sources", {}))
    failed = []
    for src in SOURCES:
        if a.only and src["id"] != a.only:
            continue
        seen = states.get(src["id"])
        try:
            state, updates = CHECKS[src["kind"]](src, seen)
        except Exception as exc:  # one broken source must not hide the others
            print(f"FAILED {src['id']}: {exc}")
            failed.append(src["id"])
            continue
        states[src["id"]] = state
        if seen is None:
            print(f"First run for {src['id']}: recorded.")
        elif updates:
            t, b, labels = source_issue(src, updates)
            print(f"UPDATE: {t}\n" + "\n".join(f"  {u.splitlines()[0]}" for u in updates))
            if not a.dry_run:
                open_issue(t, b, labels)
        else:
            print(f"No change: {src['id']}")

    if not a.dry_run:
        SEEN_FILE.write_text(json.dumps({"checked": dt.date.today().isoformat(), "sources": states},
                                        indent=1, ensure_ascii=False) + "\n")
    if failed:
        print(f"Could not read: {', '.join(failed)}. Check the URLs in SOURCES.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
