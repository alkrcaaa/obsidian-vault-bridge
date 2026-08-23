# vault-search

Obsidian vault search MCP server — keyword search with backlink-aware ranking.

## What it does

Searches markdown files in an Obsidian vault, ranking results by:

1. **Title/heading match** (10x weight) — query terms in the file's `# Title`
2. **Frontmatter match** (5x weight) — query terms in YAML frontmatter
3. **Body keyword frequency** (1x per occurrence, capped at 20)
4. **Wikilink connections** (+2 per `[[link]]` that contains query terms)

## Tools

### `vault_search`

Search the vault for query terms.

**Parameters:**
- `query` (str, required): Space-separated search terms
- `vault_dir` (str, optional): Absolute path to vault root. Falls back to `VAULT_DIR` env var.
- `limit` (int, default 20): Max results
- `subdir` (str, optional): Search only within a subdirectory (e.g. `"08- Wiki"`)
- `case_sensitive` (bool, default False): Case-sensitive matching

**Returns:** `{query, vault, total_matches, returned, results: [{path, score, title, snippet, links}]}`

### `vault_list`

List files in the vault, optionally filtered.

**Parameters:**
- `vault_dir` (str, optional): Vault root path
- `subdir` (str, optional): Subdirectory to list
- `pattern` (str, optional): Glob pattern (e.g. `"*.md"`, `"Karagag*"`)
- `include_links` (bool, default False): Include `[[wikilink]]` targets

**Returns:** `{vault, subdir, pattern, count, files: [{path, title, size, links?}]}`

## Install

```bash
cd mcp-infra/vault-search
./install.sh --vault-dir /path/to/your/obsidian-vault
```

## Configure

### Claude (stdio)

Add to `~/.claude/claude.json` MCP block:

```json
"vault-search": {
  "command": "/path/to/dev-agent-kit/mcp-infra/vault-search/.venv/bin/python3",
  "args": ["/path/to/dev-agent-kit/mcp-infra/vault-search/server.py"],
  "env": {"VAULT_DIR": "/path/to/your/obsidian-vault"}
}
```

### Qwen (stdio)

Add to `~/.qwen/settings.json` MCP block (same format as Claude).

## Example

```
Agent: vault_search(query="project X network integration", vault_dir="/path/to/your/vault")
→ Returns: [
    {path: "02- Kuartis/01- Karagag/Karagag İntegration/...", score: 28.5, title: "...", snippet: "..."},
    {path: "08- Wiki/İkinci Beyin Projesi.md", score: 12.0, ...},
    ...
  ]
```

## Design

- **No dependencies beyond FastMCP** — pure Python, no external search library
- **Skips** `.obsidian`, `.git`, `.smart-env`, `__pycache__`, etc.
- **Fail-open** — missing vault or bad path returns actionable error, never crashes
- **Budget-aware** — default 20 results, snippet truncated to 200 chars
