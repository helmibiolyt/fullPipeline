# chictr.org.cn

Chinese Clinical Trial Registry (ChiCTR).

## Files in this folder
Three related scripts travel together; the pipeline entrypoint is
`chictr_downloader.py`:

- **`chictr_downloader.py`** — ENTRYPOINT (declared in `manifest.yaml`). Scrapes the
  ChiCTR English search results into a list CSV. Implements "virtual page sizing"
  (merges several 10-item server pages into one logical page) and drives a
  Playwright browser to bypass the site WAF. With `--download-xml` it also visits
  each detail page and saves the trial XML into `chictr_trails2/xml_details/`.
- **`scrape_chictr_ids.py`** — HELPER. A lighter, standalone Playwright scraper that
  walks the search pages and writes a minimal `chictr_ids.csv` (registration number,
  title, type, date, detail URL). Useful for quickly harvesting IDs / detail URLs
  without the virtual-paging machinery.
- **`chictr_downloader_details.py`** — HELPER. A consolidation/enrichment pass: it
  merges the basic list CSV(s), downloads each trial's detail XML in-memory,
  parses the full field set with ElementTree, and appends flattened rows to a
  detailed CSV (deleting XMLs as it goes). It provides the deep per-trial fields
  the entrypoint's list scrape does not.

Typical flow: run the entrypoint to build the trial list, then optionally run
`chictr_downloader_details.py` to expand each listed trial into a detailed record.

## Source URLs
- https://www.chictr.org.cn/searchprojEN.html — English search results
- https://www.chictr.org.cn/ ... DownloadXml ... — per-trial XML export (from each detail page)

## Output
- `chictr_trails2/chictr_list.csv` — one row per trial (registration number, title, sponsor, type, date, detail URL).
- `chictr_trails2/xml_details/*.xml` — per-trial detail XML, only when run with `--download-xml`.

## Run
```
pip install -r requirements.txt
python -m playwright install chromium
python chictr_downloader.py                     # scrape list into chictr_list.csv
python chictr_downloader.py --download-xml       # also save per-trial detail XML
python chictr_downloader.py --visible            # headful, to solve WAF challenges
```

## Notes
- The entrypoint writes only inside this folder (`BASE_DIR/chictr_trails2/`, plus the
  nested `xml_details/`). The `chictr_trails2` leaf name (a pre-existing typo of
  "trials") is preserved.
- Full snapshot each run: resumes from an existing CSV but rebuilds the full list
  (mirror: true).
- Needs Playwright + Chromium for the WAF-guarded HTML pages; XML file downloads use
  plain `requests`.
- ⚠️ Helper `chictr_downloader_details.py` still contains HARDCODED absolute input
  paths (`c:\Users\LeMonde\Desktop\Biolyt_Intern\Test\...`) for the list CSVs it
  consolidates. It is not the pipeline entrypoint, so it was left as-is, but those
  paths must be pointed at this folder before the helper will run here.
