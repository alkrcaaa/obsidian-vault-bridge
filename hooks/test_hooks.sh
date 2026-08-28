#!/usr/bin/env bash
# Tests for the vault hooks. Every failure mode here is silent by design --
# a hook that finds nothing exits 0 and says nothing -- so the only way to
# know injection still works is to assert on it.
set -uo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0

pass() { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; echo "        $2"; FAIL=$((FAIL + 1)); }

check() { # name, expected-substring, actual
  if [[ "$3" == *"$2"* ]]; then pass "$1"; else fail "$1" "expected '$2' in: ${3:0:200}"; fi
}
check_absent() { # name, forbidden-substring, actual
  if [[ "$3" != *"$2"* ]]; then pass "$1"; else fail "$1" "did not expect '$2' in: ${3:0:200}"; fi
}

VAULT="$(mktemp -d)"
REPO="$(mktemp -d)"

# Redirect HOME for the whole run, not just the cases that assert on it.
# record_metric() resolves the host kit through $HOME, so every hook a test
# fired was appending to the real ~/.claude/hook-metrics.jsonl: 68 of the 83
# vault-inject "sessions" on this machine were test runs. That is telemetry
# lying about production, and it lied to the one question the metrics exist
# to answer -- what does this hook actually cost in real sessions.
HOME="$(mktemp -d)"
export HOME
trap 'rm -rf "$VAULT" "$REPO" "$HOME"' EXIT

git -C "$REPO" init -q 2>/dev/null
PROJECT="$(basename "$(dirname "$REPO")")--$(basename "$REPO")"
PROJECT="${PROJECT//[^a-zA-Z0-9_.-]/-}"

mkdir -p "$VAULT/Code"
cat >"$VAULT/Code/repo.md" <<EOF
---
mem_lite_project: $PROJECT
last_compiled: 2026-01-01
---

# repo

<!-- agent-card:start -->
CARD-BODY-MARKER
<!-- agent-card:end -->

## Detail
BODY-ONLY-MARKER
EOF

# Decode the hook's JSON to the text it actually injects: asserting on the raw
# JSON silently tests the encoder instead (a non-ASCII ellipsis arrives as
# … and never matches), which is a test that passes for the wrong reason.
decode() {
  python3 -c '
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    sys.exit(0)
try:
    print(json.loads(raw)["hookSpecificOutput"]["additionalContext"])
except Exception:
    print(raw)
'
}

run_inject() {
  echo "{\"cwd\":\"$REPO\"}" | VAULT_DIR="$1" python3 "$HOOKS_DIR/vault-inject.py" 2>&1 | decode
}

echo "vault-inject"

out="$(run_inject "$VAULT")"
check "injects the repo note's agent-card" "CARD-BODY-MARKER" "$out"
check_absent "does not inject the rest of the note" "BODY-ONLY-MARKER" "$out"
check "names the note so the agent can read the rest" "Code/repo.md" "$out"

out="$(echo '{"cwd":"/nonexistent"}' | VAULT_DIR="$VAULT" python3 "$HOOKS_DIR/vault-inject.py" 2>&1 | decode)"
check_absent "a repo with no note injects nothing" "CARD-BODY-MARKER" "$out"

# An empty frontmatter field used to return the `---` that closes the block,
# so an uncompiled note was announced as "compiled ---", and the staleness
# check that parses the same field as a date failed and exited on every note
# that had never been compiled.
fm() { python3 -c '
import sys; sys.path.insert(0, sys.argv[1])
from vault_common import frontmatter_field
print(repr(frontmatter_field(sys.stdin.read(), "last_compiled")))
' "$HOOKS_DIR"; }
out="$(printf -- '---\nlast_compiled:\n---\n' | fm)"
check "an empty frontmatter field reads as unset" "None" "$out"
out="$(printf -- '---\nlast_compiled: "2026-08-17"\n---\n' | fm)"
check "a quoted frontmatter value is unquoted" "'2026-08-17'" "$out"
out="$(printf -- '---\nlast_compiled: 2026-08-17\n---\n' | fm)"
check "a plain frontmatter value still reads" "'2026-08-17'" "$out"

# A key that is a prefix of a longer one used to win on a substring test, so
# the note for `<repo>_streaming_operations` was injected for `<repo>`.
cat >"$VAULT/Code/repo-longer.md" <<EOF
---
mem_lite_project: ${PROJECT}_extra
---

# repo-longer

<!-- agent-card:start -->
WRONG-NOTE-MARKER
<!-- agent-card:end -->
EOF
out="$(run_inject "$VAULT")"
check_absent "a longer project key is not matched by prefix" "WRONG-NOTE-MARKER" "$out"
rm -f "$VAULT/Code/repo-longer.md"

# Several notes legitimately share a repo's key -- one per component. The note
# named after the repo is the one to inject; walk order decided it before.
mkdir -p "$VAULT/Code/parts"
cat >"$VAULT/Code/parts/aaa-component.md" <<EOF
---
mem_lite_project: $PROJECT
---

# aaa-component

<!-- agent-card:start -->
COMPONENT-MARKER
<!-- agent-card:end -->
EOF
mv "$VAULT/Code/repo.md" "$VAULT/Code/$(basename "$REPO").md"
out="$(run_inject "$VAULT")"
check "the note named after the repo wins over a component note" "CARD-BODY-MARKER" "$out"
check_absent "the component note is not injected instead" "COMPONENT-MARKER" "$out"
mv "$VAULT/Code/$(basename "$REPO").md" "$VAULT/Code/repo.md"
rm -rf "$VAULT/Code/parts"

# An uncompiled note is a heading with an empty bullet under it. Injecting
# that spends the ambient budget on nothing, so it must count as no slice.
cat >"$VAULT/Code/stub.md" <<EOF
---
mem_lite_project: ${PROJECT}-stub
---

# stub

## Mimari Ozet
-

## Son Kararlar
-
EOF
STUBREPO="$(dirname "$REPO")/$(basename "$REPO")-stub"
mkdir -p "$STUBREPO" && git -C "$STUBREPO" init -q 2>/dev/null
out="$(echo "{\"cwd\":\"$STUBREPO\"}" | VAULT_DIR="$VAULT" python3 "$HOOKS_DIR/vault-inject.py" 2>&1)"
if [[ -z "$out" ]]; then pass "an uncompiled stub note injects nothing"
else fail "an uncompiled stub note injects nothing" "expected no output, got: ${out:0:120}"; fi
rm -rf "$VAULT/Code/stub.md" "$STUBREPO"

# Unconfigured means no env AND no installer config file -- HOME is redirected
# so the real machine's ~/.config/dev-agent-kit/vault.env cannot answer for it.
out="$(echo "{\"cwd\":\"$REPO\"}" | VAULT_DIR="" HOME="$(mktemp -d)" python3 "$HOOKS_DIR/vault-inject.py" 2>&1)"
if [[ -z "$out" ]]; then pass "an unconfigured vault is a silent no-op"
else fail "an unconfigured vault is a silent no-op" "expected no output, got: ${out:0:120}"; fi

# The config file is the fallback that keeps the hook alive when the agent was
# launched from a shell that never exported VAULT_DIR -- the failure that kept
# these hooks dead in production while every manual run looked fine.
CONFHOME="$(mktemp -d)"
mkdir -p "$CONFHOME/.config/dev-agent-kit"
echo "VAULT_DIR=$VAULT" >"$CONFHOME/.config/dev-agent-kit/vault.env"
out="$(echo "{\"cwd\":\"$REPO\"}" | VAULT_DIR="" HOME="$CONFHOME" python3 "$HOOKS_DIR/vault-inject.py" 2>&1 | decode)"
check "the config file stands in for a missing VAULT_DIR" "CARD-BODY-MARKER" "$out"

# Profile: opting in is a positive act. A note that carries the flag but marks
# no card must inject nothing -- falling back to "first section" would leak
# whatever the personal note happens to open with.
cat >"$VAULT/profile.md" <<'EOF'
---
agent_profile: true
---

# Profile

## Private
PRIVATE-MARKER
EOF
out="$(run_inject "$VAULT")"
check_absent "profile without a card injects nothing" "PRIVATE-MARKER" "$out"

cat >"$VAULT/profile.md" <<'EOF'
---
agent_profile: true
---

<!-- agent-card:start -->
PROFILE-CARD-MARKER
<!-- agent-card:end -->

## Private
PRIVATE-MARKER
EOF
out="$(run_inject "$VAULT")"
check "profile with a card injects the card" "PROFILE-CARD-MARKER" "$out"
check_absent "profile card does not drag the rest along" "PRIVATE-MARKER" "$out"

# The cap must not cut mid-sentence: a truncated claim about the user still
# reads as a complete one.
python3 - "$VAULT/Code/repo.md" <<'PYEOF'
import sys
p = sys.argv[1]
body = "\n".join(f"- line {i} padded out to make this note exceed the cap" for i in range(60))
text = open(p).read().replace("CARD-BODY-MARKER", body)
open(p, "w").write(text)
PYEOF
out="$(run_inject "$VAULT")"
check "an oversized card is trimmed" "[…]" "$out"
# Every surviving padded line must be whole -- a cut mid-line is the bug.
truncated="$(printf '%s\n' "$out" | grep -c 'line [0-9]* padded out to make this note exceed the cap$' || true)"
kept="$(printf '%s\n' "$out" | grep -c 'padded out' || true)"
if [[ "$truncated" -eq "$kept" && "$kept" -gt 0 ]]; then
  pass "trimming happens at a line boundary"
else
  fail "trimming happens at a line boundary" "$kept lines kept, only $truncated intact"
fi

echo
echo "personal-capture"

# The model call is the one part that needs a server, so the gates around it
# are what these tests pin: what leaves the machine, and what never does.
pc() { python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('pc', '$HOOKS_DIR/personal-capture.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
$1
"; }

TRANSCRIPT="$VAULT/transcript.jsonl"
cat >"$TRANSCRIPT" <<'EOF'
{"type":"user","message":{"role":"user","content":"ben hiç makale okumam"}}
{"type":"user","message":{"role":"user","content":"<system-reminder>machine noise</system-reminder>"}}
{"type":"user","message":{"role":"user","content":"<local-command-caveat>noise</local-command-caveat>"}}
{"type":"user","message":{"role":"user","content":"benim şifrem hunter2"}}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"tool output"}]}}
{"type":"assistant","message":{"role":"assistant","content":"my own words"}}
{"type":"user","message":{"role":"user","content":"ben hiç makale okumam"}}
EOF

