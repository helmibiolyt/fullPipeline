import { GetObjectCommand } from '@aws-sdk/client-s3'
import { getSignedUrl } from '@aws-sdk/s3-request-presigner'
import { s3, BUCKET, guard } from '../../../lib/s3'

export const dynamic = 'force-dynamic'

// A short-lived presigned URL rather than streaming the file through this
// server: a 200 MB PDF should go browser-to-S3, and the credentials still
// never leave the server that signed it.
export async function GET(req) {
  const key = new URL(req.url).searchParams.get('key') || ''
  try {
    const url = await getSignedUrl(
      s3(), new GetObjectCommand({ Bucket: BUCKET, Key: guard(key) }),
      { expiresIn: 900 })
    return Response.json({ url })
  } catch (e) {
    return Response.json({ error: e.message }, { status: e.status || 502 })
  }
}
