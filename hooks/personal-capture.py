#!/usr/bin/env python3
"""
personal-capture hook (Stop).

The repo half of this bridge learns on its own: an agent calls mem_save,
obsidian-mirror files it, the daily vault-compile run synthesises it. The half
about the *user* had no such trigger. Facts about a person do not arrive as
a tool call -- they arrive as ordinary prose in a prompt ("I never read
articles, mostly git repos"), and a hook is a regex, not a reader. So the
one thing that could notice was a model, and nothing was asking one.

This hook asks one. When a session ends it takes what the user actually
typed, sends it to a local vLLM (QWEN_BASE_URL) and writes durable facts
into the vault's profile note. No Claude tokens, no turn, no prompt for
approval -- the user asked for capture without a checkpoint.

What keeps that safe:

  * It writes into its own dated section, never into the hand-written prose
    above it, and stamps every line with the date it was captured. A wrong
    line stays findable and deletable.
  * It never touches the `<!-- agent-card -->` block. That block is what
    vault-inject puts into every future session, so an unsupervised write
    there would amplify a bad inference into every conversation. Promoting
    a captured fact into the card stays a human (or agent) decision.
  * Prompts that look like credentials are dropped before anything leaves
    the machine, and never written.
  * Only what the user typed is sent -- not tool output, not file contents,
    not the assistant's own words.

The model call runs detached: a Stop hook holds up the end of the turn, and
a 27B on a LAN box takes seconds. The parent returns immediately.

Off unless both VAULT_DIR and QWEN_BASE_URL are set, and unless some note
opts in with `agent_profile: true`. Fail-open everywhere else.
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
        env_or_conf, find_note, locked_note, record_metric,
        # Shared with concept-capture: both append to a note they will append
        # to again next session, so "does the note already say this" has to be
        # answered the same way in both or one of them starts duplicating.
        is_new_line as _is_new,
        vault_dir as resolve_vault,
    )
except Exception:
    sys.exit(0)

AUTO_SECTION = "## Otomatik Yakalananlar"
MIN_CHARS = 200          # below this a session has not said anything about anyone
MAX_PROMPT_CHARS = 12000  # newest messages only; a 27B degrades on a long tail
TIMEOUT = 90
MAX_FACTS = 3
# is_new_line compares stems, so it only catches a restatement that reuses the
# note's words. The 27B rewords the same observation every session: measured
# 2026-09-12, 66 captured lines in 7 days, ~60% of them facts the card already
# made or an earlier line said in other words (design taste alone 8 times).
# Only something that reads meaning can reject those, so the model is shown
# the profile it is adding to.
MAX_KNOWN_CHARS = 20000
# Backstop for when the model misjudges anyway: unreviewed lines are a queue
# for a human (OKM, vault CLAUDE.md 2c), and a queue nobody drains must stop
# growing instead of burying the few lines worth promoting.
MAX_PENDING = 25
# Below the shared 0.6: a profile fact is one short sentence restated with the
# same few content words ("commit mesajları kısa" reworded shares 3 of 6
# stems). Measured 2026-09-12 on the live profile: 0.5 kept 6/6 new facts and
# blocked 3/4 rewordings, 0.6 blocked 2/4, 0.45 started dropping new facts.
DEDUP_THRESHOLD = 0.5

# Machine-generated user records: slash commands, hook injections, interrupt
# notices. They are not the user talking, and they dominate by volume.
MACHINE_MARKERS = (
    "<local-command", "<command-name>", "<system-reminder>", "Caveat:",
    "[Request interrupted", "[Usage limit", "<session-handoff",
    # Qwen files its own injected hook context as another part of the user's
    # message, so this has to be dropped part by part -- checking the joined
    # message would let it ride along behind whatever the user actually typed.
    # Named exactly: a bare "<qwen:" also matches a user asking about one of
    # these blocks, and silently deletes the very message they typed.
    "<qwen:user-prompt-submit-context", "<qwen:session-start-context",
)

SECRET_MARKERS = re.compile(
    r"(password|passwd|şifre|parola|secret|api[_-]?key|token\s*[=:]|bearer\s|"
    r"BEGIN [A-Z ]*PRIVATE KEY|ssh-rsa\s)", re.IGNORECASE
)

PROMPT = """Aşağıda bir kullanıcının bir yazılım oturumunda YAZDIĞI mesajlar var.

