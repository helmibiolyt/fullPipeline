# VigiAccess (WHO / Uppsala Monitoring Centre)

Scrapes WHO's global database of reported potential side effects (adverse drug
reactions / individual case safety reports) from <https://vigiaccess.org/>.

- **Topic:** Safety_Pharmacovigilance
- **Source:** vigiaccess.org
- **Entrypoint:** `scraper.py` (`python scraper.py`)
- **Output:** CSV only, in `VigiAccess/`

## What it does

VigiAccess is an Angular single-page app. For a small hardcoded list of common
drugs (`paracetamol, ibuprofen, aspirin, metformin, atorvastatin`) the scraper:

1. Opens the homepage and accepts the terms-and-conditions disclaimer
   (the checkbox `#accept-terms-and-conditions` is custom-styled, so its
   `<label>` is clicked with `force=True`).
2. Clicks **"Search database"**, types the drug and presses Enter.
3. Selects the first matching medicinal product from the results table
   (`table.is-hoverable tbody tr`) and confirms the **"Drug selected"** dialog.
4. Reads the **"Reported potential side effects"** accordion: one row per
   **System Organ Class** (`span[dtid="dashboard-socrow"]`) with a percentage
   and ADR count. Each SOC is expanded to reveal its individual adverse
   reactions (`[dtid="dashboard-ptrow-<i>"]`), capped at 25 reactions per SOC.

### The obfuscation problem

The visible text is **deliberately obfuscated**: invisible unicode characters
(category `Cf`, plus zero-width joiners, word-joiner, BOM, etc.) are inserted
between letters, and numbers are separated with narrow no-break spaces
(`U+202F`) to defeat scrapers. Text/ID matching against visible strings does
NOT work; the scraper locates elements by their stable `dtid` attributes and by
DOM position, then strips the obfuscation with `clean()` / `norm()` helpers
before saving.

There is a POST API `POST /protocol/IProtocol/search` (body `["<drug>"]`) but
its response uses a non-standard RPC serialization that does not parse as clean
JSON, so we scrape the rendered DOM instead.

## Outputs

`VigiAccess/vigiaccess_adr.csv`

| column | meaning |
|---|---|
| search_term | the drug queried |
| product_name | selected medicinal product (de-obfuscated) |
| system_organ_class | MedDRA System Organ Class |
| soc_percent | SOC share of all ADRs for this product (%) |
| soc_total_count | total ADRs in this SOC |
| adverse_reaction | individual reaction (blank if only SOC-level captured) |
| reaction_count | reports for that reaction |
| total_reports | total reports for the active ingredient |
| dataset_date | VigiAccess data-set date (e.g. 7/19/2026) |

`VigiAccess/vigiaccess_meta.csv` — one row per product: `search_term,
product_name, total_reports, dataset_date`.

## Requirements

`playwright` + chromium. The scraper launches **headful** chromium, so it must
run under a virtual display (`manifest.yaml` sets `xvfb: true`).

```bash
xvfb-run -a python scraper.py
```

## Robustness

Each drug is wrapped in try/except so one failure does not abort the run. The
process exits non-zero only if the final CSV would be empty.
