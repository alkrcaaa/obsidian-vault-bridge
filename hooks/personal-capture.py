#!/usr/bin/env python3
"""
personal-capture hook (Stop).

The repo half of this bridge learns on its own: an agent calls mem_save,
obsidian-mirror files it, compile-nudge asks for a synthesis pass. The half
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
        CARD_END, env_or_conf, find_note, read_text, record_metric,
        vault_dir as resolve_vault,
    )
except Exception:
    sys.exit(0)

AUTO_SECTION = "## Otomatik Yakalananlar"
MIN_CHARS = 200          # below this a session has not said anything about anyone
MAX_PROMPT_CHARS = 12000  # newest messages only; a 27B degrades on a long tail
TIMEOUT = 90
MAX_FACTS = 3

# Machine-generated user records: slash commands, hook injections, interrupt
# notices. They are not the user talking, and they dominate by volume.
MACHINE_MARKERS = (
    "<local-command", "<command-name>", "<system-reminder>", "Caveat:",
    "[Request interrupted", "[Usage limit", "<session-handoff",
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

MESAJLAR:
---
{messages}
---
JSON:"""


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
                if rec.get("type") != "user":
                    continue
                content = (rec.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
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


def _ask_model(base_url, messages):
    body = json.dumps({
        "model": os.environ.get("PERSONAL_CAPTURE_MODEL", "/models/qwen3.8-27b"),
        "messages": [{"role": "user", "content": PROMPT.format(messages=messages)}],
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


def _words(text):
    """Word stems, crudely: the first four characters of each word.

    Whole-word comparison misses the case it exists for. Turkish inflects by
    suffix, so the same fact restated reads as "repo" one day and
    "repolardan" the next, and an exact-word overlap scores those as
    unrelated -- the duplicate then gets written every session.
    """
    return {w[:4] for w in re.findall(r"\w+", text.lower()) if len(w) > 2}


def _is_new(fact, existing_lines):
    """Skip anything the note already says, however it is worded.

    Exact-match dedup would let the same fact accumulate in five phrasings,
    which is how a profile turns into noise nobody reads.
    """
    fw = _words(fact)
    # Guard on the raw sentence, not on stems: a short but real fact ("Ankara'da
    # yaşıyor") has few stems, and rejecting it here would look like dedup.
    if len(re.findall(r"\w+", fact)) < 3 or not fw:
        return False
    for line in existing_lines:
        lw = _words(line)
        if not lw:
            continue
        if len(fw & lw) / len(fw) > 0.6:
            return False
    return True


def _append(note_path, facts):
    text = read_text(note_path, limit=400000)
    # Everything after the injected card: the card is curated by hand and this
    # hook must not grow what rides in every future session.
    idx = text.find(CARD_END)
    body = text[idx:] if idx != -1 else text
    fresh = [f for f in facts if _is_new(f, body.splitlines())]
    if not fresh:
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"- {f} <!-- auto:{today} -->" for f in fresh]
    if AUTO_SECTION in text:
        addition = "\n".join(lines) + "\n"
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(addition)
    else:
        header = (
            f"\n\n{AUTO_SECTION}\n"
            "Oturum sonunda otomatik çıkarılan gerçekler (lokal model). Elle\n"
            "düzenlenebilir; yanlış bir satırı silmek yeterli. Buradaki hiçbir\n"
            "satır kendiliğinden yukarıdaki ajan kartına girmez.\n\n"
        )
        with open(note_path, "a", encoding="utf-8") as f:
            f.write(header + "\n".join(lines) + "\n")
    return len(fresh)


def _worker(transcript, note_path, base_url):
    messages = _user_messages(transcript)
    if not messages:
        return
    joined = "\n\n".join(f"- {m}" for m in messages)
    if len(joined) < MIN_CHARS:
        return
    if len(joined) > MAX_PROMPT_CHARS:
        joined = joined[-MAX_PROMPT_CHARS:]
    try:
        facts = _ask_model(base_url, joined)
    except Exception:
        return
    if not facts:
        return
    try:
        written = _append(note_path, facts)
    except OSError:
        return
    if written:
        record_metric("personal-capture", "capture", os.path.dirname(note_path),
                      f"{written}fact")


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

    transcript = data.get("transcript_path") or ""
    if not transcript or not os.path.isfile(transcript):
        sys.exit(0)

    # Stop can fire more than once for a session; capture it once.
    session = re.sub(r"[^A-Za-z0-9_.-]", "-", str(data.get("session_id") or "")[:60])
    if session:
        marker = os.path.join(tempfile.gettempdir(), f"personal-capture-{session}.done")
        if os.path.exists(marker):
            sys.exit(0)
        try:
            open(marker, "w").close()
        except OSError:
            pass

    note_path, _ = find_note(vault_dir, "agent_profile: true")
    if not note_path:
        sys.exit(0)

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker",
             transcript, note_path, base_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
