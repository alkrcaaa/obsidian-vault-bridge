# Design decisions

Why this module is shaped the way it is. Each entry is a decision that survived
being measured, not an intention. Read this before changing a hook's contract —
several of these were re-derived the expensive way after a session lost the
context that produced them.

Format: **Decision** — what was settled. **Because** — the evidence. **Don't** —
the specific reversal that would undo it.

---

## D1. The point of this module

**Decision.** While the user works with Claude or Qwen, the agents write notes
about *the user* and about *the repos*, into their Obsidian vault. The vault then
feeds those notes back into later sessions. Two payoffs, in this order:
a second brain the user reads, and token savings from injecting a compiled note
instead of rediscovering the repo.

**Because.** This is the standing instruction, restated whenever the work drifted
into mechanism. The vault's own vision note (`08- Wiki/İkinci Beyin Projesi.md`,
2026-08-16) frames it as Karpathy's LLM-knowledge-base loop: ingest → compile →
Q&A → output.

**Don't.** Turn any piece of this into an article summarizer, a general note
organizer, or a chat archive. If a change doesn't make the agent write better
notes about the user/repos, or make those notes cheaper to reuse, it is off-axis.

---

## D2. The pipeline, and where it was broken

**Decision.** The chain is:

```
repo work → mem_save → obsidian-mirror → _mem-log/<project>/<date>.md ─┐
                                                                        ├→ COMPILE → Code/<repo>.md ─┐
user talk → personal-capture (Stop) ────────────→ Hakkımda.md ─────────┘                             ├→ vault-inject (session start)
session explanations → concept-capture (Stop) ──→ 08- Wiki/<konu>.md                                 ┘
```

**Because.** Measured 2026-08-26, and then re-measured after the first
measurement turned out to be misread:

- mem-lite holds **335 observations, ~280 with `lesson_learned`, over 18 projects**.
- The vault holds **38 `Code/<repo>.md` notes: 17 never compiled** (empty
  `last_compiled`) and **18 compiled by hand on 2026-08-16/17**, with **1
  recompiled since** (dev-agent-kit, 08-23).
- The tempting reading — "~100 lessons stranded in `miivii_setup_ansible`" — is
  **wrong**, and was corrected by querying dates instead of counts: every one of
  that project's 117 observations predates 2026-08-02, so they were the *input*
  to the August hand-compile, not a backlog it missed. Same for `overwatch-infra`
  and `metadata-llm-brige`: zero observations after 08-16.
- Likewise the 17 never-compiled notes are not a backlog: those repos have **zero**
  mem-lite observations. Nothing to compile.
- The real, current backlog is **~31 observations across 6 notes**, 25 of them
  piled up in `dev-agent-kit` in three days.

So the case for a compiler is not a stranded pile. It is that the hand-compile
was a **one-off**: every note has been drifting from its repo since the day it
was written, and the drift is invisible until someone returns to that repo. The
failing step is Compile, and only Compile — capture works, feedback works.

**Don't.** Argue from record counts alone. Counts said "100 lessons unread";
dates said those lessons were already compiled. Any claim about a backlog has to
compare observation dates against `last_compiled`, which is exactly what the
tool does and what the eyeball estimate did not.

---

## D3. Compile runs out-of-band, not inside a session

**Decision.** Compile is a separate executable the user (or cron) runs, not a
hook and not something the agent does mid-session.

**Because.** `compile-nudge` fired 3 times in 7 days and was acted on 0 times.
The reason is structural, not forgetfulness: compile is a *different job* than
the session's job, and it loses to the task at hand every time. Making the nudge
louder does not change that.

**Don't.** Re-solve this by escalating the nudge, or by having a Stop hook
compile silently.

**Since.** `compile-nudge` was kept at first as a staleness signal. Once the
timer ran daily there was nothing left for it to report that the next run would
not already have fixed, so it was removed rather than left to speak up about
work that was already scheduled. Moving a job out of the session also means
installing the thing that starts it: for the first three commits the compiler
existed and nothing ran it, which is the same once-and-drift failure this
decision set out to end.

---

## D4. Compile's source of truth is the mem-lite DB, not `_mem-log`

