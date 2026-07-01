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