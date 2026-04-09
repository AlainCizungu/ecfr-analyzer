# eCFR Analyzer

A full-stack web application that downloads, stores, and visualizes federal regulation data from the [Electronic Code of Federal Regulations (eCFR) public API](https://www.ecfr.gov/developers/documentation/api/v1).

**Live demo:** https://ecfr-analyzer-pxov.onrender.com

---

## Features

- **Live eCFR data** — pulls directly from the eCFR versioner API across all ~50 CFR titles
- **Agency word-count metrics** — tracks regulatory burden by counting words per agency
- **Historical trend charts** — compares word counts across up to 3 time snapshots
- **Smart incremental refresh** — SHA-256 checksums skip re-downloading unchanged titles
- **Large-title protection** — titles exceeding 5 million words (e.g. EPA Title 40, IRS Title 26) skip historical fetching to stay within memory limits
- **Quick / Full Refresh modes** — Quick (~15–30 min) fetches current data only; Full (~90 min) also fetches historical snapshots for trend charts
- **Persistent storage** — SQLite database on a mounted Render disk survives deploys

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | FastAPI + Uvicorn |
| Database | SQLite (WAL mode for concurrent reads) |
| Data pipeline | Python `requests`, `concurrent.futures`, `hashlib` |
| Frontend | Vanilla JS + Chart.js (no build step) |
| Deployment | Docker on Render.com with persistent disk |

---

## Architecture

```
eCFR Public API
      │
      ▼
downloader.py          — parallel title fetcher, checksum diffing, SQLite writes
      │
      ▼
ecfr.db (SQLite)       — agencies, snapshots, title_versions, metadata tables
      │
      ▼
api.py (FastAPI)       — REST endpoints + subprocess.Popen for background refresh
      │
      ▼
frontend/index.html    — single-page dashboard, auto-polling every 15 s
```

---

## Running Locally

```bash
# 1. Clone and create virtualenv
git clone https://github.com/AlainCizungu/ecfr-analyzer.git
cd ecfr-analyzer
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Seed with sample data (fast, no API calls)
python seed_data.py

# 3. Start the API server
uvicorn api:app --reload

# 4. Open http://localhost:8000
# Click "Quick Refresh" to pull live eCFR data (~15–30 min first run)
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | DB health check — agency count, snapshot count, last updated |
| GET | `/api/agencies` | All agencies with current word counts (cached 60 s) |
| GET | `/api/agencies/{id}` | Single agency detail + metrics |
| GET | `/api/agencies/{id}/history` | Word-count snapshots for trend chart |
| POST | `/api/refresh` | Trigger background data download |
| POST | `/api/refresh?history=true` | Trigger full download including historical snapshots |
| GET | `/api/refresh/status` | Check if a download is currently running |

---

## Deployment (Render.com)

The app is containerized with Docker and deployed on Render with a 1 GB persistent disk mounted at `/data/ecfr.db`.

Key deployment decisions:
- **`--workers 1`** in uvicorn — globals (`_refresh_process`, cache dict) are not shared across worker processes; a single worker keeps state consistent
- **Subprocess.Popen** for the downloader — fire-and-forget background process that survives HTTP request timeouts
- **`DB_PATH` env var** — lets the same Docker image use `/data/ecfr.db` on Render and a local path in development

---

## Recent Fixes

### Memory — OOM crashes on 512 MB Render container
The original word-count pipeline (`xml_to_text()` → `text.split()`) allocated ~950 MB for large titles:
- `~100 MB` — full XML bytes in memory
- `~100 MB` — extracted text string
- `~850 MB` — Python list from `split()` (16.9 M string objects for EPA Title 40)

**Fix:** replaced with `count_words_in_xml()`, a single-pass byte iterator that counts whitespace-delimited tokens with O(1) extra memory — no intermediate string or list built.

### Routing — `/api/status` returning 223 KB instead of 281 bytes
Running `--workers 2` meant each uvicorn process had its own `_cache` dict. One worker would populate the cache from `all_agencies_summary()` and accidentally serve it via `/api/status`; the other worker's `_refresh_process` was always `None` so refresh status was always wrong.

**Fix:** `--workers 1` — single process, single source of truth for all globals.

### Downloader — 504 timeouts on large historical XML fetches
The original fetcher called `raise_for_status()` immediately, crashing the whole run on any 5xx.

**Fix:** explicit status-code checks with one retry and 8 s backoff before skipping; the run continues even if individual dates fail.
