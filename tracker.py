#!/usr/bin/env python3
"""Intern radar: open a GitHub issue for every new internship posting.

Standard library only. Reads companies.json, fetches each company's job
board, keeps intern / internship / co-op titles in computer science, and creates one issue per
posting not yet recorded in seen.json.

Usage:
    python tracker.py            # create issues, update seen.json
    python tracker.py --dry-run  # print new postings, touch nothing
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPANIES_FILE = ROOT / "companies.json"
SEEN_FILE = ROOT / "seen.json"

TITLE_RE = re.compile(r"\b(interns?|internships?|co-?ops?)\b", re.IGNORECASE)
# Only computer-science roles: the title must also contain one of these.
CS_RE = re.compile(
    r"\b(software|engineer(ing)?|developer|swe|sde|computer|data|machine learning|ml|ai"
    r"|research|scientist|security|infrastructure|backend|back-end|frontend|front-end"
    r"|full[- ]?stack|systems?|platform|mobile|ios|android|cloud|devops|sre|reliability"
    r"|gpu|compiler|algorithms?|kernel|distributed|applied science)\b",
    re.IGNORECASE,
)
# ...and none of these, which are non-CS roles that happen to share a keyword.
NON_CS_RE = re.compile(
    r"\b(ux|user research|design|analyst|risk|marketing|sales|finance|accounting"
    r"|recruit(ing|er)?|legal|people|content)\b",
    re.IGNORECASE,
)
# Search-based boards (Workday, Eightfold) only return what we ask for.
SEARCH_TERMS = ("intern", "co-op")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TIMEOUT = 30
MAX_ATTEMPTS = 4
PAGE_DELAY = 1.0     # seconds between paginated requests to the same board
ISSUE_DELAY = 2.0    # seconds between `gh issue create` calls
MAX_ISSUE_FAILURES = 3  # consecutive `gh` failures before we stop (likely rate-limited)
MAX_PAGES = 40       # hard cap per search term, in case a board never ends


# ---------------------------------------------------------------- HTTP

def http_json(url: str, payload: dict | None = None) -> object:
    """GET (or POST when payload is given) a URL and decode JSON.

    Retries on 429, 5xx and network errors with backoff, honoring
    Retry-After when the server sends it.
    """
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == MAX_ATTEMPTS:
                raise
            wait = _retry_after(e) or 5 * 2 ** (attempt - 1)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == MAX_ATTEMPTS:
                raise
            wait = 5 * 2 ** (attempt - 1)
        log(f"    retry {attempt}/{MAX_ATTEMPTS - 1} in {wait}s: {url}")
        time.sleep(wait)
    raise RuntimeError("unreachable")


def _retry_after(err: urllib.error.HTTPError) -> int | None:
    value = err.headers.get("Retry-After") if err.headers else None
    if value and value.isdigit():
        return min(int(value), 120)
    return None


# ---------------------------------------------------------------- boards
# Each fetcher returns a list of {"id", "title", "location", "url"} dicts.
# "id" must be stable across runs; it is what seen.json records.

def fetch_greenhouse(c: dict) -> list[dict]:
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs")
    return [
        {
            "id": str(j["id"]),
            "title": j["title"],
            "location": (j.get("location") or {}).get("name", ""),
            "url": j["absolute_url"],
        }
        for j in d["jobs"]
    ]


def fetch_ashby(c: dict) -> list[dict]:
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}")
    return [
        {
            "id": j["id"],
            "title": j["title"],
            "location": j.get("location", ""),
            "url": j["jobUrl"],
        }
        for j in d["jobs"]
    ]


def fetch_lever(c: dict) -> list[dict]:
    d = http_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json")
    return [
        {
            "id": j["id"],
            "title": j["text"],
            "location": (j.get("categories") or {}).get("location", ""),
            "url": j["hostedUrl"],
        }
        for j in d
    ]


def fetch_workday(c: dict) -> list[dict]:
    """c: tenant, wd (e.g. "wd5"), site."""
    host = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com"
    api = f"{host}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    jobs = {}
    for term in SEARCH_TERMS:
        offset, total, limit = 0, None, 20
        for _ in range(MAX_PAGES):
            if total is not None and offset >= total:
                break
            d = http_json(api, {"appliedFacets": {}, "searchText": term,
                                "limit": limit, "offset": offset})
            if total is None:  # Workday only reports total reliably on page 1
                total = d.get("total", 0)
            page = d.get("jobPostings", [])
            # Results are relevance-sorted and the search also hits "internal",
            # "international", ...; stop once a page has no real matches.
            if not page or (offset and not any(TITLE_RE.search(j["title"]) for j in page)):
                break
            for j in page:
                path = j["externalPath"]
                jobs[path] = {
                    "id": path,
                    "title": j["title"],
                    "location": j.get("locationsText", ""),
                    "url": f"{host}/{c['site']}{path}",
                }
            offset += limit
            time.sleep(PAGE_DELAY)
    return list(jobs.values())


def fetch_eightfold(c: dict) -> list[dict]:
    """c: host (careers site host), domain (the site's ?domain= value)."""
    host, domain = c["host"], c["domain"]
    jobs = {}
    for term in SEARCH_TERMS:
        start, total = 0, None
        for _ in range(MAX_PAGES):
            if total is not None and start >= total:
                break
            q = urllib.parse.urlencode({"domain": domain, "query": term, "start": start})
            d = http_json(f"https://{host}/api/apply/v2/jobs?{q}&num=10")
            page = d.get("positions", [])
            for p in page:
                jobs[str(p["id"])] = {
                    "id": str(p["id"]),
                    "title": p["name"],
                    "location": p.get("location", ""),
                    "url": p.get("canonicalPositionUrl")
                    or f"https://{host}/careers/job/{p['id']}",
                }
            total = d.get("count", 0)
            if not page:
                break
            start += len(page)
            time.sleep(PAGE_DELAY)
    return list(jobs.values())


def is_cs_internship(title: str) -> bool:
    return bool(TITLE_RE.search(title) and CS_RE.search(title)
                and not NON_CS_RE.search(title))


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "eightfold": fetch_eightfold,
}


