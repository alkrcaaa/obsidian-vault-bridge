#!/usr/bin/env python3
"""
concept-capture hook (Stop).

The third write path of this bridge. The first two learn about a *repo*
(mem_save -> obsidian-mirror -> vault-compile -> Code/<repo>.md) and about
the *user* (personal-capture -> the profile note). Neither catches the
thing that actually taught the user something: a concept explained at
length in the middle of ordinary work -- how a scheduler places a pod, what
Terraform state is for -- which lives in the transcript and dies with it.

Two measurements shaped what this is, and what it is not:

  * There is no such thing as a "teaching session" here. Over 90 days, 4 of
    69 sessions carried more learning signal than work signal, and even
    those were work: the session where Kubernetes actually got taught scored
    4 learning cues against 38 work cues. Teaching is a mode inside a work
    session, so this hook classifies *content*, never the session.

  * A cheap structural trigger does not exist. The best signal a hook can
    see without a model -- a long assistant turn that calls no tool -- is
    present in 85% of sessions, because that is also how an agent reports
    finished work. So the judgment has to be a model's.

What the model is asked to do is the load-bearing decision. It does NOT
write an article about the concept: the explanation was already written, in
this session, by the strong model the user was talking to. A local 27B
asked to synthesise a concept note is at exactly its ceiling, and a wrong
sentence is most expensive in a note the user reads *in order to learn*.
So the task here is extraction -- pull the passages that explain something
repo-independent, drop the rest -- which is the same shape of task
personal-capture already does reliably on the user's own prose.

The safety story is personal-capture's, one step stricter because this one
chooses its own filename:

  * It writes only under the wiki folder, only into its own dated section
    at the end of a note, and never inside an `<!-- agent-card -->` block.
  * A note it creates is stamped `status: draft` / `auto_compiled: true`.
    Promotion to a real, human-shaped article stays a decision for the user
    or a strong-model session -- this hook only ever gathers the raw
    understanding that used to evaporate.
  * The concept name comes from the model, so it is sanitised and confined:
    a path that does not resolve inside the wiki folder is dropped, and a
    note carrying `mem_lite_project:` or `agent_profile: true` is never
    touched even if the name happens to collide.
  * Existing note titles are shown to the model so it reuses one instead of
    inventing a fifth spelling of the same subject.
  * Anything matching a credential pattern never leaves the machine.

Off unless both VAULT_DIR and QWEN_BASE_URL are set. Fail-open everywhere.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vault_common import (
        env_or_conf, frontmatter_field, is_new_line, read_text, record_metric,
        vault_dir as resolve_vault,
    )
except Exception:
    sys.exit(0)

AUTO_SECTION = "## Otomatik Yakalananlar"
DEFAULT_WIKI_DIR = "08- Wiki"
MIN_TURN_CHARS = 1200     # below this a turn is an answer, not an explanation
MIN_CHARS = 2000          # below this the session explained nothing at length
MAX_PROMPT_CHARS = 14000  # newest turns only; a 27B degrades on a long tail
TIMEOUT = 120
MAX_LINES = 5

MACHINE_MARKERS = (
    "<local-command", "<command-name>", "<system-reminder>", "Caveat:",
    "[Request interrupted", "[Usage limit", "<session-handoff",
    "<qwen:user-prompt-submit-context", "<qwen:session-start-context",
)

SECRET_MARKERS = re.compile(
    r"(password|passwd|şifre|parola|secret|api[_-]?key|token\s*[=:]|bearer\s|"
    r"BEGIN [A-Z ]*PRIVATE KEY|ssh-rsa\s)", re.IGNORECASE
)

# Fenced code is the most repo-specific part of any explanation and the
# bulkiest: stripping it keeps the prompt inside the window and keeps the
# note from filling up with one project's function names.
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

PROMPT = """Aşağıda bir yazılım oturumunda bir asistanın kullanıcıya YAZDIĞI \
uzun açıklamalar var.

Görevin: bu açıklamalarda REPO'DAN BAĞIMSIZ bir kavramın öğretilip \
öğretilmediğine karar vermek ve öğretildiyse o açıklamayı ÇIKARMAK.

