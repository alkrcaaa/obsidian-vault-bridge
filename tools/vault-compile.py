#!/usr/bin/env python3
"""
vault-compile -- the missing Compile step of the vault loop.

mem-lite captures (335 observations, ~280 carrying a lesson) and vault-inject
feeds compiled notes back into every session. Between them sat nothing: 17 of
this vault's 38 `Code/<repo>.md` notes had never been compiled at all and 18
were compiled once, by hand, on a single day in August. `compile-nudge` only
ever asked; it fired three times in a week and was acted on zero times, because
compiling is a different job than the job the session is doing and loses to it
every time (DECISIONS.md D3).

So this is an executable, not a hook. Run it between sessions:

    vault-compile.py                 # this repo, dry-run diff
    vault-compile.py --apply
    vault-compile.py --all           # every note with a mem_lite_project key
    vault-compile.py --all --apply --min-new 1

Contract (DECISIONS.md D4-D9):
  source     mem-lite's SQLite, not the mirrored markdown
  model      `claude -p` -- compiling is synthesis, which is the local 27B's
             ceiling and the most expensive place to hit it
  isolation  the child runs with VAULT_DIR cleared, so the vault's own Stop
             hooks no-op on the compile transcript instead of mistaking a pile
             of repo lessons for facts about the user
  ownership  it rewrites three sections and preserves the rest of the file
             byte-for-byte; the model never picks a file or a heading
  proof      the metric records where it wrote, not just that it ran
"""
import argparse
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hooks"))

from vault_common import (  # noqa: E402
    CARD_END, CARD_START, frontmatter_field, infer_project, record_metric,
    vault_dir as resolve_vault,
)

MEM_DB = os.path.join(os.path.expanduser("~"), ".claude-mem-lite", "claude-mem-lite.db")
DEFAULT_MODEL = "sonnet"
MIN_NEW_DEFAULT = 8

# One repo's material has to fit one prompt. miivii_setup_ansible alone carries
# 109 lessons; past this budget the oldest low-importance entries are dropped
# rather than truncating mid-record, which would feed the model half a lesson.
MAX_MATERIAL_CHARS = 60000
MAX_NARRATIVE_CHARS = 700

CARD_HEADING = "## Mimari Özet"
SECTIONS = ("## Son Kararlar", "## Açık Sorular")

MARK_ARCH = "<<<MIMARI>>>"
MARK_DECISIONS = "<<<KARARLAR>>>"
MARK_QUESTIONS = "<<<SORULAR>>>"

SYSTEM_PROMPT = (
    "Sen bir bilgi tabanı derleyicisisin. Bir yazılım deposu hakkında biriken ham "
    "kayıtları, o deponun bugünkü halini anlatan kısa ve okunabilir bir nota "
    "indirgiyorsun. Yeni bilgi UYDURMUYORSUN: sadece sana verilen kayıtlarda geçen "
    "şeyleri yazıyorsun. Çıktın Türkçe ve madde madde. Asla dosya adı, başlık adı "
    "veya bölüm adı önermiyorsun; sadece istenen üç bölümün içeriğini üretiyorsun."
)


def log(msg):
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------
# vault side


