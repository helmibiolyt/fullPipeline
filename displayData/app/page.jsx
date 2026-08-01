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

const when = (iso) => (iso ? iso.slice(0, 10) : '—')
const ext = (n) => (n.includes('.') ? n.split('.').pop().toLowerCase() : '')
const isCsv = (n) => ext(n) === 'csv'
const isPdf = (n) => ext(n) === 'pdf'
const isViewable = (n) => ['pdf', 'png', 'jpg', 'jpeg', 'gif', 'svg'].includes(ext(n))
const isDoc = (n) => ['pdf', 'doc', 'docx', 'ppt', 'pptx', 'dotx'].includes(ext(n))

async function signed(key, download) {
  const r = await fetch(
    `/api/link?key=${encodeURIComponent(key)}${download ? '&download=1' : ''}`)
  const j = await r.json()
  if (!j.url) throw new Error(j.error || 'could not sign')
  return j.url
}

//: Files shown per folder before "show all". sfda.gov.sa/linked_documents
//: holds 416 PDFs and a browser rendering all of them makes the folder feel
//: broken; the count is always stated, so 30 is a cap and not a lie about how
//: much is there.
const PAGE_FILES = 30

export default function Page() {
  const [prefix, setPrefix] = useState('')
  const [showAll, setShowAll] = useState(false)
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [file, setFile] = useState(null)
  const [filter, setFilter] = useState('')

  const load = useCallback(async (p) => {
    setBusy(true); setError(''); setFile(null); setFilter(''); setShowAll(false)
    try {
      const r = await fetch(`/api/list?prefix=${encodeURIComponent(p)}`)
      const j = await r.json()
      if (j.error) throw new Error(j.error)
      setData(j)
    } catch (e) {
      setError(String(e.message || e)); setData(null)
    } finally { setBusy(false) }
  }, [])

  useEffect(() => { load(prefix) }, [prefix, load])

  const crumbs = prefix ? prefix.replace(/\/$/, '').split('/') : []
  const match = (n) => n.toLowerCase().includes(filter.toLowerCase())

  const allFiles = (data?.files || []).filter((f) => match(f.name))
  const shownFiles = showAll ? allFiles : allFiles.slice(0, PAGE_FILES)
  const hidden = allFiles.length - shownFiles.length

  return (
    <main className="wrap">
      <header>
        <h1>Data lake</h1>
        <span className="sub">the eight categories, shown the way they sit in S3</span>
      </header>

      <nav className="crumbs">
        <button className="crumb" onClick={() => setPrefix('')}>bucket</button>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="sep">/</span>
            <button className="crumb"
                    onClick={() => setPrefix(crumbs.slice(0, i + 1).join('/') + '/')}>
              {c}
            </button>
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
            <input className="filter" placeholder="filter this folder…"
                   value={filter} onChange={(e) => setFilter(e.target.value)} />
          )}

          <table className="listing">
            <thead>
              <tr><th>name</th><th className="num">size</th>
                  <th className="num">modified</th><th className="num">actions</th></tr>
            </thead>
            <tbody>
              {data.folders.filter((f) => match(f.name)).map((f) => (
                <tr key={f.prefix} className="row folder" onClick={() => setPrefix(f.prefix)}>
                  <td><span className="ico">▸</span>{f.name}</td>
                  <td className="num muted">—</td>
                  <td className="num muted">—</td>
                  <td />
                </tr>
              ))}
              {shownFiles.map((f) => (
                <tr key={f.key} className="row" onClick={() => setFile(f)}>
                  <td>
                    <span className={'ico ' + (isCsv(f.name) ? 'csv' : isDoc(f.name) ? 'doc' : '')}>
                      {isCsv(f.name) ? '▤' : isDoc(f.name) ? '▪' : '·'}
                    </span>
                    {f.name}
                  </td>
                  <td className="num">{bytes(f.bytes)}</td>
                  <td className="num muted">{when(f.modified)}</td>
                  <td className="num acts-cell">
                    <button className="act" title="preview in the page"
                            onClick={(e) => { e.stopPropagation(); setFile(f) }}>
                      preview
                    </button>
                    <button
                      className="act"
                      title="download"
                      onClick={async (e) => {
                        e.stopPropagation()
                        try { window.location.href = await signed(f.key, true) }
                        catch (err) { setError(String(err.message || err)) }
                      }}
                    >download</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {allFiles.length > 0 && (
            <div className="count">
              {showAll
                ? `all ${allFiles.length.toLocaleString()} files`
                : `showing ${shownFiles.length} of ${allFiles.length.toLocaleString()} files`}
              {hidden > 0 && (
                <button className="act" onClick={() => setShowAll(true)}>
                  show all {allFiles.length.toLocaleString()}
                </button>
              )}
              {showAll && allFiles.length > PAGE_FILES && (
                <button className="act" onClick={() => setShowAll(false)}>
                  show first {PAGE_FILES}
                </button>
              )}
            </div>
          )}

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
  const [viewUrl, setViewUrl] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    let live = true
    setState({ loading: true }); setViewUrl(''); setErr('')
    fetch(`/api/preview?key=${encodeURIComponent(file.key)}`)
      .then((r) => r.json())
      .then((j) => { if (live) setState({ loading: false, ...j }) })
      .catch((e) => { if (live) setState({ loading: false, error: String(e) }) })

    // A PDF or image is rendered by the browser from a signed URL, so no bytes
    // pass through this app. Fetched here rather than on a second click, so
    // "preview" means the document is simply there.
    if (isViewable(file.name)) {
      signed(file.key, false)
        .then((u) => { if (live) setViewUrl(u) })
        .catch((e) => { if (live) setErr(String(e.message || e)) })
    }
    return () => { live = false }
  }, [file.key, file.name])

  async function download() {
    try { window.location.href = await signed(file.key, true) }
    catch (e) { setErr(String(e.message || e)) }
  }

  // Escape closes, and a click on the backdrop closes. A modal you can only
  // dismiss with a button is the thing everyone complains about.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [onClose])

  return (
    <div className="backdrop" onClick={onClose}>
    <div className="sheet" onClick={(e) => e.stopPropagation()}>
      <div className="sheethead">
        <div>
          <div className="sheetname">{file.name}</div>
          <div className="muted small">
            {bytes(file.bytes)} · {when(file.modified)} · etag {file.etag?.slice(0, 12)}
          </div>
        </div>
        <div className="acts">
          {viewUrl && (
            <button onClick={() => window.open(viewUrl, '_blank', 'noopener')}>
              open in tab
            </button>
          )}
          <button onClick={download}>download</button>
          <button onClick={onClose}>close</button>
        </div>
      </div>

      {err && <div className="err">{err}</div>}
      {state.loading && <div className="muted">reading…</div>}
      {state.error && <div className="err">{state.error}</div>}

      {state.kind === 'pdf' && (
        viewUrl
          ? <iframe className="viewer" src={viewUrl} title={file.name} />
          : !err && <div className="muted">preparing the document…</div>
      )}

      {state.kind === 'image' && (
        viewUrl
          ? <img className="img" src={viewUrl} alt={file.name} />
          : !err && <div className="muted">preparing the image…</div>
      )}

      {state.kind === 'text' && (
        <>
          <div className="muted small">
            first {bytes(Math.min(file.bytes, 262144))}
            {state.truncated && ' of the file'} · <b>download</b> for the whole thing
          </div>
          <pre className="text">{state.text}</pre>
        </>
      )}

      {state.kind === 'binary' && (
        <div className="muted">
          No inline viewer for <code>.{ext(file.name)}</code> — use <b>download</b>.
        </div>
      )}

      {state.kind === 'csv' && (
        <>
          <div className="muted small">
            {state.columns.length} columns
            {state.rowCount != null && (
              <> · {state.rowCountExact
                ? `${state.rowCount.toLocaleString()} rows`
                : `~${state.rowCount.toLocaleString()} rows (estimated from a sample)`}</>
            )}
            {' · showing first '}{state.rows.length}
            {state.headerRow > 0 && ' · header is on line 2, line 1 is a report title'}
            {' · '}<b>download</b> for the whole file
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
    </div>
  )
}
