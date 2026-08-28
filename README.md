# obsidian-vault-bridge

Optional Obsidian/markdown-vault integration for [dev-agent-kit](https://github.com/alkrcaaa/dev-agent-kit)
(Claude Code + Qwen Code agent setup). Split out as its own module because it's a
personal note-taking workflow choice, not core to the agent-orchestration kit.

Everything here is **opt-in** and **fail-open**: unset the relevant env var and the
piece is a silent no-op.

Design decisions behind these pieces — and the measurements that settled them —
live in [DECISIONS.md](DECISIONS.md). Read it before changing a hook's contract.

## Pieces

- `hooks/obsidian-mirror.py` — PostToolUse hook on `mcp__mem-lite__mem_save`. Mirrors
  every mem-lite save as a raw markdown append into
  `$MEM_OBSIDIAN_VAULT/<project>/<YYYY-MM-DD>.md`. mem-lite's SQLite DB stays the
  source of truth; this is a read-only mirror for anyone who keeps notes in an
  Obsidian-style vault.
- `tools/vault-compile.py` — not a hook. A daily systemd user timer (installed by
  dev-agent-kit's `install.sh`) runs `--all --apply` outside any session: it
  rewrites the managed sections of every `Code/<repo>.md` from mem-lite, refreshes
  the profile card from what `personal-capture` collected, and links the raw
  `_mem-log` day files it drew on so they do not sit in the vault as orphans.
  This replaced `compile-nudge`, a hook that asked the agent to compile and was
  acted on zero times in three firings — see D3 in `DECISIONS.md`.
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
- `hooks/concept-capture.py` — Stop hook, the write half for *concepts*. The repo
  path learns about code and the profile path learns about the user; neither
  catches a concept explained at length in the middle of ordinary work, which
  lives in the transcript and dies with it. Two measurements set its shape.
  There is no "teaching session" to detect — over 90 days, 4 of 69 sessions
  carried more learning signal than work signal, and the session where
  Kubernetes actually got taught scored 4 learning cues against 38 work ones —
  so it classifies content, not sessions. And no cheap structural trigger
  exists: a long assistant turn that calls no tool is present in 85% of
  sessions, because that is also how an agent reports finished work. So a local
  model reads the session's prose turns and **extracts** the passages that
  explain something repo-independent. It does not write the article: the
  explanation was already written by the strong model the user was talking to,
  and a 27B asked to synthesise a concept is at its ceiling — worst exactly in
  a note read in order to learn. Output lands under `VAULT_WIKI_DIR`
  (default `08- Wiki`) in a note stamped `status: draft` / `auto_compiled:
  true`, in its own dated section; promotion to a real article stays a human or
  strong-model decision. The model names the note, so the name is treated as
  untrusted: path separators are refused, existing titles are offered back so it
  reuses one, and a note carrying `mem_lite_project:` or `agent_profile: true`
  is never written to. Opt-in via `VAULT_DIR` + `QWEN_BASE_URL`.
- `tools/vault-lint.py` — not a hook, run manually or on a cron. The three checks
  CLAUDE.md Section 5 / Vault Standards.md's "Haftalık Bakım" already name, as a
  script instead of an ad hoc read: stale `compiled: false` raw notes, broken
  `[[wikilink]]`s (resolved against notes and attachments alike), and
  "sınıfsız/başlıksız" notes (root-level strays, `07- Raw` notes missing required
  frontmatter, `08- Wiki` notes with no heading). Read-only, always — it reports,
  it never edits or deletes. See D16 in `DECISIONS.md`.
- `hooks/vault_common.py` — the project-key, note lookup, and locked-append
  helper all three write-side hooks share. A key resolved differently in one
  hook than the other fails silently (the note is simply never found), so it
  exists once; the same is true of `locked_note()`, which every append point
  goes through so two hosts (or two overlapping sessions) writing the same
  note at once cannot interleave and corrupt it (D15).
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

Env alone does not reach a hook reliably. Hooks are spawned by the agent
process, which inherits whatever shell launched it: exporting a variable in a
shell rc reaches sessions started from that shell afterwards and nothing else,
and a variable set only inside an MCP server's `env` block is private to that
server. Both failures look identical to "switched off" — `compile-nudge` shipped
that way and did nothing for weeks. So `VAULT_DIR` and `QWEN_BASE_URL` are also
read from `~/.config/dev-agent-kit/vault.env` (`KEY=value` per line), written by
`install.sh` from the installing shell's env. Env wins when both are present.

- `MEM_OBSIDIAN_VAULT` — write target for `obsidian-mirror.py`. Typically a
  subfolder of your vault, e.g. `<vault>/_mem-log`. Env only; mem-lite runs it,
  not the bridge.
- `VAULT_DIR` — read target for `vault-search` and every hook except the mirror.
  The vault root. Separate from `MEM_OBSIDIAN_VAULT` on purpose: one is where the
  mirror writes, the other is what gets searched/read back. When it resolves to
  nothing, `vault-inject` records a `skip`/`no-vault-dir` metric once per session
  so `hook-stats.sh` can tell "off" apart from "broken".
- `QWEN_BASE_URL` — OpenAI-compatible endpoint for `personal-capture.py` and
  `concept-capture.py`, e.g. a local vLLM at `http://host:8002/v1`. Unset means
  both hooks are off. `PERSONAL_CAPTURE_MODEL` overrides the model id;
  `CONCEPT_CAPTURE_MODEL` overrides it for concept capture alone.
- `VAULT_WIKI_DIR` — folder `concept-capture.py` files concept notes in,
  relative to `VAULT_DIR` (or absolute). Defaults to `08- Wiki`. It is the only
  place that hook may write, so a folder that does not exist switches it off
  rather than creating one.
