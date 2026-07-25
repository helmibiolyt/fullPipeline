# meshb.nlm.nih.gov

Downloads the U.S. National Library of Medicine (NLM) **Medical Subject Headings (MeSH)** XML
databases and parses them into structured CSVs with a streaming XML parser.

## What it scrapes
Fetches the yearly MeSH XML files (descriptors, qualifiers, pharmacological actions, and optionally
supplemental concept records) and converts each to a flat CSV.

## Source URLs
- https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/ — `desc{year}.zip`, `qual{year}.xml`,
  `pa{year}.xml`, `supp{year}.zip`
- Browse portal: https://meshb.nlm.nih.gov/

## Output
Written under `mesh_data/`:
- `mesh_descriptors.csv`
- `mesh_qualifiers.csv`
- `mesh_pharmacological_actions.csv`
- `mesh_supplemental_concepts.csv` (only with `--include-scr`)

## Run
```
pip install -r requirements.txt
python mesh_downloader.py                 # default year 2026
python mesh_downloader.py --include-scr    # also compile Supplemental Concept Records (large)
```
A `--output-dir` override is still accepted; the default resolves to `BASE_DIR/mesh_data`.

## Notes
- Writes only inside this folder (`BASE_DIR/mesh_data/`).
- Final output is **CSV**. Raw XML/ZIP are downloaded to `mesh_data/` and deleted after parsing
  unless `--keep-raw` is passed. Multi-hundred-MB downloads (esp. `--include-scr`) → `size_class: medium`.
- `mirror: true` — re-fetches and rebuilds the full snapshot per production year (skips a CSV only
  if it already exists on disk; delete the CSVs or bump `--year` to force a fresh pull).
- No secrets required.
