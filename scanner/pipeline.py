"""The pipeline: fetch -> title filter -> geo+recency filter -> dedup/first-seen -> write.

Connectors are independent; if one returns nothing (no key, or an error) the
run still completes on the others. To widen coverage, register another
connector in `build_connectors` — nothing downstream changes.
"""
from __future__ import annotations

import csv
import logging
import os
import re
import shutil
from datetime import date, datetime

from scanner import classifier, filters, poc_enricher
from scanner.config import Config
from scanner.connectors.aggregator import AggregatorConnector
from scanner.connectors.ats import ATSConnector
from scanner.connectors.base import Connector
from scanner.connectors.tinyfish import TinyFishConnector
from scanner.models import NormalizedJob
from scanner.seen_store import SeenStore

log = logging.getLogger(__name__)


def build_connectors(cfg: Config) -> list[Connector]:
    connectors: list[Connector] = []
    if cfg.aggregator_enabled:
        connectors.append(AggregatorConnector(
            provider=cfg.aggregator_provider,
            api_key=cfg.aggregator_key(),
            titles=cfg.titles,
            countries=cfg.aggregator_countries,
            recency_days=cfg.recency_days,
            queries=cfg.aggregator_queries,
        ))
    if cfg.ats_enabled:
        connectors.append(ATSConnector(cfg.ats_companies))
    if cfg.tinyfish_enabled:
        connectors.append(TinyFishConnector(
            api_key=cfg.tinyfish_key,
            targets=cfg.tinyfish_targets,
            titles=cfg.titles,
        ))
    return connectors


def _title_matches(title: str, titles: list[str],
                   patterns: list[re.Pattern] | None = None,
                   excludes: list[re.Pattern] | None = None) -> bool:
    """Keep this title? An exclusion always wins (intern, insurance
    underwriter, etc. are dropped even if an include pattern matched)."""
    if excludes and any(p.search(title) for p in excludes):
        return False
    if patterns:
        return any(p.search(title) for p in patterns)
    t = title.lower()
    return any(kw.lower() in t for kw in titles)


def _compile(raw_patterns: list[str], label: str) -> list[re.Pattern]:
    compiled = []
    for p in raw_patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning("Bad %s %r skipped: %s", label, p, e)
    return compiled


def _compile_patterns(cfg: Config) -> list[re.Pattern]:
    return _compile(cfg.title_patterns, "title_pattern")


def _compile_excludes(cfg: Config) -> list[re.Pattern]:
    return _compile(cfg.title_exclude_patterns, "title_exclude_pattern")


def run(cfg: Config, today: date | None = None) -> list[NormalizedJob]:
    today = today or date.today()
    store = SeenStore(cfg.seen_db)
    kept: list[NormalizedJob] = []
    seen_keys: set[str] = set()
    patterns = _compile_patterns(cfg)
    excludes = _compile_excludes(cfg)
    regions = cfg.regions

    for connector in build_connectors(cfg):
        try:
            raw = connector.fetch()
        except Exception as e:  # a connector must never kill the whole run
            log.warning("Connector %s crashed: %s", connector.name, e)
            continue

        for job in raw:
            # 1. title filter (exclusions win; then regex patterns / keyword list)
            if cfg.require_title_match and not _title_matches(job.title, cfg.titles, patterns, excludes):
                continue
            # 2. geography filter — detect region, keep if it's one we want
            region = filters.region_of(job.location)
            job.region = region
            if job.location:
                if region not in regions:
                    continue
            elif not cfg.us_empty_location_passes:
                continue  # blank location and we don't allow blanks
            # 3. dedup within this run
            key = job.dedup_key()
            if key in seen_keys:
                continue
            # 4. first-seen + age (check "is this brand new?" BEFORE we record it)
            newly_seen = store.is_new(key)
            job.first_seen = store.first_seen(key, today)
            anchor = job.posted_date or job.first_seen
            job.days_since_posted = (today - anchor).days
            # 5. recency filter — a never-before-seen posting gets a grace
            #    window (first_seen_grace_days) so a skipped day doesn't lose
            #    postings whose date is a couple of days old on first sight.
            effective_days = (max(cfg.recency_days, cfg.first_seen_grace_days)
                              if newly_seen else cfg.recency_days)
            if not filters.within_recency(anchor, today, effective_days):
                continue
            seen_keys.add(key)
            kept.append(job)

    store.close()
    kept.sort(key=lambda j: (j.days_since_posted if j.days_since_posted is not None else 999))

    # 6. rule-based classification — only postings that survived every filter
    if cfg.classifier_enabled:
        try:
            classifier.classify(kept, cfg)
        except Exception as e:  # classification must never kill the run
            log.warning("Classifier crashed: %s — postings left unclassified", e)

    # 7. POC enrichment — per unique company, after classification
    if cfg.poc_enrichment_enabled:
        try:
            poc_enricher.enrich(kept, cfg)
        except Exception as e:  # enrichment must never kill the run
            log.warning("POC enrichment crashed: %s — POC fields left blank", e)

    _write_csv(kept, cfg.csv_path)
    # Live snapshot + immutable dated archive: jobs.csv is overwritten every
    # run, so each run also keeps a timestamped copy no later run touches.
    root, ext = os.path.splitext(cfg.csv_path)
    archive_path = f"{root}_{datetime.now().strftime('%Y-%m-%d_%H%M')}{ext}"
    shutil.copyfile(cfg.csv_path, archive_path)
    by_region = {}
    for j in kept:
        by_region[j.region or "Unknown"] = by_region.get(j.region or "Unknown", 0) + 1
    region_summary = ", ".join(f"{r}:{n}" for r, n in sorted(by_region.items()))
    log.info("KEPT %d postings (%s; <= %d days) -> %s (archived copy: %s)",
             len(kept), region_summary or "none", cfg.recency_days, cfg.csv_path, archive_path)
    return kept


def _write_csv(jobs: list[NormalizedJob], path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = ["company", "title", "category", "subcategory", "posted_date", "first_seen",
            "days_since_posted", "region", "location", "source", "url", "match_reason",
            "poc_name", "poc_designation", "poc_profile", "poc_source"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for j in jobs:
            w.writerow(j.as_row())
