#!/usr/bin/env python3
"""
crtk-collect — standalone PR review collector.

Hand this file to a dev. They run it, paste GitHub repo URLs (one per line,
blank line to finish), and it discovers every PR they authored in each repo
with their active `gh` account, then writes a sqlite db containing each PR's
metadata, review comments, review submissions, and full unified diff.

Requires: Python 3.9+, `gh` CLI authenticated (`gh auth login`).
No other dependencies.

Usage:
    python3 crtk-collect.py [--db path.db] [--login GH_LOGIN]
                            [--all-authors] [--state all|closed|open]
                            [--repos-file repos.txt]

Output: crtk-collect-YYYYMMDD-HHMMSS.db in the current directory by default.
Share that file back to whoever asked you to run this.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

MAX_RETRIES = 5
BASE_DELAY = 2.0   # seconds
MAX_DELAY = 60.0   # cap per retry sleep

# Substrings that indicate a transient failure worth retrying.
_TRANSIENT_MARKERS = (
    "rate limit", "secondary rate", "abuse",
    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
    "Connection reset", "Connection refused", "timeout", "timed out",
    "EOF", "temporary failure",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL,
    number          INTEGER NOT NULL,
    title           TEXT NOT NULL,
    user_login      TEXT NOT NULL,
    merged_at       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    html_url        TEXT NOT NULL,
    state           TEXT,
    base_ref        TEXT,
    head_ref        TEXT,
    fetched_by      TEXT,
    fetched_at      TEXT NOT NULL,
    UNIQUE(repo, number)
);

CREATE TABLE IF NOT EXISTS reviews (
    id              TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    user_login      TEXT NOT NULL,
    state           TEXT NOT NULL,
    body            TEXT,
    submitted_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comments (
    id                      INTEGER PRIMARY KEY,
    repo                    TEXT NOT NULL,
    pr_number               INTEGER NOT NULL,
    review_id               INTEGER,
    user_login              TEXT NOT NULL,
    body                    TEXT NOT NULL,
    path                    TEXT,
    line                    INTEGER,
    original_line           INTEGER,
    side                    TEXT,
    diff_hunk               TEXT,
    commit_id               TEXT,
    author_association      TEXT,
    in_reply_to_id          INTEGER,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    html_url                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pr_diffs (
    repo            TEXT NOT NULL,
    pr_number       INTEGER NOT NULL,
    diff            TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number)
);

CREATE INDEX IF NOT EXISTS idx_comments_pr ON comments(repo, pr_number);
CREATE INDEX IF NOT EXISTS idx_reviews_pr ON reviews(repo, pr_number);
"""

REPO_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)
GLAB_URL_RE = re.compile(
    r"gitlab\.com/(.+)/([^/]+?)(?:\.git)?/?$"
)
# Also accept bare "owner/repo" shorthand.
REPO_SHORT_RE = re.compile(r"^(.+)/([^/]+?)(?:\.git)?$")


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def print_banner() -> None:
    print("=" * 60)
    print("  crtk-collect — PR review collector")
    print("=" * 60)
    print("This script will:")
    print("  1. Check for `gh` or `glab` CLI authentication.")
    print("  2. Ask you for the GitHub/GitLab repos you've worked on.")
    print("  3. Find every PR/MR you authored in those repos.")
    print("  4. Save each PR's metadata, review comments, reviewer")
    print("     submissions, and full diff into a single .db file.")
    print()
    print("When it's done, send the .db file to whoever asked you to")
    print("run this. That's it — no further action needed.")
    print("=" * 60)
    print()


def list_gh_accounts() -> list[str]:
    """Parse `gh auth status` for all authenticated logins."""
    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    text = (result.stdout or "") + (result.stderr or "")
    # Lines look like: "  ✓ Logged in to github.com account <login> (...)"
    return re.findall(r"Logged in to \S+ account (\S+)", text)


def check_gh() -> str:
    """Verify gh CLI is installed and authenticated. Return active login."""
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        die("`gh` CLI not found.\n"
            "  Install: https://cli.github.com/\n"
            "  Then run: gh auth login")

    result = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        die("gh is not authenticated.\n  Run: gh auth login")

    who = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True,
    )
    if who.returncode != 0:
        die("could not determine active gh account. Try: gh auth status")
    login = who.stdout.strip()

    accounts = list_gh_accounts()
    print(f"gh CLI: authenticated as '{login}' (active)")
    if len(accounts) > 1:
        others = [a for a in accounts if a != login]
        print(f"  other accounts available: {', '.join(others)}")
        print(f"  (switch with: gh auth switch -u <login>)")
    print()
    return login


