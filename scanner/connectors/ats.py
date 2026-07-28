"""Connector B — public ATS feeds. Free, no key, watchlist-driven.

Covers the major applicant tracking systems that expose public JSON:
Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday. Point it at a
list of {name, ats, token} and it pulls every open role, first-party and
fresh. For Workday, `token` is the company's careers-page URL, e.g.
https://acme.wd5.myworkdayjobs.com/External — tenant/host/site are parsed
out of it.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import requests

from scanner.connectors.base import Connector
from scanner.models import NormalizedJob

log = logging.getLogger(__name__)
TIMEOUT = 20


def _epoch_ms_to_date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError):
        return None


def _iso_to_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _workday_posted_on(s):
    """Workday gives human text like 'Posted Today' / 'Posted 3 Days Ago' /
    'Posted 30+ Days Ago'. Best-effort; '30+' returns None (first-seen logic
    takes over)."""
    if not s:
        return None
    s = s.lower()
    if "today" in s:
        return date.today()
    if "yesterday" in s:
        return date.today() - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*days?\s*ago", s)
    if m and "+" not in s:
        return date.today() - timedelta(days=int(m.group(1)))
    return None


class ATSConnector(Connector):
    name = "ats"

    def __init__(self, companies: list[dict]):
        # each company: {"name": ..., "ats": "greenhouse|lever|ashby|workable|smartrecruiters", "token": ...}
        self.companies = companies or []

    def fetch(self) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        for c in self.companies:
            ats = (c.get("ats") or "").lower()
            token = c.get("token")
            name = c.get("name") or token
            if not token:
                continue
            try:
                handler = getattr(self, f"_{ats}", None)
                if handler is None:
                    log.warning("Unknown ATS '%s' for %s — skipping", ats, name)
                    continue
                found = handler(name, token)
                log.info("ATS %-14s %-24s %d roles", ats, name, len(found))
                jobs.extend(found)
            except requests.RequestException as e:
                log.warning("ATS fetch failed for %s (%s): %s", name, ats, e)
        return jobs

    # --- per-ATS handlers -------------------------------------------------

    def _greenhouse(self, name, token):
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        data = requests.get(url, timeout=TIMEOUT).json()
        out = []
        for j in data.get("jobs", []):
            out.append(NormalizedJob(
                company=name,
                title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                source="ats:greenhouse",
                # Greenhouse only exposes updated_at (changes on edit) — treat as
                # unreliable; leave posted_date None so first-seen logic takes over.
                posted_date=None,
                location=(j.get("location") or {}).get("name", ""),
                job_id=str(j.get("id", "")),
                description=re.sub(r"<[^>]+>", " ", j.get("content") or "")[:2000],
            ))
        return out

    def _lever(self, name, token):
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        data = requests.get(url, timeout=TIMEOUT).json()
        out = []
        for j in data:
            out.append(NormalizedJob(
                company=name,
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                source="ats:lever",
                posted_date=_epoch_ms_to_date(j.get("createdAt")),
                location=(j.get("categories") or {}).get("location", ""),
                job_id=str(j.get("id", "")),
                description=(j.get("descriptionPlain") or "")[:2000],
            ))
        return out

    def _ashby(self, name, token):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        data = requests.get(url, timeout=TIMEOUT).json()
        out = []
        for j in data.get("jobs", []):
            out.append(NormalizedJob(
                company=name,
                title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                source="ats:ashby",
                posted_date=_iso_to_date(j.get("publishedAt")),
                location=j.get("location", ""),
                job_id=str(j.get("id", "")),
            ))
        return out

    def _workable(self, name, token):
        url = f"https://apply.workable.com/api/v1/widget/accounts/{token}"
        data = requests.get(url, timeout=TIMEOUT).json()
        out = []
        for j in data.get("jobs", []):
            out.append(NormalizedJob(
                company=name,
                title=j.get("title", ""),
                url=j.get("url") or j.get("application_url", ""),
                source="ats:workable",
                posted_date=_iso_to_date(j.get("published_on")),
                location=", ".join(filter(None, [j.get("city"), j.get("country")])),
                job_id=str(j.get("shortcode") or j.get("id", "")),
            ))
        return out

    def _workday(self, name, token):
        """token = the public careers URL, e.g.
        https://acme.wd5.myworkdayjobs.com/External (locale segment optional).
        Workday exposes a public JSON endpoint per site:
        POST /wday/cxs/{tenant}/{site}/jobs {"limit", "offset", "searchText"}."""
        m = re.match(
            r"https?://(?P<tenant>[^.]+)\.(?P<host>wd\d+)\.myworkdayjobs\.com"
            r"/(?:(?P<locale>[a-z]{2}-[A-Z]{2})/)?(?P<site>[^/?#]+)",
            token.strip(),
        )
        if not m:
            log.warning("Workday URL not recognized for %s: %s", name, token)
            return []
        tenant, host, site = m["tenant"], m["host"], m["site"]
        base = f"https://{tenant}.{host}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
        job_base = f"{base}/{m['locale'] or 'en-US'}/{site}"

        out, offset, page = [], 0, 20
        while offset < 300:  # safety cap: 15 pages of 20
            r = requests.post(api, json={"limit": page, "offset": offset, "searchText": ""},
                              timeout=TIMEOUT)
            r.raise_for_status()
            postings = r.json().get("jobPostings") or []
            if not postings:
                break
            for j in postings:
                path = j.get("externalPath") or ""
                out.append(NormalizedJob(
                    company=name,
                    title=j.get("title", ""),
                    url=f"{job_base}{path}" if path else base,
                    source="ats:workday",
                    posted_date=_workday_posted_on(j.get("postedOn")),
                    location=j.get("locationsText", ""),
                    job_id=str(j.get("bulletFields", [""])[0] or path),
                ))
            if len(postings) < page:
                break
            offset += page
        return out

    def _smartrecruiters(self, name, token):
        url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
        data = requests.get(url, timeout=TIMEOUT).json()
        out = []
        for j in data.get("content", []):
            loc = j.get("location") or {}
            out.append(NormalizedJob(
                company=name,
                title=j.get("name", ""),
                url=(j.get("ref") or ""),
                source="ats:smartrecruiters",
                posted_date=_iso_to_date(j.get("releasedDate")),
                location=", ".join(filter(None, [loc.get("city"), loc.get("country")])),
                job_id=str(j.get("id", "")),
            ))
        return out