**Decision.** The compiler reads `~/.claude-mem-lite/claude-mem-lite.db` directly.

**Because.** `_mem-log` markdown only covers 5 projects / 29 lessons — mirroring
started recently. The DB has all 335, with structured `lesson_learned`,
`importance`, `type` and supersession fields to rank and filter on.

**Don't.** Parse the mirrored markdown. It is a human-readable mirror, explicitly
not the source of truth (see README, `obsidian-mirror`).

---

## D5. Compile is synthesis, so it uses the strong model — not the local 27B

**Decision.** The compiler shells out to `claude -p`. The local 27B is not used
for this step.

**Because.** Two reasons, both already paid for once. (1) The rule set in #416:
give the local model *extraction*, never *synthesis* — synthesis is its ceiling,
and the most expensive place to make that mistake is the note the user reads to
learn. Compile is synthesis by definition: 109 lessons → a readable note.
(2) Cost is not an argument here: all 280 lessons together are ≈35k tokens, about
a tenth of one working session. Using the weak model to save that would be false
economy.

**Don't.** "Optimize" this onto qwen later. Capture hooks (`personal-capture`,
`concept-capture`) stay on the 27B — those are extraction, and they run per
session. Compile is the one place the asymmetry flips.

---

## D6. The compiler runs its subprocess with `VAULT_DIR` unset

**Decision.** The `claude -p` child is spawned with `VAULT_HOOKS_OFF=1`, cwd in
a scratch dir, and the vault variables emptied for good measure.

**Because.** Otherwise the child session's own Stop hooks fire on the *compile*
transcript: `personal-capture` reads a pile of repo lessons as facts about the
user and writes them into `Hakkımda.md`.

This decision was first written as "clear `VAULT_DIR`, reuse the module's
existing opt-in switch, don't invent a suppression flag." That was wrong, and
the test written for it passed while being wrong — it asserted the variable was
cleared in the child, which is the mechanism, not the outcome. The outcome was
that the child resolved the vault anyway: the installer writes the same values
to `~/.config/dev-agent-kit/vault.env`, and an *absent* `VAULT_DIR` is exactly
the case that file exists to answer (a hook inherits the agent's env, and the
agent is started from a shell that never exported it — the failure that kept
these hooks dead in production for weeks). So "unset" already meant "read the
file", and the contract had no way at all to say "off". A dedicated switch was
not bloat; it was the missing half of the contract.

**Don't.** Assert that a disabling mechanism was applied. Assert that the thing
is disabled — here, that `vault_dir()` returns None in the child's environment,
with a config file present.

---

## D7. `Code/<repo>.md` is a cumulative MOC, rewritten — not an append log

**Decision.** Each compile rewrites the managed sections to reflect the repo's
current state plus durable lessons. It does not append a dated section per run.

**Because.** Appending is what `_mem-log` already does; a second chronological
copy adds nothing and drifts. The drift is already visible: dev-agent-kit's note
carries both `## Son Kararlar` and `## Son Kararlar (2026-08-23 derlemesi)` from
a single hand-compile.

**Don't.** Delete the raw `_mem-log` history to "save space". Research finding
already recorded in that note (arXiv 2601.00821): a compiled note must sit *on
top of* verbatim records, never replace them.

---

## D8. The compiler owns three sections and nothing else

**Decision.** It rewrites only: the `<!-- agent-card:start/end -->` block
(`## Mimari Özet`), `## Son Kararlar`, and `## Açık Sorular`. Frontmatter (except
`last_compiled`), title, description line, and any section a human added are
preserved byte-for-byte. Default run is a dry-run diff; writing requires
`--apply`.

**Because.** The agent-card block is exactly what `vault-inject` puts into every
session — it is the token-saving payload, so it is the part that must stay
accurate. And #417's lesson: an automated writer that lands in a human's note is
more expensive than one that writes nothing, because the line inherits the
authority of a note the human wrote.

The vault being git-tracked was written down here as the backstop. The first
real run showed why that is not enough: the working tree already held hundreds
of uncommitted lines of the user's own note-writing, so `git checkout` would
have discarded *their* work, not this tool's. Every note is therefore copied to
`$VAULT/.vault-compile-backups/<stamp>_<path>.md` before it is overwritten, and
a failed copy aborts the write.

