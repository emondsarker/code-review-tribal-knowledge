from __future__ import annotations

import logging
import os
from collections import defaultdict

from crtk.config import CrtkConfig
from crtk.models import SearchResult

logger = logging.getLogger(__name__)


def synthesize_results(results: list[SearchResult], config: CrtkConfig) -> str:
    """Synthesize search results into actionable conventions.

    Groups comments by file directory, deduplicates, and formats as structured markdown.
    """
    if not results:
        return "No conventions found."

    if config.synthesis.mode == "llm":
        return _synthesize_llm(results, config)
    return _synthesize_template(results, config)


def _synthesize_template(results: list[SearchResult], config: CrtkConfig) -> str:
    """Template-based synthesis: group, dedup, format."""
    # Group by file directory
    groups: dict[str, list[SearchResult]] = defaultdict(list)
    for r in results[:config.synthesis.max_comments]:
        dirname = os.path.dirname(r.comment.path) if r.comment.path else "general"
        groups[dirname].append(r)

    lines = [f"Found {len(results)} relevant review comments across {len(groups)} locations.\n"]

    for group_path, group_results in sorted(groups.items()):
        lines.append(f"--- {group_path or 'General'} ---\n")

        # Deduplicate similar comments
        seen_bodies: list[str] = []
        for r in group_results:
            if _is_duplicate(r.comment.body, seen_bodies):
                continue
            seen_bodies.append(r.comment.body)

            date = r.comment.created_at[:10]
            reviewer = r.comment.user_login
            tags_str = f"  tags: {', '.join(r.tags)}" if r.tags else ""
            location = ""
            if r.comment.path:
                location = f"  {r.comment.path}"
                if r.comment.line:
                    location += f":{r.comment.line}"

            body_summary = _summarize_body(r.comment.body, max_length=200)
            lines.append(f"  [{reviewer} {date}]{location}")
            lines.append(f"  {body_summary}")
            if tags_str:
                lines.append(tags_str)
            lines.append("")

    return "\n".join(lines)


def _synthesize_llm(results: list[SearchResult], config: CrtkConfig) -> str:
    """LLM-based synthesis — formats context and calls Claude for summarization."""
    import subprocess

    context = _format_for_llm(results, config)
    prompt = f"""You are analyzing code review comments from a team to extract conventions and preferences.

Here are relevant past review comments:

{context}

Based on these review comments, list the team conventions and preferences as actionable rules.
Format as a numbered list. Only include conventions clearly supported by the comments.
Be concise — each convention should be 1-2 sentences."""

    try:
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"## Team Review Conventions (AI-synthesized)\n\n{result.stdout.strip()}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("LLM synthesis failed, falling back to template")

    return _synthesize_template(results, config)


def _format_for_llm(results: list[SearchResult], config: CrtkConfig) -> str:
    """Format results for LLM consumption."""
    lines = []
    for i, r in enumerate(results[:config.synthesis.max_comments], 1):
        c = r.comment
        lines.append(f"Comment #{i} by {c.user_login} on {c.created_at[:10]}:")
        if c.path:
            lines.append(f"  File: {c.path}")
        lines.append(f"  Review: {c.body}")
        if c.diff_hunk:
            lines.append(f"  Code context: {_truncate_hunk(c.diff_hunk)}")
        lines.append("")
    return "\n".join(lines)


def _is_duplicate(body: str, seen: list[str], threshold: float = 0.7) -> bool:
    """Check if a comment body is too similar to already-seen comments using Jaccard similarity."""
    words_new = set(body.lower().split())
    for seen_body in seen:
        words_seen = set(seen_body.lower().split())
        if not words_new or not words_seen:
            continue
        intersection = words_new & words_seen
        union = words_new | words_seen
        similarity = len(intersection) / len(union)
        if similarity >= threshold:
            return True
    return False


def _summarize_body(body: str, max_length: int = 300) -> str:
    """Truncate and clean up a comment body for display."""
    # Remove markdown images and long URLs
    import re
    body = re.sub(r'!\[.*?\]\(.*?\)', '[image]', body)
    body = re.sub(r'https?://\S{80,}', '[long-url]', body)

    lines = body.strip().split('\n')
    # Take the first meaningful lines
    summary_lines = []
    length = 0
    for line in lines:
        if length + len(line) > max_length:
            summary_lines.append(line[:max_length - length] + "...")
            break
        summary_lines.append(line)
        length += len(line)

    return " ".join(summary_lines)


def _truncate_hunk(hunk: str, max_lines: int = 6) -> str:
    """Truncate a diff hunk to max_lines."""
    lines = hunk.strip().split('\n')
    if len(lines) <= max_lines:
        return hunk.strip()
    return '\n'.join(lines[:max_lines]) + '\n  ...'
