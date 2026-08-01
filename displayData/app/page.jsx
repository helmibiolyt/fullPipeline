'use client'

import { useCallback, useEffect, useState } from 'react'

const CATEGORY_BLURB = {
  Clinical_Trials_Pipeline_Intelligence: 'Trial registries — CT.gov, EU CTR, WHO ICTRP and the national ones',
  Drug_Substance_Reference: 'What a substance IS — GSRS, ChEMBL, RxNorm, ATC, DailyMed',
  Literature_Evidence: 'Publications — PubMed, Europe PMC, OpenAlex',
  MENA_GCC_Regulatory_Market: 'Gulf and MENA regulators — SFDA, NHRA, DHA, MOH Oman, NUPCO',
  Ontologies_Standards: 'Vocabularies — MeSH, ICD-10/11, LOINC, NCI Thesaurus, CDISC',
  Regulatory_Approvals: 'Approvals and products — FDA, EMA, MHRA, PMDA, Health Canada',
  Safety_Pharmacovigilance: 'Adverse events and recalls — FAERS, VigiAccess, EMA events',
  Targets_Genomics_Biomarkers: 'Genes, proteins and variants — UniProt, HGNC, ClinVar, COSMIC, OpenTargets',
}

function bytes(n) {
  if (!n) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++ }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`
}

function when(iso) {
  if (!iso) return '—'
  return iso.slice(0, 10)
}

const isCsv = (n) => n.toLowerCase().endsWith('.csv')
const isDoc = (n) => /\.(pdf|docx?|pptx?|dotx)$/i.test(n)

export default function Page() {
  const [prefix, setPrefix] = useState('')
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [file, setFile] = useState(null)      // the previewed object
  const [filter, setFilter] = useState('')

  const load = useCallback(async (p) => {
    setBusy(true); setError(''); setFile(null); setFilter('')
    try {
      const r = await fetch(`/api/list?prefix=${encodeURIComponent(p)}`)
      const j = await r.json()
      if (j.error) throw new Error(j.error)
      setData(j)
    } catch (e) {
      setError(String(e.message || e))
      setData(null)
    } finally {
      setBusy(false)
    }
  }, [])

  useEffect(() => { load(prefix) }, [prefix, load])

  const crumbs = prefix ? prefix.replace(/\/$/, '').split('/') : []

  return (
    <main className="wrap">
      <header>
        <h1>Data lake</h1>
        <span className="sub">
          the eight categories, shown the way they sit in S3
        </span>
      </header>

      <nav className="crumbs">
        <button className="crumb" onClick={() => setPrefix('')}>bucket</button>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="sep">/</span>
            <button
              className="crumb"
              onClick={() => setPrefix(crumbs.slice(0, i + 1).join('/') + '/')}
            >{c}</button>
          </span>
        ))}
      </nav>

      {error && <div className="err">{error}</div>}
      {busy && <div className="muted">loading…</div>}

      {!busy && data && !prefix && (
        <div className="cats">
          {data.folders.map((f) => (
            <button key={f.prefix} className="cat" onClick={() => setPrefix(f.prefix)}>
              <span className="catname">{f.name.replaceAll('_', ' ')}</span>
              <span className="catdesc">{CATEGORY_BLURB[f.name] || ''}</span>
            </button>
          ))}
        </div>
      )}

      {!busy && data && prefix && (
        <>
          {(data.folders.length + data.files.length) > 12 && (
            <input
              className="filter"
              placeholder="filter this folder…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          )}

          <table className="listing">
            <thead>
              <tr><th>name</th><th className="num">size</th><th className="num">modified</th></tr>
            </thead>
            <tbody>
              {data.folders
                .filter((f) => f.name.toLowerCase().includes(filter.toLowerCase()))
                .map((f) => (
                <tr key={f.prefix} className="row folder" onClick={() => setPrefix(f.prefix)}>
                  <td><span className="ico">▸</span>{f.name}</td>
                  <td className="num muted">—</td>
                  <td className="num muted">—</td>
                </tr>
              ))}
              {data.files
                .filter((f) => f.name.toLowerCase().includes(filter.toLowerCase()))
                .map((f) => (
                <tr key={f.key} className="row" onClick={() => setFile(f)}>
                  <td>
                    <span className={'ico ' + (isCsv(f.name) ? 'csv' : isDoc(f.name) ? 'doc' : '')}>
                      {isCsv(f.name) ? '▤' : isDoc(f.name) ? '▪' : '·'}
                    </span>
                    {f.name}
                  </td>
                  <td className="num">{bytes(f.bytes)}</td>
                  <td className="num muted">{when(f.modified)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {!data.folders.length && !data.files.length && (
            <div className="muted">this prefix is empty</div>
          )}
        </>
      )}

      {file && <Preview file={file} onClose={() => setFile(null)} />}
    </main>
  )
}

function Preview({ file, onClose }) {
  const [state, setState] = useState({ loading: true })

  useEffect(() => {
    let live = true
    setState({ loading: true })
    fetch(`/api/preview?key=${encodeURIComponent(file.key)}`)
      .then((r) => r.json())
      .then((j) => { if (live) setState({ loading: false, ...j }) })
      .catch((e) => { if (live) setState({ loading: false, error: String(e) }) })
    return () => { live = false }
  }, [file.key])

  async function open() {
    const r = await fetch(`/api/link?key=${encodeURIComponent(file.key)}`)
    const j = await r.json()
    if (j.url) window.open(j.url, '_blank', 'noopener')
  }

  return (
    <div className="sheet">
      <div className="sheethead">
        <div>
          <div className="sheetname">{file.name}</div>
          <div className="muted small">
            {bytes(file.bytes)} · {when(file.modified)} · etag {file.etag?.slice(0, 12)}
          </div>
        </div>
        <div className="acts">
          <button onClick={open}>open</button>
          <button onClick={onClose}>close</button>
        </div>
      </div>

      {state.loading && <div className="muted">reading…</div>}
      {state.error && <div className="err">{state.error}</div>}

      {!state.loading && state.kind === 'binary' && (
        <div className="muted">
          Not a CSV — use <b>open</b> for a signed link to the file itself.
        </div>
      )}

      {!state.loading && state.kind === 'csv' && (
        <>
          <div className="muted small">
            {state.columns.length} columns · showing {state.rows.length} rows
            {state.truncated && ' · preview is the first 512 KB of the file'}
            {state.headerRow > 0 &&
              ' · header is on line 2, line 1 is a report title'}
          </div>
          <div className="scroll">
            <table className="grid">
              <thead>
                <tr>{state.columns.map((c, i) => <th key={i}>{c || <em>—</em>}</th>)}</tr>
              </thead>
              <tbody>
                {state.rows.map((r, i) => (
                  <tr key={i}>
                    {state.columns.map((_, j) => <td key={j}>{r[j] ?? ''}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
