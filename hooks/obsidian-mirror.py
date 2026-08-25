#!/usr/bin/env python3
"""
PostToolUse(mcp__mem-lite__mem_save) hook: mirror saves into a markdown vault.

mem-lite is the source of truth (SQLite, `~/.claude-mem-lite`) — this hook
does not change that. It only appends a copy of each saved observation as a
plain markdown entry under a dated file, so anyone who keeps notes in an
Obsidian-style vault (or any plain folder of `.md` files) gets an ambient,
append-only raw log per project without a second manual step.

Off by default: with MEM_OBSIDIAN_VAULT set neither in the environment nor in
the installer's config file, this hook is a silent no-op. Point it at a
directory and every mem_save lands at:

    $MEM_OBSIDIAN_VAULT/<project>/<YYYY-MM-DD>.md

one file per project per day, entries appended as they happen. This is
deliberately a *raw* log, not a curated note — one project, one growing file
per day. Turning that into backlinked wiki articles, folder hierarchies, or
anything else is a workflow you build on top in your own vault; this hook's
only job is getting the data out of SQLite and onto disk as text.

Fail-open, always: memory mirroring is an enhancement, never a gate. Any
missing field, IO error, or malformed payload exits 0 silently rather than
surfacing a tool error.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vault_common import env_or_conf, record_metric
except Exception:
    sys.exit(0)

SAVE_RE = re.compile(r'Saved as observation #(\d+) \[(\w+)\] in project "([^"]+)"')


def _response_text(tool_response):
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        texts = [p.get("text", "") for p in tool_response if isinstance(p, dict)]
        if texts:
            return "\n".join(texts)
    if isinstance(tool_response, dict):
        # Claude spells the parts list `content`; Qwen spells it `llmContent`.
        # Missing the second one did not fail loudly -- it fell through to the
        # json.dumps() below, which escapes every quote, so the regex that
        # looks for `project "<name>"` saw `project \"<name>\"` and matched
        # nothing. Every Qwen save was silently mirrored as `unparsed`.
        parts = tool_response.get("content") or tool_response.get("llmContent") or []
        if isinstance(parts, str):
            return parts
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        if texts:
            return "\n".join(texts)
    return json.dumps(tool_response, default=str)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # Match on the tool, not on one host's spelling of it. Qwen wires this
    # PostToolUse on `mcp__mem-lite__.*` and an exact-name compare is the shape
    # of bug that made personal-capture dispatch every session and find
    # nothing. Every other mem-lite tool still falls through here untouched,
    # which is why nothing is recorded before this line -- a metric here would
    # fire on every search and recall.
    if not str(data.get("tool_name") or "").endswith("mem_save"):
        sys.exit(0)

    # Env first, then the installer's config file. Reading only the env meant
    # this hook was enabled by a hand-written `export` in a shell rc: it fired
    # for sessions started from that shell and silently mirrored nothing
    # everywhere else, which is the same failure VAULT_DIR already had.
    vault = env_or_conf("MEM_OBSIDIAN_VAULT")
    if not vault:
        record_metric("obsidian-mirror", "skip", os.getcwd(), "no-mirror-dir")
        sys.exit(0)

    try:
        text = _response_text(data.get("tool_response"))
        m = SAVE_RE.search(text)
        if not m:
            # A dedup skip, an error, or a response shape this regex does not
            # know. The third is a silent break, so say so rather than exiting
            # the way a successful skip does.
            record_metric("obsidian-mirror", "skip", os.getcwd(), "unparsed")
            sys.exit(0)
        obs_id, obs_type, project = m.group(1), m.group(2), m.group(3)

        tool_input = data.get("tool_input") or {}
        title = tool_input.get("title") or f"observation #{obs_id}"
        content = tool_input.get("content") or ""
        lesson = tool_input.get("lesson_learned") or ""
        files = tool_input.get("files") or []

        now = datetime.now(timezone.utc).astimezone()
        day_dir = os.path.join(vault, project)
        os.makedirs(day_dir, exist_ok=True)
        day_file = os.path.join(day_dir, f"{now.strftime('%Y-%m-%d')}.md")

        lines = [f"### {now.strftime('%H:%M')} — [{obs_type}] {title} (#{obs_id})", ""]
        if lesson:
            lines += [f"**Lesson:** {lesson}", ""]
        if content:
            lines += [content.strip(), ""]
        if files:
            lines += [f"Files: {', '.join(files)}", ""]
        lines += ["---", ""]

        is_new = not os.path.exists(day_file)
        with open(day_file, "a", encoding="utf-8") as f:
            if is_new:
                f.write(f"# {project} — {now.strftime('%Y-%m-%d')}\n\n")
            f.write("\n".join(lines) + "\n")
    except Exception:
        record_metric("obsidian-mirror", "skip", os.getcwd(), "write-error")
        sys.exit(0)

    record_metric("obsidian-mirror", "mirror", os.getcwd(), obs_type)
    sys.exit(0)


if __name__ == "__main__":
    main()
