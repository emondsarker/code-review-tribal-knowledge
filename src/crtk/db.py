from __future__ import annotations

import json
import logging
import sqlite3
from importlib import resources
from pathlib import Path

from crtk.models import Comment, FetchState, PullRequest, Review

logger = logging.getLogger(__name__)


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create database and initialize schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    schema_sql = resources.files("crtk").joinpath("schema.sql").read_text()
    conn.executescript(schema_sql)
    logger.info("Database initialized at %s", db_path)
    return conn


def upsert_pr(conn: sqlite3.Connection, pr: PullRequest) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO pull_requests
           (id, repo, number, title, user_login, merged_at, created_at, updated_at, html_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pr.id, pr.repo, pr.number, pr.title, pr.user_login,
         pr.merged_at, pr.created_at, pr.updated_at, pr.html_url),
    )


def upsert_review(conn: sqlite3.Connection, review: Review) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO reviews
           (id, repo, pr_number, user_login, state, body, submitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (review.id, review.repo, review.pr_number, review.user_login,
         review.state, review.body, review.submitted_at),
    )


def upsert_comment(conn: sqlite3.Connection, comment: Comment) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO comments
           (id, repo, pr_number, review_id, user_login, body, path, line,
            original_line, side, diff_hunk, commit_id, author_association,
            in_reply_to_id, created_at, updated_at, html_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (comment.id, comment.repo, comment.pr_number, comment.review_id,
         comment.user_login, comment.body, comment.path, comment.line,
         comment.original_line, comment.side, comment.diff_hunk,
         comment.commit_id, comment.author_association, comment.in_reply_to_id,
         comment.created_at, comment.updated_at, comment.html_url),
    )


def get_fetch_state(conn: sqlite3.Connection, repo: str) -> FetchState | None:
    row = conn.execute(
        "SELECT repo, last_fetched_at, last_pr_number FROM fetch_state WHERE repo = ?",
        (repo,),
    ).fetchone()
    if row is None:
        return None
    return FetchState(repo=row["repo"], last_fetched_at=row["last_fetched_at"],
                      last_pr_number=row["last_pr_number"])


def set_fetch_state(conn: sqlite3.Connection, repo: str, last_fetched_at: str,
                    last_pr_number: int) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO fetch_state (repo, last_fetched_at, last_pr_number)
           VALUES (?, ?, ?)""",
        (repo, last_fetched_at, last_pr_number),
    )


def get_comments_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[Comment]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM comments WHERE id IN ({placeholders})", ids
    ).fetchall()
    return [_row_to_comment(r) for r in rows]


def get_all_comment_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT id FROM comments").fetchall()
    return [r["id"] for r in rows]


def get_unembedded_comment_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """SELECT c.id FROM comments c
           LEFT JOIN comment_embeddings ce ON c.id = ce.comment_id
           WHERE ce.comment_id IS NULL"""
    ).fetchall()
    return [r["id"] for r in rows]


def get_untagged_comment_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        """SELECT c.id FROM comments c
           LEFT JOIN comment_tags ct ON c.id = ct.comment_id
           WHERE ct.comment_id IS NULL"""
    ).fetchall()
    return [r["id"] for r in rows]


def get_stats(conn: sqlite3.Connection) -> dict:
    stats = {}
    for table in ["pull_requests", "reviews", "comments", "comment_embeddings", "comment_tags"]:
        row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
        stats[table] = row["cnt"]

    # Per-repo breakdown
    rows = conn.execute(
        "SELECT repo, COUNT(*) as cnt FROM pull_requests GROUP BY repo"
    ).fetchall()
    stats["prs_by_repo"] = {r["repo"]: r["cnt"] for r in rows}

    rows = conn.execute(
        "SELECT repo, COUNT(*) as cnt FROM comments GROUP BY repo"
    ).fetchall()
    stats["comments_by_repo"] = {r["repo"]: r["cnt"] for r in rows}

    # Top reviewers
    rows = conn.execute(
        "SELECT user_login, COUNT(*) as cnt FROM comments GROUP BY user_login ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    stats["top_reviewers"] = {r["user_login"]: r["cnt"] for r in rows}

    # Tag count
    row = conn.execute("SELECT COUNT(*) as cnt FROM tags").fetchone()
    stats["tags"] = row["cnt"]

    # Date range
    row = conn.execute(
        "SELECT MIN(created_at) as oldest, MAX(created_at) as newest FROM comments"
    ).fetchone()
    stats["date_range"] = {"oldest": row["oldest"], "newest": row["newest"]}

    return stats


def ensure_tag(conn: sqlite3.Connection, name: str, description: str | None = None) -> int:
    """Get or create a tag, return its ID."""
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cursor = conn.execute(
        "INSERT INTO tags (name, description) VALUES (?, ?)", (name, description)
    )
    return cursor.lastrowid


def add_comment_tag(conn: sqlite3.Connection, comment_id: int, tag_id: int,
                    confidence: float = 1.0) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO comment_tags (comment_id, tag_id, confidence) VALUES (?, ?, ?)",
        (comment_id, tag_id, confidence),
    )


def get_all_tags_with_counts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT t.name, t.description, COUNT(ct.comment_id) as count
           FROM tags t
           LEFT JOIN comment_tags ct ON t.id = ct.tag_id
           GROUP BY t.id
           ORDER BY count DESC"""
    ).fetchall()
    return [{"name": r["name"], "description": r["description"], "count": r["count"]} for r in rows]


def _row_to_comment(row: sqlite3.Row) -> Comment:
    return Comment(
        id=row["id"], repo=row["repo"], pr_number=row["pr_number"],
        review_id=row["review_id"], user_login=row["user_login"],
        body=row["body"], path=row["path"], line=row["line"],
        original_line=row["original_line"], side=row["side"],
        diff_hunk=row["diff_hunk"], commit_id=row["commit_id"],
        author_association=row["author_association"],
        in_reply_to_id=row["in_reply_to_id"], created_at=row["created_at"],
        updated_at=row["updated_at"], html_url=row["html_url"],
    )
