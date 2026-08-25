#!/usr/bin/env python3
"""
Shared vault lookups for the bridge's hooks.

compile-nudge (is this repo's note behind?) and vault-inject (load this
repo's note at session start) ask the vault the same two questions: what is
this session's project key, and which note carries it. Answering them twice
would be two chances to drift apart -- and a key resolved differently in one
hook than the other fails silently: the note is simply never found, so the
hook just stays quiet and nobody learns it broke.

Nothing here writes. Every function is fail-open: on any missing piece it
returns None/empty rather than raising, because a knowledge layer is an
enhancement and must never be able to block a session.
"""
import os
import re
import subprocess

# A note can mark an explicit slice for the agent to load at session start.
# Everything between these markers is what gets injected -- the rest of the
# note stays on disk until something actually asks for it. HTML comments so
# Obsidian renders nothing.
CARD_START = "<!-- agent-card:start -->"
CARD_END = "<!-- agent-card:end -->"

# Where the installer records the vault path. Env alone is not a reliable
# channel for a hook: hooks are spawned by the agent process, which inherits
# whatever shell launched it. `VAULT_DIR` exported in ~/.zshenv reaches a
# session started from a fresh zsh and nothing else -- an agent launched from
# an older shell, a desktop entry or a non-zsh shell sees it unset, and every
# hook here then exits in silence. That is exactly how compile-nudge stayed
# dead from the day it shipped. Resolving through a file the installer writes
# makes the answer independent of how the agent happened to be started, and
# works the same on both hosts.
VAULT_CONF = os.path.expanduser("~/.config/dev-agent-kit/vault.env")


def env_or_conf(key):
    """Env first (so a caller can override), then the installer's file."""
    return os.environ.get(key) or _conf_value(key)


def vault_dir():
    """The configured vault root, or None if unset or not a directory."""
    value = env_or_conf("VAULT_DIR")
    return value if value and os.path.isdir(value) else None


def _conf_value(key):
    """One `KEY=value` out of the installer's config file, quotes stripped."""
    try:
        with open(VAULT_CONF, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line[len(key) + 1:].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def infer_project(cwd):
    """Mirror ~/.claude-mem-lite/utils.mjs::inferProject() -- git root first.

    Must stay in sync with mem-lite's own inference: this key is how a note
    in the vault is matched to the repo a session is running in.
    """
    root = cwd
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            root = result.stdout.strip()
    except Exception:
        pass
    root = root.rstrip("/")
    base = os.path.basename(root)
    parent = os.path.basename(os.path.dirname(root))
    raw = f"{parent}--{base}" if parent and parent not in (".", "/") else base
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)[:100]


def find_note(vault_dir, marker, read_bytes=1000, prefer=None):
    """Best (path, head) whose frontmatter carries `marker`, else (None, None).

    `read_bytes` bounds the scan: frontmatter lives at the top of the file,
    so there is no reason to read a whole note to reject it. Pass more when
    the caller also wants body text out of the same read.

    Two things this deliberately does not do naively:

    A substring test matched a prefix of a longer key -- looking for
    `mem_lite_project: workspace--sida_azn` found the note whose key is
    `workspace--sida_azn_streaming_operations`, so the wrong repo's note was
    injected and nothing said so. The marker must fill a frontmatter line.

    Returning the first walk hit made the answer depend on filesystem order
    when several notes share a key, which is normal: a repo with per-component
    notes has one per component, all pointing at the same project. `prefer`
    (a filename stem) names the note that should win; ties then break on the
    shallowest path and finally alphabetically, so the choice is at least the
    same one every session.
    """
    if not vault_dir or not os.path.isdir(vault_dir):
        return None, None
    line_re = re.compile(rf"^{re.escape(marker)}\s*$", re.MULTILINE)
    matches = []
    for dirpath, dirnames, filenames in os.walk(vault_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(read_bytes)
            except OSError:
                continue
            if line_re.search(head):
                matches.append((path, head))
    if not matches:
        return None, None
    matches.sort(key=lambda m: (
        os.path.splitext(os.path.basename(m[0]))[0] != prefer,
        m[0].count(os.sep),
        m[0],
    ))
    return matches[0]


def record_metric(hook, action, cwd, detail=""):
    """Log to the host kit's hook metrics if one is installed.

    The bridge runs standalone too, so this is a soft dependency: the kit's
    hooks/ dir is not on sys.path for a script launched from extern/, which
    is why a bare `import hook_metrics` here silently did nothing at all --
    the hooks looked instrumented and reported zero. Try the known kit
    locations, stay silent when none is present.
    """
    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, ".claude", "hooks"),
                      os.path.join(home, ".qwen", "hooks")):
        if not os.path.isfile(os.path.join(candidate, "hook_metrics.py")):
            continue
        try:
            import sys
            if candidate not in sys.path:
                sys.path.append(candidate)
            import hook_metrics
            hook_metrics.record(hook, action, cwd, detail)
            return
        except Exception:
            return


def read_text(path, limit=60000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def frontmatter_field(text, field):
    r"""The field's value, or None when it is absent or left blank.

    `\s*` used to stand between the colon and the value, and `\s` crosses a
    newline: an empty `last_compiled:` therefore returned the `---` that
    closes the frontmatter. It read as a value, so nothing looked broken --
    vault-inject announced notes as "compiled ---" and compile-nudge, which
    parses that same field as a date, failed and exited silently on every
    uncompiled note in the vault. The one class of note that most needs a
    synthesis pass was the one class that could never ask for it.

    Quotes are stripped for the same reason: `last_compiled: "2026-08-17"` is
    valid YAML and was another silent parse failure.
    """
    m = re.search(rf"^{re.escape(field)}:[^\S\n]*(\S.*?)[^\S\n]*$",
                  text, re.MULTILINE)
    return m.group(1).strip('"\'') if m else None


def agent_card(text):
    """The note's explicitly marked slice, or None if it declares none."""
    start = text.find(CARD_START)
    if start == -1:
        return None
    end = text.find(CARD_END, start)
    if end == -1:
        return None
    return text[start + len(CARD_START):end].strip() or None


def first_section(text, max_chars=1200):
    """Fallback slice: the first `## ` section's body, trimmed.

    Only for notes where any opening section is safe to surface (a repo
    note). Never use this on a personal note -- absent an explicit card,
    the right amount to inject there is nothing.
    """
    body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
    m = re.search(r"^##\s+.+?$", body, re.MULTILINE)
    if not m:
        return body.strip()[:max_chars]
    rest = body[m.start():]
    nxt = re.search(r"^##\s+", rest[3:], re.MULTILINE)
    section = rest[: nxt.start() + 3] if nxt else rest
    return section.strip()[:max_chars]