Görevin: bu mesajlardan kullanıcının KENDİSİ hakkında KALICI olan gerçekleri çıkarmak.

Kalıcı gerçek = kişi değişmedikçe doğru kalan şey. Örnekler:
- alışkanlık ("makale okumam, git repolarını okurum")
- tercih ("onay sormadan kaydetmeni istiyorum")
- kimlik/geçmiş ("4 yıldır Kuartis'te çalışıyorum")
- öğrenme biçimi ("önce somut bir mekanizmadan başlayarak öğreniyorum")

Kalıcı DEĞİL, bunları ASLA çıkarma:
- bu oturuma özgü görev/istek ("şu hook'u yaz", "devam edelim", "commit at")
- kod, repo, dosya, mimari hakkında bilgi (onlar başka yere kaydediliyor)
- üzerinde çalışılan sistemin ne yapması gerektiği, amacı, tasarımı — bu
  projenin bilgisidir, kişinin değil
- geçici durum ("şu an hata alıyorum")
- senin çıkarımın/yorumun — kullanıcı söylememişse yazma

Çıktı: SADECE bir JSON dizisi, başka hiçbir şey yok. Her eleman tek cümlelik
bir gerçek, kullanıcı hakkında 3. tekil şahısla yazılmış (ör.
"Makale okumuyor, bilgiyi git repolarından ve haberlerden alıyor.").

EN FAZLA 3 gerçek döndür — en kalıcı, en çok tekrar edeceklerini seç. Aynı
şeyi farklı kelimelerle iki kez yazma. Emin değilsen az yaz.
Hiç kalıcı gerçek yoksa boş dizi döndür: []

PROFİLDE ZATEN YAZANLAR — bunları, ya da aynı anlama gelen bir cümleyi
(farklı kelimelerle olsa bile) ASLA döndürme. Sadece burada olmayan yeni bir
gerçek döndür:
---
{known}
---

MESAJLAR:
---
{messages}
---
JSON:"""


def _record_text(rec):
    """The user's words in one transcript record, across both hosts' shapes.

    Claude writes `message.content` (a string, or blocks with `type: text`);
    Qwen writes `message.parts` (objects carrying `text`, no type tag) and
    appends its own hook context as a further part of the same message. The
    two hosts share this hook, so reading only Claude's shape means Qwen
    dispatches the worker every session and it always finds nothing -- which
    is what the first live Qwen run actually did.
    """
    message = rec.get("message") or {}
    content = message.get("content") or rec.get("content")
    if isinstance(content, str):
        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
        if m:
            return m.group(1).strip()
        return content
    blocks = content if isinstance(content, list) else message.get("parts")
    if not isinstance(blocks, list):
        return None
    kept = []
    for b in blocks:
        if not isinstance(b, dict) or not isinstance(b.get("text"), str):
            continue
        if "type" in b and b["type"] != "text":
            continue
        text = b["text"]
        if any(m in text[:400] for m in MACHINE_MARKERS):
            continue
        kept.append(text)
    return "\n".join(kept)


def _user_messages(path):
    """What the user typed, oldest first, machine records removed."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                is_user = (
                    rec.get("type") in ("user", "USER_INPUT")
                    or rec.get("source") == "USER_EXPLICIT"
                )
                if not is_user:
                    continue
                content = _record_text(rec)
                if not isinstance(content, str):
                    continue
                text = content.strip()
                if not text or any(m in text[:400] for m in MACHINE_MARKERS):
                    continue
                if SECRET_MARKERS.search(text):
                    continue  # never leaves the machine, never gets written
                if text not in out:
                    out.append(text)
    except OSError:
        return []
    return out


def _get_model(base_url, env_var, default="/models/qwen3.6-27b"):
    configured = os.environ.get(env_var)
    if configured:
        return configured
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/models")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.load(resp)
            models = data.get("data", [])
            if models and "id" in models[0]:
                return models[0]["id"]
    except Exception:
        pass
    return default


