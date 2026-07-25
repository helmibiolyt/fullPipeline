# products.mhra.gov.uk — UK MHRA drug approvals

## What it scrapes
UK Medicines and Healthcare products Regulatory Agency (MHRA) drug documents
(SPC, PIL, PAR). Harvests the full document metadata index directly from MHRA's
backing Azure AI Search service, streams each PDF into memory, extracts text with
`pypdf`, and uses LLMs to extract structured regulatory fields (indication,
posology, active substances, contraindications, adverse effects, storage, MAH,
approval/revision date).

## Source URLs
- https://products.mhra.gov.uk/ — MHRA products search (public front-end)
- https://mhraproducts4853.search.windows.net/indexes/products-index/docs — backing Azure AI Search index (metadata harvest)

## Output
- `mhra_data/mhra_llm_extracted_data.csv` — raw metadata + LLM-extracted fields (one row per document).
- `mhra_data/raw_metadata.csv` — harvested document metadata.
- `mhra_data/pdfs/<doc_type>/` — local PDFs, only when run with `--download-pdfs`.

## Run
```
pip install -r requirements.txt
python mhra_downloader.py
```
LLM API keys are read from environment variables (see below). A `.env` file in
the working directory / script folder is auto-loaded.

## Notes
- Output path is BASE_DIR-relative (`BASE_DIR / "mhra_data"`, PDFs under
  `mhra_data/pdfs`); the `--output-dir` CLI override is preserved.
- LLM keys are read from env vars only — `GROQ_API_KEY`, `GEMINI_API_KEY`,
  `MINIMAX_API_KEY` (optional `MINIMAX_BASE_URL`). No LLM keys are hardcoded. Use
  `--skip-llm` to run metadata/text only.
- The `API_KEY` constant is the MHRA Azure Search *query* key (a public
  read-only search key baked into the site), not an LLM secret.
- `mirror: true` — each run re-harvests the full MHRA index. Note: the extracted
  CSV is written in append mode and the checkpoint is deleted at the end of a run,
  so the folder should be cleared (mirror) between runs to avoid duplicate rows.
- NON-COMMON deps: `pypdf`, `python-dotenv`, `tqdm` (tqdm optional).
