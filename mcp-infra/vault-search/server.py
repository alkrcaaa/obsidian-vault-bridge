#!/usr/bin/env python3
"""MCP server for searching Obsidian vaults.

Provides keyword search over markdown files with backlink-aware ranking.
Designed for LLM agents to query a personal knowledge base vault.

Transport: stdio (FastMCP).
Env: VAULT_DIR (absolute path to the Obsidian vault root).
"""

import os
import re
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# VAULT_DIR can be set via env or passed as parameter to each tool.
# If neither is set, the tool returns an actionable error.
DEFAULT_VAULT = os.environ.get("VAULT_DIR", "")

# Directories to skip (Obsidian internals, git, cache, etc.)
SKIP_DIRS = {
    ".obsidian",
    ".git",
    ".smart-env",
    "__pycache__",
    "node_modules",
    ".venv",
    ".ruff_cache",
    ".code-review-graph",
    ".stfolder",
}

# Obsidian wikilink pattern: [[path|alias]] or [[path]]
WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^]]+)?\]\]")

# Max snippet length
SNIPPET_MAX = 200

# Default result limit
DEFAULT_LIMIT = 20


server = FastMCP("vault-search", "1.0.0")


def _resolve_vault(vault_dir: str | None) -> Path:
    """Resolve vault directory from param, env, or error."""
    if vault_dir:
        return Path(vault_dir).resolve()
    if DEFAULT_VAULT:
        return Path(DEFAULT_VAULT).resolve()
    raise ValueError(
        "No vault directory. Pass 'vault_dir' parameter or set VAULT_DIR env variable. "
        "Example: VAULT_DIR=/home/user/Notes/AliNotes"
    )


def _collect_md_files(vault: Path, subdir: str | None = None) -> list[Path]:
    """Collect all .md files in vault, optionally filtered to a subdirectory."""
    base = vault / subdir if subdir else vault
    if not base.is_dir():
        return []
    files: list[Path] = []
    for root, dirs, filenames in os.walk(base):
        # Prune skip dirs in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(".md"):
                files.append(Path(root) / f)
    return files


def _relative_path(p: Path, vault: Path) -> str:
    """Return vault-relative path."""
    try:
        return str(p.relative_to(vault))
    except ValueError:
        return str(p)


def _extract_links(content: str) -> list[str]:
    """Extract [[wikilink]] targets from markdown content."""
    return WIKILINK_RE.findall(content)


def _snippet(text: str, keyword: str, max_len: int = SNIPPET_MAX) -> str:
    """Extract a snippet around the first occurrence of keyword."""
    lower = text.lower()
    idx = lower.find(keyword.lower())
    if idx == -1:
        return text[:max_len] + ("..." if len(text) > max_len else "")
    start = max(0, idx - 60)
    end = min(len(text), idx + len(keyword) + max_len - 80)
    result = text[start:end]
    if start > 0:
        result = "..." + result
    if end < len(text):
        result = result + "..."
    return result


def _score_file(
    file_path: Path,
    vault: Path,
    query: str,
    link_index: dict[str, list[str]],
    case_sensitive: bool = False,
) -> tuple[float, str]:
    """Score a file against the query. Returns (score, snippet).

    Scoring:
    - Title match (first # heading): 10x
    - Frontmatter match: 5x
    - Body keyword match: 1x per occurrence (capped at 20)
    - Backlink bonus: +2 per link that contains query terms
    """
    try:
        content = file_path.read_text(errors="replace")
    except Exception:
        return (0.0, "")

    if not case_sensitive:
        content_lower = content.lower()
        query_lower = query.lower()
        search_content = content_lower
        search_query = query_lower
    else:
        search_content = content
        search_query = query

    score = 0.0
    query_terms = search_query.split()

    # Title match (first heading)
    title_match = re.search(r"^#\s+(.+)", content, re.MULTILINE)
    if title_match:
        title = title_match.group(1)
        if not case_sensitive:
            title = title.lower()
        for term in query_terms:
            if term in title:
                score += 10.0

    # Frontmatter match
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if fm_match:
        fm = fm_match.group(1)
        if not case_sensitive:
            fm = fm.lower()
        for term in query_terms:
            if term in fm:
                score += 5.0

    # Body keyword match
    for term in query_terms:
        count = search_content.count(term)
        score += min(count, 20)  # cap per term

    # Backlink bonus: does this file link to anything matching query terms?
    links = _extract_links(content)
    for link_target in links:
        # Normalize: remove path prefix, keep filename
        target_name = link_target.split("|")[-1].split("/")[-1]
        if not case_sensitive:
            target_name = target_name.lower()
        for term in query_terms:
            if term in target_name:
                score += 2.0
                break  # one bonus per link

    # Reverse link bonus: does anything matching query link TO this file?
    rel = _relative_path(file_path, vault)
    filename = file_path.stem.lower() if not case_sensitive else file_path.stem
    for term in query_terms:
        term_search = term if case_sensitive else term.lower()
        if term_search in filename:
            # Files that appear in link_index pointing to this file get bonus
            for link_key in link_index:
                if term_search in link_key.lower() if not case_sensitive else term in link_key:
                    if rel in link_index[link_key]:
                        score += 1.5
                        break

    snippet = _snippet(content, query) if score > 0 else ""
    return (score, snippet)


