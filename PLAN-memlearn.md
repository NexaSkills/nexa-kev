# PLAN: nexa-kev v0.4.0 — self-learning memory lanes (memlearn + memrecall)

Status: **PROPOSED — awaiting Jan's sign-off.** No code changed yet.
Author: Devon, 2026-11-10.

Goal: make agents **auto-learn** (catch learning moments and persist them in
the right bank) and **auto-recall** (surface stored memories exactly where
they change behavior — including mid-task), with kev making the
save/bank decisions honestly (median + spread doctrine) and every new lane
kill-switchable and fail-open.

---

## 1. State of the art today (audited, with evidence)

What already exists — the memory system is NOT write-only:

| Capability | Where | Default | Notes |
|---|---|---|---|
| Passive recall injection per user turn | `mnemo-pg2/plugin_entry.py:343` `_pre_llm_call` — top-3 private + top-2 shared via `Engine.recall(user_message)` | ON (`MNEMO2_PREFETCH` default 1) | Query = raw user message |
| Prefetch via memory-provider lane | `mnemo-pg2/memory_provider.py:266` `prefetch()` — 5 private + 3 shared (`bank=surface`) | on provider path | same query weakness |
| Raw turn auto-capture | `memory_provider.py:315` `sync_turn` (sanitized, ledger-idempotent, `source=auto`, importance 0.3, session scope) | code OFF, rollout pins `MNEMO2_AUTO_CAPTURE=1` (see `scripts/apply_surface_fixes.sh:96`) | transcript store, NOT learned rules |
| Shared-vs-private SAVE gate | `nexa-kev/memroute.py` (pre_tool_call, kev ensemble + hard secret gate) | ON (v0.3.0) | only fires when the agent already chose a shared-save tool |
| Session-end consolidation | `mnemosyne_sleep` auto lanes | env-gated | hygiene, not learning |
| Advisory per user message | `nexa-kev/advisory.py` (pre_llm_call, 1 gateway call, skill/tool choice probs) | ON | ⚡ line |

Host contract facts (verified in the installed host tree):
* `pre_llm_call` fires **once per user turn** with `user_message` AND
  `conversation_history` (host: `agent/turn_context.py:862-875`). Returned
  `{"context": ...}` is injected into the **user message**, never the system
  prompt. → we CAN see the last assistant message (needed to judge
  "yes, do it like that" replies).
* `transform_tool_result` (`model_tools.py:862-877`): runs after
  `post_tool_call`, before the result enters context; receives
  `status/error_type/error_message`; first string return wins → a plugin can
  **append a trailer to a tool result mid-task**. This is our mid-turn
  injection channel.
* `post_tool_call` is an observer (audit only).
* `pre_verify` is a "continue" nudge but fires only on code-edit turns —
  not a general save-enforcement lever.

## 2. Gap analysis (the real deficits)

* **G1 — learning moments are nobody's job.** When a user says "yes, that
  was good, do it like that from now on" (or states a new rule), nothing
  recognizes the speech act. Persistence depends on the agent happening to
  call `mnemosyne_remember`. This is the #1 hole: corrections expire with
  the session.
* **G2 — recall is passive and shallow.** Every turn gets a similarity
  injection keyed on the raw message — but there is no DEEP recall when the
  passive block misses on an important task, and ZERO mid-task recall:
  after 15 tool calls the agent never thinks to query its own memory,
  especially on repeated failure patterns it already solved.
* **G3 — bank choice at save time is unguided.** memroute only catches a
  shared-save *after* the agent picked the shared tool; the save-time
  "private or shared?" recommendation that should come BEFORE the choice is
  convention only (`memroute.py:14-15` admits the recall side is
  "documented convention").
* **G4 — duplicate sprawl.** No recall-before-save discipline, so
  "do it like that" rules accumulate as near-duplicates instead of
  `mnemosyne_update` on the existing row.

## 3. Design — three lanes, one module each

### L1 `memlearn.py` — learning-moment capture (pre_llm_call, saves side)

Flow per user turn (registered beside the advisory hook; message-hash dedup):

1. **Lexical prefilter (local, 0 ms, 0 kev):** regex for the durable-
   instruction speech act — `from now on|going forward|always|never (do|
   again)|next time|that was (good|right|correct)|well done.*do it like|
   don't do it like|remember (that|this)|prefer|stop doing|rule[: ]|
   always remember…` (+ NL equivalents). Miss → lane fully silent, zero
   gateway cost. This keeps steady-state latency at ~0 for normal chat.
2. **kev ensemble decision** (one `decide()` call, 3 framings — same
   machinery as memroute):
   * Q1 "Is the message+context a durable instruction/preference/correction
     the agent should follow in FUTURE sessions, not just today?"
   * Q2 "Is this fact true and useful for EVERY agent (shared bank), or
     specific to this agent (private bank)?" — criteria text SHARED verbatim
     with memroute's existing question (one source of truth in config).
   Gate: verdict YES with spread ≤ 0.2 (existing ensemble doctrine);
   UNCERTAIN/NO → silent, `kind=memlearn` audit row either way.
