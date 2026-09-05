#!/usr/bin/env python3
"""
project-narrative hook (Stop).

personal-capture answers "who is the user." This answers a different
question that nothing else in the bridge captures: "what did we actually
work on today, why, and what changed along the way." mem-lite's own
`narrative`/`content` field already holds long-form prose per mem_save
call, and obsidian-mirror already mirrors it into the vault's per-project
day file untouched -- the missing piece was a trigger that writes a
session-level version of that prose automatically, without relying on the
agent remembering to call mem_save with a rich enough narrative before the
user closes the laptop.

Lesson learned building concept-capture (mem-lite #416): a local 27B doing
SYNTHESIS on a coding transcript is the most expensive place for a 27B to
be wrong -- it should COMPILE what was already said, not interpret. So the
prompt here is explicitly an extraction task: pull together what the
assistant already stated it did, what the user already stated they wanted
and why, and how the user's own later messages changed the ask -- in
flowing paragraphs, but with nothing added that was not already said in
the transcript.

Unlike personal-capture, this reads BOTH sides of the conversation (the
assistant's own account of what it did is exactly the material this needs)
and writes into mem-lite via the CLI `save` command, not into a vault note
directly -- obsidian-mirror's existing --reconcile Stop hook picks up the
new mem-lite row and mirrors it into the vault day file with zero changes
on the mirror side, and it becomes searchable via mem_recent/mem_search
like anything else in mem-lite.

Off unless QWEN_BASE_URL is configured. Fail-open everywhere else.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from vault_common import env_or_conf, mem_lite_key, record_metric
except Exception:
    sys.exit(0)

MIN_CHARS = 3000          # a quick Q&A session has nothing worth a narrative
REFIRE_CHARS = 6000       # this much NEW content since the last fire re-fires
MAX_PROMPT_CHARS = 24000  # curl-tested: 17k tokens in ~2s against QWEN_BASE_URL
TIMEOUT = 120
NONE_SENTINEL = "NONE"

CLI = os.path.join(os.path.expanduser("~"), ".claude-mem-lite", "cli.mjs")

# Same shape problem personal-capture solved: Claude writes message.content
# (string or type:text blocks), Qwen writes message.parts (no type tag) plus
# its own injected hook context as a further part of the same message.
MACHINE_MARKERS = (
    "<local-command", "<command-name>", "<system-reminder>", "Caveat:",
    "[Request interrupted", "[Usage limit", "<session-handoff",
    "<qwen:user-prompt-submit-context", "<qwen:session-start-context",
)

SECRET_MARKERS = re.compile(
    r"(password|passwd|şifre|parola|secret|api[_-]?key|token\s*[=:]|bearer\s|"
    r"BEGIN [A-Z ]*PRIVATE KEY|ssh-rsa\s)", re.IGNORECASE
)

PROMPT = """Aşağıda bir yazılım oturumunun kullanıcı ve asistan mesajları var (kronolojik sıra).

Görevin SENTEZ değil ÇIKARIM: oturumda zaten söylenmiş olanları derleyip
düzenli paragraflar halinde sun. Metinde açıkça yer almayan hiçbir yorum,
tahmin veya kendi görüşünü EKLEME. Emin olmadığın bir noktayı atla, uydurma.

Şunları -- SADECE metinde gerçekten varsa -- çıkar ve düzenli paragraflar
halinde yaz:
1. Hangi proje/konu üzerinde çalışıldığı ve genel odak.
2. Asistanın fiilen ne yaptığı (asistanın kendi ifadelerinden: değişiklik,
   karar, bulgu).
3. Kullanıcının bunu neden istediği (kullanıcının kendi ifadelerinden).
4. Oturum içinde kapsamın nasıl değiştiği -- erken mesajlarla sonraki
   mesajları karşılaştır; değişim gerçekten görülüyorsa belirt, yoksa bu
   maddeyi atla.
5. Açıkça dile getirilmiş sıradaki adımlar / açık sorular / istekler.

3-5 paragraf, akıcı Türkçe düzyazı. Bu bilgilerin hiçbiri metinde yoksa
(çok kısa veya konu dışı bir oturum), SADECE şunu döndür: {none}

