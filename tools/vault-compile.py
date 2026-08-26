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
  isolation  the child runs with VAULT_HOOKS_OFF=1, so the vault's own Stop
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
    CARD_END, CARD_START, CATCHALL_PROJECT, agent_card, find_note,
    frontmatter_field, infer_project, record_metric,
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
SOURCES_HEADING = "## Kaynaklar"

# The catch-all note is not a repo, and every piece of framing here assumes one
# -- the card is headed "Mimari Özet", the prompt calls the subject a depo and
# asks for structural facts about a codebase. Pointed at a general conversation
# that produces a note describing a chat as if it were software. The profile
# note already had to solve the same problem (it passes heading=None rather
# than stamping "architecture" over a description of a person); this is the
# third subject the compiler serves, so the framing becomes a lookup.
CATCHALL_CARD_HEADING = "## Genel Özet"
CATCHALL_SUBJECT = "repo dışında kalan işlerin"
CATCHALL_CARD_RULE = (
    "3-6 madde. Bu bölüm her oturumun başında ajana enjekte edilen karttır — "
    "repoya bağlı olmayan işlerde kalıcı olarak geçerli olan gerçekler olsun, "
    "haber değil."
)

# How much material a project has to accumulate before a missing note is worth
# reporting. Low enough that a repo does not stay silent for weeks, high enough
# that a single stray observation does not ask for a note.
NOTE_THRESHOLD = 10

# Where obsidian-mirror drops the raw per-day records, relative to the vault
# root. The template already names this folder; the compiler now has to agree
# with it, because it is the only thing that gives those files a real link.
MEM_LOG_DIR = "_mem-log"

PROFILE_MARKER = "agent_profile: true"
AUTO_HEADING = "## Otomatik Yakalananlar"
AUTO_STAMP = re.compile(r"<!--\s*auto:(\d{4}-\d{2}-\d{2})\s*-->")

MARK_CARD = "<<<KART>>>"
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


CATCHALL_SYSTEM_PROMPT = (
    "Sen bir bilgi tabanı derleyicisisin. Bir kullanıcının herhangi bir kod "
    "deposuna bağlı olmayan işleri hakkında biriken ham kayıtları, bugün geçerli "
    "olan halini anlatan kısa ve okunabilir bir nota indirgiyorsun. Konu bir "
    "yazılım deposu DEĞİL: mimari anlatmaya çalışma, kayıtlarda ne varsa onu "
    "özetle. Yeni bilgi UYDURMUYORSUN. Çıktın Türkçe ve madde madde. Asla dosya "
    "adı, başlık adı veya bölüm adı önermiyorsun; sadece istenen üç bölümün "
    "içeriğini üretiyorsun."
)


