"""
rag.py — Core RAG logic.
Embeds a question, retrieves relevant chunks from Qdrant,
feeds them to gemma4:e4b for a fast, grounded answer.
"""

import os
import logging
from datetime import datetime

import ollama
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

log = logging.getLogger(__name__)

QDRANT_HOST   = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT   = int(os.getenv("QDRANT_PORT", 6333))
OLLAMA_HOST   = os.getenv("OLLAMA_HOST", "localhost")
OLLAMA_PORT   = int(os.getenv("OLLAMA_PORT", 11434))
COLLECTION    = "blackbox"
EMBED_MODEL   = "nomic-embed-text"
LLM_MODEL   = "gemma4:e4b"
TOP_K         = 10

qdrant        = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
ollama_client = ollama.Client(host=f"http://{OLLAMA_HOST}:{OLLAMA_PORT}")


def embed(text: str) -> list[float]:
    response = ollama_client.embeddings(model=EMBED_MODEL, prompt=text)
    return response["embedding"]


def search(question: str, top_k: int = TOP_K, date_filter: str | None = None) -> list[dict]:
    """
    Search Qdrant for chunks relevant to `question`.
    Optionally filter by exact date string (YYYY-MM-DD).
    """
    vector = embed(question)

    query_filter = None
    if date_filter:
        query_filter = Filter(
            must=[FieldCondition(key="date", match=MatchValue(value=date_filter))]
        )

    results = qdrant.search(
        collection_name=COLLECTION,
        query_vector=vector,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    return [r.payload for r in results]


def answer(question: str, date_filter: str | None = None) -> dict:
    """
    Full RAG pipeline: retrieve relevant chunks then answer with Gemma.
    Returns {answer, sources, retrieved_chunks}.
    """
    chunks = search(question, date_filter=date_filter)

    if not chunks:
        return {
            "answer": "I don't have any data relevant to that question yet. Make sure the ETL has run at least once.",
            "sources": [],
            "retrieved_chunks": [],
        }

    # Build context block
    context_parts = []
    for c in chunks:
        context_parts.append(
            f"[{c.get('window_start', 'unknown time')}] "
            f"Source: {c.get('source', 'unknown')} | "
            f"Apps: {', '.join(c.get('apps', []))} | "
            f"{c.get('text', '')}"
        )
    context = "\n\n".join(context_parts)

    prompt = f"""You are a personal life assistant with access to logged data about the user's daily activity.
Answer the user's question using ONLY the context provided below. Be specific and cite timestamps when relevant.
If the context doesn't contain enough information to answer fully, say so honestly.

Context from activity logs:
{context}

User question: {question}

Answer:"""

    response = ollama_client.chat(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}]
    )

    return {
        "answer": response["message"]["content"],
        "sources": list({c.get("source") for c in chunks}),
        "retrieved_chunks": chunks,
    }
