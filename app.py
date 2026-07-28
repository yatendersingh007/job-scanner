#!/usr/bin/env python3
"""Job Scanner — installable web app (desktop + mobile).

Run this file (or double-click start_app.bat) and it opens a dashboard at
http://127.0.0.1:5000 on this PC. It's also reachable from your phone over
the same WiFi (the LAN URL is printed on startup) and can be "installed" —
Add to Home Screen on iPhone, or the Install-app prompt on Android Chrome —
so it opens full-screen with its own icon, like a real app.

One button — Run Scan — does the whole job: board scan, classification,
and POC enrichment in one pass, landing in one CSV with job + POC details
together. Everything else is just viewing that result: live progress, a
sortable/filterable table, CSV download, and a settings editor — all
without touching a terminal or YAML file by hand.

The heavy lifting (web scraping via TinyFish, long scans) runs here, on
this PC — that only works on a real computer, not inside a phone's app
sandbox — so the phone is a thin client to whichever machine runs this file.

The scan itself still runs scanner/pipeline.py — this file only adds a
local web front-end around it. Nothing here changes how jobs are fetched,
filtered, classified, or enriched.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import socket
import threading
import webbrowser
from datetime import datetime

import yaml
from flask import Flask, jsonify, redirect, request, send_file, session

from scanner import config as config_mod
from scanner import pipeline

app = Flask(__name__)

# Sessions only matter once phone/LAN access is turned on (see below) — the
# key is generated once and cached in a local, gitignored file so logins
# survive an app restart instead of booting everyone out each time.
_SECRET_KEY_FILE = ".flask_secret_key"
if os.path.exists(_SECRET_KEY_FILE):
    with open(_SECRET_KEY_FILE, encoding="utf-8") as f:
        app.secret_key = f.read().strip()
else:
    app.secret_key = secrets.token_hex(32)
    with open(_SECRET_KEY_FILE, "w", encoding="utf-8") as f:
        f.write(app.secret_key)

_PUBLIC_PATHS = {"/login"}
_PUBLIC_PREFIXES = ("/static/", "/sw.js")


@app.before_request
def _require_login_on_lan():
    """Desktop-only mode (the default) needs no login — it's just you, on
    your own PC, at 127.0.0.1. The moment LAN access is turned on so a
    phone can reach it too, every other device on the WiFi can reach it as
    well, so a password gate goes up around everything except the login
    page and static assets (icons/manifest — harmless, needed pre-login)."""
    cfg = config_mod.load("config.yaml")
    if not cfg.web_app_allow_lan:
        return None
    if request.path in _PUBLIC_PATHS or request.path.startswith(_PUBLIC_PREFIXES):
        return None
    if session.get("authed"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "Not logged in."}), 401
    return redirect("/login")


@app.get("/login")
def login_form():
    return LOGIN_HTML


@app.post("/login")
def login_submit():
    cfg = config_mod.load("config.yaml")
    password = request.form.get("password", "")
    expected = cfg.web_app_password or ""
    if expected and hmac.compare_digest(password, expected):
        session["authed"] = True
        session.permanent = True
        return redirect("/")
    return LOGIN_HTML.replace("<!--ERROR-->", '<p class="error">Wrong password.</p>')


def _lan_ip() -> str:
    """Best-effort local network IP (the address your phone would use) —
    opens no real connection, just asks the OS which interface would be
    used to reach the public internet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

STATE_LOCK = threading.Lock()
STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "last_result_count": None,
}
LOG_HISTORY: list[str] = []
MAX_LOG_HISTORY = 500


_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


class _QueueLogHandler(logging.Handler):
    """Mirrors scanner log lines into the in-memory queue the browser polls,
    so the dashboard shows the same play-by-play the terminal always showed.
    Flask/Werkzeug's own request-access logs are excluded — noise, not scan
    progress — via the logger name filter set up below."""

    def emit(self, record):
        line = _ANSI_RE.sub("", self.format(record))
        LOG_HISTORY.append(line)
        del LOG_HISTORY[:-MAX_LOG_HISTORY]


