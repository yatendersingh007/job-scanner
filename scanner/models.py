"""The one schema every connector normalizes into.

Every source (aggregator API, ATS feed, TinyFish) produces a list of these,
so the rest of the pipeline never needs to know where a job came from.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class NormalizedJob:
    company: str
    title: str
    url: str
    source: str                          # which connector/source produced this
    posted_date: Optional[date] = None   # TRUE post date, if the source gives one
    location: str = ""
    job_id: str = ""                     # source-native id, if available
    description: str = ""                # posting text where the source gives one
                                         # (feeds the classifier; never written to CSV)

    # filled in by the pipeline:
    first_seen: Optional[date] = None
    days_since_posted: Optional[int] = None
    region: str = ""                     # US | UK | Canada | India | Unknown (from location)

    # filled in by the rule-based classifier:
    category: str = ""                   # Real Estate | Family Office | Other
    subcategory: str = ""
    match_reason: str = ""               # which keywords/rules fired (audit trail)

    # filled in by POC enrichment (per-company, see scanner/poc_enricher.py):
    poc_name: str = ""
    poc_designation: str = ""
    poc_profile: str = ""                # public profile URL if one was found, else ""
    poc_source: str = ""                 # where it came from, or why it's blank

    def dedup_key(self) -> str:
        """Stable identity for a posting across runs.

        Prefer the source-native id (namespaced by source); fall back to a
        hash of company|title|url so date-less / id-less sources still dedup.
        """
        if self.job_id:
            return f"{self.source}:{self.job_id}"
        raw = f"{self.company.strip().lower()}|{self.title.strip().lower()}|{self.url.strip().lower()}"
        return f"{self.source}:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def as_row(self) -> dict:
        return {
            "company": self.company,
            "title": self.title,
            "category": self.category,
            "subcategory": self.subcategory,
            "posted_date": self.posted_date.isoformat() if self.posted_date else "",
            "first_seen": self.first_seen.isoformat() if self.first_seen else "",
            "days_since_posted": self.days_since_posted if self.days_since_posted is not None else "",
            "region": self.region,
            "location": self.location,
            "source": self.source,
            "url": self.url,
            "match_reason": self.match_reason,
            "poc_name": self.poc_name,
            "poc_designation": self.poc_designation,
            "poc_profile": self.poc_profile,
            "poc_source": self.poc_source,
        }
