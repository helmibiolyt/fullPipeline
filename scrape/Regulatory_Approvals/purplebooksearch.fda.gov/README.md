# purplebooksearch.fda.gov — FDA Purple Book

## What it scrapes
The FDA Purple Book (licensed biological products): the latest monthly product
CSV, the master patent list, and the glossary. Parses monthly changes and the
full product database, links patents to reference products, resolves reference
BLA numbers, and unifies everything into enriched CSV output. An optional deep
crawler fetches individual BLA detail pages.

## Source URLs
- https://purplebooksearch.fda.gov/ — Purple Book search site
- https://purplebooksearch.fda.gov/index.cfm?event=downloads — monthly CSV downloads
- https://purplebooksearch.fda.gov/index.cfm?event=patentlist — master patent list
- https://purplebooksearch.fda.gov/assets/js/glossary.js — glossary definitions

## Output
- `purplebook_data/` — `purplebook_raw_monthly.csv`, `patent_list.csv`,
  `glossary.csv` (enriched product output). Intermediate HTML/JS are cleaned up,
  leaving CSV.

## Run
```
pip install -r requirements.txt
python purplebook_downloader.py
```

## Notes
- Output path is BASE_DIR-relative (`BASE_DIR / "purplebook_data"`); the
  `-o/--output-dir` CLI override is preserved.
- Runs with no required subcommand (`--deep-crawl`, `--force`, etc. optional).
- `mirror: true` — each run re-downloads the latest monthly Purple Book CSV.
- `tqdm` is an optional progress bar (NON-COMMON dependency).
