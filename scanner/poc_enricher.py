"""POC (point-of-contact) enrichment — per unique company, via TinyFish.

One single process: Run Scan does the board scan, classification, AND POC
enrichment in one pass, writing one CSV with job + POC details together —
no separate step, no second file. A company is looked up over TinyFish ONCE
EVER (cached in poc_cache.db); later runs reuse the cache unless a refresh
is forced. Lookups are capped per run so cost stays predictable, and
companies on the blocklist (staffing/recruiting firms) are skipped without
spending any TinyFish steps.

Two-step lookup per company:
  a) the job posting page we already have a URL for — many postings show
     who posted the job or a named hiring contact.
  b) if (a) finds nobody, the company's own website leadership/team page.
     TinyFish's Agent API always starts from a URL (there is no blank-slate
     "search the web" mode), so step (b) starts from a Bing search-results
     page for "{company} official website" and asks the agent to find and
     navigate into the real site from there.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse

import requests

from scanner.connectors.tinyfish import RUN_ENDPOINT, START_ENDPOINT
from scanner.models import NormalizedJob
from scanner.poc_store import PocStore

log = logging.getLogger(__name__)

MAX_WAIT = 180        # seconds per single-page lookup — much shorter than a job-board scan
POLL_INTERVAL = 8
HTTP_TIMEOUT = 60

CAP_REACHED = "not yet attempted (run cap reached)"
NOT_FOUND = "not found"
SKIPPED_STAFFING = "skipped - staffing firm"
SKIPPED_NO_KEY = "skipped - no TinyFish key set"
FROM_POSTING = "job posting page"
FROM_SITE = "company site"

GOAL_POSTING = (
    "This is a job posting page. It may name who posted the job or a hiring "
    "contact (e.g. 'Posted by', 'Hiring Manager', 'Recruiter', a team-member "
    "byline). Find that person's name, their job title/designation, and a "
    "public profile URL if one is shown (e.g. LinkedIn). If no such person "
    "is named anywhere on this page, return name as null."
)
SCHEMA_POSTING = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "nullable": True},
        "designation": {"type": "string", "nullable": True},
        "profile_url": {"type": "string", "nullable": True},
    },
    "required": ["name"],
}

GOAL_COMPANY_SITE = (
    "These are search results for {company}'s official website. Identify the "
    "correct official corporate site (not a directory, news article, or "
    "unrelated company) and navigate to it. Then find their team, "
    "leadership, management, or 'about us' page. Identify the most senior "
    "person responsible for finance or accounting (titles like CFO, VP "
    "Finance, Controller, Director of Accounting) — if none is listed, "
    "identify the Head of Talent, HR, or People instead. Extract that "
    "person's name, their exact title, a public profile URL if shown (e.g. "
    "LinkedIn), and the URL of the page where you found them. Also return "
    "the root domain of the official site you found (e.g. example.com), "
    "even if no person was found there. If you cannot find the official "
    "website, or no such person is listed anywhere on it, return name as null."
)
SCHEMA_COMPANY_SITE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "nullable": True},
        "designation": {"type": "string", "nullable": True},
        "profile_url": {"type": "string", "nullable": True},
        "source_url": {"type": "string", "nullable": True},
        "company_domain": {"type": "string", "nullable": True},
    },
    "required": ["name"],
}


def _company_key(name: str) -> str:
    return (name or "").strip().lower()


def _is_blocklisted(company: str, blocklist: list[str]) -> bool:
    c = _company_key(company)
    if not c:
        return False
    return any(_company_key(b) in c for b in blocklist if b.strip())


def _tinyfish_run(api_key: str, url: str, goal: str, schema: dict) -> dict:
    """One automation run, start + poll, tuned for a single-page lookup.
    Returns the parsed result dict, or {} on any failure — a POC miss must
    never fail the whole enrichment run."""
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    try:
        r = requests.post(START_ENDPOINT, json={"url": url, "goal": goal, "output_schema": schema},
                          headers=headers, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        run_id = r.json().get("run_id")
        if not run_id:
            return {}
    except requests.RequestException as e:
        log.info("POC lookup: could not start run for %s: %s", url, e)
        return {}

    start = time.monotonic()
    deadline = start + MAX_WAIT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            r = requests.get(RUN_ENDPOINT.format(run_id=run_id), headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code >= 500:
                continue
            r.raise_for_status()
            run = r.json()
        except requests.RequestException:
            continue
        status = run.get("status")
        if status == "COMPLETED":
            result = run.get("result") or {}
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    return {}
            return result if isinstance(result, dict) else {}
        if status in ("FAILED", "CANCELLED"):
            return {}
    log.info("POC lookup timed out after %ds for %s", MAX_WAIT, url)
    return {}


def _lookup_company(api_key: str, company: str, posting_url: str) -> dict:
    """Two-step lookup for one company. `poc_name` is "" when nobody could
    be identified either way."""
    if posting_url:
        log.info("POC lookup: %s — trying the job posting page", company)
        r = _tinyfish_run(api_key, posting_url, GOAL_POSTING, SCHEMA_POSTING)
        if r.get("name"):
            return {"poc_name": r["name"], "poc_designation": r.get("designation") or "",
                    "poc_profile": r.get("profile_url") or "", "poc_source": FROM_POSTING,
                    "company_domain": ""}

    log.info("POC lookup: %s — trying the company's own website", company)
    search_url = "https://www.bing.com/search?q=" + urllib.parse.quote(f"{company} official website")
    r = _tinyfish_run(api_key, search_url, GOAL_COMPANY_SITE.format(company=company), SCHEMA_COMPANY_SITE)
    if r.get("name"):
        return {"poc_name": r["name"], "poc_designation": r.get("designation") or "",
                "poc_profile": r.get("profile_url") or "", "poc_source": FROM_SITE,
                "company_domain": r.get("company_domain") or ""}
    return {"poc_name": "", "poc_designation": "", "poc_profile": "", "poc_source": NOT_FOUND,
            "company_domain": r.get("company_domain") or ""}


def compute_poc_map(companies: dict[str, tuple[str, str]], cfg) -> tuple[dict[str, dict], dict]:
    """companies: company_key -> (display_name, representative_posting_url).
    Returns (company_key -> poc-fields dict, stats dict). Applies the
    blocklist and the per-run cap; never raises."""
    store = PocStore(cfg.poc_cache_db)
    blocklist = cfg.poc_blocklist
    cap = max(0, cfg.poc_enrich_cap)
    force = cfg.poc_force_refresh
    api_key = cfg.tinyfish_key

    result: dict[str, dict] = {}
    stats = {"looked_up": 0, "cache_hits": 0, "blocklisted": 0, "capped": 0, "no_key": 0}
    try:
        for key, (name, url) in companies.items():
            if _is_blocklisted(name, blocklist):
                result[key] = {"poc_name": "", "poc_designation": "", "poc_profile": "",
                               "poc_source": SKIPPED_STAFFING, "company_domain": ""}
                stats["blocklisted"] += 1
                continue

            cached = None if force else store.get(key)
            if cached is not None:
                result[key] = cached
                stats["cache_hits"] += 1
                continue

            if not api_key:
                result[key] = {"poc_name": "", "poc_designation": "", "poc_profile": "",
                               "poc_source": SKIPPED_NO_KEY, "company_domain": ""}
                stats["no_key"] += 1
                continue

            if stats["looked_up"] >= cap:
                result[key] = {"poc_name": "", "poc_designation": "", "poc_profile": "",
                               "poc_source": CAP_REACHED, "company_domain": ""}
                stats["capped"] += 1
                continue

            stats["looked_up"] += 1
            log.info("POC lookup %d/%d: %s", stats["looked_up"], cap, name)
            found = _lookup_company(api_key, name, url)
            store.set(key, company_name=name, poc_name=found["poc_name"],
                      poc_designation=found["poc_designation"], poc_profile=found["poc_profile"],
                      poc_source=found["poc_source"], company_domain=found.get("company_domain", ""))
            result[key] = found
    finally:
        store.close()

    found_total = sum(1 for v in result.values() if v.get("poc_name"))
    log.info("POC enrichment: %d companies looked up this run (%d from cache, %d skipped as "
             "staffing firms, %d hit the run cap) — %d/%d companies have a contact",
             stats["looked_up"], stats["cache_hits"], stats["blocklisted"], stats["capped"],
             found_total, len(companies))
    return result, stats


def _unique_companies(jobs: list[NormalizedJob]) -> dict[str, tuple[str, str]]:
    companies: dict[str, tuple[str, str]] = {}
    for j in jobs:
        key = _company_key(j.company)
        if key and key not in companies:
            companies[key] = (j.company, j.url)
    return companies


def enrich(jobs: list[NormalizedJob], cfg) -> dict:
    """Enrich a freshly-scanned job list in place — called from
    pipeline.run() right after classification, as part of the one Run Scan
    process. No separate step, no second file: poc_name/poc_designation/
    poc_profile/poc_source land directly on the same rows that go into
    jobs.csv. Returns stats for logging/reporting."""
    if not cfg.poc_enrichment_enabled or not jobs:
        return {"looked_up": 0, "cache_hits": 0, "blocklisted": 0, "capped": 0, "no_key": 0}

    companies = _unique_companies(jobs)
    poc_map, stats = compute_poc_map(companies, cfg)
    for j in jobs:
        info = poc_map.get(_company_key(j.company), {})
        j.poc_name = info.get("poc_name", "")
        j.poc_designation = info.get("poc_designation", "")
        j.poc_profile = info.get("poc_profile", "")
        j.poc_source = info.get("poc_source", "")
    return stats
