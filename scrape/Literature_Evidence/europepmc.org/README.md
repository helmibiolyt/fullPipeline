# europepmc.org

Crawls biomedical literature metadata from Europe PMC and, optionally, distills
clinical/scientific details from open-access full-text JATS XML using LLM APIs.
Supports cursor-based resume, de-duplication, and concurrent full-text
processing.

## What it scrapes
- **Metadata** for articles matching a biomedical search query (id, PMID,
  PMCID, DOI, title, abstract, authors, affiliations, ORCIDs, journal info,
  MeSH-derived fields, open-access flags, citation counts, full-text URLs).
- **Full-text LLM extraction** (opt-in with `--extract-llm`): fetches the
  open-access full-text XML, strips it to clean narrative text, and asks an LLM
  to extract ~27 structured clinical-trial/drug fields per article.

## Source URLs
- https://www.ebi.ac.uk/europepmc/webservices/rest/search — search endpoint
- https://www.ebi.ac.uk/europepmc/webservices/rest/{PMCID}/fullTextXML — full text
- https://europepmc.org/ — project landing page

## Output
Everything is written into `europe_pmc/`:
- `europe_pmc_metadata.csv` — one row per article (metadata).
- `europe_pmc_full_text.csv` — LLM-extracted clinical fields (only with `--extract-llm`).
- `europe_pmc_progress.json` — cursor/resume checkpoint.
- `europe_pmc_merged_clean.csv` — produced by the merge helper (see below).
- `europe_pmc_downloader.log` — run log (written next to the script).

Output format: **CSV**.

## Run
```
pip install -r requirements.txt
python europe_pmc_downloader.py
```
Useful options: `--extract-llm` (enable full-text LLM extraction),
`--open-access`, `--has-pdf`, `--from-year`/`--to-year`, `--limit N`,
`--threads N`, `--fresh`, `--output-dir <path>` (overrides the default).

### Merge helper — `merge_europe_pmc.py`
`merge_europe_pmc.py` is a **separate, manual post-processing step** — it is NOT
called by the downloader. After a crawl with `--extract-llm`, run it to join
`europe_pmc_metadata.csv` with `europe_pmc_full_text.csv` on `id`, normalize the
values (booleans, integers, enrollment, NCT-ID formatting, "None" placeholders
to blanks), print merge statistics, and emit
`europe_pmc_merged_clean.csv`:
```
python merge_europe_pmc.py
```
Its default input/output paths resolve to this folder's `europe_pmc/` dir.

## Notes
- Writes only inside this folder (`BASE_DIR/europe_pmc/`); the default output
  dir resolves from `__file__`, not the working directory.
- LLM extraction (`--extract-llm`) calls Groq, Google Gemini, and/or MiniMax via
  plain HTTPS (no vendor SDKs). Keys are read from environment variables
  `GROQ_API_KEY`, `GEMINI_API_KEY`, `MINIMAX_API_KEY` (and optional
  `MINIMAX_BASE_URL`); never hardcoded. A `.env` file is auto-loaded via
  python-dotenv. Without `--extract-llm` the crawl needs no LLM keys.
- `mirror: false` — the crawler keeps a cursor checkpoint
  (`europe_pmc_progress.json`) and de-duplicates against already-written rows,
  so a re-run resumes and appends rather than re-fetching the whole dataset. Use
  `--fresh` to force a clean crawl. Do not wipe the output between runs unless
  you intend a full re-crawl.
- Full-text + LLM extraction is large and slow (`size_class: heavy`,
  `timeout_min: 360`); metadata-only crawls are much lighter.
