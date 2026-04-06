# CRTK — Code Review Tribal Knowledge

CRTK harvests PR review comments from GitHub, builds a searchable knowledge base, and serves synthesized team conventions via CLI and MCP server.

## What it does

1. **Fetches** all merged PR review comments from configured GitHub repos
2. **Embeds** each comment using a local embedding model (`all-MiniLM-L6-v2`, 22MB)
3. **Tags** comments with metadata categories (43 tags: `database`, `naming`, `n-plus-one-queries`, etc.)
4. **Searches** via 3-stage hybrid search: tag pre-filter → FTS5 keyword + vector cosine → Reciprocal Rank Fusion
5. **Synthesizes** results into actionable team conventions
6. **Serves** conventions to Claude Code via MCP server

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- GitHub CLI (`gh`) authenticated with repo access
- ~500MB disk for PyTorch + embedding model (first run)

## Installation

```bash
git clone <repo-url> /path/to/crtk
cd /path/to/crtk

# Install with uv (recommended)
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# Or with pip
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

Verify:

```bash
crtk --help
```

## Configuration

Copy and edit `crtk.toml` to configure your repos:

```toml
[general]
db_path = "~/.local/share/crtk/crtk.db"
log_level = "INFO"                           # DEBUG for verbose output

[repos]
list = [
    "your-org/repo-one",
    "your-org/repo-two",
]

[fetch]
page_size = 100
max_retries = 5
base_delay_seconds = 1.0
max_delay_seconds = 60.0
inter_request_delay = 0.5                    # delay between API calls (seconds)

[search]
fts_weight = 0.4
vector_weight = 0.6
default_limit = 15

[embeddings]
model = "all-MiniLM-L6-v2"                  # 22MB, 384d, runs on CPU

[tagging]
auto_tag_on_fetch = true                     # auto-embed + auto-tag after fetch

[synthesis]
mode = "template"                            # "template" or "llm"
max_comments = 20
```

Config file search order:
1. `$CRTK_CONFIG` environment variable
2. `~/.config/crtk/crtk.toml`
3. `./crtk.toml` (current directory)

Or pass explicitly: `crtk --config /path/to/crtk.toml <command>`

## CLI Usage

### Fetch data from GitHub

```bash
# Incremental fetch (only new data since last fetch)
crtk fetch

# Full re-fetch all repos
crtk fetch --full

# Fetch a specific repo
crtk fetch --repo my-repo-name
```

Each fetch:
- Pulls all merged PRs and their inline review comments + reviews
- Generates embeddings for new comments
- Auto-tags new comments with metadata categories
- Tracks last fetch timestamp per repo for incremental updates
- Uses exponential backoff with jitter on API failures

### Query the knowledge base

```bash
# Search by file path
crtk query --file src/modules/batches/my-file.ts

# Search with full context
crtk query \
  --file src/modules/batches/my-file.ts \
  --diff "$(git diff)" \
  --title "add validation to batch processing"

# Pre-filter by tags for targeted results
crtk query --file src/modules/batches/my-file.ts --tags database,n-plus-one-queries

# Limit results
crtk query --file src/modules/users/users.service.ts --limit 5
```

### View tags and stats

```bash
# List all tags with comment counts
crtk tags

# Show knowledge base statistics
crtk stats
```

### Start MCP server

```bash
crtk serve
```

## Claude Code Integration

CRTK is designed to work with Claude Code through two components: an **MCP server** (provides the data) and a **code review skill** (uses the data during reviews). You need both.

### Step 1: Add the MCP server config

Add `.mcp.json` to your project root (commit this so teammates get it):

```json
{
  "mcpServers": {
    "crtk": {
      "command": "/path/to/crtk/.venv/bin/crtk",
      "args": ["--config", "/path/to/crtk/crtk.toml", "serve"]
    }
  }
}
```

Replace `/path/to/crtk` with the actual install location.

### Step 2: Add the code review skill

Copy `code-review.md` into your project so teammates can use it:

```bash
mkdir -p .claude/commands
cp /path/to/crtk/code-review.md .claude/commands/code-review.md
```

The skill is also available at `~/.claude/commands/code-review.md` for personal use across all projects.

This skill runs `/code-review` in Claude Code. It spawns 6 parallel subagents to check conventions, tests, performance, security, logic, and magic strings — and feeds CRTK tribal knowledge into each one.

### Step 3: Add CLAUDE.md instruction (optional)

If you want Claude Code to use CRTK even outside of `/code-review` (e.g. when you say "review my changes" in conversation), add this to your project's `.claude/CLAUDE.md`:

```markdown
## Code Review with CRTK

When reviewing code, checking PRs, or before suggesting changes, consult the CRTK (Code Review Tribal Knowledge) MCP server for team conventions:

1. Call `list_tags` to see available review categories and their comment counts
2. Based on the files being changed and the nature of the change, pick 2-5 relevant tags
3. Call `search_conventions` with:
   - `file_paths`: the files being changed
   - `diff`: the code diff (truncated if large)
   - `pr_title`: description of what the change does
   - `tags`: the tags you picked
4. Incorporate any relevant conventions into your review feedback — cite the reviewer and date when referencing specific past feedback