def _setup_logging():
    handler = _QueueLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("app")


def _run_scan_in_background():
    """One process, one button: Run Scan does the board scan,
    classification, AND POC enrichment (pipeline.run calls poc_enricher.
    enrich internally after classification) — the result is one CSV with
    job + POC details together. No separate enrichment step."""
    with STATE_LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, started_at=datetime.now().isoformat(),
                     finished_at=None, error=None)

    def worker():
        try:
            cfg = config_mod.load("config.yaml")
            jobs = pipeline.run(cfg)
            with STATE_LOCK:
                STATE["last_result_count"] = len(jobs)
        except Exception as e:
            log.exception("Scan failed: %s", e)
            with STATE_LOCK:
                STATE["error"] = str(e)
        finally:
            with STATE_LOCK:
                STATE["running"] = False
                STATE["finished_at"] = datetime.now().isoformat()

    threading.Thread(target=worker, daemon=True).start()
    return True


@app.get("/")
def home():
    return DASHBOARD_HTML


@app.get("/sw.js")
def service_worker():
    """Served from the root path (not /static/) so its default scope covers
    the whole app — a service worker registered from /static/sw.js would
    only control pages under /static/ without extra scope headers."""
    with open("static/sw.js", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "application/javascript"}


@app.post("/api/scan/start")
def start_scan():
    started = _run_scan_in_background()
    if not started:
        return jsonify({"ok": False, "message": "A scan is already running."}), 409
    return jsonify({"ok": True})


@app.get("/api/scan/status")
def scan_status():
    with STATE_LOCK:
        return jsonify(dict(STATE))


@app.get("/api/scan/log")
def scan_log():
    """Long-poll-ish: returns any new lines since the client's last cursor,
    identified by how many lines it has already seen."""
    since = request.args.get("since", type=int, default=0)
    lines = LOG_HISTORY[since:]
    return jsonify({"lines": lines, "next": since + len(lines)})


@app.get("/api/results")
def results():
    cfg = config_mod.load("config.yaml")
    try:
        with open(cfg.csv_path, newline="", encoding="utf-8") as f:
            import csv
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        rows = []
    return jsonify({"rows": rows, "csv_path": cfg.csv_path})


@app.get("/api/results/download")
def download_results():
    cfg = config_mod.load("config.yaml")
    return send_file(cfg.csv_path, as_attachment=True,
                     download_name=f"jobs_{datetime.now().strftime('%Y-%m-%d')}.csv")


@app.get("/api/settings")
def get_settings():
    with open("config.yaml", encoding="utf-8") as f:
        cfg_text = f.read()
    with open("classifier_rules.yaml", encoding="utf-8") as f:
        rules_text = f.read()
    env_keys = {}
    try:
        with open(".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_keys[k.strip()] = bool(v.strip())  # never send key values to the browser
    except FileNotFoundError:
        pass
    return jsonify({"config_yaml": cfg_text, "classifier_rules_yaml": rules_text, "env_keys_set": env_keys})


@app.post("/api/settings/config")
def save_config():
    text = request.json.get("text", "")
    try:
        yaml.safe_load(text)  # validate before writing
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "message": f"Invalid YAML: {e}"}), 400
    with open("config.yaml", "w", encoding="utf-8") as f:
        f.write(text)
    return jsonify({"ok": True})


@app.post("/api/settings/rules")
def save_rules():
    text = request.json.get("text", "")
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as e:
        return jsonify({"ok": False, "message": f"Invalid YAML: {e}"}), 400
    with open("classifier_rules.yaml", "w", encoding="utf-8") as f:
        f.write(text)
    return jsonify({"ok": True})


