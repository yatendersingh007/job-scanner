"""Load config.yaml + environment. Everything tunable lives in config;
secrets live in the environment. Neither lives in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    recency_days: int
    target_country: str
    titles: list[str]
    ats_enabled: bool
    ats_companies: list[dict]
    aggregator_enabled: bool
    aggregator_provider: str
    tinyfish_enabled: bool
    tinyfish_targets: list[dict]
    csv_path: str
    seen_db: str
    require_title_match: bool = True
    us_empty_location_passes: bool = True    # blank-location postings pass the geo gate (any region)
    title_patterns: list[str] = field(default_factory=list)   # regex gate; falls back to `titles`
    title_exclude_patterns: list[str] = field(default_factory=list)  # drop even if a title_pattern matched
    aggregator_queries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=lambda: ["US", "Canada"])
    aggregator_countries: list[str] = field(default_factory=lambda: ["us", "ca"])
    first_seen_grace_days: int = 3    # a never-seen posting still passes if posted within this many days
    classifier_enabled: bool = True
    classifier_rules_file: str = "classifier_rules.yaml"
    poc_enrichment_enabled: bool = True
    poc_enrich_cap: int = 10
    poc_force_refresh: bool = False
    poc_cache_db: str = "poc_cache.db"
    poc_blocklist: list[str] = field(default_factory=list)
    web_app_allow_lan: bool = False   # true = phone can reach this app over WiFi (requires a password, see .env)

    @property
    def web_app_password(self):
        return os.getenv("WEB_APP_PASSWORD")

    @property
    def theirstack_key(self):
        return os.getenv("THEIRSTACK_API_KEY")

    @property
    def predictleads_key(self):
        return os.getenv("PREDICTLEADS_API_KEY")

    @property
    def jsearch_key(self):
        return os.getenv("JSEARCH_API_KEY")

    @property
    def tinyfish_key(self):
        return os.getenv("TINYFISH_API_KEY")

    def aggregator_key(self):
        return {
            "jsearch": self.jsearch_key,
            "theirstack": self.theirstack_key,
            "predictleads": self.predictleads_key,
        }.get(self.aggregator_provider.lower())


def load(path: str = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:   # utf-8 so comments with — / accents load on any OS
        raw = yaml.safe_load(f) or {}
    conn = raw.get("connectors", {})
    ats = conn.get("ats", {})
    agg = conn.get("aggregator", {})
    tf = conn.get("tinyfish", {})
    cls = raw.get("classifier", {})
    poc = raw.get("poc_enrichment", {})
    web = raw.get("web_app", {})
    out = raw.get("output", {})
    return Config(
        recency_days=raw.get("recency_days", 7),
        target_country=raw.get("target_country", "US"),
        titles=raw.get("titles", []),
        title_patterns=raw.get("title_patterns", []),
        title_exclude_patterns=raw.get("title_exclude_patterns", []),
        require_title_match=raw.get("require_title_match", True),
        us_empty_location_passes=raw.get("us_empty_location_passes", True),
        regions=raw.get("regions", ["US", "Canada"]),
        first_seen_grace_days=raw.get("first_seen_grace_days", 3),
        ats_enabled=ats.get("enabled", True),
        ats_companies=ats.get("companies", []),
        aggregator_enabled=agg.get("enabled", True),
        aggregator_provider=agg.get("provider", "theirstack"),
        aggregator_queries=agg.get("queries", []),
        aggregator_countries=agg.get("countries", ["us", "ca"]),
        tinyfish_enabled=tf.get("enabled", False),
        tinyfish_targets=tf.get("targets", []),
        classifier_enabled=cls.get("enabled", True),
        classifier_rules_file=cls.get("rules_file", "classifier_rules.yaml"),
        poc_enrichment_enabled=poc.get("enabled", True),
        poc_enrich_cap=poc.get("cap_per_run", 10),
        poc_force_refresh=poc.get("force_refresh", False),
        poc_cache_db=poc.get("cache_db", "poc_cache.db"),
        poc_blocklist=poc.get("blocklist", []),
        web_app_allow_lan=web.get("allow_lan_access", False),
        csv_path=out.get("csv_path", "output/jobs.csv"),
        seen_db=out.get("seen_db", "seen.db"),
    )
