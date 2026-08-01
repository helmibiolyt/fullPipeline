import { headObject, readRange } from '../../../lib/s3'

export const dynamic = 'force-dynamic'

const PREVIEW_BYTES = 256 * 1024      // plenty for the first rows of anything
const PREVIEW_ROWS = 10

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

/**
 * Ten rows and the column names. Not a viewer for the whole file.
 *
 * The full thing is a download - chembl_structures is 3.1M rows and ClinVar's
 * variant_summary is ~21.8M, and no browser renders that in a table. A preview
 * answers "what is in this file"; the download answers "give me the data".
 */
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

    if (!key.toLowerCase().endsWith('.csv')) {
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
      return Response.json({ ...info, kind: 'csv', columns: [], rows: [] })
    }
    const start = headerRow(all)
    return Response.json({
      ...info,
      kind: 'csv',
      columns: all[start],
      rows: all.slice(start + 1, start + 1 + PREVIEW_ROWS),
      headerRow: start,
    })
  } catch (e) {
    return Response.json({ error: e.message }, { status: e.status || 502 })
  }
}
