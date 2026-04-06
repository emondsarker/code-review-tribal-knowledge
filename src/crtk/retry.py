from __future__ import annotations

import json
import logging
import random
import subprocess
import time

logger = logging.getLogger(__name__)


class GhApiError(Exception):
    def __init__(self, returncode: int, stderr: str, is_rate_limit: bool = False,
                 reset_at: float | None = None):
        self.returncode = returncode
        self.stderr = stderr
        self.is_rate_limit = is_rate_limit
        self.reset_at = reset_at
        super().__init__(f"gh api error (rc={returncode}): {stderr[:200]}")


def gh_api(endpoint: str, paginate: bool = False, method: str = "GET") -> list | dict:
    """Call gh api and return parsed JSON."""
    cmd = ["gh", "api", endpoint, "--method", method]
    if paginate:
        cmd.append("--paginate")

    logger.debug("gh api call: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        is_rate = "rate limit" in stderr.lower() or "403" in stderr
        raise GhApiError(result.returncode, stderr, is_rate_limit=is_rate)

    if not result.stdout.strip():
        return []

    # gh --paginate concatenates JSON arrays, producing invalid JSON.
    # It outputs each page's array on its own; we need to handle that.
    text = result.stdout.strip()
    if paginate and text.startswith("["):
        # Multiple arrays concatenated: "][" or "]\n["
        text = text.replace("]\n[", ",").replace("][", ",")

    return json.loads(text)


def gh_api_with_retry(endpoint: str, paginate: bool = False,
                      max_retries: int = 5, base_delay: float = 1.0,
                      max_delay: float = 60.0) -> list | dict:
    """Call gh_api with exponential backoff retry."""
    for attempt in range(max_retries + 1):
        try:
            return gh_api(endpoint, paginate=paginate)
        except GhApiError as e:
            if attempt == max_retries:
                logger.error("Max retries reached for %s", endpoint)
                raise

            if e.is_rate_limit:
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, base_delay)
                logger.warning(
                    "Rate limited on %s (attempt %d/%d). Retrying in %.1fs",
                    endpoint, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
            elif any(s in e.stderr.lower() for s in ["timeout", "connection", "network"]):
                delay = min(base_delay * (2 ** attempt), max_delay)
                delay += random.uniform(0, base_delay)
                logger.warning(
                    "Transient error on %s (attempt %d/%d). Retrying in %.1fs",
                    endpoint, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
            else:
                raise  # Non-retryable
        except subprocess.TimeoutExpired:
            if attempt == max_retries:
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning("Timeout on %s (attempt %d/%d). Retrying in %.1fs",
                           endpoint, attempt + 1, max_retries, delay)
            time.sleep(delay)

    return []  # unreachable, but satisfies type checker


def check_rate_limit() -> dict:
    """Check current GitHub API rate limit status."""
    data = gh_api("/rate_limit")
    core = data.get("rate", {})
    remaining = core.get("remaining", 0)
    reset_at = core.get("reset", 0)
    logger.info("Rate limit: %d remaining, resets at %s", remaining, reset_at)

    if remaining < 100:
        wait = max(reset_at - time.time(), 1)
        logger.warning("Low rate limit (%d remaining). Waiting %.0fs.", remaining, wait)
        time.sleep(wait)

    return core
