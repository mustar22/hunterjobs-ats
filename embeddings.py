"""
embeddings.py

Semantic embeddings for the RAG "similar past applications" feature.

Embeds each job's title/company/description with Gemini gemini-embedding-001
(768-dim, output_dimensionality=768) via the same google-genai SDK + keys.py auth path the brains use,
stores the vector in the sqlite-vec `job_embeddings` table, and ranks applied
jobs by cosine similarity for the dashboard's "Similar past applications" panel.

Everything here degrades gracefully: if the sqlite-vec extension didn't load
(database.RAG_AVAILABLE is False) or an embedding call fails, functions log and
return empty/None rather than raising, so a failed embed never breaks a scrape
or the UI.
"""

from __future__ import annotations

import json
import logging
import math

import database
from database import get_db_connection

log = logging.getLogger(__name__)

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768


# ── keys (same path as the brains) ──────────────────────────────────────────────
def load_keys() -> dict:
    try:
        import keys
        return {"google": getattr(keys, "GOOGLE_API_KEY", "")}
    except ImportError:
        return {"google": ""}


# ── Gemini embedding calls ──────────────────────────────────────────────────────
def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch with gemini-embedding-001 at 768 dims; None per slot on failure, never raises."""
    if not texts:
        return []
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=load_keys()["google"])
        resp = client.models.embed_content(
            model=EMBED_MODEL,
            contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
        )
        return [list(e.values) for e in resp.embeddings]
    except Exception as e:
        log.warning("Embedding batch of %d failed: %s", len(texts), e)
        return [None] * len(texts)


def embed_text(text: str) -> list[float] | None:
    """Embed a single text. Returns the 768-float vector or None on failure."""
    return embed_texts([text])[0]


# ── what we embed ───────────────────────────────────────────────────────────────
def build_embedding_text(job: dict) -> str:
    """Job → "title — company\\ndescription[:2000]" for embedding."""
    title = (job.get("title") or "").strip()
    company = (job.get("company") or "").strip()
    description = (job.get("description") or "").strip()
    return f"{title} — {company}\n{description[:2000]}"


def embed_job(job: dict) -> list[float] | None:
    return embed_text(build_embedding_text(job))


# ── storage ─────────────────────────────────────────────────────────────────────
def store_embedding(conn, job_id: str, vector: list[float]) -> None:
    """Persist one job's vector into the vec0 table; no-op if RAG is disabled."""
    if not database.RAG_AVAILABLE or vector is None:
        return
    import sqlite_vec

    conn.execute(
        "INSERT OR REPLACE INTO job_embeddings (job_id, embedding) VALUES (?, ?)",
        (job_id, sqlite_vec.serialize_float32(list(vector))),
    )
    conn.commit()


def get_embedding(conn, job_id: str) -> list[float] | None:
    """Read one job's stored vector back as a list, or None if absent."""
    if not database.RAG_AVAILABLE:
        return None
    row = conn.execute(
        "SELECT vec_to_json(embedding) AS emb FROM job_embeddings WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if not row or row["emb"] is None:
        return None
    try:
        return json.loads(row["emb"])
    except (json.JSONDecodeError, TypeError):
        return None


def embed_and_store(conn, job: dict) -> bool:
    """Embed and store one job; best-effort, never raises (a failed embed must not fail a scrape)."""
    if not database.RAG_AVAILABLE:
        return False
    try:
        vector = embed_job(job)
        if vector is None:
            return False
        store_embedding(conn, job["id"], vector)
        return True
    except Exception as e:
        log.warning("embed_and_store failed for %s: %s", job.get("id"), e)
        return False


# ── similarity ──────────────────────────────────────────────────────────────────
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity; 0.0 if either is zero-magnitude or lengths differ."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_by_similarity(
    query: list[float], candidates: list[dict], top_k: int = 3
) -> list[dict]:
    """Top_k candidates by cosine similarity to query, highest score first."""
    if not query:
        return []
    scored = []
    for c in candidates:
        emb = c.get("embedding")
        if not emb:
            continue
        scored.append(
            {
                "id": c.get("id"),
                "title": c.get("title"),
                "company": c.get("company"),
                "score": cosine_similarity(query, emb),
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def find_similar_applications(conn, job_id: str, top_k: int = 3) -> list[dict]:
    """Top_k applied jobs most similar to job_id (self excluded); [] if none."""
    if not database.RAG_AVAILABLE:
        return []
    query = get_embedding(conn, job_id)
    if query is None:
        return []
    rows = conn.execute(
        """
        SELECT j.id, j.title, j.company, vec_to_json(e.embedding) AS emb
        FROM jobs j
        JOIN job_embeddings e ON j.id = e.job_id
        WHERE j.applied = 1 AND j.id != ?
        """,
        (job_id,),
    ).fetchall()
    candidates: list[dict] = []
    for r in rows:
        try:
            emb = json.loads(r["emb"]) if r["emb"] else None
        except (json.JSONDecodeError, TypeError):
            emb = None
        if emb:
            candidates.append(
                {"id": r["id"], "title": r["title"], "company": r["company"], "embedding": emb}
            )
    return rank_by_similarity(query, candidates, top_k=top_k)


# ── backfill ────────────────────────────────────────────────────────────────────
def backfill_embeddings(progress=None, batch_size: int = 50) -> tuple[int, int]:
    """Embed all not-yet-embedded jobs in batches (idempotent); returns (embedded, total)."""
    if not database.RAG_AVAILABLE:
        return (0, 0)

    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, title, company, description
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM job_embeddings)
            """
        ).fetchall()
        total = len(rows)
        embedded = 0
        if progress:
            progress(0, total)
        for start in range(0, total, batch_size):
            batch = rows[start : start + batch_size]
            texts = [build_embedding_text(dict(r)) for r in batch]
            vectors = embed_texts(texts)
            for r, vec in zip(batch, vectors):
                if vec is not None:
                    try:
                        store_embedding(conn, r["id"], vec)
                        embedded += 1
                    except Exception as e:
                        log.warning("backfill store failed for %s: %s", r["id"], e)
            if progress:
                progress(min(start + batch_size, total), total)
        return (embedded, total)
    finally:
        conn.close()
