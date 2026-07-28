"""POC (point-of-contact) lookup cache — one row per company.

A company is looked up over TinyFish ONCE EVER; every later run reads this
cache instead of spending TinyFish steps again, unless a refresh is forced
(see config.yaml poc_enrichment.force_refresh). Uses stdlib sqlite3, same
pattern as seen_store.py.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

_COLUMNS = ["company_name", "poc_name", "poc_designation", "poc_profile",
            "poc_source", "company_domain"]


class PocStore:
    def __init__(self, path: str = "poc_cache.db"):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS poc_cache ("
            "  company_key TEXT PRIMARY KEY,"
            "  company_name TEXT,"
            "  poc_name TEXT,"
            "  poc_designation TEXT,"
            "  poc_profile TEXT,"
            "  poc_source TEXT,"
            "  company_domain TEXT,"
            "  looked_up_at TEXT"
            ")"
        )
        self.conn.commit()

    def get(self, company_key: str) -> dict | None:
        cur = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM poc_cache WHERE company_key = ?",
            (company_key,),
        )
        row = cur.fetchone()
        return dict(zip(_COLUMNS, row)) if row else None

    def set(self, company_key: str, *, company_name: str, poc_name: str,
            poc_designation: str, poc_profile: str, poc_source: str,
            company_domain: str = "") -> None:
        self.conn.execute(
            "INSERT INTO poc_cache (company_key, company_name, poc_name, poc_designation, "
            "  poc_profile, poc_source, company_domain, looked_up_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(company_key) DO UPDATE SET "
            "  company_name=excluded.company_name, poc_name=excluded.poc_name, "
            "  poc_designation=excluded.poc_designation, poc_profile=excluded.poc_profile, "
            "  poc_source=excluded.poc_source, company_domain=excluded.company_domain, "
            "  looked_up_at=excluded.looked_up_at",
            (company_key, company_name, poc_name, poc_designation, poc_profile,
             poc_source, company_domain, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
