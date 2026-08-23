# vault-search

Obsidian vault search MCP server — keyword search with backlink-aware ranking.

## What it does

Searches markdown files in an Obsidian vault, ranking results by:

1. **Title/heading match** (10x weight) — query terms in the file's `# Title`
2. **Frontmatter match** (5x weight) — query terms in YAML frontmatter
3. **Body keyword frequency** (1x per occurrence, capped at 20)
4. **Wikilink connections** (+2 per `[[link]]` that contains query terms)

The total is then scaled by what kind of note it is: a curated note carrying
`last_compiled:` in its frontmatter scores ×1.5, and a raw append-only log under
`MEM_OBSIDIAN_VAULT` (what `obsidian-mirror.py` writes) scores ×0.5. Without that,
the raw mem-lite record outranks the article it was compiled into — backwards for
a raw → compile → wiki flow.

## Tools

### `vault_search`

Search the vault for query terms.

**Parameters:**
- `query` (str, required): Space-separated search terms
- `vault_dir` (str, optional): Absolute path to vault root. Falls back to `VAULT_DIR` env var.
- `limit` (int, default 20): Max results
- `subdir` (str, optional): Search only within a subdirectory (e.g. `"08- Wiki"`)
- `case_sensitive` (bool, default False): Case-sensitive matching
- `frontmatter` (dict, optional): Only search notes whose frontmatter carries these
  key/value pairs, e.g. `{"mem_lite_project": "workspace--myrepo"}` to land on a
  repo's compiled note directly — the same key `compile-nudge.py` matches on.

**Returns:** `{query, vault, total_matches, returned, results: [{path, score, title, snippet, links}]}`

`snippet` is drawn from the note body around the first matching term (the whole
query first, then each term), never from the frontmatter.

### `vault_read`

Read a note back — `vault_search` returns vault-relative paths, and this takes one
straight back, so nothing has to rebuild an absolute path for a file-reading tool.

**Parameters:**
- `path` (str, required): Vault-relative path from `vault_search` / `vault_list`
- `vault_dir` (str, optional): Vault root path
- `section` (str, optional): Return only one heading's section (e.g. `"Known Risks"`
  or `"## Known Risks"`), down to the next heading at the same or higher level.
  Compiled notes run to hundreds of lines; pull the one section you need.

**Returns:** `{path, section, content}`, or `{error, headings}` if the section is
missing, so a near-miss on the heading name is one retry rather than a dead end.

### `vault_list`

List files in the vault, optionally filtered.

**Parameters:**
- `vault_dir` (str, optional): Vault root path
- `subdir` (str, optional): Subdirectory to list
- `pattern` (str, optional): Glob pattern (e.g. `"*.md"`, `"Karagag*"`)
- `include_links` (bool, default False): Include `[[wikilink]]` targets
- `limit` (int, default 50): Max entries returned

**Returns:** `{vault, subdir, pattern, total, count, truncated, files: [{path, title, size, links?}]}`

Prefer `vault_search` for finding something — it ranks and explains each hit. Reach for
`vault_list` to see what a folder holds, and narrow with `subdir`/`pattern` rather than
raising `limit`. Measured on a 165-note vault: an unfiltered listing cost ~4.9k tokens and
was being called almost as often as search, so most of that was paths nobody read. The
default cap brings it to ~1.7k, and `subdir` to ~0.6k.

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
