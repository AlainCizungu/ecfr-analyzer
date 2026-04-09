"""
api.py — FastAPI application exposing eCFR analysis endpoints.

Start with:  uvicorn api:app --reload
             uvicorn api:app --host 0.0.0.0 --port 8000
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from database import get_conn, get_meta
from metrics import agency_metrics, agency_history, all_agencies_summary

app = FastAPI(
    title="eCFR Analyzer",
    description="Analyze Federal Regulations from the Electronic Code of Federal Regulations.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ─── Simple in-memory cache ───────────────────────────────────────────────────
# Avoids recomputing all_agencies_summary() (which walks every agency) on every
# request. Cache is busted automatically whenever the downloader finishes and
# writes a new last_updated value.

_cache: dict = {}          # {"data": [...], "ts": float, "last_updated": str}
CACHE_TTL = 60             # seconds — also acts as max staleness during a refresh


def _get_cached_agencies() -> list:
    now = time.time()
    last_updated = get_meta("last_updated") or ""

    # Return cached copy if fresh and data hasn't changed
    if (
        _cache
        and now - _cache["ts"] < CACHE_TTL
        and _cache["last_updated"] == last_updated
    ):
        return _cache["data"]

    data = all_agencies_summary()
    _cache.update({"data": data, "ts": now, "last_updated": last_updated})
    return data


def _bust_cache() -> None:
    _cache.clear()


# ─── UI ───────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# ─── Status ──────────────────────────────────────────────────────────────────

@app.get("/api/status", summary="Database status")
def status():
    conn = get_conn()
    agency_count   = conn.execute("SELECT COUNT(*) FROM agencies").fetchone()[0]
    snapshot_count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    conn.close()
    return {
        "last_updated":   get_meta("last_updated"),
        "agency_count":   agency_count,
        "snapshot_count": snapshot_count,
        "data_ready":     snapshot_count > 0,
    }


# ─── Agencies ─────────────────────────────────────────────────────────────────

@app.get("/api/agencies", summary="All agencies with current metrics")
def list_agencies():
    """
    Returns every agency with at least one snapshot, sorted by word count.
    Cached for 60 s and busted when last_updated changes (i.e. after a refresh).
    """
    return _get_cached_agencies()


@app.get("/api/agencies/{agency_id}", summary="Single agency detail")
def get_agency(agency_id: str):
    conn = get_conn()
    ag = conn.execute(
        "SELECT id, name, short_name, cfr_refs FROM agencies WHERE id=?",
        (agency_id,),
    ).fetchone()
    conn.close()

    if not ag:
        raise HTTPException(status_code=404, detail="Agency not found")

    m = agency_metrics(agency_id)
    if not m:
        raise HTTPException(status_code=404, detail="No data yet — run the downloader first")

    return {**dict(ag), **m}


@app.get("/api/agencies/{agency_id}/history", summary="Word-count history for an agency")
def get_history(agency_id: str):
    """Returns snapshots ordered oldest → newest for charting trends."""
    conn = get_conn()
    exists = conn.execute("SELECT 1 FROM agencies WHERE id=?", (agency_id,)).fetchone()
    conn.close()
    if not exists:
        raise HTTPException(status_code=404, detail="Agency not found")
    return agency_history(agency_id)


# ─── Refresh ──────────────────────────────────────────────────────────────────

# Absolute path to downloader.py — works correctly inside Docker on Render
DOWNLOADER = str(Path(__file__).parent / "downloader.py")
_refresh_process: subprocess.Popen | None = None


def _is_running() -> bool:
    return _refresh_process is not None and _refresh_process.poll() is None


@app.post("/api/refresh", summary="Trigger a background data refresh")
def refresh(history: bool = False):
    """
    Spawns downloader.py as a detached subprocess (Popen, not run) so it
    keeps running independently regardless of request timeouts. The frontend
    polls /api/status every 3 s and auto-reloads when last_updated changes.

    history=False (default): ~15–30 min — fetches current snapshot only.
    history=True           : ~90–120 min — also fetches historical snapshots
                             for trend charts (only needed once; checksums skip
                             unchanged titles on subsequent runs).
    """
    global _refresh_process
    if _is_running():
        return {"status": "already running"}

    _bust_cache()
    cmd = [sys.executable, DOWNLOADER]
    if not history:
        cmd.append("--no-history")

    _refresh_process = subprocess.Popen(
        cmd,
        env={**os.environ},                   # inherit DB_PATH and all env vars
        cwd=str(Path(__file__).parent),       # run from /app inside Docker
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return {"status": "refresh started", "history": history}


@app.get("/api/refresh/status", summary="Check if a refresh is running")
def refresh_status():
    return {"running": _is_running()}
