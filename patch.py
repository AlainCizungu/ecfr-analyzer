"""
patch.py — Fixes downloader.py to skip 504/timeout errors instead of crashing.
Run once:  python patch.py
Then run:  python downloader.py --titles 29 40 17
"""

OLD = '''def fetch_title_xml(title_num: int, iso_date: str) -> bytes | None:
    url = f"{BASE}/api/versioner/v1/full/{iso_date}/title-{title_num}.xml"
    try:
        r = _get(url, stream=True)
        return r.content
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise'''

NEW = '''def fetch_title_xml(title_num: int, iso_date: str) -> bytes | None:
    url = f"{BASE}/api/versioner/v1/full/{iso_date}/title-{title_num}.xml"
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            if r.status_code == 404:
                log.warning("    title-%s @ %s not found \u2014 skipping", title_num, iso_date)
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                if attempt == 0:
                    log.warning("    HTTP %s for title-%s @ %s \u2014 retrying in 8s\u2026",
                                r.status_code, title_num, iso_date)
                    import time; time.sleep(8)
                    continue
                log.warning("    HTTP %s for title-%s @ %s \u2014 skipping after retry",
                            r.status_code, title_num, iso_date)
                return None
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            if attempt == 0:
                log.warning("    Error for title-%s @ %s: %s \u2014 retrying\u2026",
                            title_num, iso_date, exc)
                import time; time.sleep(8)
                continue
            log.warning("    Error for title-%s @ %s: %s \u2014 skipping",
                        title_num, iso_date, exc)
            return None
    return None'''

with open("downloader.py", "r") as f:
    content = f.read()

if OLD in content:
    content = content.replace(OLD, NEW)
    with open("downloader.py", "w") as f:
        f.write(content)
    print("✓ downloader.py patched — 504 errors will now be skipped gracefully.")
    print("  Run: python downloader.py --titles 29 40 17")
elif NEW in content:
    print("✓ Already patched — nothing to do.")
    print("  Run: python downloader.py --titles 29 40 17")
else:
    print("✗ Could not find the target function. Check that downloader.py is in this folder.")
