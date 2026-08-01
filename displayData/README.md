# displayData

Browse the eight S3 categories, shown the way they sit in the bucket.

    cp .env.example .env.local     # fill in the AWS keys
    npm install
    npm run dev                    # http://localhost:3100

Next.js rather than a React app plus a separate API: the bucket is private, so
the credentials have to live on a server somewhere, and route handlers are that
server. One process, one deploy, no CORS.

**The eight categories are enforced in `lib/s3.js`, not in the UI.** A UI that
merely never links to `Old_DataLake` is not a boundary - anyone can put a key
in the query string. `guard()` refuses anything outside them with a 403.

Three endpoints:

| | |
|---|---|
| `/api/list?prefix=` | one level, `Delimiter: '/'` - folders and files, the way S3 presents itself |
| `/api/preview?key=` | first 512 KB of a CSV, parsed into rows |
| `/api/link?key=` | 15-minute presigned URL, so a 200 MB PDF goes browser-to-S3 |

Two things the preview does that a naive version gets wrong:

- **CSV is parsed, not `split(',')`.** Trial titles and company addresses
  contain commas inside quotes; splitting on the delimiter turns one row into
  several and shifts every column after it.
- **The header row is detected.** NUPCO and NHRA publish spreadsheet exports
  with the report title in A1 and the column names underneath. Reading row 0 as
  the header gives one column named after the report and the rest
  `Unnamed: 3` - which is exactly what the generated data-source pages used to
  show.
