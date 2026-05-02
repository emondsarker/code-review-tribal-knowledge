from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from crtk.config import CrtkConfig, RepoConfig
from crtk.db import (
    get_fetch_state,
    set_fetch_state,
    upsert_comment,
    upsert_pr,
    upsert_review,
)
from crtk.models import Comment, PullRequest, Review
from crtk.retry import check_rate_limit, gh_api_with_retry, glab_api_with_retry

logger = logging.getLogger(__name__)


class BaseFetcher(ABC):
    def __init__(self, repo_config: RepoConfig, config: CrtkConfig):
        self.repo_config = repo_config
        self.config = config
        self.repo = repo_config.name

    @abstractmethod
    def fetch_prs(self, since: str | None = None) -> list[PullRequest]:
        pass

    @abstractmethod
    def fetch_comments(self, pr_number: int) -> list[Comment]:
        pass

    @abstractmethod
    def fetch_reviews(self, pr_number: int) -> list[Review]:
        pass


class GitHubFetcher(BaseFetcher):
    def fetch_prs(self, since: str | None = None) -> list[PullRequest]:
        endpoint = f"/repos/{self.repo}/pulls?state=closed&sort=updated&direction=desc&per_page={self.config.fetch.page_size}"
        if since:
            endpoint += f"&since={since}"

        logger.info("Fetching PRs from %s (since=%s)", self.repo, since or "all time")
        data = gh_api_with_retry(
            endpoint, paginate=True,
            max_retries=self.config.fetch.max_retries,
            base_delay=self.config.fetch.base_delay_seconds,
            max_delay=self.config.fetch.max_delay_seconds,
        )

        prs = []
        for item in data:
            if not item.get("merged_at"):
                continue
            prs.append(PullRequest(
                id=item["id"],
                repo=self.repo,
                number=item["number"],
                title=item["title"],
                user_login=item["user"]["login"],
                merged_at=item["merged_at"],
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                html_url=item["html_url"],
            ))
        return prs

    def fetch_comments(self, pr_number: int) -> list[Comment]:
        endpoint = f"/repos/{self.repo}/pulls/{pr_number}/comments?per_page=100"
        data = gh_api_with_retry(
            endpoint, paginate=True,
            max_retries=self.config.fetch.max_retries,
            base_delay=self.config.fetch.base_delay_seconds,
            max_delay=self.config.fetch.max_delay_seconds,
        )

        comments = []
        for item in data:
            comments.append(Comment(
                id=item["id"],
                repo=self.repo,
                pr_number=pr_number,
                review_id=item.get("pull_request_review_id"),
                user_login=item["user"]["login"],
                body=item["body"] or "",
                path=item.get("path"),
                line=item.get("line"),
                original_line=item.get("original_line"),
                side=item.get("side"),
                diff_hunk=item.get("diff_hunk"),
                commit_id=item.get("commit_id"),
                author_association=item.get("author_association"),
                in_reply_to_id=item.get("in_reply_to_id"),
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                html_url=item["html_url"],
            ))
        return comments

    def fetch_reviews(self, pr_number: int) -> list[Review]:
        endpoint = f"/repos/{self.repo}/pulls/{pr_number}/reviews?per_page=100"
        data = gh_api_with_retry(
            endpoint, paginate=True,
            max_retries=self.config.fetch.max_retries,
            base_delay=self.config.fetch.base_delay_seconds,
            max_delay=self.config.fetch.max_delay_seconds,
        )

        reviews = []
        for item in data:
            reviews.append(Review(
                id=item["id"],
                repo=self.repo,
                pr_number=pr_number,
                user_login=item["user"]["login"],
                state=item["state"],
                body=item.get("body"),
                submitted_at=item["submitted_at"],
            ))
        return reviews


