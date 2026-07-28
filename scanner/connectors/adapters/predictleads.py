"""PredictLeads adapter.

Sketched against PredictLeads' job-openings API shape. Confirm exact auth
(API key + token pair) and field names from their docs
(https://predictleads.com) before enabling. Self-skips without a key.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from scanner.connectors.adapters.base import AggregatorAdapter
from scanner.models import NormalizedJob

log = logging.getLogger(__name__)
ENDPOINT = "https://predictleads.com/api/v3/job_openings"  # verify in docs
TIMEOUT = 30


class PredictLeadsAdapter(AggregatorAdapter):
    name = "predictleads"

    def fetch(self, *, titles, country, recency_days, queries=None, countries=None) -> list[NormalizedJob]:
        if not self.api_key:
            log.info("PredictLeads: no API key set — skipping (add PREDICTLEADS_API_KEY to enable)")
            return []
        # PredictLeads commonly uses an API key + API token header pair.
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Token": os.getenv("PREDICTLEADS_API_TOKEN", ""),
            "Content-Type": "application/json",
        }
        params = {
            "limit": 100,
            "categories": ",".join(titles),
            "country": country,
            "last_seen_days": recency_days,
        }
        try:
            r = requests.get(ENDPOINT, params=params, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except requests.RequestException as e:
            log.warning("PredictLeads request failed: %s", e)
            return []

        out = []
        for item in data.get("data", []):
            attrs = item.get("attributes", item)
            out.append(NormalizedJob(
                company=attrs.get("company_name", ""),
                title=attrs.get("title", ""),
                url=attrs.get("url", ""),
                source="aggregator:predictleads",
                posted_date=_parse_date(attrs.get("first_seen_at") or attrs.get("date")),
                location=attrs.get("location", ""),
                job_id=str(item.get("id", "")),
            ))
        return out


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None
