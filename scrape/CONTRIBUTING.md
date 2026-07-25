# Adding a new scraper

Follow this contract and your scraper is picked up by the Airflow pipeline as a
new node automatically — **you only need access to the `scrape/` folder**. No
pipeline code changes, no central registry to edit.

Everything the pipeline needs lives in your source folder:
1. **The folder location** — `scrape/<Topic>/<source>/` (Topic + source are read from the path)
2. **A `manifest.yaml`** in that folder — the pipeline auto-discovers it

A ready-to-copy skeleton is in [`_template/example.com/`](_template/example.com).

---

## 1. Folder layout

```
scrape/
└── <Topic>/                 # category, e.g. Clinical_Trials  (underscores, no spaces/&)
    └── <source>/            # the source, e.g. clinicaltrials.gov
        ├── scraper.py       # your entrypoint (any name; declared in the registry)
        ├── requirements.txt # your Python deps
        ├── README.md        # what it scrapes + source URLs
        └── <Category>/      # output folder(s) your scraper writes into
            └── .gitkeep     # keep the empty folder in git
```

- **Topic** and **source** names become the S3 path `s3://moine-data/<Topic>/<source>/`.
- Reuse an existing `<Topic>` folder or create a new one for a new category.

---

## 2. The scraper contract

Your entrypoint MUST:

- **Be runnable as `python <entrypoint>`** with a `if __name__ == "__main__": main()` block.
- **Resolve its own path**, not the current directory:
  ```python
  from pathlib import Path
  BASE_DIR = Path(__file__).resolve().parent
  ```
- **Write output only inside its own folder** (`BASE_DIR/<Category>/...`). Never write
  elsewhere on disk.
- **Produce CSV.** Only `.csv` is published to S3. You may also download `.xlsx`
  (auto-converted to CSV) or `.pdf/.doc/.ppt` (converted via MiniMax by the
  pipeline's `convert_docs` step) — but the *data* must end up as CSV.
- **Exit non-zero on failure** (raise an exception or `sys.exit(1)`). A zero exit
  with no output is treated as a failed scrape.
- **Re-fetch the full dataset each run** (idempotent snapshot). The pipeline mirrors
  your output to S3 and deletes anything you no longer produce. If your source is
  append-only/incremental, say so — set `mirror: false` in the registry.

Your entrypoint MUST NOT:

- Hardcode secrets — read them from environment variables.
- Depend on files from other sources, or on network state left by a previous run.
- Delete or write outside `BASE_DIR`.

### Minimal skeleton

```python
#!/usr/bin/env python3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def main() -> None:
    out = BASE_DIR / "MyCategory"
    out.mkdir(parents=True, exist_ok=True)
    rows = fetch_everything()          # your scraping logic
    if not rows:
        sys.exit("no data scraped")    # non-zero exit -> pipeline marks it failed
    write_csv(out / "my_dataset.csv", rows)

if __name__ == "__main__":
    main()
```

---

## 3. `manifest.yaml` (in your source folder)

Drop a `manifest.yaml` next to your scraper. The pipeline auto-discovers it — no
central file to edit. Topic and source are taken from the folder path, so you
don't repeat them here.

```yaml
entrypoint: scraper.py          # your file name
in_place: true                  # true: writes next to the script (typical)
                                # false: writes to {run_dir} (also set args below)
# args: "--output-dir {run_dir}"   # only when in_place: false
output_subdirs: [MyCategory]    # folders you write into; [] = auto-detect all CSVs
size_class: small               # small | medium | heavy  (concurrency pool)
timeout_min: 60                 # hard cap on the scrape step
mirror: true                    # true: full replace; false: additive/append-only
enabled: true                   # false = discovered but NOT run in the DAG (gated/WIP)
```

New scrapers are typically added with `enabled: false` first, then flipped to
`true` by the maintainer once verified — never write to S3 yourself; the pipeline
owns S3 (upload, verify, atomic swap). Your scraper only writes local CSV.

Field notes:
- **`in_place: true`** is the normal case. Use `false` only if your scraper takes an
  output directory, and set `args: "--output-dir {run_dir}"`.
- **`output_subdirs`** lists your category folders so only your CSVs are collected.
  Leave `[]` to auto-detect every CSV under your folder.
- **`size_class`** picks the Airflow pool: `heavy` for multi-GB/slow sources so they
  don't hog workers.

---

## 4. Dependencies

List your deps in your source `requirements.txt`. These common packages are
**already installed** in the pipeline image, so if you only use them, you're done:

> `requests`, `beautifulsoup4`, `lxml`, `pandas`, `openpyxl`, `python-dateutil`

If you need anything **outside that list**, note it in your PR/message so the
maintainer can add it to the image (`automation/requirements.txt`) — that's the one
thing outside `scrape/` that a new dependency requires.

---

## 5. What gets committed

`.gitignore` ignores everything except code + docs + `.gitkeep`. So commit:

- `scraper.py`, `requirements.txt`, `README.md`
- a `.gitkeep` in each empty output folder (`touch MyCategory/.gitkeep`)

**Never commit** scraped data (`.csv/.pdf/.xlsx/...`), logs, or `.env`. They're
gitignored — do not `git add -f` them.

---

## 6. Pre-PR checklist

- [ ] Folder at `scrape/<Topic>/<source>/`, entrypoint runs via `python scraper.py`.
- [ ] Writes CSV only inside its own folder; exits non-zero on failure.
- [ ] `manifest.yaml` present in the source folder.
- [ ] `requirements.txt` present; any non-common dep flagged for the maintainer.
- [ ] `.gitkeep` in each output folder; no data/secrets staged
      (`git ls-files | grep -E '\.csv|\.env' ` returns nothing).
- [ ] Ran it once locally and it produced CSVs.

That's it — everything is inside `scrape/`. When the maintainer pulls, the new
scraper appears as a node automatically.
```
