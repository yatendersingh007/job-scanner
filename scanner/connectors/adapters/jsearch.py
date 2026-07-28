"""JSearch adapter (OpenWeb Ninja, via RapidAPI).

JSearch indexes Google for Jobs in real time, which itself aggregates
LinkedIn, Indeed, ZipRecruiter, Glassdoor, Monster and thousands of other
boards — one API call covers all the major US platforms without scraping
any of them directly.

Verified against the vendor docs (July 2026):
  GET https://jsearch.p.rapidapi.com/search
  headers: X-RapidAPI-Key, X-RapidAPI-Host
  params:  query, page, num_pages, country (ISO code), date_posted
           (all|today|3days|week|month), language
  response: {"status": "OK", "data": [{job_id, employer_name, job_title,
             job_apply_link, job_city, job_state, job_country,
             job_posted_at_datetime_utc, job_description, ...}]}

Self-skips without a key (JSEARCH_API_KEY = your RapidAPI key).
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from scanner.connectors.adapters.base import AggregatorAdapter
from scanner.models import NormalizedJob

log = logging.getLogger(__name__)
ENDPOINT = "https://jsearch.p.rapidapi.com/search"
HOST = "jsearch.p.rapidapi.com"
TIMEOUT = 30


def _date_posted(recency_days: int) -> str:
    if recency_days <= 1:
        return "today"
    if recency_days <= 3:
        return "3days"
    if recency_days <= 7:
        return "week"
    return "month"


class JSearchAdapter(AggregatorAdapter):
    name = "jsearch"

    def fetch(self, *, titles, country, recency_days, queries=None, countries=None) -> list[NormalizedJob]:
        if not self.api_key:
            log.info("JSearch: no API key set — skipping (add JSEARCH_API_KEY to enable)")
            return []
        headers = {"X-RapidAPI-Key": self.api_key, "X-RapidAPI-Host": HOST}
        # One focused search phrase per request keeps quota usage predictable;
        # falling back to the first few title keywords if none are configured.
        queries = queries or titles[:5]
        countries = [c.lower() for c in (countries or [country.lower()])]
        date_posted = _date_posted(recency_days)
        # Each (country, query) pair is exactly one RapidAPI call. Announce
        # the plan up front so the user can watch their quota.
        planned = len(countries) * len(queries)
        log.info("JSearch: %d queries x %d countries = %d API calls this run "
                 "(date_posted=%s)", len(queries), len(countries), planned, date_posted)

        jobs: list[NormalizedJob] = []
        calls = 0
        for cc in countries:
            for q in queries:
                calls += 1
                params = {
                    "query": q,
                    "page": 1,
                    "num_pages": 1,
                    "country": cc,
                    "date_posted": date_posted,
                }
                try:
                    r = requests.get(ENDPOINT, params=params, headers=headers, timeout=TIMEOUT)
                    r.raise_for_status()
                    data = r.json()
                except (requests.RequestException, ValueError) as e:
                    log.warning("JSearch [%s] query %r failed: %s", cc, q, e)
                    continue
                rows = data.get("data") or []
                log.info("JSearch [%s] %-34s %d roles", cc, repr(q), len(rows))
                for j in rows:
                    jobs.append(NormalizedJob(
                        company=j.get("employer_name") or "",
                        title=j.get("job_title") or "",
                        url=j.get("job_apply_link") or "",
                        source="aggregator:jsearch",
                        posted_date=_parse_date(j.get("job_posted_at_datetime_utc")),
                        location=_location(j),
                        job_id=str(j.get("job_id") or ""),
                        description=(j.get("job_description") or "")[:2000],
                    ))
        log.info("JSearch: %d API calls made, %d raw roles returned", calls, len(jobs))
        return jobs


def _location(j: dict) -> str:
    parts = [j.get("job_city"), j.get("job_state"), j.get("job_country")]
    loc = ", ".join(p for p in parts if p)
    return loc or (j.get("job_location") or "")


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None
