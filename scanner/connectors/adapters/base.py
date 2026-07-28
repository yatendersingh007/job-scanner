"""Provider-agnostic aggregator adapters.

The aggregator connector doesn't know or care which vendor it talks to. It
delegates to an adapter that implements two methods: build the request, and
parse the response into NormalizedJob. Swap TheirStack <-> PredictLeads (or
add a third) by writing one adapter — the pipeline never changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from scanner.models import NormalizedJob


class AggregatorAdapter(ABC):
    name: str = "base"
    requires_key: bool = True

    def __init__(self, api_key: str | None):
        self.api_key = api_key

    @abstractmethod
    def fetch(self, *, titles: list[str], country: str, recency_days: int,
              queries: list[str] | None = None,
              countries: list[str] | None = None) -> list[NormalizedJob]:
        """Query the vendor for postings matching titles/country/recency,
        return them as NormalizedJob. Must be defensive (return [] on error).
        `queries` is an optional list of free-text search phrases for vendors
        whose API is search-driven rather than filter-driven. `countries` is
        an optional list of country codes for multi-country vendors (e.g.
        JSearch); single-country vendors just use `country`."""
        raise NotImplementedError
