#!/usr/bin/env python3
"""Job Scanner — run on demand.

    python run.py                # uses config.yaml
    python run.py --config x.yaml
    python run.py --days 14      # override recency window

Finds job postings (company + posting date) across all enabled connectors,
filtered to the target country and recency window, deduplicated across runs.
"""
from __future__ import annotations

import argparse
import logging

from scanner import config as config_mod
from scanner import pipeline


def main():
    ap = argparse.ArgumentParser(description="Multi-source job posting scanner")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int, help="override recency_days")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = config_mod.load(args.config)
    if args.days:
        cfg.recency_days = args.days

    jobs = pipeline.run(cfg)

    print(f"\n{'COMPANY':<32} {'DAYS':>4}  {'DATE':<11} TITLE")
    print("-" * 90)
    for j in jobs[:50]:
        print(f"{j.company[:31]:<32} {j.days_since_posted:>4}  "
              f"{(j.posted_date.isoformat() if j.posted_date else j.first_seen.isoformat()):<11} "
              f"{j.title[:40]}")
    if not jobs:
        print("(no matching postings — enable an aggregator key or add ATS companies)")
    print(f"\nFull results: {cfg.csv_path}")


if __name__ == "__main__":
    main()