def _build_link_index(vault: Path, files: list[Path]) -> dict[str, list[str]]:
    """Build index: for each file, which other files link to it.

    Returns {relative_path: [list of files that link to it]}
    """
    index: dict[str, list[str]] = {}
    for f in files:
        try:
            content = f.read_text(errors="replace")
        except Exception:
            continue
        links = _extract_links(content)
        rel = _relative_path(f, vault)
        for link in links:
            # Normalize link target
            target = link.split("|")[0].strip()  # remove alias
            if target not in index:
                index[target] = []
            index[target].append(rel)
    return index


@server.tool()
def vault_search(
    query: str,
    vault_dir: str | None = None,
    limit: int = DEFAULT_LIMIT,
    subdir: str | None = None,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """Search an Obsidian vault by keyword with backlink-aware ranking.

    Searches markdown files for the given query terms, ranking results by:
    - Title/heading matches (highest weight)
    - Frontmatter matches
    - Body text frequency
    - Wikilink connections to matching files

    Args:
        query: Search terms (space-separated). Each term is scored independently.
        vault_dir: Absolute path to the Obsidian vault root. Falls back to VAULT_DIR env var.
        limit: Maximum number of results to return (default 20).
        subdir: Optional subdirectory within vault to search (e.g. "08- Wiki").
        case_sensitive: Whether to use case-sensitive matching (default False).

    Returns:
        dict with 'query', 'total_matches', 'results' (list of {path, score, title, snippet, links})
    """
    try:
        vault = _resolve_vault(vault_dir)
    except ValueError as e:
        return {"error": str(e), "query": query, "results": []}

    if not vault.is_dir():
        return {"error": f"Vault directory does not exist: {vault}", "query": query, "results": []}

    files = _collect_md_files(vault, subdir)
    if not files:
        return {"query": query, "total_matches": 0, "results": [], "vault": str(vault)}

    # Build reverse link index for backlink scoring
    link_index = _build_link_index(vault, files)

    # Score all files
    scored: list[tuple[float, Path, str]] = []
    for f in files:
        s, snippet = _score_file(f, vault, query, link_index, case_sensitive)
        if s > 0:
            scored.append((s, f, snippet))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    results = []
    for score, f, snippet in top:
        rel = _relative_path(f, vault)
        try:
            content = f.read_text(errors="replace")
            title_m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
            title = title_m.group(1) if title_m else f.name
            links = _extract_links(content)[:10]  # first 10 links
        except Exception:
            title = f.name
            links = []

        results.append({
            "path": rel,
            "score": round(score, 1),
            "title": title,
            "snippet": snippet,
            "links": links,
        })

    return {
        "query": query,
        "vault": str(vault),
        "subdir": subdir,
        "total_matches": len(scored),
        "returned": len(results),
        "results": results,
    }


@server.tool()
def vault_list(
    vault_dir: str | None = None,
    subdir: str | None = None,
    pattern: str | None = None,
    include_links: bool = False,
) -> dict[str, Any]:
    """List files in an Obsidian vault, optionally filtered by subdirectory or glob pattern.

    Args:
        vault_dir: Absolute path to the Obsidian vault root. Falls back to VAULT_DIR env var.
        subdir: Optional subdirectory to list (e.g. "07- Raw", "08- Wiki").
        pattern: Optional glob pattern to filter files (e.g. "*.md", "Karagag*").
        include_links: If True, include [[wikilink]] targets for each file (slower).

    Returns:
        dict with 'vault', 'files' (list of {path, title, size, links?})
    """
    try:
        vault = _resolve_vault(vault_dir)
    except ValueError as e:
        return {"error": str(e), "files": []}

    if not vault.is_dir():
        return {"error": f"Vault directory does not exist: {vault}", "files": []}

    files = _collect_md_files(vault, subdir)

    # Apply glob pattern if given
    if pattern:
        filtered: list[Path] = []
        for f in files:
            rel = _relative_path(f, vault)
            # Use fnmatch for glob matching
            import fnmatch
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                filtered.append(f)
        files = filtered

    results = []
    for f in files:
        rel = _relative_path(f, vault)
        size = f.stat().st_size if f.exists() else 0
        title = f.stem

        # Extract title from content
        try:
            content = f.read_text(errors="replace")
            title_m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
            if title_m:
                title = title_m.group(1)
        except Exception:
            pass

        entry: dict[str, Any] = {
            "path": rel,
            "title": title,
            "size": size,
        }

        if include_links:
            try:
                content = f.read_text(errors="replace")
                entry["links"] = _extract_links(content)[:10]
            except Exception:
                entry["links"] = []

        results.append(entry)

    return {
        "vault": str(vault),
        "subdir": subdir,
        "pattern": pattern,
        "count": len(results),
        "files": results,
    }


if __name__ == "__main__":
    server.run()
