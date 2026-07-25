# ema.europa.eu — European Medicines Agency (EMA) EPARs

## What it scrapes
EMA centralized approvals (EPARs) and regulatory data. Downloads the master
medicines Excel report, dynamically builds a SQLite schema, crawls each
medicine's EPAR page for PDF links, streams PDFs in memory, extracts text with
`pypdf`, and uses the MiniMax-M3 LLM to extract structured clinical/regulatory
fields plus per-trial clinical-registry details. Exports document-level,
trial-level and drug-level CSV reports. Can optionally download 10 additional
EMA report tables and stream to AWS S3.

## Entrypoint & helper
- `ema_downloader.py` — **the manifest entrypoint**. Runs the full pipeline:
  master Excel → EPAR crawl → in-memory PDF text extraction → LLM extraction →
  CSV exports, writing checkpoints to `ema_data/ema_database.db`.
- `extract_ema_pdfs.py` — **helper that travels with the entrypoint**. A
  standalone recovery/backfill tool that reprocesses PDFs already staged in AWS
  S3 against the SAME `ema_data/ema_database.db` checkpoint DB and re-exports the
  compiled CSV. It is not imported by `ema_downloader.py`; it is run manually to
  recover or re-extract from S3-stored PDFs and shares the entrypoint's SQLite
  database and `ema_data/` output. Its `--db-path` / `--export-path` defaults are
  BASE_DIR-relative to match the entrypoint.

## Source URLs
- https://www.ema.europa.eu/en/medicines/download-medicine-data — medicines data downloads
- https://www.ema.europa.eu/en/documents/report/medicines-output-medicines-report_en.xlsx — master medicines Excel
- https://www.ema.europa.eu/ — EPAR medicine pages (PDF links)

## Output
- `ema_data/` — `ema_medicines.csv`, `ema_documents.csv`,
  `ema_pdf_extractions.csv`, `ema_clinical_trials.csv`, `ema_drug_summary.csv`,
  plus `ema_database.db` (checkpoint).
- `ema_data/pdfs/` — raw PDFs, only when run with `--save-pdfs`.
- `ema_data/additional_tables/` — extra report CSVs, only with `--download-all-tables`.

## Run
```
pip install -r requirements.txt
python ema_downloader.py
```
Optional S3 backfill/recovery: `python extract_ema_pdfs.py` (requires AWS creds).

## Notes
- Output paths are BASE_DIR-relative (`BASE_DIR / "ema_data"`, PDFs under
  `ema_data/pdfs`, extra tables under `ema_data/additional_tables`); the
  `--output-dir` CLI override is preserved.
- ⚠️ LLM key handling: unlike the other LLM scrapers, the MiniMax key here is NOT
  read from the environment by default. `ema_downloader.py` uses
  `--minimax-api-key` defaulting to a hardcoded `DEFAULT_MINIMAX_API_KEY`
  constant, and `extract_ema_pdfs.py` falls back to the same hardcoded constant
  (it does read `MINIMAX_API_KEY` from env before that fallback). The hardcoded
  key was left in place to avoid changing scraping logic — the maintainer should
  rotate/remove it and switch to an env var.
- `size_class: heavy` — crawls all EMA medicines and streams a very large PDF
  corpus with per-document LLM calls; timeout raised to 720 min.
- `mirror: true` — each run performs a full crawl (resumable via the SQLite DB).
- NON-COMMON deps: `pypdf`, `openai`, `boto3`, `tqdm` (`boto3` only needed for
  the S3 modes / the helper; `tqdm` optional).
