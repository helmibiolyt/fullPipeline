# Vector Store — document RAG layer

Turns the **raw documents** (PDF/DOC/PPT) that the scraping pipeline uploads to S3
into searchable, provenance-tagged chunks in **Qdrant**, for retrieval-augmented
answers to the "why / explain / what does the label say" questions the Neo4j graph
can't answer.

CSVs are **not** handled here — they go to the deterministic graph path. This layer
is only for unstructured documents.

## Pipeline
```
S3 raw docs (pdf/doc/ppt)
   → extract text (structure-aware: sections, tables kept)
   → chunk (by section; long sections split semantically with bge-m3)
   → embed (bge-m3: dense + sparse in one pass)
   → upsert to Qdrant  (idempotent on chunk_id)
        metadata: source, doc_id, s3_key, page, section, language, molecule_id*
   ← query: embed → filter (molecule/section/lang) + hybrid search → bge-reranker → top chunks
```
`molecule_id` is filled later when the graph links a fact to its evidence chunk.

## Components
| File | Role |
|---|---|
| `config.py` | settings (Qdrant, bge-m3, chunk sizes, S3) via env |
| `schema.py` | the `Chunk` model + metadata |
| `s3_docs.py` | list / download raw docs from `moine-data` |
| `extract.py` | PDF/DOCX/PPTX → text + sections (pymupdf / python-docx / python-pptx) |
| `chunk.py` | structure-aware chunking + optional semantic split |
| `embed.py` | bge-m3 dense+sparse embeddings; bge-reranker |
| `qdrant_store.py` | Qdrant collection, hybrid upsert/search |
| `ingest.py` | orchestrate S3 docs → chunks → Qdrant (idempotent) |
| `retrieve.py` | query → filtered hybrid search → rerank |
| `docker-compose.yaml` | Qdrant service |

## Deploy — portable stack (Qdrant + API), runs on any EC2
The whole vector store is **two containers**: Qdrant + a FastAPI service the
researcher agent calls. `docker compose up` and it's live anywhere.
```bash
cd vector_store
cp .env.example .env            # set AWS keys
docker compose up -d --build    # Qdrant :6333  +  API :8000

# ingest raw docs from S3 (background)
curl -X POST localhost:8000/ingest \
  -H 'Content-Type: application/json' \
  -d '{"prefix":"Regulatory_Approvals/ema.europa.eu","limit":20}'

# search (what the researcher agent calls)
curl -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"contraindication in hepatic impairment","final_k":5}'
```

### API
| Endpoint | Body | Returns |
|---|---|---|
| `POST /search` | `{query, molecule_id?, section?, language?, final_k?}` | ranked chunks + provenance |
| `POST /ingest` | `{prefix?, limit?}` | starts background ingest |
| `GET /stats` | — | chunk count |
| `GET /health` | — | ok |

### Dev without Docker (embedded Qdrant)
For quick local testing, skip Docker — unset `QDRANT_URL` and Qdrant runs
in-process into `./qdrant_data`:
```bash
pip install -r requirements.txt
python ingest.py --limit 20 && python retrieve.py "your question"
```
`QDRANT_URL` set → server mode (Docker/Cloud); unset → embedded. Same code.

## Models
- **Embedding + semantic-split:** `BAAI/bge-m3` (multilingual — needed for Arabic/Chinese/
  Japanese docs; dense + sparse for hybrid).
- **Reranker:** `BAAI/bge-reranker-v2-m3`.
