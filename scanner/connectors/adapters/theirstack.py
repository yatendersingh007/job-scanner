"""TheirStack adapter.

Sketched against TheirStack's job-postings API shape. Fill in the exact
request body/field names from their current docs (https://theirstack.com)
before enabling. Without an API key it self-skips and returns [].
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from scanner.connectors.adapters.base import AggregatorAdapter
from scanner.models import NormalizedJob

log = logging.getLogger(__name__)
ENDPOINT = "https://api.theirstack.com/v1/jobs/search"  # verify in docs
TIMEOUT = 30


class TheirStackAdapter(AggregatorAdapter):
    name = "theirstack"

    def fetch(self, *, titles, country, recency_days, queries=None, countries=None) -> list[NormalizedJob]:
        if not self.api_key:
            log.info("TheirStack: no API key set — skipping (add THEIRSTACK_API_KEY to enable)")
            return []
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # NOTE: confirm exact filter keys against TheirStack docs.
        payload = {
            "page": 0,
            "limit": 100,
            "job_title_or": titles,
            "job_country_code_or": [country],
            "posted_at_max_age_days": recency_days,
        }
        try:
            r = requests.post(ENDPOINT, json=payload, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            log.warning("TheirStack request failed: %s", e)
            return []

        out = []
        for j in data.get("data", data.get("results", [])):
            out.append(NormalizedJob(
                company=j.get("company") or (j.get("company_object") or {}).get("name", ""),
                title=j.get("job_title") or j.get("title", ""),
                url=j.get("url") or j.get("final_url", ""),
                source="aggregator:theirstack",
                posted_date=_parse_date(j.get("date_posted") or j.get("posted_at")),
                location=j.get("location", ""),
                job_id=str(j.get("id", "")),
            ))
        return out


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None
