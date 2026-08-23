# obsidian-vault-bridge

Optional Obsidian/markdown-vault integration for [dev-agent-kit](https://github.com/alkrcaaa/dev-agent-kit)
(Claude Code + Qwen Code agent setup). Split out as its own module because it's a
personal note-taking workflow choice, not core to the agent-orchestration kit.

Everything here is **opt-in** and **fail-open**: unset the relevant env var and the
piece is a silent no-op.

## Pieces

- `hooks/obsidian-mirror.py` — PostToolUse hook on `mcp__mem-lite__mem_save`. Mirrors
  every mem-lite save as a raw markdown append into
  `$MEM_OBSIDIAN_VAULT/<project>/<YYYY-MM-DD>.md`. mem-lite's SQLite DB stays the
  source of truth; this is a read-only mirror for anyone who keeps notes in an
  Obsidian-style vault.
- `hooks/compile-nudge.py` — UserPromptSubmit hook. If the vault has a compiled note
  for the current project (frontmatter `mem_lite_project: <key>`) and it's fallen
  behind mem-lite (8+ new observations or 14+ days since `last_compiled`), nudges
  once per project per day. Never writes anything itself — the agent does the
  synthesis, only with the user's go-ahead. Opt-in via `VAULT_DIR`.
- `mcp-infra/vault-search/` — FastMCP server, keyword + backlink search over an
  Obsidian vault (`vault_search`, `vault_read`, `vault_list`). Ranks curated notes
  above the raw mirror logs below, and `vault_read` takes a search hit's path back
  directly — optionally just one section of it. Opt-in via `VAULT_DIR`.
  Deliberately **not** auto-injected into every prompt — call it only when a
  question needs vault context, to avoid ambient per-prompt token cost.

## Wiring into dev-agent-kit

Pulled in as a git submodule at `extern/obsidian-vault-bridge`. dev-agent-kit's
deploy pipeline (`scripts/lib/agents.sh::_deploy_extern_symlink`) already symlinks
the whole `extern/` tree into `~/.claude/extern` (and the Qwen equivalent), so no
extra deploy step is needed — only the hook `command` paths in
`claude/settings.json` / `qwen/settings.json` and the MCP config generator in
`scripts/lib/mcp.sh` need to point here instead of `claude/hooks/` /
`mcp-infra/vault-search/`.

## Env vars

- `MEM_OBSIDIAN_VAULT` — write target for `obsidian-mirror.py`. Typically a
  subfolder of your vault, e.g. `<vault>/_mem-log`.
- `VAULT_DIR` — read target for `vault-search` and `compile-nudge.py`. The vault
  root. Separate from `MEM_OBSIDIAN_VAULT` on purpose: one is where the mirror
  writes, the other is what gets searched/read back.
