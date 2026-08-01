import { S3Client, ListObjectsV2Command, HeadObjectCommand, GetObjectCommand }
  from '@aws-sdk/client-s3'

/**
 * The only prefixes this app will read.
 *
 * Enforced here, on the server, rather than by the UI only ever linking to
 * them. A UI that hides something is not a boundary - anyone can type a key
 * into the query string. `Old_DataLake` and `endpoint-schema.md` are out of
 * scope by decision, so refusing them belongs where the request is served.
 */
export const CATEGORIES = [
  'Clinical_Trials_Pipeline_Intelligence',
  'Drug_Substance_Reference',
  'Literature_Evidence',
  'MENA_GCC_Regulatory_Market',
  'Ontologies_Standards',
  'Regulatory_Approvals',
  'Safety_Pharmacovigilance',
  'Targets_Genomics_Biomarkers',
]

export const BUCKET = process.env.S3_BUCKET || 'moine-data'
const REGION = process.env.AWS_REGION || 'us-east-1'

let client
export function s3() {
  // One client for the process. The SDK pools connections, and a new client
  // per request re-resolves credentials on every listing.
  if (!client) client = new S3Client({ region: REGION })
  return client
}

/** True for the eight categories and anything beneath them. */
export function allowed(prefix) {
  const p = (prefix || '').replace(/^\/+/, '')
  if (p === '') return true
  return CATEGORIES.some((c) => p === c || p.startsWith(c + '/'))
}

export function guard(prefix) {
  const p = (prefix || '').replace(/^\/+/, '')
  if (!allowed(p)) {
    const e = new Error('outside the eight categories')
    e.status = 403
    throw e
  }
  return p
}

/**
 * One level of the bucket: folders and files directly under `prefix`.
 *
 * Delimiter '/' rather than a recursive walk, because that is how S3 presents
 * itself and the point is to show the data where it actually sits. It is also
 * the only workable choice: Drug_Substance_Reference alone is millions of keys.
 */
export async function listLevel(prefix) {
  let p = guard(prefix)
  if (p && !p.endsWith('/')) p += '/'

  if (!p) {
    return {
      prefix: '',
      folders: CATEGORIES.map((c) => ({ name: c, prefix: c + '/' })),
      files: [],
    }
  }

  const folders = []
  const files = []
  let token
  do {
    const out = await s3().send(new ListObjectsV2Command({
      Bucket: BUCKET, Prefix: p, Delimiter: '/', ContinuationToken: token,
    }))
    for (const cp of out.CommonPrefixes || []) {
      folders.push({ name: cp.Prefix.slice(p.length).replace(/\/$/, ''), prefix: cp.Prefix })
    }
    for (const o of out.Contents || []) {
      if (o.Key === p) continue          // the folder marker itself
      files.push({
        name: o.Key.slice(p.length),
        key: o.Key,
        bytes: o.Size ?? 0,
        modified: o.LastModified ? o.LastModified.toISOString() : '',
        etag: (o.ETag || '').replaceAll('"', ''),
      })
    }
    token = out.IsTruncated ? out.NextContinuationToken : undefined
  } while (token)

  return { prefix: p, folders, files }
}

export async function headObject(key) {
  return s3().send(new HeadObjectCommand({ Bucket: BUCKET, Key: guard(key) }))
}

/** A byte range, so previewing a multi-GB CSV does not download it. */
export async function readRange(key, bytes) {
  const out = await s3().send(new GetObjectCommand({
    Bucket: BUCKET, Key: guard(key), Range: `bytes=0-${bytes}`,
  }))
  return Buffer.from(await out.Body.transformToByteArray())
}
