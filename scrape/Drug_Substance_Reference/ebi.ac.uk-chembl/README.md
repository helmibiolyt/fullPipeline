# ebi.ac.uk-chembl — ChEMBL Database

## What it scrapes
Searches and extracts drug, compound, target and bioactivity data from ChEMBL via
its REST API, and can download the full offline ChEMBL SQLite database dump from the
EBI FTP mirror (multi-GB).

## Source URLs
- https://www.ebi.ac.uk/chembl/api/data — ChEMBL REST API
- https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/latest/ — bulk SQLite dump

## Output
- `chembl_data/molecule_*.csv` — compound/substance records.
- `chembl_data/target_*.csv` — biological targets.
- `chembl_data/activity_*.csv` — bioactivity measurements (IC50, Ki, …).
- `chembl_data/*_sqlite/…` — extracted full SQLite database (from `download-db`).

## Run
```
pip install -r requirements.txt
python chembl_downloader.py molecule --name aspirin      # example query
python chembl_downloader.py download-db                  # full bulk SQLite dump
```
Subcommands: `molecule`, `target`, `activity`, `download-db`. Override with `--output-dir`,
choose `--format csv|json`.

## Notes
- Writes only inside this folder (`BASE_DIR/chembl_data/`).
- Heavy source: the `download-db` bulk dump is multi-GB and slow → `size_class: heavy`, long timeout.
- Re-fetches / re-downloads the full dataset each run → `mirror: true`.
- `tqdm` is an optional (but imported) progress-bar dependency — see NON-COMMON DEPS.