out="$(pc "print(m._user_messages('$TRANSCRIPT'))")"
check "keeps what the user typed" "makale okumam" "$out"
check_absent "drops hook/system records" "machine noise" "$out"
check_absent "drops slash-command records" "local-command" "$out"
check_absent "drops tool results" "tool output" "$out"
check_absent "never sends the assistant's own words" "my own words" "$out"
check_absent "credentials never leave the machine" "hunter2" "$out"
if [[ "$(pc "print(len(m._user_messages('$TRANSCRIPT')))")" == "1" ]]; then
  pass "repeated prompts are sent once"
else fail "repeated prompts are sent once" "$(pc "print(m._user_messages('$TRANSCRIPT'))")"; fi

# Qwen writes message.parts, not message.content, and files its own injected
# hook context as a further part of the same record. Reading only Claude's
# shape made a live Qwen session dispatch the worker and find nothing at all.
QTRANSCRIPT="$VAULT/qwen-transcript.jsonl"
cat >"$QTRANSCRIPT" <<'EOF'
{"type":"user","provenance":"real_user","message":{"role":"user","parts":[{"text":"ben hiç makale okumam"},{"text":"<qwen:user-prompt-submit-context>\ninjected hook noise\n</qwen:user-prompt-submit-context>"}]}}
{"type":"assistant","message":{"role":"assistant","parts":[{"text":"my own words"}]}}
EOF
out="$(pc "print(m._user_messages('$QTRANSCRIPT'))")"
check "reads Qwen's parts shape too" "makale okumam" "$out"
check_absent "drops Qwen's injected hook context" "injected hook noise" "$out"
check_absent "never sends the assistant's own words (Qwen shape)" "my own words" "$out"

out="$(pc "print(m._is_new('Makale okumuyor, repo okuyor.', ['- Makale okumuyor, bilgiyi repolardan alıyor.']))")"
check "a fact the note already makes is not re-added" "False" "$out"
out="$(pc "print(m._is_new('Ankara da yasiyor.', ['- Makale okumuyor.']))")"
check "a genuinely new fact is added" "True" "$out"

# The card is what vault-inject puts into every future session; an
# unsupervised writer must never grow it.
cp "$VAULT/profile.md" "$VAULT/profile-before.md"
pc "m._append('$VAULT/profile.md', ['Ankara da yasiyor.'])" >/dev/null
out="$(cat "$VAULT/profile.md")"
check "captured facts land in their own dated section" "auto:$(date +%Y-%m-%d)" "$out"
card_lines_before="$(sed -n '/agent-card:start/,/agent-card:end/p' "$VAULT/profile-before.md" | wc -l)"
card_lines_after="$(sed -n '/agent-card:start/,/agent-card:end/p' "$VAULT/profile.md" | wc -l)"
if [[ "$card_lines_before" == "$card_lines_after" ]]; then
  pass "the injected card is left untouched"
else fail "the injected card is left untouched" "$card_lines_before -> $card_lines_after"; fi

# HOME is redirected as well: without it the config-file fallback would supply
# the machine's real endpoint and this test would quietly ship a transcript to
# it instead of asserting the gate.
out="$(echo '{"transcript_path":"'"$TRANSCRIPT"'","session_id":"t1"}' | VAULT_DIR="$VAULT" QWEN_BASE_URL="" HOME="$(mktemp -d)" python3 "$HOOKS_DIR/personal-capture.py" 2>&1)"
if [[ -z "$out" ]]; then pass "no model endpoint is a silent no-op"
else fail "no model endpoint is a silent no-op" "$out"; fi