**Don't.** Let the model choose a target file or a heading name. Both are
untrusted output (#417: given three headings and no "none of these" branch, the
27B filed a k8s concept into an unrelated hand-written note and the metric still
said `action=capture`).

---

## D9. Every silent exit records a metric, and the metric says where it wrote

**Decision.** Success metrics carry the destination, not just the action.

**Because.** Four separate silent no-ops shipped before this rule (#404 env not
read, #414 `content` vs `llmContent`, #415 regex crossing a line ending, #417
wrong note) and every one of them looked healthy from the outside. #417 in
particular recorded `action=capture` while writing to the wrong file — "it ran"
and "it did the right thing" are different claims and only the first was
instrumented.

**Don't.** Call a hook verified because its metric line is green. First live run
gets its output read by eye.


---

## D10. Deleting a bullet requires evidence; running out of room is not evidence

**Decision.** The compile prompt states that existing bullets are kept by
default, and may only be dropped when the records show the decision was
reversed or the question closed. The section cap (16 bullets) is not a licence
to trim.

**Because.** The first live run read "rewrite these sections" as "list what is
recent" and silently dropped 6 still-valid decisions — qwen sampling, the skill
wrapper rewrite, the mem-lite namespace bug, GitLab MCP, infra-repo support,
night-run. A note that does that becomes a rolling window and forgets a little
on every compile, which is the exact opposite of D7. With deletion made
conditional on evidence, the second run dropped exactly one bullet: the module
extraction question, which the records show actually closed.

**Don't.** Judge a compile by whether the new bullets look good. Read the
removed ones — that is where the loss hides, and a diff shows them for free.

---

## D11. The 27B captures broadly; the strong model decides what is durable

**Decision.** `personal-capture` (local 27B, every session) writes into the
profile note's own `## Otomatik Yakalananlar` section, which is never injected.
`vault-compile --profile` (strong model, out-of-band) reads that section and
rewrites the injected agent card. Recall in the cheap pass, precision in the
expensive one.

**Because.** The 27B's captures measured ~3/10 durable: alongside real
preferences it wrote "is currently working on the second-brain project"
(project state), "has not used Claude or Qwen lately" (true for a week), and
four rewordings of one fact. Tightening its prompt fights the model's ceiling;
the same rule as D5 says extraction is its job and judgement is not. Given the
same ten sentences, the compile pass kept two, dropped both temporal lines,
merged the duplicates, and generalised "wants a daily cron" into a durable
preference for automation over manual steps — which is the transformation the
27B cannot be asked for.

This also answers the cost question the shape invites: the note is 10KB and
only the card is injected, so captured lines cost nothing until they are
promoted. Low precision in the auto section is a readability problem, not a
token problem. The card itself went 1089 -> 1489 chars (~272 -> ~372 tokens)
for two bullets, and that is the only figure that recurs every session.

**Don't.** Promote captured lines automatically, and don't let the capture hook
write into the card. The section header states the promise that a machine line
never reaches the injected card on its own; that promise is what makes running
a small model broadly safe in the first place.

---

## D12. The mirror reconciles state instead of learning every capture path

**Decision.** `obsidian-mirror.py` keeps its PostToolUse trigger, but the
guarantee now comes from a second mode, `--reconcile`, wired on `Stop`: it
diffs mem-lite against the vault for the session's project and writes whatever
is missing. The capture path stopped mattering.

