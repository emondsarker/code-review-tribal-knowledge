from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PullRequest:
    id: int
    repo: str
    number: int
    title: str
    user_login: str
    merged_at: str | None
    created_at: str
    updated_at: str
    html_url: str


@dataclass
class Review:
    id: int
    repo: str
    pr_number: int
    user_login: str
    state: str
    body: str | None
    submitted_at: str


@dataclass
class Comment:
    id: int
    repo: str
    pr_number: int
    review_id: int | None
    user_login: str
    body: str
    path: str | None
    line: int | None
    original_line: int | None
    side: str | None
    diff_hunk: str | None
    commit_id: str | None
    author_association: str | None
    in_reply_to_id: int | None
    created_at: str
    updated_at: str
    html_url: str


@dataclass
class FetchState:
    repo: str
    last_fetched_at: str
    last_pr_number: int = 0


@dataclass
class SearchResult:
    comment: Comment
    score: float
    match_source: str  # "fts", "vector", or "hybrid"
    tags: list[str] = field(default_factory=list)


@dataclass
class Convention:
    id: int | None
    title: str
    body: str
    category: str | None
    file_patterns: list[str] | None
    source_comment_ids: list[int]
    confidence: float | None
    created_at: str
    updated_at: str
