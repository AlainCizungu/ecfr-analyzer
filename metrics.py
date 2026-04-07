"""
metrics.py — Computes and returns per-agency metrics from the SQLite store.

Standard metrics
  • word_count      — total words in current regulatory text
  • checksum        — SHA-256 of raw XML for the current snapshot

Custom metric: Amendment Velocity Index (AVI)
  Definition: (distinct amendment dates in last 365 days across covered titles)
              ÷ (current word count / 1 000)
  Interpretation: amendments per 1 000 words of regulation per year.
  Higher → text is revised frequently relative to its size, a signal that
  rules are contested or volatile — a useful filter for deregulation review.

Custom metric 2: Word-Count Growth Rate (%)
  Definition: ((current_words − year_ago_words) / year_ago_words) × 100
  Interpretation: how much the agency's regulatory footprint has expanded (or
  contracted) over the past year. Positive = growing, negative = shrinking.
"""
import json
from datetime import date, timedelta

from database import get_conn


# ─── Amendment Velocity Index ─────────────────────────────────────────────────

def amendment_velocity(agency_id: str) -> float:
    """Return AVI (float, rounded to 4 dp) for the given agency."""
    conn = get_conn()
    snap = conn.execute(
        "SELECT word_count, title_nums FROM snapshots "
        "WHERE agency_id=? ORDER BY date DESC LIMIT 1",
        (agency_id,),
    ).fetchone()

    if not snap or not snap["word_count"]:
        conn.close()
        return 0.0

    title_nums: list[int] = json.loads(snap["title_nums"] or "[]")
    if not title_nums:
        conn.close()
        return 0.0

    one_year_ago = (date.today() - timedelta(days=365)).isoformat()
    placeholders = ",".join("?" * len(title_nums))
    row = conn.execute(
        f"SELECT COUNT(DISTINCT date) AS cnt FROM title_versions "
        f"WHERE title_num IN ({placeholders}) AND date >= ?",
        title_nums + [one_year_ago],
    ).fetchone()
    conn.close()

    amendment_count = row["cnt"] if row else 0
    words_per_thousand = snap["word_count"] / 1_000
    return round(amendment_count / words_per_thousand, 4) if words_per_thousand else 0.0


# ─── Word-Count Growth Rate ───────────────────────────────────────────────────

def word_count_growth(agency_id: str) -> float | None:
    """Return % change in word count from oldest to newest snapshot, or None."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, word_count FROM snapshots "
        "WHERE agency_id=? AND word_count > 0 ORDER BY date ASC",
        (agency_id,),
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        return None
    oldest_wc = rows[0]["word_count"]
    newest_wc = rows[-1]["word_count"]
    if oldest_wc == 0:
        return None
    return round((newest_wc - oldest_wc) / oldest_wc * 100, 2)


# ─── Public API ───────────────────────────────────────────────────────────────

def agency_metrics(agency_id: str) -> dict | None:
    """Return the full metric bundle for one agency, or None if no data."""
    conn = get_conn()
    snap = conn.execute(
        "SELECT date, word_count, checksum, title_nums FROM snapshots "
        "WHERE agency_id=? ORDER BY date DESC LIMIT 1",
        (agency_id,),
    ).fetchone()
    conn.close()

    if not snap:
        return None

    return {
        "snapshot_date": snap["date"],
        "word_count": snap["word_count"],
        "checksum": snap["checksum"],
        "title_nums": json.loads(snap["title_nums"] or "[]"),
        "amendment_velocity": amendment_velocity(agency_id),
        "word_count_growth_pct": word_count_growth(agency_id),
    }


def agency_history(agency_id: str) -> list[dict]:
    """Return time-series snapshots for an agency, oldest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, word_count, checksum FROM snapshots "
        "WHERE agency_id=? ORDER BY date ASC",
        (agency_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_agencies_summary() -> list[dict]:
    """
    Return a summary list (one row per agency) sorted by word count desc.
    Only agencies with at least one snapshot are included.
    """
    conn = get_conn()
    agencies = conn.execute(
        "SELECT id, name, short_name FROM agencies ORDER BY name"
    ).fetchall()
    conn.close()

    result = []
    for ag in agencies:
        m = agency_metrics(ag["id"])
        if m and m["word_count"]:
            result.append({
                "id": ag["id"],
                "name": ag["name"],
                "short_name": ag["short_name"] or ag["name"],
                **m,
            })

    return sorted(result, key=lambda x: x["word_count"], reverse=True)
