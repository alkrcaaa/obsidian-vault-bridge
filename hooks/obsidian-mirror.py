#!/usr/bin/env python3
"""
PostToolUse(mcp__mem-lite__mem_save) hook: mirror saves into a markdown vault.

mem-lite is the source of truth (SQLite, `~/.claude-mem-lite`) — this hook
does not change that. It only appends a copy of each saved observation as a
plain markdown entry under a dated file, so anyone who keeps notes in an
Obsidian-style vault (or any plain folder of `.md` files) gets an ambient,
append-only raw log per project without a second manual step.

Off by default: unset MEM_OBSIDIAN_VAULT and this hook is a silent no-op. Set
it to a directory and every mem_save lands at:

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

SAVE_RE = re.compile(r'Saved as observation #(\d+) \[(\w+)\] in project "([^"]+)"')


def _response_text(tool_response):
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, list):
        texts = [p.get("text", "") for p in tool_response if isinstance(p, dict)]
        if texts:
            return "\n".join(texts)
    if isinstance(tool_response, dict):
        parts = tool_response.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        if texts:
            return "\n".join(texts)
    return json.dumps(tool_response, default=str)


def main():
    vault = os.environ.get("MEM_OBSIDIAN_VAULT")
    if not vault:
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "mcp__mem-lite__mem_save":
        sys.exit(0)

    try:
        text = _response_text(data.get("tool_response"))
        m = SAVE_RE.search(text)
        if not m:
            sys.exit(0)  # dedup skip, error, or unrecognised response shape
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
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
