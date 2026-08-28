#!/usr/bin/env python3
"""
Shared vault lookups for the bridge's hooks.

vault-inject (load this repo's note at session start) and vault-compile
(which note does this project key own?) ask the vault the same two questions:
what is this session's project key, and which note carries it. Answering them twice
would be two chances to drift apart -- and a key resolved differently in one
hook than the other fails silently: the note is simply never found, so the
hook just stays quiet and nobody learns it broke.

Nothing here writes. Every function is fail-open: on any missing piece it
returns None/empty rather than raising, because a knowledge layer is an
enhancement and must never be able to block a session.
"""
import contextlib
import os
import re
import subprocess

# A note can mark an explicit slice for the agent to load at session start.
# Everything between these markers is what gets injected -- the rest of the
# note stays on disk until something actually asks for it. HTML comments so
# Obsidian renders nothing.
CARD_START = "<!-- agent-card:start -->"
CARD_END = "<!-- agent-card:end -->"

# The project key for every session that is not inside a git repo -- a general
# conversation, a session started in a home or scratch directory, anything
# with no repo to belong to. One key, so one note in the vault can own the
# whole class; see infer_project() for what splitting it costs.
CATCHALL_PROJECT = "genel"

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
    """Env first (so a caller can override), then the installer's file.

    `VAULT_HOOKS_OFF` turns every piece of this module off for a process and
    everything it spawns. Clearing VAULT_DIR is not enough and cannot be: an
    absent VAULT_DIR is exactly the case the config file exists to answer (a
    hook inherits the agent's env, and the agent was started from a shell that
    never exported it -- the failure that kept these hooks dead in production).
    So "unset" already means "look in the file", and the contract had no way to
    say "off". vault-compile needs one, because its `claude -p` child would
    otherwise run the vault live on the compile transcript and let
    personal-capture file a pile of repo lessons under the user's profile note.
    """
    if os.environ.get("VAULT_HOOKS_OFF", "").strip():
        return None
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
    """This session's project key: the git repo's, or the catch-all.

    Inside a repo this mirrors ~/.claude-mem-lite/utils.mjs::inferProject() --
    git root, then `parent--base`. That key is how a note in the vault is
    matched to the repo a session is running in, so it has to stay in sync
    with mem-lite's own inference.

    Outside a repo it deliberately stops mirroring. mem-lite keeps naming
    those sessions after the directory they happen to start in, which splits
    one kind of work -- everything that is not a repo -- across an unbounded
    set of keys: `/home/ali` becomes `home--ali`, `~/Downloads` becomes
    `ali--Downloads`, a scratchpad becomes `scratchpad--smoke`. Each shard is
    then too small to ever reach NOTE_THRESHOLD, so none is reported as
    noteless, none gets a note, and none is ever compiled or injected. The
    material is on disk and unreachable, which looks exactly like nothing
    having been captured. One key for all of it gives that work a single note,
    and every existing lookup keeps working unchanged.
    """
    root, in_repo = _git_root(cwd)
    return _dir_key(root) if in_repo else CATCHALL_PROJECT


def mem_lite_key(cwd):
    """mem-lite's own project key for a directory, repo or not.

    infer_project() answers a different question -- which *vault* note owns
    this session -- and folds every repo-less directory into the catch-all.
    The reconcile pass needs the un-folded key as well, because that string is
    what mem-lite actually wrote into the `project` column, and it is the only
    way to find a session's rows again after the fact.
    """
    root, _ = _git_root(cwd)
    return _dir_key(root)


def _git_root(cwd):
    """(repo root, True) if cwd is inside a git repo, else (cwd, False)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip(), True
    except Exception:
        pass
    return cwd, False


def _dir_key(root):
    """`parent--base`, sanitised -- mem-lite's inferProject() naming."""
    root = root.rstrip("/")
    base = os.path.basename(root)
    parent = os.path.basename(os.path.dirname(root))
    raw = f"{parent}--{base}" if parent and parent not in (".", "/") else base
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", raw)[:100]


