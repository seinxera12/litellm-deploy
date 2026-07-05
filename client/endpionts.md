### 1. Chat Completions

**Endpoint:** `POST /v1/chat/completions`

Note: your config lists the route as `/chat/completions`, but LiteLLM's actual OpenAI-compatible surface is versioned under `/v1/` — this matches what you already tested successfully earlier in this conversation, so use `/v1/chat/completions`.

bash

```bash
curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Authorization: Bearer <VIRTUAL_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qfind-chat",
    "messages": [
      {"role": "system", "content": "You are QuickFind's assistant. Answer using only the provided context."},
      {"role": "user", "content": "Where is the Q3 budget spreadsheet?"}
    ],
    "temperature": 0.7,
    "max_tokens": 512,
    "stream": false
  }'
```

**Streaming variant** (set `"stream": true`) — client should parse Server-Sent Events, terminating on `data: [DONE]`:

json

```json
{
  "model": "qfind-chat",
  "messages": [...],
  "stream": true
}
```

**Response shape** (standard OpenAI chat completion object):

json

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "qfind-chat",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}
}
```

---

### 2. Embeddings

**Endpoint:** `POST /v1/embeddings`

bash

```bash
curl https://ubuntu.tailcd8da4.ts.net/v1/embeddings \
  -H "Authorization: Bearer <VIRTUAL_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qfind-embed",
    "input": "Qfind searches your files."
  }'
```

**Note the field name:** `input`, singular string or array of strings — not TEI's native `inputs`. LiteLLM translates this to TEI's format internally; the client only ever needs to speak OpenAI's schema.

**Batch embedding** (multiple documents in one call — recommended for ingestion workflows over looping single calls):

json

```json
{
  "model": "qfind-embed",
  "input": ["First document chunk.", "Second document chunk.", "Third document chunk."]
}
```

**Response shape:**

json

```json
{
  "object": "list",
  "data": [
    {"object": "embedding", "index": 0, "embedding": [0.0123, -0.0456, ...]}
  ],
  "model": "qfind-embed",
  "usage": {"prompt_tokens": ..., "total_tokens": ...}
}
```

---

### 3. Rerank

**Endpoint:** `POST /rerank`

Your config uses `huggingface/qfind-rerank` as the underlying model type (not `openai/...` like the other two) — this matters because LiteLLM's rerank route and payload shape follow Cohere's rerank API convention, not the OpenAI chat/embeddings shape.

bash

```bash
curl https://ubuntu.tailcd8da4.ts.net/rerank \
  -H "Authorization: Bearer <VIRTUAL_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qfind-rerank",
    "query": "quarterly budget report",
    "documents": [
      "Q3 budget spreadsheet, finance department.",
      "Company holiday party photos.",
      "Q3 financial summary and forecasts."
    ],
    "top_n": 3
  }'
```

**Response shape:**

json

```json
{
  "results": [
    {"index": 0, "relevance_score": 0.94, "document": {"text": "Q3 budget spreadsheet, finance department."}},
    {"index": 2, "relevance_score": 0.89, "document": {"text": "Q3 financial summary and forecasts."}},
    {"index": 1, "relevance_score": 0.02, "document": {"text": "Company holiday party photos."}}
  ]
}
```

---

### Integration notes for whoever wires up the QuickFind client

- **Never hardcode the virtual key in client-side/frontend code** — it should live in the application's backend config/secrets store, same discipline as any other API credential.
- **One virtual key per environment/consumer** (per the deployment guide's §8.2) — if QuickFind has separate dev/staging/prod instances, request a distinct virtual key for each so usage, budgets, and revocation are meaningful per-consumer, not shared.
- **Rerank's `top_n`** truncates results server-side — if the client needs all documents scored (not just the top N), omit `top_n` or set it to the full document count.