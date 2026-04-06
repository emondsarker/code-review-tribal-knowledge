from __future__ import annotations

import logging
import os
import sqlite3

import numpy as np

from crtk.config import CrtkConfig
from crtk.db import get_comments_by_ids
from crtk.embeddings import embed_query, load_all_embeddings
from crtk.models import SearchResult

logger = logging.getLogger(__name__)

# Cache for loaded embeddings
_embeddings_cache: tuple[list[int], np.ndarray] | None = None


def hybrid_search(
    conn: sqlite3.Connection,
    config: CrtkConfig,
    file_paths: list[str] | None = None,
    diff_text: str | None = None,
    pr_title: str | None = None,
    commit_messages: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int | None = None,
) -> list[SearchResult]:
    """3-stage hybrid search: tag pre-filter → FTS5 + vector → RRF fusion."""
    limit = limit or config.search.default_limit
    query_text = _build_query_text(file_paths, diff_text, pr_title, commit_messages)

    if not query_text.strip():
        logger.warning("Empty query text, cannot search")
        return []

    logger.info("Searching with query: %s", query_text[:200])

    # Stage 1: Tag pre-filter (get candidate comment IDs)
    candidate_ids = None
    if tags:
        candidate_ids = _get_tagged_comment_ids(conn, tags)
        logger.info("Tag pre-filter: %d candidates from tags %s", len(candidate_ids), tags)
        if not candidate_ids:
            return []

    # Stage 2a: FTS5 search
    fts_results = _search_fts(conn, query_text, candidate_ids, limit=limit * 3)

    # Stage 2b: Vector search
    vec_results = _search_vector(conn, config, query_text, candidate_ids, limit=limit * 3)

    # Stage 3: Reciprocal Rank Fusion
    fused = _reciprocal_rank_fusion(
        fts_results, vec_results,
        fts_weight=config.search.fts_weight,
        vec_weight=config.search.vector_weight,
    )

    # Take top N and hydrate
    top_ids = [cid for cid, _ in fused[:limit]]
    comments = get_comments_by_ids(conn, top_ids)
    comment_map = {c.id: c for c in comments}

    # Get tags for results
    tag_map = _get_tags_for_comments(conn, top_ids)

    results = []
    for cid, score in fused[:limit]:
        if cid in comment_map:
            results.append(SearchResult(
                comment=comment_map[cid],
                score=score,
                match_source="hybrid",
                tags=tag_map.get(cid, []),
            ))

    logger.info("Search returned %d results", len(results))
    return results


def _build_query_text(
    file_paths: list[str] | None,
    diff_text: str | None,
    pr_title: str | None,
    commit_messages: list[str] | None,
) -> str:
    parts = []
    if pr_title:
        parts.append(pr_title)
    if file_paths:
        for fp in file_paths:
            parts.append(fp)
            dirname = os.path.dirname(fp)
            if dirname:
                parts.append(dirname)
    if diff_text:
        parts.append(diff_text[:4000])
    if commit_messages:
        parts.extend(commit_messages[:5])
    return "\n".join(parts)


def _get_tagged_comment_ids(conn: sqlite3.Connection, tags: list[str]) -> set[int]:
    """Get comment IDs matching any of the given tags."""
    placeholders = ",".join("?" for _ in tags)
    rows = conn.execute(
        f"""SELECT DISTINCT ct.comment_id
            FROM comment_tags ct
            JOIN tags t ON ct.tag_id = t.id
            WHERE t.name IN ({placeholders})""",
        tags,
    ).fetchall()
    return {r["comment_id"] for r in rows}


def _search_fts(conn: sqlite3.Connection, query_text: str,
                candidate_ids: set[int] | None, limit: int = 50) -> list[tuple[int, float]]:
    """FTS5 full-text search. Returns (comment_id, bm25_rank) pairs."""
    # Build FTS query from significant words
    words = _extract_search_terms(query_text)
    if not words:
        return []

    fts_query = " OR ".join(words[:20])  # FTS5 query

    try:
        if candidate_ids is not None:
            # Filter to candidates
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                f"""SELECT rowid, rank FROM comments_fts
                    WHERE comments_fts MATCH ? AND rowid IN ({placeholders})
                    ORDER BY rank
                    LIMIT ?""",
                [fts_query, *candidate_ids, limit],
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT rowid, rank FROM comments_fts
                   WHERE comments_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
    except Exception as e:
        logger.warning("FTS search failed: %s", e)
        return []

    return [(r["rowid"], -r["rank"]) for r in rows]  # rank is negative in FTS5


