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
trap 'rm -rf "$VAULT" "$REPO"' EXIT

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

out="$(echo "{\"cwd\":\"$REPO\"}" | VAULT_DIR="" python3 "$HOOKS_DIR/vault-inject.py" 2>&1)"
if [[ -z "$out" ]]; then pass "no VAULT_DIR is a silent no-op"
else fail "no VAULT_DIR is a silent no-op" "expected no output, got: ${out:0:120}"; fi

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

out="$(echo '{"transcript_path":"'"$TRANSCRIPT"'","session_id":"t1"}' | VAULT_DIR="$VAULT" QWEN_BASE_URL="" python3 "$HOOKS_DIR/personal-capture.py" 2>&1)"
if [[ -z "$out" ]]; then pass "no model endpoint is a silent no-op"
else fail "no model endpoint is a silent no-op" "$out"; fi

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
