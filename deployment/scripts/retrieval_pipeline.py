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