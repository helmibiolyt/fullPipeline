import { GetObjectCommand } from '@aws-sdk/client-s3'
import { getSignedUrl } from '@aws-sdk/s3-request-presigner'
import { s3, BUCKET, guard } from '../../../lib/s3'

export const dynamic = 'force-dynamic'

/**
 * Content types by extension.
 *
 * Needed because every object in this bucket is stored as
 * binary/octet-stream - the scrapers upload without one and so did the
 * document backfill. A browser handed octet-stream saves the file whatever
 * the extension says, so an <iframe> pointed at a PDF produced a download
 * prompt and an empty frame instead of the document.
 *
 * Fixed at signing time rather than by rewriting the objects: there are 93,000
 * of them, the fix would have to be repeated after every scrape, and
 * ResponseContentType overrides the stored value for exactly this case.
 */
const TYPES = {
  pdf: 'application/pdf',
  csv: 'text/csv; charset=utf-8',
  json: 'application/json',
  txt: 'text/plain; charset=utf-8',
  xml: 'application/xml',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  ppt: 'application/vnd.ms-powerpoint',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  xls: 'application/vnd.ms-excel',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

/**
 * A short-lived presigned URL.
 *
 * Presigned rather than streaming the object through this server: a 200 MB PDF
 * should go browser-to-S3, and the credentials still never leave the server
 * that signed it.
 *
 * `?download=1` asks the browser to save the file under its real name.
 * Without it the response is marked inline, which is what lets the viewer in
 * the page render the document rather than offering to save it.
 */
export async function GET(req) {
  const url = new URL(req.url)
  const key = url.searchParams.get('key') || ''
  const download = url.searchParams.get('download') === '1'
  try {
    const name = (key.split('/').pop() || 'file').replace(/"/g, '')
    const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : ''
    const cmd = new GetObjectCommand({
      Bucket: BUCKET,
      Key: guard(key),
      ResponseContentType: TYPES[ext] || 'application/octet-stream',
      ResponseContentDisposition: download
        ? `attachment; filename="${name}"`
        : `inline; filename="${name}"`,
    })
    return Response.json({ url: await getSignedUrl(s3(), cmd, { expiresIn: 900 }) })
  } catch (e) {
    return Response.json({ error: e.message }, { status: e.status || 502 })
  }
}
