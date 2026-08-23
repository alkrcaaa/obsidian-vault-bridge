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

# Where obsidian-mirror.py appends raw mem-lite saves, if configured. Notes under
# it are an append-only log, not a curated note, so they rank below the compiled
# note they were compiled *into* — otherwise the raw record outranks the article,
# which is backwards for the whole raw -> compile -> wiki flow.
RAW_MIRROR = os.environ.get("MEM_OBSIDIAN_VAULT", "")

# Rank multipliers: a note carrying `last_compiled:` is the curated output of a
# synthesis pass; a raw mirror log is its input.
COMPILED_BOOST = 1.5
RAW_MIRROR_PENALTY = 0.5

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
    """Collect all .md files in vault, optionally filtered to a subdirectory.

    A subdir that resolves outside the vault is ignored: every path we return is
    reported relative to the vault, so escaping it would silently start emitting
    absolute paths from _relative_path()'s fallback.
    """
    base = (vault / subdir).resolve() if subdir else vault
    if subdir and not base.is_relative_to(vault):
        return []
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


def _snippet(text: str, query: str, max_len: int = SNIPPET_MAX) -> str:
    """Extract a snippet around the first occurrence of the query.

    The whole query is tried first (an exact phrase hit is the best snippet
    there is), then each term on its own. Without the per-term fallback every
    multi-word query -- which is most of what an agent writes -- misses and
    returns the head of the file, i.e. the frontmatter, for every result.
    """
    lower = text.lower()
    keyword = ""
    idx = -1
    for candidate in [query, *query.split()]:
        idx = lower.find(candidate.lower())
        if idx != -1:
            keyword = candidate
            break
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


def _frontmatter(content: str) -> str:
    """Return the raw YAML frontmatter block, or '' if there is none."""
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    return m.group(1) if m else ""


def _matches_frontmatter(fm: str, wanted: dict[str, str]) -> bool:
    """True if every wanted key/value appears as a frontmatter line."""
    return all(
        re.search(rf"^{re.escape(k)}:\s*[\"']?{re.escape(v)}[\"']?\s*$", fm, re.MULTILINE)
        for k, v in wanted.items()
    )


def _rank_multiplier(file_path: Path, frontmatter: str) -> float:
    """Weight a note by what it is: compiled article, raw log, or neither."""
    if RAW_MIRROR and str(file_path).startswith(RAW_MIRROR):
        return RAW_MIRROR_PENALTY
    if "last_compiled:" in frontmatter:
        return COMPILED_BOOST
    return 1.0


