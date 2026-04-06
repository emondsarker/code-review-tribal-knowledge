from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import numpy as np

# Suppress noisy progress bars and warnings from transformers/torch
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")

from crtk.config import CrtkConfig
from crtk.db import get_comments_by_ids

logger = logging.getLogger(__name__)

_model = None
_model_name = None


def _get_model(model_name: str):
    """Lazy-load the embedding model."""
    global _model, _model_name
    if _model is not None and _model_name == model_name:
        return _model

    logger.info("Loading embedding model: %s", model_name)

    import io
    import sys
    import warnings

    from sentence_transformers import SentenceTransformer

    # Suppress progress bars, load reports, and C-level output from torch
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stdout_fd = os.dup(1)
    old_stderr_fd = os.dup(2)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _model = SentenceTransformer(model_name)
    finally:
        os.dup2(old_stdout_fd, 1)
        os.dup2(old_stderr_fd, 2)
        os.close(devnull)
        os.close(old_stdout_fd)
        os.close(old_stderr_fd)

    _model_name = model_name
    dim = _model.get_sentence_embedding_dimension()
    logger.info("Model ready (%s, %dd)", model_name, dim)
    return _model


def embed_texts(texts: list[str], model_name: str) -> np.ndarray:
    """Embed a batch of texts. Returns (N, dim) float32 array."""
    model = _get_model(model_name)
    embeddings = model.encode(texts, show_progress_bar=False,
                              batch_size=32, normalize_embeddings=True)
    return np.asarray(embeddings, dtype=np.float32)


def embed_query(text: str, model_name: str) -> np.ndarray:
    """Embed a single query text. Returns (dim,) float32 array."""
    model = _get_model(model_name)
    embedding = model.encode(text, normalize_embeddings=True)
    return np.asarray(embedding, dtype=np.float32)


def _build_embed_text(comment) -> str:
    """Build the text to embed for a comment: body + file path + diff context."""
    parts = []
    if comment.path:
        parts.append(f"File: {comment.path}")
    parts.append(comment.body)
    if comment.diff_hunk:
        parts.append(f"Code context:\n{comment.diff_hunk}")
    return "\n".join(parts)


def embed_and_store(conn, comment_ids: list[int], config: CrtkConfig,
                    batch_size: int = 64) -> int:
    """Generate embeddings for comments and store in DB. Returns count embedded."""
    model_name = config.embeddings.model
    now = datetime.now(timezone.utc).isoformat()
    total = 0

    for i in range(0, len(comment_ids), batch_size):
        batch_ids = comment_ids[i:i + batch_size]
        comments = get_comments_by_ids(conn, batch_ids)

        if not comments:
            continue

        texts = [_build_embed_text(c) for c in comments]
        embeddings = embed_texts(texts, model_name)

        for comment, embedding in zip(comments, embeddings):
            conn.execute(
                "INSERT OR REPLACE INTO comment_embeddings (comment_id, embedding, model, created_at) "
                "VALUES (?, ?, ?, ?)",
                (comment.id, embedding.tobytes(), model_name, now),
            )

        conn.commit()
        total += len(comments)
        logger.info("Embedded %d/%d comments", total, len(comment_ids))

    return total


def load_all_embeddings(conn) -> tuple[list[int], np.ndarray]:
    """Load all embeddings from DB. Returns (comment_ids, embeddings_matrix)."""
    rows = conn.execute(
        "SELECT comment_id, embedding FROM comment_embeddings"
    ).fetchall()

    if not rows:
        return [], np.array([], dtype=np.float32)

    ids = [r["comment_id"] for r in rows]
    # Determine dimension from first embedding
    first = np.frombuffer(rows[0]["embedding"], dtype=np.float32)
    dim = len(first)

    matrix = np.zeros((len(rows), dim), dtype=np.float32)
    matrix[0] = first
    for i, r in enumerate(rows[1:], 1):
        matrix[i] = np.frombuffer(r["embedding"], dtype=np.float32)

    return ids, matrix
