"""Deterministic rule-based classifier — assigns category/subcategory.

No LLM, no API, no keys. Keyword rules are applied to the job title +
description + company name together:

  1. Employer-type signals (family-office vs real-estate keyword lists) are
     searched in company name + description (whole-word, case-insensitive).
  2. Category: FO signals -> Family Office; else RE signals -> Real Estate;
     both -> more distinct hits wins, tie -> Other/"Needs Review";
     neither -> Other/"Needs Review". Never guesses.
  3. Subcategory: ordered title-keyword rules within the decided category,
     first match wins; no match -> "Needs Review".

Every keyword list lives in an editable YAML file (classifier_rules.yaml) —
tune the rules there, not here. Each decision records the keywords that
fired in `match_reason` so rows are auditable.
"""
from __future__ import annotations

import logging
import re

import yaml

from scanner.models import NormalizedJob

log = logging.getLogger(__name__)


def _phrase_pattern(keyword: str) -> re.Pattern:
    """Whole-word, case-insensitive, whitespace-flexible phrase matcher.
    'cre' matches 'CRE' but never 'concrete'; 'rent roll' matches
    'rent  roll' too."""
    parts = [re.escape(p) for p in keyword.split()]
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)


class Rules:
    def __init__(self, raw: dict):
        self.include_title = bool(raw.get("include_title_in_signals", False))
        self.fo_signals = [(kw, _phrase_pattern(kw)) for kw in raw.get("family_office_signals", [])]
        self.re_signals = [(kw, _phrase_pattern(kw)) for kw in raw.get("real_estate_signals", [])]
        self.subcats: dict[str, list[tuple[str, list[tuple[str, re.Pattern]]]]] = {}
        for category, rules in (raw.get("subcategories") or {}).items():
            self.subcats[category] = [
                (r["name"], [(kw, _phrase_pattern(kw)) for kw in r.get("title_keywords", [])])
                for r in rules
            ]


def load_rules(path: str) -> Rules | None:
    try:
        with open(path, encoding="utf-8") as f:
            return Rules(yaml.safe_load(f) or {})
    except OSError as e:
        log.warning("Classifier: cannot read rules file %s: %s", path, e)
        return None


def classify(jobs: list[NormalizedJob], cfg) -> None:
    """Fill category/subcategory/match_reason on each job, in place."""
    if not jobs:
        return
    rules = load_rules(cfg.classifier_rules_file)
    if rules is None:
        return
    for job in jobs:
        _classify_one(job, rules)
    review = sum(1 for j in jobs if j.subcategory == "Needs Review")
    log.info("Classifier: %d postings classified by rules (%d flagged Needs Review)",
             len(jobs), review)


def _classify_one(job: NormalizedJob, rules: Rules) -> None:
    signal_text = f"{job.company}\n{job.description}"
    if rules.include_title:
        signal_text += f"\n{job.title}"

    fo_hits = [kw for kw, pat in rules.fo_signals if pat.search(signal_text)]
    re_hits = [kw for kw, pat in rules.re_signals if pat.search(signal_text)]

    if fo_hits and re_hits:
        if len(fo_hits) > len(re_hits):
            category, why = "Family Office", f"FO wins {len(fo_hits)}:{len(re_hits)} ({_join(fo_hits)})"
        elif len(re_hits) > len(fo_hits):
            category, why = "Real Estate", f"RE wins {len(re_hits)}:{len(fo_hits)} ({_join(re_hits)})"
        else:
            job.category, job.subcategory = "Other", "Needs Review"
            job.match_reason = f"tie — FO({_join(fo_hits)}) vs RE({_join(re_hits)})"
            return
    elif fo_hits:
        category, why = "Family Office", f"FO signals: {_join(fo_hits)}"
    elif re_hits:
        category, why = "Real Estate", f"RE signals: {_join(re_hits)}"
    else:
        job.category, job.subcategory = "Other", "Needs Review"
        job.match_reason = "no employer-type signals in company/description"
        return

    job.category = category
    for name, keywords in rules.subcats.get(category, []):
        hit = next((kw for kw, pat in keywords if pat.search(job.title)), None)
        if hit:
            job.subcategory = name
            job.match_reason = f"{why} | title: {hit}"
            return
    job.subcategory = "Needs Review"
    job.match_reason = f"{why} | no title rule matched"


def _join(hits: list[str], cap: int = 4) -> str:
    shown = ", ".join(hits[:cap])
    return shown + (f" +{len(hits) - cap}" if len(hits) > cap else "")