def _score_file(
    file_path: Path,
    rel: str,
    content: str,
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
    Then scaled by _rank_multiplier() for the note's kind.
    """
    fm = _frontmatter(content)
    body = content[len(fm) + 8:] if fm else content

    def norm(s: str) -> str:
        return s if case_sensitive else s.lower()

    score = 0.0
    query_terms = norm(query).split()

    # Title match (first heading)
    title_match = re.search(r"^#\s+(.+)", content, re.MULTILINE)
    if title_match:
        title = norm(title_match.group(1))
        for term in query_terms:
            if term in title:
                score += 10.0

    # Frontmatter match
    if fm:
        fm_search = norm(fm)
        for term in query_terms:
            if term in fm_search:
                score += 5.0

    # Body keyword match (frontmatter excluded -- it already scored above)
    body_search = norm(body)
    for term in query_terms:
        score += min(body_search.count(term), 20)  # cap per term

    # Backlink bonus: does this file link to anything matching query terms?
    links = _extract_links(content)
    for link_target in links:
        # Normalize: remove path prefix, keep filename
        target_name = norm(link_target.split("|")[-1].split("/")[-1])
        for term in query_terms:
            if term in target_name:
                score += 2.0
                break  # one bonus per link

    # Reverse link bonus: does anything matching query link TO this file?
    filename = norm(file_path.stem)
    for term in query_terms:
        if term in filename:
            # Files that appear in link_index pointing to this file get bonus
            for link_key in link_index:
                if term in norm(link_key) and rel in link_index[link_key]:
                    score += 1.5
                    break

    if score <= 0:
        return (0.0, "")
    # Snippet from the body only: query terms very often occur in the
    # frontmatter too, and a snippet of `name:`/`description:` just repeats
    # what `title` already carries.
    return (score * _rank_multiplier(file_path, fm), _snippet(body, query))


def _build_link_index(contents: dict[str, str]) -> dict[str, list[str]]:
    """Build index: for each link target, which files link to it.

    Returns {link target: [list of vault-relative paths that link to it]}
    """
    index: dict[str, list[str]] = {}
    for rel, content in contents.items():
        for link in _extract_links(content):
            # Normalize link target
            target = link.split("|")[0].strip()  # remove alias
            index.setdefault(target, []).append(rel)
    return index


def _read_all(files: list[Path], vault: Path) -> dict[str, str]:
    """Read every file once, keyed by vault-relative path.

    Search used to read the whole vault three times per call (link index,
    scoring, then the top-N pass); one pass feeds all three.
    """
    contents: dict[str, str] = {}
    for f in files:
        try:
            contents[_relative_path(f, vault)] = f.read_text(errors="replace")
        except Exception:
            continue
    return contents


@server.tool()
def vault_search(
    query: str,
    vault_dir: str | None = None,
    limit: int = DEFAULT_LIMIT,
    subdir: str | None = None,
    case_sensitive: bool = False,
    frontmatter: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Search an Obsidian vault by keyword with backlink-aware ranking.

    Searches markdown files for the given query terms, ranking results by:
    - Title/heading matches (highest weight)
    - Frontmatter matches
    - Body text frequency
    - Wikilink connections to matching files
    Notes carrying `last_compiled:` (curated, human-read articles) rank above
    raw append-only mem-lite mirror logs covering the same material.

    Args:
        query: Search terms (space-separated). Each term is scored independently.
        vault_dir: Absolute path to the Obsidian vault root. Falls back to VAULT_DIR env var.
        limit: Maximum number of results to return (default 20).
        subdir: Optional subdirectory within vault to search (e.g. "08- Wiki").
        case_sensitive: Whether to use case-sensitive matching (default False).
        frontmatter: Only search notes whose frontmatter has these key/value pairs,
            e.g. {"mem_lite_project": "workspace--myrepo"} to find the compiled note
            for a repo rather than guessing its path.

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

    contents = _read_all(files, vault)

    # Build reverse link index for backlink scoring -- over the whole vault, so
    # a frontmatter filter narrows the results without blinding the ranking.
    link_index = _build_link_index(contents)

    if frontmatter:
        contents = {
            rel: c for rel, c in contents.items()
            if _matches_frontmatter(_frontmatter(c), frontmatter)
        }

    # Score all files
    scored: list[tuple[float, Path, str, str]] = []
    for rel, content in contents.items():
        f = vault / rel
        s, snippet = _score_file(f, rel, content, query, link_index, case_sensitive)
        if s > 0:
            scored.append((s, f, rel, snippet))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    results = []
    for score, f, rel, snippet in top:
        content = contents[rel]
        title_m = re.search(r"^#\s+(.+)", content, re.MULTILINE)
        title = title_m.group(1) if title_m else f.name
        links = _extract_links(content)[:10]  # first 10 links

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
def vault_read(
    path: str,
    vault_dir: str | None = None,
    section: str | None = None,
) -> dict[str, Any]:
    """Read a note from the vault, optionally just one section of it.

    vault_search returns vault-relative paths; pass one straight back here
    instead of rebuilding an absolute path to hand to a file-reading tool.
    Prefer `section` when the search snippet already told you which heading
    holds the answer -- a compiled note runs to hundreds of lines.

    Args:
        path: Vault-relative path, as returned by vault_search / vault_list.
        vault_dir: Absolute path to the Obsidian vault root. Falls back to VAULT_DIR env var.
        section: Optional heading to return, matched case-insensitively on its
            text (e.g. "Bilinen Riskler" or "## Bilinen Riskler"). Returns that
            heading down to the next one at the same or higher level.

    Returns:
        dict with 'path', 'content', and 'section' (None if the whole note).
    """
    try:
        vault = _resolve_vault(vault_dir)
    except ValueError as e:
        return {"error": str(e), "path": path}

    target = (vault / path).resolve()
    if not target.is_relative_to(vault):
        return {"error": f"Path escapes the vault root: {path}", "path": path}
    if not target.is_file():
        return {"error": f"No such note: {path}", "path": path}

    try:
        content = target.read_text(errors="replace")
    except OSError as e:
        return {"error": f"Could not read {path}: {e}", "path": path}

    if not section:
        return {"path": path, "section": None, "content": content}

    wanted = section.lstrip("#").strip().lower()
    lines = content.splitlines()
    start = None
    level = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s+(.+)", line)
        if not m:
            continue
        if start is None:
            if m.group(2).strip().lower() == wanted:
                start, level = i, len(m.group(1))
        elif len(m.group(1)) <= level:
            return {"path": path, "section": section, "content": "\n".join(lines[start:i]).rstrip()}
    if start is None:
        headings = [m.group(1) for m in (re.match(r"^#+\s+(.+)", ln) for ln in lines) if m]
        return {"error": f"No section {section!r} in {path}", "path": path, "headings": headings}
    return {"path": path, "section": section, "content": "\n".join(lines[start:]).rstrip()}


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
