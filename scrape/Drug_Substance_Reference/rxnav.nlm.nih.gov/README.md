# rxnav.nlm.nih.gov — RxNorm (RxNav API)

## What it scrapes
Fetches all RxNorm ingredient concepts (TTY=IN and TTY=MIN) from the NIH RxNav
RxNorm API, then enriches each concept with brand-name synonyms and ingredient
mappings, writing one atomic row per concept/ingredient to CSV.

## Source URLs
- https://rxnav.nlm.nih.gov/REST — RxNorm REST API

## Output
- `rxnorm_data/rxnorm_drugs.csv` — query, rxcui, name, tty, synonym, ingredient_rxcui, ingredient_name.
- `rxnorm_data/drug_names.txt` — intermediate list of concept names (removed on completion).

## Run
```
pip install -r requirements.txt
python rxnorm_scraper.py
```

## Notes
- Writes only inside this folder (`BASE_DIR/rxnorm_data/`).
- `mirror: false` — the scraper is resumable/append-only: on start it reads the existing
  CSV, collects already-processed rxcuis, and appends only concepts not yet processed.
  It does not rewrite the full file from scratch, so it must not be mirror-replaced.
- Medium source: ~2 additional API calls per concept over the full IN/MIN concept list.
- Output is CSV.
