"""First-seen tracking.

Not every source gives a true post date (Greenhouse being the notable one).
So we record the date THIS scanner first saw each posting. Age is then:
  - days since posted_date, when the source provides a real one, else
  - days since first_seen.
After a few runs this makes "posted in the last 7 days" accurate on every
source going forward. Uses stdlib sqlite3 — no dependency, survives restarts.
"""
from __future__ import annotations

import sqlite3
from datetime import date


class SeenStore:
    def __init__(self, path: str = "seen.db"):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            "  key TEXT PRIMARY KEY,"
            "  first_seen TEXT NOT NULL"
            ")"
        )
        self.conn.commit()

    def first_seen(self, key: str, today: date) -> date:
        """Return the stored first-seen date for key, inserting today if new."""
        cur = self.conn.execute("SELECT first_seen FROM seen WHERE key = ?", (key,))
        row = cur.fetchone()
        if row:
            return date.fromisoformat(row[0])
        self.conn.execute(
            "INSERT INTO seen (key, first_seen) VALUES (?, ?)",
            (key, today.isoformat()),
        )
        self.conn.commit()
        return today

    def is_new(self, key: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
        return cur.fetchone() is None

    def close(self):
        self.conn.close()