Do this automatically whenever you are asked to review code, review a PR, or when making significant changes to existing code.
```

### Step 4: Verify

Start a new Claude Code session in the repo and run:

```
/mcp
```

You should see `crtk` listed with 4 tools: `list_tags`, `search_conventions`, `fetch_new_reviews`, `get_stats`.

Then run:

```
/code-review
```

The review report will include a "CRTK Context" section showing which tags were searched and what tribal knowledge was found.

### MCP Tools Reference

| Tool | Description |
|------|-------------|
| `list_tags` | List all metadata tags with descriptions and comment counts. Call this first. |
| `search_conventions` | Search for team conventions. Accepts `file_paths`, `diff`, `pr_title`, `commit_messages`, `tags`, `limit`. |
| `fetch_new_reviews` | Trigger incremental fetch of new PR review comments. |
| `get_stats` | Get knowledge base statistics. |

### How it works during code review

The `/code-review` skill:

1. Reads repo conventions from CLAUDE.md
2. Gets the git diff and changed files
3. **Calls CRTK**: `list_tags` → picks relevant tags → `search_conventions` with file paths + diff + tags
4. Spawns 6 parallel subagents, each receiving the CRTK tribal knowledge as context
5. Deduplicates and fact-checks findings
6. Generates a report with a CRTK Context section

If CRTK is not available (no MCP server, no database), the skill works normally without tribal knowledge — it degrades gracefully.

## Sharing the Database

The SQLite database at `~/.local/share/crtk/crtk.db` (~3MB for 477 comments) is fully self-contained and portable.

### Export a database dump

```bash
# Option 1: Copy the file directly
cp ~/.local/share/crtk/crtk.db ./crtk-dump.db

# Option 2: SQL dump (text-based, version-controllable)
sqlite3 ~/.local/share/crtk/crtk.db .dump > crtk-dump.sql

# Option 3: Compressed for sharing
gzip -c ~/.local/share/crtk/crtk.db > crtk-dump.db.gz
```

### Import / restore from a dump

```bash
# From a .db file — just place it at the configured path
cp crtk-dump.db ~/.local/share/crtk/crtk.db

# From a SQL dump
mkdir -p ~/.local/share/crtk
sqlite3 ~/.local/share/crtk/crtk.db < crtk-dump.sql

# From a compressed dump
gunzip -c crtk-dump.db.gz > ~/.local/share/crtk/crtk.db
```

After importing, verify with:

```bash
crtk stats
```

The imported database includes all comments, embeddings, and tags — no need to re-fetch, re-embed, or re-tag.

### Custom database location

Set `db_path` in `crtk.toml` to use a different location:

```toml
[general]
db_path = "/shared/team/crtk/reviews.db"
```

## Architecture

```
src/crtk/
├── cli.py           # click CLI (fetch, query, tags, stats, serve)
├── config.py        # TOML config loading with typed dataclasses
├── db.py            # SQLite connection, schema init, CRUD operations
├── schema.sql       # DDL: pull_requests, reviews, comments, FTS5, embeddings, tags
├── models.py        # dataclasses: PullRequest, Review, Comment, SearchResult, etc.
├── fetcher.py       # GitHub API fetching via `gh api --paginate`
├── retry.py         # exponential backoff + jitter + rate limit detection
├── embeddings.py    # sentence-transformers embedding gen + storage
├── tagger.py        # keyword-heuristic auto-tagger (43 seed tag rules)
├── search.py        # 3-stage hybrid search: tag filter → FTS5 + vector → RRF
├── synthesizer.py   # group, dedup, format results as conventions
└── mcp_server.py    # MCP server (stdio transport, 4 tools)
```

### Search pipeline

```
Query context (file_paths, diff, title, tags)
  │
  ├─ Tag pre-filter: 477 comments → ~50 matching tags
  │
  ├─ FTS5 search: BM25 keyword matching on filtered set
  │
  ├─ Vector search: cosine similarity against comment embeddings
  │
  └─ Reciprocal Rank Fusion (k=60): merge both ranked lists
      │
      └─ Synthesize top 15-20 into grouped conventions
```

### Database schema

| Table | Purpose |
|-------|---------|
| `pull_requests` | PR metadata (id, repo, number, title, author, merged_at) |
| `reviews` | Review submissions (APPROVED, CHANGES_REQUESTED, COMMENTED) |
| `comments` | Inline review comments with file path, line, diff_hunk, author |
| `comments_fts` | FTS5 virtual table for full-text search (auto-synced via triggers) |
| `comment_embeddings` | 384d float32 vectors stored as BLOBs |
| `tags` | Metadata tag definitions (43 seed tags) |
| `comment_tags` | Many-to-many: which tags apply to which comments |
| `fetch_state` | Last fetch timestamp per repo (for incremental fetches) |
| `conventions` | Pre-synthesized convention summaries (future use) |

## Troubleshooting

### Rate limiting

CRTK uses exponential backoff with jitter. If you hit rate limits:

```bash
# Check current rate limit
gh api /rate_limit --jq '.rate'

# Use debug logging to see retry behavior
# Set log_level = "DEBUG" in crtk.toml
```

### Re-embedding after model change

If you change the embedding model in config, clear and regenerate:

```bash
sqlite3 ~/.local/share/crtk/crtk.db "DELETE FROM comment_embeddings;"
crtk fetch  # will re-embed all comments
```

### Re-tagging all comments

```bash
sqlite3 ~/.local/share/crtk/crtk.db "DELETE FROM comment_tags;"
crtk tag
```

### Reset everything

```bash
rm ~/.local/share/crtk/crtk.db
crtk fetch --full
```
