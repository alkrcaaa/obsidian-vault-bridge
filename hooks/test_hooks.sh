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
# so an uncompiled note was announced as "compiled ---" and compile-nudge --
# which parses the same field as a date -- failed and exited on every note
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

# --- compile-nudge: the notes that need compiling most were the silent ones --
echo
echo "compile-nudge"

# Its own mem-lite DB under the redirected HOME, and its own TMPDIR so the
# once-per-day marker cannot be a leftover from the real machine.
mkdir -p "$HOME/.claude-mem-lite"
python3 - "$HOME/.claude-mem-lite/claude-mem-lite.db" "$PROJECT" <<'EOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE observations (project TEXT, created_at TEXT)")
con.executemany("INSERT INTO observations VALUES (?, '2026-01-01T00:00:00')",
                [(sys.argv[2],)] * 12)
con.commit()
EOF

nudge() {
  echo "{\"cwd\":\"$REPO\"}" | TMPDIR="$(mktemp -d)" VAULT_DIR="$VAULT" \
    python3 "$HOOKS_DIR/compile-nudge.py" 2>&1
}

# `last_compiled:` left blank is a note nobody ever compiled, not a malformed
# date. Reading it as the frontmatter's closing `---` made every such note
# unparsable, so the hook exited and 18 stub notes never asked to be filled.
python3 - "$VAULT/Code/repo.md" <<'EOF'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
open(p, "w", encoding="utf-8").write(t.replace("last_compiled: 2026-01-01",
                                               "last_compiled:"))
EOF
out="$(nudge)"
check "a never-compiled note asks to be compiled" "never been compiled" "$out"

# A value that is there but unparsable is a typo to fix by hand, not a backlog.
python3 - "$VAULT/Code/repo.md" <<'EOF'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
open(p, "w", encoding="utf-8").write(t.replace("last_compiled:",
                                               "last_compiled: not-a-date"))
EOF
out="$(nudge)"
if [[ -z "$out" ]]; then pass "a malformed date stays silent"
else fail "a malformed date stays silent" "${out:0:120}"; fi

# A quoted date is valid YAML and used to be unparsable too -- the note then
# looked malformed and never nudged however stale it got.
python3 - "$VAULT/Code/repo.md" <<'EOF'
import sys
p = sys.argv[1]
t = open(p, encoding="utf-8").read()
open(p, "w", encoding="utf-8").write(t.replace("last_compiled: not-a-date",
                                               'last_compiled: "2026-01-01"'))
EOF
out="$(nudge)"
check "a quoted date is parsed and its note nudged" "is behind" "$out"

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

# D6: the child must not inherit the vault it is compiling for, or its own Stop
# hooks fire on the compile transcript and personal-capture files a pile of repo
# lessons under Hakkimda.md.
STUB="$(mktemp -d)"
cat >"$STUB/claude" <<'STUBEOF'
#!/bin/sh
echo "VAULT_DIR=[${VAULT_DIR-unset}] MEM=[${MEM_OBSIDIAN_VAULT-unset}] CWD=$(pwd)"
STUBEOF
chmod +x "$STUB/claude"
out="$(VAULT_DIR="$VAULT" MEM_OBSIDIAN_VAULT="$VAULT" PATH="$STUB:$PATH" vc "
print(m.run_model('x', 'sonnet', 30)[0])
")"
check "the model child gets no VAULT_DIR" "VAULT_DIR=[unset]" "$out"
check "the model child gets no mirror dir" "MEM=[unset]" "$out"
check "the model child runs in a scratch dir" "vault-compile-" "$out"

# The vault's git tree carries the user's own uncommitted work, so `git checkout`
# is not an undo for this tool. Every note is copied aside before it is written.
BTMP="$(mktemp -d)"; mkdir -p "$BTMP/sub"; echo "orijinal" >"$BTMP/sub/n.md"
out="$(vc "
dest = m.backup('$BTMP/sub/n.md', '$BTMP')
print(dest, open(dest).read().strip())
")"
check "a note is backed up before writing" "orijinal" "$out"
check "backups live in the vault's backup dir" ".vault-compile-backups" "$out"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