def _search_vector(conn: sqlite3.Connection, config: CrtkConfig,
                   query_text: str, candidate_ids: set[int] | None,
                   limit: int = 50) -> list[tuple[int, float]]:
    """Vector similarity search. Returns (comment_id, cosine_score) pairs."""
    global _embeddings_cache

    if _embeddings_cache is None:
        ids, matrix = load_all_embeddings(conn)
        if len(ids) == 0:
            logger.warning("No embeddings in DB, skipping vector search")
            return []
        _embeddings_cache = (ids, matrix)

    all_ids, all_embeddings = _embeddings_cache

    if len(all_ids) == 0:
        return []

    # Embed query
    query_embedding = embed_query(query_text, config.embeddings.model)

    # Filter to candidates if needed
    if candidate_ids is not None:
        mask = [i for i, cid in enumerate(all_ids) if cid in candidate_ids]
        if not mask:
            return []
        filtered_ids = [all_ids[i] for i in mask]
        filtered_embeddings = all_embeddings[mask]
    else:
        filtered_ids = all_ids
        filtered_embeddings = all_embeddings

    # Cosine similarity (embeddings are already normalized)
    scores = filtered_embeddings @ query_embedding

    # Get top results
    top_indices = np.argsort(scores)[::-1][:limit]
    return [(filtered_ids[i], float(scores[i])) for i in top_indices]


def _reciprocal_rank_fusion(
    fts_results: list[tuple[int, float]],
    vec_results: list[tuple[int, float]],
    fts_weight: float = 0.4,
    vec_weight: float = 0.6,
    k: int = 60,
) -> list[tuple[int, float]]:
    """Merge two ranked lists using Reciprocal Rank Fusion."""
    scores: dict[int, float] = {}

    for rank, (cid, _) in enumerate(fts_results):
        scores[cid] = scores.get(cid, 0) + fts_weight / (k + rank + 1)

    for rank, (cid, _) in enumerate(vec_results):
        scores[cid] = scores.get(cid, 0) + vec_weight / (k + rank + 1)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _get_tags_for_comments(conn: sqlite3.Connection, comment_ids: list[int]) -> dict[int, list[str]]:
    """Get tags for a list of comments."""
    if not comment_ids:
        return {}
    placeholders = ",".join("?" for _ in comment_ids)
    rows = conn.execute(
        f"""SELECT ct.comment_id, t.name
            FROM comment_tags ct
            JOIN tags t ON ct.tag_id = t.id
            WHERE ct.comment_id IN ({placeholders})""",
        comment_ids,
    ).fetchall()

    result: dict[int, list[str]] = {}
    for r in rows:
        result.setdefault(r["comment_id"], []).append(r["name"])
    return result


def _extract_search_terms(text: str) -> list[str]:
    """Extract significant search terms from query text, filtering out noise."""
    import re
    # Split on non-alphanumeric, filter short/common words
    words = re.findall(r'\b[a-zA-Z]{3,}\b', text)
    stopwords = {
        "the", "and", "for", "this", "that", "with", "from", "are", "was", "were",
        "have", "has", "been", "will", "can", "not", "but", "all", "any", "each",
        "src", "dist", "node_modules", "import", "export", "const", "let", "var",
        "function", "return", "class", "new", "null", "undefined", "true", "false",
    }
    seen = set()
    terms = []
    for w in words:
        low = w.lower()
        if low not in stopwords and low not in seen:
            seen.add(low)
            terms.append(low)
    return terms


def invalidate_cache() -> None:
    """Clear the embeddings cache (call after new embeddings are stored)."""
    global _embeddings_cache
    _embeddings_cache = None
