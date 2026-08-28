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

Run with `--reconcile` (wired on Stop) the same script instead diffs mem-lite
against the vault for the session's project and writes whatever is missing.
That is the path that survives: a save made through the CLI rather than the
MCP tool -- which is what the agent reaches for whenever the `mem_*` tools are
deferred behind a tool search -- fires no PostToolUse at all, so the tool hook
above never learns it happened. See reconcile() for why the recovery diffs
state rather than learning a second trigger.

Fail-open, always: memory mirroring is an enhancement, never a gate. Any
missing field, IO error, or malformed payload exits 0 silently rather than
surfacing a tool error.
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vault_common import (
        CATCHALL_PROJECT, env_or_conf, infer_project, locked_note,
        mem_lite_key, record_metric,
    )
except Exception:
    sys.exit(0)

SAVE_RE = re.compile(r'Saved as observation #(\d+) \[(\w+)\] in project "([^"]+)"')
ID_RE = re.compile(r"\(#(\d+)\)")

MEM_DB = os.path.join(os.path.expanduser("~"), ".claude-mem-lite", "claude-mem-lite.db")

# How far back the reconcile pass looks. A save this hook missed is only worth
# recovering while the note it belongs to is still being written; past that the
# row is still in mem-lite and still searchable, and back-filling it would drop
# weeks of history into a vault whose contract is "what is true now".
RECONCILE_DAYS = 7


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


def scope_of(cwd):
    """(short tag, ~-relative path) for the directory a save was made in.

    Only the catch-all needs this. A repo's folder name already says where its
    entries came from; the catch-all deliberately collects every repo-less
    session into one folder, and a day file there can mix a self-hosted MDM
    PoC with a bringup session driving a remote machine, with nothing above
    the body text to tell them apart. Splitting the folder instead would undo
    the reason it exists -- each shard falls under the note threshold, so none
    is ever compiled and all of it goes unreachable.

    The directory is the only locator this hook can state as fact. Which
    remote host the work actually touched lives in the body, because the hook
    runs locally and cannot know it; the model writing the save is what has
    that, and does record it.
    """
    cwd = (cwd or "").rstrip("/")
    if not cwd:
        return "", ""
    home = os.path.expanduser("~")
    pretty = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
    return os.path.basename(cwd) or cwd, pretty


def write_entry(vault, project, when, obs_id, obs_type, title, content, lesson,
                files, scope=None):
    """Append one observation to `<vault>/<project>/<when:%Y-%m-%d>.md`.

    Both entry points land here so the two paths cannot drift into writing the
    same record two different ways -- the reconcile pass finds an entry by the
    `(#id)` this function stamps, so a second spelling of it would make every
    already-mirrored row look missing and duplicate the lot.

    Locked: the PostToolUse path and the reconcile pass can both write the
    same day file in the same second (a mem_save fired right as a stale
    session's Stop hook reconciles), and an unlocked pair of appends can
    interleave mid-write and corrupt the file, not just duplicate a line.
    """
    day_dir = os.path.join(vault, project)
    day_file = os.path.join(day_dir, f"{when.strftime('%Y-%m-%d')}.md")

    tag, pretty = scope_of(scope) if scope else ("", "")
    head = f"### {when.strftime('%H:%M')} — "
    head += f"{tag} · " if tag else ""
    lines = [f"{head}[{obs_type}] {title} (#{obs_id})", ""]
    if lesson:
        lines += [f"**Lesson:** {lesson}", ""]
    if content:
        lines += [content.strip(), ""]
    if files:
        lines += [f"Files: {', '.join(files)}", ""]
    if pretty:
        lines += [f"Scope: {pretty}", ""]
    lines += ["---", ""]

    with locked_note(day_file) as f:
        f.seek(0, os.SEEK_END)
        is_new = f.tell() == 0
        if is_new:
            f.write(f"# {project} — {when.strftime('%Y-%m-%d')}\n\n")
        f.write("\n".join(lines) + "\n")


def mirrored_ids(vault, project):
    """Observation ids already written under one project's folder."""
    found = set()
    day_dir = os.path.join(vault, project)
    try:
        names = os.listdir(day_dir)
    except OSError:
        return found
    for name in names:
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(day_dir, name), "r", encoding="utf-8",
                      errors="ignore") as f:
                found.update(ID_RE.findall(f.read()))
        except OSError:
            continue
    return found