def _pending(text):
    """Captured lines still waiting in the auto section for a human decision."""
    _, _, section = text.partition(AUTO_SECTION)
    return sum(1 for line in section.splitlines()
               if line.startswith("- ") and "<!-- auto:" in line)


def _logical_lines(text):
    """The note's bullets and paragraphs, each rejoined into one line.

    The profile's prose is hard-wrapped at ~80 columns, so one bullet spans
    three or four physical lines and a restatement never shares 60% of its
    stems with any single one of them: is_new_line waved through every
    rewording of a wrapped fact, which was most of the profile.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        continues = (out and stripped and line[:1].isspace()
                     and not stripped.startswith(("- ", "#", "<!--")))
        if continues:
            out[-1] += " " + stripped
        else:
            out.append(stripped)
    return out


def _known(text):
    """The profile as the model should see it: body only, tail clipped.

    The card and the hand-written prose sit at the top and are what captured
    lines most often restate, so a clip keeps the head.
    """
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()[:MAX_KNOWN_CHARS] or "(boş)"


def _ask_model(base_url, messages, known="(boş)"):
    model = _get_model(base_url, "PERSONAL_CAPTURE_MODEL")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user",
                      "content": PROMPT.format(messages=messages, known=known)}],
        "temperature": 0.2,
        "max_tokens": 1200,
        # Qwen3 reasons before answering and the reasoning is billed against
        # max_tokens: on a real prompt it spent the whole budget thinking and
        # returned content=None, which read exactly like "no facts found".
        # This is extraction, not a problem that needs deliberation.
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
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    facts = json.loads(m.group(0))
    # Hard cap on top of the prompt's: a model that ignores "at most 3" would
    # otherwise pad the profile with five rewordings of one fact per session,
    # and a profile nobody can read is the same as no profile.
    return [f.strip() for f in facts if isinstance(f, str) and f.strip()][:MAX_FACTS]


def _append(note_path, facts):
    # "Where may I write" and "what does the note already say" are two
    # different questions, and one slice used to answer both. Writes append at
    # the end of the note, always below the injected card -- the card is
    # curated by hand and this hook must not grow what rides in every future
    # session. The dedup window used to start after the card as well, so it
    # could not see the card or the note's own prose above it. The design
    # invites a human to promote a captured line up into the card, and a window
    # starting below it made exactly the facts the user endorsed come back
    # every single session.
    #
    # The read (what does the note say) and the write (append what is new)
    # happen inside one locked_note section: concept-capture and a second
    # instance of this same hook (Claude and Qwen can both fire Stop on
    # overlapping sessions) append to the same profile note, and locking only
    # the final write would still let two readers both decide the same fact
    # is new before either has written it.
    with locked_note(note_path) as f:
        text = f.read()
        existing = _logical_lines(text)
        fresh = [fact for fact in facts
                 if _is_new(fact, existing, threshold=DEDUP_THRESHOLD)]
        if not fresh:
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        lines = [f"- {fact} <!-- auto:{today} -->" for fact in fresh]
        if AUTO_SECTION in text:
            f.write("\n".join(lines) + "\n")
        else:
            header = (
                f"\n\n{AUTO_SECTION}\n"
                "Oturum sonunda otomatik çıkarılan gerçekler (lokal model). Elle\n"
                "düzenlenebilir; yanlış bir satırı silmek yeterli. Buradaki hiçbir\n"
                "satır kendiliğinden yukarıdaki ajan kartına girmez.\n\n"
            )
            f.write(header + "\n".join(lines) + "\n")
        return len(fresh)


def _progress_path(session_id):
    """Where this session records how much of the user's typing it has read."""
    session = re.sub(r"[^A-Za-z0-9_.-]", "-", str(session_id or "")[:60])
    if not session:
        return None
    return os.path.join(tempfile.gettempdir(), f"personal-capture-{session}.chars")


def _progress(path):
    """Characters the user had typed at this session's last dispatch."""
    if not path:
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def _record_progress(path, typed):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(typed))
    except OSError:
        pass


