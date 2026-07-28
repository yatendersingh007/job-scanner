"""Connector A — the aggregator backbone.

Provider-agnostic: it holds no vendor logic. It reads which provider to use
from config, loads that adapter, and asks it for postings. Switch vendors by
changing one line of config; add a vendor by adding one adapter file.
"""
from __future__ import annotations

import logging

from scanner.connectors.adapters import get_adapter
from scanner.connectors.base import Connector
from scanner.models import NormalizedJob

log = logging.getLogger(__name__)


class AggregatorConnector(Connector):
    name = "aggregator"

    def __init__(self, *, provider: str, api_key, titles, countries, recency_days, queries=None):
        self.provider = provider
        self.adapter = get_adapter(provider, api_key)
        self.titles = titles
        self.countries = countries or ["us"]
        self.recency_days = recency_days
        self.queries = queries or []

    def fetch(self) -> list[NormalizedJob]:
        jobs = self.adapter.fetch(
            titles=self.titles,
            country=self.countries[0],   # single-country adapters use the first
            countries=self.countries,    # multi-country adapters (JSearch) use them all
            recency_days=self.recency_days,
            queries=self.queries,
        )
        log.info("Aggregator (%s): %d roles across %d countries",
                 self.provider, len(jobs), len(self.countries))
        return jobs