@app.post("/api/settings/env")
def save_env_key():
    key = request.json.get("key", "").strip()
    value = request.json.get("value", "").strip()
    if not key or not key.replace("_", "").isalnum():
        return jsonify({"ok": False, "message": "Invalid key name."}), 400
    lines = []
    try:
        with open(".env", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    with open(".env", "w", encoding="utf-8") as f:
        f.writelines(lines)
    return jsonify({"ok": True})


LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Scanner — Log in</title>
<style>
:root { color-scheme: light dark; }
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; display: flex; min-height: 100vh; align-items: center;
      justify-content: center; margin: 0; background: #f5f6f8; color: #1a1a1a; }
@media (prefers-color-scheme: dark) { body { background: #16181d; color: #e8e8e8; } }
form { background: #fff; padding: 32px; border-radius: 10px; border: 1px solid #e2e4e8; width: 280px; }
@media (prefers-color-scheme: dark) { form { background: #1e2127; border-color: #2c3038; } }
h1 { font-size: 18px; margin: 0 0 16px; }
input { width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #ccc; font: inherit; box-sizing: border-box; margin-bottom: 12px; }
button { width: 100%; padding: 10px; border-radius: 6px; border: none; background: #2563eb; color: #fff; font: inherit; cursor: pointer; }
.error { color: #dc2626; font-size: 13px; margin: -4px 0 12px; }
</style></head>
<body>
<form method="post" action="/login">
  <h1>Job Scanner</h1>
  <!--ERROR-->
  <input type="password" name="password" placeholder="Password" autofocus>
  <button type="submit">Log in</button>
</form>
</body></html>
"""


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#2563eb">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Job Scanner">
<link rel="manifest" href="/static/manifest.json">
<link rel="icon" href="/static/icons/icon-192.png">
<link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
<title>Job Scanner</title>
<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #f5f6f8; color: #1a1a1a; }
@media (prefers-color-scheme: dark) { body { background: #16181d; color: #e8e8e8; } }
header { padding: 16px max(16px, env(safe-area-inset-left)); background: #fff; border-bottom: 1px solid #e2e4e8; display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
@media (prefers-color-scheme: dark) { header { background: #1e2127; border-color: #2c3038; } }
header h1 { font-size: 18px; margin: 0; flex: 1; min-width: 120px; }
button { font: inherit; padding: 10px 18px; border-radius: 6px; border: 1px solid #ccc; background: #fff; cursor: pointer; min-height: 40px; }
button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
button.primary:disabled { background: #94a3b8; border-color: #94a3b8; cursor: not-allowed; }
@media (prefers-color-scheme: dark) { button { background: #262a33; border-color: #3a3f4a; color: #e8e8e8; } }
main { padding: 16px max(16px, env(safe-area-inset-left)); max-width: 1200px; margin: 0 auto; }
.tabs { display: flex; gap: 4px; margin-bottom: 0; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.tab { padding: 10px 16px; border-radius: 6px 6px 0 0; cursor: pointer; border: 1px solid transparent; white-space: nowrap; }
.tab.active { background: #fff; border-color: #e2e4e8; border-bottom-color: #fff; font-weight: 600; }
@media (prefers-color-scheme: dark) { .tab.active { background: #1e2127; border-color: #2c3038; border-bottom-color: #1e2127; } }
.panel { display: none; background: #fff; border: 1px solid #e2e4e8; border-radius: 0 8px 8px 8px; padding: 20px; }
.panel.active { display: block; }
@media (prefers-color-scheme: dark) { .panel { background: #1e2127; border-color: #2c3038; } }
#status { font-size: 13px; color: #666; }
@media (prefers-color-scheme: dark) { #status { color: #999; } }
#logbox { height: 260px; overflow-y: auto; background: #0d1117; color: #b6f0b6; font: 12px/1.5 Consolas, monospace; padding: 10px; border-radius: 6px; margin-top: 12px; white-space: pre-wrap; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; }
th, td { text-align: left; padding: 10px 10px; border-bottom: 1px solid #edf0f3; }
@media (prefers-color-scheme: dark) { th, td { border-color: #2c3038; } }
th { cursor: pointer; user-select: none; background: #fafbfc; position: sticky; top: 0; }
@media (prefers-color-scheme: dark) { th { background: #22262e; } }
tr:hover { background: #f5f8ff; }
@media (prefers-color-scheme: dark) { tr:hover { background: #232838; } }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.badge.re { background: #dbeafe; color: #1e40af; }
.badge.fo { background: #fce7f3; color: #9d174d; }
.badge.other { background: #f1f5f9; color: #64748b; }
@media (prefers-color-scheme: dark) { .badge.re { background: #1e3a5f; color: #93c5fd; } .badge.fo { background: #4a1942; color: #f9a8d4; } .badge.other { background: #2c3038; color: #9ca3af; } }
input[type=text] { padding: 10px; border-radius: 6px; border: 1px solid #ccc; font: inherit; width: 260px; max-width: 100%; }
select { padding: 10px; border-radius: 6px; border: 1px solid #ccc; font: inherit; }
textarea { width: 100%; height: 420px; font: 13px/1.5 Consolas, monospace; padding: 10px; border-radius: 6px; border: 1px solid #ccc; box-sizing: border-box; }
label.key-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px; }
.small { font-size: 12px; color: #888; }
a.link { color: #2563eb; }
.toolbar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 8px; }
@media (max-width: 640px) {
  header h1 { font-size: 16px; }
  main { padding: 12px; }
  .panel { padding: 14px; border-radius: 8px; }
  button { padding: 10px 14px; font-size: 14px; }
  input[type=text] { width: 100%; }
  .toolbar > span.small { order: 5; width: 100%; }
  table { font-size: 12px; }
  th, td { padding: 8px 6px; }
}
</style>
</head>
<body>
<header>
  <h1>Job Scanner</h1>
  <span id="status">idle</span>
  <button class="primary" id="runBtn" onclick="startScan()">Run Scan</button>
</header>
<main>
  <div class="tabs">
    <div class="tab active" data-tab="results" onclick="showTab('results')">Results</div>
    <div class="tab" data-tab="progress" onclick="showTab('progress')">Progress Log</div>
    <div class="tab" data-tab="settings" onclick="showTab('settings')">Settings</div>
  </div>

  <div class="panel active" id="panel-results">
    <div class="toolbar">
      <input type="text" id="filterBox" placeholder="Filter by company, title, location..." oninput="renderTable()">
      <select id="categoryFilter" onchange="renderTable()">
        <option value="">All categories</option>
        <option>Real Estate</option>
        <option>Family Office</option>
        <option>Other</option>
      </select>
      <select id="regionFilter" onchange="renderTable()">
        <option value="">All regions</option>
        <option>US</option>
        <option>Canada</option>
        <option>Unknown</option>
      </select>
      <span class="small" id="rowCount"></span>
      <span style="flex:1"></span>
      <button onclick="window.location='/api/results/download'">Download CSV</button>
    </div>
    <div style="overflow:auto; max-height: 65vh;">
    <table id="resultsTable">
      <thead><tr>
        <th onclick="sortBy('company')">Company</th>
        <th onclick="sortBy('title')">Title</th>
        <th onclick="sortBy('category')">Category</th>
        <th onclick="sortBy('subcategory')">Subcategory</th>
        <th onclick="sortBy('posted_date')">Posted</th>
        <th onclick="sortBy('days_since_posted')">Days</th>
        <th onclick="sortBy('region')">Region</th>
        <th onclick="sortBy('location')">Location</th>
        <th onclick="sortBy('source')">Source</th>
        <th>Link</th>
        <th onclick="sortBy('poc_name')">POC Name</th>
        <th onclick="sortBy('poc_designation')">POC Title</th>
        <th>POC Profile</th>
        <th onclick="sortBy('poc_source')">POC Source</th>
      </tr></thead>
      <tbody id="resultsBody"></tbody>
    </table>
    </div>
  </div>

  <div class="panel" id="panel-progress">
    <div class="small">Live log from the running or most recent scan.</div>
    <div id="logbox"></div>
  </div>

  <div class="panel" id="panel-settings">
    <h3>API Keys</h3>
    <div class="small" style="margin-bottom:10px">Keys are stored only in your local .env file and never displayed once saved.</div>
    <div id="envKeys"></div>
    <h3 style="margin-top:24px">config.yaml <span class="small">— sources, titles, filters</span></h3>
    <textarea id="configText"></textarea>
    <button class="primary" style="margin-top:8px" onclick="saveConfig()">Save config.yaml</button>
    <span id="configMsg" class="small"></span>
    <h3 style="margin-top:24px">classifier_rules.yaml <span class="small">— category/subcategory keyword rules</span></h3>
    <textarea id="rulesText"></textarea>
    <button class="primary" style="margin-top:8px" onclick="saveRules()">Save classifier_rules.yaml</button>
    <span id="rulesMsg" class="small"></span>
  </div>
</main>

<script>
let rows = [];
let sortCol = 'days_since_posted', sortAsc = true;
let logCursor = 0;
let polling = null;

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'settings') loadSettings();
}

function categoryBadge(cat) {
  const cls = cat === 'Real Estate' ? 're' : cat === 'Family Office' ? 'fo' : 'other';
  return `<span class="badge ${cls}">${cat || 'Other'}</span>`;
}

function renderTable() {
  const q = document.getElementById('filterBox').value.toLowerCase();
  const catFilter = document.getElementById('categoryFilter').value;
  const regionFilter = document.getElementById('regionFilter').value;
  let filtered = rows.filter(r =>
    (!catFilter || r.category === catFilter) &&
    (!regionFilter || (r.region || 'Unknown') === regionFilter) &&
    (!q || Object.values(r).some(v => (v || '').toLowerCase().includes(q)))
  );
  filtered.sort((a, b) => {
    let av = a[sortCol] || '', bv = b[sortCol] || '';
    if (sortCol === 'days_since_posted') { av = +av || 0; bv = +bv || 0; return sortAsc ? av - bv : bv - av; }
    return sortAsc ? String(av).localeCompare(bv) : String(bv).localeCompare(av);
  });
  document.getElementById('rowCount').textContent = filtered.length + ' of ' + rows.length + ' postings';
  document.getElementById('resultsBody').innerHTML = filtered.map(r => `
    <tr>
      <td>${esc(r.company)}</td>
      <td>${esc(r.title)}</td>
      <td>${categoryBadge(r.category)}</td>
      <td>${esc(r.subcategory)}</td>
      <td>${esc(r.posted_date || r.first_seen)}</td>
      <td>${esc(r.days_since_posted)}</td>
      <td>${esc(r.region)}</td>
      <td>${esc(r.location)}</td>
      <td>${esc(r.source)}</td>
      <td>${r.url ? `<a class="link" href="${esc(r.url)}" target="_blank">open</a>` : ''}</td>
      <td>${esc(r.poc_name)}</td>
      <td>${esc(r.poc_designation)}</td>
      <td>${r.poc_profile ? `<a class="link" href="${esc(r.poc_profile)}" target="_blank">profile</a>` : ''}</td>
      <td class="small">${esc(r.poc_source)}</td>
    </tr>`).join('');
}

function sortBy(col) {
  if (sortCol === col) sortAsc = !sortAsc; else { sortCol = col; sortAsc = true; }
  renderTable();
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function loadResults() {
  const r = await fetch('/api/results');
  const data = await r.json();
  rows = data.rows;
  renderTable();
}

async function startScan() {
  const r = await fetch('/api/scan/start', { method: 'POST' });
  const data = await r.json();
  if (!data.ok) { alert(data.message); return; }
  showTab('progress');
  pollStatus();
}

async function pollStatus() {
  if (polling) clearInterval(polling);
  polling = setInterval(async () => {
    const r = await fetch('/api/scan/status');
    const s = await r.json();
    document.getElementById('runBtn').disabled = s.running;
    document.getElementById('status').textContent = s.running ? 'scan running...' : (s.error ? 'last scan errored' : 'idle');
    const lr = await fetch('/api/scan/log?since=' + logCursor);
    const ld = await lr.json();
    if (ld.lines.length) {
      const box = document.getElementById('logbox');
      box.textContent += ld.lines.join('\\n') + '\\n';
      box.scrollTop = box.scrollHeight;
      logCursor = ld.next;
    }
    if (!s.running) { clearInterval(polling); loadResults(); }
  }, 1500);
}

async function loadSettings() {
  const r = await fetch('/api/settings');
  const data = await r.json();
  document.getElementById('configText').value = data.config_yaml;
  document.getElementById('rulesText').value = data.classifier_rules_yaml;
  const known = ['TINYFISH_API_KEY', 'JSEARCH_API_KEY', 'THEIRSTACK_API_KEY', 'PREDICTLEADS_API_KEY'];
  document.getElementById('envKeys').innerHTML = known.map(k => `
    <label class="key-row">
      <span style="width:180px">${k}</span>
      <input type="text" id="env_${k}" placeholder="${data.env_keys_set[k] ? '(already set — leave blank to keep)' : 'not set'}">
      <button onclick="saveEnvKey('${k}')">Save</button>
    </label>`).join('');
}

async function saveEnvKey(key) {
  const value = document.getElementById('env_' + key).value.trim();
  if (!value) return;
  await fetch('/api/settings/env', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ key, value }) });
  document.getElementById('env_' + key).value = '';
  document.getElementById('env_' + key).placeholder = '(already set — leave blank to keep)';
}

async function saveConfig() {
  const text = document.getElementById('configText').value;
  const r = await fetch('/api/settings/config', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ text }) });
  const d = await r.json();
  document.getElementById('configMsg').textContent = d.ok ? 'Saved.' : ('Error: ' + d.message);
}

async function saveRules() {
  const text = document.getElementById('rulesText').value;
  const r = await fetch('/api/settings/rules', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ text }) });
  const d = await r.json();
  document.getElementById('rulesMsg').textContent = d.ok ? 'Saved.' : ('Error: ' + d.message);
}

loadResults();

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(() => {}));
}
</script>
</body></html>
"""


if __name__ == "__main__":
    startup_cfg = config_mod.load("config.yaml")
    lan_mode = startup_cfg.web_app_allow_lan and bool(startup_cfg.web_app_password)

    print("\nJob Scanner is running:")
    print(f"  On this PC:  http://127.0.0.1:5000")
    if startup_cfg.web_app_allow_lan and not startup_cfg.web_app_password:
        print("  Phone access is turned on in config.yaml (web_app.allow_lan_access)")
        print("  but no WEB_APP_PASSWORD is set in .env, so — for safety — this run")
        print("  is staying on this PC only. Set WEB_APP_PASSWORD in .env and restart.\n")
    elif lan_mode:
        print(f"  On your phone*:  http://{_lan_ip()}:5000  (password-protected)")
        print("  * phone must be on the same WiFi as this PC. Open that link in your")
        print("    phone's browser, log in, then use the browser menu to \"Add to Home")
        print("    Screen\" (iPhone/Safari) or the \"Install app\" prompt (Android/Chrome).\n")
    else:
        print("  Phone/LAN access is off (default). To turn it on: set")
        print("  web_app.allow_lan_access: true in config.yaml and WEB_APP_PASSWORD in .env.\n")

    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="0.0.0.0" if lan_mode else "127.0.0.1", port=5000, debug=False, use_reloader=False)
