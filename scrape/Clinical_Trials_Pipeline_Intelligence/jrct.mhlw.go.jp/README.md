# jrct.mhlw.go.jp

Japan Registry of Clinical Trials (jRCT).

## What it scrapes
Iterates the jRCT English search-result pages to collect trial IDs, then visits
each detail page and parses its structured HTML tables (handling rowspan/colspan)
into flat key-value records, writing a unified CSV. Includes retries, exponential
backoff, randomized delays, optional TLS impersonation (curl_cffi), and optional
proxy/VPN rotation to survive the site's WAF.

## Source URLs
- https://jrct.mhlw.go.jp/search?language=en&searched=1&... — search result listing
- https://jrct.mhlw.go.jp/en-latest-detail/<TrialID> — trial detail page

## Output
- `jrct_trials/jrct_list.csv` — one row per trial (flattened detail fields).
- `jrct_trials/jrct_ids.txt` — transient discovered-ID cache for resume (deleted on clean completion).

## Run
```
pip install -r requirements.txt
python jrct_downloader.py                     # crawl all pages + details
python jrct_downloader.py --max-pages 5       # limit for testing
```

## Notes
- Writes only inside this folder (`BASE_DIR/jrct_trials/`).
- Incremental / resume-based: only fetches trial IDs not already in `jrct_list.csv`
  and re-writes the CSV from the accumulated record set (mirror: false). The pipeline
  should not wipe the folder between runs.
- `curl_cffi` is imported for TLS impersonation but is optional — the code falls back
  to standard `requests` if it is not installed.
- Deliberate 7–15s per-request delays make this a slow crawl.