# ---------------------------------------------------------------- issues

def create_issue(company: dict, job: dict) -> None:
    body = (
        f"**Company:** {company['name']}\n"
        f"**Location:** {job['location'] or 'n/a'}\n"
        f"**Apply:** {job['url']}\n"
    )
    subprocess.run(
        ["gh", "issue", "create",
         "--title", f"{company['name']}: {job['title'].strip()}",
         "--body", body,
         "--label", company["category"]],
        check=True, capture_output=True, text=True,
    )


# ---------------------------------------------------------------- main

def log(msg: str) -> None:
    print(msg, flush=True)


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print new postings instead of creating issues; don't write seen.json")
    ap.add_argument("--only", metavar="NAME", action="append",
                    help="limit the run to these companies (repeatable)")
    args = ap.parse_args()

    companies = json.loads(COMPANIES_FILE.read_text())["companies"]
    if args.only:
        wanted = {n.lower() for n in args.only}
        companies = [c for c in companies if c["name"].lower() in wanted]
    seen = load_seen()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ok, failed, new_postings, issue_failures = [], [], [], []
    consecutive_issue_failures = 0

    try:
        for c in companies:
            name = c["name"]
            try:
                fetcher = FETCHERS[c["board"]]
                jobs = fetcher(c)
                matches = [j for j in jobs if is_cs_internship(j["title"] or "")]
            except Exception as e:  # one broken board must not break the run
                log(f"[FAIL] {name} ({c.get('board')}): {type(e).__name__}: {e}")
                failed.append(name)
                continue

            company_seen = seen.setdefault(name, {})
            fresh = [j for j in matches if j["id"] not in company_seen]
            ok.append(name)
            log(f"[ok]   {name}: {len(jobs)} jobs, {len(matches)} intern, {len(fresh)} new")

            for job in fresh:
                if args.dry_run:
                    log(f"         NEW  {name}: {job['title'].strip()} | {job['location']} | {job['url']}")
                    new_postings.append((name, job))
                    continue
                if consecutive_issue_failures >= MAX_ISSUE_FAILURES:
                    issue_failures.append((name, job))  # left unseen for next run
                    continue
                try:
                    create_issue(c, job)
                except (subprocess.CalledProcessError, OSError) as e:
                    # Leave it unseen so the next run retries it.
                    detail = getattr(e, "stderr", "") or str(e)
                    log(f"         issue FAILED for {job['title']!r}: {detail.strip()}")
                    issue_failures.append((name, job))
                    consecutive_issue_failures += 1
                    if consecutive_issue_failures == MAX_ISSUE_FAILURES:
                        log("         too many issue failures in a row; skipping issue creation for the rest of this run")
                    continue
                consecutive_issue_failures = 0
                company_seen[job["id"]] = {"title": job["title"].strip(), "first_seen": today}
                new_postings.append((name, job))
                log(f"         issue created: {name}: {job['title'].strip()}")
                time.sleep(ISSUE_DELAY)
    finally:
        # Save even if the run dies midway, so issues already created
        # are not created again next time.
        if not args.dry_run:
            save_seen(seen)

    log("")
    log("=" * 60)
    log(f"Summary{' (dry run)' if args.dry_run else ''}")
    log(f"  companies ok:     {len(ok)}")
    log(f"  companies failed: {len(failed)}" + (f"  -> {', '.join(failed)}" if failed else ""))
    log(f"  new postings:     {len(new_postings)}")
    if issue_failures:
        log(f"  issue failures:   {len(issue_failures)} (will retry next run)")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
