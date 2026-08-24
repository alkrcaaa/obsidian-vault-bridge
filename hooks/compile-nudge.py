#!/usr/bin/env python3
"""
compile-nudge hook (UserPromptSubmit).

mem-lite auto-injects raw memory into every session — that half of the
knowledge-base loop is already automatic. The other half, Karpathy's "LLM
compile" step (raw records -> a curated note a human reads), is not: it
only happens when the user remembers to ask for it. Left alone, a compiled
note quietly drifts from what mem-lite actually knows and nobody notices
until they trip over the gap.

Opt-in, same signal as vault-search: no-op unless VAULT_DIR is set. When it
is, and the current repo's compiled note in the vault is behind, nudge once
per project per day rather than staying silent (compile-nudge writes
nothing itself; the fix is one of the agent's own turns).

Where "behind" is decided:
  1. Resolve this session's project key the same way mem-lite's own
     inferProject() does (git root, not just the immediate parent dir) --
     see ~/.claude-mem-lite/utils.mjs. Keeping this in sync matters: a
     different key here would silently never find the note or always miss
     the count.
  2. Find the vault note carrying `mem_lite_project: <key>` in its
     frontmatter -- the existing Code/<repo>.md convention. No note yet?
     Stay silent; deciding where a new repo's note belongs is a user
     decision (see the vault's own CLAUDE.md Section 7), not this hook's.
  3. Compare `last_compiled:` in that note against mem-lite's own DB: how
     many observations landed for this project since then.

Thresholds (MIN_NEW_OBS=8, MAX_DAYS=14): either enough new material has
piled up to be worth a synthesis pass, or enough time has passed that
"nothing changed" stops being a safe assumption even at a low rate.

Fail-open, always: any missing piece (no VAULT_DIR, no note, DB unreadable)
exits 0 silently.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vault_common import find_note, infer_project, record_metric
except Exception:
    sys.exit(0)

MIN_NEW_OBS = 8
MAX_DAYS = 14
# A repo with no note at all: wait for enough history that a note would have
# something to say, so a throwaway clone never triggers this.
BOOTSTRAP_MIN_OBS = 10

MEM_DB = os.path.join(os.path.expanduser("~"), ".claude-mem-lite", "claude-mem-lite.db")


def _last_compiled(head):
    m = re.search(r"^last_compiled:\s*(\S+)", head, re.MULTILINE)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _obs_count(project, since=None):
    """Observations mem-lite holds for this project, all of them or since a date."""
    con = None
    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=2)
        cur = con.cursor()
        if since is None:
            cur.execute("SELECT COUNT(*) FROM observations WHERE project = ?", (project,))
        else:
            cur.execute(
                "SELECT COUNT(*) FROM observations WHERE project = ? AND created_at > ?",
                (project, since.strftime("%Y-%m-%dT%H:%M:%S")),
            )
        return cur.fetchone()[0]
    except Exception:
        return 0
    finally:
        if con is not None:
            con.close()


def _notify_once(project, kind):
    """True the first time today, False afterwards -- once per project per day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    marker = os.path.join(tempfile.gettempdir(), f"{kind}-{project}-{today}.notified")
    if os.path.exists(marker):
        return False
    try:
        open(marker, "w").close()
    except OSError:
        pass
    return True


def _emit(user_msg, model_ctx):
    print(json.dumps({
        "systemMessage": user_msg,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": model_ctx,
        },
    }))
    sys.exit(0)


def main():
    vault_dir = os.environ.get("VAULT_DIR")
    if not vault_dir or not os.path.isdir(vault_dir) or not os.path.isfile(MEM_DB):
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cwd = data.get("cwd") or os.getcwd()
    project = infer_project(cwd)

    note_path, head = find_note(vault_dir, f"mem_lite_project: {project}")
    if not note_path:
        # No note yet. Where a new one belongs is a user decision, so this hook
        # still writes nothing -- but staying silent meant a repo with no note
        # never got one either: no note, no nudge, no note. Once the project has
        # accumulated real history, say so and let the user place it.
        total = _obs_count(project)
        if total < BOOTSTRAP_MIN_OBS or not _notify_once(project, "vault-bootstrap"):
            sys.exit(0)
        record_metric("compile-nudge", "bootstrap", cwd, f"{total}obs")
        _emit(
            f"📓 {project} has {total} mem-lite entries but no vault note yet — "
            "nothing is being compiled for this repo.",
            f"[compile-nudge] mem-lite holds {total} observations for this project and "
            "no vault note carries `mem_lite_project: " + project + "`, so none of it is "
            "being compiled and nothing gets injected at session start. Ask the user "
            "which folder the note belongs in (the vault's own CLAUDE.md owns that "
            "mapping — do not guess), then create it with `mem_lite_project` and "
            "`last_compiled` frontmatter.",
        )

    last_compiled = _last_compiled(head)
    if last_compiled is None:
        sys.exit(0)  # malformed/missing date -- don't guess, don't nag

    days_stale = (datetime.now(timezone.utc) - last_compiled).days
    new_obs = _obs_count(project, last_compiled)

    if new_obs < MIN_NEW_OBS and days_stale < MAX_DAYS:
        sys.exit(0)

    if not _notify_once(project, "compile-nudge"):
        sys.exit(0)

    rel_note = os.path.relpath(note_path, vault_dir)
    user_msg = (
        f"📓 {project}'s compiled note ({rel_note}) is behind: {new_obs} new "
        f"mem-lite entries, last compiled {days_stale}d ago. Worth a synthesis pass."
    )
    model_ctx = (
        f"[compile-nudge] The vault note for this project ({rel_note}) has not been "
        f"recompiled from mem-lite in {days_stale} days ({new_obs} new observations "
        "since). If the user agrees, summarize what's new since last_compiled into "
        "that note and update its last_compiled date -- don't do this silently."
    )

    record_metric("compile-nudge", "nudge", cwd, f"{new_obs}obs/{days_stale}d")
    _emit(user_msg, model_ctx)


if __name__ == "__main__":
    main()
