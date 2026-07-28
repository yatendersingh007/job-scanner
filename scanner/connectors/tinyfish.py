"""Connector C — TinyFish, for niche boards that have no public API.

Point it at board search URLs (SelectLeaders, eFinancialCareers, Job Search
Digest). Each target gets a plain-English goal plus an output_schema that pins
a strict JSON shape so the output drops straight into NormalizedJob.
Self-skips without a key.

Verified against https://docs.tinyfish.ai (July 2026):
  - start:  POST https://agent.tinyfish.ai/v1/automation/run-async
            body {"url", "goal", "output_schema"}, header X-API-Key
            → {"run_id": "..."}
  - poll:   GET  https://agent.tinyfish.ai/v1/runs/{run_id}
            → {"status": PENDING|RUNNING|COMPLETED|FAILED|CANCELLED,
               "result": {...}, "error": {...}}
The synchronous /run endpoint is documented for <30s tasks only; job-board
scans run minutes, hence async + poll.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime

import requests

from scanner.connectors.base import Connector
from scanner.models import NormalizedJob

log = logging.getLogger(__name__)

BASE = "https://agent.tinyfish.ai/v1"
START_ENDPOINT = f"{BASE}/automation/run-async"
RUN_ENDPOINT = f"{BASE}/runs/{{run_id}}"
HTTP_TIMEOUT = 60
POLL_INTERVAL = 10
MAX_WAIT = 900  # seconds per target before giving up; dense boards (90+ listings) exceed 600

GOAL_TEMPLATE = (
    "This is a job board search results page. Extract every job posting listed "
    "whose title contains any of: {titles}. For each posting capture the company "
    "name, exact job title, location, posting date (YYYY-MM-DD, empty string if "
    "not shown), and the direct URL to the posting. If the page shows no matching "
    "jobs, return an empty jobs list."
)

# TinyFish output_schema: top level must be an object; arrays nest inside a
# property. Only the documented keyword subset is allowed.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "title": {"type": "string"},
                    "location": {"type": "string"},
                    "posted_date": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["title", "url"],
            },
        }
    },
    "required": ["jobs"],
}


class TinyFishConnector(Connector):
    name = "tinyfish"

    def __init__(self, *, api_key, targets: list[dict], titles: list[str]):
        self.api_key = api_key
        self.targets = targets or []
        self.titles = titles

    def fetch(self) -> list[NormalizedJob]:
        if not self.api_key:
            log.info("TinyFish: no API key set — skipping (add TINYFISH_API_KEY to enable)")
            return []
        goal = GOAL_TEMPLATE.format(titles=", ".join(f'"{t}"' for t in self.titles))
        jobs: list[NormalizedJob] = []
        for t in self.targets:
            name, url = t.get("name", "board"), t.get("url")
            if not url:
                continue
            try:
                log.info("TinyFish %s: starting agent run on %s", name, url)
                rows = self._run(url, goal)
                log.info("TinyFish %-22s %d roles", name, len(rows))
                for j in rows:
                    jobs.append(NormalizedJob(
                        company=j.get("company", ""),
                        title=j.get("title", ""),
                        url=j.get("url", ""),
                        source=f"tinyfish:{name}",
                        posted_date=_parse_date(j.get("posted_date")),
                        location=j.get("location", ""),
                    ))
            except (requests.RequestException, ValueError, TimeoutError) as e:
                log.warning("TinyFish target %s failed: %s", name, e)
        return jobs

    def _headers(self):
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def _run(self, url, goal) -> list[dict]:
        body = {"url": url, "goal": goal, "output_schema": OUTPUT_SCHEMA}
        r = requests.post(START_ENDPOINT, json=body, headers=self._headers(), timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        run_id = r.json().get("run_id")
        if not run_id:
            raise ValueError(f"no run_id in response: {r.text[:200]}")
        log.info("TinyFish run %s accepted — track it on the dashboard at "
                 "https://agent.tinyfish.ai", run_id)
        run = self._poll(run_id)
        status = run.get("status")
        if status != "COMPLETED":
            err = run.get("error") or status
            raise ValueError(f"run {run_id} ended {status}: {err}")
        return _extract_jobs(run.get("result"))

    def _poll(self, run_id) -> dict:
        start = time.monotonic()
        deadline = start + MAX_WAIT
        streaming_logged = False
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            # A status check is retryable: DNS blips and 5xx gateway errors
            # must not abandon a run that is still executing server-side.
            try:
                r = requests.get(RUN_ENDPOINT.format(run_id=run_id),
                                 headers=self._headers(), timeout=HTTP_TIMEOUT)
                if r.status_code >= 500:
                    log.info("TinyFish run %s: server hiccup (HTTP %d) — retrying",
                             run_id, r.status_code)
                    continue
                r.raise_for_status()
                run = r.json()
            except requests.RequestException as e:
                log.info("TinyFish run %s: status check failed (%s) — retrying", run_id, e)
                continue
            if not streaming_logged and run.get("streaming_url"):
                log.info("TinyFish run %s: watch the browser live at %s",
                         run_id, run["streaming_url"])
                streaming_logged = True
            if run.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
                return run
            log.info("TinyFish run %s: %s (%ds elapsed)",
                     run_id, run.get("status", "?"), int(time.monotonic() - start))
        raise TimeoutError(f"run {run_id} still not finished after {MAX_WAIT}s")


def _extract_jobs(result) -> list[dict]:
    """The run's `result` holds the schema-shaped data, but tolerate the
    documented variations: JSON encoded as a string, or nested one level
    under result/data/output."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return []
    if isinstance(result, list):
        return [j for j in result if isinstance(j, dict)]
    if isinstance(result, dict):
        jobs = result.get("jobs")
        if isinstance(jobs, list):
            return [j for j in jobs if isinstance(j, dict)]
        for key in ("result", "data", "output"):
            if key in result:
                return _extract_jobs(result[key])
    return []


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        return None
