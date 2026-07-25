# adrreports.eu (EudraVigilance) Scraper

Topic: **Safety & Pharmacovigilance**
Source: **https://www.adrreports.eu/** — the European public web portal for the
EudraVigilance database of suspected adverse drug reaction (ADR) reports,
operated by the European Medicines Agency (EMA).

## What this scraper collects

The public A-Z browse page
([`/en/search_subst.html`](https://www.adrreports.eu/en/search_subst.html))
exposes an index of every **centrally authorised active substance** and
**medicinal product** for which EudraVigilance holds ADR reports. This scraper
captures that index:

| Output file | Rows (approx) | Columns |
| --- | --- | --- |
| `EudraVigilance/adrreports_substances.csv` | ~4,580 | `active_substance`, `substance_code`, `letter`, `report_url` |
| `EudraVigilance/adrreports_products.csv` | ~1,750 | `medicinal_product`, `product_code`, `letter`, `report_url` |

- `*_code` — the EudraVigilance "High Level Code" (Substance/Product), extracted
  from the `P3` query parameter of the report URL. Useful as a stable join key.
- `letter` — the A-Z bucket the entry was listed under (`a`-`z` or `0-9`).
- `report_url` — deep link to that substance/product's ADR line-listing report
  on the EMA analytics server (`dap.ema.europa.eu`).

## How it works

The browse page renders its tables client-side: `Scripts/dashboard-api.js`
XHR-loads static HTML fragments from

```
https://www.adrreports.eu/tables/substance/{letter}.html
https://www.adrreports.eu/tables/product/{letter}.html
```

where `{letter}` is `a`-`z` or `0-9`. These fragments are **plain static HTML**
tables, so the scraper fetches them directly with `requests` and parses them
with BeautifulSoup — **no browser / JS execution / Playwright required**. Each
`<a>` in a fragment gives the name (link text) and the report URL (href); the
code is parsed out of the URL. Results are de-duplicated on `(name, code)` and
sorted by name.

## Out of scope: the ADR counts (Power BI / EMA dashboard)

The actual adverse-reaction figures — number of reports, reaction breakdowns by
age/sex/seriousness/MedDRA term, geography, time series — live behind an
interactive EMA analytics dashboard (a Power-BI-style embedded OBIEE/`saw.dll`
application) reached via each `report_url`. That dashboard is session- and
widget-driven and is **intentionally not scraped here** — it is extremely
brittle to automate and out of scope for this source. This scraper delivers the
public **index** (substance/product names, codes and their report deep links);
the per-substance line-listing numbers behind those links are a separate,
non-trivial effort.

## Running

```bash
# From inside the pipeline container (deps already available there):
cd /opt/scrape/Safety_Pharmacovigilance/adrreports.eu
python scraper.py
```

- Writes only inside `EudraVigilance/`.
- Exits non-zero if no data could be scraped.
- No authentication, no API key, no secrets required.

## Dependencies

`requests`, `beautifulsoup4`, `lxml` (see `requirements.txt`). All are already
provided in the pipeline container.

## Status

`enabled: false` in `manifest.yaml` — not yet wired into the DAG schedule.