PROFILE_SYSTEM_PROMPT = (
    "Sen bir kullanıcı profili derleyicisisin. Bir kişi hakkında oturumlarda "
    "yakalanmış ham cümlelerden, o kişiyle çalışan bir ajanın davranışını "
    "değiştirecek KALICI talimatları süzüyorsun. Geçici durumları, proje "
    "bilgisini ve tekrarları eliyorsun. Yeni bilgi UYDURMUYORSUN. Çıktın "
    "Türkçe ve madde madde."
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
            # An unresolved placeholder anywhere in the key means this is the
            # template, not a note. The prefix-only test this replaces missed
            # the template that actually ships -- its key is
            # `workspace--{{REPO}}`, so it read as a real project and was
            # iterated on every run. Harmless while it never matched new
            # observations, but the day it did, the compile would have been
            # written into the template every note is created from.
            if "{{" in project:
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


def replace_card(text, bullets, heading=CARD_HEADING):
    """Rewrite the agent-card block -- the payload vault-inject injects.

    `heading` is None for the profile note, whose card holds bullets directly:
    a repo note's card sits under `## Mimari Özet`, and stamping that heading
    into the profile would inject the word "architecture" over a description of
    a person.
    """
    inner = ((heading + "\n") if heading else "") + "\n".join(bullets)
    body = CARD_START + "\n" + inner + "\n" + CARD_END
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
    """Set `last_compiled`, adding the field if the note has none.

    Adding it went in *above* the opening `---`, because the first `---` in a
    note is the delimiter that opens frontmatter, not a line to insert before.
    Every repo note already carried the field so the branch never ran until the
    profile note, which does not: the result was a file whose first line sits
    outside its own frontmatter, i.e. no valid frontmatter at all.
    """
    if re.search(r"^last_compiled:.*$", text, re.MULTILINE):
        return re.sub(r"^last_compiled:.*$", f"last_compiled: {day}", text,
                      count=1, flags=re.MULTILINE)
    if re.match(r"^---\s*$", text.split("\n", 1)[0]):
        head, _, rest = text.partition("\n")
        return f"{head}\nlast_compiled: {day}\n{rest}"
    return f"---\nlast_compiled: {day}\n---\n\n{text}"


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


def unowned_projects(owned, minimum=NOTE_THRESHOLD):
    """Projects mem-lite has material for that no note claims.

    A repo with no note produces no compile, and a compile that never runs
    reports nothing -- so the repo stays noteless forever and the silence
    looks like nothing needing doing. compile-nudge used to speak up here;
    when it went, this went with it, so it moved to where the compiling
    happens. Which folder a note belongs in is the user's call, so this only
    names the gap and never creates the note.
    """
    try:
        con = sqlite3.connect(f"file:{MEM_DB}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT project, COUNT(*) FROM observations "
            "WHERE superseded_at IS NULL AND compressed_into IS NULL "
            "GROUP BY project HAVING COUNT(*) >= ?", (minimum,)).fetchall()
        con.close()
    except Exception as exc:
        log(f"  ! mem-lite okunamadı: {exc}")
        return []
    # An unresolved placeholder is the note template's key, not a real repo --
    # the same guard iter_notes applies, for the same reason.
    return [(p, n) for p, n in sorted(rows)
            if p and p not in owned and "{{" not in p]


CATCHALL_ID_RE = re.compile(r"^###\s.*\(#(\d+)\)\s*$", re.MULTILINE)


def catchall_ids(vault_root):
    """Observation ids the catch-all note owns, read off its raw day files.

    mem-lite still names a repo-less session after whatever directory it
    started in, so the DB keeps one key per directory and records nothing
    about whether a key was ever a repo. The catch-all note therefore cannot
    find its own material by name, and no property of the key recovers it: a
    repo and a scratch directory produce the same `parent--base` shape.

    Guessing from volume was tried and is wrong. Folding in every unclaimed
    key under NOTE_THRESHOLD swallowed four real repos whose notes simply had
    not been written yet -- lookout, kida_azn, cvml_st_integration,
    cross_repo_integration -- and a repo's material silently reappearing under
    a general note is worse than it having no note at all.

    obsidian-mirror already answers the question exactly, at the moment it can
    still be answered: it runs `git rev-parse` in the session's own directory
    and files the day under `_mem-log/genel/` when there is no repo. Those
    files carry the observation id in every entry heading, so the classifica-
    tion made from live git state is on disk and can just be read back. Only
    sessions captured since that rule shipped are in there, which is correct:
    what came before was never classified and cannot be reconstructed now.
    """
    log_dir = os.path.join(vault_root, MEM_LOG_DIR, CATCHALL_PROJECT)
    if not os.path.isdir(log_dir):
        return []
    ids = set()
    for name in sorted(os.listdir(log_dir)):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(log_dir, name), "r",
                      encoding="utf-8", errors="ignore") as f:
                ids.update(int(m) for m in CATCHALL_ID_RE.findall(f.read()))
        except OSError:
            continue
    return sorted(ids)


def observations(project, since, vault_root=None):
    """Live observations for a project, oldest first, within a char budget.

    Superseded and compressed rows are skipped: a note compiled from a record
    that mem-lite itself has retired would reintroduce a fact the DB already
    knows is stale.

    The catch-all note is the one project whose material is not stored under
    its own name -- mem-lite scattered it across a key per directory -- so it
    selects by the ids catchall_ids() reads off its own raw day files instead.
    Without a vault_root it has no way to resolve those, and selecting by the
    name `genel` would quietly compile an empty note; returning nothing says
    the same thing without writing anything.
    """
    if project == CATCHALL_PROJECT:
        ids = catchall_ids(vault_root) if vault_root else []
        if not ids:
            return [], 0
        placeholders = ",".join("?" * len(ids))
        where, params = f"id IN ({placeholders})", list(ids)
    else:
        where, params = "project = ?", [project]
    query = (
        "SELECT created_at, type, title, subtitle, lesson_learned, narrative, "
        f"importance FROM observations WHERE {where} "
        "AND superseded_at IS NULL AND compressed_into IS NULL"
    )
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


def card_heading(project):
    """The heading stamped over a note's injected card.

    Read in two places that must not drift: the prompt tells the model which
    heading it is filling, and apply_compiled writes it. Naming them separately
    is how the model gets asked for one section and the writer produces
    another.
    """
    return CATCHALL_CARD_HEADING if project == CATCHALL_PROJECT else CARD_HEADING


