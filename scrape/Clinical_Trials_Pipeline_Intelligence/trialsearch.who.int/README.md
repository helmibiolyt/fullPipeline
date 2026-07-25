# trialsearch.who.int

WHO ICTRP (International Clinical Trials Registry Platform) trial search portal.

## What it scrapes
Parses the "List By" country page, then for each country replays the ASP.NET
postback sequence (Export to XML -> accept terms -> export all trials) to download
that country's trials as an XML file. An optional step (`--csv-dir`) merges all
downloaded XMLs into a single `who_trials.csv`.

## Source URLs
- https://trialsearch.who.int/ListBy.aspx?TypeListing=1 — country listing
- https://trialsearch.who.int/AdvSearch.aspx?Country=... — per-country export flow

## Output
- `who_trials_xml/<Country>.xml` — one XML file per country (default output).
- `who_trials.csv` — combined CSV, only when run with `--csv-dir` (built via the
  script's own `convert_xmls_to_csv`).

## Run
```
pip install -r requirements.txt
python who_collector.py                                   # download per-country XML
python who_collector.py --csv-dir who_trials_csv          # download + build combined CSV
python who_collector.py --csv-only --csv-dir who_trials_csv  # only convert existing XML
```

## Notes
- Writes only inside this folder (`BASE_DIR/who_trials_xml/`).
- Full snapshot each run; per-country resume (skip existing XML) is an optimization
  (mirror: true).
- ⚠️ Currently emits XML by default; pipeline publishes only CSV — needs a CSV
  conversion step. The script CAN produce CSV, but only when invoked with `--csv-dir`
  (not the default `python who_collector.py` invocation the manifest runs).