# --- obsidian-mirror: both hosts spell the response parts differently -------
# Claude sends tool_response.content, Qwen sends tool_response.llmContent.
# Only the first was read, and the miss was not an error: the payload fell
# through to json.dumps(), whose escaped quotes made the "Saved as
# observation" regex match nothing, so every Qwen save was logged `unparsed`
# and mirrored nowhere.
MIRROR="$(mktemp -d)"
trap 'rm -rf "$VAULT" "$REPO" "$MIRROR"' EXIT
# Embedded in JSON below, so the quotes around the project name are escaped
# here rather than at each use site.
SAVED='Saved as observation #999 [bugfix] in project \"'"$PROJECT"'\"'

mirror() { # response-json
  echo '{"tool_name":"mcp__mem-lite__mem_save","tool_input":{"title":"t","content":"c"},"tool_response":'"$1"'}' \
    | MEM_OBSIDIAN_VAULT="$MIRROR" HOME="$(mktemp -d)" python3 "$HOOKS_DIR/obsidian-mirror.py" 2>&1
}
day_file="$MIRROR/$PROJECT/$(date +%Y-%m-%d).md"

mirror '{"content":[{"text":"'"$SAVED"'"}]}' >/dev/null
check "Claude's response shape is mirrored" "#999" "$(cat "$day_file" 2>/dev/null)"

