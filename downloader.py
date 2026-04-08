"""
downloader.py — Parallel eCFR data fetcher with smart historical skipping.

Improvements over v1:
  • ThreadPoolExecutor  : downloads multiple titles simultaneously (~3-4x faster)
  • Checksum comparison : skips re-fetching history if title text hasn't changed
  • Large-title guard   : skips historical for titles > LARGE_TITLE_WORDS (e.g. EPA)
  • 504 resilience      : retries once then skips, never crashes the whole run

Usage:
  python downloader.py                          # all agencies/titles
  python downloader.py --titles 29 40 17        # subset for testing
  python downloader.py --titles 29 --no-history # current snapshot only, fastest
"""
import argparse
import concurrent.futures
import hashlib
import json
import logging
import re
import sys
import threading
import time
from datetime import date, timedelta, datetime
from xml.etree import ElementTree as ET

import requests

from database import (get_conn, init_db, set_meta,
                      get_title_checksum, upsert_title_snapshot)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

BASE    = "https://www.ecfr.gov"
TIMEOUT = 120
HEADERS = {"User-Agent": "eCFR-Analyzer/1.0 (educational project)"}

LARGE_TITLE_WORDS = 5_000_000  # skip historical if current snapshot exceeds this
MAX_WORKERS       = 2          # parallel title downloads — kept low to stay under 512 MB RAM

_db_lock = threading.Lock()    # serialises all SQLite writes across threads

# ─── API helpers ──────────────────────────────────────────────────────────────

def _get(url: str, **kwargs) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def fetch_agencies() -> list[dict]:
    return _get(f"{BASE}/api/admin/v1/agencies.json").json().get("agencies", [])


def fetch_title_metadata() -> dict[int, dict]:
    titles = _get(f"{BASE}/api/versioner/v1/titles.json").json().get("titles", [])
    return {int(t["number"]): t for t in titles}


def fetch_version_dates(title_num: int) -> list[str]:
    try:
        data = _get(f"{BASE}/api/versioner/v1/versions/title-{title_num}.json").json()
        return [v["date"] for v in data.get("content_versions", [])]
    except Exception as exc:
        log.warning("Could not fetch versions for title %s: %s", title_num, exc)
        return []


def fetch_title_xml(title_num: int, iso_date: str) -> bytes | None:
    """Fetch raw XML. Returns None on 404, 5xx, or network error — never raises."""
    url = f"{BASE}/api/versioner/v1/full/{iso_date}/title-{title_num}.xml"
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            if r.status_code == 404:
                log.warning("  [title-%s] %s not found (404) — skipping", title_num, iso_date)
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 0:
                    log.warning("  [title-%s] HTTP %s @ %s — retrying in 8s…",
                                title_num, r.status_code, iso_date)
                    time.sleep(8)
                    continue
                log.warning("  [title-%s] HTTP %s @ %s — skipping after retry",
                            title_num, r.status_code, iso_date)
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            if attempt == 0:
                log.warning("  [title-%s] Error @ %s: %s — retrying…", title_num, iso_date, exc)
                time.sleep(8)
                continue
            log.warning("  [title-%s] Error @ %s: %s — skipping", title_num, iso_date, exc)
            return None
    return None

# ─── Text / metric helpers ────────────────────────────────────────────────────

def xml_to_text(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
        parts = [
            s.strip() for elem in root.iter()
            for s in (elem.text or "", elem.tail or "")
            if s.strip()
        ]
        return " ".join(parts)
    except ET.ParseError:
        return re.sub(r"<[^>]+>", " ", xml_bytes.decode("utf-8", errors="ignore"))


def word_count(text: str) -> int:
    return len(text.split())


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# ─── Date helpers ─────────────────────────────────────────────────────────────

def closest_date(available: list[str], target: str) -> str | None:
    if not available:
        return None
    def diff(d: str) -> int:
        try:
            return abs((datetime.fromisoformat(d) - datetime.fromisoformat(target)).days)
        except ValueError:
            return 999_999
    return min(available, key=diff)

# ─── Thread-safe DB writers ───────────────────────────────────────────────────

def _write_agency(agency: dict) -> None:
    refs = json.dumps([
        {"title": r.get("title"), "chapter": r.get("chapter")}
        for r in agency.get("cfr_references", [])
    ])
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO agencies (id, name, short_name, cfr_refs) VALUES (?,?,?,?)",
        (agency["slug"], agency["name"], agency.get("short_name", ""), refs),
    )
    conn.commit()
    conn.close()


