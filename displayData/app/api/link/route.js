import { GetObjectCommand } from '@aws-sdk/client-s3'
import { getSignedUrl } from '@aws-sdk/s3-request-presigner'
import { s3, BUCKET, guard } from '../../../lib/s3'

export const dynamic = 'force-dynamic'

/**
 * A short-lived presigned URL.
 *
 * Presigned rather than streaming the object through this server: a 200 MB PDF
 * should go browser-to-S3, and the credentials still never leave the server
 * that signed it.
 *
 * `?download=1` sets Content-Disposition so the browser saves the file under
 * its real name instead of opening it. Without it a CSV opens as a wall of
 * text in a tab and a PDF is named after the S3 key hash.
 */
export async function GET(req) {
  const url = new URL(req.url)
  const key = url.searchParams.get('key') || ''
  const download = url.searchParams.get('download') === '1'
  try {
    const name = key.split('/').pop() || 'file'
    const cmd = new GetObjectCommand({
      Bucket: BUCKET,
      Key: guard(key),
      ...(download
        ? { ResponseContentDisposition: `attachment; filename="${name.replace(/"/g, '')}"` }
        : {}),
    })
    return Response.json({ url: await getSignedUrl(s3(), cmd, { expiresIn: 900 }) })
  } catch (e) {
    return Response.json({ error: e.message }, { status: e.status || 502 })
  }
}
