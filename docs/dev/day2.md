# Day 2 — Local Gateway, Vector Store & End-to-End Validation (Beginner Guide, WSL2)

**Who this is for:** the same beginner developer who completed Day 1. Day 1 concepts (containers, images, volumes, networks, the Compose loop) are assumed. New concepts are explained on first use.

**Day 2 goal in one sentence:** by the end of today, the full local stack is wired end-to-end — LiteLLM sits in front of the engines and issues virtual API keys, Qdrant holds the document vectors, the four-stage RAG pipeline (lexical → dense → fuse → rerank) retrieves the right chunks, the Qfind client has been rewired away from Jina/Groq, and a streamed, source-cited answer arrives at the desktop client using a Japanese document.

**What you built yesterday (prerequisite — all must be true):**
- vLLM serving `qfind-chat` (Qwen2.5-3B AWQ) on the GPU, port 8000.
- TEI embedding (`qfind-embed`, BGE-M3) on CPU, port 8001.
- TEI reranking (`qfind-rerank`, BGE-reranker-v2-m3) on CPU, port 8002.
- Postgres + Redis running on `qfind-net`.
- `scripts/smoke-test.sh` passing against all three engines.

> **Why Day 2 is the densest day.** Eight steps cover three separate domains: gateway setup, vector database, and application pipeline. Each one is a potential learning-curve bloat point. The golden rule still applies: **do not advance past a failing Check**. The local load test (Step 8) is deliberately lightweight because the 4050 cannot simulate 20–30 real users — its job is to prove the gateway doesn't crash under 3–5 parallel requests, nothing more.

---

## 0. New concepts for Day 2