rm -rf "${MIRROR:?}"/*
mirror '{"llmContent":[{"text":"'"$SAVED"'"}]}' >/dev/null
check "Qwen's response shape is mirrored" "#999" "$(cat "$day_file" 2>/dev/null)"

rm -rf "${MIRROR:?}"/*
mirror '{"llmContent":"'"$SAVED"'"}' >/dev/null
check "a bare-string parts field is mirrored" "#999" "$(cat "$day_file" 2>/dev/null)"

rm -rf "${MIRROR:?}"/*
mirror '{"llmContent":[{"text":"Skipped as a duplicate"}]}' >/dev/null
if [[ ! -e "$day_file" ]]; then pass "an unparsable response writes nothing"
else fail "an unparsable response writes nothing" "$(cat "$day_file")"; fi

# --- obsidian-mirror --reconcile: the capture path stopped mattering --------
# The PostToolUse hook above only ever sees saves made through the mem_save
# *tool*. mem-lite is equally reachable as `cli.mjs save`, which the agent
# picks whenever the MCP tools sit behind a tool search -- that path fires no
# PostToolUse, so three real saves landed in SQLite and the vault never heard
# about them. The recovery diffs the two stores rather than learning a second
# trigger, so a fourth capture path cannot break it the same way.
RHOME="$(mktemp -d)"
trap 'rm -rf "$VAULT" "$REPO" "$MIRROR" "$RHOME"' EXIT
mkdir -p "$RHOME/.claude-mem-lite"
python3 - "$RHOME/.claude-mem-lite/claude-mem-lite.db" "$PROJECT" <<'PY'
import sqlite3, sys
from datetime import datetime, timedelta, timezone
db, project = sys.argv[1], sys.argv[2]
con = sqlite3.connect(db)
con.execute(
    "CREATE TABLE observations (id INTEGER, project TEXT, created_at TEXT, "
    "type TEXT, title TEXT, narrative TEXT, text TEXT, lesson_learned TEXT, "
    "files_modified TEXT, superseded_at TEXT, compressed_into TEXT)")
now = datetime.now(timezone.utc)
rows = [
    (777, project, now.isoformat(), "bugfix", "cli save", "n", "t", "l", '["a.py"]'),
    (778, project, (now - timedelta(days=30)).isoformat(), "bugfix", "ancient",
     "n", "t", "l", "[]"),
]
con.executemany("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)", rows)
con.commit()
PY

reconcile() { # cwd
  echo '{"cwd":"'"$1"'"}' \
    | MEM_OBSIDIAN_VAULT="$MIRROR" HOME="$RHOME" \
      python3 "$HOOKS_DIR/obsidian-mirror.py" --reconcile 2>&1
}

rm -rf "${MIRROR:?}"/*
reconcile "$REPO" >/dev/null
check "a save the tool hook never saw is recovered" "#777" "$(cat "$day_file" 2>/dev/null)"

# Re-running must be a no-op. The reconcile finds an entry by the `(#id)` the
# writer stamps, so any drift between the two paths' formatting would make
# every already-mirrored row look missing and duplicate the whole log on every
# session end.
reconcile "$REPO" >/dev/null
count="$(grep -c '(#777)' "$day_file" 2>/dev/null || echo 0)"
if [[ "$count" == "1" ]]; then pass "reconcile is idempotent"
else fail "reconcile is idempotent" "#777 appears $count times"; fi

# Old rows stay in mem-lite rather than being dumped into a vault whose
# contract is what is true now.
if ! grep -rq "ancient" "$MIRROR"; then pass "reconcile leaves history behind"
else fail "reconcile leaves history behind" "$(grep -r ancient "$MIRROR")"; fi

# A repo-less session is filed under the catch-all in the vault while mem-lite
# went on keying it by directory, so the folder and the rows have different
# names. Skipping that case left one whole class of save with no recovery path
# -- the consistency check found a stranded row the same day it shipped.
rm -rf "${MIRROR:?}"/*
CATCHDIR="$(mktemp -d)"; trap 'rm -rf "$VAULT" "$REPO" "$MIRROR" "$RHOME" "$CATCHDIR"' EXIT
CATCHKEY="$(basename "$(dirname "$CATCHDIR")")--$(basename "$CATCHDIR")"
CATCHKEY="${CATCHKEY//[^a-zA-Z0-9_.-]/-}"
python3 - "$RHOME/.claude-mem-lite/claude-mem-lite.db" "$CATCHKEY" <<'PY'
import sqlite3, sys
from datetime import datetime, timezone
con = sqlite3.connect(sys.argv[1])
con.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL)",
            (779, sys.argv[2], datetime.now(timezone.utc).isoformat(), "decision",
             "repo-less save", "n", "t", "l", "[]"))
con.commit()
PY
reconcile "$CATCHDIR" >/dev/null
catch_file="$MIRROR/genel/$(date +%Y-%m-%d).md"
check "a repo-less save is recovered under the catch-all" "#779" \
  "$(cat "$catch_file" 2>/dev/null)"

# The catch-all folder mixes every repo-less session, so without this the only
# way to tell an MDM PoC from a bringup session driving a remote machine is to
# read the body. A repo's folder name already carries that, which is why the
# stamp is scoped to the catch-all.
check "a catch-all entry names the directory it came from" \
  "$(basename "$CATCHDIR")" "$(cat "$catch_file" 2>/dev/null)"
check "and carries the full path" "Scope: " "$(cat "$catch_file" 2>/dev/null)"
if ! grep -q "Scope:" "$day_file"; then pass "a repo entry gets no scope line"
else fail "a repo entry gets no scope line" "$(cat "$day_file")"; fi
if [[ ! -d "$MIRROR/$CATCHKEY" ]]; then pass "and mem-lite's directory key gets no folder"
else fail "and mem-lite's directory key gets no folder" "$(ls "$MIRROR")"; fi

# --- concept-capture: an unsupervised writer that picks its own filename ----
echo
echo "concept-capture"

cc() { python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('cc', '$HOOKS_DIR/concept-capture.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
$1
"; }

WIKI="$VAULT/08- Wiki"
mkdir -p "$WIKI"
printf -- '---\ntype: wiki\n---\n\n# Kubernetes Scheduling\n\nEski satır.\n' \
  >"$WIKI/Kubernetes Scheduling.md"
printf -- '---\nmem_lite_project: %s\n---\n\n# repo\n' "$PROJECT" >"$WIKI/repo.md"

# The trigger has to classify content, not the session: an explanation and a
# work report look identical structurally, and 85% of sessions contain the
# structural signal. All this stage may do is hand the model prose the
# assistant wrote to the user, with the work and the secrets removed.
CTRANSCRIPT="$VAULT/concept-transcript.jsonl"
LONG_TEACH="$(python3 -c "print('scheduler bir podu nodea yerlestirir. ' * 60)")"
LONG_TOOL="$(python3 -c "print('bu turda arac cagirdim ve dosyayi okudum. ' * 60)")"
LONG_SECRET="$(python3 -c "print('baglanti icin api_key=hunter2 kullanilir. ' * 60)")"
python3 - "$CTRANSCRIPT" "$LONG_TEACH" "$LONG_TOOL" "$LONG_SECRET" <<'EOF'
import json, sys
path, teach, tool, secret = sys.argv[1:5]
recs = [
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": teach + "\n```\nKOD-BLOGU\n```\n"}]}},
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": tool}, {"type": "tool_use", "id": "1"}]}},
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": "kisa cevap"}]}},
    {"type": "assistant", "message": {"role": "assistant",
     "content": [{"type": "text", "text": secret}]}},
    {"type": "user", "message": {"role": "user", "content": "kullanicinin sorusu"}},
]
with open(path, "w", encoding="utf-8") as f:
    for r in recs:
        f.write(json.dumps(r) + "\n")
EOF

out="$(cc "print(m._explanations('$CTRANSCRIPT'))")"
check "keeps a long prose-only explanation" "yerlestirir" "$out"
check_absent "drops turns that called a tool" "arac cagirdim" "$out"
check_absent "drops turns too short to be an explanation" "kisa cevap" "$out"
check_absent "code fences are stripped, not sent" "KOD-BLOGU" "$out"
check_absent "credentials never leave the machine" "hunter2" "$out"
check_absent "never sends the user's own words" "kullanicinin sorusu" "$out"

# The first group is shaped after the lines this hook actually produced on a
# real session: the prompt forbids every one of them and the model wrote them
# regardless, so the rule is enforced in code instead -- what *we* did, and
# what names the repo, never reach a note. (Shaped after, not copied: the
# sentences a capture hook gets wrong are by definition someone's private
# notes, and this repo is public.) The second group is the note the model got
# right; a filter that eats those has replaced one failure with a worse one.
CCREPO="$VAULT/repos/dev-agent-kit"
mkdir -p "$CCREPO"
git -C "$CCREPO" init -q 2>/dev/null

out="$(cc "
tokens = m.project_tokens('$CCREPO')
bad = [
    'Kit, bu kurulumda en çok kaynak tüketen bileşen oldu.',
    'Aynı ayarı iki serviste de elle güncelliyoruz ve sonucunu hiç ölçmedik.',
    'Yeni modülü ölçmeden inşa ediyoruz.',
]
good = [
    'Claude Code, başlarken permission kurallarını denetliyor.',
    'Joker komutun ortasında kullanıldığında o pozisyona sokulan her şeyi onaylıyor.',
    'Jokerin sondaki kullanımı sorun değildir, bu yüzden diğer kurallar uyarı vermez.',
]
for ln in bad:
    print('DROPPED' if m.is_local_claim(ln, tokens) else 'KEPT-BAD:' + ln)
for ln in good:
    print('KEPT' if not m.is_local_claim(ln, tokens) else 'DROPPED-GOOD:' + ln)
print('TOKENS=' + ','.join(tokens))
print('CATCHALL=' + repr(m.project_tokens('/tmp')))
")"
check_absent "a line naming the repo never reaches a note" "KEPT-BAD:Kit," "$out"
check_absent "a report of what we did is not a concept" "KEPT-BAD:Tasarrufu" "$out"
check_absent "a project status report is not a concept" "KEPT-BAD:Temel" "$out"
check_absent "the filter does not eat a real explanation" "DROPPED-GOOD" "$out"
check "generic name parts are not treated as the subject" "TOKENS=agent,kit" "$out"
check "outside a repo the catch-all key is not a filter" "CATCHALL=()" "$out"

QC="$VAULT/concept-qwen.jsonl"
python3 - "$QC" "$LONG_TEACH" <<'EOF'
import json, sys
path, teach = sys.argv[1:3]
with open(path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
        "parts": [{"text": teach}]}}) + "\n")
    f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
        "parts": [{"text": teach}, {"functionCall": {"name": "read_file"}}]}}) + "\n")
EOF
out="$(cc "print(len(m._explanations('$QC')))")"
check "reads Qwen's parts shape, and its tool turns" "1" "$out"

# The filename comes out of a model, so it is untrusted input.
out="$(cc "print(m.note_path('$WIKI', '../../Code/repo'))")"
check "a traversing topic is refused" "None" "$out"
out="$(cc "print(m.note_path('$WIKI', 'kubernetes scheduling'))")"
check "an existing title is reused, not respelled" "Kubernetes Scheduling.md" "$out"
out="$(cc "print(m._titles('$WIKI'))")"
check_absent "the folder note is not offered as a subject" "'08- Wiki'" "$out"

# A live run filed a Kubernetes explanation under an unrelated existing note:
# "reuse a title" had been written as an unconditional ban on inventing one,
# which a small model reads as "you must pick from the list". The branch for
# "nothing here fits" is what stops that, so it is pinned here.
out="$(cc "print(m.PROMPT)")"
check "the model may open a new note when nothing fits" "kapsamıyorsa" "$out"
check_absent "reusing a title is never an unconditional ban" "Yeni bir başlık uydurma." "$out"

# A note owned by another write path must survive a name collision.
cc "print(m._append('$WIKI/repo.md', 'repo', ['Tamamen yeni bir cumle burada.']))" >/dev/null
check_absent "never writes into a repo note" "Tamamen yeni" "$(cat "$WIKI/repo.md")"

NEW="$WIKI/Terraform State.md"
cc "print(m._append('$NEW', 'Terraform State', ['State kaynak ile gercek dunyayi eslestirir.']))" >/dev/null
out="$(cat "$NEW" 2>/dev/null)"
check "a new note is stamped as a draft" "status: draft" "$out"
check "a new note is marked machine-written" "auto_compiled: true" "$out"
check "captured lines land in their own dated section" "auto:$(date +%Y-%m-%d)" "$out"

before="$(wc -l <"$NEW")"
cc "print(m._append('$NEW', 'Terraform State', ['State, kaynagi gercek dunya ile eslestirir.']))" >/dev/null
if [[ "$(wc -l <"$NEW")" == "$before" ]]; then
  pass "the same point reworded is not written twice"
else fail "the same point reworded is not written twice" "$before -> $(wc -l <"$NEW")"; fi

# HOME is redirected so the config fallback cannot supply the real endpoint
# and ship a transcript to it instead of asserting the gate.
out="$(echo '{"transcript_path":"'"$CTRANSCRIPT"'","session_id":"c1"}' \
  | VAULT_DIR="$VAULT" QWEN_BASE_URL="" HOME="$(mktemp -d)" \
    python3 "$HOOKS_DIR/concept-capture.py" 2>&1)"
if [[ -z "$out" ]]; then pass "no model endpoint is a silent no-op"
else fail "no model endpoint is a silent no-op" "$out"; fi

# --- vault-compile ------------------------------------------------------------
echo
echo "vault-compile"

TOOLS_DIR="$(cd "$HOOKS_DIR/../tools" && pwd)"
vc() { python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('vc', '$TOOLS_DIR/vault-compile.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
$1
"; }

NOTE_BODY='---
repo_path: /x/repo
mem_lite_project: workspace--repo
last_compiled:
---

# repo — Coding Notlari

## Mimari Ozet
- eski mimari

## Elle Yazilmis Bolum
- INSAN YAZDI bu satir kalmali

## Son Kararlar
- eski karar
### alt baslik korunmali

## Acik Sorular
- eski soru'

# The compiler owns three headings, not the file: a section a human added and a
# `###` nested under a managed one both have to survive a rewrite.
out="$(vc "
import sys
text = sys.stdin.read()
text = m.replace_section(text, '## Son Kararlar', ['- yeni karar'])
text = m.replace_section(text, '## Acik Sorular', ['- yeni soru'])
print(text)
" <<<"$NOTE_BODY")"
check "a human-written section survives a rewrite" "INSAN YAZDI bu satir kalmali" "$out"
check "a nested ### under a managed section survives" "alt baslik korunmali" "$out"
check_absent "a replaced bullet is gone" "eski karar" "$out"

# kida_azn carries one project key across seven per-component notes. Compiling
# all of them would write the whole repo's material into each, flattening seven
# hand-written notes into seven copies of one summary.
out="$(vc "
notes = [('/v/Code/repo/comp_a.md', '', 'workspace--repo'),
         ('/v/Code/repo.md', '', 'workspace--repo'),
         ('/v/Code/repo/comp_b.md', '', 'workspace--repo'),
         ('/v/Code/other.md', '', 'workspace--other')]
print([n[0] for n in m.preferred_note(notes)])
")"
check "one note per project key" "'/v/Code/other.md', '/v/Code/repo.md'" "$out"
check_absent "sibling component notes are not compiled" "comp_a" "$out"

# Output that is prose, or missing a marker, must leave the note untouched: a
# bad write into a note the user reads costs more than no write at all.
out="$(vc "print(m.parse_sections('duz metin, hic isaret yok')[1])")"
check "prose with no markers is refused" "missing-arch" "$out"
out="$(vc "print(m.parse_sections('<<<MIMARI>>>\n- a\n<<<KARARLAR>>>\n- b')[1])")"
check "a dropped marker is refused" "missing-questions" "$out"
out="$(vc "print(m.parse_sections('<<<MIMARI>>>\n<<<KARARLAR>>>\n- b\n<<<SORULAR>>>\n- c')[1])")"
check "an empty section is refused" "empty-arch" "$out"
out="$(vc "print(m.parse_sections('<<<SORULAR>>>\n- c\n<<<MIMARI>>>\n- a\n<<<KARARLAR>>>\n- b')[1])")"
check "markers in the wrong order are refused" "markers-out-of-order" "$out"
# A one-letter bullet is not a bullet: the length floor is what stops a model
# that answers with a stub list from overwriting a real section.
out="$(vc "print(m.parse_sections('<<<MIMARI>>>\n- a\n<<<KARARLAR>>>\n- b\n<<<SORULAR>>>\n- c')[1])")"
check "stub bullets do not count as content" "empty-arch" "$out"
out="$(vc "print(m.parse_sections('<<<MIMARI>>>\n- alfa\n<<<KARARLAR>>>\n- beta\n<<<SORULAR>>>\n- gama')[0])")"
check "well-formed output parses into three lists" "'arch': ['- alfa']" "$out"

out="$(vc "
import sys
print(m.stamp_compiled(m.replace_card(sys.stdin.read(), ['- yeni mimari']), '2026-01-02'))
" <<<"$NOTE_BODY")"
check "an empty last_compiled is filled in" "last_compiled: 2026-01-02" "$out"
check "an unmarked heading gains card markers" "agent-card:start" "$out"
if [[ "$(grep -c 'agent-card:start' <<<"$out")" == "1" ]]; then
  pass "card markers are not duplicated"
else fail "card markers are not duplicated" "$(grep -c 'agent-card:start' <<<"$out")"; fi

# The stamp used to go in above the opening `---`, which is the delimiter that
# opens frontmatter, not a line to insert before. Every repo note already had
# the field, so the branch first ran on the profile note -- and produced a file
# whose first line sat outside its own frontmatter.
out="$(vc "
print(repr(m.stamp_compiled('---\nagent_profile: true\n---\n\ngovde', '2026-01-02')))
")"
check "a note with no last_compiled gets one inside the frontmatter" \
  "'---\\nlast_compiled: 2026-01-02\\nagent_profile: true\\n---" "$out"

# The profile card holds bullets directly; a repo note's sits under a heading.
out="$(vc "
print(m.replace_card('<!-- agent-card:start -->\neski\n<!-- agent-card:end -->', ['- yeni'], heading=None))
")"
check_absent "the profile card gets no architecture heading" "Mimari" "$out"
check "the profile card keeps the new bullets" "- yeni" "$out"

# Machine-captured lines older than the last compile are already in the card.
AUTO='## Otomatik Yakalananlar
- eski gercek <!-- auto:2026-01-01 -->
- yeni gercek <!-- auto:2026-06-01 -->

## Baska Bolum
- bu bolum profil malzemesi degil'
out="$(vc "
import sys, datetime
text = sys.stdin.read()
since = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
print(m.auto_facts(text, since))
" <<<"$AUTO")"
check "only facts newer than the last compile are used" "yeni gercek" "$out"
check_absent "already-compiled facts are not re-sent" "eski gercek" "$out"
check_absent "the profile pass stops at the next section" "profil malzemesi degil" "$out"
check_absent "the auto stamp is stripped before the model sees it" "auto:2026-06-01" "$out"

out="$(vc "print(m.parse_card('duz metin')[1])")"
check "a card with no marker is refused" "missing-card" "$out"
out="$(vc "print(m.parse_card('<<<KART>>>\nbullet yok, sadece prose')[1])")"
check "a card with no bullets is refused" "no-bullets" "$out"
out="$(vc "print(m.parse_card('<<<KART>>>\n- **Bir sey:** aciklama\n  devam satiri')[0])")"
check "a wrapped bullet keeps its continuation line" "devam satiri" "$out"

# D6: the child must not inherit the vault it is compiling for, or its own Stop
# hooks fire on the compile transcript and personal-capture files a pile of repo
# lessons under Hakkimda.md.
STUB="$(mktemp -d)"
cat >"$STUB/claude" <<'STUBEOF'
#!/bin/sh
echo "VAULT_DIR=[${VAULT_DIR-unset}] MEM=[${MEM_OBSIDIAN_VAULT-unset}] OFF=[${VAULT_HOOKS_OFF-unset}] CWD=$(pwd)"
STUBEOF
chmod +x "$STUB/claude"
out="$(VAULT_DIR="$VAULT" MEM_OBSIDIAN_VAULT="$VAULT" PATH="$STUB:$PATH" vc "
print(m.run_model('x', 'sonnet', 30)[0])
")"
check "the model child gets no VAULT_DIR" "VAULT_DIR=[]" "$out"
check "the model child gets no mirror dir" "MEM=[]" "$out"
check "the model child is told the vault is off" "OFF=[1]" "$out"
check "the model child runs in a scratch dir" "vault-compile-" "$out"

# Asserting the variable is cleared proved nothing: the installer writes the
# same values to ~/.config/dev-agent-kit/vault.env, so the first version of
# this test passed while the child resolved the vault from the file anyway.
# The outcome is what has to be pinned, not the mechanism.
CONF_HOME="$(mktemp -d)"; mkdir -p "$CONF_HOME/.config/dev-agent-kit"
printf 'VAULT_DIR=%s\n' "$VAULT" >"$CONF_HOME/.config/dev-agent-kit/vault.env"
out="$(HOME="$CONF_HOME" VAULT_DIR="" VAULT_HOOKS_OFF=1 python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('vcm', '$HOOKS_DIR/vault_common.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
print('resolved:', c.vault_dir())
")"
check "VAULT_HOOKS_OFF beats the config file" "resolved: None" "$out"
out="$(HOME="$CONF_HOME" env -u VAULT_DIR python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('vcm', '$HOOKS_DIR/vault_common.py')
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
print('resolved:', c.vault_dir())
")"
check "an absent VAULT_DIR still falls back to the config file" "resolved: $VAULT" "$out"

# The vault's git tree carries the user's own uncommitted work, so `git checkout`
# is not an undo for this tool. Every note is copied aside before it is written.
BTMP="$(mktemp -d)"; mkdir -p "$BTMP/sub"; echo "orijinal" >"$BTMP/sub/n.md"
out="$(vc "
dest = m.backup('$BTMP/sub/n.md', '$BTMP')
print(dest, open(dest).read().strip())
")"
check "a note is backed up before writing" "orijinal" "$out"
check "backups live in the vault's backup dir" ".vault-compile-backups" "$out"

# The template every repo note is created from carries a placeholder key. It
# read as a real project for as long as the guard only tested the prefix --
# the shipped template is keyed `workspace--{{REPO}}`, which does not start
# with a brace. It was iterated on every run, and the first time it matched
# new observations the compile would have landed in the template itself.
# obsidian-mirror writes the day files with no frontmatter and no links, and
# the repo note's dataviewjs lists are render-time queries that never reach
# Obsidian's link index. Without these wikilinks every raw day file is an
# orphan -- all ten in the live vault were.
STMP="$(mktemp -d)"
mkdir -p "$STMP/_mem-log/workspace--repo"
: > "$STMP/_mem-log/workspace--repo/2026-08-25.md"
: > "$STMP/_mem-log/workspace--repo/2026-08-26.md"
out="$(vc "
print(m.source_links('workspace--repo', '$STMP'))
print('EMPTY:', m.source_links('workspace--absent', '$STMP'))
")"
check "raw day files get a real wikilink" "[[_mem-log/workspace--repo/2026-08-26|" "$out"
# Vault-root-relative, not `../../../`: a repo note sits three levels down
# under one tree and four under another, so a fixed depth is broken for one.
if [[ "$out" == *"../"* ]]; then
  FAIL=$((FAIL + 1)); echo "  FAIL: source links are vault-root-relative"
else
  PASS=$((PASS + 1)); echo "  PASS: source links are vault-root-relative"
fi
check "newest day first" "2026-08-26|2026-08-26 ham log]]', '- [[_mem-log/workspace--repo/2026-08-25" "$out"
check "a project with no log dir yields nothing" "EMPTY: []" "$out"

# The outcome, not the helper: deleting the call that writes the section left
# the source_links tests above completely green.
out="$(vc "
note = '''---
mem_lite_project: workspace--repo
last_compiled:
---

## Mimari Ozet
<!-- agent-card:start -->
- eski
<!-- agent-card:end -->

## Son Kararlar
- eski

## Acik Sorular
- eski
'''
parsed = {'arch': ['- yeni'], 'decisions': ['- karar'], 'questions': ['- soru']}
print(m.apply_compiled(note, parsed, 'workspace--repo', '$STMP', '2026-08-26'))
")"
check "the compiled note carries a Kaynaklar section" "## Kaynaklar" "$out"
check "and the link lands in it" "[[_mem-log/workspace--repo/2026-08-26|" "$out"
check "the compile is still stamped" "last_compiled: 2026-08-26" "$out"

# A project under the compile threshold is skipped before the section is ever
# rewritten, so every day file it collects meanwhile would stay an orphan.
# Linking costs nothing -- the paths come off the filesystem -- so it must not
# wait on a compile.
mkdir -p "$STMP/note"
printf -- '---\nmem_lite_project: workspace--repo\nlast_compiled: 2026-08-01\n---\n\n# repo\n' \
  > "$STMP/note/repo.md"
out="$(vc "
class A: apply = True
m.refresh_sources('$STMP/note/repo.md', 'workspace--repo', A(), '$STMP')
print(open('$STMP/note/repo.md').read())
")"
check "links are refreshed without a compile" "[[_mem-log/workspace--repo/2026-08-26|" "$out"
check_absent "and no compile date is stamped by it" "last_compiled: 2026-08-26" "$out"

# A repo with no note produces no compile, and a compile that never runs
# reports nothing -- so it stays noteless and the silence reads as "nothing to
# do". compile-nudge used to say this; it moved here when that hook went.
MEMDB="$(mktemp -d)/mem.db"
python3 - "$MEMDB" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE observations (project TEXT, superseded_at TEXT, "
            "compressed_into TEXT)")
con.executemany("INSERT INTO observations VALUES (?, NULL, NULL)",
                [("workspace--noteless",)] * 12
                + [("workspace--owned",)] * 12
                + [("workspace--{{REPO}}",)] * 12
                + [("workspace--barely",)] * 3)
con.commit()
PYEOF
out="$(vc "
m.MEM_DB = '$MEMDB'
print(m.unowned_projects({'workspace--owned'}))
")"
check "a project with material and no note is named" "workspace--noteless" "$out"
check_absent "a project that has a note is not" "workspace--owned" "$out"
check_absent "the template's placeholder key is not reported" "{{REPO}}" "$out"
check_absent "and neither is a project below the threshold" "workspace--barely" "$out"
rm -rf "$STMP" "$(dirname "$MEMDB")"

ITMP="$(mktemp -d)"
mkdir -p "$ITMP/04- Templates"
printf -- '---\nmem_lite_project: workspace--{{REPO}}\nlast_compiled:\n---\n' \
  > "$ITMP/04- Templates/CodeRepoTemplate.md"
printf -- '---\nmem_lite_project: workspace--real\nlast_compiled:\n---\n' \
  > "$ITMP/real.md"
out="$(vc "
print(sorted(p for _, _, p in m.iter_notes('$ITMP')))
")"
check "a real note is still picked up" "workspace--real" "$out"
if [[ "$out" == *"{{REPO}}"* ]]; then
  FAIL=$((FAIL + 1)); echo "  FAIL: the note template is skipped, not compiled"
else
  PASS=$((PASS + 1)); echo "  PASS: the note template is skipped, not compiled"
fi
rm -rf "$ITMP"

# --- vault-lint: CLAUDE.md Section 5 / Vault Standards.md's "Haftalık Bakım"
# as a script instead of an ad hoc read -- stale raw notes, broken backlinks,
# and the notes Vault Standards.md calls "sınıfsız veya başlıksız".
echo
echo "vault-lint"

vl() { python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('vl', '$TOOLS_DIR/vault-lint.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
$1
"; }

LVAULT="$VAULT/lintvault"
mkdir -p "$LVAULT/07- Raw" "$LVAULT/08- Wiki"

cat >"$LVAULT/07- Raw/07- Raw.md" <<'EOF'
# 07- Raw
EOF

cat >"$LVAULT/07- Raw/Old.md" <<'EOF'
---
source_type: article
source_url: https://example.com
captured: 2020-01-01
compiled: false
---
old, unread
EOF

cat >"$LVAULT/07- Raw/New.md" <<'EOF'
---
source_type: article
source_url: https://example.com
captured: 2026-08-28
compiled: false
---
just captured
EOF

cat >"$LVAULT/07- Raw/Bare.md" <<'EOF'
no frontmatter at all
EOF

cat >"$LVAULT/08- Wiki/HasHeading.md" <<'EOF'
# A Concept
body
EOF

cat >"$LVAULT/08- Wiki/NoHeading.md" <<'EOF'
just a paragraph, no heading
EOF

cat >"$LVAULT/Target.md" <<'EOF'
# Target
EOF

cat >"$LVAULT/Linker.md" <<'EOF'
Points at [[Target]] and at [[Nowhere]].
EOF

cat >"$LVAULT/Stray.md" <<'EOF'
root-level, no folder
EOF

cat >"$LVAULT/AutoNote.md" <<EOF
- old fact <!-- auto:2020-01-01 -->
- fresh fact <!-- auto:$(date +%Y-%m-%d) -->
EOF

out="$(vl "
notes = list(m._walk_notes('$LVAULT'))
all_files = list(m._walk_all_files('$LVAULT'))
print('stale:', m.stale_raw_notes('$LVAULT', notes, 14))
print('broken:', m.broken_backlinks('$LVAULT', notes, all_files))
print('unclassified:', m.unclassified_notes('$LVAULT', notes))
print('stale_auto:', m.stale_auto_captures('$LVAULT', notes, 45))
")"

check "a compiled:false note past the window is stale" "07- Raw/Old.md" "$out"
check_absent "a compiled:false note within the window is not" "07- Raw/New.md" "$out"
check "an unresolved wikilink is broken" "Nowhere" "$out"
check_absent "a wikilink to a real note is not" "('Linker.md', 'Target')" "$out"
check "a note with no frontmatter at all is unclassified" "07- Raw/Bare.md" "$out"
check_absent "the folder's own index note is exempt from the raw contract" \
  "07- Raw/07- Raw.md" "$out"
check "a wiki note with no heading is unclassified" "08- Wiki/NoHeading.md" "$out"
check_absent "a wiki note with a heading is not" "HasHeading" "$out"
check "a root-level file is a stray note" "'Stray.md'" "$out"
check "an old auto-capture past the window is reported" "old fact" "$out"
check_absent "a fresh auto-capture within the window is not" "fresh fact" "$out"

# --- the catch-all: the work that belongs to no repo ------------------------
# mem-lite names a repo-less session after whatever directory it started in,
# so `/home/ali`, `~/Downloads` and a scratch dir each became their own key.
# Every shard stayed far under the note threshold, so none was ever reported
# as noteless, none got a note, and none was compiled or injected -- captured
# and unreachable, which looks exactly like nothing having been captured.
echo
echo "catch-all"

CREPO="$(mktemp -d)"; git -C "$CREPO" init -q
CPLAIN="$(mktemp -d)"
REPO_KEY="$(basename "$(dirname "$CREPO")")--$(basename "$CREPO")"
out="$(python3 -c "
import sys; sys.path.insert(0, '$HOOKS_DIR')
import vault_common as v
print('repo=' + v.infer_project('$CREPO'))
print('plain=' + v.infer_project('$CPLAIN'))
")"
check "a repo still keys off its git root" "repo=$REPO_KEY" "$out"
check "a directory outside any repo collapses to one key" "plain=genel" "$out"

# The classification has to be redone in the hook rather than trusted from
# mem-lite's key, because mem-lite has no notion of the catch-all at all.
CMIRROR="$(mktemp -d)"
cmirror() { # cwd, project-as-mem-lite-named-it
  echo '{"tool_name":"mcp__mem-lite__mem_save","cwd":"'"$1"'",'\
'"tool_input":{"title":"t","content":"c"},"tool_response":{"content":[{"text":'\
'"Saved as observation #777 [bugfix] in project \"'"$2"'\""}]}}' \
    | MEM_OBSIDIAN_VAULT="$CMIRROR" HOME="$(mktemp -d)" python3 "$HOOKS_DIR/obsidian-mirror.py" 2>&1
}
TODAY="$(date +%Y-%m-%d)"

cmirror "$CPLAIN" "home--ali" >/dev/null
if [[ -f "$CMIRROR/genel/$TODAY.md" ]]; then
  pass "a repo-less save is filed under the catch-all"
else fail "a repo-less save is filed under the catch-all" "$(ls -R "$CMIRROR")"; fi
if [[ ! -d "$CMIRROR/home--ali" ]]; then
  pass "and mem-lite's directory-shaped key gets no folder of its own"
else fail "and mem-lite's directory-shaped key gets no folder of its own" "$(ls -R "$CMIRROR")"; fi

rm -rf "${CMIRROR:?}"/*
cmirror "$CREPO" "workspace--repo" >/dev/null
if [[ -f "$CMIRROR/workspace--repo/$TODAY.md" ]]; then
  pass "a save inside a repo keeps mem-lite's key untouched"
else fail "a save inside a repo keeps mem-lite's key untouched" "$(ls -R "$CMIRROR")"; fi

# Selection by id off the day files, not by name and not by volume. Folding in
# every unclaimed key under the threshold was tried first and swallowed four
# real repos whose notes had simply not been written yet.
CVAULT="$(mktemp -d)"; mkdir -p "$CVAULT/_mem-log/genel"
printf -- '### 10:00 — [decision] bir (#11)\n\n---\n\n### 10:05 — [bugfix] iki (#22)\n\n---\n' \
  > "$CVAULT/_mem-log/genel/2026-08-26.md"
CDB="$(mktemp -d)/mem.db"
python3 - "$CDB" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE observations (id INTEGER, project TEXT, created_at TEXT, "
            "type TEXT, title TEXT, subtitle TEXT, lesson_learned TEXT, "
            "narrative TEXT, importance INTEGER, superseded_at TEXT, "
            "compressed_into TEXT)")
con.executemany(
    "INSERT INTO observations VALUES (?,?,?,?,?,NULL,NULL,NULL,1,NULL,NULL)",
    [(11, "home--ali", "2026-08-26T10:00:00", "decision", "bir"),
     (22, "ali--Downloads", "2026-08-26T10:05:00", "bugfix", "iki"),
     (33, "workspace--kucukrepo", "2026-08-26T10:09:00", "feature", "uc")])
con.commit()
PYEOF
out="$(vc "
m.MEM_DB = '$CDB'
print('ids=%s' % m.catchall_ids('$CVAULT'))
recs, total = m.observations('genel', None, '$CVAULT')
print('total=%d' % total)
print(' '.join(r['text'] for r in recs))
")"
check "the catch-all reads its ids off its own day files" "ids=[11, 22]" "$out"
check "and pulls exactly those records, whatever mem-lite called them" "total=2" "$out"
check_absent "a small repo's material is never swallowed into it" "kucukrepo" "$out"

# Selecting by the name `genel` would find nothing and compile the note empty,
# overwriting a real one with a summary of no records.
out="$(vc "
m.MEM_DB = '$CDB'
print('novault=%s' % (m.observations('genel', None, None),))
")"
check "without a vault root the catch-all compiles nothing" "novault=([], 0)" "$out"

# Every piece of framing in the compiler assumed a repo. Left alone, the
# catch-all note would be headed "Mimari Özet" and the model told to summarise
# a codebase -- a general conversation described as if it were software. The
# heading the prompt asks for and the heading apply_compiled writes come from
# one function for the same reason.
rec="[{'text': '### 2026-08-26 [decision] x', 'importance': 1, 'created': '2026-08-26'}]"
out="$(vc "
for p in ('workspace--repo', 'genel'):
    print(p, '|', m.card_heading(p), '|', m.system_prompt(p)[:70])
    print(m.build_prompt(p, 'n.md', '(mevcut)', $rec, 1, 0).splitlines()[0])
")"
check "a repo note is still framed as a repo" 'deposunun vault notu' "$out"
check "and still headed Mimari Özet" "workspace--repo | ## Mimari Özet" "$out"
check "the catch-all is not framed as a repo" "repo dışında kalan işlerin vault notu" "$out"
check "and gets its own card heading" "genel | ## Genel Özet" "$out"
check_absent "nor is it called a depo in its system prompt" "genel | ## Genel Özet | Sen bir bilgi tabanı derleyicisisin. Bir yazılım deposu" "$out"
rm -rf "$CREPO" "$CPLAIN" "$CMIRROR" "$CVAULT" "$(dirname "$CDB")"

# --- source classification: two consumers, one corpus -----------------------
# The vault is ~88% coding telemetry by volume. A general or voice assistant
# reading the same corpus retrieves an ENC gRPC port number when it was asked
# who the user is, so the audience split has to be assertable. The sensitivity
# half is the D#10 debt: which material needs a pass before anything leaves the
# machine. Both are lookups, so the only thing that can rot is the defaults --
# an unlisted source that reads as "not sensitive" is the failure.
echo
echo "source classification"

cls() { python3 -c "
import sys; sys.path.insert(0, '$HOOKS_DIR')
import vault_common as v
$1
"; }

out="$(cls "
for k in ('workspace--dev-agent-kit', 'workspace--miivii_setup_ansible',
          'workspace--never-seen-before', None):
    print('%s -> %s' % (k, v.classify_project(k)))
")"
check "a listed personal repo is personal" \
  "workspace--dev-agent-kit -> ('coding', 'personal')" "$out"
check "a work repo is work" \
  "workspace--miivii_setup_ansible -> ('coding', 'work')" "$out"
# The whole point of inverting the list: forgetting to classify a new repo must
# cost an unnecessary review, never a missed one.
check "an unlisted repo defaults to sensitive" \
  "workspace--never-seen-before -> ('coding', 'work')" "$out"
check "and so does no key at all" "None -> ('coding', 'work')" "$out"
check "the catch-all admits it is mixed" "('coding', 'mixed')" \
  "$(cls "print(v.classify_project(v.CATCHALL_PROJECT))")"

out="$(cls "
for p in ('00- Home/01-Personal/Personal Space/Hakkimda.md',
          '02- Kuartis/02- Vuran/x.md',
          '08- Wiki/Fine-tuning.md',
          '_mem-log/workspace--dev-agent-kit/2026-08-25.md',
          '99- Unlisted/x.md'):
    print('%s | %s | life=%s' % (p, v.classify_vault_path(p), v.is_life_material(p)))
")"
check "the profile folder is what a general assistant may read" \
  "Hakkimda.md | ('life', 'personal') | life=True" "$out"
check "a work-repo note is not" "x.md | ('coding', 'work') | life=False" "$out"
check "a wiki concept serves both" "Fine-tuning.md | ('both', 'personal') | life=True" "$out"
check "the raw mem log is coding material" \
  "2026-08-25.md | ('coding', 'mixed') | life=False" "$out"
check "an unlisted folder defaults to sensitive coding material" \
  "99- Unlisted/x.md | ('coding', 'work') | life=False" "$out"

# Keyed on the first segment, so classifying a folder and classifying a note
# inside it can never disagree.
if [[ "$(cls "print(v.classify_vault_path('08- Wiki'))")" == \
      "$(cls "print(v.classify_vault_path('08- Wiki/anything.md'))")" ]]; then
  pass "a folder and a note inside it classify the same"
else fail "a folder and a note inside it classify the same" "segment split drifted"; fi

echo
echo "concurrent writers"

# personal-capture, concept-capture and obsidian-mirror all append to the
# same handful of notes from Stop/PostToolUse hooks that two hosts (Claude,
# Qwen) or two overlapping sessions can fire at once. Without a lock, two
# processes racing an `open(path, "a")` can interleave their writes and
# corrupt the note rather than merely duplicate a line -- that is the failure
# locked_note() exists to close, and the only way to know it still does is a
# real multi-process race, not a single-process unit test.
LOCKFILE="$(mktemp -u)"
WRITERS=8
LINES_EACH=50
for i in $(seq 1 $WRITERS); do
  python3 -c "
import sys; sys.path.insert(0, '$HOOKS_DIR')
from vault_common import locked_note
with locked_note('$LOCKFILE') as f:
    for n in range($LINES_EACH):
        f.write('writer-$i-line-%d\n' % n)
" &
done
wait

got_lines="$(wc -l < "$LOCKFILE" | tr -d ' ')"
want_lines=$((WRITERS * LINES_EACH))
if [[ "$got_lines" == "$want_lines" ]]; then
  pass "concurrent locked_note writers keep every line"
else
  fail "concurrent locked_note writers keep every line" "expected $want_lines lines, got $got_lines"
fi

# Corruption reads as a line that is neither empty nor one of the exact
# lines a writer wrote -- flock guarantees each writer's block lands whole,
# so an interleaved write would produce a malformed line, not a missing one.
bad_lines="$(grep -cvE '^writer-[0-9]+-line-[0-9]+$' "$LOCKFILE" || true)"
if [[ "${bad_lines:-0}" == "0" ]]; then
  pass "no writer's lines interleave with another's"
else
  fail "no writer's lines interleave with another's" "$bad_lines malformed line(s)"
fi
rm -f "$LOCKFILE"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