OTURUM:
---
{{messages}}
---
ÖZET:""".format(none=NONE_SENTINEL)


def _record_text(rec):
    message = rec.get("message") or {}
    content = message.get("content") or rec.get("content")
    if isinstance(content, str):
        m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
        return m.group(1).strip() if m else content
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


def _session_messages(path):
    """Both sides of the conversation, oldest first, machine records removed.

    personal-capture deliberately excludes the assistant's own words -- this
    hook needs exactly the opposite, since "what did we do" lives in what the
    assistant said it did, not in what the user typed.
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                role = rec.get("type")
                if role not in ("user", "assistant", "USER_INPUT"):
                    continue
                content = _record_text(rec)
                if not isinstance(content, str):
                    continue
                text = content.strip()
                if not text or any(m in text[:400] for m in MACHINE_MARKERS):
                    continue
                if SECRET_MARKERS.search(text):
                    continue
                speaker = "Kullanıcı" if role in ("user", "USER_INPUT") else "Asistan"
                out.append(f"{speaker}: {text}")
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


def _ask_model(base_url, messages):
    model = _get_model(base_url, "PROJECT_NARRATIVE_MODEL")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(messages=messages)}],
        "temperature": 0.2,
        "max_tokens": 1000,
        # Same fix personal-capture needed: reasoning eats max_tokens and
        # returns content=None on a real prompt if left on.
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
        return None
    text = text.strip()
    if not text or text.upper() == NONE_SENTINEL:
        return None
    return text


def _progress_path(session_id):
    session = re.sub(r"[^A-Za-z0-9_.-]", "-", str(session_id or "")[:60])
    if not session:
        return None
    return os.path.join(tempfile.gettempdir(), f"project-narrative-{session}.chars")


def _progress(path):
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


def _save(project, narrative):
    """Write through the mem-lite CLI, not the MCP tool -- this runs detached,
    long after the turn (and its tool calls) has ended."""
    lesson = narrative.strip().splitlines()[0][:500]
    cmd = [
        "node", CLI, "save",
        "--type", "change",
        "--title", "Oturum Özeti",
        "--importance", "1",
        "--project", project,
        "--lesson", lesson,
        narrative,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _worker(transcript, project, base_url):
    def quiet(reason):
        record_metric("project-narrative", "skip", project, reason)

    messages = _session_messages(transcript)
    if not messages:
        return quiet("no-text")
    joined = "\n\n".join(messages)
    if len(joined) < MIN_CHARS:
        return quiet("too-short")
    if len(joined) > MAX_PROMPT_CHARS:
        joined = joined[-MAX_PROMPT_CHARS:]
    try:
        narrative = _ask_model(base_url, joined)
    except Exception as e:
        return quiet(f"model-error:{type(e).__name__}:{str(e)[:150]}")
    if not narrative:
        return quiet("no-narrative")
    try:
        _save(project, narrative)
    except Exception as e:
        return quiet(f"save-error:{type(e).__name__}:{str(e)[:150]}")
    record_metric("project-narrative", "capture", project, f"{len(narrative)}chars")


def main():
    if len(sys.argv) > 3 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3], sys.argv[4])
        return

    base_url = env_or_conf("QWEN_BASE_URL")
    if not base_url or not os.path.isfile(CLI):
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript = data.get("transcript_path") or data.get("transcriptPath") or ""
    if not transcript or not os.path.isfile(transcript):
        record_metric("project-narrative", "skip", os.getcwd(), "no-transcript")
        sys.exit(0)

    project = mem_lite_key(os.getcwd())

    # Same progress-marker pattern as personal-capture: every Stop is a
    # candidate, and re-fires (a fresh save, not an edit -- mem-lite's CLI
    # has no update-in-place for `save`) once REFIRE_CHARS more has
    # accumulated, so a long session leaves its LATEST snapshot as the last
    # "Oturum Özeti" entry in today's day file. vault-inject reads only the
    # last one; the earlier snapshots stay in mem-lite as an honest history
    # of how the session's scope moved, not noise to clean up.
    session_id = data.get("session_id") or data.get("conversationId") or ""
    progress = _progress_path(session_id)
    seen = _progress(progress)
    typed = sum(len(m) for m in _session_messages(transcript))
    threshold = MIN_CHARS if not seen else seen + REFIRE_CHARS
    if typed < threshold:
        record_metric("project-narrative", "skip", os.getcwd(),
                       "too-short" if not seen else "no-new-input")
        sys.exit(0)

    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--worker",
             transcript, project, base_url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        record_metric("project-narrative", "skip", project, "spawn-failed")
        sys.exit(0)
    _record_progress(progress, typed)
    record_metric("project-narrative", "dispatch", project)
    sys.exit(0)


if __name__ == "__main__":
    main()
