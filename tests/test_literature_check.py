"""Literature check with fake OpenAlex responses (no network)."""
import datetime as dt
import json
import urllib.error

import pytest

import literature_check as lc


def work(i, title, date, abstract="", source="Plants", doi=True, oa=None):
    inv = {}
    for pos, word in enumerate(abstract.split()):
        inv.setdefault(word, []).append(pos)
    return {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/10.1/{i}" if doi else None,
            "display_name": title, "publication_date": date, "type": "article",
            "primary_location": {"source": {"display_name": source}},
            "authorships": [{"author": {"display_name": "A. Rossi"}}, {"author": {"display_name": "B. Garcia"}}],
            "open_access": {"oa_url": oa}, "abstract_inverted_index": inv or None}


WORKS = [
    work(1, "First report of Xylella fastidiosa in olive groves of Calabria", "2026-10-10",
         "An outbreak was detected in the Gioia Tauro plain, Italy."),
    work(2, "Sentinel-2 time series reveal almond decline", "2026-10-01",
         "We use satellite data in Mallorca to detect Xylella symptoms.", oa="https://example.org/pdf"),
    work(3, "Genome of a Xylella strain from coffee", "2026-09-30", "Sequencing of an isolate.", doi=False),
    work(4, "Philaenus spumarius feeding behaviour", "2026-07-01", "Vector transmission experiments."),
]


def test_abstract_and_summary():
    assert lc.abstract_text({"b": [1], "a": [0, 2]}) == "a b a"
    p = lc.summarize(WORKS[0])
    assert p["id"] == "W1" and p["authors"] == "A. Rossi et al." and p["url"] == "https://doi.org/10.1/1"
    assert p["topics"][0] == "Spread, outbreaks and surveillance"
    assert p["places"] == ["Calabria", "Italy"]
    p2 = lc.summarize(WORKS[1])
    assert "Detection and remote sensing" in p2["topics"] and p2["places"] == ["Balearic Islands"]
    assert lc.summarize(WORKS[2])["url"] == "https://openalex.org/W3"


def test_digest_orders_and_limits():
    papers = [lc.summarize(w) for w in WORKS]
    title, body = lc.digest(papers, dt.date(2026, 10, 22), first_run=False)
    assert title == "Literature: 4 new Xylella papers (week of 2026-10-22)"
    assert body.index("Calabria") < body.index("Genome")
    assert "[free full text](https://example.org/pdf)" in body
    _, long_body = lc.digest(papers * 2000, dt.date(2026, 10, 22), first_run=False)
    assert len(long_body) <= 61000


def test_fetch_pages_and_sort_fallback():
    calls = []

    def fake(params):
        calls.append(params)
        if params.get("sort"):
            raise urllib.error.HTTPError("u", 400, "bad sort", {}, None)
        return {"results": WORKS if params["page"] == 1 else []}

    got = lc.fetch_recent(dt.date(2026, 10, 22), fetch=fake)
    assert len(got) == 4
    filt = calls[0]["filter"].split(",")
    assert "title_and_abstract.search:xylella fastidiosa" in filt and "publication_year:2025-2026" in filt
    assert "search" not in calls[0]                   # full-text search returns papers that merely mention it
    types = next(f for f in filt if f.startswith("type:"))[5:].split("|")
    assert "article" in types and "preprint" in types and "dataset" not in types and "erratum" not in types
    assert "sort" not in calls[-1]


def test_api_key_goes_in_header_not_url(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"results": []}'

    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key")
    monkeypatch.setattr(lc.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Resp())
    lc.fetch_json({"filter": "x"})
    assert seen[0].get_header("Authorization") == "Bearer secret-key"
    assert "secret-key" not in seen[0].full_url


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(lc, "STATE_FILE", tmp_path / "literature.json")
    issues = []
    monkeypatch.setattr(lc, "open_issue", lambda t, b, l: issues.append((t, b, l)))
    page = tmp_path / "page.json"
    return page, issues, tmp_path


def test_first_run_reports_last_month_then_only_new(state):
    page, issues, tmp = state
    page.write_text(json.dumps({"results": WORKS}))
    assert lc.main(["--from-file", str(page), "--today", "2026-10-22"]) == 0
    assert len(issues) == 1 and issues[0][2] == ["literature"]
    assert "3 new" in issues[0][0]                    # W4 is older than 30 days
    saved = json.loads((tmp / "literature.json").read_text())
    assert saved["seen_ids"] == ["W1", "W2", "W3", "W4"] and saved["recent"][0]["id"] == "W1"

    issues.clear()
    assert lc.main(["--from-file", str(page), "--today", "2026-10-29"]) == 0
    assert issues == []                               # nothing new

    page.write_text(json.dumps({"results": WORKS + [work(5, "Xylella in Greece", "2026-10-25",
                                                         "Survey in Crete")]}))
    assert lc.main(["--from-file", str(page), "--today", "2026-10-29"]) == 0
    assert len(issues) == 1 and "1 new Xylella paper " in issues[0][0] and "Greece" in issues[0][1]


def test_empty_response_fails(state):
    page, issues, tmp = state
    page.write_text(json.dumps({"results": []}))
    assert lc.main(["--from-file", str(page)]) == 1
    assert not (tmp / "literature.json").exists() and issues == []