3. **Directive injection** (only on confident YES), prepended context block:
   ```
   🧠 [nexa-kev memlearn] Learning moment detected (kev <p>, bank=<private|shared>).
   Persist it NOW: first mnemosyne_recall("<topic>") to find an existing
   similar memory — if one exists, mnemosyne_update it instead of adding a
   duplicate. Otherwise save with mnemosyne_remember (private) or
   mnemosyne_shared_remember (shared), importance 0.7, scope persistent,
   as ONE declarative rule sentence (state the rule, not the incident).
   ```
   The AGENT performs the write; memroute keeps guarding shared misuse;
   mnemo's `audit_log` keeps recording it (writes through the tool surface).
4. Noise control: max 3 directives per session (learning moments are rare;
   a 4th is logged-only), per-message hash dedup, kill-switch
   `NEXA_KEV_MEMLEARN=0`.

### L2 `memrecall.py` — mid-task recall (transform_tool_result + post_tool_call, recall side)

1. **Pattern prefilter on tool results** (0 kev): `status=error`,
   traceback/`exit_code!=0`/permission-denied/timeout patterns, and
   empty-result-where-output-expected. Budget: max 2 injections per session,
   query-hash dedup (same error twice = recall once).
2. On hit: **direct recall probe** — query = compact error summary
   (tool name + error line). Private bank first (`bank` default), shared
   bank on private miss. Execution path: duck-typed discovery of mnemo-pg2's
   in-process dispatch via `sys.modules` (module whose `__file__` is under
   `/mnemo-pg2/` exposing `_dispatch`); fail-INERT (lane no-ops) when the
   sibling isn't loaded — never a direct PG connection from nexa-kev
   (schema resolution stays mnemo-pg2's single lane, P3L discipline).
3. On recall hits: append to the tool result trailer
   `--- Related memory (nexa-kev recall): … ---` (≤3 lines). No hits →
   result untouched. `kind=memrecall` audit row (query, hits, ms).
   Kev's role here is deliberately ZERO on the error lane (an error is an
   unambiguous recall trigger; kev would add 1-3 s for no decision value).
   For ambiguous "surprise" results we keep the door open via
   `NEXA_KEV_MEMRECALL_KEVGATE=1` (kev gates the recall question) — off by
   default, evaluated during shadow phase.
4. The passive per-turn injection (mnemo-pg2 prefetch) is NOT duplicated —
   L2 is the deep/mid-task layer on top of it.

### L3 bank policy — unify, don't invent

`memlearn` Q2 + `memroute` gate share one criteria constant; README gains a
"Save/Recall policy" section replacing today's convention-only note:
private = own habits/history/prefs/tasks; shared = fleet-wide true for every
agent; unsure → kev decides, and kev unsure → private (safe default).

### Explicitly NOT in v0.4.0 (documented, deferred)

* **Hook-side direct writes** (hook itself saves when the agent didn't):
  bypasses the audit hook and mnemo's P3L binding discipline; cross-plugin
  `sys.modules` reaching is fragile across boots. Option kept as an
  env-gated spike (v0.4.x) IF shadow data shows agents ignore directives
  often. Decision knob below.
* Fleet-wide `MNEMO2_AUTO_CAPTURE` changes (already pinned by mnemo repo).
* Query reformulation/expansion (noul answers questions; it writes no text).

## 3b. Vendor-neutrality contract (HARD requirement, added per Jan 2026-11-10)

**We never modify Hermes vendor code, and a future Hermes update must not
break these lanes.** Architecture rules this design is bound by:

1. **Public plugin API only.** Everything goes through what `register(ctx)`
   gives us: `ctx.register_tool` + `ctx.register_hook` for hook names in
   the host's published `VALID_HOOKS` set (`pre_llm_call`,
   `pre_tool_call`, `post_tool_call`, `transform_tool_result`). Zero host
   file edits, zero monkey-patching, zero `sys.path` mutations, zero
   imports of host-private internals on any hot path. (Same discipline
   mnemo-pg2/lcm2-pg already ship — vendor-parity, hooks-only.)