def system_prompt(project):
    """Repo compiler or general compiler -- the subject is not the same."""
    return CATCHALL_SYSTEM_PROMPT if project == CATCHALL_PROJECT else SYSTEM_PROMPT


def build_prompt(project, note_name, current, records, total, dropped):
    material = "\n\n".join(r["text"] for r in records)
    scope = (
        f"{len(records)} kayıt veriliyor"
        + (f" ({total} kaydın en önemli/en yenileri; {dropped} tanesi yer nedeniyle "
           "dışarıda bırakıldı)" if dropped else "")
    )
    catchall = project == CATCHALL_PROJECT
    subject = CATCHALL_SUBJECT if catchall else f"`{project}` deposunun"
    about = "o işler" if catchall else "o depo"
    heading = card_heading(project)
    card_rule = CATCHALL_CARD_RULE if catchall else (
        "3-6 madde. Bu bölüm her oturumun başında ajana enjekte edilen karttır — "
        "depoyu ilk kez açan birinin bilmesi gereken yapısal gerçekler olsun, "
        "haber değil."
    )
    return f"""Aşağıda {subject} vault notu ve {about} hakkında biriken ham kayıtlar var. {scope}.

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
- "{heading}": {card_rule}
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


def run_model(prompt, model, timeout, system=SYSTEM_PROMPT):
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
    # Clearing the variables is not enough: the installer also writes them to
    # ~/.config/dev-agent-kit/vault.env, and an absent VAULT_DIR is precisely
    # the case that file exists to answer, so the child resolved the vault
    # anyway. VAULT_HOOKS_OFF is the explicit off the contract was missing.
    env["VAULT_HOOKS_OFF"] = "1"
    for key in ("VAULT_DIR", "MEM_OBSIDIAN_VAULT"):
        env[key] = ""
    env["VAULT_COMPILE_CHILD"] = "1"
    cmd = [
        "claude", "-p",
        "--model", model,
        "--strict-mcp-config",
        "--no-session-persistence",
        "--system-prompt", system,
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


# --------------------------------------------------------------------------
# the profile note


def auto_facts(text, since):
    """Machine-captured lines from the profile note, newest-first date filter.

    personal-capture writes into its own section and stamps each line, and the
    section's own heading promises that nothing there reaches the injected card
    on its own. That promise is what makes a local 27B safe to run broadly: it
    can over-capture, because a second pass decides what is durable. This is
    that second pass, so it reads the section rather than replacing it.
    """
    if AUTO_HEADING not in text:
        return []
    body = text.split(AUTO_HEADING, 1)[1]
    nxt = re.search(r"^## ", body, re.MULTILINE)
    if nxt:
        body = body[:nxt.start()]
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        stamp = AUTO_STAMP.search(line)
        if since is not None and stamp and stamp.group(1) <= since.strftime("%Y-%m-%d"):
            continue
        out.append(AUTO_STAMP.sub("", line).strip())
    return out


def build_profile_prompt(current_card, facts):
    material = "\n".join(facts)
    return f"""Aşağıda bir kullanıcının ajan kartı ve o kullanıcı hakkında oturumlarda otomatik yakalanmış ham cümleler var.

Görevin: KARTI yeniden yazmak. Kart, kullanıcıyla çalışan her ajana her oturumun başında enjekte edilir — yani biyografi değil, ÇALIŞMA TALİMATIDIR.

KARTIN ŞU ANKİ HALİ:
---
{current_card}
---

OTOMATİK YAKALANMIŞ HAM CÜMLELER (hepsi doğru veya kalıcı değil, eleme senin işin):
---
{material}
---

KURALLAR:
- MEVCUT KART MADDELERİ VARSAYILAN OLARAK KALIR. Ancak ham cümlelerde açıkça çeliştiğine dair kanıt varsa değiştir. "Yer kalmadı" gerekçe değildir.
- Ham cümlelerden SADECE kalıcı olanları al. Şunları ASLA karta yazma:
  * geçici durum ("son günlerde X kullanmıyor", "şu an Y projesi üzerinde çalışıyor")
  * üzerinde çalışılan projenin bilgisi, amacı veya kararları (bunlar repo notlarının işi)
  * aynı şeyin farklı kelimelerle tekrarı — tek maddede birleştir
- Bir cümlenin kalıcı mı geçici mi olduğundan emin değilsen ALMA. Kart küçük ve doğru olmalı, geniş ve şüpheli değil.
- Her madde ajanın davranışını değiştirecek bir şey söylesin: nasıl anlatılmasını istediği, neyi varsayabileceğin, neyi yapmaman gerektiği.
- En fazla 12 madde. Her madde `- ` ile başlar; bir maddenin devamı girintili satır olarak gelebilir.