ÇOK ÖNEMLİ: Kavramı kendin ANLATMA. Kendi bilgini ekleme. Sadece aşağıdaki \
metinde geçen cümleleri al, gerekiyorsa kısalt. Metinde olmayan hiçbir bilgi \
çıktına giremez.

Repo'dan bağımsız kavram = başka bir projede de doğru kalan bilgi. Örnekler:
- "Kubernetes'te scheduler bir pod'u nasıl yerleştirir"
- "Terraform state ne işe yarar, neden kilitlenir"
- "Docker layer cache neden invalidate olur"

TEK ÖLÇÜT: cümleyi bu projeyi hiç bilmeyen biri okusa hâlâ doğru ve faydalı \
mı? Değilse atla.

Bunları ASLA çıkarma:
- bu repo'ya özgü şeyler (dosya adı, fonksiyon adı, bu projenin mimarisi) — \
onlar başka bir nota kaydediliyor
- bu ortamın kendi envanteri: makine/node/sunucu adı, kaç tanesi bozuk, \
hangi cihazda ne çıktı, bu kurulumun mevcut durumu. Kavram genel, bulgu \
yerel — sadece genel olanı al.
- yapılan işin raporu ("şunu düzelttim", "testler geçti", "3 dosya değişti")
- plan, öneri, seçenek listesi, özür, durum özeti
- kullanıcıya söylenmiş söz: soru, onay isteme, "sen/senin" diye hitap. Her \
satır bir BİLGİ cümlesi olmalı, konuşmanın parçası değil.
- kullanıcı hakkında çıkarım

MEVCUT NOT BAŞLIKLARI:
{titles}
Açıklanan kavram bu başlıklardan birinin kapsamına giriyorsa, "konu" alanına \
o başlığı HARFİ HARFİNE yaz — aynı konuyu ikinci bir yazımla açma. \
Hiçbiri kapsamıyorsa yeni ve kısa bir başlık yaz. Listede yakın duran bir \
başlığa ZORLAMA: alakasız bir nota yazmak, yeni not açmaktan daha kötüdür.

Çıktı: SADECE tek bir JSON nesnesi, başka hiçbir şey yok:
{{"konu": "<kavram başlığı>", "satirlar": ["<metinden alınmış cümle>", ...]}}

Metinde birden fazla kavram anlatılmış olabilir. SADECE BİR TANESİNİ seç — \
en çok yer verileni. "satirlar" içine yalnızca o kavramla ilgili cümleler \
girer; başka bir kavramla ilgili cümle ne kadar iyi olursa olsun ATLA.

En fazla {max_lines} satır. Her satır tek başına anlamlı olsun. Emin \
değilsen az yaz. Repo'dan bağımsız bir kavram öğretilmemişse: {{}}

AÇIKLAMALAR:
---
{explanations}
---
JSON:"""


def _blocks(message):
    """Text blocks of one record, across both hosts' transcript shapes.

    Claude writes `message.content` (a string, or typed blocks); Qwen writes
    `message.parts` (untyped objects carrying `text`). A hook that reads only
    Claude's shape dispatches on Qwen every session and always finds
    nothing -- which is what the Qwen side of personal-capture actually did
    until it was taught both.
    """
    content = message.get("content")
    if isinstance(content, str):
        return [content], False
    blocks = content if isinstance(content, list) else message.get("parts")
    if not isinstance(blocks, list):
        return [], False
    texts = []
    used_tool = False
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" or "functionCall" in b:
            used_tool = True
            continue
        if isinstance(b.get("text"), str) and b.get("type", "text") == "text":
            texts.append(b["text"])
    return texts, used_tool


def _explanations(path):
    """Long prose-only assistant turns, oldest first.

    A turn that called a tool is doing work, not explaining; the
    explanations that matter here are the ones written straight to the user.
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "assistant":
                    continue
                texts, used_tool = _blocks(rec.get("message") or {})
                if used_tool or not texts:
                    continue
                text = FENCE.sub("", "\n".join(texts)).strip()
                if len(text) < MIN_TURN_CHARS:
                    continue
                if any(m in text[:400] for m in MACHINE_MARKERS):
                    continue
                if SECRET_MARKERS.search(text):
                    continue  # never leaves the machine, never gets written
                if text not in out:
                    out.append(text)
    except OSError:
        return []
    return out


