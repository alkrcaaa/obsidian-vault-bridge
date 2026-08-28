#!/usr/bin/env python3
"""
vault-lint -- the missing Bakım/Linting step of CLAUDE.md Section 5.

CLAUDE.md Section 5 and Vault Standards.md's "Haftalık Bakım" both describe
the same three checks and, until now, the same answer for how they run: an
agent reads the vault by hand whenever asked. That means the check only
happens when someone remembers to ask, and "read the whole vault by hand"
gets more expensive every week the vault grows. This is that check as a
script instead, so it costs the same to run on week 40 as on week 4.

What it checks, straight out of those two files:
  * stale raw notes  -- `07- Raw` notes still `compiled: false` past a
    staleness window (default 14 days): a not-yet-read source, not a
    problem by itself, but old enough that it is more likely forgotten
    than pending.
  * broken backlinks -- a `[[wikilink]]` whose target resolves to no note
    anywhere in the vault (Obsidian resolves by basename, so this does too).
  * unclassified/stray notes -- what Vault Standards.md calls "sınıfsız veya
    başlıksız": a file sitting directly in the vault root (the standards doc
    says the root is inbox-only), a `07- Raw` note missing one of its
    required frontmatter fields, or a `08- Wiki` note with no `# ` heading.
  * stale auto-captures -- CLAUDE.md Section 2c (OKM): a `<!-- auto:DATE -->`
    line from personal-capture/concept-capture that has sat unpromoted past
    a review window (default 45 days). Not a defect either -- just a line
    nobody has yet decided is durable enough to promote or wrong enough to
    delete, surfaced so that decision actually gets made.

Read-only, always. CLAUDE.md Section 5 is explicit: "Bulguları rapor olarak
sun, otomatik silme/toplu değişiklik yapma" -- this reports, it never edits
or deletes a note. Promoting a finding into a fix stays a human decision,
same as personal-capture and concept-capture's own auto-sections.

Usage:
    vault-lint.py                  # default vault (env/config), text report
    vault-lint.py --vault PATH
    vault-lint.py --stale-days 21  # override the default staleness window

Exit code is 1 when any finding was reported, 0 when the vault is clean --
so a cron/CI caller can act on the exit code without parsing the report.
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))

from vault_common import frontmatter_field, vault_dir as resolve_vault  # noqa: E402

# .obsidian/.smart-env are plugin state, not notes. .vault-compile-backups and
# .trash hold prior versions of real notes -- scanning them would report the
# same stale/unclassified note once per backup taken, and count as a resolved
# link target every name that ever existed, masking a genuinely broken one.
SKIP_DIRS = {".git", ".obsidian", ".smart-env", ".vault-compile-backups", ".trash"}
RAW_DIR = "07- Raw"
WIKI_DIR = "08- Wiki"
# Templates carry example wikilinks as literal placeholder text (e.g. `[[...]]`
# in RawSourceTemplate.md) -- real notes, never meant to resolve, so they are
# not a source of outgoing links to check.
TEMPLATES_DIR = "04- Templates"
# _mem-log is CLAUDE.md's own raw, append-only mirror of session text
# ("elle düzenlenmez") -- it is agent prose *about* the vault, so a session
# discussing wikilink syntax in plain words ends up logged as literal
# `[[link]]`. Curating that noise out is not this hook's job, so it is not
# a source of outgoing links to check either.
MEM_LOG_DIR = "_mem-log"
STALE_DAYS_DEFAULT = 14
# personal-capture and concept-capture review their own section for dedup
# only, not for age -- CLAUDE.md Section 2c gives this the same window for
# "review it" that stale_raw_notes gives a source for "read it".
AUTO_STALE_DAYS_DEFAULT = 45

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
# CLAUDE.md documents wikilink syntax by example -- "`[[wikilink]]` yalnızca
# gerçek not adları için kullanılır" -- and those examples live inside inline
# code spans, not fences. Stripped separately so a doc's own meta-example
# does not get flagged as a broken link to a note called "wikilink".
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# Both hooks stamp a bullet the same way: `- <fact/line> <!-- auto:YYYY-MM-DD -->`.
AUTO_STAMP_RE = re.compile(r"^(.*?)<!-- auto:(\d{4}-\d{2}-\d{2}) -->\s*$", re.MULTILINE)


def _walk_notes(vault):
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames
                        if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)


def _walk_all_files(vault):
    """Every file in the vault, notes and attachments alike.

    A wikilink target is not always a note: `[[Kgag Sunucu.png]]` embeds an
    image, and Obsidian resolves it the same way it resolves a note link --
    by basename, against anything in the vault. Checking broken links
    against notes only made every attachment link a false positive.
    """
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames
                        if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            yield os.path.join(dirpath, name)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _rel(vault, path):
    return os.path.relpath(path, vault)


def stale_raw_notes(vault, notes, stale_days):
    """07- Raw notes still `compiled: false` past the staleness window."""
    cutoff = datetime.now(timezone.utc).timestamp() - stale_days * 86400
    raw_root = os.path.join(vault, RAW_DIR)
    out = []
    for path in notes:
        if not path.startswith(raw_root + os.sep):
            continue
        text = _read(path)
        if frontmatter_field(text, "compiled") != "false":
            continue
        when = None
        captured = frontmatter_field(text, "captured")
        if captured:
            try:
                when = datetime.fromisoformat(
                    captured.replace("Z", "+00:00")).timestamp()
            except ValueError:
                when = None
        if when is None:
            when = os.path.getmtime(path)
        if when < cutoff:
            out.append(_rel(vault, path))
    return sorted(out)


def broken_backlinks(vault, notes, all_files):
    """[[wikilink]] targets that resolve to no file anywhere in the vault.

    Obsidian resolves a link by basename: with or without the extension for
    an attachment (`[[Foo.png]]` or, less often, `[[Foo]]`), without it for
    a note. Both forms of every file's name go into the resolvable set.
    """
    resolvable = set()
    for p in all_files:
        base = os.path.basename(p).lower()
        resolvable.add(base)
        resolvable.add(os.path.splitext(base)[0])

    templates_root = os.path.join(vault, TEMPLATES_DIR)
    mem_log_root = os.path.join(vault, MEM_LOG_DIR)
    out = set()
    for path in notes:
        if path.startswith(templates_root + os.sep) or path.startswith(mem_log_root + os.sep):
            continue
        text = INLINE_CODE_RE.sub("", FENCE_RE.sub("", _read(path)))
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target:
                continue
            stem = os.path.basename(target).lower()
            if stem not in resolvable:
                out.add((_rel(vault, path), target))
    return sorted(out)


def unclassified_notes(vault, notes):
    """Notes CLAUDE.md/Vault Standards.md call out as needing a fix by hand."""
    raw_root = os.path.join(vault, RAW_DIR)
    wiki_root = os.path.join(vault, WIKI_DIR)
    raw_fields = ("source_type", "source_url", "captured", "compiled")
    out = []
    for path in notes:
        # CLAUDE.md is the vault's own operating instructions, not inbox
        # material -- Vault Standards.md's "kök yalnızca inbox" rule is about
        # stray content notes, not the config file every session reads.
        if (os.path.dirname(path) == vault
                and os.path.basename(path) != "CLAUDE.md"):
            out.append((_rel(vault, path), "vault kökünde başıboş not"))
            continue
        # Vault Standards.md: "bir klasörün ana notu varsa adı klasörle aynı
        # olmalı" -- that index note (07- Raw/07- Raw.md) is the folder's MOC,
        # not a source, so the raw-source frontmatter contract does not apply
        # to it.
        is_folder_index = (
            os.path.splitext(os.path.basename(path))[0]
            == os.path.basename(os.path.dirname(path))
        )
        text = _read(path)
        if path.startswith(raw_root + os.sep) and not is_folder_index:
            missing = [f for f in raw_fields if frontmatter_field(text, f) is None]
            if missing:
                out.append((_rel(vault, path), f"eksik alan: {', '.join(missing)}"))
        elif path.startswith(wiki_root + os.sep):
            if not re.search(r"^#\s+\S", text, re.MULTILINE):
                out.append((_rel(vault, path), "başlıksız (# yok)"))
    return sorted(out)


def stale_auto_captures(vault, notes, stale_days):
    """`<!-- auto:DATE -->` lines sitting unpromoted past the review window."""
    cutoff = datetime.now(timezone.utc).timestamp() - stale_days * 86400
    out = []
    for path in notes:
        for m in AUTO_STAMP_RE.finditer(_read(path)):
            try:
                when = datetime.strptime(m.group(2), "%Y-%m-%d") \
                    .replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if when < cutoff:
                line = m.group(1).strip().lstrip("-").strip()
                out.append((_rel(vault, path), line[:80], m.group(2)))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vault", help="Vault root (default: env/config)")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS_DEFAULT,
                     help=f"Staleness window for compiled:false raw notes "
                          f"(default: {STALE_DAYS_DEFAULT})")
    ap.add_argument("--auto-stale-days", type=int, default=AUTO_STALE_DAYS_DEFAULT,
                     help=f"Review window for unpromoted auto-captured lines "
                          f"(default: {AUTO_STALE_DAYS_DEFAULT})")
    args = ap.parse_args()

    vault = os.path.abspath(args.vault or resolve_vault() or "")
    if not vault or not os.path.isdir(vault):
        print("no vault configured (VAULT_DIR unset) or path missing",
              file=sys.stderr)
        return 1

    notes = list(_walk_notes(vault))
    all_files = list(_walk_all_files(vault))
    stale = stale_raw_notes(vault, notes, args.stale_days)
    broken = broken_backlinks(vault, notes, all_files)
    unclassified = unclassified_notes(vault, notes)
    stale_auto = stale_auto_captures(vault, notes, args.auto_stale_days)

    print(f"vault-lint — {len(notes)} note(s) scanned\n")

    print(f"stale raw notes (compiled: false, >{args.stale_days}d) — {len(stale)}")
    for rel in stale:
        print(f"  {rel}")
    print()

    print(f"broken backlinks — {len(broken)}")
    for rel, target in broken:
        print(f"  {rel}: [[{target}]]")
    print()

    print(f"unclassified / stray notes — {len(unclassified)}")
    for rel, why in unclassified:
        print(f"  {rel} — {why}")
    print()

    print(f"stale auto-captures (>{args.auto_stale_days}d, unpromoted) — {len(stale_auto)}")
    for rel, line, when in stale_auto:
        print(f"  {rel} [{when}]: {line}")

    return 1 if (stale or broken or unclassified or stale_auto) else 0


if __name__ == "__main__":
    sys.exit(main())
