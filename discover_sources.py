#!/usr/bin/env python3
"""
Quarterly scan for official Xylella pages that aren't watched yet.

It reads every page in official_sources.json plus the "extra_hubs" listed
there, and collects links to other sites whose address or link text mentions
Xylella. Sites already watched, sites in "ignore_hosts", and sites suggested
in an earlier scan are left out, so each site is suggested once. The rest go
into one GitHub issue, each with a link that opens the "Suggest an official
source" form already filled in. Nothing is added without a person approving it.

Standard library only. Usage:
    python discover_sources.py            # scan and open an issue (needs GITHUB_TOKEN, GITHUB_REPOSITORY)
    python discover_sources.py --dry-run  # print the candidates
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse

from eppo_check import open_issue, strip_tags
from official_check import SEEN_FILE, fetch_text, load_config

DOCUMENT_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip|jpe?g|png|mov|mp4)(\?|#|$)", flags=re.I)
TOPIC_RE = re.compile(r"xylella|\bxf\b", flags=re.I)


def host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def find_candidates(config: dict, skip_hosts: set[str]) -> tuple[dict, list[str], int]:
    # {page: verify certificate?}, watched pages first, keeping each source's certificate setting
    hubs = {p: s.get("verify_tls", True) for s in config["sources"]
            for p in s.get("pages", [s["page"]] if "page" in s else [])}
    hubs |= {p: True for p in config.get("discovery", {}).get("extra_hubs", []) if p not in hubs}
    candidates, failed = {}, []
    for hub, verify in hubs.items():
        try:
            page = fetch_text(hub, verify)
        except Exception as exc:
            print(f"FAILED {hub}: {exc}")
            failed.append(hub)
            continue
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I):
            url = urllib.parse.urljoin(hub, m.group(1).replace("&amp;", "&"))
            text, h = strip_tags(m.group(2)), host(url)
            if (not url.startswith("http") or not h or h == host(hub) or h in skip_hosts
                    or h in candidates or DOCUMENT_RE.search(url) or not TOPIC_RE.search(f"{url} {text}")):
                continue
            candidates[h] = {"url": url, "text": text[:120], "found_on": hub}
    return candidates, failed, len(hubs)


def form_link(repo: str, c: dict) -> str:
    params = {"template": "official-source.yml", "title": f"Source: {c['text'] or host(c['url'])}"[:120],
              "name": c["text"][:100], "url": c["url"]}
    return f"https://github.com/{repo}/issues/new?{urllib.parse.urlencode(params)}"


def candidates_issue(candidates: dict, repo: str) -> tuple[str, str, list[str]]:
    lines = [f"- **{h}**: {c['text'] or '(no link text)'}\n  {c['url']}\n  Found on {c['found_on']} · "
             f"[Suggest it]({form_link(repo, c)})" for h, c in sorted(candidates.items())]
    body = ("The quarterly scan found sites about Xylella that aren't watched yet:\n\n" + "\n".join(lines)
            + "\n\n**To add one:** open its *Suggest it* link, choose what to watch, submit, "
              "then add the `approved-source` label.\n"
              "Sites you skip won't be suggested again. Close this issue when you're done.\n")
    return f"Possible new official sources ({dt.date.today().isoformat()})", body, ["source-candidate"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    config = load_config()
    saved = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    watched = {host(p) for s in config["sources"] for p in s.get("pages", [s.get("page", "")]) if p}
    skip = watched | set(config.get("discovery", {}).get("ignore_hosts", [])) | set(saved.get("suggested", []))
    candidates, failed, hubs = find_candidates(config, skip)

    for h, c in sorted(candidates.items()):
        print(f"CANDIDATE: {h} | {c['text']} | {c['url']}")
    if candidates:
        t, b, labels = candidates_issue(candidates, os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO"))
        if not a.dry_run:
            open_issue(t, b, labels)
    else:
        print("No new sites found.")

    if not a.dry_run:
        SEEN_FILE.write_text(json.dumps({**saved, "suggested": sorted(set(saved.get("suggested", [])) | set(candidates))},
                                        indent=1, ensure_ascii=False) + "\n")
    if failed:
        print(f"Could not read {len(failed)} of {hubs} page(s); the weekly check reports broken sources.")
    # Only a scan that read nothing at all is a failure
    return 1 if len(failed) == hubs else 0


if __name__ == "__main__":
    sys.exit(main())