def wiki_dir(vault):
    """The folder concept notes live in, and the only place this may write."""
    name = env_or_conf("VAULT_WIKI_DIR") or DEFAULT_WIKI_DIR
    path = name if os.path.isabs(name) else os.path.join(vault, name)
    return path if os.path.isdir(path) else None


def _titles(wiki):
    """Existing note titles, so the model reuses one instead of inventing.

    Without this the same subject arrives as "Kubernetes Scheduling" one
    week and "K8s scheduler" the next, and the folder grows four thin notes
    where it should have grown one.
    """
    try:
        names = sorted(n[:-3] for n in os.listdir(wiki) if n.endswith(".md"))
    except OSError:
        return []
    # The folder note (same name as the folder) is Obsidian's index, not a
    # subject -- offering it as a title invites writes into the index.
    return [n for n in names if n != os.path.basename(wiki)][:60]


def _ask_model(base_url, explanations, titles):
    listed = "\n".join(f"- {t}" for t in titles) or "- (henüz not yok)"
    body = json.dumps({
        "model": os.environ.get("CONCEPT_CAPTURE_MODEL",
                                os.environ.get("PERSONAL_CAPTURE_MODEL",
                                               "/models/qwen3.8-27b")),
        "messages": [{"role": "user", "content": PROMPT.format(
            titles=listed, max_lines=MAX_LINES, explanations=explanations)}],
        "temperature": 0.2,
        "max_tokens": 1600,
        # Qwen3 bills its reasoning against max_tokens: on a real prompt it
        # spent the whole budget thinking and returned content=None, which
        # reads exactly like "nothing was taught here". This is extraction,
        # not a problem that needs deliberation.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body, headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        payload = json.load(resp)
    text = payload["choices"][0]["message"].get("content")
    if not text:
        return None, []
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None, []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None, []
    if not isinstance(data, dict):
        return None, []
    topic = data.get("konu")
    lines = data.get("satirlar")
    if not isinstance(topic, str) or not isinstance(lines, list):
        return None, []
    clean = [ln.strip() for ln in lines if isinstance(ln, str) and ln.strip()]
    # A question is never knowledge. On a real session the model returned four
    # good lines and then "has anyone forked this repo?" -- a sentence it had
    # asked the user, extracted faithfully and worthless in a note. The prompt
    # asks for statements; this makes the one mechanical case of the rule
    # something the model cannot get wrong.
    clean = [ln for ln in clean if not ln.rstrip().endswith("?")]
    return topic.strip(), clean[:MAX_LINES]


def note_path(wiki, topic):
    """Where a topic is filed, or None when the name cannot be trusted.

    The name comes out of a model, so it is treated as untrusted input. A
    topic carrying a path separator is rejected outright rather than
    scrubbed: "../../Code/repo" is not a badly spelled subject, it is the
    model answering a different question, and scrubbing it would file a real
    capture under the junk name that survives the scrub. What remains is
    then matched case-insensitively against the notes already there, and the
    result still has to resolve back inside the wiki folder.
    """
    if "/" in topic or "\\" in topic or ".." in topic:
        return None
    stem = re.sub(r"[:*?\"<>|\n\r\t]", " ", topic).strip().strip(".")
    stem = re.sub(r"\s+", " ", stem)[:80].strip()
    if len(stem) < 3:
        return None
    try:
        for name in os.listdir(wiki):
            if name.endswith(".md") and name[:-3].lower() == stem.lower():
                stem = name[:-3]
                break
    except OSError:
        pass
    path = os.path.join(wiki, stem + ".md")
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(wiki):
        return None
    return path


def _append(path, topic, lines):
    """Add what is new to the note's own dated section. Returns lines written."""
    text = read_text(path, limit=400000) if os.path.exists(path) else ""
    if text:
        # Defence in depth: those two fields mark the notes the other two
        # write paths own -- a repo note (injected into every session of that
        # repo) and the profile note. A name collision must never let this
        # hook write into one of them.
        if (frontmatter_field(text, "mem_lite_project")
                or frontmatter_field(text, "agent_profile")):
            return 0
    fresh = [ln for ln in lines if is_new_line(ln, text.splitlines())]
    if not fresh:
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    body = "\n".join(f"- {ln} <!-- auto:{today} -->" for ln in fresh) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        if not text:
            f.write(
                "---\n"
                "type: wiki\n"
                "status: draft\n"
                "auto_compiled: true\n"
                f"created: {today}\n"
                "tags: []\n"
                "---\n\n"
                f"# {topic}\n\n"
            )
        if not text or AUTO_SECTION not in text:
            f.write(
                f"\n{AUTO_SECTION}\n"
                "Oturum sonunda otomatik çıkarılan açıklamalar (lokal model,\n"
                "kaynak: oturumun kendi metni). Ham malzemedir — düzenlenmiş\n"
                "bir makaleye dönüştürmek insanın/güçlü modelin kararıdır.\n"
                "Yanlış bir satırı silmek yeterli.\n\n"
            )
        f.write(body)
    return len(fresh)


def _progress_path(session_id):
    session = re.sub(r"[^A-Za-z0-9_.-]", "-", str(session_id or "")[:60])
    if not session:
        return None
    return os.path.join(tempfile.gettempdir(), f"concept-capture-{session}.chars")


def _progress(path):
    if not path:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _record_progress(path, seen):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(seen))
    except OSError:
        pass


def _worker(transcript, wiki, base_url):
    """Run the capture, and say why on every path that writes nothing.

    This runs detached, so its only channel is the metric line. Without a
    reason on the empty paths, "nothing was taught" and "the hook never ran"
    leave the same trace -- and that ambiguity is what let the rest of this
    bridge look alive while doing nothing for weeks.
    """
    def quiet(reason):
        record_metric("concept-capture", "skip", wiki, reason)

    turns = _explanations(transcript)
    if not turns:
        return quiet("no-explanations")
    joined = "\n\n---\n\n".join(turns)
    if len(joined) < MIN_CHARS:
        return quiet("too-short")
    if len(joined) > MAX_PROMPT_CHARS:
        joined = joined[-MAX_PROMPT_CHARS:]
    try:
        topic, lines = _ask_model(base_url, joined, _titles(wiki))
    except Exception:
        return quiet("model-error")
    if not topic or not lines:
        return quiet("no-concept")
    path = note_path(wiki, topic)
    if not path:
        return quiet("bad-topic")
    try:
        written = _append(path, topic, lines)
    except OSError:
        return quiet("write-error")
    if written:
        record_metric("concept-capture", "capture", wiki, f"{written}line")
    else:
        quiet("all-known")  # the note already made every point returned


def main():
    if len(sys.argv) > 4 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3], sys.argv[4])
        return

    vault = resolve_vault()
    base_url = env_or_conf("QWEN_BASE_URL")
    if not vault or not base_url:
        sys.exit(0)
    wiki = wiki_dir(vault)
    if not wiki:
        record_metric("concept-capture", "skip", vault, "no-wiki-dir")
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript = data.get("transcript_path") or ""
    if not transcript or not os.path.isfile(transcript):
        # Configured but handed nothing to read: a host that does not pass a
        # transcript on Stop switches this hook off without ever saying so.
        record_metric("concept-capture", "skip", os.getcwd(), "no-transcript")
        sys.exit(0)

    # Stop fires at the end of every turn, not once per session. A flag set on
    # the first Stop would pin this to the opening turn -- the turn least
    # likely to contain an explanation, since nothing has been discussed yet.
    # The marker holds progress instead: dispatch again once another
    # MIN_CHARS of explanation has accumulated, the same floor the worker
    # needs before it can say anything.
    progress = _progress_path(data.get("session_id"))
    seen = _progress(progress)
    total = sum(len(t) for t in _explanations(transcript))
    if total - seen < MIN_CHARS:
        reason = "no-explanations" if not total else (
            "too-short" if not seen else "no-new-explanations")
        record_metric("concept-capture", "skip", os.getcwd(), reason)
        sys.exit(0)

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker",
             transcript, wiki, base_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        record_metric("concept-capture", "skip", wiki, "spawn-failed")
        sys.exit(0)
    _record_progress(progress, total)
    # The worker is detached and reports for itself; this pairs with whatever
    # it records, so a dispatch with no follow-up means the worker died.
    record_metric("concept-capture", "dispatch", wiki)
    sys.exit(0)


if __name__ == "__main__":
    main()
