# openalex.org

Harvests scholarly metadata from the OpenAlex API, focused on Health
Sciences / Medicine. Reconstructs abstracts from OpenAlex inverted indexes,
parses MeSH terms, authorships, institutions, and funders, and exports to flat
or relational CSVs. Supports cursor-based resume, API-key rotation, and
year-partitioned multithreading.

## What it scrapes
Biomedical scholarly works (title, DOI, journal, authors, institutions,
countries, open-access status, MeSH terms, topics, funders, reconstructed
abstract, citation counts) filtered by OpenAlex domain/field (default domain
`4` = Health Sciences).

## Source URLs
- https://api.openalex.org/works — OpenAlex works endpoint
- https://openalex.org/ — project landing page

## Output
Everything is written into `openalex/`:
- `openalex_works.csv` — one row per work (flat metadata). Default file prefix.
- With `--relational`: `openalex_works_authors.csv`,
  `openalex_works_institutions.csv`, `openalex_works_sources.csv`,
  `openalex_works_affiliations.csv`.
- `openalex_works_progress.json` — cursor/resume checkpoint.
- `openalex_works_downloader.log` — run log.

Output format: **CSV**.

## Run
```
pip install -r requirements.txt
python openalex_downloader.py
```
Common options: `--field-id 27` (Medicine), `--start-year`/`--end-year`,
`--threads N` (year-partitioned), `--relational`, `--limit N`,
`--output-dir <path>` (overrides the default).

## Notes
- Writes only inside this folder (`BASE_DIR/openalex/`); the default output dir
  resolves from `__file__`, not the working directory.
- API keys are read from environment variables `OPENALEX_API_KEY` and/or
  `OPENALEX_API_KEYS` (comma-separated); never hardcoded. Runs in public
  sandbox mode with no keys. A `.env` file is auto-loaded via python-dotenv.
- `mirror: false` — the crawler keeps a cursor checkpoint
  (`*_progress.json`) and de-duplicates against already-written rows, so a
  re-run resumes and appends rather than re-fetching the whole dataset. Do not
  wipe the output between runs unless you intend a full re-crawl.