def _write_snapshot(agency_id: str, snap_date: str,
                    wc: int, checksum: str, title_nums: list[int]) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO snapshots (agency_id, date, word_count, checksum, title_nums)
           VALUES (?,?,?,?,?)
           ON CONFLICT(agency_id, date) DO UPDATE SET
               word_count=excluded.word_count,
               checksum=excluded.checksum,
               title_nums=excluded.title_nums""",
        (agency_id, snap_date, wc, checksum, json.dumps(title_nums)),
    )
    conn.commit()
    conn.close()


def _write_versions(title_num: int, dates: list[str]) -> None:
    conn = get_conn()
    conn.executemany(
        "INSERT OR IGNORE INTO title_versions (title_num, date) VALUES (?,?)",
        [(title_num, d) for d in dates],
    )
    conn.commit()
    conn.close()

# ─── Per-title worker (runs in thread pool) ───────────────────────────────────

def process_title(tnum: int, sample_dates: list[str],
                  version_cache: dict, title_meta: dict) -> dict[str, tuple[str, int]]:
    """
    Fetch snapshots for one title. Returns {date: (checksum, word_count)}.

    Text is never kept in memory after word count is computed — titles like
    EPA (16.9 M words) and IRS (10.2 M words) would otherwise consume hundreds
    of MB each and crash the 512 MB Render container.

    Logic:
      1. Always fetch the current snapshot.
      2. Compare checksum with stored value — if unchanged, skip historical.
      3. If current snapshot > LARGE_TITLE_WORDS, skip historical (server can't serve it).
      4. Otherwise fetch historical dates.
    """
    results: dict[str, tuple[str, int]] = {}   # {date: (checksum, word_count)}
    available = sorted(version_cache.get(tnum, []))
    fallback  = title_meta.get(tnum, {}).get("latest_issue_date", date.today().isoformat())

    # ── Step 1: current snapshot ──────────────────────────────────────────────
    current_target = closest_date(available, sample_dates[0]) or fallback

    # Read stored checksum BEFORE downloading (to detect change later)
    with _db_lock:
        stored_checksum = get_title_checksum(tnum, current_target)

    log.info("[title-%s] Fetching current @ %s…", tnum, current_target)
    xml = fetch_title_xml(tnum, current_target)
    if xml is None:
        return results

    chk = sha256_hex(xml)
    wc  = word_count(xml_to_text(xml))
    del xml                          # free raw bytes immediately — large titles use 100 MB+
    results[current_target] = (chk, wc)

    with _db_lock:
        upsert_title_snapshot(tnum, current_target, wc, chk)

    log.info("[title-%s] %s words, checksum %s…", tnum, f"{wc:,}", chk[:12])

    # ── Step 2: decide whether to fetch historical ────────────────────────────
    if len(sample_dates) <= 1:
        log.info("[title-%s] --no-history mode — done", tnum)
        return results

    if wc > LARGE_TITLE_WORDS:
        log.info("[title-%s] %s words > %s limit — skipping historical snapshots",
                 tnum, f"{wc:,}", f"{LARGE_TITLE_WORDS:,}")
        return results

    if stored_checksum == chk:
        log.info("[title-%s] Unchanged since last run (checksum match) — skipping historical", tnum)
        return results

    # ── Step 3: fetch historical snapshots ────────────────────────────────────
    log.info("[title-%s] Changed or first run — fetching %d historical snapshots",
             tnum, len(sample_dates) - 1)

    for sample in sample_dates[1:]:
        target = closest_date(available, sample) or fallback
        if target in results:
            continue  # same version as current — no need to re-fetch

        log.info("[title-%s] Historical @ %s…", tnum, target)
        xml_h = fetch_title_xml(tnum, target)
        if xml_h is None:
            continue

        chk_h = sha256_hex(xml_h)
        wc_h  = word_count(xml_to_text(xml_h))
        del xml_h                    # free raw bytes immediately
        results[target] = (chk_h, wc_h)

        with _db_lock:
            upsert_title_snapshot(tnum, target, wc_h, chk_h)

        log.info("[title-%s] Historical %s: %s words", tnum, target, f"{wc_h:,}")

    return results

# ─── Main orchestration ───────────────────────────────────────────────────────

def download(title_filter: set[int] | None = None, history: bool = True) -> None:
    init_db()
    today = date.today().isoformat()

    log.info("Fetching agency list…")
    agencies = fetch_agencies()
    log.info("Found %d agencies", len(agencies))

    log.info("Fetching title metadata…")
    title_meta = fetch_title_metadata()

    all_titles: set[int] = set()
    for ag in agencies:
        for ref in ag.get("cfr_references", []):
            if ref.get("title"):
                t = int(ref["title"])
                if title_filter is None or t in title_filter:
                    all_titles.add(t)
    log.info("Titles to process: %s", sorted(all_titles))

    sample_dates = [today]
    if history:
        sample_dates += [
            (date.today() - timedelta(days=182)).isoformat(),
            (date.today() - timedelta(days=365)).isoformat(),
        ]

    # Fetch all version histories (fast, metadata only)
    log.info("Fetching version histories…")
    version_cache: dict[int, list[str]] = {}
    for tnum in sorted(all_titles):
        dates_list = fetch_version_dates(tnum)
        version_cache[tnum] = dates_list
        with _db_lock:
            _write_versions(tnum, dates_list)
        log.info("  title-%-3s: %d versions", tnum, len(dates_list))

    # ── Parallel title downloads ───────────────────────────────────────────────
    log.info("Downloading title text (%d workers)…", MAX_WORKERS)
    title_text: dict[int, dict[str, tuple[str, int]]] = {}   # {tnum: {date: (checksum, wc)}}

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(process_title, tnum, sample_dates, version_cache, title_meta): tnum
            for tnum in sorted(all_titles)
        }
        for future in concurrent.futures.as_completed(futures):
            tnum = futures[future]
            try:
                title_text[tnum] = future.result()
                log.info("✓ title-%s complete (%d snapshots)", tnum, len(title_text[tnum]))
            except Exception as exc:
                log.error("✗ title-%s failed: %s", tnum, exc)
                title_text[tnum] = {}

    # ── Store per-agency snapshots (sequential — fast, just DB writes) ─────────
    log.info("Storing agency snapshots…")
    for ag in agencies:
        with _db_lock:
            _write_agency(ag)

        agency_id  = ag["slug"]
        title_nums = [
            int(r["title"])
            for r in ag.get("cfr_references", [])
            if r.get("title") and (title_filter is None or int(r["title"]) in title_filter)
        ]
        if not title_nums:
            continue

        for sample in sample_dates:
            total_words   = 0
            combined_hash = hashlib.sha256()
            covered: list[int] = []

            for tnum in title_nums:
                cache   = title_text.get(tnum, {})
                nearest = closest_date(list(cache.keys()), sample)
                if nearest and abs(
                    (datetime.fromisoformat(nearest) - datetime.fromisoformat(sample)).days
                ) <= 60:
                    chk, wc = cache[nearest]
                    total_words += wc
                    combined_hash.update(chk.encode())
                    covered.append(tnum)

            if covered:
                with _db_lock:
                    _write_snapshot(agency_id, sample,
                                    total_words, combined_hash.hexdigest(), covered)

    set_meta("last_updated", today)
    log.info("✓ All done. Last updated: %s", today)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download eCFR data into SQLite")
    parser.add_argument("--titles", nargs="*", type=int,
                        help="Limit to specific title numbers (e.g. --titles 29 40 17)")
    parser.add_argument("--no-history", action="store_true",
                        help="Fetch today's snapshot only — much faster, skips trend data")
    args = parser.parse_args()
    download(
        title_filter=set(args.titles) if args.titles else None,
        history=not args.no_history,
    )