2. **Defensive kwargs everywhere.** Every hook payload field is read with
   `.get()` + fallbacks (e.g. `user_message or message`; tool-status
   derived from the result dict if the host doesn't pass `status`). A
   Hermes update that renames/adds fields degrades the lane to
   silent-fail-open, never a crash and never wrong behavior.
3. **Line numbers in §1 are evidence, not dependencies.** They document
   today's behavior for reviewers; the code depends only on the return
   contracts (`{"context"}` injection, tool-result string passthrough),
   which are the plugin-authoring surface.
4. **Siblings are OUR code, not vendor.** L2's recall probe reaches
   mnemo-pg2 (same repo family) via capability probing — try
   `handle_tool_call`, then `_dispatch`, on a module identified as
   mnemo-pg2 by `__file__` — and is INERT when the probe fails. A
   contract test in `lcm2-mnemo2-pg`'s suite pins the probed entrypoints
   so a rename there fails OUR checker, not production.
5. **Update-robustness is tested, not assumed.** The offline suite runs
   hooks with minimal/host-shaped/extra-field kwargs variants (must all
   behave), and `check_nexa_kev.sh` gained a contract probe: boot log
   shows the new hook registrations + a direct call of each lane function
   returns cleanly. A future Hermes update that removes a hook name makes
   the checker report the lane dormant — loudly, before traffic.
6. **Contained blast radius.** Kill-switch + shadow flag per lane, config
   re-read per call: any post-update surprise is one env var away from
   off, with no code rollback needed.

## 4. Config surface (all `NEXA_KEV_*` / `plugins.entries.nexa-kev.*`)

| Key | Default | Meaning |
|---|---|---|
| `MEMLEARN` | 1 | L1 on/off (kill) |
| `MEMLEARN_SHADOW` | rollout-phase: 1 | log verdicts, inject nothing |
| `MEMLEARN_MAX_SESSION` | 3 | directive cap per session |
| `MEMRECALL` | 1 | L2 on/off (kill) |
| `MEMRECALL_MAX_SESSION` | 2 | injection cap per session |
| `MEMRECALL_KEVGATE` | 0 | kev-gate ambiguous recalls (experiment) |
| `MEMROUTE` | 1 | existing gate, unchanged |

All lanes: fail-open on any error (gateway down → NO DECISION → silent),
every decision → `<profile>/plugins-data/nexa-kev/decisions.jsonl`
(`kind=memlearn|memrecall`), kill-switch honored at call time (config
re-read per call — proven pattern).

## 5. Latency budget

* Normal chat turn: +0 s (both lanes prefilter-gated locally).
* Learning-candidate message: one ensemble call ~1-3 s (rare by design).
* Error tool result: one recall probe ~0.2-0.5 s, ≤2/session.
* Hook callback timeout 30 s (host bound) — our internal deadline is 5 s;
  overdue → silent fail-open.

## 6. Failure modes + mitigations

| Failure | Mitigation |
|---|---|
| False-positive "learning" (small talk matched) | UNCERTAIN→silent + spread rule + session cap + shadow phase measures FP rate from decisions.jsonl |
| Agent ignores the directive | directive rides the user message every turn it's detected; (spike lane if shadow shows a pattern) |
| Duplicate rule rows | directive mandates recall-before-save + update over add |
| Shared-bank pollution/secrets | unchanged memroute hard gates (L1 only *recommends* the bank) |
| Prompt-injection via content draft | directive is plugin-authored; user text only fills the proposed rule sentence |
| mnemo sibling missing | L2 inert, one log line; no PG code in nexa-kev |
| Latency regression | prefilter-gated; measured in shadow; per-lane kill |

## 7. Implementation plan (repo-first, tests-first)

Work on `main` via `feat/memlearn` branch in `~/Projects/nexa-kev`.

1. `plugin/nexa-kev/memlearn.py` + `memrecall.py` + config keys + hook
   registration in `__init__.py`; shared criteria constant; bump 0.4.0.
2. Offline pytest suite (`tests/test_memlearn.py`, `tests/test_memrecall.py`)
   run with the agent venv python + `HERMES_HOME=<profile>` (known harness
   pitfall): prefilter table (12 pos / 12 neg phrases), monkeypatched
   `decide()` → YES/NO/UNCERTAIN paths, bank recommendation, session caps,
   shadow mode silent-but-logged, kill-switches, sibling-missing inertness,
   trailer append contract on fake error results.
3. `scripts/check_nexa_kev.sh`: version bump + new boot log lines + offline
   suite call.
4. README: policy section, new env table, verification prompts (P0 offline
   decide probe, P1 chat learning-moment scripted line + audit_log SELECT,
   P2 fresh-session recall of the saved rule, P3 induced-error recall
   trailer), clone-prep duty unchanged.
5. Shadow-verify on buddyos-nexa + buddyos-template (MEMLEARN_SHADOW=1,
   ~48 h of real traffic + scripted P1-P3), review FP/FN from
   decisions.jsonl, flip shadow off, then fleet roll (clone, trade): git
   pull + install script + gateway bounce + checker PASS quoted.
6. Follow-up memory/skill updates (nexa-kev-plugin skill §Memory lanes).

Estimated: 2 dev-days + 2 days shadow soak. Rollback = env kill per lane;
worst case git-pull previous tag.

## 8. Success criteria (measured, not asserted)

* P1: scripted "yes, that was good, do it like that from now on" →
  directive injected AND a persistent row in `audit_log` within the turn.
* P2: new session, related task → the rule appears in the passive
  injection block (and drives behavior — quoted in reply).
* P3: induced known-error pattern → recall trailer appears under the tool
  result when a matching memory exists; absent budget spent.
* Shadow metrics: memlearn FP < 20% of firings; ≤3 directives/session p95;
  zero added latency on prefilter-miss turns (log `ms` field proves it).
