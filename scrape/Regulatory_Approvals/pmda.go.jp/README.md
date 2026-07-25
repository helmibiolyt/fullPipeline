# pmda.go.jp — Japan PMDA approvals & review reports

## What it scrapes
Approval and review-report data from Japan's Pharmaceuticals and Medical Devices
Agency (PMDA) English portals across Drugs, Devices, Regenerative products and
Quasi-drugs. Scrapes approval tables, streams English review-report PDFs into
memory, extracts text with `pypdf`, and uses LLMs (MiniMax / Gemini / Groq with
round-robin failover) to extract structured clinical, dosage, indication,
efficacy, safety and trial-identifier fields.

## Source URLs
- https://www.pmda.go.jp/english/review-services/reviews/approved-information/drugs/0001.html
- https://www.pmda.go.jp/english/review-services/reviews/approved-information/devices/0003.html
- https://www.pmda.go.jp/english/review-services/reviews/approved-information/0004.html (regenerative)
- https://www.pmda.go.jp/english/review-services/reviews/approved-information/0005.html (quasi-drug)

## Output
- `pmda_data/pmda_llm_extracted_data.csv` — one row per approval with
  LLM-extracted fields.
- `pmda_data/pdfs/` and `pmda_data/master_lists/` — provisioned output subdirs
  (see Notes).

## Run
```
pip install -r requirements.txt
python pmda_collector.py
```
LLM API keys are read from environment variables (see below).

## Notes
- Output path is BASE_DIR-relative (`BASE_DIR / "pmda_data"`); the `--output-dir`
  CLI override is preserved.
- `.env` loading is intentionally tolerant: it checks the CWD, the script folder,
  and the hardcoded absolute repo path
  `c:/Users/LeMonde/Desktop/Biolyt_Inter/Biolyt_data_collection/.env`. The
  absolute path was left in place so it keeps working; the script-folder and CWD
  fallbacks make it portable.
- LLM keys are read from env vars only — `MINIMAX_API_KEY`
  (optional `MINIMAX_BASE_URL`), `GEMINI_API_KEY`, `GROQ_API_KEY`. No LLM keys
  are hardcoded.
- ⚠️ The current `pmda_collector.py` writes only into `pmda_data/`
  (`pmda_llm_extracted_data.csv` + log); it does NOT currently write to
  `pdfs/` or `master_lists/`. Those subdirs are provisioned (with `.gitkeep`) and
  listed in `output_subdirs` per the migration instruction, but are unused by the
  present code.
- `mirror: true` — each run re-scrapes all portals and reprocesses every record.
- NON-COMMON deps: `pypdf`, `tqdm` (tqdm optional).