- **API gateway:** a single front door that all clients call. Instead of Qfind talking directly to vLLM or TEI, it talks to LiteLLM, which validates the key, checks limits, and routes to the right backend. Same idea as a hotel reception desk: guests don't walk straight into the kitchen.
- **Virtual API key:** a key (`sk-...`) that LiteLLM generates and issues. It is *not* an OpenAI or Groq key — it's a company-controlled credential scoped to specific models, with a budget and rate limit. The company can revoke it instantly.
- **Master key:** the admin key that creates and revokes virtual keys. It never leaves the server and is never given to any user or application.
- **Budget / rate limit:** `max_budget` caps how many "token-equivalent" units a key can spend (even self-hosted, tracking usage matters for capacity planning); `rpm_limit`/`tpm_limit` cap requests-per-minute and tokens-per-minute. An accidentally-looping client won't exhaust the GPU for everyone else.
- **Vector / embedding:** a list of numbers that represents the *meaning* of a piece of text. Similar texts produce similar vectors. Used for semantic search ("find passages about invoices" even if the word "invoice" doesn't appear).
- **Qdrant:** a database designed specifically for storing and searching vectors. Like Postgres is for rows and columns, Qdrant is for dense/sparse numeric vectors.
- **Collection:** Qdrant's equivalent of a database table. You create one collection to hold all document chunks.
- **Chunk:** a short slice of a document (a paragraph, a section). We embed each chunk separately so retrieval can point to a specific passage, not a whole file.
- **Hybrid search:** combining lexical search (Lucene/BM25, exact keywords) with dense vector search (Qdrant) and merging the results. Better than either alone — especially for Japanese, where exact terms and semantic meaning both matter.
- **Reranking:** after hybrid search returns ~20 candidates, the reranker cross-checks each one against the query to find the best ~5. Slower but far more accurate than embedding-similarity alone.
- **RAG (Retrieval-Augmented Generation):** the full pipeline — search → rerank → assemble prompt → LLM answer. The LLM never "knows" your documents from training; it reads the relevant chunks inserted into the prompt.
- **RRF (Reciprocal Rank Fusion):** a simple, effective formula for merging two ranked lists (Lucene results + Qdrant results) into one. Each item gets a score based on its rank in each list; scores are summed.
- **SSE (Server-Sent Events):** the streaming protocol vLLM and LiteLLM use to send tokens one-by-one as they're generated. The Qfind client receives and renders each token as it arrives instead of waiting for the full response.

---

## 1. Stand up LiteLLM (the API gateway)

LiteLLM is the single front door for all Qfind requests. It needs Postgres (to store key/budget state) and Redis (for rate-limit counters) — both are already running from Day 1.

### 1a. Add the LiteLLM config file

Create `deployment/litellm/config.yaml`:
```yaml
model_list:
  - model_name: qfind-chat
    litellm_params:
      model: openai/qfind-chat       # the stable served name vLLM uses
      api_base: http://vllm-chat:8000/v1
      api_key: "none"                # self-hosted, no upstream key

  - model_name: qfind-embed
    litellm_params:
      model: openai/qfind-embed
      api_base: http://tei-embed:80
      api_key: "none"

  - model_name: qfind-rerank
    litellm_params:
      model: openai/qfind-rerank
      api_base: http://tei-rerank:80
      api_key: "none"

general_settings:
  master_key: ${LITELLM_MASTER_KEY}
  database_url: ${DATABASE_URL}
  store_model_in_db: true
```

> `http://vllm-chat:8000` uses the Docker *service name* as the hostname — this works because all containers are on `qfind-net`. Containers find each other by service name; `localhost` from inside a container refers to that container only, not the host.

### 1b. Add `DATABASE_URL` and `LITELLM_MASTER_KEY` to `.env`

Open `deployment/.env` and add:
```dotenv
DATABASE_URL=postgresql://litellm:change-me@litellm-db:5432/litellm
LITELLM_MASTER_KEY=sk-master-change-me-to-a-long-random-string
```
> The master key should be a long random string, not a hand-typed password. Generate one:
> ```bash
> python3 -c "import secrets; print('sk-master-' + secrets.token_hex(24))"
> ```
> Paste the output into `.env`. **Never commit this file.**

### 1c. Add the `litellm` service to `docker-compose.yml`

Add under `services:`:
```yaml
  litellm:
    image: ghcr.io/berriai/litellm:main-v1.40.0   # pin a tested version
    command: ["--config", "/app/config.yaml", "--port", "4000", "--detailed_debug"]
    volumes:
      - ./litellm/config.yaml:/app/config.yaml:ro
    ports:
      - "4000:4000"
    env_file: [.env]
    depends_on:
      - litellm-db
      - litellm-redis
    networks: [qfind-net]
```
- **`depends_on`** tells Compose to start Postgres and Redis first. It does **not** wait for them to be *ready* (just started) — LiteLLM has its own retry logic for that.
- **`env_file`** injects the entire `.env` into the container's environment, so LiteLLM can read `LITELLM_MASTER_KEY` and `DATABASE_URL`.

### 1d. Start LiteLLM and watch it connect

```bash
docker compose up -d litellm
docker compose logs -f litellm    # wait for "LiteLLM: Proxy initialized"; Ctrl+C to stop watching
```

### Check — gateway routes a request

Send the *same* chat request from Day 1, but now through port **4000** instead of 8000, with the master key as the bearer token:
```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-master-change-me-to-a-long-random-string" \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"Say hello."}]}'
```
→ returns the same JSON answer as yesterday, but the gateway routed it.

> **Why this matters:** LiteLLM is now the only entry point. The day-1 direct-to-vLLM port (8000) still works for debugging, but Qfind will only ever talk to port 4000 going forward — and that's what will exist in production.

---

## 2. Generate a scoped virtual key and update smoke-test.sh

The master key is for administration only. Qfind gets a *virtual* key — one that only allows the three `qfind-*` models, has a budget, and can be revoked.

### 2a. Generate the virtual key

```bash
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-master-change-me-to-a-long-random-string" \
  -H "Content-Type: application/json" \
  -d '{
    "models": ["qfind-chat", "qfind-embed", "qfind-rerank"],
    "max_budget": 10.0,
    "rpm_limit": 60,
    "tpm_limit": 50000,
    "budget_duration": "30d",
    "user_id": "local-dev"
  }'
```
→ returns JSON with a `"key": "sk-..."` field. That is your virtual key.

Copy it somewhere safe (e.g., a scratch file — **not** committed). You'll use it in every subsequent request.

### 2b. Verify the virtual key is scoped correctly

```bash
# This should SUCCEED (allowed model):
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-YOUR-VIRTUAL-KEY-HERE" \
  -H "Content-Type: application/json" \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}'

# This should FAIL with 401/403 (model not in the key's allowed list):
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-YOUR-VIRTUAL-KEY-HERE" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4","messages":[{"role":"user","content":"hi"}]}'
```

### 2c. Extend smoke-test.sh with a gateway-routed variant

Edit `scripts/smoke-test.sh` to add a second block that tests *through LiteLLM* (port 4000) using the virtual key:

```bash
#!/usr/bin/env bash
set -euo pipefail

# ---- Direct engine checks (Day 1 — keep for debugging) ----
echo "=== Direct engine checks ==="
echo "== chat (non-stream) =="
curl -fsS http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}' > /dev/null && echo OK
echo "== embed =="
curl -fsS http://localhost:8001/embed -H 'Content-Type: application/json' \
  -d '{"inputs":"hello"}' > /dev/null && echo OK
echo "== rerank =="
curl -fsS http://localhost:8002/rerank -H 'Content-Type: application/json' \
  -d '{"query":"x","texts":["x","y"]}' > /dev/null && echo OK

# ---- Gateway checks (Day 2+) ----
VKEY="${LITELLM_VIRTUAL_KEY:-}"   # read from env or leave empty
if [ -z "$VKEY" ]; then
  echo "SKIP gateway checks: set LITELLM_VIRTUAL_KEY env var to enable"
else
  echo ""
  echo "=== Gateway checks (via LiteLLM port 4000) ==="
  echo "== gateway chat =="
  curl -fsS http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $VKEY" -H 'Content-Type: application/json' \
    -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}' > /dev/null && echo OK
  echo "== gateway embed =="
  curl -fsS http://localhost:4000/v1/embeddings \
    -H "Authorization: Bearer $VKEY" -H 'Content-Type: application/json' \
    -d '{"model":"qfind-embed","input":"hello"}' > /dev/null && echo OK
fi

echo "ALL CHECKS PASSED"
```
Run it (passing the virtual key via env):
```bash
LITELLM_VIRTUAL_KEY="sk-YOUR-VIRTUAL-KEY-HERE" ./scripts/smoke-test.sh
```

### Check
- All direct checks still pass.
- Both gateway checks pass with the virtual key.
- The "wrong model" test from Step 2b returned an error (not 200).

> **Why this matters:** the virtual key is what separates "anyone can call vLLM directly on port 8000" from "Qfind uses a company-controlled, revocable, budget-limited credential." The security model only works if requests go through the gateway.

---

## 3. Stand up Qdrant (the vector database)

Qdrant stores the document chunk vectors. Think of it as a searchable index where each entry is a chunk of text represented as a list of numbers (a vector), along with metadata (which file it came from, which section, when it was last modified).

### 3a. Add Qdrant to docker-compose.yml

```yaml
  qdrant:
    image: qdrant/qdrant:v1.9.0    # pin a specific version
    volumes:
      - qdrant-data:/qdrant/storage
    ports:
      - "6333:6333"   # HTTP API
      - "6334:6334"   # gRPC (optional, can leave out if not used)
    networks: [qfind-net]
```

Also add `qdrant-data` to the top-level `volumes:` section:
```yaml
volumes:
  pgdata:
  qdrant-data:    # add this line
```

Start it:
```bash
docker compose up -d qdrant
docker compose logs -f qdrant    # wait for "Qdrant HTTP listening on 0.0.0.0:6333"
```

### 3b. Create the collection

A **collection** is like a table — you create it once and it holds all your document chunk vectors. BGE-M3 produces 1024-dimensional dense vectors plus sparse vectors, so we configure both.

Install the Python Qdrant client in WSL2 (you'll need this to create and query collections from the embedding pipeline code later):
```bash
pip install qdrant-client
```

Then create `deployment/qdrant/init_collection.py`:
```python
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance,
    SparseVectorParams, SparseIndexParams,
)

client = QdrantClient(host="localhost", port=6333)

COLLECTION = "qfind_docs"

if client.collection_exists(COLLECTION):
    print(f"Collection '{COLLECTION}' already exists — skipping creation.")
else:
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={
            # BGE-M3 dense vector: 1024 dimensions
            "dense": VectorParams(size=1024, distance=Distance.COSINE)
        },
        sparse_vectors_config={
            # BGE-M3 sparse vector (for hybrid BM25-style search)
            "sparse": SparseVectorParams(index=SparseIndexParams())
        },
    )
    print(f"Collection '{COLLECTION}' created.")

print("Collections:", [c.name for c in client.get_collections().collections])
```

Run it:
```bash
python3 deployment/qdrant/init_collection.py
```
→ prints `Collection 'qfind_docs' created.`

### Check
```bash
curl http://localhost:6333/collections/qfind_docs
```
→ returns JSON with `"status": "green"` and config showing 1024-dimensional dense + sparse vectors.

> **Why this matters:** the collection has to exist before any chunk can be inserted. Running this script is idempotent (safe to re-run), which is the correct pattern for setup scripts.

---

## 4. Build the embedding pipeline

The embedding pipeline is the bridge between Qfind's file system and Qdrant. When a file is indexed, each chunk is embedded via TEI and stored in Qdrant. On re-index, old chunks for that file are replaced, not duplicated.

> **Scope clarification.** The execution plan assumes Qfind's file-watch/indexing hook largely exists. This step is about *wiring* that hook to call TEI and upsert to Qdrant, not building a file watcher from scratch. If Qfind has no hook yet, see the troubleshooting note at the end of this step.

### 4a. Understand the pipeline structure

```
File changed on disk
  → Chunk the text (paragraph-aware, Japanese-sentence-safe)
  → For each chunk batch: call TEI /embed  → get dense + sparse vectors
  → Upsert to Qdrant with payload: { file_path, section, mtime, chunk_index }
  → Also update Lucene index (existing, unchanged)
```

The **chunk ID** must be stable and derived from the file path + chunk index. Using a random ID on each run would duplicate entries. Using a hash of `file_path + chunk_index` ensures re-embedding a file overwrites exactly its prior chunks.

### 4b. Create the embedding helper

Create `deployment/scripts/embed_pipeline.py`:

```python
"""
Embedding pipeline: chunk a file, embed with TEI, upsert to Qdrant.
Usage: python embed_pipeline.py <file_path>
"""
import sys, hashlib, requests
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, SparseVector

TEI_EMBED_URL = "http://localhost:8001"
QDRANT_URL = "localhost"
QDRANT_PORT = 6333
COLLECTION = "qfind_docs"
CHUNK_SIZE = 512    # approximate characters per chunk
OVERLAP = 50        # characters of overlap between consecutive chunks

client = QdrantClient(host=QDRANT_URL, port=QDRANT_PORT)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[str]:
    """Fixed-size chunking with overlap. Replace with structure-aware chunking for production."""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        start += size - overlap
    return [c for c in chunks if c.strip()]


def stable_chunk_id(file_path: str, chunk_index: int) -> str:
    """Deterministic ID so re-embedding overwrites, not duplicates."""
    raw = f"{file_path}::chunk::{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()


def embed_batch(texts: list[str]) -> list[dict]:
    """Call TEI /embed and return the list of dense embedding vectors."""
    resp = requests.post(f"{TEI_EMBED_URL}/embed", json={"inputs": texts})
    resp.raise_for_status()
    return resp.json()   # list of lists (one float[] per text)


def index_file(file_path: str):
    text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    chunks = chunk_text(text)
    print(f"  {len(chunks)} chunks from {file_path}")

    BATCH = 16   # TEI handles batches efficiently; avoid one-by-one calls
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        dense_vecs = embed_batch(batch)

        points = []
        for j, (chunk, dense) in enumerate(zip(batch, dense_vecs)):
            chunk_idx = i + j
            cid = stable_chunk_id(file_path, chunk_idx)
            # Convert hex string ID to a numeric ID Qdrant accepts
            numeric_id = int(cid[:16], 16)
            points.append(PointStruct(
                id=numeric_id,
                vector={"dense": dense},    # sparse omitted for now; add in Step 5
                payload={
                    "file_path": file_path,
                    "chunk_index": chunk_idx,
                    "chunk_text": chunk,
                },
            ))

        client.upsert(collection_name=COLLECTION, points=points)
        print(f"    upserted chunks {i}–{i+len(batch)-1}")

    print(f"  Done: {file_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python embed_pipeline.py <file_path>")
        sys.exit(1)
    index_file(sys.argv[1])
```

### 4c. Test it on a sample file

Create a tiny test document:
```bash
cat > /tmp/test_doc.txt << 'EOF'
Qfind is a desktop file search application written in Java.
It uses a local Lucene index to search files quickly.
The AI chat feature lets users ask questions about their documents.
ファイル検索はLuceneを使って高速に動作します。
EOF
```

Run the pipeline:
```bash
python3 deployment/scripts/embed_pipeline.py /tmp/test_doc.txt
```

### Check
```bash
curl -X POST "http://localhost:6333/collections/qfind_docs/points/count" -H "Content-Type: application/json" -d '{}'
```
→ `"count"` is > 0, meaning points (chunk vectors) were inserted.

Also verify idempotency: run the pipeline on the same file again and confirm the count stays the same (upsert, not insert).

> **Why this matters:** the idempotent pipeline is what lets Qfind re-index a changed file without bloating the vector store with duplicates. This is a correctness property, not just a performance one.

---

## 5. Build the hybrid retrieval + reranking pipeline

This is the four-stage pipeline that turns a user's question into a ranked list of relevant text passages:
1. **Lucene BM25** — fast lexical search (existing Qfind capability, unchanged).
2. **Qdrant dense vector search** — semantic similarity.
3. **RRF score fusion** — merge the two ranked lists into one.
4. **Reranker** — precision-refine the merged top-K.

> **Today's scope for the beginner:** we implement stages 2, 3, and 4 as a standalone Python component that can be tested independently. Stage 1 (Lucene) remains in the existing Java code and provides its results as a list of `(file_path, score)` pairs that we read in.

### 5a. Understand RRF in one formula

Given two ranked lists, the RRF score of a document `d` is:
```
RRF(d) = 1/(k + rank_in_list_A(d))  +  1/(k + rank_in_list_B(d))
```
`k = 60` is the standard default. A document ranked 1st in both lists gets the highest combined score. A document appearing in only one list still gets a partial score.

### 5b. Create the retrieval component

Create `deployment/scripts/retrieval_pipeline.py`:

```python
"""
Hybrid retrieval pipeline: Qdrant dense search + Lucene results → RRF → rerank.
Usage: python retrieval_pipeline.py "your query here"
"""
import sys, requests
from qdrant_client import QdrantClient

TEI_EMBED_URL  = "http://localhost:8001"
TEI_RERANK_URL = "http://localhost:8002"
QDRANT_URL     = "localhost"
QDRANT_PORT    = 6333
COLLECTION     = "qfind_docs"
TOP_K_RETRIEVE = 20   # candidates before reranking
TOP_N_RERANK   = 5    # final passages sent to the LLM

client = QdrantClient(host=QDRANT_URL, port=QDRANT_PORT)


def embed_query(query: str) -> list[float]:
    resp = requests.post(f"{TEI_EMBED_URL}/embed", json={"inputs": [query]})
    resp.raise_for_status()
    return resp.json()[0]   # single vector for the query


def qdrant_search(query_vec: list[float], top_k: int) -> list[dict]:
    """Stage 2: dense vector search in Qdrant."""
    response = client.query_points(
        collection_name=COLLECTION,
        query=query_vec,        # plain vector; name is selected via `using`
        using="dense",          # named vector to query against
        limit=top_k,
        with_payload=True,
    )
    return [
        {"id": str(r.id), "score": r.score, "payload": r.payload}
        for r in response.points   # QueryResponse wraps results in .points
    ]


def rrf_fuse(
    lucene_hits: list[dict],   # [{"id": file_path_or_chunk_id, "score": bm25_score}, ...]
    qdrant_hits: list[dict],
    k: int = 60,
) -> list[dict]:
    """Stage 3: Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}

    for rank, hit in enumerate(lucene_hits):
        doc_id = hit["id"]
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        payloads[doc_id] = hit.get("payload", {"chunk_text": hit.get("text", "")})

    for rank, hit in enumerate(qdrant_hits):
        doc_id = hit["id"]
        scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        payloads[doc_id] = hit.get("payload", {})

    fused = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [{"id": doc_id, "score": sc, "payload": payloads[doc_id]}
            for doc_id, sc in fused]


def rerank(query: str, candidates: list[dict], top_n: int) -> list[dict]:
    """Stage 4: cross-encoder reranking via TEI /rerank."""
    texts = [c["payload"].get("chunk_text", "") for c in candidates]
    resp = requests.post(
        f"{TEI_RERANK_URL}/rerank",
        json={"query": query, "texts": texts},
    )
    resp.raise_for_status()
    ranked = sorted(resp.json(), key=lambda x: x["score"], reverse=True)
    return [
        {**candidates[r["index"]], "rerank_score": r["score"]}
        for r in ranked[:top_n]
    ]


def retrieve(query: str, lucene_hits: list[dict] | None = None) -> list[dict]:
    """Full four-stage pipeline. lucene_hits come from the Qfind Java layer."""
    if lucene_hits is None:
        lucene_hits = []   # no Lucene results in standalone test

    query_vec = embed_query(query)
    qdrant_hits = qdrant_search(query_vec, top_k=TOP_K_RETRIEVE)
    fused = rrf_fuse(lucene_hits, qdrant_hits)
    top_candidates = fused[:TOP_K_RETRIEVE]
    reranked = rerank(query, top_candidates, top_n=TOP_N_RERANK)
    return reranked


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python retrieval_pipeline.py 'your query'")
        sys.exit(1)
    query = sys.argv[1]
    results = retrieve(query)
    print(f"\nTop {len(results)} passages for: '{query}'\n")
    for i, r in enumerate(results, 1):
        print(f"  [{i}] score={r['rerank_score']:.4f}  file={r['payload'].get('file_path','?')}")
        print(f"       {r['payload'].get('chunk_text','')[:120]}...")
        print()
```

### 5c. Test the retrieval pipeline

First make sure the test document from Step 4 is indexed. Then:
```bash
python3 deployment/scripts/retrieval_pipeline.py "Lucene index"
python3 deployment/scripts/retrieval_pipeline.py "AI chat document"
python3 deployment/scripts/retrieval_pipeline.py "ファイル検索"
```

### Check
- Each query returns at least one passage with a `rerank_score` and a `file_path`.
- The Japanese query returns the Japanese chunk (`ファイル検索はLuceneを使って...`).
- Running `retrieve()` twice with the same query produces the same order — it's deterministic.

> **Why this matters:** this component is tested *completely independently of the chat UI*. If retrieval quality is wrong later, you know it's this file, not LiteLLM or vLLM. Isolation makes debugging fast.

---

## 6. Rewire the Qfind client

Qfind's AI client code already speaks the OpenAI HTTP protocol (it was calling Jina/Groq, both of which use the same API shape). This step is mostly a **configuration change**, not a code rewrite.

### 6a. What changes in Qfind

| Before | After |
|---|---|
| Base URL: `https://api.groq.com/openai/v1` | Base URL: `http://localhost:4000/v1` |
| Auth header: user's Groq key | Auth header: company virtual key (`sk-...`) |
| Model name for chat: `mixtral-8x7b-...` or similar | Model name: `qfind-chat` |
| Embedding base URL: `https://api.jina.ai/v1` | Embedding base URL: `http://localhost:4000/v1` |
| Embedding model: `jina-embeddings-v2-...` | Embedding model: `qfind-embed` |
| Key entry UI visible to user | Key entry UI removed |

The OpenAI-compatible `/v1/chat/completions`, `/v1/embeddings` paths remain exactly the same. Only the base URL, auth, and model name change.

### 6b. Update Qfind's configuration

Locate the Qfind configuration file (or the constant in code) that holds the API base URL and model names. Change the values as in the table above. Set the virtual key from Step 2 as the auth token.

If Qfind uses a `settings.properties` or similar:
```properties
ai.api.baseUrl=http://localhost:4000/v1
ai.api.key=sk-YOUR-VIRTUAL-KEY-HERE
ai.chat.model=qfind-chat
ai.embed.model=qfind-embed
```

### 6c. Remove the key-entry UI

The screen that asked users to enter their Groq/Jina keys should be removed or hidden — users will no longer manage credentials. The key is embedded in the application config (or distributed at onboarding time by the company).

### 6d. Confirm streaming still renders correctly

Qfind's chat view must render tokens as they arrive (Server-Sent Events). Confirm the client sets `stream: true` in its chat completion request and that the UI renders incrementally. A non-streaming call waits for the full response — users will notice and complain.

### Check
- Open Qfind, trigger a chat request against a local document.
- The chat response appears incrementally (word by word), not all at once.
- No Jina/Groq key prompt appears.
- The model name sent in the request is `qfind-chat` (visible in `docker compose logs litellm`).

---

## 7. End-to-end test with a Japanese document

This is the full pipeline verification:
```
Qfind desktop client
  → LiteLLM (port 4000, virtual key)
    → TEI embed (query embedding)
    → Qdrant (dense search)
    → RRF fusion
    → TEI rerank
    → vLLM (prompt with retrieved context → streamed answer)
```

### 7a. Index a real Japanese document

Create or use a real Japanese-language text file with a few paragraphs about something Qfind-relevant (e.g., a sample invoice, a meeting note, a README). Index it:
```bash
python3 deployment/scripts/embed_pipeline.py /path/to/japanese_doc.txt
```

### 7b. Ask a question about it from Qfind

Open the Qfind client, navigate to the document, and ask a question that requires retrieving a specific passage (not just metadata). For example: a question that can only be answered by reading the document content.

### 7c. Check the answer cites the source file

The design doc §8.6 says: "assemble the prompt with the reranked passages in descending relevance order, each tagged with its source filename, so the LLM's response can (and should be instructed to) cite which file(s) it drew from."

Confirm the answer mentions the source filename or section. If not, check the prompt assembly in Qfind — the context passages must be tagged with their `file_path` from the Qdrant payload.

### Check
- A Japanese-language question produces a coherent streamed answer.
- The answer cites the source document.
- LiteLLM logs show: `POST /v1/chat/completions` → 200, with the `qfind-chat` model.

---

## 8. Local plumbing load test

The 4050 cannot simulate 20–30 concurrent users — that's a production-only validation (Day 5). What we're proving here is that the **gateway handles concurrent requests without erroring**, that it correctly enforces the rate limit, and that VRAM stays stable. Use 3–5 concurrent requests.

### 8a. Create the load test script

Create `deployment/scripts/loadtest.py`:

```python
"""
Local plumbing load test — proves the gateway handles concurrency without errors.
NOT a performance benchmark. Use 3-5 workers locally; 20-30 workers on prod (Day 5).
Usage: python loadtest.py [--workers N] [--requests N]
"""
import argparse, time, concurrent.futures, requests, statistics

LITELLM_URL = "http://localhost:4000/v1/chat/completions"
VKEY = ""   # set via --key or env

PAYLOAD = {
    "model": "qfind-chat",
    "messages": [{"role": "user", "content": "In one sentence, what is Qfind?"}],
    "stream": False,
    "max_tokens": 50,
}

def single_request(key: str) -> tuple[int, float]:
    start = time.monotonic()
    try:
        r = requests.post(LITELLM_URL, json=PAYLOAD,
                          headers={"Authorization": f"Bearer {key}"},
                          timeout=60)
        return r.status_code, time.monotonic() - start
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0, time.monotonic() - start

def run(workers: int, n_requests: int, key: str):
    print(f"\nLoad test: {n_requests} requests, {workers} concurrent workers\n")
    latencies, errors = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(single_request, key) for _ in range(n_requests)]
        for f in concurrent.futures.as_completed(futures):
            status, lat = f.result()
            if status == 200:
                latencies.append(lat)
            else:
                errors += 1
                print(f"  Non-200 status: {status}")

    print(f"\nResults:")
    print(f"  Success: {len(latencies)}/{n_requests}")
    print(f"  Errors:  {errors}")
    if latencies:
        print(f"  Latency  min={min(latencies):.2f}s  median={statistics.median(latencies):.2f}s  max={max(latencies):.2f}s")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--workers",  type=int, default=3)
    p.add_argument("--requests", type=int, default=6)
    p.add_argument("--key",      type=str, required=True)
    args = p.parse_args()
    run(args.workers, args.requests, args.key)
```

### 8b. Run it

```bash
python3 deployment/scripts/loadtest.py \
  --workers 3 --requests 6 \
  --key sk-YOUR-VIRTUAL-KEY-HERE
```

Watch VRAM in another terminal while it runs:
```bash
watch -n 1 nvidia-smi
```

### Check
- All 6 requests return status 200.
- VRAM stays under the cap (`--gpu-memory-utilization 0.80` = ~4 GB on the 4050); no OOM crash.
- `docker compose logs litellm` shows 6 successful routed requests.

Save the output (success count, latency numbers) to a text file — this is your local plumbing baseline. Note: the latency numbers are meaningless for capacity planning; the production load test on the 5090 (Day 5) produces the real figures.

---

---

## End-of-Day 2 Definition of Done

- [ ] `litellm/config.yaml` authored; LiteLLM starts and routes requests to vLLM + TEI through port 4000.
- [ ] Master key in `.env` (not committed); virtual key generated and scoped to `qfind-chat/embed/rerank`.
- [ ] Virtual key passes allowed model requests; fails on disallowed ones.
- [ ] `qdrant/init_collection.py` creates `qfind_docs` collection; Qdrant health check green.
- [ ] `scripts/embed_pipeline.py` indexes a test document; Qdrant point count > 0; idempotent re-run doesn't duplicate.
- [ ] `scripts/retrieval_pipeline.py` returns ranked passages for English and Japanese queries.
- [ ] Qfind client configured to `http://localhost:4000/v1` with the virtual key; streaming works; key-entry UI removed.
- [ ] End-to-end Japanese document chat: streamed answer, source file cited, logs show 200.
- [ ] `scripts/loadtest.py` runs 3 concurrent workers without errors; VRAM stable.
- [ ] `scripts/smoke-test.sh` still passes (both direct and gateway variants).
- [ ] All new files committed to the repo; `git status` clean.

If every box is checked, Phase A (local validation) is complete. Day 3 begins the production cutover.

---

## Common Day-2 problems and fixes

| Symptom | Likely cause | Fix |
|---|---|---|
| LiteLLM crashes at startup with DB error | `DATABASE_URL` wrong, or Postgres isn't ready | verify `POSTGRES_USER/PASSWORD/DB` in `.env` match the postgres service; `docker compose restart litellm` after postgres is healthy |
| `401 Unauthorized` through LiteLLM | wrong key or key not included | include `-H "Authorization: Bearer sk-..."` in every request; confirm the key was generated, not hand-typed |
| `model not found` through LiteLLM | model name mismatch | `model_name:` in `config.yaml` must exactly match what Qfind sends (`qfind-chat`, not `Qwen2.5-14B`) |
| Qdrant point count stays at 0 | embed_pipeline.py called but TEI not reachable | confirm `tei-embed` is running (`docker compose ps`); TEI URL in the script uses `localhost:8001` |
| Retrieval returns nothing | collection empty, or query vector wrong shape | run `init_collection.py` to confirm collection exists; index at least one file first |
| Qfind still calling Groq/Jina | old base URL / model name not updated | check the configuration path described in Step 6; restart Qfind after the config change |
| Streaming doesn't render incrementally | `stream: false` still set, or client not consuming SSE | verify `"stream": true` in the Qfind chat request; check Qfind's HTTP client handles chunked SSE |
| Load test errors on rate limit | `rpm_limit` set too low on the dev key | regenerate the key with a higher `rpm_limit`, or wait for the window to reset |
| LiteLLM embed request fails | TEI `/embed` is at port 8001 but LiteLLM routes to port 80 | inside `qfind-net` the service name is `tei-embed` and TEI listens on port 80 internally; `api_base: http://tei-embed:80` is correct |

---

## Learnings — what Day 2 teaches

Day 2 is the "integration" day. Completing it teaches:

1. **Why a gateway exists.** Without LiteLLM, Qfind would need to manage credentials for each backend, implement budgets, and build rate limiting itself. The gateway is the single point of auth, routing, and usage control — exactly what a company needs when it has both internal and external users on a shared model server.
2. **Virtual keys vs. master key.** The master key creates and revokes; it never leaves the server. Virtual keys are issued per-user or per-application, scoped to allowed models, and can be killed instantly. This hierarchy is standard in API security and directly replaces the "user enters their own Groq key" approach Qfind used before.
3. **What a vector database does (and doesn't do).** Qdrant finds "similar vectors" fast — it is not a full-text search engine. It's better than Lucene for "find passages about the same concept even with different words," and worse for "find the exact phrase invoice number 12345." That's why hybrid search (both) exists.
4. **Idempotency is a correctness property.** The embedding pipeline uses stable chunk IDs (hash of path + index) so re-indexing a changed file overwrites the old chunks exactly. Without this, every re-index duplicates entries and retrieval degrades silently. Designing systems for idempotency from the start is cheaper than fixing the bug later.
5. **Test retrieval quality independently from the UI.** The retrieval pipeline (`retrieval_pipeline.py`) was tested with direct queries before being wired into Qfind. If retrieval is poor, you know where to look. If the full chat is wrong but retrieval is good, you look at prompt assembly. Isolation gives you fast failure attribution.
6. **The LLM is the last piece, not the first.** In a RAG system, everything before the LLM (chunking, embedding, retrieval, reranking) determines whether the LLM can even answer correctly. A perfect LLM with bad retrieval gives bad answers; good retrieval with a mediocre model often works well. The investment in the pipeline is what makes the chat useful.
7. **The local load test proves plumbing, not capacity.** 3 concurrent requests on a 3B model on a 4050 is not a performance benchmark — it's a "does the gateway queue and route correctly under minimal concurrency" check. Accepting that boundary explicitly (instead of pretending it's a capacity test) is an honest engineering habit.
8. **Service names are DNS inside Docker.** Containers find each other by the service name defined in Compose (e.g., `tei-embed`, `litellm-db`). `localhost` inside a container is that container — not the host, not another container. This single rule explains every "connection refused" mystery inside Docker networks.