@contextlib.contextmanager
def locked_note(path):
    """Open a vault note for read-decide-append under an exclusive lock.

    Three hooks (personal-capture, concept-capture, obsidian-mirror) append to
    a handful of notes from Stop/PostToolUse, which two hosts (Claude, Qwen)
    or two overlapping sessions can fire at the same moment. Locking only the
    final `open(path, "a")` still races on the decision before it: two
    readers can both read the note before either has appended, both decide
    the same line is new, and both write it. So the lock has to wrap
    read-decide-write as one section, not just the write -- this yields an
    `a+` handle already positioned at the start, exclusively locked for the
    caller's whole read-then-append.

    `fcntl` is POSIX-only; where it is unavailable this silently skips the
    lock rather than raising; a note write is a fail-open enhancement, not a
    contract.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        f.seek(0)
        yield f
    finally:
        f.close()


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


def stem_words(text):
    """Word stems, crudely: the first four characters of each word.

    Whole-word comparison misses the case it exists for. Turkish inflects by
    suffix, so the same fact restated reads as "repo" one day and
    "repolardan" the next, and an exact-word overlap scores those as
    unrelated -- the duplicate then gets written every session.
    """
    return {w[:4] for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


def is_new_line(candidate, existing_lines, threshold=0.6):
    """Is this line saying something the note does not already say?

    Exact-match dedup would let the same point accumulate in five phrasings,
    which is how a note turns into noise nobody reads. Both unsupervised
    writers (personal-capture, concept-capture) append to a note they will
    append to again next session, so both need this and both need the same
    answer -- two copies would drift and one of them would start duplicating.
    """
    cw = stem_words(candidate)
    # Guard on the raw sentence, not on stems: a short but real line
    # ("Ankara'da yaşıyor") has few stems, and rejecting it here would look
    # like dedup.
    if len(re.findall(r"\w+", candidate)) < 3 or not cw:
        return False
    for line in existing_lines:
        lw = stem_words(line)
        if not lw:
            continue
        if len(cw & lw) / len(cw) > threshold:
            return False
    return True


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


# --- Source classification -------------------------------------------------
#
# Two questions get asked of this corpus, and they are not the same question.
#
#   audience    -- which assistant should ever see this? The vault is ~88%
#                  coding telemetry by volume (324KB of work-repo notes, 133KB
#                  of _mem-log lessons) against ~12% life material. A coding
#                  agent wants the former; a general or voice assistant wants
#                  the identity card and is actively degraded by a retrieval
#                  hit on "the ENC gRPC port is 50053". One corpus, two
#                  consumers, so the split has to be declared somewhere.
#
#   sensitivity -- what has to be reviewed the day any of this leaves the
#                  machine (D#10). The vault is fully private today, so this
#                  costs nothing to carry now and a pass over 175 notes and
#                  359 observations to reconstruct later.
#
# Derived from the project key and the vault's top-level folder, deliberately
# not stored per record. A per-record flag means an LLM classifying every save:
# it mislabels silently, and it would leave every existing row unclassified.
# A lookup answers for material already on disk, cannot drift, and costs one
# line per new repo.
#
# This classifies provenance, not content. A personal repo's session that spent
# its time in a work repo still lands under the personal key. Content-level
# leakage is a redaction problem for the opening-up day; a scope map is the
# wrong tool for it and pretending otherwise is how you ship a false negative.

# Personal is the closed set; everything else is work. That is the fail-safe
# direction -- a new work repo nobody remembered to list still reads as
# sensitive, where an unlisted personal repo only costs an unnecessary review.
PERSONAL_PROJECTS = {
    "workspace--dev-agent-kit",
    "workspace--AliKaraca",
    "home--ali",
    "ali--workspace",
}

# The catch-all is genuinely mixed -- D13 keeps its scope per entry, so no
# project-level answer for it is honest. "mixed" means: review entry by entry.
MIXED_PROJECTS = {CATCHALL_PROJECT}

# Vault top-level folder -> (audience, sensitivity). A folder absent from this
# map falls through to the same conservative default as an unlisted repo.
VAULT_FOLDERS = {
    "00- Home":              ("life", "personal"),
    "01-Daily Notes":        ("life", "personal"),
    "02- Kuartis":           ("coding", "work"),
    "03- Personal Projects": ("coding", "personal"),
    "04- Templates":         ("none", "personal"),
    "05- Archive":           ("none", "mixed"),
    "07- Raw":               ("both", "mixed"),
    "08- Wiki":              ("both", "personal"),
    "09- Docs":              ("coding", "mixed"),
    "_mem-log":              ("coding", "mixed"),
}

# What an unlisted source gets: assume coding telemetry (mem-lite writes little
# else) and assume sensitive, the only default that fails safe.
DEFAULT_CLASS = ("coding", "work")


def classify_project(project_key):
    """(audience, sensitivity) for a mem-lite / _mem-log project key.

    An unknown key gets DEFAULT_CLASS rather than None. An unclassified source
    that reads as "not sensitive" is the exact failure this map exists to
    prevent, and a caller forced to special-case None will eventually forget.
    """
    if not project_key:
        return DEFAULT_CLASS
    if project_key in MIXED_PROJECTS:
        return ("coding", "mixed")
    if project_key in PERSONAL_PROJECTS:
        return ("coding", "personal")
    return DEFAULT_CLASS


def classify_vault_path(rel_path):
    """(audience, sensitivity) for a vault-relative note path.

    Keyed on the first path segment, so "08- Wiki" and "08- Wiki/Fine-tuning
    ve RAG.md" answer the same.
    """
    if not rel_path:
        return DEFAULT_CLASS
    top = rel_path.strip("/").split("/")[0]
    return VAULT_FOLDERS.get(top, DEFAULT_CLASS)


def is_life_material(rel_path):
    """True if a general or voice assistant should retrieve this note at all.

    The one lever a non-coding consumer needs: narrow the corpus to the ~12%
    that is about the person rather than about a repo.
    """
    return classify_vault_path(rel_path)[0] in ("life", "both")
