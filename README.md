# Job Scanner

## How to use the app (no terminal needed)

1. Double-click **`start_app.bat`** in this folder.
2. A browser tab opens automatically at `http://127.0.0.1:5000` — that's the app.
3. Click **Run Scan**. Watch progress on the **Progress Log** tab (each board's
   live-browser link shows up there too).
4. When it finishes, the **Results** tab fills in — sort any column by clicking
   its header, filter with the search box, filter by category, and click
   **Download CSV** to save a copy.
5. Use the **Settings** tab to paste in API keys, or edit the source list /
   title filters / classifier keyword rules — no text editor required.

Closing the browser tab does not stop the app; close the black console window
(or press Ctrl+C in it) when you're done. Everything below this point is the
technical reference for the underlying scanner.

## Using it on your phone (optional, off by default)

The app can be installed on your phone like a real app — its own icon, opens
full-screen, no browser address bar — but the actual scanning still runs on
this PC (it's the machine doing the web-scraping work), so your phone talks
to it over your home WiFi.

This is **off by default** — today, the app only answers on this PC
(`127.0.0.1`), exactly as before. To turn phone access on:

1. Open `config.yaml`, find `web_app:`, set `allow_lan_access: true`.
2. Open `.env`, set `WEB_APP_PASSWORD=` to a password of your choice. This is
   required — without it, the app stays PC-only for safety even if step 1 is
   done, since turning this on makes it reachable by *every* device on your
   WiFi, not just your phone.
3. Restart the app (close the console window, double-click `start_app.bat`
   again). It prints two links: one for this PC, one for your phone.
4. On your phone (same WiFi), open the phone link, type the password, then:
   - **iPhone (Safari):** tap the Share icon → **Add to Home Screen**.
   - **Android (Chrome):** tap the menu (⋮) → **Install app** (or you'll see
     an automatic install banner).

Your phone now has a Job Scanner icon that opens the dashboard full-screen.
It only works while this PC is on, running the app, and on the same WiFi —
there's no cloud server involved, so nothing about your API keys or data
leaves your own network.

---

A small, on-demand agent that finds job postings across **multiple sources at once**
and returns **company name + posting date**, filtered to a target country (default US)
and a recency window (default ≤ 7 days), deduplicated across runs.

Built to *stack* sources, not narrow them: each source is a connector behind one
interface, so adding coverage never touches the rest of the code.

```
Aggregator API ─┐
ATS feeds     ──┼─► normalize ─► title filter ─► US + ≤7d filter ─► dedup/first-seen ─► CSV
TinyFish      ─┘
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env        # fill keys for paid connectors (optional)
python run.py               # runs every enabled connector, writes output/jobs.csv
```

Runs out of the box on the free **ATS** connector (no key needed) — just put your
target companies in `config.yaml`. The aggregator and TinyFish connectors activate
the moment you add their keys to `.env`; until then they self-skip.

```bash
python run.py --days 14     # override the recency window
python run.py -v            # verbose per-connector logging
```

## How it's put together

| File | Role |
|---|---|
| `run.py` | CLI entrypoint |
| `config.yaml` | all tunables: titles, country, recency, which connectors, watchlist |
| `.env` | secrets only (API keys) |
| `scanner/models.py` | `NormalizedJob` — the one schema every source maps into |
| `scanner/connectors/base.py` | the `Connector` interface: `fetch() -> list[NormalizedJob]` |
| `scanner/connectors/ats.py` | **Connector B** — Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Workday public feeds |
| `scanner/connectors/aggregator.py` | **Connector A** — provider-agnostic backbone |
| `scanner/connectors/adapters/` | vendor adapters (JSearch, TheirStack, PredictLeads) + registry |
| `scanner/connectors/tinyfish.py` | **Connector C** — niche boards with no API |
| `scanner/pipeline.py` | fetch → filter → classify → dedup/first-seen → CSV |
| `scanner/classifier.py` | rule-based category/subcategory (see `classifier_rules.yaml`) |
| `scanner/seen_store.py` | SQLite first-seen tracking |
| `scanner/filters.py` | US-location + recency predicates |
| `app.py` | the desktop web app — dashboard, live progress, settings editor |

## Provider-agnostic aggregator

`config.yaml` picks the vendor by name; the connector loads that adapter. Switch
vendors in one line; add a vendor by dropping a file in `adapters/` and registering it:

```yaml
connectors:
  aggregator:
    enabled: true
    provider: theirstack     # or predictleads
```

The two shipped adapters are **sketched against each vendor's API shape** — confirm the
exact endpoint/field names against current docs and add your key to activate. They
return `[]` (self-skip) without a key, so the rest of the run is unaffected.

## Posting date — and the date-less sources

- Sources that expose a real post date (Lever, Ashby, Workable, SmartRecruiters,
  aggregator APIs) → `posted_date` is used directly.
- Sources without a reliable one (**Greenhouse** only gives `updated_at`) → the scanner
  records the date it **first saw** each posting and computes age from that. After a few
  runs, "posted in the last 7 days" becomes accurate on every source going forward.

Each output row: `company, title, category, subcategory, posted_date, first_seen, days_since_posted, location, source, url, match_reason`.

## Adding a source (widening coverage)

1. Write a class in `scanner/connectors/` with `fetch() -> list[NormalizedJob]`.
2. Register it in `pipeline.build_connectors`.

That's it. No other file changes. This is how you keep expanding coverage without narrowing anything.

## A note on LinkedIn

Point the TinyFish connector at company career pages and no-API niche boards — **not** at
authenticated LinkedIn. The same roles that appear on LinkedIn are syndicated to company
ATS/career pages and picked up by the aggregator layer, so you keep the coverage; you just
reach it through a source that doesn't carry account-ban and terms-of-service risk. This is
a business/compliance decision for your team — worth confirming with whoever handles it.
