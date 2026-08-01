import { listLevel } from '../../../lib/s3'

export const dynamic = 'force-dynamic'

export async function GET(req) {
  const prefix = new URL(req.url).searchParams.get('prefix') || ''
  try {
    return Response.json(await listLevel(prefix))
  } catch (e) {
    return Response.json({ error: e.message }, { status: e.status || 502 })
  }
}
