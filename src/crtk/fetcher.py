from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from crtk.config import CrtkConfig
from crtk.db import (
    get_fetch_state,
    set_fetch_state,
    upsert_comment,
    upsert_pr,
    upsert_review,
)
from crtk.models import Comment, PullRequest, Review
from crtk.retry import check_rate_limit, gh_api_with_retry

logger = logging.getLogger(__name__)


def fetch_merged_prs(repo: str, since: str | None = None,
                     config: CrtkConfig | None = None) -> list[PullRequest]:
    """Fetch merged PRs from a repo. If since is provided, only fetch PRs updated after that date."""
    cfg = config or CrtkConfig()
    endpoint = f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={cfg.fetch.page_size}"
    if since:
        endpoint += f"&since={since}"

    logger.info("Fetching PRs from %s (since=%s)", repo, since or "all time")
    data = gh_api_with_retry(
        endpoint, paginate=True,
        max_retries=cfg.fetch.max_retries,
        base_delay=cfg.fetch.base_delay_seconds,
        max_delay=cfg.fetch.max_delay_seconds,
    )

    prs = []
    for item in data:
        if not item.get("merged_at"):
            continue
        prs.append(PullRequest(
            id=item["id"],
            repo=repo,
            number=item["number"],
            title=item["title"],
            user_login=item["user"]["login"],
            merged_at=item["merged_at"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            html_url=item["html_url"],
        ))

    logger.info("Found %d merged PRs from %s", len(prs), repo)
    return prs


def fetch_pr_comments(repo: str, pr_number: int,
                      config: CrtkConfig | None = None) -> list[Comment]:
    """Fetch inline review comments for a PR."""
    cfg = config or CrtkConfig()
    endpoint = f"/repos/{repo}/pulls/{pr_number}/comments?per_page=100"

    data = gh_api_with_retry(
        endpoint, paginate=True,
        max_retries=cfg.fetch.max_retries,
        base_delay=cfg.fetch.base_delay_seconds,
        max_delay=cfg.fetch.max_delay_seconds,
    )

    comments = []
    for item in data:
        comments.append(Comment(
            id=item["id"],
            repo=repo,
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


def fetch_pr_reviews(repo: str, pr_number: int,
                     config: CrtkConfig | None = None) -> list[Review]:
    """Fetch review submissions for a PR."""
    cfg = config or CrtkConfig()
    endpoint = f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100"

    data = gh_api_with_retry(
        endpoint, paginate=True,
        max_retries=cfg.fetch.max_retries,
        base_delay=cfg.fetch.base_delay_seconds,
        max_delay=cfg.fetch.max_delay_seconds,
    )

    reviews = []
    for item in data:
        reviews.append(Review(
            id=item["id"],
            repo=repo,
            pr_number=pr_number,
            user_login=item["user"]["login"],
            state=item["state"],
            body=item.get("body"),
            submitted_at=item["submitted_at"],
        ))

    return reviews


def run_fetch(conn, config: CrtkConfig, full: bool = False,
              repo_filter: str | None = None) -> dict:
    """Run fetch for all configured repos. Returns summary stats."""
    repos = config.repos
    if repo_filter:
        repos = [r for r in repos if repo_filter in r]

    if not repos:
        logger.error("No repos configured")
        return {"error": "No repos configured"}

    check_rate_limit()

    total_prs = 0
    total_comments = 0
    total_reviews = 0

    for repo in repos:
        logger.info("=== Fetching %s ===", repo)

        since = None
        if not full:
            state = get_fetch_state(conn, repo)
            if state:
                since = state.last_fetched_at
                logger.info("Incremental fetch since %s", since)

        prs = fetch_merged_prs(repo, since=since, config=config)

        max_pr_number = 0
        for pr in prs:
            upsert_pr(conn, pr)
            max_pr_number = max(max_pr_number, pr.number)

            # Fetch comments and reviews for each PR
            time.sleep(config.fetch.inter_request_delay)
            comments = fetch_pr_comments(repo, pr.number, config=config)
            for c in comments:
                upsert_comment(conn, c)

            time.sleep(config.fetch.inter_request_delay)
            reviews = fetch_pr_reviews(repo, pr.number, config=config)
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
