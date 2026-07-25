# evs.nci.nih.gov

Downloads the **NCI Thesaurus (NCIt)** terminology, CUI/ontology mappings, neoplasm core data, and
drug/substance lists from the NCI EVS FTP server and parses each into a clean CSV.

## What it scrapes
- Core NCIt flat file (`Thesaurus.FLAT.zip` → concepts with synonyms, definitions, semantic types)
- `nci_code_cui_map.dat` (NCIt code → UMLS CUI)
- Cross-ontology mappings: GO, ChEBI, HGNC, SwissProt
- Neoplasm Core subsets and Antineoplastic Agent drug list

## Source URLs
- https://evs.nci.nih.gov/ftp1/NCI_Thesaurus/ — core, mappings, neoplasm, drug/substance files

## Output
Written under `nci_thesaurus_data/`:
- `nci_thesaurus_concepts.csv`, `nci_code_cui_map.csv`
- `mapping_go_ncit.csv`, `mapping_ncit_chebi.csv`, `mapping_ncit_hgnc.csv`, `mapping_ncit_swissprot.csv`
- `neoplasm_core.csv`, `neoplasm_core_rels_ncit_molecular.csv`, `antineoplastic_agents.csv`
- (raw downloads land in a temporary `nci_thesaurus_data/raw/` that is removed unless `--keep-raw`)

## Run
```
pip install -r requirements.txt
python nci_thesaurus_downloader.py
```
A `--output-dir` override is still accepted; the default resolves to `BASE_DIR/nci_thesaurus_data`.

## Notes
- Writes only inside this folder (`BASE_DIR/nci_thesaurus_data/`).
- Final output is **CSV**. Resumable downloads (HTTP Range) + progress tracker; a completed CSV is
  skipped on re-run, but a full run rebuilds the whole snapshot → `mirror: true`.
- `size_class: medium` — the core FLAT zip plus several mapping/neoplasm files (~100s of MB total,
  ~170k concepts parsed).
- No secrets required.