def iter_notes(vault_dir):
    """(path, head, project) for every note whose frontmatter names a project.

    Same full-line match find_note() uses: a substring test here would let
    `workspace--sida_azn` claim the note keyed `workspace--sida_azn_streaming_
    operations`, which is exactly the bug that made vault-inject serve the
    wrong repo's card in silence.
    """
    pattern = re.compile(r"^mem_lite_project:\s*(\S+)\s*$", re.MULTILINE)
    for dirpath, dirnames, filenames in os.walk(vault_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    head = f.read(1500)
            except OSError:
                continue
            m = pattern.search(head)
            if not m:
                continue
            project = m.group(1).strip().strip("\"'")
            if project.startswith("{{"):  # the note template itself
                continue
            yield path, head, project


def preferred_note(notes):
    """One note per project key -- the same one vault-inject would serve.

    A repo with per-component notes has several carrying the same
    `mem_lite_project`: kida_azn has seven. Compiling all of them would write
    the whole repo's material into every component note and quietly flatten
    seven hand-written notes into seven copies of the same summary. find_note()
    already picks a single winner for injection; compile has to agree with it,
    or the note that gets injected is not the note that gets maintained.
    """
    by_project = {}
    for path, head, project in notes:
        by_project.setdefault(project, []).append((path, head, project))
    chosen = []
    for project, group in by_project.items():
        prefer = project.rsplit("--", 1)[-1]
        group.sort(key=lambda n: (
            os.path.splitext(os.path.basename(n[0]))[0] != prefer,
            n[0].count(os.sep),
            n[0],
        ))
        chosen.append(group[0])
    chosen.sort(key=lambda n: n[2])
    return chosen


def last_compiled(head):
    value = frontmatter_field(head, "last_compiled")
    if not value:
        return None
    try:
        return datetime.strptime(value.strip().strip("\"'"), "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def section_span(text, heading):
    """(start, end) of a `## Heading` section body, or None.

    The body ends at the next top-level `## `, or at the first `### ` nested
    under this heading, whichever comes first. The `###` boundary is what keeps
    a hand-written subsection alive: the compiler emits a flat bullet list, so
    without it a rewrite of `## Son Kararlar` would take any detail someone
    nested beneath it along with the bullets, and the loss would look exactly
    like a normal compile.
    """
    m = re.search(rf"^{re.escape(heading)}\s*$", text, re.MULTILINE)
    if not m:
        return None
    body_start = m.end()
    nxt = re.search(r"^#{2,3} ", text[body_start:], re.MULTILINE)
    return (body_start, body_start + nxt.start() if nxt else len(text))


def replace_section(text, heading, bullets):
    span = section_span(text, heading)
    body = "\n" + "\n".join(bullets) + "\n\n"
    if span is None:
        return text.rstrip("\n") + "\n\n" + heading + body
    return text[:span[0]] + body + text[span[1]:]


def replace_card(text, bullets):
    """Rewrite the agent-card block -- the payload vault-inject injects."""
    body = CARD_START + "\n" + CARD_HEADING + "\n" + "\n".join(bullets) + "\n" + CARD_END
    if CARD_START in text and CARD_END in text:
        start = text.index(CARD_START)
        end = text.index(CARD_END) + len(CARD_END)
        return text[:start] + body + text[end:]
    # No markers yet: wrap the existing heading if there is one, else append.
    span = section_span(text, CARD_HEADING)
    if span is None:
        return text.rstrip("\n") + "\n\n" + body + "\n"
    head_start = text.rindex(CARD_HEADING, 0, span[0])
    return text[:head_start] + body + "\n" + text[span[1]:]


BACKUP_DIR = ".vault-compile-backups"


def backup(path, vault_root):
    """Copy a note aside before overwriting it. Returns the copy's path or None.

    D8 leaned on the vault being git-tracked as the backstop. It is, but the
    first real run met a working tree with hundreds of uncommitted lines in it,
    so `git checkout` would have thrown away the user's own unsaved work rather
    than this tool's write. A backup that does not depend on the vault's git
    state costs one file copy.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rel = os.path.relpath(path, vault_root).replace(os.sep, "_")
    dest_dir = os.path.join(vault_root, BACKUP_DIR)
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{stamp}_{rel}")
        shutil.copy2(path, dest)
        return dest
    except OSError as exc:
        log(f"  ! yedek alınamadı ({exc}) — yazma iptal")
        return None


def stamp_compiled(text, day):
    if re.search(r"^last_compiled:.*$", text, re.MULTILINE):
        return re.sub(r"^last_compiled:.*$", f"last_compiled: {day}", text,
                      count=1, flags=re.MULTILINE)
    return re.sub(r"^---\s*$", f"last_compiled: {day}\n---", text, count=1,
                  flags=re.MULTILINE)


def managed_excerpt(text):
    """What the note currently says in the three sections we own."""
    out = []
    for heading in (CARD_HEADING,) + SECTIONS:
        span = section_span(text, heading)
        body = text[span[0]:span[1]].strip() if span else ""
        body = body.replace(CARD_END, "").strip()
        out.append(f"{heading}\n{body or '-'}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------
# mem-lite side


def observations(project, since):
    """Live observations for a project, oldest first, within a char budget.

    Superseded and compressed rows are skipped: a note compiled from a record
    that mem-lite itself has retired would reintroduce a fact the DB already
    knows is stale.
    """
    query = (
        "SELECT created_at, type, title, subtitle, lesson_learned, narrative, "
        "importance FROM observations WHERE project = ? "
        "AND superseded_at IS NULL AND compressed_into IS NULL"
    )
    params = [project]
    if since is not None:
        query += " AND created_at > ?"
        params.append(since.strftime("%Y-%m-%dT%H:%M:%S"))
    query += " ORDER BY created_at ASC"
    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(query, params).fetchall()
        con.close()
    except Exception as exc:
        log(f"  ! mem-lite okunamadı: {exc}")
        return [], 0

    records = []
    for created, otype, title, subtitle, lesson, narrative, importance in rows:
        parts = [f"### {(created or '')[:10]} [{otype or 'discovery'}] {title or ''}".rstrip()]
        if subtitle:
            parts.append(subtitle.strip())
        if narrative:
            text = narrative.strip()
            if len(text) > MAX_NARRATIVE_CHARS:
                text = text[:MAX_NARRATIVE_CHARS].rsplit(" ", 1)[0] + " …"
            parts.append(text)
        if lesson:
            parts.append(f"DERS: {lesson.strip()}")
        records.append({
            "text": "\n".join(parts),
            "importance": importance or 1,
            "created": created or "",
        })

    total = len(records)
    if sum(len(r["text"]) for r in records) <= MAX_MATERIAL_CHARS:
        return records, total

    # Over budget: keep the most important and most recent whole records, then
    # restore chronological order so the model reads the repo's actual arc.
    ranked = sorted(records, key=lambda r: (r["importance"], r["created"]), reverse=True)
    kept, used = [], 0
    for rec in ranked:
        if used + len(rec["text"]) > MAX_MATERIAL_CHARS:
            continue
        kept.append(rec)
        used += len(rec["text"])
    kept.sort(key=lambda r: r["created"])
    return kept, total


# --------------------------------------------------------------------------
# the model call


def build_prompt(project, note_name, current, records, total, dropped):
    material = "\n\n".join(r["text"] for r in records)
    scope = (
        f"{len(records)} kayıt veriliyor"
        + (f" ({total} kaydın en önemli/en yenileri; {dropped} tanesi yer nedeniyle "
           "dışarıda bırakıldı)" if dropped else "")
    )
    return f"""Aşağıda `{project}` deposunun vault notu ve o depo hakkında biriken ham kayıtlar var. {scope}.

Görevin: notun ÜÇ bölümünü güncel haliyle YENİDEN YAZMAK. Bu bir günlük değil, kümülatif bir özet — tarihli yeni bir bölüm ekleme, mevcut maddeleri de gözden geçir.

NOTUN ŞU ANKİ HALİ ({note_name}):
---
{current}
---

HAM KAYITLAR:
---
{material}
---

KURALLAR:
- MEVCUT MADDELER VARSAYILAN OLARAK KALIR. Bir maddeyi ancak ham kayıtlarda o kararın geri alındığına, değiştirildiğine veya sorunun kapandığına dair AÇIK kanıt varsa çıkarabilirsin. "Eskidi", "artık önemli değil" ya da "yer kalmadı" gerekçe DEĞİLDİR — bu not kümülatif bir hafızadır, son haberler listesi değil. Şüphedeysen maddeyi koru.
- Yeni kayıtlardan geleni ekle; aynı konudaki iki maddeyi tek maddede birleştirebilirsin, ama içerik kaybetmeden.
- Ham kayıtlarda geçmeyen hiçbir şey yazma. Emin değilsen yazma.
- Her madde tek satır, `- ` ile başlar. Kod/dosya/araç adlarını backtick içinde ver.
- "## Mimari Özet": 3-6 madde. Bu bölüm her oturumun başında ajana enjekte edilen karttır — depoyu ilk kez açan birinin bilmesi gereken yapısal gerçekler olsun, haber değil.
- "## Son Kararlar": en fazla 16 madde. Alınmış ve yürürlükte olan kararlar; her maddede kararın NEDENİ kısaca geçsin.
- "## Açık Sorular": en fazla 6 madde. Sadece gerçekten açık olanlar; kayıtlarda kapandığı görülen bir soruyu yazma. Açık soru yoksa tek bir `- (yok)` maddesi yaz.

ÇIKTI BİÇİMİ — tam olarak bu, başka hiçbir şey yazma (giriş cümlesi, açıklama, kod bloğu yok):
{MARK_ARCH}
- ...
{MARK_DECISIONS}
- ...
{MARK_QUESTIONS}
- ...
"""


def run_model(prompt, model, timeout):
    """One text-in/text-out turn, isolated from the vault it is compiling for.

    VAULT_DIR and MEM_OBSIDIAN_VAULT are cleared rather than suppressed by a new
    flag: every piece of this module is already a silent no-op without them
    (README), so this reuses the contract instead of adding a second one. Left
    set, the child's own Stop hooks would run on the compile transcript and
    personal-capture would file a pile of repo lessons under Hakkımda.md.
    """
    if not shutil.which("claude"):
        return None, "claude-cli-missing"
    env = dict(os.environ)
    for key in ("VAULT_DIR", "MEM_OBSIDIAN_VAULT"):
        env.pop(key, None)
    env["VAULT_COMPILE_CHILD"] = "1"
    cmd = [
        "claude", "-p",
        "--model", model,
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    with tempfile.TemporaryDirectory(prefix="vault-compile-") as workdir:
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  timeout=timeout, env=env, cwd=workdir)
        except subprocess.TimeoutExpired:
            return None, "timeout"
    if proc.returncode != 0:
        return None, f"exit{proc.returncode}:{(proc.stderr or '').strip()[:120]}"
    return proc.stdout, None


def parse_sections(raw):
    """Marked blocks -> bullet lists, or (None, reason).

    Validated rather than trusted: a run that returns prose, or drops a marker,
    must leave the note untouched. #417 is the standing reminder that a bad
    write into a note the user reads costs more than no write at all.
    """
    if not raw:
        return None, "empty"
    starts = {}
    for key, marker in (("arch", MARK_ARCH), ("decisions", MARK_DECISIONS),
                        ("questions", MARK_QUESTIONS)):
        idx = raw.find(marker)
        if idx < 0:
            return None, f"missing-{key}"
        starts[key] = (idx, idx + len(marker))
    if not starts["arch"][0] < starts["decisions"][0] < starts["questions"][0]:
        return None, "markers-out-of-order"
    blocks = {
        "arch": raw[starts["arch"][1]:starts["decisions"][0]],
        "decisions": raw[starts["decisions"][1]:starts["questions"][0]],
        "questions": raw[starts["questions"][1]:],
    }
    out = {}
    for key, block in blocks.items():
        bullets = [ln.strip() for ln in block.splitlines()
                   if ln.strip().startswith("- ") and len(ln.strip()) > 3]
        if not bullets:
            return None, f"empty-{key}"
        out[key] = bullets
    return out, None


# --------------------------------------------------------------------------


def unified_diff(before, after, name):
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=1))


def compile_note(path, project, args, vault_root):
    rel = os.path.relpath(path, vault_root)
    with open(path, "r", encoding="utf-8") as f:
        current = f.read()

    since = last_compiled(current)
    records, total = observations(project, since)
    if not records:
        log(f"  · {project}: yeni kayıt yok, atlandı")
        return "skip-no-new"
    if since is not None and len(records) < args.min_new and not args.force:
        log(f"  · {project}: {len(records)} yeni kayıt (< {args.min_new}), atlandı")
        return "skip-below-threshold"

    dropped = total - len(records)
    state = "İLK DERLEME" if since is None else f"son derleme {since:%Y-%m-%d}"
    log(f"  → {project}: {len(records)}/{total} kayıt, {state} — {rel}")
    if args.dry_run and args.no_model:
        return "planned"

    prompt = build_prompt(project, os.path.basename(path), managed_excerpt(current),
                          records, total, dropped)
    raw, err = run_model(prompt, args.model, args.timeout)
    if err:
        log(f"  ! {project}: model çağrısı başarısız ({err})")
        record_metric("vault-compile", "error", vault_root, f"{project}:{err}")
        return "error"

    parsed, why = parse_sections(raw)
    if parsed is None:
        log(f"  ! {project}: çıktı doğrulanamadı ({why}) — not değiştirilmedi")
        record_metric("vault-compile", "invalid", vault_root, f"{project}:{why}")
        if args.debug:
            log((raw or "")[:1500])
        return "invalid"

    updated = replace_card(current, parsed["arch"])
    updated = replace_section(updated, SECTIONS[0], parsed["decisions"])
    updated = replace_section(updated, SECTIONS[1], parsed["questions"])
    updated = stamp_compiled(updated, datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    if updated == current:
        log(f"  · {project}: değişiklik yok")
        return "unchanged"

    if not args.apply:
        print(unified_diff(current, updated, rel))
        return "dry-run"

    saved = backup(path, vault_root)
    if saved is None:
        record_metric("vault-compile", "error", vault_root, f"{project}:backup-failed")
        return "error"
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    log(f"  ✓ {project}: yazıldı → {rel}")
    # Destination in the metric, not just the verb: #417 recorded action=capture
    # while writing to the wrong note, so "it ran" read as "it worked".
    record_metric("vault-compile", "compile", vault_root,
                  f"{project}:{len(records)}obs:{rel}")
    return "compiled"


def main():
    ap = argparse.ArgumentParser(description="Compile mem-lite history into vault notes.")
    ap.add_argument("--all", action="store_true", help="every note with a mem_lite_project key")
    ap.add_argument("--project", help="one project key (default: infer from cwd)")
    ap.add_argument("--apply", action="store_true", help="write (default: print a diff)")
    ap.add_argument("--min-new", type=int, default=MIN_NEW_DEFAULT,
                    help=f"skip notes with fewer new observations (default {MIN_NEW_DEFAULT})")
    ap.add_argument("--force", action="store_true", help="ignore --min-new")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--limit", type=int, help="stop after N notes")
    ap.add_argument("--no-model", action="store_true", help="list work, call nothing")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    args.dry_run = not args.apply

    vault_root = resolve_vault()
    if not vault_root or not os.path.isdir(vault_root):
        log("VAULT_DIR ayarlı değil ya da dizin yok — yapılacak bir şey yok.")
        return 1
    if not os.path.isfile(MEM_DB):
        log(f"mem-lite veritabanı bulunamadı: {MEM_DB}")
        return 1

    notes = preferred_note(list(iter_notes(vault_root)))
    if not args.all:
        wanted = args.project or infer_project(os.getcwd())
        notes = [n for n in notes if n[2] == wanted]
        if not notes:
            log(f"`mem_lite_project: {wanted}` taşıyan not yok — hangi klasöre "
                "ait olduğu kullanıcı kararı, bu araç not yaratmaz.")
            return 1

    log(f"{len(notes)} not incelenecek ({'YAZIM' if args.apply else 'kuru koşu'}, model={args.model})")
    tally = {}
    for i, (path, _head, project) in enumerate(notes):
        if args.limit and i >= args.limit:
            break
        outcome = compile_note(path, project, args, vault_root)
        tally[outcome] = tally.get(outcome, 0) + 1

    log("— özet: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 0 if not tally.get("error") and not tally.get("invalid") else 2


if __name__ == "__main__":
    sys.exit(main())