class GitLabFetcher(BaseFetcher):
    def fetch_prs(self, since: str | None = None) -> list[PullRequest]:
        # GitLab uses updated_after instead of since.
        # Project path must be URL-encoded for API, but glab handles it.
        endpoint = f"/projects/{self.repo}/merge_requests?state=merged&order_by=updated_at&sort=desc&per_page={self.config.fetch.page_size}"
        if since:
            endpoint += f"&updated_after={since}"

        logger.info("Fetching MRs from GL:%s (since=%s)", self.repo, since or "all time")
        data = glab_api_with_retry(
            endpoint, paginate=True,
            max_retries=self.config.fetch.max_retries,
            base_delay=self.config.fetch.base_delay_seconds,
            max_delay=self.config.fetch.max_delay_seconds,
        )

        prs = []
        for item in data:
            if not item.get("merged_at"):
                continue
            prs.append(PullRequest(
                id=item["id"],
                repo=self.repo,
                number=item["iid"],  # GL uses iid for UI/URL
                title=item["title"],
                user_login=item["author"]["username"],
                merged_at=item["merged_at"],
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                html_url=item["web_url"],
            ))
        return prs

    def fetch_comments(self, pr_number: int) -> list[Comment]:
        # GitLab notes = comments. Filter for those with 'position' (diff comments).
        endpoint = f"/projects/{self.repo}/merge_requests/{pr_number}/notes?per_page=100"
        data = glab_api_with_retry(
            endpoint, paginate=True,
            max_retries=self.config.fetch.max_retries,
            base_delay=self.config.fetch.base_delay_seconds,
            max_delay=self.config.fetch.max_delay_seconds,
        )

        comments = []
        for item in data:
            if item.get("system"):  # Skip system notes
                continue
            
            pos = item.get("position") or {}
            comments.append(Comment(
                id=item["id"],
                repo=self.repo,
                pr_number=pr_number,
                review_id=None,  # GL notes not always linked to a 'review' object
                user_login=item["author"]["username"],
                body=item["body"] or "",
                path=pos.get("new_path") or pos.get("old_path"),
                line=pos.get("new_line") or pos.get("old_line"),
                original_line=None,
                side=None,
                diff_hunk=None, # Position has diff hunks but structure is complex
                commit_id=None,
                author_association=None,
                in_reply_to_id=None,
                created_at=item["created_at"],
                updated_at=item["updated_at"],
                html_url=f"{self.repo_config.name}/-/merge_requests/{pr_number}#note_{item['id']}",
            ))
        return comments

    def fetch_reviews(self, pr_number: int) -> list[Review]:
        # GL 13.6+ has reviews API.
        endpoint = f"/projects/{self.repo}/merge_requests/{pr_number}/reviews?per_page=100"
        try:
            data = glab_api_with_retry(
                endpoint, paginate=True,
                max_retries=self.config.fetch.max_retries,
                base_delay=self.config.fetch.base_delay_seconds,
                max_delay=self.config.fetch.max_delay_seconds,
            )
        except Exception:
            logger.warning("Reviews API failed for MR %d, skipping", pr_number)
            return []

        reviews = []
        for item in data:
            reviews.append(Review(
                id=item["id"],
                repo=self.repo,
                pr_number=pr_number,
                user_login=item["author"]["username"],
                state=item.get("state", "COMMENTED"),
                body=None,
                submitted_at=item.get("created_at") or datetime.now(timezone.utc).isoformat(),
            ))
        return reviews


def run_fetch(conn, config: CrtkConfig, full: bool = False,
              repo_filter: str | None = None) -> dict:
    """Run fetch for all configured repos. Returns summary stats."""
    repos = config.repos
    if repo_filter:
        repos = [r for r in repos if repo_filter in r.name]

    if not repos:
        logger.error("No repos configured")
        return {"error": "No repos configured"}

    check_rate_limit()

    total_prs = 0
    total_comments = 0
    total_reviews = 0

    for repo_cfg in repos:
        repo = repo_cfg.name
        logger.info("=== Fetching %s (%s) ===", repo, repo_cfg.platform)

        fetcher = _get_fetcher(repo_cfg, config)
        if not fetcher:
            logger.warning("Unsupported platform: %s", repo_cfg.platform)
            continue

        since = None
        if not full:
            state = get_fetch_state(conn, repo)
            if state:
                since = state.last_fetched_at
                logger.info("Incremental fetch since %s", since)

        prs = fetcher.fetch_prs(since=since)

        max_pr_number = 0
        for pr in prs:
            upsert_pr(conn, pr)
            max_pr_number = max(max_pr_number, pr.number)

            # Fetch comments and reviews for each PR
            time.sleep(config.fetch.inter_request_delay)
            comments = fetcher.fetch_comments(pr.number)
            for c in comments:
                upsert_comment(conn, c)

            time.sleep(config.fetch.inter_request_delay)
            reviews = fetcher.fetch_reviews(pr.number)
            for r in reviews:
                upsert_review(conn, r)

            total_comments += len(comments)
            total_reviews += len(reviews)
            logger.info("  PR #%d: %d comments, %d reviews", pr.number, len(comments), len(reviews))

        total_prs += len(prs)

        # Update fetch state
        now = datetime.now(timezone.utc).isoformat()
        set_fetch_state(conn, repo, now, max_pr_number)
        conn.commit()
        logger.info("Completed %s: %d PRs", repo, len(prs))

    summary = {
        "total_prs": total_prs,
        "total_comments": total_comments,
        "total_reviews": total_reviews,
        "repos_fetched": len(repos),
    }
    logger.info("Fetch complete: %s", summary)
    return summary


def _get_fetcher(repo_cfg: RepoConfig, config: CrtkConfig) -> BaseFetcher | None:
    if repo_cfg.platform == "github":
        return GitHubFetcher(repo_cfg, config)
    if repo_cfg.platform == "gitlab":
        return GitLabFetcher(repo_cfg, config)
    return None
