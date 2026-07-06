### 1. Chat completions (non-stream)
```bash
  curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Authorization: Bearer sk-E66b3oqHUmvcjvtVcBqDNQ" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qfind-chat",
    "messages": [
      {
        "role": "system",
        "content": "You are QuickFind'\''s assistant. Answer using only the provided context."
      },
      {
        "role": "user",
        "content": "Where is the Q3 budget spreadsheet?"
      }
    ],
    "temperature": 0.7,
    "max_tokens": 512,
    "stream": false
  }'
```

### 2. Chat completions (stream)
```bash
 curl https://ubuntu.tailcd8da4.ts.net/v1/chat/completions \
  -H "Authorization: Bearer sk-E66b3oqHUmvcjvtVcBqDNQ" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qfind-chat",
    "messages": [
      {
        "role": "system",
        "content": "You are QuickFind'\''s assistant. Answer using only the provided context."
      },
      {
        "role": "user",
        "content": "Where is the Q3 budget spreadsheet?"
      }
    ],
    "temperature": 0.7,
    "max_tokens": 512,
    "stream": true
  }'
  ```
### 3. Embeddings
```bash
  curl https://ubuntu.tailcd8da4.ts.net/v1/embeddings \
  -H "Authorization: Bearer sk-E66b3oqHUmvcjvtVcBqDNQ" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qfind-embed",
    "input": "Qfind searches your files."
  }'
```
### 4. Rerank
```bash
  curl https://ubuntu.tailcd8da4.ts.net/rerank \
  -H "Authorization: Bearer sk-E66b3oqHUmvcjvtVcBqDNQ" \
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