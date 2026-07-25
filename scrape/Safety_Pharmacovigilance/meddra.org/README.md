# MedDRA (meddra.org) scraper

Source: https://www.meddra.org/
Topic: Safety & Pharmacovigilance
Status: **`enabled: false`** in `manifest.yaml` (not wired into the DAG yet).

## What MedDRA is

MedDRA (Medical Dictionary for Regulatory Activities) is the standardized medical
terminology used worldwide for regulatory reporting of adverse events. It is
maintained by the MSSO (Maintenance and Support Services Organization) and has a
5-level hierarchy: **SOC → HLGT → HLT → PT → LLT** (27 System Organ Classes at the
top level). A new version is released twice a year (March = `X.0`, September = `X.1`).

## Login limitation (important)

The **full MedDRA terminology download** (the SOC/HLGT/HLT/PT/LLT hierarchy files,
the browser, MVAT, etc.) is **NOT publicly accessible**. It requires a free MSSO
subscription account and is served as ASCII / MedDRA-format files from behind a
login. That content is intentionally **out of scope** for this scraper.

To ingest the full hierarchy later, add MSSO credentials via environment variables
(`MEDDRA_USERNAME` / `MEDDRA_PASSWORD`) and implement the authenticated MSSO
download endpoint. **Do not hardcode credentials** — the scraper reads only from
env vars if/when this is added.

## What IS public and gets scraped

The public site https://www.meddra.org/ is an Angular single-page app backed by a
Drupal JSON API at `https://admin.meddra.org/api` (no authentication required).
The scraper reads two endpoints from it:

| Endpoint | Used for |
|----------|----------|
| `GET /api/nodes` (paginated via `?offset=`) | MedDRA release/version history |
| `GET /api/timelines` | the "Evolving MedDRA" milestone timeline |

### Outputs (`MedDRA/`)

- **`meddra_versions.csv`** — MedDRA release history derived from the site's content
  nodes. ~47 versions (7.0 through the current release).
  Columns: `version, release_date, announcement_date, source_title, source_url`.
  `release_date` (e.g. `March 2026`) is populated where a dated English release/
  guidance node exists; older versions may have a blank `release_date` because the
  public site no longer carries a dated node for them.
- **`meddra_timeline.csv`** — the official "Evolving MedDRA" milestone timeline
  (~61 milestones, 1999→present) with ISO dates.
  Columns: `date, title, url, summary`.

## What is NOT provided here (and why)

- **The 27 System Organ Classes as a data file / the full term hierarchy** — this
  lives in the subscription-only dictionary download, so it is not scraped. (The SOC
  names appear only inside login-gated tools and PDF guides, not as structured public
  data on the site.)

## Run

```bash
python scraper.py
```

- Writes only inside `MedDRA/`. Produces CSV.
- Exits non-zero if no data could be scraped.

### Run inside the pipeline container

```bash
cd /home/dev-helmi/Desktop/fullPipeline/automation
sudo docker compose exec -T -e HOME=/home/airflow airflow-scheduler \
  bash -lc 'cd /opt/scrape/Safety_Pharmacovigilance/meddra.org && python scraper.py'
```

## Dependencies

`requests`, `beautifulsoup4`, `lxml` (see `requirements.txt`). All are already
available in the pipeline container as `--user` packages.
