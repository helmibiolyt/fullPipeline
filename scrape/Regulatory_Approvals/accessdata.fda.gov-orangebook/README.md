# accessdata.fda.gov (Orange Book) — FDA Orange Book

## What it scrapes
The FDA Orange Book (Approved Drug Products with Therapeutic Equivalence
Evaluations): products, patents and exclusivities. Downloads the monthly ZIP,
scrapes patent-use and exclusivity code definitions from accessdata.fda.gov,
parses the tilde-delimited data files, joins codes to human-readable
definitions, and generates a unified product/patent/exclusivity dataset.

## Source URLs
- https://www.fda.gov/drugs/drug-approvals-and-databases/orange-book-data-files — monthly ZIP index
- https://www.accessdata.fda.gov/scripts/cder/ob/results_patent.cfm — patent use codes
- https://www.accessdata.fda.gov/scripts/cder/ob/results_exclusivity.cfm — exclusivity codes

## Output
- `orangebook_data/` — `products.csv`, `patents_enriched.csv`,
  `exclusivity_enriched.csv`, and `orange_book_unified.csv`. The ZIP, raw `.txt`,
  code JSON and intermediate JSON are cleaned up, leaving CSV.

## Run
```
pip install -r requirements.txt
python orangebook_downloader.py download
python orangebook_downloader.py parse
```

## Notes
- Output path is BASE_DIR-relative (`BASE_DIR / "orangebook_data"`); the
  `--output-dir` CLI override is preserved.
- Uses required subcommands — bare `python orangebook_downloader.py` exits
  non-zero. Run `download` then `parse` (also `unified`, `search`).
- `mirror: true` — each run re-downloads the latest monthly Orange Book ZIP.
- `tqdm` is an optional progress bar (NON-COMMON dependency).