**Because.** The PostToolUse hook fires on the `mem_save` *tool*. mem-lite is
equally reachable as `cli.mjs save`, and the agent picks that whenever the
`mem_*` MCP tools sit behind a tool search — one Bash call instead of two, which
the project's own instructions recommend. Three real saves went that way on
2026-08-27 (#441, #442, #443): all three landed in SQLite, none reached the
vault, and nothing reported a failure, because from the hook's side no event
had occurred. Teaching the hook to also recognise `cli.mjs save` would be a
string match on a command line, and a fourth path — a plugin, an editor
integration, a future CLI verb — would break it in exactly the same silence.
Comparing the two stores has no such surface: present in one and absent in the
other is the whole condition.

The pass is scoped to the session's own project and to seven days. Scope,
because the catch-all classification cannot be redone from a database row —
mem-lite keys a repo-less session by whatever directory it started in, and only
a live `cwd` says whether that should be folded in. Window, because mem-lite
held 305 unmirrored rows at the time, nearly all of them predating the mirror;
back-filling those would empty weeks of history into a vault whose contract is
what is true now. Each project heals itself the next time it is opened.

**Don't.** Give the reconcile a second way to identify an entry. It finds one
by the `(#id)` stamp the writer emits, which is why both modes share
`write_entry()` — a second spelling would make every mirrored row look missing
and duplicate the entire log on every session end.

---

## D13. The catch-all stays one folder and carries its scope per entry

**Decision.** A catch-all entry is stamped with the directory the save was made
in: a short tag in the heading (`11:20 — fos-deploy · [decision] …`) and the
`~`-relative path on a `Scope:` line. Repo folders get neither.

**Because.** The catch-all collects every repo-less session by design, so one
day file can hold a self-hosted MDM PoC and a bringup session driving a remote
machine with nothing above the body text to separate them. That is not a
cosmetic problem: `_mem-log/genel/` compiles into a single note whose card is
injected at the start of every repo-less session, and that card was measured at
100% MDM content — a session about anything else was being handed five bullets
about a tablet fleet.

Naming the folders instead is the obvious fix and the wrong one: it is the
per-directory sharding that D-catch-all removed, where each shard falls under
the note threshold, none is ever compiled, and all of it goes unreachable.
A repo's folder name already answers "where did this come from"; only the
catch-all is missing the answer, so only the catch-all pays for it.

**Don't.** Try to record which remote host the work touched. This hook runs
locally and cannot know it. The model writing the save does, and does record
it — the entry that prompted this names the host, its IP, and the three
diverging compose files it found there, all in the body. The directory is the
only locator the hook can state as fact, so it is the only one it states.

---

## D14. Sources are classified by lookup, on two axes, not by a per-record flag

**Decision.** `vault_common.classify_project()` and `classify_vault_path()`
answer `(audience, sensitivity)` for any source in the corpus, derived from the
mem-lite project key and the vault's top-level folder. Nothing is written per
record. Personal is the closed set and everything else is work; an unlisted
source gets `("coding", "work")`.

**Because.** Two different questions were being asked of one corpus and neither
had an answer on disk.

*Audience.* Measured on the live vault: 324KB of work-repo notes, 133KB of
`_mem-log` lessons, 54KB of project docs — against 71KB of life material, of
which the load-bearing part is one 229-line profile note. ~88% of this corpus is
coding telemetry. That is exactly what a coding agent should retrieve and
exactly what a general or voice assistant must not: asked who the user is, it
would surface that the ENC gRPC port is 50053. The same measurement says what
such an assistant is missing and a scope map cannot supply — there is no event
stream. `01-Daily Notes` holds one note. Identity is covered; "what happened
today, what do I owe whom" is not captured anywhere, and that is a collection
gap, not a classification gap.

*Sensitivity.* The vault is private today and may be opened later (D#10). The
cost of carrying the split now is one line per repo; the cost of reconstructing
it later is a human pass over 175 notes and 359 observations.

**Why a lookup and not a column.** A per-record flag means a model classifying
every save. It mislabels silently — a wrong label here is only discovered on the
day it matters — and it answers for nothing already written, so 359 existing
rows would need a retrofit pass by the same unreliable classifier. The project
key and the folder are facts already on disk, they cannot drift, and they
classify the whole backlog the moment the map exists.

**Why personal is the closed set.** New repos are overwhelmingly work. Listing
personal instead means a repo nobody remembered to classify still reads as
sensitive; the cost of that mistake is an unnecessary review. Inverted, the cost
is a missed one. Only one of those is recoverable.

**Don't.** Read this as a leak control. It classifies provenance, not content: a
personal repo's session that spent its time inside a work repo still lands under
the personal key, and the secret scan that prompted this found a credential in a
machine-written `_mem-log` day file — which this map labels `mixed` and does not
redact. Content-level leakage needs a scan on the opening-up day. A map that is
mistaken for one has made things worse than no map at all.

## D15. Vault note writes are locked, and the lock covers read-decide-write

**Decision.** `vault_common.locked_note()` opens a note `a+` under
`fcntl.flock(LOCK_EX)` and hands the caller that handle already positioned at
the start. personal-capture, concept-capture and obsidian-mirror's `write_entry`
all append through it now, and the lock spans their whole read-decide-write, not
just the final `open(path, "a")`.

**Because.** Three hooks append to a handful of shared notes from Stop/PostToolUse,
which two hosts (Claude, Qwen) or two overlapping sessions can fire at the same
moment, and nothing serialized that. An unlocked pair of concurrent appends can
interleave mid-write and corrupt the note, not just duplicate a line.

**Why the whole section, not just the write.** Locking only the final append
still races on the decision before it: two readers can each read the note before
either has written, both decide the same fact or line is new, and both append it.
The lock has to be acquired before the read that the write depends on.

**Proof, not a claim.** A single-process unit test cannot exercise a multi-process
race. `test_hooks.sh` spawns 8 real concurrent processes writing through
`locked_note()` and asserts both that no line was lost and that no line
interleaved with another's (a malformed line, not a missing one, is what
corruption looks like under `flock`).

## D16. vault-lint is a script, not a rule the agent re-derives per session

**Decision.** `tools/vault-lint.py` runs the three checks CLAUDE.md Section 5
and Vault Standards.md's "Haftalık Bakım" already name — stale `compiled: false`
raw notes, broken `[[wikilink]]`s, and "sınıfsız/başlıksız" notes (root-level
strays, `07- Raw` notes missing required frontmatter, `08- Wiki` notes with no
heading) — as a standalone, read-only script. It never edits or deletes; that
stays a human decision, same contract as the two auto-capture hooks' own
sections.

**Because.** Those three checks were previously "an agent reads the vault by
hand when asked." That means the check only happens when someone remembers to
ask, and its cost grows with the vault every time it does run. A script costs
the same on week 40 as on week 4.

**What it deliberately excludes as a link source.** `04- Templates` (its
wikilinks are literal placeholder text, e.g. `[[...]]`) and `_mem-log` (raw
session mirror text, "elle düzenlenmez" — a session discussing wikilink syntax
in prose logs as a literal `[[link]]` with nothing to curate it out). A folder's
own index note (name matches its parent folder, e.g. `07- Raw/07- Raw.md`) is
exempt from the raw-source frontmatter contract — it is a MOC, not a source.

**Why link resolution checks attachments, not just notes.** `[[Foo.png]]`
resolves the same way a note link does — by basename, against anything in the
vault. Checking only `.md` files made every image embed a false positive.

**The fourth check, added same day: stale auto-captures.** personal-capture
and concept-capture already stamp every line `<!-- auto:DATE -->`, but the
stamp was only ever read for dedup, never for age — nothing asked whether an
unpromoted line was still true. `stale_auto_captures()` reports any stamped
line sitting in its `Otomatik Yakalananlar` section past a 45-day window
(longer than the 14-day raw-note window: an auto-capture is already a
probably-true fact pending a promote/delete decision, not an unread source).
This is CLAUDE.md's own OKM policy (vault Section 2c) made checkable: every
piece of vault knowledge is timeless (hand-written prose, never expires),
dated (an auto-capture — needs a decision eventually), or a pointer
(`_mem-log`, never itself evaluated). The check finds the second class going
unreviewed; it never deletes, same contract as the other three.

**Caught in cross-review (2026-08-29):** the fourth check shipped without the
`04- Templates` / `_mem-log` exclusion the first three already had. A session
that explains the `<!-- auto:DATE -->` stamp format in prose (this decision
entry is itself an example) gets mirrored into `_mem-log` verbatim, stamp
syntax included — and a real date in that sentence would eventually cross the
45-day window and get reported as an unpromoted capture that was never a
capture at all. Same exclusion as `broken_backlinks()`, added to
`stale_auto_captures()` too, with a test that plants exactly this sentence in
a synthetic `_mem-log` file and asserts it never surfaces.