def _worker(transcript, note_path, base_url):
    """Run the capture, and say why on every path that writes nothing.

    This runs detached, so its only channel is the metric line. Without a
    reason on the empty paths, "the model found nothing new" and "the hook
    never ran" leave the same trace -- and that ambiguity is what let the
    rest of this bridge look alive while doing nothing for weeks.
    """
    where = os.path.dirname(note_path)

    def quiet(reason):
        record_metric("personal-capture", "skip", where, reason)

    messages = _user_messages(transcript)
    if not messages:
        return quiet("no-user-text")
    joined = "\n\n".join(f"- {m}" for m in messages)
    if len(joined) < MIN_CHARS:
        return quiet("too-short")
    if len(joined) > MAX_PROMPT_CHARS:
        joined = joined[-MAX_PROMPT_CHARS:]
    try:
        with open(note_path, "r", encoding="utf-8", errors="ignore") as f:
            note_text = f.read()
    except OSError:
        return quiet("read-error")
    if _pending(note_text) >= MAX_PENDING:
        return quiet("backlog-full")
    try:
        facts = _ask_model(base_url, joined, _known(note_text))
    except Exception as e:
        # Bare "model-error" looked identical whether the vLLM box was down
        # for two days or a single request glitched -- the model streak from
        # 2026-09-03 was invisible until someone read the source. The type
        # name plus a clipped message is enough to tell "connection refused"
        # from "bad JSON" without leaking prompt content into the metric log.
        return quiet(f"model-error:{type(e).__name__}:{str(e)[:150]}")
    if not facts:
        return quiet("no-facts")
    try:
        written = _append(note_path, facts)
    except OSError:
        return quiet("write-error")
    if written:
        record_metric("personal-capture", "capture", where, f"{written}fact")
    else:
        quiet("all-known")  # every fact the model returned, the note already made


def main():
    if len(sys.argv) > 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3], sys.argv[4])
        return

    vault_dir = resolve_vault()
    base_url = env_or_conf("QWEN_BASE_URL")
    if not vault_dir or not base_url or not os.path.isdir(vault_dir):
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript = data.get("transcript_path") or data.get("transcriptPath") or ""
    if not transcript or not os.path.isfile(transcript):
        # Configured but handed nothing to read: a host that does not pass a
        # transcript on Stop switches this hook off without ever saying so.
        record_metric("personal-capture", "skip", os.getcwd(), "no-transcript")
        sys.exit(0)

    # Stop fires at the end of every turn, not once per session. The old guard
    # wrote a flag on the first Stop and returned early ever after, so the only
    # transcript this hook ever read was the opening turn -- one prompt, the
    # turn least likely to carry a durable fact. That is why the Claude side
    # looked dead for a week: it was not silent, it had already spoken once.
    # Every Stop is a candidate now, and the marker holds progress instead of a
    # flag: dispatch again once the user has typed another MIN_CHARS worth,
    # which is the same floor the worker needs before it can say anything.
    session_id = data.get("session_id") or data.get("conversationId") or ""
    progress = _progress_path(session_id)
    seen = _progress(progress)
    typed = sum(len(m) for m in _user_messages(transcript))
    if typed - seen < MIN_CHARS:
        reason = "no-user-text" if not typed else (
            "too-short" if not seen else "no-new-input")
        record_metric("personal-capture", "skip", os.getcwd(), reason)
        sys.exit(0)

    note_path, _ = find_note(vault_dir, "agent_profile: true")
    if not note_path:
        record_metric("personal-capture", "skip", vault_dir, "no-profile-note")
        sys.exit(0)

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker",
             transcript, note_path, base_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        record_metric("personal-capture", "skip", vault_dir, "spawn-failed")
        sys.exit(0)
    _record_progress(progress, typed)
    # The worker is detached and reports for itself; this pairs with whatever
    # it records, so a dispatch with no follow-up means the worker died.
    record_metric("personal-capture", "dispatch", os.path.dirname(note_path))
    sys.exit(0)


if __name__ == "__main__":
    main()
