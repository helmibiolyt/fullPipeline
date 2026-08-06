# cris.nih.go.kr

CRIS - Clinical Research Information Service, the Republic of Korea's WHO
primary registry.

## Why this exists
Korea had **one** trial in the graph. The WHO ICTRP export carries just 3 KCT
rows, so no amount of loader work could reach the other ~12,000 - the data was
never in anything we held.

## What it scrapes
A single POST endpoint that returns XML:

    POST https://cris.nih.go.kr/cris/search/selectBasic.do
    page, pageSize, lang=en, searchFlg=Y  ->  <items><item>...</item></items>

No HTML parsing, no headless browser, no proxy. Every `/cris/index.do` style
path 404s - the site was restructured - and this is what the current search
page calls.

## Output
- `cris_trials/cris_trials.csv` - one row per trial, English fields preferred.

## Run
```
pip install -r requirements.txt
python cris_downloader.py               # all pages
python cris_downloader.py --limit 3     # first 3 pages, for testing
```

## Fields worth knowing
| column | holds |
|---|---|
| `system_number` | the KCT id, and the trial key. **Not** `research_number`, which is the sponsor's protocol code and is not unique |
| `clinical_step` | phase, already as `Phase3` etc |
| `research_kind` | study type, as `중재연구(Interventional Study)` |
| `research_step` | recruitment status, as `모집 중(Recruiting)` |
| `cp_contents` | conditions, English name followed by the Korean in brackets |
| `diss_cd` | ICD-10 range, e.g. `E00-E90` |
| `resrc_spp_en` | sponsor |

Values that carry both languages in one string are written through unchanged:
the graph's `norm_study_type` and `norm_status` read the English out of prose,
and keeping the registry's own wording is the rule for every other source.
