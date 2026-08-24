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
  synthesis, only with the user's go-ahead. Opt-in via `VAULT_DIR`. Also speaks up
  when a project has 10+ observations and no note at all — otherwise a repo with no
  note produced no nudge and so never got one.
- `hooks/vault-inject.py` — SessionStart hook, the read half of the loop. Injects the
  current repo's compiled note (matched on the same `mem_lite_project` key) and, if a
  note opts in with `agent_profile: true`, a profile card describing the user. What
  gets injected is the slice between `<!-- agent-card:start -->` and
  `<!-- agent-card:end -->`, capped (~1400/~1100 chars); a repo note with no markers
  falls back to its first `##` section, a profile note without them injects nothing.
  The bound is the point: this rides in every request of the session, so it holds the
  minimum needed to start work and leaves the rest to `vault_read`. Opt-in via
  `VAULT_DIR`.
- `hooks/personal-capture.py` — Stop hook, the write half for facts about the
  *user*. Those never arrive as a tool call the way a repo fact does; they arrive
  as prose in a prompt, so the only thing that can notice one is a model. At the
  end of a session this sends what the user typed (never tool output, never the
  assistant's words, never anything that looks like a credential) to a local
  OpenAI-compatible endpoint (`QWEN_BASE_URL`) and appends up to three durable
  facts to the profile note, each stamped with the date. It writes into its own
  section and never into the `agent-card` block: that block is injected into
  every later session, so an unsupervised write there would amplify a bad
  inference indefinitely. The model call runs detached — a Stop hook otherwise
  holds up the end of the turn. Opt-in via `VAULT_DIR` + `QWEN_BASE_URL`.
- `hooks/vault_common.py` — the project-key and note lookup both hooks share. A key
  resolved differently in one hook than the other fails silently (the note is simply
  never found), so it exists once.
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
- `VAULT_DIR` — read target for `vault-search` and every hook except the mirror.
  The vault root. Separate from `MEM_OBSIDIAN_VAULT` on purpose: one is where the
  mirror writes, the other is what gets searched/read back. It must be exported
  where non-interactive processes see it (`~/.zshenv`, not `~/.zshrc`) — set only
  in an MCP server's own env block, the hooks never see it and quietly do nothing.
- `QWEN_BASE_URL` — OpenAI-compatible endpoint for `personal-capture.py`, e.g. a
  local vLLM at `http://host:8002/v1`. Unset means that hook is off.
  `PERSONAL_CAPTURE_MODEL` overrides the model id.
