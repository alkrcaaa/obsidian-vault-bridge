#!/usr/bin/env python3
"""
vault-inject hook (SessionStart).

The bridge's write path already works: every mem_save lands in the vault
(obsidian-mirror) and compile-nudge asks for a synthesis pass once enough
has piled up. What was missing is the other direction -- nothing ever read
those notes back. A vault that is only written to costs effort and returns
nothing: the agent re-derives a repo's architecture from source every
session while a compiled note describing it sits on disk unread.

This hook closes that loop. At session start it injects two bounded slices:

  1. This repo's compiled note (matched on `mem_lite_project:`, the same key
     compile-nudge and mem-lite use).
  2. The user's profile note, if one opts in with `agent_profile: true`.

Bounded is the whole design. Injection is ambient cost -- it rides in every
request of the session, so it is charged again on every call, not once. A
note may be thousands of words; what gets injected is the slice between
`<!-- agent-card:start -->` and `<!-- agent-card:end -->`, and the caps
below are the ceiling even then. The rest stays on disk for `vault_read` to
fetch on demand, which is what the search MCP is for.

A repo note with no card falls back to its first `## ` section (any opening
section of a repo note is safe to surface). The profile note has no such
fallback: with no explicit card, nothing is injected. Personal notes leak
in a way architecture notes do not, so opting in there is a positive act,
never a default.

Either way the slice has to carry something. An uncompiled note is a heading
with an empty bullet under it -- injecting that spends the ambient budget to
say nothing, so a slice with no content is treated as no slice at all.

Opt-in and fail-open, same as the rest of the bridge: no VAULT_DIR, no
note, unreadable file -> exit 0 in silence.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vault_common import (
        agent_card, find_note, first_section, frontmatter_field,
        infer_project, read_text, record_metric, vault_dir as resolve_vault,
    )
except Exception:
    sys.exit(0)

REPO_CARD_CHARS = 1400
PROFILE_CARD_CHARS = 1100


def _cap(text, limit):
    """Trim to `limit` at a line boundary.

    A hard slice cuts mid-sentence, and a half-sentence about the user is
    worse than a missing one -- it reads as a complete claim.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > limit // 2 else cut).rstrip() + "\n[…]"


def _has_content(slice_text):
    """True if anything survives once headings and empty bullets are dropped.

    18 of this vault's repo notes are uncompiled stubs: a `## Mimari Ozet`
    heading with a lone `-` under it. That slice is 16 characters that say
    nothing, and injection is ambient -- it rides every request of the
    session. Cost with no content is the one case worth refusing outright.
    """
    for line in slice_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lstrip("-*+ \t"):
            return True
    return False


def _repo_slice(vault_dir, project):
    note_path, _ = find_note(
        vault_dir, f"mem_lite_project: {project}",
        prefer=project.rsplit("--", 1)[-1],
    )
    if not note_path:
        return None
    text = read_text(note_path)
    if not text:
        return None
    card = agent_card(text) or first_section(text, REPO_CARD_CHARS)
    if not card or not _has_content(card):
        return None
    rel = os.path.relpath(note_path, vault_dir)
    compiled = frontmatter_field(text, "last_compiled")
    stamp = f", compiled {compiled}" if compiled else ""
    return (
        f"[vault] Compiled note for this repo -- {rel}{stamp}. "
        f"Use it instead of rediscovering the codebase; vault_read(\"{rel}\") "
        f"for the full note.\n{_cap(card, REPO_CARD_CHARS)}"
    )


def _profile_slice(vault_dir):
    note_path, _ = find_note(vault_dir, "agent_profile: true")
    if not note_path:
        return None
    card = agent_card(read_text(note_path))
    if not card or not _has_content(card):
        return None  # opted in but marked no slice -- that is a choice, honour it
    rel = os.path.relpath(note_path, vault_dir)
    return (
        f"[vault] Who you are working with -- {rel} (summary card; "
        f"vault_read(\"{rel}\") for the rest).\n{_cap(card, PROFILE_CARD_CHARS)}"
    )


def main():
    vault_dir = resolve_vault()
    if not vault_dir:
        # Deployed but unconfigured. Silence here is what let the bridge's
        # hooks look alive while doing nothing for weeks, so leave one line
        # per session behind: hook-stats can then tell "off" from "broken".
        record_metric("vault-inject", "skip", os.getcwd(), "no-vault-dir")
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    cwd = data.get("cwd") or os.getcwd()

    parts = []
    try:
        repo = _repo_slice(vault_dir, infer_project(cwd))
        if repo:
            parts.append(repo)
        profile = _profile_slice(vault_dir)
        if profile:
            parts.append(profile)
    except Exception:
        sys.exit(0)

    if not parts:
        # No note for this repo, or one whose slice is an empty stub. Both are
        # normal, and both look identical to a broken hook from outside, so
        # they leave a line: hook-stats can tell "nothing to say" from "dead".
        record_metric("vault-inject", "skip", cwd, "no-slice")
        sys.exit(0)

    record_metric("vault-inject", "inject", cwd, f"{len(parts)}card")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n".join(parts),
        },
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
