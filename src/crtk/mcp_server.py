from __future__ import annotations

import json
import logging
import sqlite3

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from crtk.config import CrtkConfig
from crtk.db import get_all_tags_with_counts, get_stats

logger = logging.getLogger(__name__)


def create_server(config: CrtkConfig, conn: sqlite3.Connection) -> Server:
    server = Server("crtk")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="list_tags",
                description=(
                    "List all available metadata tags with descriptions and comment counts. "
                    "Call this FIRST to understand what categories of review feedback exist, "
                    "then pass relevant tags to search_conventions for targeted results."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            Tool(
                name="search_conventions",
                description=(
                    "Search team code review conventions relevant to the given code context. "
                    "If tags are provided, pre-filters to comments matching those tags "
                    "before running hybrid search. This dramatically improves relevance. "
                    "Returns synthesized conventions from past review comments."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "File paths being changed or reviewed",
                        },
                        "diff": {
                            "type": "string",
                            "description": "Code diff text (truncated to ~4000 chars)",
                        },
                        "pr_title": {
                            "type": "string",
                            "description": "PR title or description of the change",
                        },
                        "commit_messages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Recent commit messages",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Metadata tags to pre-filter by (from list_tags)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max number of results (default 10)",
                            "default": 10,
                        },
                    },
                    "required": ["file_paths"],
                },
            ),
            Tool(
                name="fetch_new_reviews",
                description="Trigger incremental fetch of new PR review comments from GitHub.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            Tool(
                name="get_stats",
                description="Get statistics about the CRTK knowledge base.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "list_tags":
            return await _handle_list_tags(conn)
        elif name == "search_conventions":
            return await _handle_search_conventions(conn, config, arguments)
        elif name == "fetch_new_reviews":
            return await _handle_fetch_new_reviews(conn, config)
        elif name == "get_stats":
            return await _handle_get_stats(conn)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


async def _handle_list_tags(conn: sqlite3.Connection) -> list[TextContent]:
    all_tags = get_all_tags_with_counts(conn)
    if not all_tags:
        return [TextContent(type="text", text="No tags yet. Run fetch_new_reviews first.")]

    output = json.dumps(all_tags, indent=2)
    return [TextContent(type="text", text=output)]


async def _handle_search_conventions(
    conn: sqlite3.Connection, config: CrtkConfig, arguments: dict
) -> list[TextContent]:
    from crtk.search import hybrid_search
    from crtk.synthesizer import synthesize_results

    file_paths = arguments.get("file_paths", [])
    diff_text = arguments.get("diff")
    pr_title = arguments.get("pr_title")
    commit_messages = arguments.get("commit_messages")
    tags = arguments.get("tags")
    limit = max(1, min(int(arguments.get("limit", 10)), 100))

    results = hybrid_search(
        conn, config,
        file_paths=file_paths,
        diff_text=diff_text,
        pr_title=pr_title,
        commit_messages=commit_messages,
        tags=tags,
        limit=limit,
    )

    if not results:
        return [TextContent(type="text", text="No matching conventions found for this context.")]

    output = synthesize_results(results, config)
    return [TextContent(type="text", text=output)]


async def _handle_fetch_new_reviews(
    conn: sqlite3.Connection, config: CrtkConfig
) -> list[TextContent]:
    from crtk.fetcher import run_fetch

    summary = run_fetch(conn, config, full=False)
    text = (f"Fetch complete: {summary['total_prs']} PRs, "
            f"{summary['total_comments']} comments from {summary['repos_fetched']} repos")

    # Auto-embed and tag new comments
    if summary["total_comments"] > 0:
        from crtk.db import get_unembedded_comment_ids, get_untagged_comment_ids
        from crtk.embeddings import embed_and_store
        from crtk.search import invalidate_cache
        from crtk.tagger import tag_comments

        unembedded = get_unembedded_comment_ids(conn)
        if unembedded:
            embed_and_store(conn, unembedded, config)
            invalidate_cache()
            text += f"\nEmbedded {len(unembedded)} new comments."

        untagged = get_untagged_comment_ids(conn)
        if untagged:
            tag_comments(conn, untagged, config)
            text += f"\nTagged {len(untagged)} new comments."

    return [TextContent(type="text", text=text)]


async def _handle_get_stats(conn: sqlite3.Connection) -> list[TextContent]:
    s = get_stats(conn)
    output = json.dumps(s, indent=2, default=str)
    return [TextContent(type="text", text=output)]


def run_server(config: CrtkConfig, conn: sqlite3.Connection) -> None:
    """Run the MCP server on stdio."""
    import asyncio

    server = create_server(config, conn)

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())