def reconcile(data):
    """Mirror any recent save for this session's project that is not in the vault.

    The PostToolUse path only sees saves made through the `mem_save` *tool*.
    mem-lite is equally reachable as a CLI (`cli.mjs save`), which the agent
    picks whenever the MCP tools are deferred behind a search -- and that path
    fires no PostToolUse at all, so the save landed in SQLite and the vault
    never heard about it. Rather than teach the hook a second trigger to
    recognise (a third capture path would break it again in the same silence),
    this compares what mem-lite holds against what the vault holds and writes
    the difference. Capture path stops mattering.

    Scoped to the session's own project because the classification cannot be
    redone from a database row: mem-lite's key for a repo-less directory is
    whatever directory it started in, and only a live `cwd` says whether that
    should be folded into the catch-all. A row saved for some other repo is
    simply left for a session in that repo to pick up -- within RECONCILE_DAYS,
    every project heals itself the next time it is opened.

    The two keys are looked up separately for that same reason. A repo-less
    session is filed under the catch-all in the vault while mem-lite went on
    keying it by directory, so the folder to diff and the rows to diff it
    against have different names -- and a live `cwd` is exactly what supplies
    both. Skipping the catch-all instead left one class of save with no
    recovery path at all, which the consistency check found the same day.
    """
    vault = env_or_conf("MEM_OBSIDIAN_VAULT")
    if not vault:
        return "skip", "no-mirror-dir"

    cwd = data.get("cwd") or os.getcwd()
    project = infer_project(cwd)   # the vault folder
    key = mem_lite_key(cwd)        # what mem-lite wrote in the project column
    since = (datetime.now(timezone.utc) - timedelta(days=RECONCILE_DAYS)) \
        .strftime("%Y-%m-%dT%H:%M:%S")

    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT id, created_at, type, title, narrative, text, lesson_learned, "
            "files_modified FROM observations WHERE project = ? AND created_at > ? "
            "AND superseded_at IS NULL AND compressed_into IS NULL "
            "ORDER BY created_at ASC",
            (key, since),
        ).fetchall()
        con.close()
    except Exception:
        return "skip", "db-error"

    already = mirrored_ids(vault, project)
    written = 0
    for obs_id, created, otype, title, narrative, text, lesson, files_json in rows:
        if str(obs_id) in already:
            continue
        try:
            when = datetime.fromisoformat((created or "").replace("Z", "+00:00"))
            when = when.astimezone()
        except ValueError:
            continue
        try:
            files = json.loads(files_json) if files_json else []
        except (TypeError, ValueError):
            files = []
        write_entry(
            vault, project, when, obs_id, otype or "discovery",
            title or f"observation #{obs_id}", narrative or text or "",
            lesson or "", files if isinstance(files, list) else [],
            # Safe to stamp this session's cwd: the rows were selected by
            # mem-lite's key, which is derived from the directory, so a row
            # under this key was saved from this directory.
            scope=cwd if project == CATCHALL_PROJECT else None,
        )
        written += 1

    if not written:
        return "skip", "in-sync"
    return "reconcile", str(written)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if "--reconcile" in sys.argv[1:]:
        try:
            action, detail = reconcile(data)
        except Exception:
            action, detail = "skip", "reconcile-error"
        record_metric("obsidian-mirror", action, data.get("cwd") or os.getcwd(), detail)
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

        # mem-lite names a repo-less session after whatever directory it
        # started in, so the same class of work lands under a different key
        # every time -- `home--ali`, `ali--Downloads`, `scratchpad--smoke`.
        # The vault is what has to stay navigable, so the classification is
        # redone here and all of it is filed together. A session inside a repo
        # keeps mem-lite's key untouched: that one is already right, and it is
        # what the repo's note carries. `cwd` comes off the payload rather
        # than os.getcwd() because a hook is spawned wherever the agent
        # happens to be, which is not always the session's directory.
        cwd = data.get("cwd") or os.getcwd()
        if infer_project(cwd) == CATCHALL_PROJECT:
            project = CATCHALL_PROJECT

        tool_input = data.get("tool_input") or {}
        title = tool_input.get("title") or f"observation #{obs_id}"
        content = tool_input.get("content") or ""
        lesson = tool_input.get("lesson_learned") or ""
        files = tool_input.get("files") or []

        now = datetime.now(timezone.utc).astimezone()
        write_entry(vault, project, now, obs_id, obs_type, title, content,
                    lesson, files, scope=cwd if project == CATCHALL_PROJECT else None)
    except Exception:
        record_metric("obsidian-mirror", "skip", os.getcwd(), "write-error")
        sys.exit(0)

    record_metric("obsidian-mirror", "mirror", os.getcwd(), obs_type)
    sys.exit(0)


if __name__ == "__main__":
    main()
