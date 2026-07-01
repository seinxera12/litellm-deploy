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