ÇIKTI BİÇİMİ — tam olarak bu, başka hiçbir şey yazma:
{MARK_CARD}
- ...
"""


def parse_card(raw):
    """The marked block as lines, or (None, reason).

    Bullets may wrap onto indented continuation lines, so this keeps the block
    verbatim instead of filtering to lines that start with a dash -- dropping
    the continuations would silently truncate half the sentences in the card.
    """
    if not raw:
        return None, "empty"
    idx = raw.find(MARK_CARD)
    if idx < 0:
        return None, "missing-card"
    lines = [ln.rstrip() for ln in raw[idx + len(MARK_CARD):].splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not any(ln.strip().startswith("- ") and len(ln.strip()) > 3 for ln in lines):
        return None, "no-bullets"
    return lines, None


def compile_profile(path, args, vault_root):
    rel = os.path.relpath(path, vault_root)
    with open(path, "r", encoding="utf-8") as f:
        current = f.read()

    since = last_compiled(current)
    facts = auto_facts(current, since)
    if not facts:
        log("  · profil: yeni yakalanmış gerçek yok, atlandı")
        return "skip-no-new"
    log(f"  → profil: {len(facts)} yeni yakalanmış cümle — {rel}")
    if args.no_model:
        return "planned"

    card = agent_card(current) or ""
    raw, err = run_model(build_profile_prompt(card, facts), args.model, args.timeout,
                         system=PROFILE_SYSTEM_PROMPT)
    if err:
        log(f"  ! profil: model çağrısı başarısız ({err})")
        record_metric("vault-compile", "error", vault_root, f"profile:{err}")
        return "error"
    lines, why = parse_card(raw)
    if lines is None:
        log(f"  ! profil: çıktı doğrulanamadı ({why}) — kart değiştirilmedi")
        record_metric("vault-compile", "invalid", vault_root, f"profile:{why}")
        if args.debug:
            log((raw or "")[:1500])
        return "invalid"

    updated = replace_card(current, lines, heading=None)
    updated = stamp_compiled(updated, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    if updated == current:
        log("  · profil: değişiklik yok")
        return "unchanged"
    if not args.apply:
        print(unified_diff(current, updated, rel))
        return "dry-run"
    saved = backup(path, vault_root)
    if saved is None:
        record_metric("vault-compile", "error", vault_root, "profile:backup-failed")
        return "error"
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    log(f"  ✓ profil: yazıldı → {rel}")
    record_metric("vault-compile", "compile", vault_root, f"profile:{len(facts)}fact:{rel}")
    return "compiled"


def unified_diff(before, after, name):
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}", n=1))


def source_links(project, vault_root):
    """`[[...]]` links to the raw day files this project's note is built from.

    The vault contract asks for these by name: obsidian-mirror writes
    `_mem-log/<project>/<date>.md` with no frontmatter and no links, and the
    dataviewjs lists in a repo note are render-time queries that never enter
    Obsidian's link index -- so without a real wikilink somewhere, every day
    file is an orphan. All ten of them were.

    The links are vault-root-relative rather than the `../../../` the template
    shows. Depth is not a constant: a repo note sits three levels down under
    `03- Personal Projects/` but four under `02- Kuartis/03- Dearsan/Kida/`,
    so a fixed number of `../` is right for one tree and broken for the other.
    A bare `[[2026-08-26]]` is not an option either -- day files share their
    basename across projects, so it would resolve to whichever one Obsidian
    happened to pick.
    """
    log_dir = os.path.join(vault_root, MEM_LOG_DIR, project)
    if not os.path.isdir(log_dir):
        return []
    days = sorted(
        (os.path.splitext(n)[0] for n in os.listdir(log_dir) if n.endswith(".md")),
        reverse=True,
    )
    return [f"- [[{MEM_LOG_DIR}/{project}/{d}|{d} ham log]]" for d in days]


def refresh_sources(path, project, args, vault_root):
    """Keep `## Kaynaklar` current whether or not the note gets compiled.

    Doing this only inside compile_note would leave the links as stale as the
    compile threshold allows: obsidian-mirror files a new day file every day a
    project sees work, but a project under the threshold is skipped before the
    section is ever rewritten, so each of those days is an orphan until enough
    observations pile up. Costs nothing to run -- the links come off the
    filesystem, not the model -- so it has no reason to wait on a compile.
    """
    links = source_links(project, vault_root)
    if not links:
        return False
    with open(path, "r", encoding="utf-8") as f:
        current = f.read()
    updated = replace_section(current, SOURCES_HEADING, links)
    if updated == current:
        return False
    if not args.apply:
        print(unified_diff(current, updated, os.path.relpath(path, vault_root)))
        return False
    if backup(path, vault_root) is None:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    return True


def apply_compiled(current, parsed, project, vault_root, day):
    """Fold one model result into the note text.

    Extracted from compile_note so the outcome is testable: a test that only
    exercised source_links() stayed green when the call that writes the
    section was deleted outright, which is the same shape of miss as testing
    an off-switch's mechanism instead of whether it switched anything off.
    """
    updated = replace_card(current, parsed["arch"], card_heading(project))
    updated = replace_section(updated, SECTIONS[0], parsed["decisions"])
    updated = replace_section(updated, SECTIONS[1], parsed["questions"])
    links = source_links(project, vault_root)
    if links:
        updated = replace_section(updated, SOURCES_HEADING, links)
    return stamp_compiled(updated, day)


def compile_note(path, project, args, vault_root):
    rel = os.path.relpath(path, vault_root)
    with open(path, "r", encoding="utf-8") as f:
        current = f.read()

    since = last_compiled(current)
    records, total = observations(project, since, vault_root)
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
    raw, err = run_model(prompt, args.model, args.timeout, system_prompt(project))
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

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    updated = apply_compiled(current, parsed, project, vault_root, day)

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
    ap.add_argument("--all", action="store_true",
                    help="every note with a mem_lite_project key, plus the profile")
    ap.add_argument("--profile", action="store_true",
                    help="only the profile note (agent_profile: true)")
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

    tally = {}
    if args.all or args.profile:
        # The profile's material is what personal-capture wrote, not mem-lite:
        # the local 27B captures broadly on purpose, and this pass is the one
        # that decides which of those lines are durable enough to be injected
        # into every session. Recall there, precision here.
        profile_path, _ = find_note(vault_root, PROFILE_MARKER)
        if profile_path:
            outcome = compile_profile(profile_path, args, vault_root)
            tally[outcome] = tally.get(outcome, 0) + 1
        else:
            log("`agent_profile: true` taşıyan not yok — profil atlandı")
        if args.profile:
            log("— özet: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
            return 0 if not tally.get("error") and not tally.get("invalid") else 2

    notes = preferred_note(list(iter_notes(vault_root)))
    # Every key any note claims, captured before the run is narrowed to one
    # note: this is what tells the catch-all which keys are strays, and a set
    # built from the narrowed list would call every other repo a stray.
    owned = {n[2] for n in notes}
    if not args.all:
        wanted = args.project or infer_project(os.getcwd())
        notes = [n for n in notes if n[2] == wanted]
        if not notes:
            log(f"`mem_lite_project: {wanted}` taşıyan not yok — hangi klasöre "
                "ait olduğu kullanıcı kararı, bu araç not yaratmaz.")
            return 1

    log(f"{len(notes)} not incelenecek ({'YAZIM' if args.apply else 'kuru koşu'}, model={args.model})")
    for i, (path, _head, project) in enumerate(notes):
        if args.limit and i >= args.limit:
            break
        outcome = compile_note(path, project, args, vault_root)
        tally[outcome] = tally.get(outcome, 0) + 1
        # After the compile, so a note that was just rewritten is not backed up
        # and written a second time for the same links.
        if outcome not in ("dry-run", "error") and refresh_sources(
                path, project, args, vault_root):
            tally["sources-linked"] = tally.get("sources-linked", 0) + 1

    if args.all:
        for project, count in unowned_projects(owned):
            log(f"  ? {project}: {count} kayıt var, bunu sahiplenen not yok — "
                "`mem_lite_project:` taşıyan bir not aç")
        # Repo-less material is scattered across keys that are each far under
        # NOTE_THRESHOLD, so the loop above is silent about every one of them.
        # Without this, the one note that has to exist for work outside a repo
        # would be the only note nothing ever asks for.
        if CATCHALL_PROJECT not in owned:
            ids = catchall_ids(vault_root)
            if ids:
                log(f"  ? {CATCHALL_PROJECT}: repo dışı {len(ids)} kayıt var, "
                    "bunu sahiplenen not yok — "
                    f"`mem_lite_project: {CATCHALL_PROJECT}` taşıyan bir not aç")

    log("— özet: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 0 if not tally.get("error") and not tally.get("invalid") else 2


if __name__ == "__main__":
    sys.exit(main())
