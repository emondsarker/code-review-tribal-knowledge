from __future__ import annotations

import logging
import sys

import click

from crtk.config import load_config
from crtk.db import get_all_tags_with_counts, get_stats, init_db


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Silence noisy libraries — only show their errors
    for noisy in ("httpx", "httpcore", "huggingface_hub", "huggingface_hub.utils",
                  "sentence_transformers", "transformers", "torch", "urllib3",
                  "filelock"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


@click.group()
@click.option("--config", "config_path", default=None, help="Path to crtk.toml config file")
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    """CRTK — Code Review Tribal Knowledge"""
    ctx.ensure_object(dict)
    cfg = load_config(config_path)
    setup_logging(cfg.log_level)
    ctx.obj["config"] = cfg
    ctx.obj["conn"] = init_db(cfg.resolved_db_path)


@main.command()
@click.option("--full", is_flag=True, help="Full re-fetch (ignore last fetch timestamp)")
@click.option("--repo", default=None, help="Filter to a specific repo")
@click.pass_context
def fetch(ctx: click.Context, full: bool, repo: str | None) -> None:
    """Fetch PR review data from GitHub."""
    from crtk.fetcher import run_fetch

    cfg = ctx.obj["config"]
    conn = ctx.obj["conn"]

    summary = run_fetch(conn, cfg, full=full, repo_filter=repo)
    click.echo(f"Fetch complete: {summary['total_prs']} PRs, "
               f"{summary['total_comments']} comments, "
               f"{summary['total_reviews']} reviews "
               f"from {summary['repos_fetched']} repos")

    # Auto-embed and auto-tag if configured
    if cfg.tagging.auto_tag_on_fetch and summary["total_comments"] > 0:
        click.echo("Running auto-embed + auto-tag on new comments...")
        _run_embed(conn, cfg)
        _run_tag(conn, cfg)


@main.command()
@click.pass_context
def stats(ctx: click.Context) -> None:
    """Show knowledge base statistics."""
    conn = ctx.obj["conn"]
    s = get_stats(conn)

    click.echo("=== CRTK Knowledge Base Stats ===")
    click.echo(f"Pull Requests: {s['pull_requests']}")
    click.echo(f"Reviews:       {s['reviews']}")
    click.echo(f"Comments:      {s['comments']}")
    click.echo(f"Embeddings:    {s['comment_embeddings']}")
    click.echo(f"Tagged:        {s['comment_tags']}")
    click.echo(f"Tags:          {s['tags']}")

    if s["prs_by_repo"]:
        click.echo("\nPRs by repo:")
        for repo, cnt in s["prs_by_repo"].items():
            click.echo(f"  {repo}: {cnt}")

    if s["comments_by_repo"]:
        click.echo("\nComments by repo:")
        for repo, cnt in s["comments_by_repo"].items():
            click.echo(f"  {repo}: {cnt}")

    if s["top_reviewers"]:
        click.echo("\nTop reviewers:")
        for user, cnt in s["top_reviewers"].items():
            click.echo(f"  {user}: {cnt}")

    if s["date_range"]["oldest"]:
        click.echo(f"\nDate range: {s['date_range']['oldest']} → {s['date_range']['newest']}")


@main.command()
@click.pass_context
def tag(ctx: click.Context) -> None:
    """(Re)classify all comments into metadata tags."""
    conn = ctx.obj["conn"]
    cfg = ctx.obj["config"]
    _run_tag(conn, cfg)


@main.command()
@click.pass_context
def tags(ctx: click.Context) -> None:
    """List all tags with comment counts."""
    conn = ctx.obj["conn"]
    all_tags = get_all_tags_with_counts(conn)
    if not all_tags:
        click.echo("No tags yet. Run 'crtk fetch' or 'crtk tag' first.")
        return

    click.echo(f"{'Tag':<40} {'Count':>6}  Description")
    click.echo("-" * 80)
    for t in all_tags:
        desc = (t["description"] or "")[:30]
        click.echo(f"{t['name']:<40} {t['count']:>6}  {desc}")


@main.command()
@click.option("--file", "file_paths", multiple=True, help="File paths to search for")
@click.option("--diff", "diff_text", default=None, help="Diff text for context")
@click.option("--title", "pr_title", default=None, help="PR title for context")
@click.option("--tags", "tag_names", default=None, help="Comma-separated tag names to pre-filter")
@click.option("--limit", default=15, help="Max results")
@click.pass_context
def query(ctx: click.Context, file_paths: tuple, diff_text: str | None,
          pr_title: str | None, tag_names: str | None, limit: int) -> None:
    """Search the knowledge base for relevant conventions."""
    from crtk.search import hybrid_search
    from crtk.synthesizer import synthesize_results

    conn = ctx.obj["conn"]
    cfg = ctx.obj["config"]

    tags_list = [t.strip() for t in tag_names.split(",")] if tag_names else None

    results = hybrid_search(
        conn, cfg,
        file_paths=list(file_paths),
        diff_text=diff_text,
        pr_title=pr_title,
        tags=tags_list,
        limit=limit,
    )

    if not results:
        click.echo("No matching review comments found.")
        return

    output = synthesize_results(results, cfg)
    click.echo(output)


@main.command()
@click.pass_context
def synthesize(ctx: click.Context) -> None:
    """Batch-synthesize conventions from all comments."""
    click.echo("Convention synthesis not yet implemented (Phase 4).")


@main.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start MCP server (stdio transport)."""
    from crtk.mcp_server import run_server
    cfg = ctx.obj["config"]
    conn = ctx.obj["conn"]
    run_server(cfg, conn)


def _run_embed(conn, cfg) -> None:
    from crtk.db import get_unembedded_comment_ids
    from crtk.embeddings import embed_and_store

    unembedded = get_unembedded_comment_ids(conn)
    if unembedded:
        click.echo(f"Embedding {len(unembedded)} new comments...")
        embed_and_store(conn, unembedded, cfg)
        click.echo("Embedding complete.")
    else:
        click.echo("All comments already embedded.")


def _run_tag(conn, cfg) -> None:
    from crtk.db import get_untagged_comment_ids
    from crtk.tagger import tag_comments

    untagged = get_untagged_comment_ids(conn)
    if untagged:
        click.echo(f"Tagging {len(untagged)} new comments...")
        tag_comments(conn, untagged, cfg)
        click.echo("Tagging complete.")
    else:
        click.echo("All comments already tagged.")


if __name__ == "__main__":
    main()
