#!/usr/bin/env python3
"""
Adds an official source to official_sources.json from a "Suggest an official
source" issue (.github/ISSUE_TEMPLATE/official-source.yml).

The source is tested first, the same way official_check.py reads it; it is
only added when that works. The outcome is written as Markdown to
--message-file, which the workflow posts as a comment on the issue.

Standard library only. Usage:
    ISSUE_BODY="..." python add_source.py --message-file msg.md
    python add_source.py --body-file issue.md --message-file msg.md --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from official_check import CHECKS, SOURCES_FILE, load_config

KINDS = {
    "New links on the page (notices, PDFs)": "links",
    "One document linked from the page that gets updated": "document",
    "A Zenodo record that gets new versions": "zenodo",
}


def parse_form(body: str) -> dict:
    """Issue forms arrive as '### Label' headings each followed by the answer."""
    fields = {}
    for m in re.finditer(r"^### (.+?)\s*\n(.*?)(?=^### |\Z)", body.replace("\r\n", "\n"), flags=re.M | re.S):
        value = m.group(2).strip()
        fields[m.group(1).strip()] = "" if value == "_No response_" else value
    return fields


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "source"


def build_source(fields: dict, existing: list[dict]) -> dict:
    name, url = fields.get("Name", ""), fields.get("Page URL", "")
    kind = KINDS.get(fields.get("What to watch", ""))
    match, todo = fields.get("Pattern or link text", ""), fields.get("What to do when it changes", "")
    if not name:
        raise ValueError("the Name field is empty")
    if not re.match(r"https?://[^\s/]+\.[^\s/]+", url):
        raise ValueError(f"'{url}' is not a web address (it must start with http:// or https://)")
    if not kind:
        raise ValueError("choose what to watch")

    ids = {s["id"] for s in existing}
    sid, n = slug(name), 2
    while sid in ids:
        sid, n = f"{slug(name)}-{n}", n + 1
    src = {"id": sid, "name": name, "kind": kind}
    if kind == "links":
        src["pages"] = [url]
        if match:
            re.compile(match)  # a broken pattern fails here, with Python's own explanation
            src["pattern"] = match
    elif kind == "document":
        if not match:
            raise ValueError("for one document, give the text of its link in 'Pattern or link text'")
        src["page"], src["link_text"] = url, match
    else:
        record = re.search(r"zenodo\.org/(?:records?|doi/10\.5281/zenodo\.)/?(\d+)", url)
        if not record:
            raise ValueError("for Zenodo, give a zenodo.org/records/... address")
        src["concept"] = record.group(1)
    if todo:
        src["todo"] = todo
    if "- [x] Read it without verifying" in fields.get("Certificate", "").replace("[X]", "[x]"):
        src["verify_tls"] = False
    watched = ("kind", "pages", "pattern", "page", "link_text", "concept")
    for other in existing:
        if all(other.get(k) == src.get(k) for k in watched):
            raise ValueError(f"this is already watched as '{other['id']}'")
    return src


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--body-file", help="read the issue body from a file instead of $ISSUE_BODY")
    ap.add_argument("--message-file", required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    body = Path(a.body_file).read_text(encoding="utf-8-sig") if a.body_file else os.environ.get("ISSUE_BODY", "")
    config = load_config()
    try:
        src = build_source(parse_form(body), config["sources"])
        state, _ = CHECKS[src["kind"]](src, None)
    except Exception as exc:
        msg = f"Could not add this source: {exc}.\n\nEdit the issue, then remove and re-add the `approved-source` label to try again."
        Path(a.message_file).write_text(msg + "\n", encoding="utf-8")
        print(msg)
        return 1

    found = (f"{len(state['items'])} matching link(s)" if "items" in state else
             f"the document at {state['href']}" if "href" in state else f"Zenodo record {state['record']}")
    if not a.dry_run:
        config["sources"].append(src)
        SOURCES_FILE.write_text(json.dumps(config, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    msg = (f"Added **{src['name']}** as `{src['id']}`: the test found {found}. "
           "The weekly check records it on its next run and reports changes from then on.\n\n"
           f"```json\n{json.dumps(src, indent=1, ensure_ascii=False)}\n```")
    Path(a.message_file).write_text(msg + "\n", encoding="utf-8")
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
