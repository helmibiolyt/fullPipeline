# pubchem.ncbi.nlm.nih.gov — PubChem

## What it scrapes
Fetches chemical properties and identifiers from the PubChem PUG-REST API (by CID or
name), runs similarity / substructure structure searches, and streams the massive
whole-database identifier mapping files (CID-SMILES, CID-InChI-Key, CID-Parent, …)
from the PubChem FTP mirror, converting them to CSV.

## Source URLs
- https://pubchem.ncbi.nlm.nih.gov/rest/pug — PUG-REST API
- https://ftp.ncbi.nlm.nih.gov/pubchem/Compound/Extras — bulk mapping files

## Output
- `pubchem_data/pubchem_properties.csv` — properties for requested CIDs/names.
- `pubchem_data/pubchem_search_results.csv` — structure-search matches + properties.
- `pubchem_data/pubchem_extracted_mappings.csv` — CSV from an extracted bulk mapping file.

## Run
```
pip install -r requirements.txt
python pubchem_downloader.py property --names "Aspirin,Ibuprofen"
python pubchem_downloader.py download-bulk --file CID-SMILES.gz     # multi-GB
python pubchem_downloader.py extract-bulk --file CID-SMILES.gz
```
Subcommands: `property`, `search`, `download-bulk`, `extract-bulk`.

## Notes
- Writes only inside this folder (`BASE_DIR/pubchem_data/`); the output dir is fixed to `BASE_DIR/pubchem_data` (no `--output-dir` flag).
- Heavy source: whole-database bulk mapping files are multi-GB and slow → `size_class: heavy`, long timeout.
- Re-fetches / re-downloads the full data each run → `mirror: true`.
- `tqdm` is an optional (but imported) progress-bar dependency — see NON-COMMON DEPS.