def check_glab() -> str:
    """Verify glab CLI is installed and authenticated. Return active login."""
    try:
        subprocess.run(["glab", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        die("`glab` CLI not found.\n"
            "  Install: https://glab.readthedocs.io/\n"
            "  Then run: glab auth login")

    # Force gitlab.com hostname to avoid confusion in GitHub repos
    who = subprocess.run(
        ["glab", "api", "user", "--hostname", "gitlab.com"],
        capture_output=True, text=True,
    )
    if who.returncode != 0:
        die("glab is not authenticated.\n  Run: glab auth login")
    
    try:
        data = json.loads(who.stdout)
        login = data["username"]
    except (json.JSONDecodeError, KeyError):
        die("could not parse glab user response. Try: glab auth status")

    print(f"glab CLI: authenticated as '{login}' (active)\n")
    return login


def verify_repo_access(owner: str, repo: str) -> tuple[bool, str]:
    """Check active gh account can read this repo. Returns (ok, message)."""
    result = subprocess.run(
        ["gh", "api", f"/repos/{owner}/{repo}", "--jq", ".full_name"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True, result.stdout.strip()
    err = (result.stderr or "").strip()
    if "404" in err or "Not Found" in err:
        return False, "not found or no access (private repo? wrong gh account?)"
    if "403" in err:
        return False, "access forbidden (403)"
    if "401" in err:
        return False, "unauthorized (401) — re-run `gh auth login`"
    # Network or other error — surface tail of stderr.
    tail = err.splitlines()[-1] if err else "unknown error"
    return False, tail


def _is_transient(stderr: str) -> bool:
    s = stderr.lower()
    return any(m.lower() in s for m in _TRANSIENT_MARKERS)


def run_cmd(cmd: list[str], label: str | None = None) -> subprocess.CompletedProcess:
    """Run a command with exponential-backoff retry on transient errors."""
    label = label or " ".join(cmd[:3])
    last_err = ""
    for attempt in range(MAX_RETRIES + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        last_err = (result.stderr or result.stdout or "").strip()
        if not _is_transient(last_err) or attempt == MAX_RETRIES:
            raise RuntimeError(f"{label} failed: {last_err}")
        delay = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
        delay += random.uniform(0, delay * 0.25)  # jitter
        sys.stderr.write(
            f"  transient error on {label} (attempt {attempt + 1}/{MAX_RETRIES}); "
            f"retrying in {delay:.1f}s — {last_err.splitlines()[-1][:120]}\n"
        )
        time.sleep(delay)
    raise RuntimeError(f"{label} failed after {MAX_RETRIES} retries: {last_err}")


def glab_api_json(endpoint: str, paginate: bool = False):
    """Call glab api and parse JSON."""
    cmd = ["glab", "api", endpoint, "--hostname", "gitlab.com"]
    if paginate:
        cmd += ["--paginate"]
    result = run_cmd(cmd, label=f"glab api {endpoint}")
    text = result.stdout or "null"
    if paginate and text.strip().startswith("["):
        # Handle concatenated arrays: [..][..] or [..]\n[..]
        text = text.strip().replace("]\n[", ",").replace("][", ",")
    return json.loads(text)



class Collector(ABC):
    @abstractmethod
    def verify_access(self, owner: str, repo: str) -> tuple[bool, str]:
        pass

    @abstractmethod
    def discover_prs(self, owner: str, repo: str, author: str | None, state: str) -> list[int]:
        pass

    @abstractmethod
    def collect_pr(self, conn: sqlite3.Connection, owner: str, repo: str,
                   number: int, fetched_by: str) -> tuple[int, int]:
        pass


class GitHubCollector(Collector):
    def verify_access(self, owner: str, repo: str) -> tuple[bool, str]:
        result = subprocess.run(
            ["gh", "api", f"/repos/{owner}/{repo}", "--jq", ".full_name"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        err = (result.stderr or "").strip()
        if "404" in err or "Not Found" in err:
            return False, "not found or no access"
        return False, err.splitlines()[-1] if err else "unknown error"

    def discover_prs(self, owner: str, repo: str, author: str | None,
                     state: str) -> list[int]:
        repo_full = f"{owner}/{repo}"
        cmd = ["gh", "search", "prs", "--repo", repo_full, "--limit", "1000", "--json", "number"]
        if author:
            cmd += ["--author", author]
        if state in ("open", "closed"):
            cmd += ["--state", state]
        result = run_cmd(cmd, label=f"gh search prs --repo {repo_full}")
        data = json.loads(result.stdout or "[]")
        return [item["number"] for item in data]

    def collect_pr(self, conn: sqlite3.Connection, owner: str, repo: str,
                   number: int, fetched_by: str) -> tuple[int, int]:
        repo_full = f"{owner}/{repo}"
        now = datetime.now(timezone.utc).isoformat()
        pr = gh_api_json(f"/repos/{repo_full}/pulls/{number}")
        conn.execute(
            """INSERT OR REPLACE INTO pull_requests
               (id, repo, number, title, user_login, merged_at, created_at,
                updated_at, html_url, state, base_ref, head_ref, fetched_by, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pr["id"], repo_full, pr["number"], pr["title"], pr["user"]["login"],
             pr.get("merged_at"), pr["created_at"], pr["updated_at"], pr["html_url"],
             pr.get("state"), (pr.get("base") or {}).get("ref"),
             (pr.get("head") or {}).get("ref"), fetched_by, now),
        )
        comments = gh_api_json(f"/repos/{repo_full}/pulls/{number}/comments?per_page=100", paginate=True)
        for c in comments:
            conn.execute(
                """INSERT OR REPLACE INTO comments
                   (id, repo, pr_number, review_id, user_login, body, path, line,
                    original_line, side, diff_hunk, commit_id, author_association,
                    in_reply_to_id, created_at, updated_at, html_url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c["id"], repo_full, number, c.get("pull_request_review_id"),
                 c["user"]["login"], c.get("body") or "", c.get("path"), c.get("line"),
                 c.get("original_line"), c.get("side"), c.get("diff_hunk"), c.get("commit_id"),
                 c.get("author_association"), c.get("in_reply_to_id"), c["created_at"],
                 c["updated_at"], c["html_url"]),
            )
        reviews = gh_api_json(f"/repos/{repo_full}/pulls/{number}/reviews?per_page=100", paginate=True)
        rc = 0
        for r in reviews:
            if not r.get("submitted_at"): continue
            conn.execute(
                """INSERT OR REPLACE INTO reviews (id, repo, pr_number, user_login, state, body, submitted_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (r["id"], repo_full, number, r["user"]["login"], r["state"], r.get("body"), r["submitted_at"]),
            )
            rc += 1
        diff = gh_api_raw(f"/repos/{repo_full}/pulls/{number}", "application/vnd.github.v3.diff")
        conn.execute("INSERT OR REPLACE INTO pr_diffs (repo, pr_number, diff, fetched_at) VALUES (?,?,?,?)",
                     (repo_full, number, diff, now))
        conn.commit()
        return len(comments), rc


class GitLabCollector(Collector):
    def verify_access(self, owner: str, repo: str) -> tuple[bool, str]:
        path = f"{owner}/{repo}".replace("/", "%2F")
        result = subprocess.run(["glab", "api", f"projects/{path}", "--hostname", "gitlab.com"], capture_output=True, text=True)
        if result.returncode == 0:
            return True, f"{owner}/{repo}"
        return False, "project not found or no access"

    def discover_prs(self, owner: str, repo: str, author: str | None,
                     state: str) -> list[int]:
        path = f"{owner}/{repo}".replace("/", "%2F")
        endpoint = f"projects/{path}/merge_requests?per_page=100"
        if author: endpoint += f"&author_username={author}"
        if state == "open": endpoint += "&state=opened"
        elif state == "closed": endpoint += "&state=closed"
        data = glab_api_json(endpoint, paginate=True)
        return [item["iid"] for item in data]

    def collect_pr(self, conn: sqlite3.Connection, owner: str, repo: str,
                   number: int, fetched_by: str) -> tuple[int, int]:
        path = f"{owner}/{repo}".replace("/", "%2F")
        repo_full = f"{owner}/{repo}"
        now = datetime.now(timezone.utc).isoformat()
        mr = glab_api_json(f"projects/{path}/merge_requests/{number}")
        
        # Cache diffs for hunk extraction
        try:
            diffs_list = glab_api_json(f"projects/{path}/merge_requests/{number}/diffs", paginate=True)
            diffs_map = {d["new_path"]: d["diff"] for d in diffs_list}
            full_diff = "\n".join(f"--- {d['new_path']}\n+++ {d['new_path']}\n{d['diff']}" for d in diffs_list)
        except Exception as e:
            logger.warning("Diff fetch failed: %s", e)
            diffs_map = {}
            full_diff = ""

        conn.execute(
            """INSERT OR REPLACE INTO pull_requests
               (id, repo, number, title, user_login, merged_at, created_at,
                updated_at, html_url, state, base_ref, head_ref, fetched_by, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mr["id"], repo_full, mr["iid"], mr["title"], mr["author"]["username"],
             mr.get("merged_at"), mr["created_at"], mr["updated_at"], mr["web_url"],
             mr.get("state"), mr.get("target_branch"), mr.get("source_branch"), fetched_by, now),
        )

        # Use Discussions API for code comments
        discussions = glab_api_json(f"projects/{path}/merge_requests/{number}/discussions?per_page=100", paginate=True)
        cc = 0
        for disc in discussions:
            for n in disc.get("notes", []):
                if n.get("system"): continue
                pos = n.get("position") or {}
                new_path = pos.get("new_path")
                new_line = pos.get("new_line")
                
                hunk = None
                if new_path in diffs_map and new_line:
                    hunk = _extract_gl_hunk(diffs_map[new_path], new_line)

                conn.execute(
                    """INSERT OR REPLACE INTO comments
                       (id, repo, pr_number, review_id, user_login, body, path, line,
                        original_line, side, diff_hunk, commit_id, author_association,
                        in_reply_to_id, created_at, updated_at, html_url)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (n["id"], repo_full, number, None, n["author"]["username"],
                     n.get("body") or "", new_path or pos.get("old_path"),
                     new_line or pos.get("old_line"), None, "RIGHT" if new_line else "LEFT",
                     hunk, None, None, None, n["created_at"], n["updated_at"],
                     f"{mr['web_url']}#note_{n['id']}"),
                )
                cc += 1

        # Fetch Approvals for reviews table
        rc = 0
        try:
            approvals = glab_api_json(f"projects/{path}/merge_requests/{number}/approvals")
            for app in approvals.get("approved_by", []):
                user = app.get("user", {})
                conn.execute(
                    """INSERT OR REPLACE INTO reviews (id, repo, pr_number, user_login, state, body, submitted_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (f"{mr['id']}_{user['id']}", repo_full, number, user["username"], "APPROVED", None, now),
                )
                rc += 1
        except: pass
        
        # Also check standard reviews if available
        try:
            reviews = glab_api_json(f"projects/{path}/merge_requests/{number}/reviews")
            for r in reviews:
                conn.execute(
                    """INSERT OR REPLACE INTO reviews (id, repo, pr_number, user_login, state, body, submitted_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (r["id"], repo_full, number, r["author"]["username"], r.get("state", "APPROVED"), None, r["created_at"]),
                )
                rc += 1
        except: pass

        conn.execute("INSERT OR REPLACE INTO pr_diffs (repo, pr_number, diff, fetched_at) VALUES (?,?,?,?)",
                     (repo_full, number, full_diff, now))
        conn.commit()
        return cc, rc


def _extract_gl_hunk(diff_text: str, target_line: int) -> str | None:
    """Extract context hunk from unified diff for target_line."""
    lines = diff_text.splitlines()
    current_line = 0
    hunk_start = 0
    
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            # Format: @@ -old_start,old_count +new_start,new_count @@
            m = re.search(r"\+(\d+)", line)
            if m:
                current_line = int(m.group(1)) - 1
            hunk_start = i
            continue
        
        if not line.startswith("-"):
            current_line += 1
            
        if current_line == target_line:
            # Found line. Return hunk from @@ or +/- 5 lines
            start = max(hunk_start, i - 5)
            end = min(len(lines), i + 6)
            return "\n".join(lines[start:end])
    return None


def run_gh(cmd: list[str], label: str | None = None) -> subprocess.CompletedProcess:
    return run_cmd(cmd, label)


def gh_api_json(endpoint: str, paginate: bool = False):
    """Call gh api and parse JSON. With paginate=True, returns a flat list."""
    cmd = ["gh", "api", endpoint]
    if paginate:
        cmd += ["--paginate", "--slurp"]
    result = run_gh(cmd, label=f"gh api {endpoint}")
    data = json.loads(result.stdout or "null")
    if paginate and isinstance(data, list):
        # --slurp wraps paginated arrays into a list-of-lists; flatten.
        flat = []
        for page in data:
            if isinstance(page, list):
                flat.extend(page)
            elif page is not None:
                flat.append(page)
        return flat
    return data


def gh_api_raw(endpoint: str, accept: str) -> str:
    """Call gh api with a custom Accept header; return raw stdout."""
    result = run_gh(
        ["gh", "api", endpoint, "-H", f"Accept: {accept}"],
        label=f"gh api {endpoint}",
    )
    return result.stdout


def parse_repo_urls(raw_lines: list[str], platform: str) -> list[tuple[str, str]]:
    seen = set()
    repos: list[tuple[str, str]] = []
    url_re = GLAB_URL_RE if platform == "gitlab" else REPO_URL_RE
    for line in raw_lines:
        line = line.strip().rstrip("/")
        if not line or line.startswith("#"):
            continue
        m = url_re.search(line) or REPO_SHORT_RE.match(line)
        if not m:
            print(f"  skipped (not a valid {platform} repo URL): {line}", file=sys.stderr)
            continue
        owner, repo = m.group(1), m.group(2)
        key = (owner, repo)
        if key in seen:
            continue
        seen.add(key)
        repos.append(key)
    return repos


def prompt_repos(platform: str) -> list[tuple[str, str]]:
    print()
    print(f"Paste {platform.capitalize()} repo URLs (one per line). Blank line to finish.")
    print("Examples:")
    if platform == "github":
        print("  https://github.com/owner/repo")
    else:
        print("  https://gitlab.com/owner/repo")
    print("  owner/repo")
    print()
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line)
    repos = parse_repo_urls(lines, platform)
    if not repos:
        die("no valid repo URLs provided.")
    print(f"\nCollected {len(repos)} unique repo(s).")
    return repos


def discover_prs(owner: str, repo: str, author: str | None,
                 state: str) -> list[int]:
    """Find PR numbers in a repo, optionally filtered by author, via `gh search prs`."""
    repo_full = f"{owner}/{repo}"
    cmd = [
        "gh", "search", "prs",
        "--repo", repo_full,
        "--limit", "1000",
        "--json", "number",
    ]
    if author:
        cmd += ["--author", author]
    if state in ("open", "closed"):
        cmd += ["--state", state]
    # state == "all" -> no flag (gh defaults to all when --state omitted for search)

    result = run_gh(cmd, label=f"gh search prs --repo {repo_full}")
    data = json.loads(result.stdout or "[]")
    return [item["number"] for item in data]


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def collect_pr(conn: sqlite3.Connection, owner: str, repo: str,
               number: int, fetched_by: str) -> tuple[int, int]:
    """Fetch one PR's data into the db. Returns (comment_count, review_count)."""
    repo_full = f"{owner}/{repo}"
    now = datetime.now(timezone.utc).isoformat()

    pr = gh_api_json(f"/repos/{repo_full}/pulls/{number}")
    conn.execute(
        """INSERT OR REPLACE INTO pull_requests
           (id, repo, number, title, user_login, merged_at, created_at,
            updated_at, html_url, state, base_ref, head_ref,
            fetched_by, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pr["id"], repo_full, pr["number"], pr["title"],
         pr["user"]["login"], pr.get("merged_at"),
         pr["created_at"], pr["updated_at"], pr["html_url"],
         pr.get("state"),
         (pr.get("base") or {}).get("ref"),
         (pr.get("head") or {}).get("ref"),
         fetched_by, now),
    )

    comments = gh_api_json(
        f"/repos/{repo_full}/pulls/{number}/comments?per_page=100",
        paginate=True,
    )
    for c in comments:
        conn.execute(
            """INSERT OR REPLACE INTO comments
               (id, repo, pr_number, review_id, user_login, body, path, line,
                original_line, side, diff_hunk, commit_id, author_association,
                in_reply_to_id, created_at, updated_at, html_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c["id"], repo_full, number, c.get("pull_request_review_id"),
             c["user"]["login"], c.get("body") or "",
             c.get("path"), c.get("line"), c.get("original_line"),
             c.get("side"), c.get("diff_hunk"), c.get("commit_id"),
             c.get("author_association"), c.get("in_reply_to_id"),
             c["created_at"], c["updated_at"], c["html_url"]),
        )

    reviews = gh_api_json(
        f"/repos/{repo_full}/pulls/{number}/reviews?per_page=100",
        paginate=True,
    )
    review_count = 0
    for r in reviews:
        if not r.get("submitted_at"):
            continue
        conn.execute(
            """INSERT OR REPLACE INTO reviews
               (id, repo, pr_number, user_login, state, body, submitted_at)
               VALUES (?,?,?,?,?,?,?)""",
            (r["id"], repo_full, number, r["user"]["login"],
             r["state"], r.get("body"), r["submitted_at"]),
        )
        review_count += 1

    diff = gh_api_raw(
        f"/repos/{repo_full}/pulls/{number}",
        "application/vnd.github.v3.diff",
    )
    conn.execute(
        """INSERT OR REPLACE INTO pr_diffs (repo, pr_number, diff, fetched_at)
           VALUES (?,?,?,?)""",
        (repo_full, number, diff, now),
    )

    conn.commit()
    return len(comments), review_count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", type=Path, default=None,
                    help="output sqlite path (default: ./crtk-collect-<ts>.db)")
    ap.add_argument("--login", default=None,
                    help="Login to filter PRs by author")
    ap.add_argument("--all-authors", action="store_true",
                    help="fetch PRs by any author")
    ap.add_argument("--state", choices=["all", "closed", "open"], default="all",
                    help="PR state filter (default: all)")
    ap.add_argument("--repos-file", type=Path, default=None,
                    help="read repo URLs from a file instead of stdin")
    args = ap.parse_args()

    print_banner()
    
    print("Select platform:")
    print("  h) GitHub (gh)")
    print("  l) GitLab (glab)")
    choice = input("Choice [h]: ").strip().lower() or "h"
    platform = "gitlab" if choice == "l" else "github"
    
    if platform == "github":
        active_login = check_gh()
        collector = GitHubCollector()
    else:
        active_login = check_glab()
        collector = GitLabCollector()

    fetched_by = args.login or active_login
    author_filter = None if args.all_authors else fetched_by

    if args.repos_file:
        repos = parse_repo_urls(args.repos_file.read_text().splitlines(), platform)
        if not repos:
            die(f"no valid repo URLs in {args.repos_file}")
        print(f"Loaded {len(repos)} repo(s) from {args.repos_file}.")
    else:
        repos = prompt_repos(platform)

    # Access check before any expensive work.
    print()
    print(f"Verifying access to each repo with the active {platform} account...")
    accessible: list[tuple[str, str]] = []
    for owner, repo in repos:
        ok, msg = collector.verify_access(owner, repo)
        if ok:
            print(f"  OK  {owner}/{repo}")
            accessible.append((owner, repo))
        else:
            print(f"  --  {owner}/{repo}  ({msg})")
    
    if not accessible:
        die(f"no accessible repos for {platform}.")

    skipped = len(repos) - len(accessible)
    if skipped:
        print(f"  ({skipped} repo(s) skipped — see above)")

    print()
    if author_filter:
        print(f"Filtering PRs by author: {author_filter} (state={args.state})")
    else:
        print(f"Fetching all PRs regardless of author (state={args.state})")

    # Discovery pass: find PRs in each accessible repo.
    discovered: list[tuple[str, str, int]] = []
    for owner, repo in accessible:
        print(f"  discovering {owner}/{repo} ... ", end="", flush=True)
        try:
            numbers = collector.discover_prs(owner, repo, author_filter, args.state)
        except Exception as e:
            print(f"FAILED: {e}")
            continue
        print(f"{len(numbers)} PR(s)")
        for n in numbers:
            discovered.append((owner, repo, n))

    if not discovered:
        die("no PRs matched — nothing to collect.")

    db_path = args.db or Path(
        f"crtk-collect-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    )
    conn = init_db(db_path)
    print(f"\nWriting to {db_path}")
    print(f"Collecting {len(discovered)} PR(s)...\n")

    total_c = total_r = ok = failed = 0
    for i, (owner, repo, number) in enumerate(discovered, 1):
        label = f"{owner}/{repo}#{number}"
        print(f"[{i}/{len(discovered)}] {label} ... ", end="", flush=True)
        try:
            c, r = collector.collect_pr(conn, owner, repo, number, fetched_by)
            total_c += c
            total_r += r
            ok += 1
            print(f"{c} comments, {r} reviews")
        except Exception as e:
            failed += 1
            print(f"FAILED: {e}")

    conn.close()
    print()
    print(f"Done. {ok} OK, {failed} failed. "
          f"{total_c} review comments, {total_r} review submissions.")
    print(f"DB: {db_path.resolve()}")
    print("Send that file back to whoever requested this collection.")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\naborted.", file=sys.stderr)
        sys.exit(130)
