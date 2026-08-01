import { headObject, readRange, streamObject } from '../../../lib/s3'

export const dynamic = 'force-dynamic'

const PREVIEW_BYTES = 256 * 1024      // plenty for the first rows of anything
const PREVIEW_ROWS = 10

//: Above this, the row count is estimated instead of counted. Counting means
//: reading the whole object: 25 MB is a second, and ClinVar's variant_summary
//: is several GB - a browser asking "how many rows" must not pull that down.
const EXACT_COUNT_LIMIT = 25 * 1024 * 1024

//: How much to read when estimating. Enough that a few long rows do not skew
//: the average, small enough to stay instant.
const SAMPLE_BYTES = 2 * 1024 * 1024

/**
 * Minimal RFC4180 reader.
 *
 * Not `split(',')`: trial titles and company addresses contain commas inside
 * quotes, and splitting on the delimiter turns one row into several and shifts
 * every column after it. Also handles the doubled quote ("") escape and CRLF,
 * both of which the regulator exports use.
 */
function parseCsv(text) {
  const rows = []
  let row = [], field = '', inQuotes = false

  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++ }
        else inQuotes = false
      } else field += c
    } else if (c === '"') {
      inQuotes = true
    } else if (c === ',') {
      row.push(field); field = ''
    } else if (c === '\n') {
      row.push(field); rows.push(row); row = []; field = ''
    } else if (c !== '\r') {
      field += c
    }
  }
  if (field.length || row.length) { row.push(field); rows.push(row) }
  return rows
}

/**
 * Count record separators, ignoring newlines inside quoted fields.
 *
 * A plain newline count is wrong here and wrong in a way that looks right:
 * ClinicalTrials.gov titles and Health Canada addresses contain line breaks
 * inside quotes, so counting '\n' reports more rows than the file has and the
 * number simply looks plausible. State carries across chunks, which is why
 * `inQuotes` is passed in and out rather than reset per buffer.
 */
function countRows(buf, state) {
  let { inQuotes, rows, sawAny } = state
  for (let i = 0; i < buf.length; i++) {
    const c = buf[i]
    if (inQuotes) {
      if (c === 0x22) {
        if (buf[i + 1] === 0x22) i++
        else inQuotes = false
      }
    } else if (c === 0x22) {
      inQuotes = true
    } else if (c === 0x0a) {
      rows++
      sawAny = true
      continue
    }
    if (c !== 0x0d) sawAny = true
  }
  return { inQuotes, rows, sawAny }
}

/**
 * Which line the real header is on.
 *
 * NUPCO and NHRA publish spreadsheet exports with the report title in A1 and
 * the column names underneath. Reading row 0 as the header is what the
 * generated data-source pages used to do, and it produced one column named
 * after the report and the rest "Unnamed: 3".
 */
function headerRow(rows) {
  if (rows.length < 2) return 0
  const first = rows[0].map((c) => c.trim())
  const filled = first.filter(Boolean)
  const placeholder = first.filter((c) => c.toLowerCase().startsWith('unnamed:')).length
  return (filled.length <= 1 || placeholder >= Math.max(2, Math.floor(first.length / 2)))
    ? 1 : 0
}

/** Exact count for a small file: stream it, never hold it. */
async function exactRows(key, headerLines) {
  let state = { inQuotes: false, rows: 0, sawAny: false }
  let trailing = false
  for await (const chunk of await streamObject(key)) {
    state = countRows(chunk, state)
    trailing = chunk.length > 0 && chunk[chunk.length - 1] !== 0x0a
  }
  // A file whose last row has no trailing newline still has that row.
  const total = state.rows + (trailing ? 1 : 0)
  return Math.max(0, total - headerLines)
}

/**
 * Estimated count for a large file: rows per byte from a sample.
 *
 * Reported separately from an exact count and labelled as an estimate,
 * because a number that is quietly approximate is worse than no number - it
 * gets quoted, and nothing in the UI would say it was never counted.
 */
async function estimateRows(key, size, headerLines) {
  const buf = await readRange(key, Math.min(SAMPLE_BYTES, size - 1))
  const state = countRows(buf, { inQuotes: false, rows: 0, sawAny: false })
  if (!state.rows) return null
  const perByte = state.rows / buf.length
  return Math.max(0, Math.round(size * perByte) - headerLines)
}

export async function GET(req) {
  const key = new URL(req.url).searchParams.get('key') || ''
  try {
    const head = await headObject(key)
    const size = head.ContentLength ?? 0
    const info = {
      key,
      bytes: size,
      modified: head.LastModified ? head.LastModified.toISOString() : '',
      etag: (head.ETag || '').replaceAll('"', ''),
    }

    const lower = key.toLowerCase()
    const ext = lower.includes('.') ? lower.split('.').pop() : ''

    // Rendered in the page by the browser itself, from a signed URL - no
    // bytes come through this server for these.
    if (['pdf', 'png', 'jpg', 'jpeg', 'gif', 'svg'].includes(ext)) {
      return Response.json({ ...info, kind: ext === 'pdf' ? 'pdf' : 'image',
        columns: [], rows: [] })
    }

    // Text-ish formats get their head returned as text, so JSON and XML are
    // readable in place rather than being a download with no way to look
    // inside first.
    if (['json', 'txt', 'xml', 'md', 'yaml', 'yml', 'log', 'tsv'].includes(ext)) {
      const b = await readRange(key, Math.min(PREVIEW_BYTES, Math.max(0, size - 1)))
      let t = b.toString('utf8').replace(/^﻿/, '')
      if (size > PREVIEW_BYTES) {
        const cut = t.lastIndexOf('\n')
        t = cut > 0 ? t.slice(0, cut + 1) : t
      }
      return Response.json({ ...info, kind: 'text', text: t,
        truncated: size > PREVIEW_BYTES, columns: [], rows: [] })
    }

    if (ext !== 'csv') {
      return Response.json({ ...info, kind: 'binary', columns: [], rows: [] })
    }

    const buf = await readRange(key, Math.min(PREVIEW_BYTES, Math.max(0, size - 1)))
    let text = buf.toString('utf8').replace(/^﻿/, '')
    // A range read almost always cuts the last line in half, and a truncated
    // row renders as a column count that does not match the header.
    if (size > PREVIEW_BYTES && text.includes('\n')) {
      text = text.slice(0, text.lastIndexOf('\n'))
    }

    const all = parseCsv(text)
    if (!all.length) {
      return Response.json({ ...info, kind: 'csv', columns: [], rows: [],
        rowCount: 0, rowCountExact: true })
    }
    const start = headerRow(all)
    const headerLines = start + 1

    let rowCount = null
    let rowCountExact = false
    try {
      if (size <= EXACT_COUNT_LIMIT) {
        rowCount = await exactRows(key, headerLines)
        rowCountExact = true
      } else {
        rowCount = await estimateRows(key, size, headerLines)
      }
    } catch {
      // A count that fails must not take the preview with it.
      rowCount = null
    }

    return Response.json({
      ...info,
      kind: 'csv',
      columns: all[start],
      rows: all.slice(start + 1, start + 1 + PREVIEW_ROWS),
      headerRow: start,
      rowCount,
      rowCountExact,
    })
  } catch (e) {
    return Response.json({ error: e.message }, { status: e.status || 502 })
  }
}
